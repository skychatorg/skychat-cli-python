"""
ChatUI — all ncurses rendering, theme management, and colour setup.
"""

import asyncio
import curses
import os
import subprocess
import time
import webbrowser
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

from .constants import (
    C_BASE, C_HEADER, C_ITEM_ACTIVE, C_ITEM_IDLE, C_ITEM_CURSOR,
    C_USERNAME, C_TIMESTAMP, C_STATUS, C_INPUT, C_SELF, C_ERROR,
    C_LOGIN_FIELD, C_LOGIN_LABEL, C_LOGIN_BTN, C_LOGIN_BTN_S,
    C_BORDER, C_USER_ONLINE, C_USER_AFK, C_USER_RECENT, C_MSG_SELECT,
    C_DYN_BASE, CACA_PAIR_BASE,
    FB_BG, FB_FG, FB_ACCENT, FB_CYAN, FB_GREEN, FB_YELLOW, FB_RED,
    URL_RE, BUTTON_RE, STICKER_RE, TAG_RE,
)
from .config import load_config, save_config
from .helpers import (
    _printable_char, _strip_tags, _char_width, _str_cols, _cols_slice,
    _cols_aware_wrap, _cols_aware_wrap_offsets, _get_interactables, _hex_to_xterm256,
    _parse_reactions, _user_status, _parse_msg_ts, _room_display_name,
)
from .images import (
    ImagePopup, _init_caca_pairs, _is_image_url,
    _detect_protocol, _query_cell_pixels,
    _detect_url_openers,
    _dbg,
)


# ── Focus ─────────────────────────────────────────────────────────────────────

class Focus(Enum):
    INPUT = auto()
    ROOMS = auto()
    USERS = auto()

FOCUS_ORDER = [Focus.ROOMS, Focus.INPUT, Focus.USERS]


# ── Box drawing helper ────────────────────────────────────────────────────────

