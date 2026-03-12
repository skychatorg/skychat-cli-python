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
import time
import unicodedata
import webbrowser
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("Missing dependency. Install with:  pip install websockets")
    sys.exit(1)

try:
    from PIL import Image as _PILImage
    _PILLOW_OK = True
except ImportError:
    _PILImage  = None
    _PILLOW_OK = False

try:
    import caca as _caca
    from caca.canvas  import Canvas  as _CacaCanvas
    from caca.dither  import Dither  as _CacaDither
    from caca.display import Display as _CacaDisplay
    _CACA_OK = True
except ImportError:
    _caca = _CacaCanvas = _CacaDither = _CacaDisplay = None
    _CACA_OK = False


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
BUTTON_RE  = re.compile(r'\[\[([^/\[]+)/([^\[]+?)(?:\]\]|(?=\[\[))')


def _draw_box(stdscr, y: int, x: int, h: int, w: int,
              colour_pair: int, title: str = "") -> None:
    """Draw a rounded-corner box on *stdscr* using *colour_pair*."""
    attr = colour_pair | curses.A_BOLD
    H, W = stdscr.getmaxyx()
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


def _printable_char(key) -> str:
    """Return the printable character for *key*, or empty string if not printable."""
    if isinstance(key, str) and key.isprintable():
        return key
    if isinstance(key, int) and 32 <= key < 127:
        return chr(key)
    return ""


def _get_interactables(content: str) -> List[Tuple[str, str]]:
    """Return list of (value, kind) for all buttons and URLs in content.
    kind is 'btn' or 'url'.  Buttons come first, then URLs."""
    btns = [(m.group(2), 'btn') for m in BUTTON_RE.finditer(content)]
    urls = [(u, 'url') for u in URL_RE.findall(BUTTON_RE.sub('', content))]
    return btns + urls


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


def _cols_aware_wrap(text: str, width: int) -> List[str]:
    """Word-aware, column-aware line wrap.  Keeps whole words together and
    only breaks mid-word when a single word exceeds the available width.
    Also splits after ']' so button runs don't overflow."""
    if not text:
        return [""]

    # Split into tokens preserving spaces: alternate between word and space runs
    import re as _re2
    tokens = _re2.split(r'(\s+)', text)

    lines: List[str] = []
    current      = ""
    current_cols = 0

    for token in tokens:
        if not token:
            continue
        token_cols = _str_cols(token)

        if current_cols + token_cols <= width:
            # Token fits on current line
            current      += token
            current_cols += token_cols
        elif token_cols > width:
            # Token is wider than the line — must break it character by character
            # First flush current line
            if current.strip():
                lines.append(current.rstrip())
            current, current_cols = "", 0
            for ch in token:
                ch_w = _char_width(ch)
                if current_cols + ch_w > width:
                    if current:
                        lines.append(current)
                    current, current_cols = ch, ch_w
                else:
                    current      += ch
                    current_cols += ch_w
        else:
            # Token doesn't fit — flush current line and start fresh
            if current.strip():
                lines.append(current.rstrip())
            # Don't carry leading whitespace to a new line
            stripped = token.lstrip()
            current      = stripped
            current_cols = _str_cols(stripped)

    if current.strip():
        lines.append(current.rstrip())

    return lines or [""]




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
    rooms = session.get('rooms') or []
    if not rooms:
        return '○', C_USER_RECENT
    last = session.get('lastInteractionTime')
    if last is not None:
        try:
            if time.time() - float(last) > AFK_SECONDS:
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
# Kitty graphics protocol
# ─────────────────────────────────────────────────────────────────────────────

def _query_cell_pixels() -> Tuple[int, int]:
    """Query terminal for cell size in pixels via CSI 16 t.
    Returns (cell_w_px, cell_h_px). Falls back to (10, 20) if unsupported."""
    import select, termios, tty
    try:
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        os.write(1, b'\033[16t')
        ready, _, _ = select.select([fd], [], [], 0.5)
        resp = b''
        if ready:
            resp = os.read(fd, 32)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        import re as _re
        m = _re.search(rb'\033\[6;(\d+);(\d+)t', resp)
        if m:
            cw, ch = int(m.group(2)), int(m.group(1))
            _dbg.debug('_query_cell_pixels: raw=%r -> w=%d h=%d', resp, cw, ch)
            if 4 <= cw <= 64 and 4 <= ch <= 128:
                return cw, ch
    except Exception as e:
        _dbg.debug('_query_cell_pixels failed: %s', e)
    return 10, 20


def _probe_sixel() -> bool:
    """Ask the terminal if it supports sixel via DA1 (CSI c).
    Response contains '4' in the list if sixel is supported, e.g. ESC[?64;4c"""
    import select, termios, tty
    try:
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        os.write(1, b'\033[c')
        ready, _, _ = select.select([fd], [], [], 0.5)
        resp = b''
        if ready:
            resp = os.read(fd, 64)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        _dbg.debug('_probe_sixel: DA1 raw=%r', resp)
        # Response: ESC [ ? <attrs> c  — attrs are semicolon-separated numbers
        import re as _re
        m = _re.search(rb'\033\[\?([0-9;]+)c', resp)
        if m:
            attrs = m.group(1).split(b';')
            return b'4' in attrs
    except Exception as e:
        _dbg.debug('_probe_sixel failed: %s', e)
    return False


def _probe_kitty() -> bool:
    """Send a 1×1 dummy Kitty graphics command and check for an OK response."""
    import select, termios, tty, base64
    try:
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        # Minimal 1×1 transparent PNG as base64 — just enough to get a response
        dummy = base64.standard_b64encode(b'\x89PNG\r\n\x1a\n' + b'\x00' * 20)
        os.write(1, b'\033_Ga=q,s=1,v=1,f=32;' + dummy[:8] + b'\033\\')
        ready, _, _ = select.select([fd], [], [], 0.5)
        resp = b''
        if ready:
            resp = os.read(fd, 64)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        _dbg.debug('_probe_kitty: raw=%r', resp)
        return b'OK' in resp or b'ok' in resp
    except Exception as e:
        _dbg.debug('_probe_kitty failed: %s', e)
    return False


_CELL_PX:    Optional[Tuple[int, int]] = None  # (w_px, h_px), queried once
# Protocol selection: 'sixel', 'kitty', or None (disabled)
_IMG_PROTO:  Optional[str]             = None  # None = not yet probed

IMAGE_EXTS = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif', '.avif',
})

def _is_image_url(url: str) -> bool:
    from urllib.parse import urlparse
    return any(urlparse(url).path.lower().endswith(e) for e in IMAGE_EXTS)


def _detect_protocol() -> Optional[str]:
    """Detect the best available image protocol.
    Order of preference: sixel > kitty > caca.
    Sixel/kitty require Pillow. Caca is a pure ASCII-art fallback.
    Uses env vars first; falls back to terminal probing for ambiguous cases.
    Returns 'sixel', 'kitty', 'caca', or None."""
    term         = os.environ.get('TERM', '')
    term_program = os.environ.get('TERM_PROGRAM', '')
    kitty_id     = os.environ.get('KITTY_WINDOW_ID', '')

    if _PILLOW_OK:
        # --- Kitty: definitive env var ---
        if kitty_id or 'kitty' in term:
            _dbg.debug('_detect_protocol: kitty via env')
            return 'kitty'

        # --- Sixel: known terminals ---
        if term in ('foot', 'foot-extra'):
            _dbg.debug('_detect_protocol: sixel via env (foot)')
            return 'sixel'
        if 'mlterm' in term or 'yaft' in term:
            _dbg.debug('_detect_protocol: sixel via env (mlterm/yaft)')
            return 'sixel'
        if term_program == 'iTerm.app':
            _dbg.debug('_detect_protocol: sixel via env (iTerm2)')
            return 'sixel'

        # --- Ambiguous / unknown: probe the terminal ---
        _dbg.debug('_detect_protocol: probing terminal (TERM=%r)', term)
        if _probe_sixel():
            _dbg.debug('_detect_protocol: sixel confirmed by probe')
            return 'sixel'
        if _probe_kitty():
            _dbg.debug('_detect_protocol: kitty confirmed by probe')
            return 'kitty'

    # --- Caca fallback: works in any colour terminal, no pixel protocol needed ---
    import shutil as _shutil
    _img2txt_available = bool(_shutil.which('img2txt'))
    if _CACA_OK or _img2txt_available:
        _dbg.debug('_detect_protocol: caca fallback (bindings=%s img2txt=%s)',
                   _CACA_OK, _img2txt_available)
        return 'caca'

    _dbg.debug('_detect_protocol: no image protocol detected (Pillow=%s caca=%s)',
               _PILLOW_OK, _CACA_OK)
    return None


