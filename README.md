# skychat-tui

Vibe coded client for [skych.at](https://skych.at), built with Python and curses.

## Install

```bash
pip install .
```

Or for development (editable install — changes to the source take effect immediately):

```bash
pip install -e .
```

## Usage

```bash
skychat                      # opens login screen (username pre-filled from last session)
skychat username password    # skip login screen
```

## Keys

| Key | Action |
|-----|--------|
| `Esc` (Main view) | Open menu |
| `Tab` / `Shift+Tab` | Cycle focus: Rooms → Input → Users |
| `↑ / ↓` (Input focus) | Scroll messages / move selection cursor |
| `Shift+↑` | Jump 5 messages up |
| `Shift+↓` | Snap back to bottom |
| `Space` (while scrolled) | Quote selected message |
| `e` (while scrolled) | Edit selected message |
| `Enter` (Users focus) | Open DM with selected user |
| `Enter` (Rooms focus) | Join selected room |
| `Backspace` (Rooms focus) | Leave selected room |

## Slash commands

| Command | Action |
|---------|--------|
| `/quit` | Exit |
| `/join <id>` | Join room by ID |
| `/rooms` | List available rooms |
| `/who` | List online users |
| `/history` | Fetch message history |

## Config

Settings are saved to `~/.skychat_tui.json` (username and auth token).

## Requirements

- Python 3.10+
- `websockets` (installed automatically)
- A terminal with 256-colour support for the full Dracula theme