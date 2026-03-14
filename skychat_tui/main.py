"""
Entry point: tui_chat loop, event wiring, connect/auth, _run, main.
"""

import argparse
import asyncio
import curses
import os
import subprocess
import sys
import time
import urllib.parse
from typing import Optional

from .constants import DEFAULT_WSS_URL, DEFAULT_ROOM_ID, C_DYN_BASE
from .config import load_config, save_config, load_token, save_token
from .helpers import (
    _strip_tags, _parse_reactions, _get_interactables, _printable_char,
    _room_display_name, _cols_aware_wrap, _str_cols,
)
from .images import (
    _enable_debug_logging, _detect_protocol, _query_cell_pixels,
    _wss_to_http, _upload_file_bytes, _upload_local_file,
    _grab_clipboard_image, _is_image_url, _dbg, ImagePopup,
    _get_upload_method, _detect_url_openers, _open_url,
)
import skychat_tui.images as _img_mod
from .ui import (
    ChatUI, Focus, _setup_colors, _apply_theme, THEMES, THEME_NAMES,
)
from .client import SkyChatClient
from .login import ncurses_login, _RESUME_SESSION

def _wire_events(client: "SkyChatClient", ui: "ChatUI") -> None:
    """Register all client event handlers that bridge the WebSocket layer to the UI."""

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
        try:
            curses.beep()
        except Exception:
            pass
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
        own = client.current_user.get("username", "")
        sender_obj = msg.get("user", {})
        sender = sender_obj.get("username", "") if isinstance(sender_obj, dict) else str(sender_obj)
        if ui._is_hidden(sender):
            return
        content = _strip_tags(msg.get("content") or msg.get("formatted") or "")
        raw_rid = msg.get("room") if msg.get("room") is not None else msg.get("roomId")
        try:
            room_id = int(raw_rid) if raw_rid is not None else None
        except (TypeError, ValueError):
            room_id = None
        if sender == own:
            return
        room  = next((r for r in client.rooms if r.get("id") == room_id), None)
        is_dm = room.get("isPrivate", False) if room else False
        mentioned = bool(own) and f'@{own}' in content
        if mentioned:
            _notify(f'@{own} ← {sender}', content[:120])
        elif is_dm and room_id != client.current_room_id:
            _notify(f'DM from {sender}', content[:120])
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
            _s = m.get('user', {})
            _s = _s.get('username', '') if isinstance(_s, dict) else str(_s)
            if ui._is_hidden(_s):
                continue
            entry = ChatUI._msg_to_entry(m, storage=m.get("storage"))
            if entry:
                prepend.append(entry)
        ui.history_fetching = False
        if prepend:
            was_empty = len(ui.messages) == 0
            anchor_id = None
            if not was_empty and ui.scroll_offset > 0:
                newest_idx = max(0, len(ui.messages) - 1 - ui.scroll_offset)
                anchor_msg = ui.messages[newest_idx] if 0 <= newest_idx < len(ui.messages) else None
                anchor_id  = anchor_msg.get('id') if anchor_msg else None
            ui.messages = prepend + ui.messages
            if anchor_id:
                for new_idx, m in enumerate(ui.messages):
                    if m.get('id') == anchor_id:
                        ui.scroll_offset = max(0, len(ui.messages) - 1 - new_idx)
                        break
                new_newest = max(0, len(ui.messages) - 1 - ui.scroll_offset)
                ui._last_msg_range = (0, new_newest)
                ui._last_skip_top  = 0
            ui.set_status(f"✓ {len(ui.messages)} messages loaded", ttl=2.0)
        else:
            ui._history_empty_count += 1
            if ui._history_empty_count >= 2:
                ui.history_exhausted = True
                ui.set_status("✓ No more history", ttl=3.0)
            else:
                ui.set_status("✓ No new messages", ttl=2.0)

    @client.on("info")
    def _on_info(t):  ui.set_status(f"ℹ  {t}")

    @client.on("error")
    def _on_err(t):   ui.set_status(f"✗  {t}")

    @client.on("ws-open")
    def _on_open(_):  ui.set_status("Connected  ✓  skych.at", ttl=4.0)

    @client.on("set-user")
    def _on_set_user_save(user):
        uname = user.get("username", "")
        if uname and uname != "*Guest" and not uname.startswith("*"):
            save_config({"username": uname})

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
        msgs_with_id = [m for m in ui.messages if m.get("id")]
        if not msgs_with_id:
            await client.send_message("/messagehistory")
            return
        oldest    = min(msgs_with_id, key=lambda m: m["id"])
        oldest_id = oldest["id"]
        await client.send_message(f"/messagehistory {oldest_id}")

    ui._history_fetch_cb = _fetch_more_history

    @client.on("join-room")
    def _on_join(rid):
        room = next((r for r in client.rooms if r.get("id") == rid), None)
        name = room.get("name", rid) if room else rid
        if rid is not None and room:
            last_id = room.get("lastReceivedMessageId") or 0
            if last_id:
                client.update_lastseen(rid, last_id)
        ui.messages.clear()
        ui.scroll_offset   = 0
        ui.scroll_cursor   = -1
        ui._last_msg_range = (0, 0)
        ui.reset_history_state()
        ui.set_status(f"# {name}", ttl=3.0)
        asyncio.ensure_future(client.send_message("/messagehistory"))