import logging as _logging
_logging.basicConfig(filename='/tmp/skychat_img_debug.log', level=_logging.DEBUG,
                     format='%(asctime)s %(message)s')
_dbg = _logging.getLogger('img')


# ── Upload capability detection ────────────────────────────────────────────

def _detect_upload() -> Optional[str]:
    """Detect which clipboard image tool is available.
    Returns 'aiohttp', 'xclip', 'wl-paste', 'pbpaste', or None."""
    try:
        import aiohttp as _aiohttp  # noqa: F401
        return 'aiohttp'
    except ImportError:
        pass
    # aiohttp not available — still support file:// / path paste via stdlib
    # but check for clipboard image tools
    import shutil
    if shutil.which('xclip'):
        return 'xclip'
    if shutil.which('wl-paste'):
        return 'wl-paste'
    if shutil.which('pbpaste'):
        return 'pbpaste'
    return None

_UPLOAD_METHOD: Optional[str] = None   # set lazily on first use

def _get_upload_method() -> Optional[str]:
    global _UPLOAD_METHOD
    if _UPLOAD_METHOD is None:
        _UPLOAD_METHOD = _detect_upload() or 'stdlib'
    return _UPLOAD_METHOD

def _wss_to_http(wss_url: str) -> str:
    """Convert wss://host/path to https://host"""
    return wss_url.replace('wss://', 'https://').replace('ws://', 'http://').split('/api/')[0]

async def _upload_file_bytes(data: bytes, filename: str, base_url: str,
                              token: Optional[dict] = None) -> str:
    """Upload raw bytes as multipart/form-data. Returns the full URL of the uploaded file.

    Auth strategy: SkyChat's web client uses a browser session cookie the TUI never
    receives. We instead send the auth token in every way the server might accept:
    - As a 'token' query parameter in the URL
    - As a 'token' field in the multipart form body
    - As an Authorization header (Bearer)
    """
    import uuid
    token_json = json.dumps(token) if token else None

    endpoint = base_url.rstrip('/') + '/api/upload'
    method = _get_upload_method()

    # The upload plugin has no auth — but the reverse proxy may enforce CSRF checks.
    # Mimic a browser request: send Origin + Referer so the proxy accepts us.
    browser_headers = {
        'Origin':  base_url,
        'Referer': base_url + '/',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) skychat-tui/1.0',
    }

    # ── aiohttp path ──────────────────────────────────────────────────
    if method == 'aiohttp':
        import aiohttp
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            form = aiohttp.FormData()
            import mimetypes as _mt
            mime = _mt.guess_type(filename)[0] or 'image/png'
            form.add_field('file', data, filename=filename,
                           content_type=mime)
            async with session.post(endpoint, data=form, headers=browser_headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 403:
                    body = await resp.text()
                    raise RuntimeError(f'403 Forbidden: {body[:120]}')
                result = await resp.json(content_type=None)
    else:
        # ── stdlib urllib multipart path ──────────────────────────────
        import urllib.request, urllib.error
        boundary = uuid.uuid4().hex
        parts = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {__import__("mimetypes").guess_type(filename)[0] or "image/png"}\r\n\r\n'
        ).encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            **browser_headers,
        }
        req = urllib.request.Request(endpoint, data=parts, headers=headers, method='POST')
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        loop = asyncio.get_running_loop()
        def _do_req():
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors='replace')[:120]
                raise RuntimeError(f'{e.code} {e.reason}: {body}')
        result = await loop.run_in_executor(None, _do_req)

    if result.get('status') == 500:
        raise RuntimeError(result.get('message', 'Upload failed'))
    path = result.get('path', '')
    return base_url.rstrip('/') + '/' + path.lstrip('/')

async def _upload_local_file(path: str, base_url: str,
                              token: Optional[dict] = None) -> str:
    """Read a local file and upload it."""
    import os.path, mimetypes
    path = path.strip()
    if not os.path.isfile(path):
        raise RuntimeError(f'File not found: {path}')
    filename = os.path.basename(path)
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: open(path, 'rb').read())
    return await _upload_file_bytes(data, filename, base_url, token)

