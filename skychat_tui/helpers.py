"""
Pure utility functions: text processing, message parsing, data helpers.
No network, no curses rendering — safe to import anywhere.
"""

import re
import time
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .constants import (
    AFK_SECONDS, C_USER_ONLINE, C_USER_AFK, C_USER_RECENT,
    URL_RE, BUTTON_RE, TAG_RE, STICKER_RE,
)


# ── Text / column utilities ───────────────────────────────────────────────────

def _printable_char(key) -> str:
    """Return the printable character for *key*, or empty string if not printable."""
    if isinstance(key, str) and key.isprintable():
        return key
    if isinstance(key, int) and 32 <= key < 127:
        return chr(key)
    return ""


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


def _cols_aware_wrap(text: str, width: int, keep_trailing: bool = False) -> List[str]:
    """Word-aware, column-aware line wrap.  Keeps whole words together and
    only breaks mid-word when a single word exceeds the available width.
    Also splits after ']' so button runs don't overflow.

    *keep_trailing* — when True the final chunk is not rstripped.  Pass
    True when the result is used for cursor-position tracking (i.e. in the
    input box renderer) so that a trailing space does not cause the cursor
    to jump to the beginning of the line.
    """
    if not text:
        return [""]

    tokens = re.split(r'(\s+)', text)
    lines: List[str] = []
    current      = ""
    current_cols = 0

    for token in tokens:
        if not token:
            continue
        token_cols = _str_cols(token)

        if current_cols + token_cols <= width:
            current      += token
            current_cols += token_cols
        elif token_cols > width:
            # Token wider than line — break character by character
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
            if current.strip():
                lines.append(current.rstrip())
            stripped     = token.lstrip()
            current      = stripped
            current_cols = _str_cols(stripped)

    if current.strip():
        lines.append(current if keep_trailing else current.rstrip())

    return lines or [""]



def _cols_aware_wrap_offsets(text: str, width: int) -> List[Tuple[str, int]]:
    """Like :func:`_cols_aware_wrap` but returns ``(chunk, start_in_text)`` pairs.

    The start offset is the index of the chunk's first character in *text*,
    derived directly from the tokeniser position rather than re-computed by
    the caller.  This is the correct form to use for cursor-position tracking
    because it stays accurate across both space-split wraps (where one
    whitespace character is consumed between chunks) and word-break wraps
    (where no separator is consumed and a naive ``+1`` offset would be wrong).
    """
    if not text:
        return [("", 0)]

    tokens       = re.split(r'(\s+)', text)
    result: List[Tuple[str, int]] = []
    current      = ""
    current_cols = 0
    current_start = 0   # start of *current* accumulation buffer in *text*
    pos          = 0    # running position in *text*

    for token in tokens:
        if not token:
            continue
        token_cols = _str_cols(token)

        if current_cols + token_cols <= width:
            if not current:
                current_start = pos
            current      += token
            current_cols += token_cols
            pos          += len(token)

        elif token_cols > width:
            # Token wider than one line — flush current, then break char-by-char
            if current.strip():
                result.append((current.rstrip(), current_start))
            current, current_cols = "", 0
            tok_start = pos
            char_buf  = ""
            char_buf_start = pos
            for ch in token:
                ch_w = _char_width(ch)
                if current_cols + ch_w > width:
                    if char_buf:
                        result.append((char_buf, char_buf_start))
                    char_buf       = ch
                    char_buf_start = tok_start
                    current_cols   = ch_w
                else:
                    char_buf     += ch
                    current_cols += ch_w
                tok_start += 1
            current       = char_buf
            current_start = char_buf_start
            pos          += len(token)

        else:
            # Token doesn't fit on current line — flush and start fresh
            if current.strip():
                result.append((current.rstrip(), current_start))
            pos          += len(token)
            stripped      = token.lstrip()
            current_start = pos - len(stripped)
            current       = stripped
            current_cols  = _str_cols(stripped)

    if current.strip():
        # Keep trailing whitespace so cursor-position math works correctly
        # (a trailing space increments cursor_pos but rstrip would hide it).
        result.append((current, current_start))

    return result or [("", 0)]


