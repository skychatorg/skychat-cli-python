"""
SkyChatClient — WebSocket layer.
No curses, no rendering. All UI interaction goes through the event system.
"""

import asyncio
import json
import logging
import random
import struct
import time
from typing import Any, Callable, Dict, List, Optional

_dbg = logging.getLogger('skychat')
_dbg.addHandler(logging.NullHandler())

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    _WEBSOCKETS_OK = True
except ImportError:
    websockets = None  # type: ignore
    ConnectionClosed = Exception  # type: ignore
    _WEBSOCKETS_OK = False

from .constants import DEFAULT_WSS_URL, DEFAULT_ROOM_ID, BINARY_MSG_AUDIO, BINARY_MSG_CURSOR
from .config import save_config, save_token
from .helpers import _apply_jsondiffpatch_array

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

        # Scroll-read ack: track which message we last sent /lastseen for,
        # and debounce so we only fire after the viewport has been stable 1s.
        self._scroll_ack_last_sent: int   = 0   # highest mid we confirmed to server
        self._scroll_ack_candidate: int   = 0   # newest visible mid this frame
        self._scroll_ack_since:     float = 0.0 # monotonic time candidate first seen

        self.on("set-user",       self._on_set_user)
        self.on("auth-token",     self._on_auth_token)
        self.on("config",         lambda _: None)
        self.on("room-list",      lambda r: setattr(self, '_rooms', r))
        def _on_join_room(rid):
            self._current_room_id      = rid
            self._scroll_ack_last_sent = 0
            self._scroll_ack_candidate = 0
            self._scroll_ack_since     = 0.0
        self.on("join-room", _on_join_room)
        def _on_connected_list(s):
            self._connected_list[:] = s  # mutate in-place so existing references stay valid
            self._connected_list_dirty = True
        self.on("connected-list", _on_connected_list)

        def _on_connected_list_patch(patch):
            """Apply a jsondiffpatch delta to the connected list."""
            if not isinstance(patch, dict):
                return
            new_list = _apply_jsondiffpatch_array(self._connected_list, patch)
            self._connected_list[:] = new_list  # mutate in-place so existing references stay valid
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
            except Exception as e:
                _dbg.debug('handler exception for event %r: %s', event, e, exc_info=True)

    def _on_set_user(self, user: Dict) -> None:
        self._user = user

    def _on_auth_token(self, token) -> None:
        self._token = token
        if token:
            save_token(token)

    def _on_message_internal(self, msg: Dict) -> None:
        if self.auto_message_ack and self._user.get("id", 0) != 0:
            asyncio.ensure_future(self._ack(msg.get("id", 0)))

    async def _ack(self, mid: int) -> None:
        await self.send_message(f"/lastseen {mid}")

    def scroll_ack_tick(self, newest_visible_mid: int) -> None:
        """Call each frame with the highest message-id currently visible.

        Sends /lastseen once the viewport has been stable on a new high-water
        mark for at least 1 second, and only if the id exceeds what we already
        confirmed.  Fires-and-forgets via ensure_future so it never blocks.
        """
        if newest_visible_mid <= self._scroll_ack_last_sent:
            # Already acked this message or nothing new to ack
            self._scroll_ack_candidate = 0
            return

        now = time.monotonic()
        if newest_visible_mid != self._scroll_ack_candidate:
            # Viewport moved — restart the debounce timer
            self._scroll_ack_candidate = newest_visible_mid
            self._scroll_ack_since     = now
            return

        if now - self._scroll_ack_since < 1.0:
            # Still within debounce window
            return

        # Stable for >= 1s and above the last confirmed id — send it
        self._scroll_ack_last_sent = newest_visible_mid
        asyncio.ensure_future(self._ack(newest_visible_mid))

    # ── WebSocket lifecycle ────────────────────────────────────────────

    async def connect(self) -> None:
        if not _WEBSOCKETS_OK:
            raise RuntimeError("websockets not installed. Run: pip install websockets")
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
    @property
    def typing_active(self) -> bool:             return self._typing_active

    def mark_connected_list_dirty(self) -> None:
        """Signal that the connected list was mutated in-place by the caller."""
        self._connected_list_dirty = True
    @property
    def connected_list(self) -> List[Dict]:       return self._connected_list

    def take_connected_list_dirty(self) -> bool:
        """Return True if the connected list changed since the last call, then reset the flag."""
        dirty = self._connected_list_dirty
        self._connected_list_dirty = False
        return dirty

    def stop(self) -> None:
        """Signal the connection loop to stop reconnecting and exit."""
        self._running = False

    def set_token(self, token) -> None:
        """Set the auth token directly (used when resuming a saved session)."""
        self._token = token

    def cancel_typing(self) -> None:
        """Immediately cancel any in-flight typing indicator."""
        self._typing_active = False
        if self._typing_task and not self._typing_task.done():
            self._typing_task.cancel()

    def set_user_color(self, hex_val: str) -> None:
        """Locally apply a new username colour so the UI reflects it immediately.

        Patches the in-memory user dict and every cached message entry; the
        authoritative server-side change must be sent separately via
        ``send_message('/custom use color:<id>')``.
        """
        try:
            (self._user
             .setdefault('data', {})
             .setdefault('plugins', {})
             .setdefault('custom', {})
             )['color'] = hex_val
        except Exception:
            pass