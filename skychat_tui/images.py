"""
Image rendering and upload support.

Handles protocol detection (sixel / kitty / caca), encoding, terminal
placement, clipboard/file upload, and the ImagePopup overlay widget.
"""

import asyncio
import base64
import curses
import io
import json
import logging as _logging
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

from .constants import (
    C_BORDER, C_ERROR, C_DYN_BASE,
    CACA_PAIR_BASE, CACA_PAIR_END,
)

# ── Optional dependencies ─────────────────────────────────────────────────────

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

# ── Logging ───────────────────────────────────────────────────────────────────

_dbg = _logging.getLogger('skychat')
_dbg.addHandler(_logging.NullHandler())  # silent by default; --debug activates it


def _enable_debug_logging() -> None:
    """Activate file logging to /tmp/skychat_debug.log. Called when --debug is passed."""
    handler = _logging.FileHandler('/tmp/skychat_debug.log')
    handler.setFormatter(_logging.Formatter('%(asctime)s %(name)s %(message)s'))
    _dbg.addHandler(handler)
    _dbg.setLevel(_logging.DEBUG)


# ── Protocol detection ────────────────────────────────────────────────────────

_CELL_PX:   Optional[Tuple[int, int]] = None   # (w_px, h_px), queried once
_IMG_PROTO: Optional[str]             = None   # None = not yet probed

IMAGE_EXTS = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif', '.avif',
})


def _is_image_url(url: str) -> bool:
    return any(urllib.parse.urlparse(url).path.lower().endswith(e) for e in IMAGE_EXTS)


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
        m = re.search(rb'\033\[6;(\d+);(\d+)t', resp)
        if m:
            cw, ch = int(m.group(2)), int(m.group(1))
            _dbg.debug('_query_cell_pixels: raw=%r -> w=%d h=%d', resp, cw, ch)
            if 4 <= cw <= 64 and 4 <= ch <= 128:
                return cw, ch
    except Exception as e:
        _dbg.debug('_query_cell_pixels failed: %s', e)
    return 10, 20


def _probe_sixel() -> bool:
    """Ask the terminal if it supports sixel via DA1 (CSI c)."""
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
        m = re.search(rb'\033\[\?([0-9;]+)c', resp)
        if m:
            attrs = m.group(1).split(b';')
            return b'4' in attrs
    except Exception as e:
        _dbg.debug('_probe_sixel failed: %s', e)
    return False


def _probe_kitty() -> bool:
    """Send a 1×1 dummy Kitty graphics command and check for an OK response."""
    import select, termios, tty
    try:
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
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


def _detect_protocol() -> Optional[str]:
    """Detect the best available image protocol.
    Order of preference: sixel > kitty > caca.
    Returns 'sixel', 'kitty', 'caca', or None."""
    term         = os.environ.get('TERM', '')
    term_program = os.environ.get('TERM_PROGRAM', '')
    kitty_id     = os.environ.get('KITTY_WINDOW_ID', '')

    if _PILLOW_OK:
        if kitty_id or 'kitty' in term:
            _dbg.debug('_detect_protocol: kitty via env')
            return 'kitty'
        if term in ('foot', 'foot-extra'):
            _dbg.debug('_detect_protocol: sixel via env (foot)')
            return 'sixel'
        if 'mlterm' in term or 'yaft' in term:
            _dbg.debug('_detect_protocol: sixel via env (mlterm/yaft)')
            return 'sixel'
        if term_program == 'iTerm.app':
            _dbg.debug('_detect_protocol: sixel via env (iTerm2)')
            return 'sixel'
        _dbg.debug('_detect_protocol: probing terminal (TERM=%r)', term)
        if _probe_sixel():
            _dbg.debug('_detect_protocol: sixel confirmed by probe')
            return 'sixel'
        if _probe_kitty():
            _dbg.debug('_detect_protocol: kitty confirmed by probe')
            return 'kitty'

    _img2txt_available = bool(shutil.which('img2txt'))
    if _CACA_OK or _img2txt_available:
        _dbg.debug('_detect_protocol: caca fallback (bindings=%s img2txt=%s)',
                   _CACA_OK, _img2txt_available)
        return 'caca'

    _dbg.debug('_detect_protocol: no image protocol detected (Pillow=%s caca=%s)',
               _PILLOW_OK, _CACA_OK)
    return None


# ── URL opener detection ──────────────────────────────────────────────────────

_URL_OPENERS_AVAILABLE: Optional[List[str]] = None  # cached after first call