async def _connect_and_auth(
    client: "SkyChatClient", ui: "ChatUI", stdscr,
    username: Optional[str], password: Optional[str],
    guest: bool, saved_token: Optional[dict],
) -> Optional[asyncio.Task]:
    """Connect to the server, authenticate, and join the default room.

    Returns the connection Task on success, or None if auth failed
    (in which case the client is already shut down and the UI has shown an error).
    """
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
        return None

    await asyncio.sleep(0.3)
    await client.join(DEFAULT_ROOM_ID)
    for i, r in enumerate(client.rooms):
        if r.get("id") == DEFAULT_ROOM_ID:
            ui.room_cursor = i
            break

    return conn_task


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

    # ── Event wiring ────────────────────────────────────────────────────
    _wire_events(client, ui)

    # ── Connect & auth ──────────────────────────────────────────────────
    conn_task = await _connect_and_auth(
        client, ui, stdscr, username, password, guest, saved_token)
    if conn_task is None:
        return  # auth failed, already cleaned up

    # ── Key handlers ────────────────────────────────────────────────────

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
            elif key in ('\x1b', curses.KEY_BACKSPACE, 127, '\x7f', 8):
                ui.colour_pick_open = False
            return False

        # Main menu
        menu_items_count = 8
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
                save_config({'notifications': ui.notifications_enabled})
                ui.set_status(f'Notifications {"ON" if ui.notifications_enabled else "OFF"}', ttl=2.0)
            elif ui.menu_cursor == 2:  # Toggle image preview
                ui.image_preview_enabled = not ui.image_preview_enabled
                save_config({'image_preview': ui.image_preview_enabled})
                if ui.image_preview_enabled:
                    # Detect protocol now if it wasn't done at startup
                    if _img_mod._IMG_PROTO is None:
                        _img_mod._IMG_PROTO = _detect_protocol()
                        _dbg.debug('image protocol (on enable): %s', _img_mod._IMG_PROTO)
                    if _img_mod._IMG_PROTO and _img_mod._CELL_PX is None:
                        _img_mod._CELL_PX = _query_cell_pixels()
                        _dbg.debug('cell pixels (on enable): %s', _img_mod._CELL_PX)
                ui.set_status(f'Image Preview {"ON" if ui.image_preview_enabled else "OFF"}', ttl=2.0)
            elif ui.menu_cursor == 3:  # Cycle URL opener
                available = _detect_url_openers()
                idx = available.index(ui.url_opener) if ui.url_opener in available else 0
                ui.url_opener = available[(idx + 1) % len(available)]
                save_config({'url_opener': ui.url_opener})
                ui.set_status(f'Open URLs with: {ui.url_opener}', ttl=2.0)
            elif ui.menu_cursor == 4:  # Hide guests
                ui.hide_guests = not ui.hide_guests
                save_config({'hide_guests': ui.hide_guests})
                ui.set_status(f'Hide guests {"ON" if ui.hide_guests else "OFF"}', ttl=2.0)
            elif ui.menu_cursor == 5:  # Pick colour
                if ui.colour_list:
                    ui.colour_pick_open   = True
                    ui.colour_pick_cursor = 0
                else:
                    ui.set_status('✗  Colour list not yet received', ttl=3.0)
            elif ui.menu_cursor == 6:  # Logout
                ui.menu_open = False
                save_token(None)
                save_config({'username': ''})
                client._running = False
                conn_task.cancel()
                return True
            elif ui.menu_cursor == 7:  # Quit
                ui.menu_open = False
                client._running = False
                conn_task.cancel()
                return True
        return False

    async def _handle_rooms_key(key) -> bool:
        """Handle a keypress while the ROOMS sidebar is focused. Always returns False."""
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
            if rooms_list and 0 <= ui.room_cursor < len(rooms_list):
                room = rooms_list[ui.room_cursor]
                if room.get("isPrivate", False):
                    await client.send_message(f"/join {room['id']}")
                    await asyncio.sleep(0.3)
                    await client.send_message(f"/pmleave {room['id']}")
                    # Optimistically remove room from own session in connected list
                    _leave_rid = room['id']
                    _own_id = client.current_user.get("id")
                    if _own_id is not None:
                        for _sess in client._connected_list:
                            if _sess.get("user", {}).get("id") == _own_id:
                                _sess["rooms"] = [r for r in (_sess.get("rooms") or []) if r != _leave_rid]
                                client._connected_list_dirty = True
                                break
                    ui.set_status(f"Left {room.get('name', room['id'])}", ttl=3.0)
                else:
                    ui.set_status("✗  Can't leave public rooms", ttl=2.0)
        return False

    async def _handle_users_key(key) -> bool:
        """Handle a keypress while the USERS sidebar is focused. Always returns False."""
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
        return False

    async def _handle_input_key(key) -> bool:
        """Handle a keypress while the INPUT box is focused. Returns True to exit the loop."""
        _enter = (curses.KEY_ENTER, '\n', '\r', 10)
        _bksp  = (curses.KEY_BACKSPACE, 127, '\x7f', 8)

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
                        _open_url(_val, ui.url_opener, stdscr)
                        ui.set_status(f'✓  Opened {_val[:50]}', ttl=3.0)
                    else:
                        asyncio.ensure_future(client.send_message(_val))
                        ui.set_status(f'✓  Sent: {_val[:50]}', ttl=2.0)
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

    # ── Paste handler ────────────────────────────────────────────────────
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

        # ── Detect file:// URI (drag-drop from file manager) ────────────
        if stripped.startswith('file://'):
            local_path = stripped[7:]   # strip file://
            # Strip hostname if present (file:///home/... → /home/...)
            if local_path.startswith('/') and not local_path.startswith('//'):
                pass  # already a bare path
            elif local_path.startswith('//'):
                # file:///path or file://host/path
                local_path = '/' + local_path.lstrip('/')
            local_path = urllib.parse.unquote(local_path)
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

        # ── Detect bare local file path ─────────────────────────────────
        if stripped and os.path.isabs(stripped) and os.path.isfile(stripped):
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

        # ── Plain text paste — bulk insert (fast, single redraw) ────────
        if ui.scroll_cursor < 0 and ui.focus == Focus.INPUT:
            # Normalise \r\n and \r to \n
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            before = ui.input_buf[:ui.cursor_pos]
            after  = ui.input_buf[ui.cursor_pos:]
            ui.input_buf  = before + text + after
            ui.cursor_pos = len(before) + len(text)

    # ── Main loop ────────────────────────────────────────────────────────

    if ui.image_preview_enabled:
        if _img_mod._IMG_PROTO is None:
            _img_mod._IMG_PROTO = _detect_protocol()   # 'sixel', 'kitty', 'caca', or None
            _dbg.debug('image protocol: %s', _img_mod._IMG_PROTO)
        if _img_mod._IMG_PROTO and _img_mod._CELL_PX is None:
            _img_mod._CELL_PX = _query_cell_pixels()
            _dbg.debug('cell pixels: %s', _img_mod._CELL_PX)

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

        # ── Hover image preview ─────────────────────────────────────────
        if _img_mod._IMG_PROTO and ui.image_preview_enabled:
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
                            # ── Bracketed paste start ──────────────────────────
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

                            # ── Process the pasted content ───────────────────
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
                ui.menu_open = True
                ui.menu_cursor = 0
                ui.colour_pick_open = False
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

        # O = open popup URL with configured opener
        if key in ('o', 'O') and image_popup is not None and _hover_url:
            _open_url(_hover_url, ui.url_opener, stdscr)
            ui.force_full_redraw()
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
            if await _handle_rooms_key(key):
                break
        elif ui.focus == Focus.USERS:
            if await _handle_users_key(key):
                break
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
    cfg = load_config()

    if cli_username and cli_password:
        username, password, guest = cli_username, cli_password, False
        saved_token = None
        resume      = False
    else:
        saved_username = cfg.get("username", "")
        saved_token    = load_token()
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
    parser = argparse.ArgumentParser(
        description=f"SkyChat TUI  [{DEFAULT_WSS_URL}]",
        epilog="Tab=focus  ↑↓=navigate  Enter=join/DM  /quit=exit",
    )
    parser.add_argument("username", nargs="?", default=None)
    parser.add_argument("password", nargs="?", default=None)
    parser.add_argument("--debug", action="store_true",
                        help="Write crash tracebacks to /tmp/skychat_crash.log")
    args = parser.parse_args()
    if args.debug:
        _enable_debug_logging()
    # Reduce ncurses ESC disambiguation delay from the default ~1 s to 25 ms.
    # Must be set before curses.wrapper() initialises the terminal.
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(_run, args.username, args.password)
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        with open("/tmp/skychat_crash.log", "w") as _f:
            traceback.print_exc(file=_f)
        if args.debug:
            raise
    finally:
        try:
            curses.endwin()
        except Exception:
            pass


if __name__ == "__main__":
    main()