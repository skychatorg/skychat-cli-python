"""
Shared constants: config paths, colour pair IDs, protocol sentinels.
No imports from other skychat_tui modules — everything else imports from here.
"""

import os
import re
import curses

# ── Server ────────────────────────────────────────────────────────────────────
DEFAULT_WSS_URL = os.environ.get("SKYCHAT_URL", "wss://skych.at/api/ws")
DEFAULT_ROOM_ID = 0

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.expanduser("~/.skychat_tui.json")

# ── Binary message types ──────────────────────────────────────────────────────
BINARY_MSG_AUDIO  = 0
BINARY_MSG_CURSOR = 1

# ── Fallback 8-colour curses constants (used when 256-colour unavailable) ─────
FB_BG     = curses.COLOR_BLACK
FB_FG     = curses.COLOR_WHITE
FB_ACCENT = curses.COLOR_MAGENTA
FB_CYAN   = curses.COLOR_CYAN
FB_GREEN  = curses.COLOR_GREEN
FB_YELLOW = curses.COLOR_YELLOW
FB_RED    = curses.COLOR_RED

# ── Colour pair IDs ───────────────────────────────────────────────────────────
# 1–20  : static UI pairs (never reassigned)
# 21–31 : reserved for caca ASCII-art rendering
# 32+   : dynamic per-user nick colour pairs
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

CACA_PAIR_BASE = 21
CACA_PAIR_END  = 31

# ── AFK threshold ─────────────────────────────────────────────────────────────
AFK_SECONDS = 5 * 60

# ── Message / content regexes ─────────────────────────────────────────────────
TAG_RE     = re.compile(r'<[^>]+>')
STICKER_RE = re.compile(r':([A-Za-z0-9_-]+):')
URL_RE     = re.compile(r'https?://\S+')
BUTTON_RE  = re.compile(r'\[\[([^/\[]+)/([^\[]+?)(?:\]\]|(?=\[\[))')
