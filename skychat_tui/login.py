"""
Login screen — ncurses_login() and supporting types.
"""

import curses
from enum import Enum, auto
from typing import Optional

from .constants import (
    C_USERNAME, C_TIMESTAMP, C_BORDER, C_LOGIN_FIELD, C_LOGIN_LABEL,
    C_LOGIN_BTN, C_LOGIN_BTN_S, C_ERROR, C_INPUT, DEFAULT_WSS_URL,
)
from .helpers import _printable_char
from .ui import _draw_box

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