def _detect_url_openers() -> List[str]:
    """Return list of available URL openers in preference order.
    Always includes 'xdg-open' as the base fallback.
    Probes for 'browsh' and 'w3m' via shutil.which, same pattern as image protocol detection.
    """
    global _URL_OPENERS_AVAILABLE
    if _URL_OPENERS_AVAILABLE is not None:
        return _URL_OPENERS_AVAILABLE
    openers = ['xdg-open']
    if shutil.which('browsh'):
        openers.insert(0, 'browsh')
    if shutil.which('w3m'):
        # Insert after browsh (if present) but before xdg-open
        idx = 1 if 'browsh' in openers else 0
        openers.insert(idx, 'w3m')
    _dbg.debug('_detect_url_openers: %s', openers)
    _URL_OPENERS_AVAILABLE = openers
    return openers


def _open_url(url: str, opener: str, stdscr) -> None:
    """Open *url* with *opener*.

    For terminal browsers (browsh, w3m): suspends curses, runs the browser
    in the foreground, then reinitialises curses so the TUI can resume cleanly.
    For xdg-open: fires in the background, no curses disruption needed.
    """
    import curses as _curses
    terminal_browsers = ('browsh', 'w3m')
    if opener in terminal_browsers:
        # Hand the terminal over to the browser
        _curses.endwin()
        try:
            subprocess.run([opener, url])
        except FileNotFoundError:
            pass  # opener disappeared since detection — fall through silently
        finally:
            # Reinitialise curses fully
            stdscr.refresh()
            _curses.doupdate()
    else:
        # xdg-open / webbrowser — non-blocking, no terminal takeover
        try:
            subprocess.Popen(['xdg-open', url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            import webbrowser
            webbrowser.open(url)


# ── Upload ────────────────────────────────────────────────────────────────────

_UPLOAD_METHOD: Optional[str] = None


def _detect_upload() -> Optional[str]:
    """Detect which clipboard image tool is available.
    Returns 'aiohttp', 'xclip', 'wl-paste', 'pbpaste', or None."""
    try:
        import aiohttp as _aiohttp  # noqa: F401
        return 'aiohttp'
    except ImportError:
        pass
    if shutil.which('xclip'):
        return 'xclip'
    if shutil.which('wl-paste'):
        return 'wl-paste'
    if shutil.which('pbpaste'):
        return 'pbpaste'
    return None


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
    """Upload raw bytes as multipart/form-data. Returns the full URL of the uploaded file."""
    endpoint = base_url.rstrip('/') + '/api/upload'
    method   = _get_upload_method()

    browser_headers = {
        'Origin':     base_url,
        'Referer':    base_url + '/',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) skychat-tui/1.0',
    }

    if method == 'aiohttp':
        import aiohttp
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            form = aiohttp.FormData()
            mime = mimetypes.guess_type(filename)[0] or 'image/png'
            form.add_field('file', data, filename=filename, content_type=mime)
            async with session.post(endpoint, data=form, headers=browser_headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 403:
                    body = await resp.text()
                    raise RuntimeError(f'403 Forbidden: {body[:120]}')
                result = await resp.json(content_type=None)
    else:
        boundary = uuid.uuid4().hex
        parts = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {mimetypes.guess_type(filename)[0] or "image/png"}\r\n\r\n'
        ).encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        headers = {'Content-Type': f'multipart/form-data; boundary={boundary}', **browser_headers}
        req = urllib.request.Request(endpoint, data=parts, headers=headers, method='POST')
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
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
    path = path.strip()
    if not os.path.isfile(path):
        raise RuntimeError(f'File not found: {path}')
    filename = os.path.basename(path)
    loop     = asyncio.get_running_loop()
    data     = await loop.run_in_executor(None, lambda: open(path, 'rb').read())
    return await _upload_file_bytes(data, filename, base_url, token)


async def _grab_clipboard_image() -> Optional[bytes]:
    """Try to grab image bytes from the system clipboard. Returns None if unavailable."""
    method = _get_upload_method()
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
            script = 'set img to (the clipboard as «class PNGf»)\nreturn img'
            proc = await asyncio.create_subprocess_exec(
                'osascript', '-e', script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return out if proc.returncode == 0 and out else None
    except Exception:
        pass
    return None


# ── Sixel ─────────────────────────────────────────────────────────────────────

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


# ── Caca colour pairs ─────────────────────────────────────────────────────────
#
# Pairs 21–31 are reserved for caca ASCII-art rendering (never reassigned).
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

_CACA_PAIR_MAP: dict = {}   # (fg_curses, bg_curses) -> pair number


def _init_caca_pairs() -> None:
    """Register the 11 fixed caca pairs. Call once after curses.start_color()."""
    _pairs = [
        (curses.COLOR_WHITE,   curses.COLOR_BLACK),
        (curses.COLOR_RED,     curses.COLOR_BLACK),
        (curses.COLOR_GREEN,   curses.COLOR_BLACK),
        (curses.COLOR_YELLOW,  curses.COLOR_BLACK),
        (curses.COLOR_BLUE,    curses.COLOR_BLACK),
        (curses.COLOR_MAGENTA, curses.COLOR_BLACK),
        (curses.COLOR_CYAN,    curses.COLOR_BLACK),
        (curses.COLOR_BLACK,   curses.COLOR_BLACK),
        (curses.COLOR_WHITE,   curses.COLOR_RED),
        (curses.COLOR_BLACK,   curses.COLOR_WHITE),
        (curses.COLOR_WHITE,   curses.COLOR_BLUE),
    ]
    for i, (fg, bg) in enumerate(_pairs):
        pair_n = CACA_PAIR_BASE + i
        try:
            curses.init_pair(pair_n, fg, bg)
            _CACA_PAIR_MAP[(fg, bg)] = pair_n
        except Exception:
            pass


def _ansi256_to_curses(n: int) -> int:
    if n < 8:
        return [curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN,
                curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_MAGENTA,
                curses.COLOR_CYAN, curses.COLOR_WHITE][n]
    if n < 16:
        return _ansi256_to_curses(n - 8)
    if n >= 232:
        return curses.COLOR_WHITE if (n - 232) > 11 else curses.COLOR_BLACK
    n -= 16
    b, tmp = n % 6, n // 6
    g, r   = tmp % 6, tmp // 6
    mx = max(r, g, b)
    if mx == 0:                             return curses.COLOR_BLACK
    if r >= 4 and g < 3 and b < 3:         return curses.COLOR_RED
    if g >= 4 and r < 3 and b < 3:         return curses.COLOR_GREEN
    if b >= 4 and r < 3 and g < 3:         return curses.COLOR_BLUE
    if r >= 3 and g >= 3 and b < 2:        return curses.COLOR_YELLOW
    if r >= 3 and b >= 3 and g < 2:        return curses.COLOR_MAGENTA
    if g >= 3 and b >= 3 and r < 2:        return curses.COLOR_CYAN
    return curses.COLOR_WHITE


def _snap_caca_pair(fg: int, bg: int) -> int:
    """Return the curses color_pair int for the nearest pre-baked caca pair."""
    if (fg, bg) in _CACA_PAIR_MAP:
        return curses.color_pair(_CACA_PAIR_MAP[(fg, bg)])
    if bg != curses.COLOR_BLACK:
        for known_bg in (curses.COLOR_RED, curses.COLOR_WHITE, curses.COLOR_BLUE):
            if bg == known_bg and (fg, known_bg) in _CACA_PAIR_MAP:
                return curses.color_pair(_CACA_PAIR_MAP[(fg, known_bg)])
        bg = curses.COLOR_BLACK
    if (fg, curses.COLOR_BLACK) in _CACA_PAIR_MAP:
        return curses.color_pair(_CACA_PAIR_MAP[(fg, curses.COLOR_BLACK)])
    return curses.color_pair(CACA_PAIR_BASE)


def _parse_ansi_to_spans(raw: bytes, cols: int, rows: int
                         ) -> List[List[Tuple[str, int]]]:
    """Parse ANSI-coloured img2txt/caca output into curses-renderable spans."""
    tok_re  = re.compile(rb'\033\[([0-9;]*)m|([^\033\n]+)|\n')
    ansi8   = [curses.COLOR_BLACK, curses.COLOR_RED,     curses.COLOR_GREEN,
               curses.COLOR_YELLOW, curses.COLOR_BLUE,   curses.COLOR_MAGENTA,
               curses.COLOR_CYAN,   curses.COLOR_WHITE]

    result: List[List[Tuple[str, int]]] = []
    cur_line: List[Tuple[str, int]]     = []
    cur_col  = 0
    fg       = curses.COLOR_WHITE
    bg       = curses.COLOR_BLACK
    bold     = False

    def flush_line():
        nonlocal cur_line, cur_col
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

        if m.group(1) is not None:
            params_raw = m.group(1)
            params = [int(x) for x in params_raw.split(b';') if x] if params_raw else [0]
            i = 0
            while i < len(params):
                p = params[i]
                if p == 0:   fg, bg, bold = curses.COLOR_WHITE, curses.COLOR_BLACK, False
                elif p == 1: bold = True
                elif p == 22: bold = False
                elif 30 <= p <= 37: fg = ansi8[p - 30]
                elif p == 39: fg = curses.COLOR_WHITE
                elif 40 <= p <= 47: bg = ansi8[p - 40]
                elif p == 49: bg = curses.COLOR_BLACK
                elif 90 <= p <= 97:   fg = ansi8[p - 90]
                elif 100 <= p <= 107: bg = ansi8[p - 100]
                elif p == 38 and i + 2 < len(params) and params[i+1] == 5:
                    fg = _ansi256_to_curses(params[i+2]); i += 2
                elif p == 48 and i + 2 < len(params) and params[i+1] == 5:
                    bg = _ansi256_to_curses(params[i+2]); i += 2
                i += 1
        else:
            text  = m.group(2).decode('utf-8', errors='replace')
            avail = cols - cur_col
            if avail <= 0 or not text:
                continue
            text  = text[:avail]
            attr  = _snap_caca_pair(fg, bg)
            if bold:
                attr |= curses.A_BOLD
            cur_line.append((text, attr))
            cur_col += len(text)

    if cur_line or len(result) < rows:
        flush_line()

    blank_attr = _snap_caca_pair(curses.COLOR_WHITE, curses.COLOR_BLACK)
    while len(result) < rows:
        result.append([(' ' * cols, blank_attr)])

    return result[:rows]


def _render_caca(img_path: str, cols: int, rows: int) -> List[List[Tuple[str, int]]]:
    """Render image to coloured curses spans via img2txt or caca bindings."""
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


# ── Kitty ─────────────────────────────────────────────────────────────────────

def _kitty_place(img_rgb: bytes, px_w: int, px_h: int,
                 cell_x: int, cell_y: int) -> None:
    """Send raw RGB pixels as a Kitty graphics command (f=24, a=T, q=2)."""
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


# ── ImagePopup ────────────────────────────────────────────────────────────────

class ImagePopup:
    """Curses chrome (border/spinner) + pixel image layer for image preview.

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
        self._proto         = _IMG_PROTO
        self._state         = 'loading'
        self._error         = ''
        self._placed        = False
        self._pending_place = False
        self._dirty         = True
        self._img_data: Optional[bytes] = None
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
            self._img_data         = data
            self._px_w, self._px_h = w, h
            self._state            = 'ready'
            self._dirty            = True
            self._pending_place    = True
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
        req = urllib.request.Request(self.url, headers={'User-Agent': 'skychat-tui/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read(12 * 1024 * 1024)
        except urllib.error.URLError as e:
            raise RuntimeError(f'Download failed: {e.reason}')

        if self._proto == 'caca':
            inner_cw = max(1, cw - 2)
            inner_ch = max(1, ch - 2)
            suffix = '.png'
            try:
                suffix = os.path.splitext(urllib.parse.urlparse(self.url).path)[1] or '.png'
            except Exception:
                pass
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(raw)
                tmp_path = tf.name
            try:
                lines = _render_caca(tmp_path, inner_cw, inner_ch)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            _dbg.debug('caca rendered %d lines x %d cols', len(lines), inner_cw)
            return lines, inner_cw, inner_ch

        try:
            img = _PILImage.open(io.BytesIO(raw))
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
        scale  = min(tw / max(iw, 1), th / max(ih, 1), 1.0)
        nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
        img    = img.resize((nw, nh), _PILImage.LANCZOS)
        _dbg.debug('scaled %dx%d -> %dx%d proto=%s', iw, ih, nw, nh, self._proto)
        if self._proto == 'kitty':
            return img.tobytes(), nw, nh
        else:
            return _encode_sixel(img.tobytes(), nw, nh), nw, nh

    def _cell_pos(self) -> Tuple[int, int]:
        cell_w_px, cell_h_px = (_CELL_PX if _CELL_PX else (10, 20))
        img_cols = max(1, round(self._px_w / cell_w_px))
        img_rows = max(1, round(self._px_h / cell_h_px))
        cx = self._img_cx + max(0, (self._img_cw - img_cols) // 2)
        cy = self._img_cy + max(0, (self._img_ch - img_rows) // 2)
        return cx, cy

    def _place(self) -> None:
        _dbg.debug('_place() proto=%s has_data=%s', self._proto, bool(self._img_data))
        if not self._img_data:
            return

        if self._proto == 'caca':
            w     = self._win
            lines = self._img_data
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

        cx, cy = self._cell_pos()
        _sixel_clear(self._img_cx, self._img_cy, self._img_cw, self._img_ch)
        if self._proto == 'kitty':
            _kitty_place(self._img_data, self._px_w, self._px_h, cx, cy)
        else:
            _sixel_place(self._img_data, cx, cy)
        self._placed = True
        _dbg.debug('_place() pixel done at cell=(%d,%d)', cx, cy)

    def erase_image(self) -> None:
        if not self._placed:
            return
        if self._proto != 'caca':
            if self._proto == 'kitty':
                _kitty_clear(0)
            _sixel_clear(self._img_cx, self._img_cy, self._img_cw, self._img_ch)
        _dbg.debug('erase_image done proto=%s', self._proto)

    def draw(self) -> None:
        w      = self._win
        ph, pw = self._ph, self._pw
        bp     = curses.color_pair(C_BORDER)
        ab     = bp | curses.A_BOLD

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
        self.erase_image()
        self._placed = False
        if self._win is not None:
            self._win.erase()
            self._win.noutrefresh()
            del self._win
            self._win = None