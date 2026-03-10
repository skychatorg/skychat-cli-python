# skychat-tui

Terminal client for [skych.at](https://skych.at), built with Python and curses.

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

## Layout

```
┌──────────┬─────────────────────────┬──────────┐
│ Channels │      Chat messages      │  Users   │
│          ├─────────────────────────┤          │
│          │    Input box            │          │
└──────────┴─────────────────────────┴──────────┘
```

Tab cycles focus between the three panels. The active panel is indicated by a `▶` in its title.

## Keys

### Navigation

| Key | Action |
|-----|--------|
| `Tab` / `Shift+Tab` | Cycle focus: Rooms → Input → Users |
| `↑` / `↓` (Rooms focus) | Move room cursor |
| `Enter` (Rooms focus) | Join selected room |
| `Backspace` (Rooms focus) | Leave selected DM / private room |
| `↑` / `↓` (Users focus) | Move user cursor |
| `Enter` (Users focus) | Open DM with selected user |

### Messages (Input focus)

| Key | Action |
|-----|--------|
| `↑` / `↓` | Enter scroll mode / move message selection |
| `Shift+↑` | Scroll up 5 messages |
| `Shift+↓` | Snap back to bottom |
| `Space` (while scrolled) | Quote selected message — prefills `@<id>` in input |
| `e` (while scrolled) | Edit selected message (own messages only) |
| `◀▶` (while scrolled) | Cycle between URLs/buttons |
| `o` (while scrolled) | Open URLs in default browser/Activate buttons |
| `Esc` | Open/close menu |

### Input editing

| Key | Action |
|-----|--------|
| `←` / `→` | Move cursor |
| `Home` / `End` | Jump to start/end of input |
| `Backspace` / `Del` | Delete character |
| `Enter` | Send message |

## Slash commands

| Command | Action |
|---------|--------|
| `/quit` | Exit |
| `/join <id>` | Join room by ID |
| `/rooms` | List available rooms in the status bar |
| `/who` | List online users in the status bar |
| `/history` | Fetch older message history |
| `/edit <id> <text>` | Edit a previously sent message |

## Menu

Press `Esc` to open the menu. Use `↑`/`↓` to navigate, `Enter` to select.

| Item | Action |
|------|--------|
| Cycle theme | Step through available themes (Dracula, Nord, Gruvbox, Solarized Dark, Tokyo Night, Monokai) |
| Toggle notifications | Enable/disable desktop notifications and terminal bell |
| Pick colour | Set your username colour (loaded from server) |
| Logout | Clear saved token and exit |
| Quit | Exit without clearing token |

## Config

Settings are saved to `~/.skychat_tui.json`:

| Key | Description |
|-----|-------------|
| `username` | Pre-fills the login screen |
| `token` | Saved auth token for resuming sessions without re-entering credentials |
| `theme` | Active colour theme (default: `Dracula`) |
| `notifications` | Whether desktop notifications are enabled (default: `true`) |

## Requirements

- Python 3.10+
- `websockets` (installed automatically)
- A terminal with 256-colour support