def _draw_box(stdscr, y: int, x: int, h: int, w: int,
              colour_pair: int, title: str = "") -> None:
    """Draw a rounded-corner box on *stdscr* using *colour_pair*."""
    attr = colour_pair | curses.A_BOLD
    try:
        for r in range(h):
            stdscr.addstr(y + r, x, " " * w, colour_pair)
        for r in range(1, h - 1):
            stdscr.addch(y + r, x,         '│', attr)
            stdscr.addch(y + r, x + w - 1, '│', attr)
        for c in range(1, w - 1):
            stdscr.addch(y,         x + c, '─', attr)
            stdscr.addch(y + h - 1, x + c, '─', attr)
        stdscr.addch(y,         x,         '╭', attr)
        stdscr.addch(y,         x + w - 1, '╮', attr)
        stdscr.addch(y + h - 1, x,         '╰', attr)
        stdscr.addch(y + h - 1, x + w - 1, '╯', attr)
        if title:
            stdscr.addstr(y, x + (w - len(title)) // 2, title, attr)
    except curses.error:
        pass


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
    save_config({"theme": name})

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



def get_active_theme() -> str:
    return _active_theme


def _setup_colors() -> None:
    curses.start_color()
    saved = load_config().get("theme", "Dracula")
    _apply_theme(saved if saved in THEMES else "Dracula")
    _init_caca_pairs()


SIDEBAR_W      = 22
INPUT_H        = 3   # minimum input box height (1 border top + 1 text + 1 border bottom)
INPUT_H_MAX    = 7   # maximum input box height (caps at 5 text lines)
# Number of items in the Esc menu — single source of truth shared with main.py
# so cursor wrap and item list can never silently diverge.
MENU_ITEM_COUNT = 8


class ChatUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr

        self.messages:     List[Dict] = []
        self.status_msg:   str        = ""
        self.status_until: float      = 0.0

        self.input_buf:      str  = ""
        self.cursor_pos:     int  = 0
        self._cursor_yx           = None
        self.input_h:        int  = INPUT_H       # current input box height (dynamic)
        self.input_vscroll:  int  = 0             # first visible line row in input box

        self.focus:         Focus = Focus.INPUT
        self.room_cursor:   int   = 0
        self.user_cursor:     int   = 0
        self.user_scroll:     int   = 0   # top user index in viewport
        self.scroll_offset: int   = 0
        self.scroll_cursor: int   = -1
        self.btn_cursor:    int   = 0   # focused interactable index in selected message
        self.typing_list:   List[str] = []
        self._overlay: Optional['ImagePopup'] = None  # image popup painted before doupdate

        self._colour_pair_cache:  Dict[int, int] = {}
        self._next_pair:          int            = C_DYN_BASE
        self._last_visible_count: int            = 0
        self._last_msg_range:     tuple          = (0, 0)
        self._last_skip_top:      int            = 0   # lines clipped from topmost msg
        self._last_natural_skip:  int            = 0   # skip before scroll_line_offset
        self._last_oldest_idx:    int            = -1  # oldest visible msg index
        self.scroll_line_offset:  int            = 0   # extra lines revealed from top of topmost msg

        # History lazy-loading
        self.history_exhausted:   bool             = False
        self.history_fetching:    bool             = False
        self._history_empty_count: int             = 0
        self._history_fetch_cb:  Optional[Callable] = None

        # Escape menu
        self.menu_open:             bool       = False
        self.menu_cursor:           int        = 0
        self.notifications_enabled: bool       = load_config().get('notifications', True)
        self.image_preview_enabled: bool       = load_config().get('image_preview', True)
        self.url_opener: str                   = load_config().get('url_opener', 'xdg-open')
        _bl = load_config().get('blacklist', [])
        self.blacklist: set = {u.lower() for u in _bl if isinstance(u, str)}
        self.hide_guests: bool = load_config().get('hide_guests', False)
        self._own_user: Optional[Dict] = None
        self.colour_list:           List[Dict] = []  # from server 'custom' event
        self.colour_pick_open:      bool       = False
        self.colour_pick_cursor:    int        = 0
        self.unread: Dict[int, str] = {}  # room_id -> 'mention' | 'unread'
        self._ordered_users: List[Dict] = []  # mirrors draw order for key handler

        self._build_windows()

    def _build_windows(self) -> None:
        H, W      = self.stdscr.getmaxyx()
        chat_w    = max(10, W - SIDEBAR_W * 2)
        chat_h    = max(4,  H - self.input_h - 1)
        sidebar_h = chat_h   # sidebars stop at the same height as the chat pane

        self.win_header = curses.newwin(1,            W,         0,          0)
        self.win_rooms  = curses.newwin(sidebar_h,    SIDEBAR_W, 1,          0)
        self.win_chat   = curses.newwin(chat_h,       chat_w,    1,          SIDEBAR_W)
        self.win_input  = curses.newwin(self.input_h, chat_w,    1 + chat_h, SIDEBAR_W)
        self.win_users  = curses.newwin(sidebar_h,    SIDEBAR_W, 1,          SIDEBAR_W + chat_w)

        self.H, self.W = H, W
        self.chat_h    = chat_h
        self.chat_w    = chat_w

    def _update_input_height(self) -> bool:
        """Recompute input_h from current input_buf. Returns True if height changed."""
        vis_w = max(1, self.chat_w - 4)
        visual_rows = sum(len(_cols_aware_wrap(ln, vis_w)) for ln in self.input_buf.split('\n'))
        new_h = min(INPUT_H_MAX, max(INPUT_H, visual_rows + 2))  # +2 for borders
        if new_h != self.input_h:
            self.input_h = new_h
            return True
        return False

    def resize(self) -> None:
        # Tell curses the new terminal size first
        H, W = self.stdscr.getmaxyx()
        curses.resizeterm(H, W)
        self.stdscr.erase()
        self.stdscr.noutrefresh()
        try:
            self._build_windows()
        except curses.error:
            pass

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

    @staticmethod
    def _msg_to_entry(msg: Dict, storage: Optional[Dict] = None) -> Optional[Dict]:
        """Convert a raw server message dict into a normalised entry dict.

        Returns None if the message has no displayable content.
        Pass storage=msg.get("storage") for history messages that carry reactions.
        """
        user_obj = msg.get("user", {})
        user     = user_obj.get("username", "?") if isinstance(user_obj, dict) else str(user_obj)
        content  = _strip_tags(msg.get("content") or msg.get("formatted") or "")
        if not content:
            return None
        msg_id  = msg.get("id", 0)
        plugins = user_obj.get("data", {}).get("plugins", {}) if isinstance(user_obj, dict) else {}
        col     = plugins.get("custom", {}).get("color", "") or plugins.get("color", "") or ""
        quoted_msg = None
        quoted = msg.get("quoted")
        if quoted and isinstance(quoted, dict):
            q_user = quoted.get("user", {})
            q_name = q_user.get("username", "?") if isinstance(q_user, dict) else str(q_user)
            q_text = _strip_tags(quoted.get("content") or quoted.get("formatted") or "")
            q_ts   = (_parse_msg_ts(quoted) if any(quoted.get(k) for k in
                      ('date', 'createdTimestamp', 'createdAt', 'timestamp', 'time', 'created_at'))
                      else "")
            quoted_msg = {"user": q_name, "text": q_text, "ts": q_ts}
        return {
            "ts":        _parse_msg_ts(msg),
            "user":      user,
            "content":   content,
            "id":        msg_id,
            "col":       col,
            "quoted":    quoted_msg,
            "reactions": _parse_reactions(storage) if storage else {},
        }

    def _is_hidden(self, username: str) -> bool:
        """Return True if this username should be suppressed client-side."""
        ul = username.lower()
        if self.blacklist and ul in self.blacklist:
            return True
        if self.hide_guests and username.startswith('*'):
            return True
        return False

    def add_message(self, msg: Dict) -> None:
        sender = msg.get('user', {})
        uname = sender.get('username', '') if isinstance(sender, dict) else str(sender)
        if self._is_hidden(uname):
            return
        entry = self._msg_to_entry(msg)
        if entry:
            self.messages.append(entry)

    def set_status(self, text: str, ttl: float = 5.0) -> None:
        self.status_msg   = text
        self.status_until = time.monotonic() + ttl if text else 0.0

    # ── Master draw ───────────────────────────────────────────────────

    def _draw_box(self, y: int, x: int, h: int, w: int, title: str = "") -> None:
        """Draw a rounded box overlay (delegates to module-level _draw_box)."""
        _draw_box(self.stdscr, y, x, h, w,
                  curses.color_pair(C_ITEM_ACTIVE), title=title)

    def _draw_menu(self, own_username: str) -> None:
        """Draw the Esc overlay menu centred over the chat area."""
        H, W = self.stdscr.getmaxyx()

        if self.colour_pick_open:
            self._draw_colour_picker(H, W)
            return

        items = [
            f"Theme: {_active_theme}",
            f"Notifications: {'ON' if self.notifications_enabled else 'OFF'}",
            f"Image Preview: {'ON' if self.image_preview_enabled else 'OFF'}",
            f"Open URLs with: {self.url_opener}",
            f"Hide guests: {'ON' if self.hide_guests else 'OFF'}",
            "Pick username color…" if self.colour_list else "Pick color (not loaded)",
            "Logout",
            "Quit",
        ]
        box_w   = min(44, W - 4)
        box_h   = len(items) + 4
        box_y   = max(1, (H - box_h) // 2)
        box_x   = max(0, (W - box_w) // 2)
        inner_x = box_x + 2
        inner_w = box_w - 4

        self._draw_box(box_y, box_x, box_h, box_w, title="  Menu — Esc to close  ")
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

        self._draw_box(box_y, box_x, box_h, box_w, title="  Pick color  ")
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

    def force_full_redraw(self) -> None:
        """Force a complete terminal repaint.
        clearok tells curses to redraw every cell from scratch on the next
        doupdate — this overwrites any sixel pixels left on screen."""
        self.stdscr.clearok(True)
        self.stdscr.touchwin()
        self.stdscr.noutrefresh()
        for w in (self.win_header, self.win_rooms, self.win_chat,
                  self.win_input, self.win_users):
            try:
                w.clearok(True)
                w.touchwin()
                w.noutrefresh()
            except Exception:
                pass
        curses.doupdate()

    def draw_all(self, rooms: List[Dict], current_room_id: Optional[int],
                 connected_list: List[Dict], own_username: str,
                 typing_list: Optional[List[str]] = None,
                 own_user: Optional[Dict] = None,
                 unread_checker: Optional[Callable] = None) -> None:
        self._own_user = own_user
        if typing_list is not None:
            self.typing_list = [u for u in typing_list if not self._is_hidden(u)]
        try:
            # Rebuild windows before any drawing if input height changed, so all
            # panels use consistent window objects throughout the draw cycle.
            if self._update_input_height():
                self._build_windows()
            self._draw_header(rooms, current_room_id, own_username)
            self._draw_rooms(rooms, current_room_id, connected_list, unread_checker=unread_checker, own_username=own_username)
            self._draw_chat(own_username)
            self._draw_users(connected_list, current_room_id, own_user=self._own_user)
            self._draw_input()
            self.stdscr.noutrefresh()
            if self._cursor_yx and not self.menu_open:
                curses.curs_set(1)
                curses.setsyx(*self._cursor_yx)
            else:
                curses.curs_set(0)
            # Paint image popup chrome, then menu on top (menu wins if both open)
            if self._overlay is not None:
                _dbg.debug('draw_all: calling overlay.draw() state=%s dirty=%s', self._overlay._state, self._overlay._dirty)
                self._overlay.draw()
            if self.menu_open:
                self._draw_menu(own_username)
                self.stdscr.noutrefresh()
            curses.doupdate()
            # Re-stamp sixel image every frame after doupdate().
            # touchwin() in draw() forces curses to repaint all popup cells each frame,
            # which erases the sixel pixels. So we re-place after every doupdate.
            if self._overlay is not None and self._overlay._state == 'ready':
                if self._overlay._pending_place:
                    _dbg.debug('draw_all: calling _place() (pending)')
                    self._overlay._pending_place = False
                self._overlay._place()
            elif self._overlay is not None:
                _dbg.debug('draw_all: overlay state=%s, skipping place', self._overlay._state)
        except Exception as _e:
            _dbg.debug('draw_all: EXCEPTION %s: %s', type(_e).__name__, _e, exc_info=True)

    # ── Header ────────────────────────────────────────────────────────

    def _draw_header(self, rooms: List[Dict], room_id: Optional[int], username: str) -> None:
        w = self.win_header
        w.bkgd(' ', curses.color_pair(C_HEADER))
        w.erase()
        _, W  = w.getmaxyx()
        room  = next((r for r in rooms if r.get("id") == room_id), None)
        if room:
            rname_raw = _room_display_name(room, username)
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

        # Count users per room — use rooms[-1] as the user's active room
        # (rooms[0] is a permanent base subscription that never changes).
        room_counts: Dict[int, int] = {}
        for session in connected_list:
            rooms_s = session.get("rooms") or []
            if not rooms_s:
                continue
            try:
                r = rooms_s[-1]
                k = int(r.get("id", r) if isinstance(r, dict) else r)
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
            name = _room_display_name(room, own_username)
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

    @staticmethod
    def _wrap_with_spans(line: str, width: int) -> List[tuple]:
        """Wrap one logical line and annotate each chunk with URL/button spans.

        Returns list of (chunk_str, url_spans, btn_spans) where:
          url_spans = [(start, end, url_value), ...]
          btn_spans = [(start, end, title, action), ...]
        All positions are relative to the start of the chunk string.
        """
        btn_matches = list(BUTTON_RE.finditer(line))
        display     = BUTTON_RE.sub(lambda m: f'[{m.group(1)}]', line)
        btn_display_spans = []
        offset = 0
        for m in btn_matches:
            title, action = m.group(1), m.group(2)
            shrink  = (m.end() - m.start()) - (len(title) + 2)
            d_start = m.start() - offset
            btn_display_spans.append((d_start, d_start + len(title) + 2, title, action))
            offset += shrink
        url_spans = [(m.start(), m.end(), m.group()) for m in URL_RE.finditer(display)]
        chunks    = _cols_aware_wrap(display, width)
        result    = []
        orig_pos  = 0
        for ch in chunks:
            ci = display.find(ch, orig_pos)
            if ci == -1: ci = orig_pos
            ch_end   = ci + len(ch)
            orig_pos = ch_end
            ch_urls  = []
            for us, ue, uval in url_spans:
                os2, oe2 = max(us, ci), min(ue, ch_end)
                if os2 < oe2: ch_urls.append((os2 - ci, oe2 - ci, uval))
            ch_btns  = []
            for ds, de, title, action in btn_display_spans:
                os2, oe2 = max(ds, ci), min(de, ch_end)
                if os2 < oe2: ch_btns.append((os2 - ci, oe2 - ci, title, action))
            result.append((ch, ch_urls, ch_btns))
        return result

    @staticmethod
    def _msg_line_count(msg: dict, usable_w: int) -> int:
        """Return the number of terminal rows a message will occupy."""
        prefix     = len(msg["ts"]) + 1 + len(msg["user"]) + 2
        disp       = BUTTON_RE.sub(lambda m: f'[{m.group(1)}]', msg["content"])
        nlines     = sum(
            len(_cols_aware_wrap(ln, max(8, usable_w - prefix)))
            for ln in disp.split("\n")
        )
        if msg.get("quoted"):    nlines += 1
        if msg.get("reactions"): nlines += 1
        return nlines

    def _draw_message(
        self, w, msg: dict, mi: int, row: int,
        H: int, W: int, margin: int, usable_w: int,
        own_username: str,
        skip_lines: int = 0,
    ) -> int:
        """Render one message starting at *row*. Returns the next free row.
        skip_lines: skip this many visual lines from the top of the message
        (for partial display when the message is clipped at the viewport top).

        Rendering is lazy: whole logical lines that fall entirely within the
        skip zone are fast-pathed with a cheap line-count call so we never do
        O(total_lines) URL/button span work for a single extremely long message.
        """
        ts, user, msg_content = msg["ts"], msg["user"], msg["content"]
        is_sel = (self.scroll_cursor == mi)
        sel_a  = curses.color_pair(C_MSG_SELECT)
        qt_a   = curses.color_pair(C_TIMESTAMP)
        prefix = len(ts) + 1 + len(user) + 2
        width  = max(8, usable_w - prefix)
        _skip  = skip_lines   # mutable counter — decremented as we skip lines

        # ── Quoted line ───────────────────────────────────────────────
        qdata = msg.get("quoted")
        if qdata and row < H - 1:
            q_ts     = qdata.get("ts", "")
            q_prefix = f"  ↩ {qdata['user']}"
            if q_ts:
                q_prefix += f" [{q_ts}]"
            q_prefix += ": "
            avail  = max(4, W - margin - len(q_prefix) - 1)
            q_text = qdata["text"].replace("\n", " ").strip()
            if len(q_text) > avail:
                q_text = q_text[:avail - 1] + "…"
            q_line = q_prefix + q_text
            if _skip > 0:
                _skip -= 1
            else:
                try:
                    if is_sel:
                        w.addstr(row, 0, " " * (W - 1), sel_a)
                    w.addstr(row, margin, q_line[:max(0, W - margin - 1)],
                             sel_a if is_sel else qt_a)
                except curses.error:
                    pass
                row += 1

        # ── Wrapped content lines (lazy) ──────────────────────────────
        # Only compute interactables for selected messages (avoids O(n) regex
        # on every frame for non-selected messages).
        _msg_interactables = _get_interactables(msg_content) if is_sel else []
        wi = 0  # cumulative visual-line index across all logical lines

        for _ln in msg_content.split("\n"):
            if row >= H - 1:
                break
            _is_gt = _ln.lstrip().startswith(">")

            if _skip > 0:
                # Fast-path: count visual lines without computing URL/button
                # spans.  This keeps rendering O(screen_height) even for
                # messages with thousands of visual lines above the viewport.
                _disp = BUTTON_RE.sub(lambda m: f'[{m.group(1)}]', _ln)
                _vis  = len(_cols_aware_wrap(_disp, width))
                if _skip >= _vis:
                    # Entire logical line is above the viewport — skip it.
                    _skip -= _vis
                    wi    += _vis
                    continue
                # Partial skip: the visible portion starts partway through
                # this logical line.  Build span data only for the visible
                # tail, advancing wi by the number of skipped items so the
                # loop body never has to iterate over off-screen chunks.
                items  = list(ChatUI._wrap_with_spans(_ln, width))
                wi    += _skip
                items  = items[_skip:]
                _skip  = 0
            else:
                items = list(ChatUI._wrap_with_spans(_ln, width))

            for chunk, chunk_urls, chunk_btns in items:
                if row >= H - 1:
                    break
                is_first = (wi == 0)
                wi += 1

                col = margin

                def _draw_segment(txt, base_attr,
                                   url_ranges=chunk_urls, btn_ranges=chunk_btns):
                    nonlocal col
                    spans = sorted(
                        [(s, e, "url", uv)  for s, e, uv    in url_ranges] +
                        [(s, e, "btn", act) for s, e, _, act in btn_ranges],
                        key=lambda x: x[0],
                    )
                    i2 = 0
                    for ss, se, kind, action in spans:
                        if col >= W - 1: break
                        if ss > i2:
                            plain = _cols_slice(txt[i2:ss], max(0, W - col - 1))
                            try: w.addstr(row, col, plain, base_attr)
                            except curses.error: pass
                            col += _str_cols(plain)
                        seg = _cols_slice(txt[ss:se], max(0, W - col - 1))
                        focused_val = (_msg_interactables[self.btn_cursor % len(_msg_interactables)][0]
                                       if is_sel and _msg_interactables else None)
                        if kind == "url":
                            seg_attr = (base_attr | curses.A_UNDERLINE | curses.A_REVERSE
                                        if is_sel and action == focused_val
                                        else base_attr | curses.A_UNDERLINE)
                        else:
                            seg_attr = (base_attr | curses.A_BOLD | curses.A_REVERSE
                                        if is_sel and action == focused_val
                                        else base_attr | curses.A_BOLD)
                        try: w.addstr(row, col, seg, seg_attr)
                        except curses.error: pass
                        col += _str_cols(seg)
                        i2  = se
                    if i2 < len(txt) and col < W - 1:
                        plain = _cols_slice(txt[i2:], max(0, W - col - 1))
                        try: w.addstr(row, col, plain, base_attr)
                        except curses.error: pass
                        col += _str_cols(plain)

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
                            hex_col = msg.get("col", "")
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

                    remaining = chunk[:max(0, W - col - 1)]
                    if not is_sel and own_username and f"@{own_username}" in remaining:
                        needle = f"@{own_username}"
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
                        _draw_segment(remaining, sel_a if is_sel else (curses.color_pair(C_USER_ONLINE) if _is_gt else 0))
                except curses.error:
                    pass
                row += 1

        # ── Reaction bar ──────────────────────────────────────────────
        reactions = msg.get("reactions", {})
        if reactions and row < H - 1:
            rbar = "".join(f" {e}×{c} " for e, c in list(reactions.items())[:8])
            if _skip > 0:
                _skip -= 1
            else:
                try:
                    if is_sel:
                        w.addstr(row, 0, " " * (W - 1), sel_a)
                    w.addstr(row, margin, rbar[:max(0, W - margin - 1)],
                             sel_a if is_sel else curses.color_pair(C_SELF) | curses.A_BOLD)
                except curses.error:
                    pass
                row += 1

        return row

    def _draw_chat_statusbar(
        self, w, H: int, W: int, total: int, own_username: str,
    ) -> None:
        """Paint the status/scroll bar at the bottom of the chat window."""
        if self.scroll_offset or self.scroll_cursor >= 0:
            if self.scroll_cursor >= 0:
                sel_msg     = (self.messages[self.scroll_cursor]
                               if 0 <= self.scroll_cursor < len(self.messages) else None)
                sel_content = sel_msg.get("content", "") if sel_msg else ""
                link_hint   = "  o=open  <>=cycle" if _get_interactables(sel_content) else ""
                is_own      = sel_msg and sel_msg.get("user") == own_username
                del_hint    = "  ⌫=delete" if is_own else ""
                sel_hint    = f"  Spc=quote  e=edit{del_hint}{link_hint}"
            else:
                sel_hint = ""
            newest_idx  = max(0, total - 1 - self.scroll_offset)
            if self.scroll_cursor >= 0:
                pos = self.scroll_cursor + 1
            else:
                pos = newest_idx + 1
            status_text = f"  ↑ {pos}/{total}{sel_hint}  Shift+↓ bottom"
        elif self.typing_list:
            names       = ", ".join(self.typing_list[:3])
            suffix      = "…" if len(self.typing_list) > 3 else ""
            status_text = f"  ✎ {names}{suffix} typing…"
        else:
            if self.status_until and time.monotonic() > self.status_until:
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
            w.noutrefresh()
            return

        self.scroll_offset = max(0, min(self.scroll_offset, total - 1))
        newest_idx = total - 1 - self.scroll_offset

        # ── Layout pass: decide which messages fit on screen ──────────
        rows_avail  = H - 1
        render_msgs: List[int] = []
        rows_used   = 0
        skip_top    = 0   # lines to skip from the top of the oldest (topmost) message
        for i in range(newest_idx, -1, -1):
            nlines = self._msg_line_count(self.messages[i], usable_w)
            if rows_used + nlines > rows_avail:
                # This message doesn't fully fit. If we already have messages,
                # partially show this one — skip its top lines to fill the screen.
                overflow = (rows_used + nlines) - rows_avail
                skip_top = overflow
                render_msgs.append(i)
                rows_used = rows_avail
                break
            render_msgs.append(i)
            rows_used += nlines
            if rows_used >= rows_avail:
                break

        render_msgs.reverse()
        oldest_idx               = render_msgs[0] if render_msgs else newest_idx
        self._last_msg_range     = (oldest_idx, newest_idx)
        self._last_visible_count = len(render_msgs)

        # Reset line-scroll when the topmost message changes (new layout)
        if oldest_idx != self._last_oldest_idx:
            self.scroll_line_offset = 0
            self._last_oldest_idx   = oldest_idx
        # Clamp and apply sub-message line offset
        self._last_natural_skip     = skip_top
        self.scroll_line_offset     = max(0, min(self.scroll_line_offset, skip_top))
        skip_top                    = skip_top - self.scroll_line_offset
        self._last_skip_top         = skip_top

        if self.scroll_cursor >= 0:
            self.scroll_cursor = max(oldest_idx, min(newest_idx, self.scroll_cursor))

        # ── Render pass ───────────────────────────────────────────────
        row = max(0, H - 1 - rows_used)
        for mi in render_msgs:
            skip = skip_top if mi == render_msgs[0] else 0
            row = self._draw_message(
                w, self.messages[mi], mi, row,
                H, W, margin, usable_w, own_username,
                skip_lines=skip,
            )

        self._draw_chat_statusbar(w, H, W, total, own_username)
        w.noutrefresh()

    # ── Users sidebar ─────────────────────────────────────────────────

    # Motto scrolls one character every this many seconds
    _MOTTO_CHAR_INTERVAL = 0.25

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
            uname = session.get('user', {}).get('username', '') or ''
            if self._is_hidden(uname):
                continue
            rooms = session.get("rooms") or []
            try:
                def _rid(r):
                    return int(r.get("id", r) if isinstance(r, dict) else r)
                # rooms[-1] is the user's active chat room; rooms[0] is a
                # permanent base subscription that is never removed.
                in_cur = current_room_id is not None and bool(rooms) and _rid(rooms[-1]) == current_room_id
            except (ValueError, TypeError):
                in_cur = False
            if in_cur:
                in_room.append(session)
            else:
                out_room.append(session)
        ordered = in_room + out_room
        self._ordered_users = ordered  # kept in sync for key handler

        title = (" ▶ USERS" if focused else "   USERS") + f" — {len(connected_list)}"
        try:
            w.addstr(0, 0, title[:W].ljust(W),
                     curses.color_pair(C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

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

        # Build prefix-sum of row heights once — O(n) instead of O(n²)
        row_starts = [0] * (n + 1)
        for j in range(n):
            row_starts[j + 1] = row_starts[j] + _rows(ordered[j])

        cursor_row_top = row_starts[self.user_cursor]
        cursor_row_bot = row_starts[self.user_cursor + 1] - 1
        viewport_h     = H - 1  # row 0 is title

        # Scroll so cursor is visible
        scroll_row = row_starts[self.user_scroll]
        if cursor_row_top < scroll_row:
            self.user_scroll = self.user_cursor
        elif cursor_row_bot >= scroll_row + viewport_h:
            # Advance scroll until cursor fits
            while row_starts[self.user_scroll] + viewport_h <= cursor_row_bot:
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

            # Name row — dot occupies a fixed 2-cell slot (col 2–3) so that
            # ambiguous-width characters like ◐ never bleed into the username.
            try:
                w.addstr(row, 1, " " * (W - 2), name_attr)
                w.addstr(row, 2, dot,            dot_attr)
                w.addstr(row, 3, " ",            name_attr)   # clear second cell of slot
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
                phase_ch = 0 if self._overlay is not None else int(time.monotonic() / self._MOTTO_CHAR_INTERVAL) % plen_ch
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
        vis_w   = max(1, W - 4)   # usable text width inside borders
        text_rows = H - 2          # rows between top and bottom border

        w.bkgdset(' ', curses.color_pair(C_BASE))
        w.erase()

        try:
            border_attr = curses.color_pair(C_BORDER) | (curses.A_BOLD if focused else 0)
            w.border(0, 0, 0, 0, 0, 0, 0, 0)  # draw sides with bkgd attr first
            # Redraw entire border with correct colour and rounded corners
            H2, W2 = w.getmaxyx()
            for col in range(1, W2 - 1):
                w.addch(0,      col, '─', border_attr)
                w.addch(H2 - 1, col, '─', border_attr)
            for row in range(1, H2 - 1):
                w.addch(row, 0,      '│', border_attr)
                w.addch(row, W2 - 1, '│', border_attr)
            w.addch(0,      0,      '╭', border_attr)
            w.addch(0,      W2 - 1, '╮', border_attr)
            w.addch(H2 - 1, 0,      '╰', border_attr)
            w.addch(H2 - 1, W2 - 1, '╯', border_attr)
        except curses.error:
            pass

        # Build list of visual lines and find cursor visual position
        # Each logical line (split on \n) may wrap into multiple visual lines
        visual_lines: List[str] = []   # text of each visual line
        cur_vrow = 0                    # visual row of cursor
        cur_vcol = 0                    # visual col of cursor

        logical_lines = self.input_buf.split('\n')
        char_idx = 0
        for li, line in enumerate(logical_lines):
            # _cols_aware_wrap_offsets returns (chunk, start_in_line) pairs.
            # Using start_in_line directly avoids the off-by-one that occurs
            # when a word-break wrap (no separator consumed) is incorrectly
            # treated the same as a space-split wrap (+1 separator).
            pairs = _cols_aware_wrap_offsets(line, vis_w)
            for chunk, start_in_line in pairs:
                vrow = len(visual_lines)
                chunk_start = char_idx + start_in_line
                chunk_end   = chunk_start + len(chunk)
                if chunk_start <= self.cursor_pos <= chunk_end:
                    cur_vrow = vrow
                    cur_vcol = self.cursor_pos - chunk_start
                visual_lines.append(chunk)
            char_idx += len(line) + 1  # +1 for \n

        # Vertical scroll: keep cursor visible
        if cur_vrow < self.input_vscroll:
            self.input_vscroll = cur_vrow
        elif cur_vrow >= self.input_vscroll + text_rows:
            self.input_vscroll = cur_vrow - text_rows + 1

        # Draw visible lines
        attr = curses.color_pair(C_BASE) | curses.A_BOLD
        if visual_lines and focused or any(visual_lines):
            for row_offset in range(text_rows):
                vrow = self.input_vscroll + row_offset
                if vrow < len(visual_lines):
                    line_text = visual_lines[vrow]
                    if line_text:
                        try:
                            w.addstr(1 + row_offset, 2, line_text, attr)
                        except curses.error:
                            pass
        elif not focused:
            try:
                ph = "Type a message…"[:vis_w]
                w.addstr(1, 2, ph, curses.color_pair(C_TIMESTAMP))
            except curses.error:
                pass

        # Position terminal cursor
        if focused:
            try:
                screen_row = 1 + (cur_vrow - self.input_vscroll)
                screen_col = 2 + cur_vcol
                screen_col = min(screen_col, W - 2)
                wy, wx = w.getbegyx()
                self._cursor_yx = (wy + screen_row, wx + screen_col)
            except curses.error:
                self._cursor_yx = None
        else:
            self._cursor_yx = None

        # Scroll indicator on right border if content is scrolled
        if self.input_vscroll > 0:
            try:
                w.addch(1, W - 1, '▲', curses.color_pair(C_TIMESTAMP))
            except curses.error:
                pass
        if self.input_vscroll + text_rows < len(visual_lines):
            try:
                w.addch(H - 2, W - 1, '▼', curses.color_pair(C_TIMESTAMP))
            except curses.error:
                pass

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
        self.input_buf      = ""
        self.cursor_pos     = 0
        self.input_vscroll  = 0
        if self.input_h != INPUT_H:
            self.input_h = INPUT_H
            try:
                self._build_windows()
            except curses.error:
                pass
        return t

    def _user_colour_pair(self, xterm_idx: int) -> int:
        """Allocate a colour pair for a fixed xterm-256 index (no init_color)."""
        if xterm_idx in self._colour_pair_cache:
            return self._colour_pair_cache[xterm_idx]
        pair     = self._next_pair
        max_pair = curses.COLOR_PAIRS - 1
        if pair > max_pair:
            _dbg.warning('colour pair pool exhausted (COLOR_PAIRS=%d); falling back to C_USERNAME', curses.COLOR_PAIRS)
            return C_USERNAME
        if pair > max_pair - 8:
            _dbg.warning('colour pair pool nearly full (%d/%d used)', pair - C_DYN_BASE, max_pair - C_DYN_BASE)
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
        self.scroll_cursor      = -1
        self.btn_cursor         = 0
        self.scroll_line_offset = 0

    def scroll_up(self, n: int = 1) -> None:
        self.scroll_offset = min(self.scroll_offset + n, max(0, len(self.messages) - 1))
        # Only fetch more history when we've scrolled to the very top of loaded messages
        if self._last_msg_range[0] == 0:
            self._maybe_fetch_history()

    def _maybe_fetch_history(self) -> None:
        """Trigger a history fetch. Only fires when called from _draw_chat
        with blank space at the top — guarded by history_fetching/exhausted."""
        if self.history_exhausted or self.history_fetching:
            return
        if self._history_fetch_cb is None:
            return
        self.history_fetching = True
        asyncio.ensure_future(self._history_fetch_cb())

    def scroll_down(self, n: int = 1) -> None:
        self.scroll_offset = max(0, self.scroll_offset - n)

    def scroll_bottom(self) -> None:
        self.scroll_offset = 0

    def reset_history_state(self) -> None:
        self.history_exhausted   = False
        self.history_fetching    = False
        self._history_empty_count = 0