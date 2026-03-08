"""
SkyChat Python Client — ncurses TUI  (Dracula theme)
======================================================
Layout:
  ┌──────────┬─────────────────────────┬──────────┐
  │ Channels │      Chat messages      │  Users   │
  │  (left)  │       (centre)          │ (right)  │
  │          ├─────────────────────────┤          │
  │          │    Input box            │          │
  └──────────┴─────────────────────────┴──────────┘

Tab cycles focus:  INPUT → ROOMS → USERS → INPUT …
  • ROOMS  focus : ↑/↓ move cursor, Enter joins room
  • USERS  focus : ↑/↓ move cursor, Enter opens DM
  • INPUT  focus : type / edit / send messages

History is auto-fetched on room join.
Default room is id=0.

Dependencies:  pip install websockets
Usage:
    skychat                          # ncurses login prompt
    skychat <username> <password>    # skip prompt
"""

import asyncio
import subprocess
import curses
import json
import os
import re
import struct
import sys
import textwrap
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("Missing dependency. Install with:  pip install websockets")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_WSS_URL = "wss://skych.at/api/ws"
DEFAULT_ROOM_ID = 0
CONFIG_FILE     = os.path.expanduser("~/.skychat_tui.json")


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    try:
        existing = _load_config()
        existing.update(data)
        with open(CONFIG_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass


BINARY_MSG_AUDIO  = 0
BINARY_MSG_CURSOR = 1


FB_BG     = curses.COLOR_BLACK
FB_FG     = curses.COLOR_WHITE
FB_ACCENT = curses.COLOR_MAGENTA
FB_CYAN   = curses.COLOR_CYAN
FB_GREEN  = curses.COLOR_GREEN
FB_YELLOW = curses.COLOR_YELLOW
FB_RED    = curses.COLOR_RED

C_BASE        = 1
C_HEADER      = 2
C_ITEM_ACTIVE = 3
C_ITEM_IDLE   = 4
C_ITEM_CURSOR = 5
C_USERNAME    = 6
C_TIMESTAMP   = 7
C_STATUS      = 8
C_INPUT       = 9
C_SELF        = 10
C_ERROR       = 11
C_LOGIN_FIELD = 12
C_LOGIN_LABEL = 13
C_LOGIN_BTN   = 14
C_LOGIN_BTN_S = 15
C_BORDER      = 16
C_USER_ONLINE = 17
C_USER_AFK    = 18
C_USER_RECENT = 19
C_MSG_SELECT  = 20
C_DYN_BASE    = 32


# ─────────────────────────────────────────────────────────────────────────────
# Focus
# ─────────────────────────────────────────────────────────────────────────────

class Focus(Enum):
    INPUT = auto()
    ROOMS = auto()
    USERS = auto()

FOCUS_ORDER = [Focus.ROOMS, Focus.INPUT, Focus.USERS]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

TAG_RE     = re.compile(r'<[^>]+>')
STICKER_RE = re.compile(r':([A-Za-z0-9_-]+):')
URL_RE     = re.compile(r'https?://\S+')


def _hex_to_xterm256(hex_color: str) -> int:
    try:
        h = hex_color.lstrip('#')
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        ri, gi, bi = round(r/255*5), round(g/255*5), round(b/255*5)
        if ri <= 1 and gi <= 1 and bi <= 1:
            return 241
        return 16 + 36*ri + 6*gi + bi
    except Exception:
        return 117


def _strip_tags(s: str) -> str:
    return TAG_RE.sub('', s)


def _char_width(ch: str) -> int:
    """Return the terminal display width of a single character (1 or 2)."""
    import unicodedata
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ('W', 'F') else 1


def _str_cols(s: str) -> int:
    """Return the total terminal column width of string s."""
    return sum(_char_width(ch) for ch in s)


def _cols_slice(s: str, max_cols: int) -> str:
    """Return the longest prefix of s that fits within max_cols terminal columns."""
    cols = 0
    result = []
    for ch in s:
        w = _char_width(ch)
        if cols + w > max_cols:
            break
        result.append(ch)
        cols += w
    return ''.join(result)




def _parse_reactions(storage: dict) -> dict:
    if not storage or not isinstance(storage, dict):
        return {}
    reactions = storage.get("reactions", storage)
    if not isinstance(reactions, dict):
        return {}
    out = {}
    for k, v in reactions.items():
        if isinstance(v, list):
            out[k] = len(v)
        elif isinstance(v, (int, float)):
            out[k] = int(v)
    return out


AFK_SECONDS = 5 * 60


def _user_status(session: dict):
    import time as _time
    rooms = session.get('rooms') or []
    if not rooms:
        return '○', C_USER_RECENT
    last = session.get('lastInteractionTime')
    if last is not None:
        try:
            if _time.time() - float(last) > AFK_SECONDS:
                return '◐', C_USER_AFK
        except Exception:
            pass
    return '●', C_USER_ONLINE


def _parse_msg_ts(msg: dict) -> str:
    for key in ('date', 'createdTimestamp', 'createdAt', 'timestamp', 'time', 'created_at'):
        raw = msg.get(key)
        if raw is None:
            continue
        try:
            v = float(raw)
            if v > 4_102_444_800:
                v /= 1000
            return datetime.fromtimestamp(v).strftime('%H:%M')
        except Exception:
            continue
    return datetime.now().strftime('%H:%M')


# ─────────────────────────────────────────────────────────────────────────────
# SkyChatClient — WebSocket layer
# ─────────────────────────────────────────────────────────────────────────────

class SkyChatClient:
    MAX_RECONNECT_DELAY = 30.0
    RECONNECT_JITTER    = 1.0

    def __init__(self, url: str = DEFAULT_WSS_URL, *, auto_message_ack: bool = False):
        self.url              = url
        self.auto_message_ack = auto_message_ack

        self._ws:              Optional[Any]   = None
        self._user:            Dict            = {"id": 0, "username": "*Guest", "right": -1}
        self._token:           Optional[Dict]  = None
        self._rooms:           List[Dict]      = []
        self._current_room_id: Optional[int]  = None
        self._connected_list:       List[Dict] = []
        self._connected_list_dirty: bool       = False
        self._config:          Optional[Dict]  = None
        self._polls:           Dict[int, Dict] = {}

        self._reconnect_attempts: int        = 0
        self._is_reconnecting:    bool       = False
        self._message_queue:      List[Dict] = []
        self._max_queue_size:     int        = 100
        self._running:            bool       = False

        self._typing_list:   List[str]              = []
        self._typing_active: bool                   = False
        self._typing_task:   Optional[asyncio.Task] = None
        self._handlers:      Dict[str, List[Callable]] = {}

        self.on("set-user",       self._on_set_user)
        self.on("auth-token",     self._on_auth_token)
        self.on("config",         lambda _: None)
        self.on("room-list",      lambda r: setattr(self, '_rooms', r))
        self.on("join-room",      lambda rid: setattr(self, '_current_room_id', rid))
        def _on_connected_list(s):
            self._connected_list = s
            self._connected_list_dirty = True
        self.on("connected-list", _on_connected_list)

        def _on_connected_list_patch(patch):
            """Merge patch entries into the connected list by identifier."""
            if not isinstance(patch, list):
                return
            by_id = {s.get('identifier'): i for i, s in enumerate(self._connected_list)}
            for entry in patch:
                ident = entry.get('identifier')
                if ident in by_id:
                    self._connected_list[by_id[ident]] = entry
                else:
                    self._connected_list.append(entry)
            self._connected_list_dirty = True
        self.on("connected-list-patch", _on_connected_list_patch)
        self.on("typing-list",    lambda users: setattr(self, '_typing_list',
                                     [u.get('username', '?') if isinstance(u, dict) else str(u)
                                      for u in (users or [])]))
        self.on("message",        self._on_message_internal)

        def _on_message_seen(data):
            """Keep own lastseen in sync so has_unread_messages() stays accurate."""
            if not isinstance(data, dict):
                return
            uid      = data.get("user")
            seen_map = data.get("data")
            if not isinstance(seen_map, dict):
                return
            if uid == self._user.get("id"):
                plugins = self._user.setdefault("data", {}).setdefault("plugins", {})
                if not isinstance(plugins.get("lastseen"), dict):
                    plugins["lastseen"] = {}
                plugins["lastseen"].update(seen_map)
        self.on("message-seen", _on_message_seen)

    # ── Event emitter ──────────────────────────────────────────────────

    def on(self, event: str, handler: Optional[Callable] = None):
        """Register a handler. Works as a direct call or @decorator."""
        if handler is not None:
            self._handlers.setdefault(event, []).append(handler)
            return self
        def decorator(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn
        return decorator

    def off(self, event: str, handler: Callable) -> "SkyChatClient":
        if event in self._handlers:
            self._handlers[event] = [h for h in self._handlers[event] if h is not handler]
        return self

    def _emit(self, event: str, payload: Any = None) -> None:
        for h in list(self._handlers.get(event, [])):
            try:
                r = h(payload)
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception:
                pass

    def _on_set_user(self, user: Dict) -> None:
        self._user = user

    def _on_auth_token(self, token) -> None:
        self._token = token
        if token:
            _save_config({"token": token})

    def _on_message_internal(self, msg: Dict) -> None:
        if self.auto_message_ack and self._user.get("id", 0) != 0:
            asyncio.ensure_future(self._ack(msg.get("id", 0)))

    async def _ack(self, mid: int) -> None:
        await self.send_message(f"/lastseen {mid}")

    # ── WebSocket lifecycle ────────────────────────────────────────────

    async def connect(self) -> None:
        self._running = True
        await self._open_connection()

    async def _open_connection(self) -> None:
        try:
            async with websockets.connect(self.url) as ws:
                self._ws = ws
                await self._on_ws_open()
                await self._receive_loop()
        except Exception as exc:
            await self._on_ws_close(1006, str(exc))

    async def _on_ws_open(self) -> None:
        was_recon                = self._is_reconnecting
        self._reconnect_attempts = 0
        self._is_reconnecting    = False
        if was_recon:
            self._emit("reconnected")
        if self._token:
            await self._send_raw(json.dumps({"token": self._token}))
        await self._replay_queue()
        self._emit("ws-open")

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                await self._on_raw(raw)
        except ConnectionClosed as exc:
            code   = exc.rcvd.code   if exc.rcvd else 1006
            reason = exc.rcvd.reason if exc.rcvd else ""
            await self._on_ws_close(code, reason)
        except Exception as exc:
            await self._on_ws_close(1006, str(exc))

    async def _on_raw(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            await self._on_binary(raw)
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        ev = data.get("event", "")
        if ev:
            self._emit(ev, data.get("data"))
        elif isinstance(data, dict) and "error" in data:
            self._emit("error", data["error"])
        else:
            self._emit("connection-accepted", data)

    async def _on_binary(self, data: bytes) -> None:
        if len(data) < 2:
            return
        t = struct.unpack_from("<H", data, 0)[0]
        if t == BINARY_MSG_AUDIO and len(data) >= 6:
            self._emit("audio", {"id": struct.unpack_from("<I", data, 2)[0], "data": data[6:]})
        elif t == BINARY_MSG_CURSOR and len(data) >= 14:
            uid  = struct.unpack_from("<I", data, 2)[0]
            x, y = struct.unpack_from("<ff", data, 6)
            ent  = next((e for e in self._connected_list
                         if e.get("user", {}).get("id") == uid), None)
            self._emit("cursor", {
                "user": ent["user"] if ent else {"id": uid, "username": "?"},
                "x": x, "y": y,
            })

    async def _on_ws_close(self, code: int = 0, reason: str = "") -> None:
        self._ws              = None
        self._current_room_id = None
        self._polls           = {}
        self._emit("connection_lost", {"code": code, "reason": reason})
        if not self._running or code == 4403:
            return
        await self._reconnect()

    async def _reconnect(self, immediate: bool = False) -> None:
        import random
        self._is_reconnecting = True
        if immediate:
            self._reconnect_attempts = 0
            await self._open_connection()
            return
        delay = (min(1.0 * (2 ** self._reconnect_attempts), self.MAX_RECONNECT_DELAY)
                 + random.random() * self.RECONNECT_JITTER)
        self._reconnect_attempts += 1
        self._emit("reconnecting", {"attempt": self._reconnect_attempts, "delay": delay})
        await asyncio.sleep(delay)
        await self._open_connection()

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        self._ws = None

    # ── Send ──────────────────────────────────────────────────────────

    async def _send_raw(self, data: Any) -> None:
        if not self._ws:
            if isinstance(data, str) and len(self._message_queue) < self._max_queue_size:
                self._message_queue.append({"type": "raw", "data": data})
            return
        await self._ws.send(data)

    async def _send_event(self, ev: str, payload: Any) -> None:
        if not self._ws:
            if len(self._message_queue) < self._max_queue_size:
                self._message_queue.append({"type": "event", "eventName": ev, "data": payload})
            return
        await self._ws.send(json.dumps({"event": ev, "data": payload}))

    async def _replay_queue(self) -> None:
        if not self._message_queue:
            return
        q, self._message_queue = self._message_queue, []
        for item in q:
            if item["type"] == "raw":
                await self._send_raw(item["data"])
            else:
                await self._send_event(item["eventName"], item["data"])

    # ── Public API ────────────────────────────────────────────────────

    async def authenticate(self, auth_data: Dict) -> None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        def _ok(_):
            if not fut.done(): fut.set_result(True)
        def _err(msg):
            if not fut.done(): fut.set_exception(RuntimeError(msg))
        self.on("connection-accepted", _ok)
        self.on("error", _err)
        await self._send_raw(json.dumps(auth_data))
        try:
            await asyncio.wait_for(fut, timeout=15)
        finally:
            self.off("connection-accepted", _ok)
            self.off("error", _err)

    async def login(self, username: str, password: str,
                    room_id: Optional[int] = None) -> None:
        p: Dict[str, Any] = {"credentials": {"username": username, "password": password}}
        if room_id is not None:
            p["roomId"] = room_id
        await self.authenticate(p)

    async def login_as_guest(self, room_id: Optional[int] = None) -> None:
        p: Dict[str, Any] = {}
        if room_id is not None:
            p["roomId"] = room_id
        await self.authenticate(p)

    async def register(self, username: str, password: str) -> None:
        await self.authenticate({
            "credentials": {"username": username, "password": password, "register": True}
        })

    async def join(self, room_id: int) -> None:
        self._current_room_id = room_id
        await self.send_message(f"/join {room_id}")

    async def send_message(self, msg: str) -> None:
        await self._send_event("message", msg)

    async def open_dm(self, username: str) -> None:
        """Open a DM with username.

        1. If a private room already exists whose whitelist/participants are
           exactly {self, username}, join it directly.
        2. Otherwise send /pm <username> and wait for the room-list to update,
           then join the newly created room.
        """
        own    = self._user.get("username", "").lower()
        target = username.lower()

        def _find_existing() -> Optional[int]:
            for room in self._rooms:
                if not room.get("isPrivate"):
                    continue
                wl = room.get("whitelist") or room.get("allowedUsers") or []
                if wl:
                    members = set()
                    for u in wl:
                        if isinstance(u, dict):
                            members.add(u.get("username", "").lower())
                        elif isinstance(u, str):
                            members.add(u.lower())
                    if members == {own, target}:
                        return room.get("id")
                name = room.get("name", "")
                if name.lower().startswith("pm:"):
                    parts = {p.strip().lower() for p in name[3:].split(",")}
                    if parts == {own, target}:
                        return room.get("id")
            return None

        existing = _find_existing()
        if existing is not None:
            await self.join(existing)
            return

        rooms_before = {r.get("id") for r in self._rooms}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _on_room_list(rooms):
            if fut.done():
                return
            for room in rooms:
                if room.get("id") in rooms_before:
                    continue
                if not room.get("isPrivate"):
                    continue
                if not fut.done():
                    fut.set_result(room.get("id"))

        self.on("room-list", _on_room_list)
        try:
            await self.send_message(f"/pm {username}")
            new_id = await asyncio.wait_for(fut, timeout=10)
            await self.join(new_id)
        except asyncio.TimeoutError:
            pass
        finally:
            self.off("room-list", _on_room_list)

    async def notify_typing(self) -> None:
        if not self._typing_active:
            self._typing_active = True
            await self.send_message("/t on")
        if self._typing_task and not self._typing_task.done():
            self._typing_task.cancel()
        async def _stop():
            await asyncio.sleep(4)
            self._typing_active = False
            await self.send_message("/t off")
        try:
            self._typing_task = asyncio.ensure_future(_stop())
        except RuntimeError:
            pass

    def has_unread_messages(self, room_id: Optional[int] = None) -> bool:
        """Mirror of the web client hasUnreadMessages().
        Compares room.lastReceivedMessageId against user.data.plugins.lastseen[roomId].
        Works on first launch because the data comes from the server.
        """
        if self._user.get("id", 0) == 0:
            return False
        lastseen: Dict = (
            self._user.get("data", {}).get("plugins", {}).get("lastseen") or {}
        )
        rooms = self._rooms if room_id is None else [r for r in self._rooms if r.get("id") == room_id]
        for room in rooms:
            rid = room.get("id")
            last_received = room.get("lastReceivedMessageId", 0) or 0
            last_seen     = lastseen.get(rid) or lastseen.get(str(rid)) or 0
            if last_received > last_seen:
                return True
        return False

    def update_lastseen(self, room_id: int, message_id: int) -> None:
        """Locally update lastseen so the indicator clears immediately after joining."""
        plugins = self._user.setdefault("data", {}).setdefault("plugins", {})
        ls = plugins.get("lastseen")
        if not isinstance(ls, dict):
            plugins["lastseen"] = {}
        plugins["lastseen"][room_id] = message_id

    @property
    def current_user(self) -> Dict:              return self._user
    @property
    def rooms(self) -> List[Dict]:               return self._rooms
    @property
    def current_room_id(self) -> Optional[int]:  return self._current_room_id
    @property
    def is_connected(self) -> bool:              return self._ws is not None
    @property
    def token(self) -> Optional[Dict]:           return self._token
    @property
    def typing_list(self) -> List[str]:          return self._typing_list


# ─────────────────────────────────────────────────────────────────────────────
# Colour setup
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Themes
# ─────────────────────────────────────────────────────────────────────────────

# Each theme: dict of xterm-256 colour indices keyed by role name
THEMES: Dict[str, Dict[str, int]] = {
    "Dracula": {
        "bg": 236, "cur_line": 237, "fg": 253, "comment": 103,
        "cyan": 117, "green": 120, "orange": 215, "pink": 212,
        "purple": 141, "red": 203, "yellow": 228,
    },
    "Nord": {
        "bg": 236, "cur_line": 237, "fg": 253, "comment": 60,
        "cyan": 110, "green": 108, "orange": 179, "pink": 146,
        "purple": 104, "red": 131, "yellow": 179,
    },
    "Gruvbox": {
        "bg": 235, "cur_line": 237, "fg": 223, "comment": 243,
        "cyan": 108, "green": 142, "orange": 208, "pink": 175,
        "purple": 132, "red": 167, "yellow": 214,
    },
    "Solarized Dark": {
        "bg": 235, "cur_line": 236, "fg": 250, "comment": 240,
        "cyan": 37,  "green": 64,  "orange": 166, "pink": 125,
        "purple": 61, "red": 160, "yellow": 136,
    },
    "Tokyo Night": {
        "bg": 235, "cur_line": 236, "fg": 189, "comment": 60,
        "cyan": 111, "green": 114, "orange": 216, "pink": 183,
        "purple": 99, "red": 204, "yellow": 222,
    },
    "Monokai": {
        "bg": 235, "cur_line": 237, "fg": 253, "comment": 102,
        "cyan": 81,  "green": 148, "orange": 208, "pink": 198,
        "purple": 141, "red": 197, "yellow": 186,
    },
}
THEME_NAMES = list(THEMES.keys())
_active_theme: str = "Dracula"


def _apply_theme(name: str) -> None:
    global _active_theme
    t = THEMES.get(name, THEMES["Dracula"])
    _active_theme = name
    _save_config({"theme": name})

    # Do NOT call start_color() here — it resets all init_color definitions
    curses.use_default_colors()
    c256 = curses.COLORS >= 256

    def p(pid: int, fg256: int, fg8: int, bg256: int, bg8: int) -> None:
        curses.init_pair(pid, fg256 if c256 else fg8, bg256 if c256 else bg8)

    bg  = t["bg"];  cl  = t["cur_line"]; fg  = t["fg"];  cm  = t["comment"]
    cy  = t["cyan"]; gn = t["green"];    or_ = t["orange"]; pk = t["pink"]
    pu  = t["purple"]; rd = t["red"];    yw  = t["yellow"]

    p(C_BASE,        fg,  FB_FG,     -1,  -1)
    p(C_HEADER,      fg,  FB_FG,     cm,  FB_ACCENT)
    p(C_ITEM_ACTIVE, bg,  FB_BG,     pu,  FB_ACCENT)
    p(C_ITEM_IDLE,   cm,  FB_FG,     -1,  -1)
    p(C_ITEM_CURSOR, bg,  FB_BG,     pk,  FB_CYAN)
    p(C_USERNAME,    cy,  FB_CYAN,   -1,  -1)
    p(C_TIMESTAMP,   cm,  FB_FG,     -1,  -1)
    p(C_STATUS,      bg,  FB_BG,     pu,  FB_ACCENT)
    p(C_INPUT,       15,  FB_FG,     cl,  FB_BG)
    p(C_SELF,        yw,  FB_YELLOW, -1,  -1)
    p(C_ERROR,       rd,  FB_RED,    -1,  -1)
    p(C_LOGIN_FIELD, fg,  FB_FG,     cl,  FB_BG)
    p(C_LOGIN_LABEL, pu,  FB_ACCENT, -1,  -1)
    p(C_LOGIN_BTN,   fg,  FB_FG,     cm,  FB_ACCENT)
    p(C_LOGIN_BTN_S, bg,  FB_BG,     gn,  FB_GREEN)
    p(C_BORDER,      cm,  FB_ACCENT, -1,  -1)
    p(C_USER_ONLINE, gn,  FB_GREEN,  -1,  -1)
    p(C_USER_AFK,    or_, FB_YELLOW, -1,  -1)
    p(C_USER_RECENT, rd,  FB_RED,    -1,  -1)
    p(C_MSG_SELECT,  bg,  FB_BG,     pu,  FB_ACCENT)



def _setup_colors() -> None:
    curses.start_color()
    saved = _load_config().get("theme", "Dracula")
    _apply_theme(saved if saved in THEMES else "Dracula")


# ─────────────────────────────────────────────────────────────────────────────
# Login screen
# ─────────────────────────────────────────────────────────────────────────────

class LoginField(Enum):
    USERNAME   = 0
    PASSWORD   = 1
    BTN_LOGIN  = 2
    BTN_GUEST  = 3
    BTN_RESUME = 4

# Return value sentinel: resume session without credentials
_RESUME_SESSION = object()


def ncurses_login(stdscr, prefill_username: str = '',
                  has_token: bool = False):
    """
    Show login form.
    Returns one of:
      (username, password, False)  — login
      (None, None, True)           — guest
      _RESUME_SESSION              — use saved token
      None                         — Esc / quit
    """
    curses.curs_set(1)
    stdscr.keypad(True)
    stdscr.nodelay(False)

    username_buf = prefill_username
    password_buf = ""
    # If we have a saved token, start with cursor on Resume
    field     = LoginField.BTN_RESUME if has_token else LoginField.USERNAME
    error_msg = ""

    LOGO = [
        "  ███████╗██╗  ██╗██╗   ██╗ ██████╗██╗  ██╗ █████╗ ████████╗",
        "  ██╔════╝██║ ██╔╝╚██╗ ██╔╝██╔════╝██║  ██║██╔══██╗╚══██╔══╝",
        "  ███████╗█████╔╝  ╚████╔╝ ██║     ███████║███████║   ██║   ",
        "  ╚════██║██╔═██╗   ╚██╔╝  ██║     ██╔══██║██╔══██║   ██║   ",
        "  ███████║██║  ██╗   ██║   ╚██████╗██║  ██║██║  ██║   ██║   ",
        "  ╚══════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   ",
    ]

    # Field navigation order depends on whether we have a token
    def _field_order():
        base = [LoginField.USERNAME, LoginField.PASSWORD,
                LoginField.BTN_LOGIN, LoginField.BTN_GUEST]
        if has_token:
            base.append(LoginField.BTN_RESUME)
        return base

    while True:
        stdscr.erase()
        H, W = stdscr.getmaxyx()
        field_order = _field_order()

        logo_top = max(0, H // 2 - 12)
        for i, line in enumerate(LOGO):
            row = logo_top + i
            if row >= H:
                break
            x = max(0, (W - len(line)) // 2)
            try:
                stdscr.addstr(row, x, line[:W],
                              curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            except curses.error:
                pass

        subtitle = f"skych.at  ·  {DEFAULT_WSS_URL}"
        try:
            stdscr.addstr(logo_top + len(LOGO),
                          max(0, (W - len(subtitle)) // 2),
                          subtitle[:W], curses.color_pair(C_TIMESTAMP))
        except curses.error:
            pass

        box_w = min(52, W - 4)
        box_h = 13 if has_token else 12
        box_y = logo_top + len(LOGO) + 2
        box_x = max(0, (W - box_w) // 2)

        try:
            for r in range(box_h):
                for c in range(box_w):
                    if r in (0, box_h - 1):
                        ch = '─'
                    elif c in (0, box_w - 1):
                        ch = '│'
                    else:
                        ch = ' '
                    brow, bcol = box_y + r, box_x + c
                    if 0 <= brow < H and 0 <= bcol < W:
                        stdscr.addch(brow, bcol, ch, curses.color_pair(C_BORDER))
            stdscr.addch(box_y,           box_x,           '╭', curses.color_pair(C_BORDER))
            stdscr.addch(box_y,           box_x + box_w-1, '╮', curses.color_pair(C_BORDER))
            stdscr.addch(box_y + box_h-1, box_x,           '╰', curses.color_pair(C_BORDER))
            stdscr.addch(box_y + box_h-1, box_x + box_w-1, '╯', curses.color_pair(C_BORDER))
        except curses.error:
            pass

        inner_x     = box_x + 2
        field_w     = box_w - 4
        u_label_row = box_y + 1
        u_field_row = box_y + 2
        p_label_row = box_y + 4
        p_field_row = box_y + 5
        btn_row     = box_y + 8
        resume_row  = box_y + 10
        err_row     = box_y + 7

        for row, label in [(u_label_row, "Username"), (p_label_row, "Password")]:
            try:
                stdscr.addstr(row, inner_x, label,
                              curses.color_pair(C_LOGIN_LABEL) | curses.A_BOLD)
            except curses.error:
                pass

        for row, buf, fld, mask in [
            (u_field_row, username_buf, LoginField.USERNAME, False),
            (p_field_row, password_buf, LoginField.PASSWORD, True),
        ]:
            is_active = field == fld
            display   = ('●' * len(buf)) if mask else buf
            display   = display[-field_w:] if len(display) > field_w else display
            padded    = display.ljust(field_w)
            attr      = (curses.color_pair(C_LOGIN_FIELD) | curses.A_BOLD
                         if is_active else curses.color_pair(C_INPUT))
            try:
                stdscr.addstr(row, inner_x, padded[:field_w], attr)
                stdscr.addstr(row + 1, inner_x, '─' * field_w,
                              curses.color_pair(C_BORDER))
            except curses.error:
                pass

        # Login / Guest buttons
        btn_login_label = "  [ Login ]  "
        btn_guest_label = "  [ Guest ]  "
        btn_total = len(btn_login_label) + 2 + len(btn_guest_label)
        btn_start = box_x + (box_w - btn_total) // 2
        for lbl, fld in [(btn_login_label, LoginField.BTN_LOGIN),
                         (btn_guest_label, LoginField.BTN_GUEST)]:
            is_sel = field == fld
            attr   = (curses.color_pair(C_LOGIN_BTN_S) | curses.A_BOLD
                      if is_sel else curses.color_pair(C_LOGIN_BTN))
            try:
                stdscr.addstr(btn_row, btn_start, lbl, attr)
            except curses.error:
                pass
            btn_start += len(lbl) + 2

        # Resume session button (only if token exists)
        if has_token:
            resume_lbl = f"  [ Resume session as {prefill_username or 'saved user'} ]  "
            resume_lbl = resume_lbl[:field_w]
            is_sel = field == LoginField.BTN_RESUME
            attr   = (curses.color_pair(C_LOGIN_BTN_S) | curses.A_BOLD
                      if is_sel else curses.color_pair(C_LOGIN_BTN))
            r_start = box_x + (box_w - len(resume_lbl)) // 2
            try:
                stdscr.addstr(resume_row, max(inner_x, r_start), resume_lbl, attr)
            except curses.error:
                pass

        if error_msg:
            try:
                stdscr.addstr(err_row, inner_x,
                              error_msg[:field_w].center(field_w),
                              curses.color_pair(C_ERROR) | curses.A_BOLD)
            except curses.error:
                pass

        hint = "Tab/↑↓ navigate · Enter select · Esc quit"
        try:
            stdscr.addstr(H - 1, max(0, (W - len(hint)) // 2),
                          hint[:W], curses.color_pair(C_TIMESTAMP))
        except curses.error:
            pass

        if field == LoginField.USERNAME:
            cx = inner_x + min(len(username_buf), field_w - 1)
            try: stdscr.move(u_field_row, cx)
            except curses.error: pass
        elif field == LoginField.PASSWORD:
            cx = inner_x + min(len(password_buf), field_w - 1)
            try: stdscr.move(p_field_row, cx)
            except curses.error: pass
        else:
            curses.curs_set(0)

        stdscr.refresh()
        curses.curs_set(1)

        try:
            key = stdscr.get_wch()
        except curses.error:
            continue

        if key in ('\x1b',):
            return None

        elif key == '\t' or key == curses.KEY_DOWN:
            idx   = field_order.index(field) if field in field_order else 0
            field = field_order[(idx + 1) % len(field_order)]
            error_msg = ""

        elif key == curses.KEY_UP:
            idx   = field_order.index(field) if field in field_order else 0
            field = field_order[(idx - 1) % len(field_order)]
            error_msg = ""

        elif key in (curses.KEY_ENTER, '\n', '\r', 10):
            if field == LoginField.BTN_GUEST:
                return (None, None, True)
            elif field == LoginField.BTN_RESUME:
                return _RESUME_SESSION
            elif field == LoginField.BTN_LOGIN:
                if not username_buf.strip():
                    error_msg = "Username cannot be empty"
                    field = LoginField.USERNAME
                else:
                    return (username_buf.strip(), password_buf, False)
            else:
                idx   = field_order.index(field) if field in field_order else 0
                field = field_order[(idx + 1) % len(field_order)]

        elif key in (curses.KEY_BACKSPACE, 127, '\x7f', 8):
            if field == LoginField.USERNAME and username_buf:
                username_buf = username_buf[:-1]
            elif field == LoginField.PASSWORD and password_buf:
                password_buf = password_buf[:-1]

        elif isinstance(key, str) and key.isprintable():
            if field == LoginField.USERNAME:
                username_buf += key
            elif field == LoginField.PASSWORD:
                password_buf += key

        elif isinstance(key, int) and 32 <= key < 127:
            ch = chr(key)
            if field == LoginField.USERNAME:
                username_buf += ch
            elif field == LoginField.PASSWORD:
                password_buf += ch

        elif key == curses.KEY_RESIZE:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# ChatUI
# ─────────────────────────────────────────────────────────────────────────────

SIDEBAR_W = 22
INPUT_H   = 3


class ChatUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr

        self.messages:     List[Dict] = []
        self.status_msg:   str        = ""
        self.status_until: float      = 0.0

        self.input_buf:   str  = ""
        self.cursor_pos:  int  = 0
        self._cursor_yx        = None

        self.focus:         Focus = Focus.INPUT
        self.room_cursor:   int   = 0
        self.user_cursor:     int   = 0
        self.user_scroll:     int   = 0   # top user index in viewport
        self._motto_tick:     int   = 0   # increments each draw, drives scroll
        self._motto_offsets:  Dict[str, int] = {}  # identifier -> scroll offset
        self.scroll_offset: int   = 0
        self.scroll_cursor: int   = -1
        self.typing_list:   List[str] = []

        self._colour_pair_cache:  Dict[int, int] = {}
        self._next_pair:          int            = C_DYN_BASE
        self._last_visible_count: int            = 0
        self._last_lines_out:     list           = []
        self._last_msg_range:     tuple          = (0, 0)

        # History lazy-loading
        self.history_exhausted:  bool             = False
        self.history_fetching:   bool             = False
        self._history_fetch_cb:  Optional[Callable] = None

        # Escape menu
        self.menu_open:             bool       = False
        self.menu_cursor:           int        = 0
        self.notifications_enabled: bool       = _load_config().get('notifications', True)
        self._own_user: Optional[Dict] = None
        self.colour_list:           List[Dict] = []  # from server 'custom' event
        self.colour_pick_open:      bool       = False
        self.colour_pick_cursor:    int        = 0
        self.unread: Dict[int, str] = {}  # room_id -> 'mention' | 'unread'

        self._build_windows()

    def _build_windows(self) -> None:
        H, W      = self.stdscr.getmaxyx()
        chat_w    = max(10, W - SIDEBAR_W * 2)
        chat_h    = max(4,  H - INPUT_H - 1)
        sidebar_h = chat_h   # sidebars stop at the same height as the chat pane

        self.win_header = curses.newwin(1,         W,         0,          0)
        self.win_rooms  = curses.newwin(sidebar_h, SIDEBAR_W, 1,          0)
        self.win_chat   = curses.newwin(chat_h,    chat_w,    1,          SIDEBAR_W)
        self.win_input  = curses.newwin(INPUT_H,   chat_w,    1 + chat_h, SIDEBAR_W)
        self.win_users  = curses.newwin(sidebar_h, SIDEBAR_W, 1,          SIDEBAR_W + chat_w)

        self.H, self.W = H, W
        self.chat_h    = chat_h
        self.chat_w    = chat_w

    def resize(self) -> None:
        # Tell curses the new terminal size first
        H, W = self.stdscr.getmaxyx()
        curses.resizeterm(H, W)
        self.stdscr.erase()
        self.stdscr.noutrefresh()
        self._build_windows()

    # ── Focus ─────────────────────────────────────────────────────────

    def cycle_focus(self, reverse: bool = False) -> None:
        idx = FOCUS_ORDER.index(self.focus)
        self.focus = FOCUS_ORDER[(idx + (-1 if reverse else 1)) % len(FOCUS_ORDER)]

    def sidebar_move(self, delta: int, n: int) -> None:
        if self.focus == Focus.ROOMS:
            self.room_cursor = max(0, min(n - 1, self.room_cursor + delta))
        elif self.focus == Focus.USERS:
            self.user_cursor = max(0, min(n - 1, self.user_cursor + delta))

    # ── Messages ──────────────────────────────────────────────────────

    def add_message(self, msg: Dict, ts: str = "") -> None:
        if not ts:
            ts = _parse_msg_ts(msg)
        user_obj = msg.get("user", {})
        user     = user_obj.get("username", "?") if isinstance(user_obj, dict) else str(user_obj)
        content  = _strip_tags(msg.get("content") or msg.get("formatted") or "")
        msg_id   = msg.get("id", 0)
        plugins  = user_obj.get("data", {}).get("plugins", {}) if isinstance(user_obj, dict) else {}
        hex_col  = plugins.get("custom", {}).get("color", "") or plugins.get("color", "")
        col      = hex_col or ""
        quoted_msg = None
        quoted = msg.get("quoted")
        if quoted and isinstance(quoted, dict):
            q_user = quoted.get("user", {})
            q_name = q_user.get("username", "?") if isinstance(q_user, dict) else str(q_user)
            q_text = _strip_tags(quoted.get("content") or quoted.get("formatted") or "")
            q_ts   = _parse_msg_ts(quoted) if any(quoted.get(k) for k in
                     ('date','createdTimestamp','createdAt','timestamp','time','created_at')) else ""
            quoted_msg = {"user": q_name, "text": q_text, "ts": q_ts}
        if content:
            self.messages.append({
                "ts": ts, "user": user, "content": content,
                "id": msg_id, "col": col,
                "quoted": quoted_msg, "reactions": {},
            })

    def set_status(self, text: str, ttl: float = 5.0) -> None:
        import time as _time
        self.status_msg   = text
        self.status_until = _time.monotonic() + ttl if text else 0.0

    # ── Master draw ───────────────────────────────────────────────────

    def _draw_box(self, y: int, x: int, h: int, w: int, title: str = "") -> None:
        """Draw a rounded box overlay."""
        try:
            for r in range(h):
                self.stdscr.addstr(y + r, x, " " * w, curses.color_pair(C_INPUT))
            corners = ['╭','╮','╰','╯']
            self.stdscr.addch(y,     x,     corners[0], curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            self.stdscr.addch(y,     x+w-1, corners[1], curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            self.stdscr.addch(y+h-1, x,     corners[2], curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            self.stdscr.addch(y+h-1, x+w-1, corners[3], curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            for r in range(1, h - 1):
                self.stdscr.addch(y+r, x,     '│', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
                self.stdscr.addch(y+r, x+w-1, '│', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            for c in range(1, w - 1):
                self.stdscr.addch(y,     x+c, '─', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
                self.stdscr.addch(y+h-1, x+c, '─', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            if title:
                self.stdscr.addstr(y, x + (w - len(title)) // 2,
                                   title, curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
        except curses.error:
            pass

    def _draw_menu(self, own_username: str) -> None:
        """Draw the Esc overlay menu centred over the chat area."""
        H, W = self.stdscr.getmaxyx()

        if self.colour_pick_open:
            self._draw_colour_picker(H, W)
            return

        items = [
            f"Theme: {_active_theme}",
            f"Notifications: {'ON' if self.notifications_enabled else 'OFF'}",
            "Pick username colour…" if self.colour_list else "Pick colour (not loaded)",
            "Logout",
            "Quit",
        ]
        box_w   = min(44, W - 4)
        box_h   = len(items) + 4
        box_y   = max(1, (H - box_h) // 2)
        box_x   = max(0, (W - box_w) // 2)
        inner_x = box_x + 2
        inner_w = box_w - 4

        self._draw_box(box_y, box_x, box_h, box_w, title="  Menu  ")
        try:
            for i, item in enumerate(items):
                row    = box_y + 2 + i
                is_sel = i == self.menu_cursor
                attr   = (curses.color_pair(C_ITEM_CURSOR) | curses.A_BOLD
                          if is_sel else curses.color_pair(C_INPUT))
                self.stdscr.addstr(row, inner_x,
                                   f"  {item}  "[:inner_w].ljust(inner_w), attr)
        except curses.error:
            pass

    def _draw_colour_picker(self, H: int, W: int) -> None:
        """Draw colour picker sub-menu as a scrollable list."""
        colours = self.colour_list
        if not colours:
            return
        visible = min(12, H - 6)
        box_w   = min(36, W - 4)
        box_h   = visible + 4
        box_y   = max(1, (H - box_h) // 2)
        box_x   = max(0, (W - box_w) // 2)
        inner_x = box_x + 2
        inner_w = box_w - 4

        # Scroll so cursor stays visible
        start = max(0, self.colour_pick_cursor - visible + 1)
        start = min(start, max(0, len(colours) - visible))

        self._draw_box(box_y, box_x, box_h, box_w, title="  Pick colour  ")
        try:
            for row_i, idx in enumerate(range(start, start + visible)):
                if idx >= len(colours):
                    break
                entry  = colours[idx]
                is_sel = idx == self.colour_pick_cursor
                attr   = (curses.color_pair(C_ITEM_CURSOR) | curses.A_BOLD
                          if is_sel else curses.color_pair(C_INPUT))
                hex_val = entry.get("value", "")
                name    = entry.get("name", hex_val)
                swatch_pair = self._hex_colour_pair(hex_val)
                row = box_y + 2 + row_i
                self.stdscr.addstr(row, inner_x,
                                   f"  ● {name}  "[:inner_w].ljust(inner_w), attr)
                # Colour just the swatch dot
                self.stdscr.addstr(row, inner_x + 2, "●",
                                   curses.color_pair(swatch_pair) | curses.A_BOLD)
        except curses.error:
            pass

    def draw_all(self, rooms: List[Dict], current_room_id: Optional[int],
                 connected_list: List[Dict], own_username: str,
                 typing_list: Optional[List[str]] = None,
                 own_user: Optional[Dict] = None,
                 unread_checker: Optional[Callable] = None) -> None:
        self._own_user = own_user
        if typing_list is not None:
            self.typing_list = typing_list
        try:
            self._draw_header(rooms, current_room_id, own_username)
            self._draw_rooms(rooms, current_room_id, connected_list, unread_checker=unread_checker, own_username=own_username)
            self._draw_chat(own_username)
            self._draw_users(connected_list, current_room_id, own_user=self._own_user)
            self._draw_input()
            if self.menu_open:
                self._draw_menu(own_username)
            self.stdscr.noutrefresh()
            if self._cursor_yx and not self.menu_open:
                curses.curs_set(1)
                curses.setsyx(*self._cursor_yx)
            else:
                curses.curs_set(0)
            curses.doupdate()
        except curses.error:
            pass

    # ── Header ────────────────────────────────────────────────────────

    def _draw_header(self, rooms: List[Dict], room_id: Optional[int], username: str) -> None:
        w = self.win_header
        w.bkgd(' ', curses.color_pair(C_HEADER))
        w.erase()
        _, W  = w.getmaxyx()
        room  = next((r for r in rooms if r.get("id") == room_id), None)
        if room:
            rname_raw = room.get("name", "") or ""
            if not rname_raw and room.get("isPrivate"):
                wl = room.get("whitelist") or room.get("allowedUsers") or []
                own_l = username.lower()
                parts = [u.get("username", "") if isinstance(u, dict) else str(u) for u in wl]
                parts = [p for p in parts if p and p.lower() != own_l]
                rname_raw = ", ".join(parts) or str(room.get("id", "?"))
            rname = f"@ {rname_raw}" if room.get("isPrivate") else f"# {rname_raw}"
        else:
            rname = "~ SkyChat"
        dot   = "●" if room_id is not None else "○"
        hint  = f"[Tab] {self.focus.name}"
        left  = f" {dot}  {rname}"
        right = f" {hint}   {username} "
        try:
            w.addstr(0, 0, left[:W - 1], curses.color_pair(C_HEADER) | curses.A_BOLD)
            x = max(len(left) + 1, W - len(right) - 1)
            w.addstr(0, x, right[:W - x - 1], curses.color_pair(C_HEADER))
        except curses.error:
            pass
        w.noutrefresh()

    # ── Rooms sidebar ─────────────────────────────────────────────────

    def _draw_rooms(self, rooms: List[Dict], current_room_id: Optional[int],
                    connected_list: List[Dict] = [],
                    unread_checker: Optional[Callable] = None,
                    own_username: str = "") -> None:
        w = self.win_rooms
        w.erase()
        H, W    = w.getmaxyx()
        focused = self.focus == Focus.ROOMS
        # Last usable column (W-1 is off-limits in curses — writing there errors)
        inner_w = W

        # Count users per room — normalise both sides to int
        room_counts: Dict[int, int] = {}
        for session in connected_list:
            for rid in (session.get("rooms") or []):
                try:
                    k = int(rid)
                    room_counts[k] = room_counts.get(k, 0) + 1
                except (ValueError, TypeError):
                    pass

        title = (" ▶ CHANNELS" if focused else "   CHANNELS")
        try:
            w.addstr(0, 0, title[:inner_w].ljust(inner_w),
                     curses.color_pair(C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

        for i, room in enumerate(rooms):
            row = i + 1
            if row >= H:
                break
            rid_val = room.get("id")
            try:
                rid_int = int(rid_val)
            except (ValueError, TypeError):
                rid_int = rid_val
            name      = room.get("name", "") or ""
            if not name and room.get("isPrivate"):
                wl = room.get("whitelist") or room.get("allowedUsers") or []
                own_l = own_username.lower()
                parts = [u.get("username", "") if isinstance(u, dict) else str(u) for u in wl]
                parts = [p for p in parts if p and p.lower() != own_l]
                name = ", ".join(parts) or str(rid_val if rid_val is not None else "?")
            elif not name:
                name = str(rid_val if rid_val is not None else "?")
            count     = room_counts.get(rid_int, 0)
            is_cur    = focused and i == self.room_cursor
            is_join   = rid_int == current_room_id
            if is_cur:
                attr = curses.color_pair(C_ITEM_CURSOR) | curses.A_BOLD
            elif is_join:
                attr = curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD
            else:
                attr = curses.color_pair(C_ITEM_IDLE)
            is_leaveable = room.get("isPrivate", False)
            x_str        = " ✕" if is_leaveable else ""
            x_width      = len(x_str)
            count_str    = str(count) if count > 0 else ""
            count_width  = len(count_str) + 1 if count_str else 0  # space + digits
            name_width   = inner_w - count_width - x_width
            # Server-authoritative unread: use checker fn if provided, fall back to local dict
            if unread_checker is not None:
                has_unread = unread_checker(rid_int)
                unread_state = 'unread' if has_unread else None
            else:
                unread_state = self.unread.get(rid_int)
            show_dot     = bool(unread_state) and not is_cur and not is_join
            dot          = "● " if show_dot else "  "
            left_part    = f"{dot}# {name}"[:name_width].ljust(name_width)
            try:
                if show_dot:
                    dot_attr = (curses.color_pair(C_ERROR) | curses.A_BOLD
                                if unread_state == 'mention'
                                else curses.color_pair(C_USERNAME) | curses.A_BOLD)
                    w.addstr(row, 0, dot, dot_attr)
                    w.addstr(row, len(dot), left_part[len(dot):], attr)
                else:
                    w.addstr(row, 0, left_part, attr)
                col_x = name_width
                if x_str:
                    w.addstr(row, col_x, x_str, curses.color_pair(C_ERROR))
                    col_x += x_width
                if count_str:
                    w.addstr(row, col_x, f" {count_str}",
                             curses.color_pair(C_INPUT) | curses.A_BOLD)
            except curses.error:
                pass

        w.noutrefresh()

    # ── Chat pane ─────────────────────────────────────────────────────

    def _draw_chat(self, own_username: str) -> None:
        w = self.win_chat
        w.erase()
        H, W     = w.getmaxyx()
        margin   = 2
        usable_w = max(1, W - margin * 2)
        self._last_visible_count = 0

        total = len(self.messages)
        if total == 0:
            self._last_msg_range = (0, 0)
            self._last_lines_out = []
            return

        self.scroll_offset = max(0, min(self.scroll_offset, total - 1))
        newest_idx = total - 1 - self.scroll_offset

        rows_avail = H - 1
        render_msgs: List[int] = []
        rows_used = 0
        for i in range(newest_idx, -1, -1):
            msg    = self.messages[i]
            prefix = len(msg["ts"]) + 1 + len(msg["user"]) + 2
            nlines = sum(len(textwrap.wrap(ln, max(8, usable_w - prefix)) or [""]) for ln in msg["content"].split("\n"))
            if msg.get("quoted"):
                nlines += 1
            if msg.get("reactions"):
                nlines += 1
            if rows_used + nlines > rows_avail and render_msgs:
                break
            render_msgs.append(i)
            rows_used += nlines
            if rows_used >= rows_avail:
                break

        render_msgs.reverse()
        oldest_idx = render_msgs[0] if render_msgs else newest_idx
        self._last_msg_range     = (oldest_idx, newest_idx)
        self._last_visible_count = len(render_msgs)

        if self.scroll_cursor >= 0:
            self.scroll_cursor = max(oldest_idx, min(newest_idx, self.scroll_cursor))

        row = max(0, H - 1 - rows_used)
        lines_out_compat: List[tuple] = []

        for mi in render_msgs:
            msg     = self.messages[mi]
            ts, user, msg_content = msg["ts"], msg["user"], msg["content"]
            is_sel  = (self.scroll_cursor == mi)
            sel_a   = curses.color_pair(C_MSG_SELECT)
            qt_a    = curses.color_pair(C_TIMESTAMP)

            # Quoted line — single line, truncated with … to fit
            qdata = msg.get("quoted")
            if qdata and row < H - 1:
                q_ts     = qdata.get("ts", "")
                q_prefix = f"  ↩ {qdata['user']}"
                if q_ts:
                    q_prefix += f" [{q_ts}]"
                q_prefix += ": "
                # How much space is left for the quoted text itself
                avail = max(4, W - margin - len(q_prefix) - 1)
                q_text = qdata['text'].replace('\n', ' ').strip()
                if len(q_text) > avail:
                    q_text = q_text[:avail - 1] + '…'
                q_line = q_prefix + q_text
                try:
                    if is_sel:
                        w.addstr(row, 0, " " * (W - 1), sel_a)
                    w.addstr(row, margin, q_line[:max(0, W - margin - 1)],
                             sel_a if is_sel else qt_a)
                except curses.error:
                    pass
                lines_out_compat.append((ts, user, q_line, False))
                row += 1

            # Message lines
            prefix  = len(ts) + 1 + len(user) + 2
            wrapped = []
            for _ln in msg_content.split("\n"):
                wrapped.extend(textwrap.wrap(_ln, max(8, usable_w - prefix)) or [""])

            for wi, chunk in enumerate(wrapped):
                if row >= H - 1:
                    break
                is_first = (wi == 0)
                lines_out_compat.append((ts, user, chunk, is_first))
                col = margin
                try:
                    if is_sel:
                        w.addstr(row, 0, " " * (W - 1), sel_a)
                    if is_first:
                        ts_a = sel_a if is_sel else curses.color_pair(C_TIMESTAMP)
                        w.addstr(row, col, f"{ts} ", ts_a)
                        col += len(ts) + 1
                        if is_sel:
                            ua = sel_a | curses.A_BOLD
                        else:
                            hex_col = msg.get('col', '')
                            if hex_col:
                                ua = curses.color_pair(self._hex_colour_pair(hex_col)) | curses.A_BOLD
                            elif user == own_username:
                                ua = curses.color_pair(C_SELF) | curses.A_BOLD
                            else:
                                ua = curses.color_pair(C_USERNAME) | curses.A_BOLD
                        w.addstr(row, col, user, ua)
                        col += len(user)
                        w.addstr(row, col, ": ",
                                 sel_a if is_sel else curses.color_pair(C_TIMESTAMP))
                        col += 2
                    else:
                        col += prefix

                    # Draw chunk: highlight @mention in red, URLs underlined
                    remaining = chunk[:max(0, W - col - 1)]

                    def _draw_segment(txt, base_attr):
                        """Draw txt with URLs underlined, splitting around them."""
                        nonlocal col
                        i2 = 0
                        for m in URL_RE.finditer(txt):
                            if col >= W - 1: break
                            if m.start() > i2:
                                plain = txt[i2:m.start()][:max(0, W - col - 1)]
                                try: w.addstr(row, col, plain, base_attr)
                                except curses.error: pass
                                col += len(plain)
                            url = m.group()[:max(0, W - col - 1)]
                            try: w.addstr(row, col, url,
                                          base_attr | curses.A_UNDERLINE)
                            except curses.error: pass
                            col += len(url)
                            i2 = m.end()
                        if i2 < len(txt) and col < W - 1:
                            plain = txt[i2:][:max(0, W - col - 1)]
                            try: w.addstr(row, col, plain, base_attr)
                            except curses.error: pass
                            col += len(plain)

                    if not is_sel and own_username and f'@{own_username}' in remaining:
                        needle = f'@{own_username}'
                        i2, txt2 = 0, remaining
                        while i2 < len(txt2) and col < W - 1:
                            pos = txt2.find(needle, i2)
                            if pos == -1:
                                _draw_segment(txt2[i2:], 0)
                                break
                            if pos > i2:
                                _draw_segment(txt2[i2:pos], 0)
                            mention = needle[:max(0, W - col - 1)]
                            try: w.addstr(row, col, mention,
                                          curses.color_pair(C_ERROR) | curses.A_BOLD)
                            except curses.error: pass
                            col += len(needle)
                            i2 = pos + len(needle)
                    else:
                        _draw_segment(remaining, sel_a if is_sel else 0)
                except curses.error:
                    pass
                row += 1

            # Reaction bar
            reactions = msg.get("reactions", {})
            if reactions and row < H - 1:
                rbar = "".join(f" {e}×{c} " for e, c in list(reactions.items())[:8])
                try:
                    if is_sel:
                        w.addstr(row, 0, " " * (W - 1), sel_a)
                    w.addstr(row, margin, rbar[:max(0, W - margin - 1)],
                             sel_a if is_sel else curses.color_pair(C_SELF) | curses.A_BOLD)
                except curses.error:
                    pass
                lines_out_compat.append((ts, user, rbar, False))
                row += 1

        self._last_lines_out = lines_out_compat

        # Status bar — only paint background when there's something to say
        if self.scroll_offset or self.scroll_cursor >= 0:
            pos      = total - self.scroll_offset
            if self.scroll_cursor >= 0:
                sel_msg = self.messages[self.scroll_cursor] if 0 <= self.scroll_cursor < len(self.messages) else None
                has_url = bool(sel_msg and URL_RE.search(sel_msg.get('content', '')))
                link_hint = "  o=open link" if has_url else ""
                sel_hint = f"  Spc=quote  e=edit{link_hint}"
            else:
                sel_hint = ""
            status_text = f"  ↑ {pos}/{total}{sel_hint}  Shift+↓ bottom"
        elif self.typing_list:
            names  = ", ".join(self.typing_list[:3])
            suffix = "…" if len(self.typing_list) > 3 else ""
            status_text = f"  ✎ {names}{suffix} typing…"
        else:
            import time as _time
            if self.status_until and _time.monotonic() > self.status_until:
                self.status_msg   = ""
                self.status_until = 0.0
            status_text = self.status_msg

        if status_text:
            is_err = status_text.startswith("✗")
            sa = (curses.color_pair(C_ERROR) | curses.A_BOLD if is_err
                  else curses.color_pair(C_STATUS))
            try:
                w.addstr(H - 1, 0, status_text[:W - 1].ljust(W - 1), sa)
            except curses.error:
                pass
        w.noutrefresh()

    # ── Users sidebar ─────────────────────────────────────────────────

    # Motto scrolls one char every N draw ticks
    _MOTTO_TICK_RATE = 20

    def _draw_users(self, connected_list: List[Dict],
                    current_room_id: Optional[int] = None,
                    own_user: Optional[Dict] = None) -> None:
        w = self.win_users
        w.erase()
        H, W    = w.getmaxyx()
        focused = self.focus == Focus.USERS

        # Split into in-room and out-of-room, in-room first
        in_room, out_room = [], []
        for session in connected_list:
            rooms = session.get("rooms") or []
            try:
                in_cur = current_room_id is not None and current_room_id in [int(r) for r in rooms]
            except (ValueError, TypeError):
                in_cur = False
            if in_cur:
                in_room.append(session)
            else:
                out_room.append(session)
        ordered = in_room + out_room

        title = (" ▶ USERS" if focused else "   USERS") + f" — {len(connected_list)}"
        try:
            w.addstr(0, 0, title[:W - 1].ljust(W - 1),
                     curses.color_pair(C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

        # Advance motto tick
        self._motto_tick += 1

        # Compute row height per user (2 if has motto, 1 if not)
        def _motto(session) -> str:
            pl = session.get("user", {}).get("data", {}).get("plugins", {})
            return (pl.get("motto") or "").strip()

        def _rows(session) -> int:
            return 2 if _motto(session) else 1

        # Ensure user_scroll keeps cursor visible
        n = len(ordered)
        if n == 0:
            w.noutrefresh()
            return
        self.user_cursor = max(0, min(self.user_cursor, n - 1))

        # Find row of cursor top within full list to enforce scroll
        def _row_of(idx):
            return sum(_rows(ordered[j]) for j in range(idx))

        cursor_row_top = _row_of(self.user_cursor)
        cursor_row_bot = cursor_row_top + _rows(ordered[self.user_cursor]) - 1
        viewport_h     = H - 1  # row 0 is title

        # Scroll so cursor is visible
        scroll_row = _row_of(self.user_scroll)
        if cursor_row_top < scroll_row:
            self.user_scroll = self.user_cursor
        elif cursor_row_bot >= scroll_row + viewport_h:
            # Advance scroll until cursor fits
            while _row_of(self.user_scroll) + viewport_h <= cursor_row_bot:
                self.user_scroll = min(self.user_scroll + 1, n - 1)

        # Draw from user_scroll
        row = 1
        motto_w = W - 5  # indent 4 + 1 margin
        for i in range(self.user_scroll, n):
            if row >= H:
                break
            session  = ordered[i]
            user_obj = session.get("user", {})
            uname    = user_obj.get("username", "?")
            motto    = _motto(session)
            ident    = session.get("identifier", uname)
            dot, dot_color = _user_status(session)
            is_cur   = focused and i == self.user_cursor
            in_cur_room = session in in_room

            if is_cur:
                dot_attr   = curses.color_pair(C_ITEM_CURSOR) | curses.A_BOLD
                name_attr  = curses.color_pair(C_ITEM_CURSOR) | curses.A_BOLD
                motto_attr = curses.color_pair(C_ITEM_CURSOR)
            else:
                plugins = user_obj.get("data", {}).get("plugins", {}) if isinstance(user_obj, dict) else {}
                hex_col = plugins.get("custom", {}).get("color", "") or plugins.get("color", "")
                if not hex_col and own_user and user_obj.get("id") == own_user.get("id"):
                    own_pl  = own_user.get("data", {}).get("plugins", {})
                    hex_col = own_pl.get("custom", {}).get("color", "") or own_pl.get("color", "")
                if in_cur_room:
                    upair      = self._hex_colour_pair(hex_col) if hex_col else C_USERNAME
                    dot_attr   = curses.color_pair(dot_color) | curses.A_BOLD
                    name_attr  = curses.color_pair(upair) | curses.A_BOLD
                    motto_attr = curses.color_pair(C_TIMESTAMP)
                else:
                    dot_attr   = curses.color_pair(dot_color)
                    name_attr  = curses.color_pair(C_ITEM_IDLE)
                    motto_attr = curses.color_pair(C_ITEM_IDLE)

            # Name row
            try:
                w.addstr(row, 1, " " * (W - 2), name_attr)
                w.addstr(row, 2, dot,            dot_attr)
                w.addstr(row, 4, uname[:W - 5],  name_attr)
            except curses.error:
                pass
            row += 1

            # Motto row
            if motto and row < H:
                # Scrolling ticker — column-aware so wide CJK/emoji chars don't
                # overflow the sidebar.  We advance phase in *characters* but
                # measure the visible window in *columns*.
                padded   = motto + "   "
                loop_str = padded * 3   # enough to always find a full window
                plen_ch  = len(padded)
                phase_ch = (self._motto_tick // self._MOTTO_TICK_RATE) % plen_ch
                # Advance past `phase_ch` characters, then take up to motto_w cols
                tail    = loop_str[phase_ch:]
                visible = _cols_slice(tail, motto_w)
                # Pad to full column width so background is painted evenly
                visible_cols = _str_cols(visible)
                if visible_cols < motto_w:
                    visible += ' ' * (motto_w - visible_cols)
                try:
                    w.addstr(row, 1, " " * (W - 2), motto_attr)
                    w.addstr(row, 4, visible,        motto_attr)
                except curses.error:
                    pass
                row += 1

        w.noutrefresh()

    # ── Input box ─────────────────────────────────────────────────────

    def _draw_input(self) -> None:
        w = self.win_input
        H, W    = w.getmaxyx()
        focused = self.focus == Focus.INPUT

        # Clear with transparent background (default colours)
        w.bkgdset(' ', curses.color_pair(C_BASE))
        w.erase()

        try:
            if focused:
                # Focused: purple rounded border
                w.border()
                w.addch(0, 0,       '╭', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
                w.addch(0, W - 1,   '╮', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
                w.addch(H - 1, 0,   '╰', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
                w.addch(H - 1, W-1, '╯', curses.color_pair(C_ITEM_ACTIVE) | curses.A_BOLD)
            else:
                # Unfocused: dim border
                w.border()
        except curses.error:
            pass

        vis_w = max(1, W - 4)
        start = max(0, self.cursor_pos - vis_w + 1)
        vis   = self.input_buf[start : start + vis_w]

        try:
            if vis:
                w.addstr(1, 2, vis, curses.color_pair(C_BASE) | curses.A_BOLD)
            elif not focused:
                ph = "Type a message…"[:vis_w]
                w.addstr(1, 2, ph, curses.color_pair(C_TIMESTAMP))
        except curses.error:
            pass

        if focused:
            try:
                cx = min(2 + (self.cursor_pos - start), W - 2)
                wy, wx = w.getbegyx()
                self._cursor_yx = (wy + 1, wx + cx)
            except curses.error:
                self._cursor_yx = None
        else:
            self._cursor_yx = None

        w.noutrefresh()

    # ── Text editing ──────────────────────────────────────────────────

    def insert_char(self, ch: str) -> None:
        self.input_buf = (self.input_buf[:self.cursor_pos]
                          + ch + self.input_buf[self.cursor_pos:])
        self.cursor_pos += 1

    def backspace(self) -> None:
        if self.cursor_pos > 0:
            self.input_buf = (self.input_buf[:self.cursor_pos - 1]
                              + self.input_buf[self.cursor_pos:])
            self.cursor_pos -= 1

    def delete_char(self) -> None:
        if self.cursor_pos < len(self.input_buf):
            self.input_buf = (self.input_buf[:self.cursor_pos]
                              + self.input_buf[self.cursor_pos + 1:])

    def move_cursor(self, d: int) -> None:
        self.cursor_pos = max(0, min(len(self.input_buf), self.cursor_pos + d))

    def home(self) -> None: self.cursor_pos = 0
    def end(self)  -> None: self.cursor_pos = len(self.input_buf)

    def consume_input(self) -> str:
        t = self.input_buf
        self.input_buf  = ""
        self.cursor_pos = 0
        return t

    def _user_colour_pair(self, xterm_idx: int) -> int:
        """Allocate a colour pair for a fixed xterm-256 index (no init_color)."""
        if xterm_idx in self._colour_pair_cache:
            return self._colour_pair_cache[xterm_idx]
        pair = self._next_pair
        if pair > 255:
            return C_USERNAME
        try:
            curses.init_pair(pair, xterm_idx, -1)
            self._colour_pair_cache[xterm_idx] = pair
            self._next_pair += 1
        except curses.error:
            return C_USERNAME
        return pair

    def _hex_colour_pair(self, hex_color: str) -> int:
        """Return a colour pair for a hex colour using nearest xterm-256 neighbour."""
        if not hex_color:
            return C_USERNAME
        key = hex_color.lower().lstrip('#')
        if key in self._colour_pair_cache:
            return self._colour_pair_cache[key]
        result = self._user_colour_pair(_hex_to_xterm256(hex_color))
        self._colour_pair_cache[key] = result
        return result

    def scroll_cursor_clear(self) -> None:
        self.scroll_cursor = -1

    def scroll_up(self, n: int = 1) -> None:
        self.scroll_offset = min(self.scroll_offset + n, max(0, len(self.messages) - 1))
        self._maybe_fetch_history()

    def _maybe_fetch_history(self) -> None:
        """Trigger a history fetch when we're near the top of loaded messages."""
        if self.history_exhausted or self.history_fetching:
            return
        if self._history_fetch_cb is None:
            return
        # Fire when within 5 messages of the oldest loaded
        if self.scroll_offset >= max(0, len(self.messages) - 5):
            self.history_fetching = True
            import asyncio as _aio
            _aio.ensure_future(self._history_fetch_cb())

    def scroll_down(self, n: int = 1) -> None:
        self.scroll_offset = max(0, self.scroll_offset - n)

    def scroll_bottom(self) -> None:
        self.scroll_offset = 0

    def reset_history_state(self) -> None:
        self.history_exhausted   = False
        self.history_fetching    = False
        self._history_empty_count = 0


# ─────────────────────────────────────────────────────────────────────────────
# Main TUI loop
# ─────────────────────────────────────────────────────────────────────────────

async def tui_chat(stdscr, username: Optional[str], password: Optional[str],
                   guest: bool, saved_token: Optional[dict] = None) -> None:
    curses.curs_set(1)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    try:
        curses.set_escdelay(25)  # Python 3.9+; silently ignored if unavailable
    except AttributeError:
        pass

    ui     = ChatUI(stdscr)
    client = SkyChatClient(DEFAULT_WSS_URL, auto_message_ack=True)

    # ── Event wiring ──────────────────────────────────────────────────

    @client.on("custom")
    def _on_custom(data):
        if isinstance(data, dict) and "color" in data:
            colours = data["color"]
            if isinstance(colours, list):
                ui.colour_list = colours



    def _notify(title: str, body: str) -> None:
        """Fire a desktop + terminal-bell notification if enabled."""
        if not ui.notifications_enabled:
            return
        # Terminal bell via curses (stdout is owned by curses)
        try:
            curses.beep()
        except Exception:
            pass
        # Desktop notification via notify-send
        # Inject DBUS address so it works inside tmux/plain terminals
        try:
            env = os.environ.copy()
            if "DBUS_SESSION_BUS_ADDRESS" not in env:
                try:
                    env["DBUS_SESSION_BUS_ADDRESS"] = (
                        f"unix:path=/run/user/{os.getuid()}/bus"
                    )
                except Exception:
                    pass
            subprocess.Popen(
                ["notify-send", "--app-name=SkyChat", "-t", "4000", title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env,
            )
        except FileNotFoundError:
            pass  # notify-send not installed
        except Exception:
            pass

    @client.on("message")
    def _on_msg(msg):
        ui.add_message(msg)
        # Notifications
        own = client.current_user.get("username", "")
        sender_obj = msg.get("user", {})
        sender = sender_obj.get("username", "") if isinstance(sender_obj, dict) else str(sender_obj)
        content = _strip_tags(msg.get("content") or msg.get("formatted") or "")
        # Normalise room_id to int for reliable comparison
        raw_rid = msg.get("room") if msg.get("room") is not None else msg.get("roomId")
        try:
            room_id = int(raw_rid) if raw_rid is not None else None
        except (TypeError, ValueError):
            room_id = None
        # Don't notify for own messages
        if sender == own:
            return
        # Find the room
        room = next((r for r in client.rooms if r.get("id") == room_id), None)
        is_dm = room.get("isPrivate", False) if room else False
        # Mentions always fire regardless of which room is open
        mentioned = bool(own) and f'@{own}' in content
        if mentioned:
            _notify(f'@{own} — {sender}', content[:120])
        elif is_dm and room_id != client.current_room_id:
            _notify(f'DM from {sender}', content[:120])
        # Unread indicator — skip if this is the currently viewed room
        if room_id is not None and room_id != client.current_room_id:
            if mentioned or ui.unread.get(room_id) != 'mention':
                ui.unread[room_id] = 'mention' if mentioned else 'unread'

    @client.on("message-edit")
    def _on_msg_edit(msg):
        mid = msg.get("id")
        if not mid:
            return
        reactions = _parse_reactions(msg.get("storage", {}))
        for m in ui.messages:
            if m.get("id") == mid:
                m["reactions"] = reactions
                new_content = _strip_tags(msg.get("content") or msg.get("formatted") or "")
                if new_content:
                    m["content"] = new_content
                break

    @client.on("messages")
    def _on_msgs(msgs):
        existing_ids = {m.get("id") for m in ui.messages if m.get("id")}
        prepend = []
        for m in msgs:
            mid = m.get("id", 0)
            if mid and mid in existing_ids:
                continue
            uo  = m.get("user", {})
            usr = uo.get("username", "?") if isinstance(uo, dict) else str(uo)
            txt = _strip_tags(m.get("content") or m.get("formatted") or "")
            if not txt:
                continue
            pl2     = uo.get("data", {}).get("plugins", {}) if isinstance(uo, dict) else {}
            hx2     = pl2.get("custom", {}).get("color", "") or pl2.get("color", "")
            col2    = hx2 or ""
            quoted_h = None
            q = m.get("quoted")
            if q and isinstance(q, dict):
                qu = q.get("user", {})
                qn = qu.get("username", "?") if isinstance(qu, dict) else str(qu)
                qt = _strip_tags(q.get("content") or q.get("formatted") or "")
                q_ts2 = _parse_msg_ts(q) if any(q.get(k) for k in
                        ('date','createdTimestamp','createdAt','timestamp','time','created_at')) else ""
                quoted_h = {"user": qn, "text": qt, "ts": q_ts2}
            prepend.append({
                "ts": _parse_msg_ts(m), "user": usr, "content": txt,
                "id": mid, "col": col2, "quoted": quoted_h,
                "reactions": _parse_reactions(m.get("storage", {})),
            })
        # Release fetching lock
        ui.history_fetching = False
        if prepend:
            ui.messages = prepend + ui.messages
            ui.set_status(f"↑ {len(ui.messages)} messages loaded", ttl=2.0)
        else:
            # Server returned nothing new — either truly exhausted or same batch
            # Increment counter; after 2 empty responses in a row, give up
            ui._history_empty_count = getattr(ui, '_history_empty_count', 0) + 1
            if ui._history_empty_count >= 2:
                ui.history_exhausted = True
                ui.set_status("↑ No more history", ttl=3.0)
            else:
                ui.set_status("↑ No new messages", ttl=2.0)

    @client.on("info")
    def _on_info(t):  ui.set_status(f"ℹ  {t}")

    @client.on("error")
    def _on_err(t):   ui.set_status(f"✗  {t}")

    @client.on("ws-open")
    def _on_open(_):  ui.set_status("Connected  ●  skych.at", ttl=4.0)

    @client.on("set-user")
    def _on_set_user_save(user):
        uname = user.get("username", "")
        if uname and uname != "*Guest" and not uname.startswith("*"):
            _save_config({"username": uname})

    @client.on("connection_lost")
    def _on_lost(d):  ui.set_status(f"✗  Connection lost (code {d.get('code','?')})")

    @client.on("reconnecting")
    def _on_recon(d): ui.set_status(f"Reconnecting… attempt {d['attempt']}")

    @client.on("reconnected")
    def _on_reced(_): ui.set_status("Reconnected ✓", ttl=4.0)

    @client.on("room-list")
    def _on_rooms(_): pass

    async def _fetch_more_history():
        """Fetch older messages. Tries /messagehistory <oldest_id> first."""
        if not ui.messages:
            await client.send_message("/messagehistory")
            return
        # Find the message with the smallest id (oldest) that has one
        msgs_with_id = [m for m in ui.messages if m.get("id")]
        if not msgs_with_id:
            await client.send_message("/messagehistory")
            return
        oldest = min(msgs_with_id, key=lambda m: m["id"])
        oldest_id = oldest["id"]
        await client.send_message(f"/messagehistory {oldest_id}")

    ui._history_fetch_cb = _fetch_more_history

    @client.on("join-room")
    def _on_join(rid):
        room = next((r for r in client.rooms if r.get("id") == rid), None)
        name = room.get("name", rid) if room else rid
        # Immediately mark room as read locally so the dot clears without
        # waiting for the server to echo back an updated set-user / lastseen.
        if rid is not None and room:
            last_id = room.get("lastReceivedMessageId") or 0
            if last_id:
                client.update_lastseen(rid, last_id)
        ui.messages.clear()
        ui.scroll_offset = 0
        ui.scroll_cursor = -1
        ui._last_msg_range = (0, 0)
        ui.reset_history_state()
        ui.set_status(f"# {name}", ttl=3.0)
        asyncio.ensure_future(client.send_message("/messagehistory"))

    # ── Connect & auth ────────────────────────────────────────────────

    # For token resume: set up the future BEFORE connecting so we can't miss set-user
    _resume_fut: Optional[asyncio.Future] = None
    if saved_token and username is None and not guest:
        client._token = saved_token
        _resume_fut   = asyncio.get_running_loop().create_future()
        def _resume_ok(user):
            uname = user.get("username", "")
            if not _resume_fut.done():
                if uname and not uname.startswith("*"):
                    _resume_fut.set_result(uname)
                else:
                    _resume_fut.set_exception(RuntimeError("Token rejected — please log in again"))
        def _resume_fail(err):
            if not _resume_fut.done():
                _resume_fut.set_exception(RuntimeError(str(err)))
        client.on("set-user", _resume_ok)
        client.on("error",    _resume_fail)

    ui.set_status("Connecting…")
    conn_task = asyncio.create_task(client.connect())
    await asyncio.sleep(0.8)

    try:
        if _resume_fut is not None:
            ui.set_status("Resuming session…")
            try:
                await asyncio.wait_for(_resume_fut, timeout=10)
            finally:
                client.off("set-user", _resume_ok)
                client.off("error",    _resume_fail)
        elif guest:
            ui.set_status("Joining as guest…")
            await client.login_as_guest(room_id=DEFAULT_ROOM_ID)
        else:
            ui.set_status(f"Logging in as {username}…")
            await client.login(username, password, room_id=DEFAULT_ROOM_ID)
    except Exception as exc:
        ui.set_status(f"✗  Auth failed: {exc}  — press any key")
        ui.draw_all([], None, [], "")
        stdscr.nodelay(False)
        stdscr.get_wch()
        client._running = False
        conn_task.cancel()
        return

    await asyncio.sleep(0.3)

    await client.join(DEFAULT_ROOM_ID)
    for i, r in enumerate(client.rooms):
        if r.get("id") == DEFAULT_ROOM_ID:
            ui.room_cursor = i
            break

    # ── Main loop ─────────────────────────────────────────────────────

    while True:
        rooms_list = client.rooms
        users_list = client._connected_list
        # Clear colour pair cache on each connected-list refresh so updated colours render
        if client._connected_list_dirty:
            client._connected_list_dirty = False
            ui._colour_pair_cache.clear()
            ui._next_pair = C_DYN_BASE

        ui.draw_all(
            rooms           = rooms_list,
            current_room_id = client.current_room_id,
            connected_list  = users_list,
            own_username    = client.current_user.get("username", ""),
            typing_list     = client.typing_list,
            own_user        = client.current_user,
            unread_checker  = client.has_unread_messages,
        )

        try:
            key = stdscr.get_wch()
        except curses.error:
            await asyncio.sleep(0.03)
            continue

        if key == curses.KEY_RESIZE:
            ui.resize()
            continue

        # Esc toggles the menu
        if key == '\x1b':
            if ui.menu_open and ui.colour_pick_open:
                ui.colour_pick_open = False  # back to main menu
            else:
                ui.menu_open = not ui.menu_open
                ui.menu_cursor = 0
                ui.colour_pick_open = False
            continue

        # Route keys to menu when open
        if ui.menu_open:
            # Colour picker sub-menu
            if ui.colour_pick_open:
                n = len(ui.colour_list)
                if key == curses.KEY_UP:
                    ui.colour_pick_cursor = max(0, ui.colour_pick_cursor - 1)
                elif key == curses.KEY_DOWN:
                    ui.colour_pick_cursor = min(n - 1, ui.colour_pick_cursor + 1)
                elif key in (curses.KEY_ENTER, '\n', '\r', 10):
                    entry   = ui.colour_list[ui.colour_pick_cursor]
                    cid     = entry.get('id')
                    hex_val = entry.get('value', '')
                    await client.send_message(f'/custom use color:{cid}')
                    # Update local user data immediately so chat re-renders with new colour
                    try:
                        plugins = client._user.setdefault('data', {}).setdefault('plugins', {})
                        plugins.setdefault('custom', {})['color'] = hex_val
                    except Exception:
                        pass
                    # Update local user data, messages, and connected-list immediately
                    own = client.current_user.get('username', '')
                    own_id = client.current_user.get('id')
                    # Invalidate cached hex pair (read from custom.color, not top-level color)
                    old_hex = client._user.get('data', {}).get('plugins', {}).get('custom', {}).get('color', '')
                    if old_hex:
                        old_key = old_hex.lower().lstrip('#')
                        ui._colour_pair_cache.pop(old_key, None)
                    # Update own user object (custom.color is what /custom use sets)
                    client._user.setdefault('data', {}).setdefault('plugins', {}).setdefault('custom', {})['color'] = hex_val
                    # Update messages
                    for m in ui.messages:
                        if m.get('user') == own:
                            m['col'] = hex_val
                    # Update connected-list entry
                    for session in client._connected_list:
                        u = session.get('user', {})
                        if isinstance(u, dict) and u.get('id') == own_id:
                            u.setdefault('data', {}).setdefault('plugins', {}).setdefault('custom', {})['color'] = hex_val
                            break
                    ui.colour_pick_open = False
                    ui.menu_open = False
                    ui.set_status(f'Colour set to {entry.get("name", cid)}', ttl=3.0)
                elif key == '\x1b':
                    ui.colour_pick_open = False
                elif key in (curses.KEY_BACKSPACE, 127, '\x7f', 8):
                    ui.colour_pick_open = False
                continue

            # Main menu
            menu_items_count = 5
            if key == curses.KEY_UP:
                ui.menu_cursor = (ui.menu_cursor - 1) % menu_items_count
            elif key == curses.KEY_DOWN:
                ui.menu_cursor = (ui.menu_cursor + 1) % menu_items_count
            elif key in (curses.KEY_ENTER, '\n', '\r', 10):
                if ui.menu_cursor == 0:   # Cycle theme
                    idx = (THEME_NAMES.index(_active_theme) + 1) % len(THEME_NAMES)
                    _apply_theme(THEME_NAMES[idx])
                    ui.set_status(f'Theme: {_active_theme}', ttl=2.0)
                elif ui.menu_cursor == 1:  # Toggle notifications
                    ui.notifications_enabled = not ui.notifications_enabled
                    _save_config({'notifications': ui.notifications_enabled})
                    ui.set_status(
                        f'Notifications {"ON" if ui.notifications_enabled else "OFF"}',
                        ttl=2.0)
                elif ui.menu_cursor == 2:  # Pick colour
                    if ui.colour_list:
                        ui.colour_pick_open   = True
                        ui.colour_pick_cursor = 0
                    else:
                        ui.set_status('✗  Colour list not yet received', ttl=3.0)
                elif ui.menu_cursor == 3:  # Logout
                    ui.menu_open = False
                    _save_config({'token': None, 'username': ''})
                    client._running = False
                    conn_task.cancel()
                    break
                elif ui.menu_cursor == 4:  # Quit
                    ui.menu_open = False
                    client._running = False
                    conn_task.cancel()
                    break
            continue

        if key == '\t':
            ui.cycle_focus(reverse=False)
            if rooms_list: ui.room_cursor = min(ui.room_cursor, len(rooms_list) - 1)
            if users_list: ui.user_cursor = min(ui.user_cursor, len(users_list) - 1)
            continue

        if key == curses.KEY_BTAB:
            ui.cycle_focus(reverse=True)
            if rooms_list: ui.room_cursor = min(ui.room_cursor, len(rooms_list) - 1)
            if users_list: ui.user_cursor = min(ui.user_cursor, len(users_list) - 1)
            continue

        # ROOMS focus
        if ui.focus == Focus.ROOMS:
            if key == curses.KEY_UP:
                ui.sidebar_move(-1, len(rooms_list))
            elif key == curses.KEY_DOWN:
                ui.sidebar_move(+1, len(rooms_list))
            elif key in (curses.KEY_ENTER, '\n', '\r', 10):
                if rooms_list and 0 <= ui.room_cursor < len(rooms_list):
                    _rid = rooms_list[ui.room_cursor]["id"]
                    ui.unread.pop(_rid, None)
                    await client.join(_rid)
                    ui.focus = Focus.INPUT
            elif key in (curses.KEY_BACKSPACE, 127, '\x7f', 8):
                # Leave only private rooms (DMs/group chats), not public rooms
                if rooms_list and 0 <= ui.room_cursor < len(rooms_list):
                    room = rooms_list[ui.room_cursor]
                    if room.get("isPrivate", False):
                        # Join first (server requires membership), then leave
                        await client.send_message(f"/join {room['id']}")
                        await asyncio.sleep(0.3)
                        await client.send_message(f"/pmleave {room['id']}")
                        ui.set_status(f"Left {room.get('name', room['id'])}", ttl=3.0)
                    else:
                        ui.set_status("✗  Can't leave public rooms", ttl=2.0)

        # USERS focus
        elif ui.focus == Focus.USERS:
            if key == curses.KEY_UP:
                ui.sidebar_move(-1, len(users_list))
            elif key == curses.KEY_DOWN:
                ui.sidebar_move(+1, len(users_list))
            elif key in (curses.KEY_ENTER, '\n', '\r', 10):
                if users_list and 0 <= ui.user_cursor < len(users_list):
                    target = users_list[ui.user_cursor].get("user", {}).get("username", "")
                    if target and target != client.current_user.get("username"):
                        await client.open_dm(target)
                        ui.set_status(f"Opened DM with {target}", ttl=3.0)
                    ui.focus = Focus.INPUT

        # INPUT focus
        else:
            if key in (' ', 32) and (ui.scroll_offset > 0 or ui.scroll_cursor >= 0):
                msg = (ui.messages[ui.scroll_cursor]
                       if 0 <= ui.scroll_cursor < len(ui.messages) else None)
                if msg and msg.get("id"):
                    ref = f"@{msg['id']} "
                    ui.input_buf  = ref + ui.input_buf
                    ui.cursor_pos = len(ref)
                ui.scroll_cursor_clear()
                ui.scroll_bottom()

            elif key in (curses.KEY_ENTER, '\n', '\r', 10):
                text = ui.consume_input().strip()
                if not text:
                    pass
                elif text == "/quit":
                    break
                elif text.startswith("/join "):
                    parts = text.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        await client.join(int(parts[1]))
                    else:
                        ui.set_status("Usage: /join <room_id>")
                elif text == "/rooms":
                    ui.set_status("  ".join(
                        f"[{r['id']}]{r.get('name','?')}" for r in client.rooms
                    ) or "No rooms")
                elif text == "/who":
                    ui.set_status("Online: " + ", ".join(
                        s.get("user", {}).get("username", "?")
                        for s in client._connected_list
                    ))
                elif text in ("/history", "/messagehistory"):
                    await client.send_message("/messagehistory")
                else:
                    if client._typing_active:
                        client._typing_active = False
                        if client._typing_task:
                            client._typing_task.cancel()
                        await client.send_message("/t off")
                    await client.send_message(text)

            elif key == curses.KEY_UP:
                oldest, newest = ui._last_msg_range
                if ui.scroll_cursor >= 0:
                    if ui.scroll_cursor <= oldest:
                        ui.scroll_up(1)
                    else:
                        ui.scroll_cursor -= 1
                else:
                    ui.scroll_cursor = newest

            elif key == curses.KEY_DOWN:
                oldest, newest = ui._last_msg_range
                if ui.scroll_cursor >= 0:
                    if ui.scroll_cursor >= newest:
                        if ui.scroll_offset > 0:
                            ui.scroll_down(1)
                        else:
                            ui.scroll_cursor_clear()
                    else:
                        ui.scroll_cursor += 1
                else:
                    ui.scroll_down(1)

            elif key in ('e', 'E') and ui.scroll_cursor >= 0:
                # Edit selected message — only own messages
                msg = (ui.messages[ui.scroll_cursor]
                       if 0 <= ui.scroll_cursor < len(ui.messages) else None)
                if msg and msg.get("id") and msg.get("user") == client.current_user.get("username"):
                    mid = msg["id"]
                    ui.input_buf  = f"/edit {mid} {msg['content']}"
                    ui.cursor_pos = len(ui.input_buf)
                    ui.scroll_cursor_clear()
                    ui.scroll_bottom()
                elif msg:
                    ui.set_status("✗  Can only edit your own messages", ttl=2.0)

            elif key in ('o', 'O') and ui.scroll_cursor >= 0:
                # Open URL(s) from selected message in default browser
                msg = (ui.messages[ui.scroll_cursor]
                       if 0 <= ui.scroll_cursor < len(ui.messages) else None)
                if msg:
                    urls = URL_RE.findall(msg.get('content', ''))
                    if urls:
                        import subprocess as _sp
                        for url in urls:
                            _sp.Popen(['xdg-open', url],
                                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                        plural = 's' if len(urls) > 1 else ''
                        ui.set_status(f"↗  Opened {len(urls)} link{plural}", ttl=3.0)
                    else:
                        ui.set_status("No links in this message", ttl=2.0)

            elif key == curses.KEY_SF:
                ui.scroll_bottom()
                ui.scroll_cursor_clear()
            elif key == curses.KEY_SR:
                ui.scroll_up(5)
            elif key in (curses.KEY_BACKSPACE, 127, '\x7f', 8):
                ui.backspace()
            elif key == curses.KEY_DC:
                ui.delete_char()
            elif key == curses.KEY_LEFT:
                ui.move_cursor(-1)
            elif key == curses.KEY_RIGHT:
                ui.move_cursor(+1)
            elif key == curses.KEY_HOME:
                ui.home()
            elif key == curses.KEY_END:
                ui.end()
            elif isinstance(key, str) and key.isprintable():
                ui.insert_char(key)
                asyncio.ensure_future(client.notify_typing())
            elif isinstance(key, int) and 32 <= key < 127:
                ui.insert_char(chr(key))
                asyncio.ensure_future(client.notify_typing())

        await asyncio.sleep(0.02)

    # Cleanup
    client._running = False
    conn_task.cancel()
    try:
        await conn_task
    except asyncio.CancelledError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _run(stdscr, cli_username: Optional[str], cli_password: Optional[str]) -> None:
    _setup_colors()
    cfg = _load_config()

    if cli_username and cli_password:
        username, password, guest = cli_username, cli_password, False
        saved_token = None
        resume      = False
    else:
        saved_username = cfg.get("username", "")
        saved_token    = cfg.get("token")
        result = ncurses_login(stdscr, prefill_username=saved_username,
                               has_token=bool(saved_token))
        if result is None:
            return  # Esc / quit
        if result is _RESUME_SESSION:
            username    = None
            password    = None
            guest       = False
            resume      = True
        else:
            username, password, guest = result
            saved_token = None   # fresh login — don't reuse old token
            resume      = False

    stdscr.clear()
    stdscr.refresh()
    asyncio.run(tui_chat(stdscr, username, password, guest,
                         saved_token=saved_token if resume else None))


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description=f"SkyChat TUI  [{DEFAULT_WSS_URL}]",
        epilog="Tab=focus  ↑↓=navigate  Enter=join/DM  /quit=exit",
    )
    parser.add_argument("username", nargs="?", default=None)
    parser.add_argument("password", nargs="?", default=None)
    parser.add_argument("--debug", action="store_true",
                        help="Write crash tracebacks to /tmp/skychat_crash.log")
    args = parser.parse_args()
    # Reduce ncurses ESC disambiguation delay from the default ~1 s to 25 ms.
    # Must be set before curses.wrapper() initialises the terminal.
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(_run, args.username, args.password)
    except KeyboardInterrupt:
        pass
    except Exception:
        if args.debug:
            import traceback
            with open("/tmp/skychat_crash.log", "w") as _f:
                traceback.print_exc(file=_f)
    finally:
        try:
            curses.endwin()
        except Exception:
            pass


if __name__ == "__main__":
    main()