async def _grab_clipboard_image() -> Optional[bytes]:
    """Try to grab image bytes from the system clipboard. Returns None if unavailable."""
    method = _get_upload_method()
    loop   = asyncio.get_running_loop()
    try:
        if method == 'xclip':
            proc = await asyncio.create_subprocess_exec(
                'xclip', '-selection', 'clipboard', '-t', 'image/png', '-o',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return out if proc.returncode == 0 and out else None
        elif method == 'wl-paste':
            proc = await asyncio.create_subprocess_exec(
                'wl-paste', '--type', 'image/png',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return out if proc.returncode == 0 and out else None
        elif method == 'pbpaste':
            # pbpaste doesn't support image; try osascript
            script = 'set img to (the clipboard as «class PNGf»)\nreturn img'
            proc = await asyncio.create_subprocess_exec(
                'osascript', '-e', script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return out if proc.returncode == 0 and out else None
    except Exception:
        pass
    return None

# ── Sixel ──────────────────────────────────────────────────────────────────

def _encode_sixel(img_rgb: bytes, w: int, h: int) -> bytes:
    """Encode raw RGB bytes as a sixel stream."""
    if _PILImage is None:
        raise RuntimeError('Pillow not available')
    img   = _PILImage.frombytes('RGB', (w, h), img_rgb)
    img_p = img.quantize(colors=256, method=_PILImage.Quantize.MEDIANCUT, dither=0)
    palette_raw = img_p.getpalette()
    pixels      = list(img_p.tobytes())
    ncolors     = len(palette_raw) // 3

    buf  = bytearray()
    buf += b'\033P0;1;8q'
    buf += f'"1;1;{w};{h}\n'.encode()
    for ci in range(ncolors):
        r = round(palette_raw[ci*3]     * 100 / 255)
        g = round(palette_raw[ci*3 + 1] * 100 / 255)
        b = round(palette_raw[ci*3 + 2] * 100 / 255)
        buf += f'#{ci};2;{r};{g};{b}'.encode()

    num_bands = (h + 5) // 6
    for band in range(num_bands):
        y0   = band * 6
        used: dict = {}
        for dy in range(6):
            y = y0 + dy
            if y >= h:
                break
            bit       = 1 << dy
            row_start = y * w
            for x in range(w):
                ci = pixels[row_start + x]
                if ci not in used:
                    used[ci] = bytearray(w)
                used[ci][x] |= bit
        first = True
        for ci, bits in used.items():
            if not first:
                buf += b'$'
            first = False
            buf += f'#{ci}'.encode()
            x = 0
            while x < w:
                val = bits[x]
                run = 1
                while x + run < w and bits[x + run] == val and run < 255:
                    run += 1
                sixel_char = val + 63
                if run >= 3:
                    buf += f'!{run}'.encode() + bytes([sixel_char])
                else:
                    buf += bytes([sixel_char] * run)
                x += run
        buf += b'-'

    buf += b'\033\\'
    return bytes(buf)


def _sixel_place(sixel_data: bytes, cell_x: int, cell_y: int) -> None:
    _dbg.debug('_sixel_place cell=(%d,%d) bytes=%d', cell_x, cell_y, len(sixel_data))
    buf  = bytearray()
    buf += b'\0337'
    buf += f'\033[{cell_y + 1};{cell_x + 1}H'.encode()
    buf += sixel_data
    buf += b'\0338'
    os.write(1, bytes(buf))


def _sixel_clear(cell_x: int, cell_y: int, cell_w: int, cell_h: int) -> None:
    buf  = bytearray()
    buf += b'\0337'
    for row in range(cell_h):
        buf += f'\033[{cell_y + row + 1};{cell_x + 1}H'.encode()
        buf += b' ' * cell_w
    buf += b'\0338'
    os.write(1, bytes(buf))


# ── Caca ───────────────────────────────────────────────────────────────────

# ── Caca colour pairs ──────────────────────────────────────────────────────
# We have the gap 21-31 (11 slots) between C_MSG_SELECT=20 and C_DYN_BASE=32.
# Pre-bake a fixed palette there: 8 "colour-on-black" pairs + 3 extras.
# All caca ANSI colours are snapped to one of these at render time — no
# dynamic init_pair calls, so nothing in the main UI can ever be clobbered.
#
#  Pair  fg                  bg
#  21    COLOR_WHITE         COLOR_BLACK   (default / reset)
#  22    COLOR_RED           COLOR_BLACK
#  23    COLOR_GREEN         COLOR_BLACK
#  24    COLOR_YELLOW        COLOR_BLACK
#  25    COLOR_BLUE          COLOR_BLACK
#  26    COLOR_MAGENTA       COLOR_BLACK
#  27    COLOR_CYAN          COLOR_BLACK
#  28    COLOR_BLACK         COLOR_BLACK   (solid black)
#  29    COLOR_WHITE         COLOR_RED     (bright highlight)
#  30    COLOR_BLACK         COLOR_WHITE   (inverse)
#  31    COLOR_WHITE         COLOR_BLUE    (blue bg)

_CACA_PAIR_BASE = 21   # first pair in our reserved block
_CACA_PAIR_END  = 31   # last  pair in our reserved block (inclusive)

# (fg_curses, bg_curses) -> pair number — filled by _init_caca_pairs()
_CACA_PAIR_MAP: dict = {}

def _init_caca_pairs() -> None:
    """Register the 11 fixed caca pairs.  Call once after curses.start_color()."""
    _pairs = [
        (curses.COLOR_WHITE,   curses.COLOR_BLACK),   # 21 default
        (curses.COLOR_RED,     curses.COLOR_BLACK),   # 22
        (curses.COLOR_GREEN,   curses.COLOR_BLACK),   # 23
        (curses.COLOR_YELLOW,  curses.COLOR_BLACK),   # 24
        (curses.COLOR_BLUE,    curses.COLOR_BLACK),   # 25
        (curses.COLOR_MAGENTA, curses.COLOR_BLACK),   # 26
        (curses.COLOR_CYAN,    curses.COLOR_BLACK),   # 27
        (curses.COLOR_BLACK,   curses.COLOR_BLACK),   # 28 solid black
        (curses.COLOR_WHITE,   curses.COLOR_RED),     # 29 highlight
        (curses.COLOR_BLACK,   curses.COLOR_WHITE),   # 30 inverse
        (curses.COLOR_WHITE,   curses.COLOR_BLUE),    # 31 blue bg
    ]
    for i, (fg, bg) in enumerate(_pairs):
        pair_n = _CACA_PAIR_BASE + i
        try:
            curses.init_pair(pair_n, fg, bg)
            _CACA_PAIR_MAP[(fg, bg)] = pair_n
        except Exception:
            pass

# ANSI 256-colour index → nearest curses colour constant
def _ansi256_to_curses(n: int) -> int:
    if n < 8:
        return [curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN,
                curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_MAGENTA,
                curses.COLOR_CYAN, curses.COLOR_WHITE][n]
    if n < 16:   # bright variants — map to same base colour
        return _ansi256_to_curses(n - 8)
    if n >= 232: # greyscale ramp 232-255
        return curses.COLOR_WHITE if (n - 232) > 11 else curses.COLOR_BLACK
    # 6×6×6 colour cube 16-231
    n -= 16
    b, tmp = n % 6, n // 6
    g, r   = tmp % 6, tmp // 6
    mx = max(r, g, b)
    if mx == 0:              return curses.COLOR_BLACK
    if r >= 4 and g < 3 and b < 3: return curses.COLOR_RED
    if g >= 4 and r < 3 and b < 3: return curses.COLOR_GREEN
    if b >= 4 and r < 3 and g < 3: return curses.COLOR_BLUE
    if r >= 3 and g >= 3 and b < 2: return curses.COLOR_YELLOW
    if r >= 3 and b >= 3 and g < 2: return curses.COLOR_MAGENTA
    if g >= 3 and b >= 3 and r < 2: return curses.COLOR_CYAN
    return curses.COLOR_WHITE

def _snap_caca_pair(fg: int, bg: int) -> int:
    """Return the curses color_pair int for the nearest pre-baked caca pair."""
    # Exact hit first
    if (fg, bg) in _CACA_PAIR_MAP:
        return curses.color_pair(_CACA_PAIR_MAP[(fg, bg)])
    # Snap bg: if bg is not black, try to find a pair with that bg;
    # otherwise fall back to fg-on-black.
    if bg != curses.COLOR_BLACK:
        candidate = (fg, bg)
        # Try swapping to a known bg
        for known_bg in (curses.COLOR_RED, curses.COLOR_WHITE, curses.COLOR_BLUE):
            if bg == known_bg and (fg, known_bg) in _CACA_PAIR_MAP:
                return curses.color_pair(_CACA_PAIR_MAP[(fg, known_bg)])
        # bg not in our set — drop to black bg
        bg = curses.COLOR_BLACK
    # fg on black
    if (fg, curses.COLOR_BLACK) in _CACA_PAIR_MAP:
        return curses.color_pair(_CACA_PAIR_MAP[(fg, curses.COLOR_BLACK)])
    return curses.color_pair(_CACA_PAIR_BASE)   # ultimate fallback: white on black


def _parse_ansi_to_spans(raw: bytes, cols: int, rows: int
                         ) -> 'List[List[Tuple[str,int]]]':
    """Parse ANSI-coloured img2txt/caca output into curses-renderable spans.

    Returns a list of `rows` lines; each line is a list of (text, curses_attr).
    Handles both basic (30-37/40-47) and 256-colour (38;5;N / 48;5;N) SGR codes.
    Colours are approximated to the 11 pre-baked caca pairs (21-31).
    """
    import re as _re
    tok_re  = _re.compile(rb'\033\[([0-9;]*)m|([^\033\n]+)|\n')
    ansi8   = [curses.COLOR_BLACK, curses.COLOR_RED,     curses.COLOR_GREEN,
               curses.COLOR_YELLOW, curses.COLOR_BLUE,   curses.COLOR_MAGENTA,
               curses.COLOR_CYAN,   curses.COLOR_WHITE]

    result: 'List[List[Tuple[str,int]]]' = []
    cur_line: 'List[Tuple[str,int]]'     = []
    cur_col  = 0
    fg       = curses.COLOR_WHITE
    bg       = curses.COLOR_BLACK
    bold     = False

    def flush_line():
        nonlocal cur_line, cur_col
        # Pad to full width so the popup interior stays clean
        if cur_col < cols:
            attr = _snap_caca_pair(curses.COLOR_WHITE, curses.COLOR_BLACK)
            cur_line.append((' ' * (cols - cur_col), attr))
        result.append(cur_line)
        cur_line = []
        cur_col  = 0

    for m in tok_re.finditer(raw):
        if m.group(0) == b'\n':
            flush_line()
            if len(result) >= rows:
                break
            continue

        if m.group(1) is not None:          # SGR escape
            params_raw = m.group(1)
            params = [int(x) for x in params_raw.split(b';') if x] if params_raw else [0]
            i = 0
            while i < len(params):
                p = params[i]
                if p == 0:
                    fg, bg, bold = curses.COLOR_WHITE, curses.COLOR_BLACK, False
                elif p == 1:
                    bold = True
                elif p == 22:
                    bold = False
                elif 30 <= p <= 37:
                    fg = ansi8[p - 30]
                elif p == 39:
                    fg = curses.COLOR_WHITE
                elif 40 <= p <= 47:
                    bg = ansi8[p - 40]
                elif p == 49:
                    bg = curses.COLOR_BLACK
                elif 90 <= p <= 97:          # bright fg → same colour
                    fg = ansi8[p - 90]
                elif 100 <= p <= 107:        # bright bg → same colour
                    bg = ansi8[p - 100]
                elif p == 38 and i + 2 < len(params) and params[i+1] == 5:
                    fg = _ansi256_to_curses(params[i+2]); i += 2
                elif p == 48 and i + 2 < len(params) and params[i+1] == 5:
                    bg = _ansi256_to_curses(params[i+2]); i += 2
                i += 1

        else:                               # plain text
            text = m.group(2).decode('utf-8', errors='replace')
            avail = cols - cur_col
            if avail <= 0 or not text:
                continue
            text  = text[:avail]
            attr  = _snap_caca_pair(fg, bg)
            if bold:
                attr |= curses.A_BOLD
            cur_line.append((text, attr))
            cur_col += len(text)

    # Flush any trailing line without a final newline
    if cur_line or len(result) < rows:
        flush_line()

    # Pad missing rows
    blank_attr = _snap_caca_pair(curses.COLOR_WHITE, curses.COLOR_BLACK)
    while len(result) < rows:
        result.append([(' ' * cols, blank_attr)])

    return result[:rows]


def _render_caca(img_path: str, cols: int, rows: int) -> 'List[List[Tuple[str,int]]]':
    """Render image to coloured curses spans via img2txt or caca bindings.
    Returns List[rows] of List[(text, curses_attr)] spans."""
    import subprocess, shutil

    img2txt = shutil.which('img2txt')
    if img2txt:
        try:
            result = subprocess.run(
                [img2txt, '--width', str(cols), '--height', str(rows),
                 '--format', 'utf8', img_path],
                capture_output=True, timeout=10)
            if result.returncode == 0:
                parsed = _parse_ansi_to_spans(result.stdout, cols, rows)
                _dbg.debug('_render_caca img2txt ok: %d lines', len(parsed))
                return parsed
        except Exception as e:
            _dbg.debug('_render_caca img2txt failed: %s', e)

    if _CACA_OK and _PILImage:
        try:
            img    = _PILImage.open(img_path).convert('RGB')
            iw, ih = img.size
            pixels = img.tobytes()
            cv     = _CacaCanvas(cols, rows)
            dither = _CacaDither(24, iw, ih, iw * 3, 0xff0000, 0x00ff00, 0x0000ff, 0)
            dither.bitmap(cv, 0, 0, cols, rows, pixels)
            export = cv.export_to_memory('utf8')
            parsed = _parse_ansi_to_spans(export, cols, rows)
            _dbg.debug('_render_caca bindings ok: %d lines', len(parsed))
            return parsed
        except Exception as e:
            _dbg.debug('_render_caca bindings failed: %s', e)

    raise RuntimeError('libcaca not available (install img2txt or python-caca)')


# ── Kitty ──────────────────────────────────────────────────────────────────

def _kitty_place(img_rgb: bytes, px_w: int, px_h: int,
                 cell_x: int, cell_y: int) -> None:
    """Send raw RGB pixels as a Kitty graphics command (f=24, a=T, q=2)."""
    import base64
    _dbg.debug('_kitty_place %dx%d cell=(%d,%d) bytes=%d',
               px_w, px_h, cell_x, cell_y, len(img_rgb))
    buf  = bytearray()
    buf += b'\0337'
    buf += f'\033[{cell_y + 1};{cell_x + 1}H'.encode()
    b64    = base64.standard_b64encode(img_rgb)
    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]
    for idx, chunk in enumerate(chunks):
        more = 0 if idx == len(chunks) - 1 else 1
        hdr  = (f'a=T,f=24,s={px_w},v={px_h},m={more},q=2'
                if idx == 0 else f'm={more},q=2')
        buf += b'\033_G' + hdr.encode() + b';' + chunk + b'\033\\'
    buf += b'\0338'
    os.write(1, bytes(buf))


def _kitty_clear(img_id: int) -> None:
    os.write(1, b'\0337\033_Ga=d,d=a\033\\\0338')


# ── Caca ───────────────────────────────────────────────────────────────────


class ImagePopup:
    """Curses chrome (border/spinner) + pixel image layer for image preview.
    Supports sixel and kitty protocols transparently — protocol is chosen
    once at startup via _IMG_PROTO and stored on the instance.

    Lifecycle:
        popup = ImagePopup(stdscr, url)
        asyncio.ensure_future(popup.load())
        # every frame: popup.draw()
        # popup.handle_key(k) → True means dismiss
        # popup.close() to clean up
    """

    def __init__(self, stdscr, url: str):
        self.stdscr         = stdscr
        self.url            = url
        self._proto         = _IMG_PROTO          # 'sixel' | 'kitty'
        self._state         = 'loading'
        self._error         = ''
        self._placed        = False
        self._pending_place = False
        self._dirty         = True
        self._img_data: Optional[bytes] = None    # sixel bytes | raw RGB | caca cells (list)
        self._px_w = self._px_h = 0
        self._spinner       = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
        self._frame         = 0
        self._last_spin     = 0.0
        self._win           = None
        self._py = self._px = self._ph = self._pw = 0
        self._img_cy = self._img_cx = self._img_cw = self._img_ch = 0
        self._layout()

    def _layout(self) -> None:
        H, W = self.stdscr.getmaxyx()
        ph   = max(10, min(H - 2, int(H * 0.85)))
        pw   = max(24, min(W - 2, int(W * 0.90)))
        py   = (H - ph) // 2
        px   = (W - pw) // 2
        self._py, self._px, self._ph, self._pw = py, px, ph, pw
        self._img_cy = py + 1
        self._img_cx = px + 1
        self._img_cw = pw - 2
        self._img_ch = ph - 2
        if self._win is not None:
            del self._win
        self._win = curses.newwin(ph, pw, py, px)
        self._win.bkgd(' ', curses.color_pair(C_BORDER))
        self._dirty = True

    def resize(self) -> None:
        self._placed = False
        self._layout()
        if self._state == 'ready' and self._img_data:
            self._pending_place = True

    async def load(self) -> None:
        _dbg.debug('load() proto=%s url=%s', self._proto, self.url)
        loop = asyncio.get_event_loop()
        cw, ch = self._img_cw, self._img_ch
        try:
            data, w, h = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_scale_encode, cw, ch),
                timeout=20)
            size_desc = f'{len(data)} lines' if isinstance(data, list) else f'{len(data)} bytes'
            _dbg.debug('load() done proto=%s w=%d h=%d %s', self._proto, w, h, size_desc)
            self._img_data      = data
            self._px_w, self._px_h = w, h
            self._state         = 'ready'
            self._dirty         = True
            self._pending_place = True
        except asyncio.TimeoutError:
            self._state = 'error'
            self._error = '✗  Timed out (20 s)'
            self._dirty = True
        except Exception as exc:
            _dbg.debug('load() exception: %s', exc, exc_info=True)
            self._state = 'error'
            self._error = f'✗  {str(exc)[:60]}'
            self._dirty = True

    def _fetch_scale_encode(self, cw: int, ch: int):
        import urllib.request, urllib.error, io as _io, tempfile, os as _os
        req = urllib.request.Request(
            self.url, headers={'User-Agent': 'skychat-tui/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read(12 * 1024 * 1024)
        except urllib.error.URLError as e:
            raise RuntimeError(f'Download failed: {e.reason}')

        # ── caca: render to ASCII art lines, no pixel protocol needed ──
        if self._proto == 'caca':
            inner_cw = max(1, cw - 2)
            inner_ch = max(1, ch - 2)
            # Write raw bytes to a temp file so img2txt / caca can read it
            suffix = '.png'
            try:
                from urllib.parse import urlparse as _up
                suffix = _os.path.splitext(_up(self.url).path)[1] or '.png'
            except Exception:
                pass
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(raw)
                tmp_path = tf.name
            try:
                lines = _render_caca(tmp_path, inner_cw, inner_ch)
            finally:
                try:
                    _os.unlink(tmp_path)
                except Exception:
                    pass
            _dbg.debug('caca rendered %d lines x %d cols', len(lines), inner_cw)
            # Return lines as the data; w/h expressed in cells not pixels
            return lines, inner_cw, inner_ch

        # ── pixel protocols: decode + scale with Pillow ─────────────
        try:
            img = _PILImage.open(_io.BytesIO(raw))
            img.load()
        except Exception as e:
            raise RuntimeError(f'Cannot decode: {e}')
        if hasattr(img, 'n_frames') and img.n_frames > 1:
            img.seek(0)
        img = img.convert('RGB')
        cell_w_px, cell_h_px = (_CELL_PX if _CELL_PX else (10, 20))
        inner_cw = max(1, cw - 2)
        inner_ch = max(1, ch - 2)
        tw = min(inner_cw * cell_w_px, 800)
        th = min(inner_ch * cell_h_px, 600)
        iw, ih = img.size
        scale = min(tw / max(iw, 1), th / max(ih, 1), 1.0)
        nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
        img = img.resize((nw, nh), _PILImage.LANCZOS)
        _dbg.debug('scaled %dx%d -> %dx%d proto=%s', iw, ih, nw, nh, self._proto)
        if self._proto == 'kitty':
            return img.tobytes(), nw, nh
        else:
            return _encode_sixel(img.tobytes(), nw, nh), nw, nh

    def _cell_pos(self) -> Tuple[int, int]:
        """Centred cell position for the image inside the popup interior."""
        cell_w_px, cell_h_px = (_CELL_PX if _CELL_PX else (10, 20))
        img_cols = max(1, round(self._px_w / cell_w_px))
        img_rows = max(1, round(self._px_h / cell_h_px))
        cx = self._img_cx + max(0, (self._img_cw - img_cols) // 2)
        cy = self._img_cy + max(0, (self._img_ch - img_rows) // 2)
        return cx, cy

    def _place(self) -> None:
        """Render the image into the popup interior."""
        _dbg.debug('_place() proto=%s has_data=%s', self._proto, bool(self._img_data))
        if not self._img_data:
            return

        if self._proto == 'caca':
            # ASCII art — draw each (text, attr) span into the curses popup window
            w = self._win
            lines: 'List[List[Tuple[str,int]]]' = self._img_data
            for row, spans in enumerate(lines):
                if row >= self._img_ch:
                    break
                col = 1
                for text, attr in spans:
                    avail = (1 + self._img_cw) - col
                    if avail <= 0:
                        break
                    chunk = text[:avail]
                    if chunk:
                        try:
                            w.addstr(row + 1, col, chunk, attr)
                        except curses.error:
                            pass
                        col += len(chunk)
            w.noutrefresh()
            self._placed = True
            _dbg.debug('_place() caca done, %d lines', len(lines))
            return

        # Pixel protocols (sixel / kitty)
        cx, cy = self._cell_pos()
        # Blank interior first to prevent character bleed-through
        _sixel_clear(self._img_cx, self._img_cy, self._img_cw, self._img_ch)
        if self._proto == 'kitty':
            _kitty_place(self._img_data, self._px_w, self._px_h, cx, cy)
        else:
            _sixel_place(self._img_data, cx, cy)
        self._placed = True
        _dbg.debug('_place() pixel done at cell=(%d,%d)', cx, cy)

    def erase_image(self) -> None:
        """Remove the image from screen."""
        if not self._placed:
            return
        if self._proto == 'caca':
            # ASCII art lives in curses cells — just erase the window
            # and let force_full_redraw handle the rest
            pass
        else:
            if self._proto == 'kitty':
                _kitty_clear(0)
            _sixel_clear(self._img_cx, self._img_cy, self._img_cw, self._img_ch)
        _dbg.debug('erase_image done proto=%s', self._proto)

    def draw(self) -> None:
        w  = self._win
        ph, pw = self._ph, self._pw
        bp = curses.color_pair(C_BORDER)
        ab = bp | curses.A_BOLD

        if self._dirty:
            w.erase()
            try:
                for r in range(ph):
                    w.addstr(r, 0, ' ' * (pw - 1), bp)
                for r in range(1, ph - 1):
                    w.addch(r, 0,      '│', ab)
                    w.addch(r, pw - 1, '│', ab)
                for c in range(1, pw - 1):
                    w.addch(0,      c, '─', ab)
                    w.addch(ph - 1, c, '─', ab)
                w.addch(0,      0,      '╭', ab)
                w.addch(0,      pw - 1, '╮', ab)
                w.addch(ph - 1, 0,      '╰', ab)
                try:
                    w.addch(ph - 1, pw - 1, '╯', ab)
                except curses.error:
                    pass
            except curses.error:
                pass
            hint = ' Scroll/H = close · O = open in browser · <>  = cycle '
            try:
                w.addstr(0, max(2, (pw - len(hint)) // 2), hint, ab)
            except curses.error:
                pass
            if self._state == 'error':
                for i, line in enumerate([self._error, '', 'scroll away to close']):
                    attr = curses.color_pair(C_ERROR) | curses.A_BOLD if i == 0 else bp
                    try:
                        w.addstr(ph // 2 - 1 + i,
                                 max(0, (pw - len(line)) // 2),
                                 line[:pw - 2], attr)
                    except curses.error:
                        pass
            elif self._state == 'ready':
                try:
                    w.addstr(ph - 1, 2, self.url[:pw - 4], bp)
                except curses.error:
                    pass
            self._dirty = False

        if self._state == 'loading':
            now = time.monotonic()
            if now - self._last_spin > 0.12:
                self._frame     = (self._frame + 1) % len(self._spinner)
                self._last_spin = now
            msg = f' {self._spinner[self._frame]}  Loading… '
            try:
                w.addstr(ph // 2, max(0, (pw - len(msg)) // 2), msg, bp)
            except curses.error:
                pass

        w.touchwin()
        w.noutrefresh()

    def handle_key(self, key) -> bool:
        return key in ('\x1b', 'q', 'Q', 'h', 'H', curses.KEY_BACKSPACE, 127)

    def close(self) -> None:
        self.erase_image()          # overwrite pixels with spaces first
        self._placed = False
        if self._win is not None:
            self._win.erase()
            self._win.noutrefresh()
            del self._win
            self._win = None


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
            self._current_room_id    = rid
            self._scroll_ack_last_sent = 0
            self._scroll_ack_candidate = 0
            self._scroll_ack_since     = 0.0
        self.on("join-room", _on_join_room)
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
    _init_caca_pairs()


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
        r"  ___________           _________ .__            __      ____/\__ __   ",
        r" /   _____/  | _____.__.\\_   ___ \|  |__ _____ _/  |_   /   / /_/ \ \  ",
        r" \_____  \|  |/ <   |  |/    \  \/|  |  \\__  \\   __\  \__/ / \   \ \ ",
        r" /        \    < \___  |\     \___|   Y  \/ __ \|  |    / / /   \  / / ",
        r"/_______  /__|_ \/ ____| \______  /___|  (____  /__|   /_/ /__  / /_/  ",
        r"        \/     \/\/             \/     \/     \/         \/   \/       ",
    ]

    # Field navigation order depends on whether we have a token
    def _field_order():
        base = [LoginField.USERNAME, LoginField.PASSWORD,
                LoginField.BTN_LOGIN, LoginField.BTN_GUEST]
        if has_token:
            base.append(LoginField.BTN_RESUME)
        return base

    field_order = _field_order()  # static — has_token never changes in the loop

    while True:
        stdscr.erase()
        H, W = stdscr.getmaxyx()

        logo_top = max(0, H // 2 - 12)
        for i, line in enumerate(LOGO):
            row = logo_top + i
            if row >= H:
                break
            x = max(0, (W - len(line)) // 2)
            try:
                stdscr.addstr(row, x, line[:W],
                              curses.color_pair(C_USERNAME) | curses.A_BOLD)
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

        _draw_box(stdscr, box_y, box_x, box_h, box_w,
                  curses.color_pair(C_BORDER))

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

        elif ch := _printable_char(key):
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
INPUT_H   = 3   # minimum input box height (1 border top + 1 text + 1 border bottom)
INPUT_H_MAX = 7  # maximum input box height (caps at 5 text lines)


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
        self._last_lines_out:     list           = []
        self._last_msg_range:     tuple          = (0, 0)
        self._last_skip_top:      int            = 0   # lines clipped from topmost msg

        # History lazy-loading
        self.history_exhausted:  bool             = False
        self.history_fetching:   bool             = False
        self._history_fetch_cb:  Optional[Callable] = None

        # Escape menu
        self.menu_open:             bool       = False
        self.menu_cursor:           int        = 0
        self.notifications_enabled: bool       = _load_config().get('notifications', True)
        self.image_preview_enabled: bool       = _load_config().get('image_preview', True)
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
        lines = self.input_buf.split('\n')
        visual_rows = sum(max(1, (len(ln) + vis_w - 1) // vis_w) for ln in lines)
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

    def add_message(self, msg: Dict) -> None:
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
            self.typing_list = typing_list
        try:
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
        own_username: str, lines_out: list,
        skip_lines: int = 0,
    ) -> int:
        """Render one message starting at *row*. Returns the next free row.
        skip_lines: skip this many lines from the top of the message (for partial
        display when the message is clipped at the top of the viewport)."""
        ts, user, msg_content = msg["ts"], msg["user"], msg["content"]
        is_sel = (self.scroll_cursor == mi)
        sel_a  = curses.color_pair(C_MSG_SELECT)
        qt_a   = curses.color_pair(C_TIMESTAMP)
        prefix = len(ts) + 1 + len(user) + 2
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
            lines_out.append((ts, user, q_line, False))
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

        # ── Wrapped content lines ─────────────────────────────────────
        _msg_interactables = _get_interactables(msg_content)
        wrapped = []
        for _ln in msg_content.split("\n"):
            wrapped.extend(ChatUI._wrap_with_spans(_ln, max(8, usable_w - prefix)))

        for wi, (chunk, chunk_urls, chunk_btns) in enumerate(wrapped):
            if row >= H - 1:
                break
            is_first = (wi == 0)
            lines_out.append((ts, user, chunk, is_first))
            if _skip > 0:
                _skip -= 1
                continue
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
                    _draw_segment(remaining, sel_a if is_sel else 0)
            except curses.error:
                pass
            row += 1

        # ── Reaction bar ──────────────────────────────────────────────
        reactions = msg.get("reactions", {})
        if reactions and row < H - 1:
            rbar = "".join(f" {e}×{c} " for e, c in list(reactions.items())[:8])
            lines_out.append((ts, user, rbar, False))
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
            self._last_lines_out = []
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
        self._last_skip_top      = skip_top

        if self.scroll_cursor >= 0:
            self.scroll_cursor = max(oldest_idx, min(newest_idx, self.scroll_cursor))

        # ── Render pass ───────────────────────────────────────────────
        row          = max(0, H - 1 - rows_used)
        lines_out: List[tuple] = []
        for mi in render_msgs:
            skip = skip_top if mi == render_msgs[0] else 0
            row = self._draw_message(
                w, self.messages[mi], mi, row,
                H, W, margin, usable_w, own_username, lines_out,
                skip_lines=skip,
            )
        self._last_lines_out = lines_out

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
        self._ordered_users = ordered  # kept in sync for key handler

        title = (" ▶ USERS" if focused else "   USERS") + f" — {len(connected_list)}"
        try:
            w.addstr(0, 0, title[:W - 1].ljust(W - 1),
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
        # Rebuild windows if input height changed due to text content
        if self._update_input_height():
            self._build_windows()

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
            # Split logical line into vis_w-wide chunks
            chunks = [line[i:i+vis_w] for i in range(0, max(1, len(line)), vis_w)] if line else ['']
            for ci, chunk in enumerate(chunks):
                vrow = len(visual_lines)
                # Is cursor on this chunk?
                chunk_start = char_idx + ci * vis_w
                chunk_end   = chunk_start + len(chunk)
                if chunk_start <= self.cursor_pos <= chunk_end:
                    # cursor pos within this visual row
                    local = self.cursor_pos - chunk_start
                    if local <= len(chunk):
                        cur_vrow = vrow
                        cur_vcol = local
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
        self.input_h        = INPUT_H
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
        self.btn_cursor    = 0

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

    # Enable bracketed paste mode: terminal wraps pastes in \x1b[200~ ... \x1b[201~
    # so we receive the whole paste at once instead of char-by-char.
    sys.stdout.write('\x1b[?2004h')
    sys.stdout.flush()

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
            entry = ChatUI._msg_to_entry(m, storage=m.get("storage"))
            if entry:
                prepend.append(entry)
        # Release fetching lock
        ui.history_fetching = False
        if prepend:
            n = len(prepend)
            was_empty = len(ui.messages) == 0

            # Save the ID of the message currently at the bottom of the viewport
            # so we can reanchor after prepend without any index arithmetic.
            anchor_id = None
            if not was_empty and ui.scroll_offset > 0:
                newest_idx = max(0, len(ui.messages) - 1 - ui.scroll_offset)
                anchor_msg = ui.messages[newest_idx] if 0 <= newest_idx < len(ui.messages) else None
                anchor_id = anchor_msg.get('id') if anchor_msg else None

            ui.messages = prepend + ui.messages

            if anchor_id:
                # Find the anchor message in the new list and recompute scroll_offset
                # so newest_idx points at the same message as before.
                for new_idx, m in enumerate(ui.messages):
                    if m.get('id') == anchor_id:
                        ui.scroll_offset = max(0, len(ui.messages) - 1 - new_idx)
                        break
                # Recompute _last_msg_range from the new scroll_offset so KEY_UP
                # doesn't use stale indices. newest is the anchor, oldest unknown
                # until next draw — set to 0 as a safe lower bound.
                new_newest = max(0, len(ui.messages) - 1 - ui.scroll_offset)
                ui._last_msg_range = (0, new_newest)
                ui._last_skip_top = 0

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

    # ── Key handlers ─────────────────────────────────────────────────

    async def _handle_menu_key(key) -> bool:
        """Handle a keypress while the menu is open. Returns True to exit the loop."""
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
                try:
                    plugins = client._user.setdefault('data', {}).setdefault('plugins', {})
                    plugins.setdefault('custom', {})['color'] = hex_val
                except Exception:
                    pass
                own    = client.current_user.get('username', '')
                own_id = client.current_user.get('id')
                old_hex = client._user.get('data', {}).get('plugins', {}).get('custom', {}).get('color', '')
                if old_hex:
                    ui._colour_pair_cache.pop(old_hex.lower().lstrip('#'), None)
                client._user.setdefault('data', {}).setdefault('plugins', {}).setdefault('custom', {})['color'] = hex_val
                for m in ui.messages:
                    if m.get('user') == own:
                        m['col'] = hex_val
                for session in client._connected_list:
                    u = session.get('user', {})
                    if isinstance(u, dict) and u.get('id') == own_id:
                        u.setdefault('data', {}).setdefault('plugins', {}).setdefault('custom', {})['color'] = hex_val
                        break
                ui.colour_pick_open = False
                ui.menu_open = False
                ui.set_status(f'Colour set to {entry.get("name", cid)}', ttl=3.0)
            elif key in ('', curses.KEY_BACKSPACE, 127, '', 8):
                ui.colour_pick_open = False
            return False

        # Main menu
        menu_items_count = 6
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
                ui.set_status(f'Notifications {"ON" if ui.notifications_enabled else "OFF"}', ttl=2.0)
            elif ui.menu_cursor == 2:  # Toggle image preview
                ui.image_preview_enabled = not ui.image_preview_enabled
                _save_config({'image_preview': ui.image_preview_enabled})
                if ui.image_preview_enabled:
                    # Detect protocol now if it wasn't done at startup
                    global _IMG_PROTO, _CELL_PX
                    if _IMG_PROTO is None:
                        _IMG_PROTO = _detect_protocol()
                        _dbg.debug('image protocol (on enable): %s', _IMG_PROTO)
                    if _IMG_PROTO and _CELL_PX is None:
                        _CELL_PX = _query_cell_pixels()
                        _dbg.debug('cell pixels (on enable): %s', _CELL_PX)
                ui.set_status(f'Image Preview {"ON" if ui.image_preview_enabled else "OFF"}', ttl=2.0)
            elif ui.menu_cursor == 3:  # Pick colour
                if ui.colour_list:
                    ui.colour_pick_open   = True
                    ui.colour_pick_cursor = 0
                else:
                    ui.set_status('✗  Colour list not yet received', ttl=3.0)
            elif ui.menu_cursor == 4:  # Logout
                ui.menu_open = False
                _save_config({'token': None, 'username': ''})
                client._running = False
                conn_task.cancel()
                return True
            elif ui.menu_cursor == 5:  # Quit
                ui.menu_open = False
                client._running = False
                conn_task.cancel()
                return True
        return False

    async def _handle_rooms_key(key) -> None:
        """Handle a keypress while the ROOMS sidebar is focused."""
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
        elif key in (curses.KEY_BACKSPACE, 127, '', 8):
            if rooms_list and 0 <= ui.room_cursor < len(rooms_list):
                room = rooms_list[ui.room_cursor]
                if room.get("isPrivate", False):
                    await client.send_message(f"/join {room['id']}")
                    await asyncio.sleep(0.3)
                    await client.send_message(f"/pmleave {room['id']}")
                    ui.set_status(f"Left {room.get('name', room['id'])}", ttl=3.0)
                else:
                    ui.set_status("✗  Can't leave public rooms", ttl=2.0)

    async def _handle_users_key(key) -> None:
        """Handle a keypress while the USERS sidebar is focused."""
        ordered = ui._ordered_users
        if key == curses.KEY_UP:
            ui.sidebar_move(-1, len(ordered))
        elif key == curses.KEY_DOWN:
            ui.sidebar_move(+1, len(ordered))
        elif key in (curses.KEY_ENTER, '\n', '\r', 10):
            if ordered and 0 <= ui.user_cursor < len(ordered):
                target = ordered[ui.user_cursor].get("user", {}).get("username", "")
                if target and target != client.current_user.get("username"):
                    ui.messages.clear()
                    ui.scroll_offset = 0
                    ui.scroll_cursor = -1
                    ui._last_msg_range = (0, 0)
                    ui.reset_history_state()
                    ui.set_status(f"Opening DM with {target}…", ttl=5.0)
                    asyncio.ensure_future(client.open_dm(target))
                ui.focus = Focus.INPUT

    async def _handle_input_key(key) -> bool:
        """Handle a keypress while the INPUT box is focused. Returns True to exit the loop."""
        _enter = (curses.KEY_ENTER, '\n', '\r', 10)
        _bksp  = (curses.KEY_BACKSPACE, 127, '', 8)

        def _selected_msg():
            if 0 <= ui.scroll_cursor < len(ui.messages):
                return ui.messages[ui.scroll_cursor]
            return None

        if key in (' ', 32) and (ui.scroll_offset > 0 or ui.scroll_cursor >= 0):
            msg = _selected_msg()
            if msg and msg.get("id"):
                ref = f"@{msg['id']} "
                ui.input_buf  = ref + ui.input_buf
                ui.cursor_pos = len(ref)
            ui.scroll_cursor_clear()
            ui.scroll_bottom()

        elif key in _enter:
            text = ui.consume_input().strip()
            if text == "/quit":
                return True
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
                    s.get("user", {}).get("username", "?") for s in client._connected_list
                ))
            elif text in ("/history", "/messagehistory"):
                await client.send_message("/messagehistory")
            elif text:
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
                    ui.scroll_down(1) if ui.scroll_offset > 0 else ui.scroll_cursor_clear()
                else:
                    ui.scroll_cursor += 1
                    ui.btn_cursor = 0
            else:
                ui.scroll_down(1)

        elif key in ('e', 'E') and ui.scroll_cursor >= 0:
            msg = _selected_msg()
            if msg and msg.get("id") and msg.get("user") == client.current_user.get("username"):
                ui.input_buf  = f"/edit {msg['id']} {msg['content']}"
                ui.cursor_pos = len(ui.input_buf)
                ui.scroll_cursor_clear()
                ui.scroll_bottom()
            elif msg:
                ui.set_status("✗  Can only edit your own messages", ttl=2.0)

        elif key in ('o', 'O') and ui.scroll_cursor >= 0:
            msg = _selected_msg()
            if msg:
                _ias = _get_interactables(msg.get('content', ''))
                if _ias:
                    _val, _kind = _ias[ui.btn_cursor % len(_ias)]
                    if _kind == 'url' or _val.startswith('http'):
                        webbrowser.open(_val)
                        ui.set_status(f'↗  Opened {_val[:50]}', ttl=3.0)
                    else:
                        asyncio.ensure_future(client.send_message(_val))
                        ui.set_status(f'▶  Sent: {_val[:50]}', ttl=2.0)
                else:
                    ui.set_status('No links or buttons in this message', ttl=2.0)

        elif key == curses.KEY_SF:
            ui.scroll_bottom(); ui.scroll_cursor_clear()
        elif key == curses.KEY_SR:
            ui.scroll_up(5)

        elif key in _bksp:
            msg = _selected_msg()
            if ui.scroll_cursor >= 0 and msg:
                if msg.get('id') and msg.get('user') == client.current_user.get('username'):
                    await client.send_message(f"/delete {msg['id']}")
                    ui.set_status('✓  Message deleted', ttl=2.0)
                    ui.scroll_cursor_clear()
                    ui.scroll_bottom()
                else:
                    ui.set_status('✗  Can only delete your own messages', ttl=2.0)
            else:
                ui.backspace()

        elif key == curses.KEY_DC:
            if ui.scroll_cursor < 0:
                ui.delete_char()

        elif key == curses.KEY_LEFT:
            if ui.scroll_cursor >= 0:
                msg = _selected_msg()
                if msg:
                    _ias = _get_interactables(msg.get("content", ""))
                    if _ias:
                        ui.btn_cursor = (ui.btn_cursor - 1) % len(_ias)
            else:
                ui.move_cursor(-1)

        elif key == curses.KEY_RIGHT:
            if ui.scroll_cursor >= 0:
                msg = _selected_msg()
                if msg:
                    _ias = _get_interactables(msg.get("content", ""))
                    if _ias:
                        ui.btn_cursor = (ui.btn_cursor + 1) % len(_ias)
            else:
                ui.move_cursor(+1)

        elif key == curses.KEY_HOME:
            if ui.scroll_cursor < 0:
                ui.home()
        elif key == curses.KEY_END:
            if ui.scroll_cursor < 0:
                ui.end()

        elif ch := _printable_char(key):
            if ui.scroll_cursor < 0:
                ui.insert_char(ch)
                asyncio.ensure_future(client.notify_typing())

        return False

    # ── Paste handler ────────────────────────────────────────────────
    _upload_base_url = _wss_to_http(DEFAULT_WSS_URL)

    async def _handle_paste(text: str) -> None:
        """Process pasted text: upload if it looks like a file path or image,
        otherwise bulk-insert into the input buffer (fast, no per-char redraw)."""
        stripped = text.strip()

        # ── Try clipboard image upload first (only when text is empty/whitespace) ──
        if not stripped and _get_upload_method() not in (None, 'stdlib'):
            img_bytes = await _grab_clipboard_image()
            if img_bytes:
                ui.set_status('⬆ Uploading image…', ttl=30)
                try:
                    url = await _upload_file_bytes(
                        img_bytes, 'paste.png', _upload_base_url,
                        client.token)
                    if ui.scroll_cursor < 0:
                        if ui.input_buf and not ui.input_buf.endswith(' '):
                            ui.input_buf += ' '
                        ui.input_buf += url
                        ui.cursor_pos = len(ui.input_buf)
                    ui.set_status('✓ Image uploaded', ttl=3)
                except Exception as e:
                    ui.set_status(f'✗ Upload failed: {e}', ttl=5)
                return

        # ── Detect file:// URI (drag-drop from file manager) ──────────
        if stripped.startswith('file://'):
            local_path = stripped[7:]   # strip file://
            # Strip hostname if present (file:///home/... → /home/...)
            if local_path.startswith('/') and not local_path.startswith('//'):
                pass  # already a bare path
            elif local_path.startswith('//'):
                # file:///path or file://host/path
                local_path = '/' + local_path.lstrip('/')
            import urllib.parse as _up
            local_path = _up.unquote(local_path)
            ui.set_status(f'⬆ Uploading {os.path.basename(local_path)}…', ttl=30)
            try:
                url = await _upload_local_file(local_path, _upload_base_url, client.token)
                if ui.scroll_cursor < 0:
                    if ui.input_buf and not ui.input_buf.endswith(' '):
                        ui.input_buf += ' '
                    ui.input_buf += url
                    ui.cursor_pos = len(ui.input_buf)
                ui.set_status('✓ Uploaded', ttl=3)
            except Exception as e:
                ui.set_status(f'✗ Upload failed: {e}', ttl=5)
            return

        # ── Detect bare local file path ────────────────────────────────
        import os.path as _osp
        if stripped and _osp.isabs(stripped) and _osp.isfile(stripped):
            ui.set_status(f'⬆ Uploading {os.path.basename(stripped)}…', ttl=30)
            try:
                url = await _upload_local_file(stripped, _upload_base_url, client.token)
                if ui.scroll_cursor < 0:
                    if ui.input_buf and not ui.input_buf.endswith(' '):
                        ui.input_buf += ' '
                    ui.input_buf += url
                    ui.cursor_pos = len(ui.input_buf)
                ui.set_status('✓ Uploaded', ttl=3)
            except Exception as e:
                ui.set_status(f'✗ Upload failed: {e}', ttl=5)
            return

        # ── Plain text paste — bulk insert (fast, single redraw) ──────
        if ui.scroll_cursor < 0 and ui.focus == Focus.INPUT:
            # Normalise \r\n and \r to \n
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            before = ui.input_buf[:ui.cursor_pos]
            after  = ui.input_buf[ui.cursor_pos:]
            ui.input_buf  = before + text + after
            ui.cursor_pos = len(before) + len(text)

    # ── Main loop ─────────────────────────────────────────────────────

    global _IMG_PROTO, _CELL_PX
    if ui.image_preview_enabled:
        if _IMG_PROTO is None:
            _IMG_PROTO = _detect_protocol()   # 'sixel', 'kitty', 'caca', or None
            _dbg.debug('image protocol: %s', _IMG_PROTO)
        if _IMG_PROTO and _CELL_PX is None:
            _CELL_PX = _query_cell_pixels()
            _dbg.debug('cell pixels: %s', _CELL_PX)

    image_popup:      Optional[ImagePopup] = None
    _hover_url:       str                  = ""   # currently open popup URL
    _hover_cand:      str                       = ""   # current candidate (may differ from open)
    _hover_since:     float                     = 0.0  # when candidate last changed
    _hover_suppressed: str                      = ""   # URL hidden with H — don't re-open until focus moves
    _HOVER_OPEN_MS  = 0.25   # seconds stable before opening
    _HOVER_CLOSE_MS = 0.40   # seconds empty before closing (tolerates transient resets)

    while True:
        rooms_list = client.rooms
        users_list = client._connected_list
        # Clear colour pair cache on each connected-list refresh so updated colours render
        if client._connected_list_dirty:
            client._connected_list_dirty = False
            ui._colour_pair_cache.clear()
            ui._next_pair = C_DYN_BASE

        # While a sixel image is displayed, skip draw_all entirely to prevent
        # any curses repaint from wiping the sixel pixels and causing a flash.
        # Kitty stores images in terminal memory so doesn't need this guard.
        _sixel_open = (ui._overlay is not None
                       and ui._overlay._state == 'ready'
                       and ui._overlay._proto == 'sixel'
                       and ui._overlay._placed
                       and not ui._overlay._pending_place)
        if not _sixel_open:
            ui.draw_all(
                rooms           = rooms_list,
                current_room_id = client.current_room_id,
                connected_list  = users_list,
                own_username    = client.current_user.get("username", ""),
                typing_list     = client.typing_list,
                own_user        = client.current_user,
                unread_checker  = client.has_unread_messages,
            )

        # ── Hover image preview ───────────────────────────────────────
        if _IMG_PROTO and ui.image_preview_enabled:
            _new_hover = ""
            if ui.scroll_cursor >= 0 and 0 <= ui.scroll_cursor < len(ui.messages):
                _ias = _get_interactables(ui.messages[ui.scroll_cursor].get("content", ""))
                if _ias:
                    _focused_val, _focused_kind = _ias[ui.btn_cursor % len(_ias)]
                    if _focused_kind == "url" and _is_image_url(_focused_val):
                        _new_hover = _focused_val

            # Clear suppression once focus moves to a different URL (or no URL)
            if _hover_suppressed and _new_hover != _hover_suppressed:
                _hover_suppressed = ""

            _now = time.monotonic()
            if _new_hover != _hover_cand:
                _hover_cand  = _new_hover
                _hover_since = _now

            _elapsed = _now - _hover_since

            if image_popup is None:
                # Not open — open once candidate has been stable long enough,
                # but not if the user explicitly hid this URL with H
                if _hover_cand and _hover_cand != _hover_suppressed and _elapsed >= _HOVER_OPEN_MS:
                    _hover_url = _hover_cand
                    image_popup = ImagePopup(stdscr, _hover_url)
                    ui._overlay = image_popup
                    asyncio.ensure_future(image_popup.load())
                    _dbg.debug('popup opened: %r', _hover_url)
            else:
                # Open — only close if candidate has been empty long enough,
                # OR if candidate is a *different* URL that has stabilised
                if not _hover_cand and _elapsed >= _HOVER_CLOSE_MS:
                    _dbg.debug('popup closed (timeout empty)')
                    image_popup.close()
                    image_popup = None
                    ui._overlay = None
                    _hover_url  = ""
                    ui.force_full_redraw()  # erase sixel artifacts
                elif _hover_cand and _hover_cand != _hover_url and _elapsed >= _HOVER_OPEN_MS:
                    _dbg.debug('popup switched: %r -> %r', _hover_url, _hover_cand)
                    image_popup.close()
                    image_popup = None
                    ui._overlay = None
                    ui.force_full_redraw()  # erase previous sixel before new one loads
                    _hover_url  = _hover_cand
                    image_popup = ImagePopup(stdscr, _hover_url)
                    ui._overlay = image_popup
                    asyncio.ensure_future(image_popup.load())

        # Scroll-read ack: find the highest message-id currently on screen
        # and notify the server once the viewport has been stable for 1s.
        if ui.messages and client.current_user.get("id", 0) != 0:
            oldest_idx, newest_idx = ui._last_msg_range
            _visible_mid = 0
            for _vi in range(newest_idx, oldest_idx - 1, -1):
                if 0 <= _vi < len(ui.messages):
                    _mid = ui.messages[_vi].get("id", 0)
                    if _mid:
                        _visible_mid = _mid
                        break
            if _visible_mid:
                client.scroll_ack_tick(_visible_mid)

        try:
            key = stdscr.get_wch()
        except curses.error:
            await asyncio.sleep(0.03)
            continue

        if key == curses.KEY_RESIZE:
            ui.resize()
            if image_popup is not None:
                image_popup.resize()
            # Drain any further resize events that queued up during the resize
            # so we don't spin through them all without ever yielding.
            while True:
                try:
                    k2 = stdscr.get_wch()
                    if k2 != curses.KEY_RESIZE:
                        # Non-resize key came in — push it back by breaking and
                        # handling it on the next iteration via a small state flag.
                        # Simplest safe option: just discard it (resize mid-keystroke
                        # is inherently lossy) and fall through to the sleep.
                        break
                except curses.error:
                    break
            await asyncio.sleep(0.02)
            continue

        # Detect Shift+Enter and bracketed paste sequences starting with \x1b.
        # We peek at the next character(s) non-blockingly to classify the sequence.
        if key == '\x1b':
            try:
                k2 = stdscr.get_wch()
                if k2 in ('\r', '\n', 10):
                    # Alt/Shift+Enter — insert newline in input
                    if ui.focus == Focus.INPUT and ui.scroll_cursor < 0:
                        ui.insert_char('\n')
                    continue
                elif k2 == '[':
                    # CSI sequence — read until alpha terminator
                    try:
                        rest = ''
                        while True:
                            k3 = stdscr.get_wch()
                            rest += (k3 if isinstance(k3, str) else chr(k3))
                            if rest[-1].isalpha() or rest.endswith('~'):
                                break
                            if len(rest) > 20:
                                break
                        if rest == '13;2u':
                            # Kitty Shift+Enter
                            if ui.focus == Focus.INPUT and ui.scroll_cursor < 0:
                                ui.insert_char('\n')
                            continue
                        elif rest == '200~':
                            # ── Bracketed paste start ─────────────────────────
                            # Drain everything until \x1b[201~ into paste_buf
                            paste_buf = ''
                            _ESC_seen = False
                            while True:
                                try:
                                    pc = stdscr.get_wch()
                                except curses.error:
                                    await asyncio.sleep(0.005)
                                    continue
                                pc = pc if isinstance(pc, str) else chr(pc)
                                if _ESC_seen:
                                    if pc == '[':
                                        # Read rest of potential 201~ terminator
                                        term = ''
                                        while True:
                                            try:
                                                tc = stdscr.get_wch()
                                            except curses.error:
                                                await asyncio.sleep(0.002)
                                                continue
                                            term += tc if isinstance(tc, str) else chr(tc)
                                            if term.endswith('~') or (term and term[-1].isalpha()):
                                                break
                                            if len(term) > 10:
                                                break
                                        if term == '201~':
                                            break  # end of paste
                                        else:
                                            paste_buf += '\x1b[' + term
                                    else:
                                        paste_buf += '\x1b' + pc
                                    _ESC_seen = False
                                elif pc == '\x1b':
                                    _ESC_seen = True
                                else:
                                    paste_buf += pc

                            # ── Process the pasted content ────────────────────
                            await _handle_paste(paste_buf)
                            continue
                        # Unknown CSI — fall through to Esc handling, discard rest
                    except curses.error:
                        pass
                else:
                    # Some other Alt+key — discard k2, handle as plain Esc
                    pass
            except curses.error:
                pass  # No next char — plain Esc

        # Esc: close popup if open (suppressed like H), otherwise toggle menu
        if key == '\x1b':
            if image_popup is not None:
                _hover_suppressed = _hover_url or _hover_cand
                image_popup.close()
                image_popup = None
                ui._overlay = None
                _hover_url = _hover_cand = ""
                ui.force_full_redraw()
            elif ui.menu_open and ui.colour_pick_open:
                ui.colour_pick_open = False  # back to main menu
            else:
                ui.menu_open = not ui.menu_open
                ui.menu_cursor = 0
                ui.colour_pick_open = False
            continue

        # Route keys to menu when open
        if ui.menu_open:
            if await _handle_menu_key(key):
                break
            # If image preview was just disabled, close any open popup immediately
            if not ui.image_preview_enabled and image_popup is not None:
                image_popup.close()
                image_popup = None
                ui._overlay = None
                _hover_url = _hover_cand = ""
                ui.force_full_redraw()
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

        # H = hide image popup (stays hidden until focus moves to a different URL)
        if key in ('h', 'H') and image_popup is not None:
            _hover_suppressed = _hover_url or _hover_cand
            image_popup.close()
            image_popup = None
            ui._overlay = None
            _hover_url  = ""
            _hover_cand = ""
            ui.force_full_redraw()
            continue

        if ui.focus == Focus.ROOMS:
            await _handle_rooms_key(key)
        elif ui.focus == Focus.USERS:
            await _handle_users_key(key)
        elif await _handle_input_key(key):
            break

        await asyncio.sleep(0.02)

    # Cleanup — restore terminal to normal paste mode
    sys.stdout.write('\x1b[?2004l')
    sys.stdout.flush()
    client._running = False
    await client.disconnect()
    conn_task.cancel()
    try:
        await conn_task
    except (asyncio.CancelledError, Exception):
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