# ── Content parsing ───────────────────────────────────────────────────────────

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


def _room_display_name(room: Dict, own_username: str = "") -> str:
    """Return a human-readable name for a room.

    For private/DM rooms with no server-provided name, falls back to the
    whitelist members excluding the current user, or the room id.
    """
    name = room.get("name", "") or ""
    if not name and room.get("isPrivate"):
        wl    = room.get("whitelist") or room.get("allowedUsers") or []
        own_l = own_username.lower()
        parts = [u.get("username", "") if isinstance(u, dict) else str(u) for u in wl]
        parts = [p for p in parts if p and p.lower() != own_l]
        name  = ", ".join(parts) or str(room.get("id", "?"))
    return name or str(room.get("id", "?"))


# ── WebSocket delta patching ──────────────────────────────────────────────────

def _apply_jsondiffpatch_array(lst: list, delta: dict) -> list:
    """Apply a jsondiffpatch array delta (with _t:'a') to a list.

    jsondiffpatch array delta keys:
      "_N" with [val, 0, 0]   -> delete item originally at index N
      "_N" with [old, new, 3] -> item moved from index N to new index
      "N"  with [val]         -> insert val at result index N
      "N"  with {...}         -> nested object delta for item at result index N

    Three-phase approach to ensure correct index semantics:
      1. Delete  — filter out items by their original indices.
      2. Insert  — splice new items in at their result indices (sorted so
                   each successive insert is unaffected by earlier ones).
      3. Modify  — apply nested field deltas using result indices against the
                   now-settled list (avoids the off-by-one that arises when
                   modifications are applied during the delete pass).
    """
    if not isinstance(delta, dict) or delta.get('_t') != 'a':
        return lst

    to_delete: set  = set()
    to_insert: dict = {}   # result_idx -> value
    to_modify: dict = {}   # result_idx -> sub_delta

    for key, val in delta.items():
        if key == '_t':
            continue
        if key.startswith('_'):
            orig_idx = int(key[1:])
            if isinstance(val, list) and len(val) == 3 and val[1] == 0 and val[2] == 0:
                # delete
                to_delete.add(orig_idx)
            elif isinstance(val, list) and len(val) == 3 and val[2] == 3:
                # move: treat as delete-from-original + insert-at-result
                to_delete.add(orig_idx)
                to_insert[val[1]] = lst[orig_idx] if orig_idx < len(lst) else val[0]
        else:
            result_idx = int(key)
            if isinstance(val, list) and len(val) == 1:
                to_insert[result_idx] = val[0]
            elif isinstance(val, dict):
                to_modify[result_idx] = val

    # Phase 1: delete
    new_lst = [item for i, item in enumerate(lst) if i not in to_delete]

    # Phase 2: insert — process in ascending index order so each insertion
    # lands at the correct position without needing an offset adjustment.
    for ins_idx in sorted(to_insert.keys()):
        new_lst.insert(ins_idx, to_insert[ins_idx])

    # Phase 3: modify — now that the list is in its final shape we can
    # address items by their result indices directly.
    for mod_idx, sub in to_modify.items():
        if not (0 <= mod_idx < len(new_lst)):
            continue
        item = new_lst[mod_idx]
        if not (isinstance(item, dict) and isinstance(sub, dict)):
            continue
        item = dict(item)
        for fk, fv in sub.items():
            if isinstance(fv, list) and len(fv) == 2:
                item[fk] = fv[1]
            elif isinstance(fv, list) and len(fv) == 1:
                item[fk] = fv[0]
            elif isinstance(fv, list) and len(fv) == 3 and fv[1] == 0 and fv[2] == 0:
                item.pop(fk, None)
        new_lst[mod_idx] = item

    return new_lst