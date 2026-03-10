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
| `Tab` / `Shift+Tab` | Cycle focus: Input → Rooms → Users → Input |
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
| `Backspace` (while scrolled) | Delete selected message (own messages only) |
| `<` / `>` (while scrolled) | Cycle between URLs/buttons in selected message |
| `o` (while scrolled) | Open focused URL in browser / activate focused button |
| `Esc` | Close image preview (if open), otherwise open/close menu |

### Input editing

| Key | Action |
|-----|--------|
| `←` / `→` | Move cursor |
| `Home` / `End` | Jump to start/end of input |
| `Backspace` / `Del` | Delete character |
| `Enter` | Send message |

### Image preview

When a message is selected and the focused link is an image URL, a preview popup appears automatically after a short hover delay.

| Key | Action |
|-----|--------|
| `H` | Hide popup (stays hidden until focus moves to a different URL) |
| `Esc` | Hide popup (same suppression behaviour as `H`) |
| `O` | Open image URL in browser |
| `<` / `>` | Cycle to previous/next link in the message |

The popup closes automatically when focus moves away from the image URL.

## Slash commands

| Command | Action |
|---------|--------|
| `/quit` | Exit |
| `/join <id>` | Join room by ID |
| `/rooms` | List available rooms in the status bar |
| `/who` | List online users in the status bar |
| `/history` | Fetch older message history |

## Menu

Press `Esc` to open the menu (when no image preview is open). Use `↑`/`↓` to navigate, `Enter` to select.

| Item | Action |
|------|--------|
| Cycle theme | Step through available themes: Dracula, Nord, Gruvbox, Solarized Dark, Tokyo Night, Monokai |
| Toggle notifications | Enable/disable desktop notifications and terminal bell |
| Image Preview | Enable/disable inline image preview (persisted to config) |
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
| `image_preview` | Whether inline image preview is enabled (default: `true`) |

## Requirements

- Python 3.10+
- `websockets` (installed automatically)
- A terminal with 256-colour support

### Optional — image preview

Image preview is auto-detected at startup and disabled gracefully if none of the following are available:

| Method | What's needed | Quality |
|--------|---------------|---------|
| Sixel | `Pillow` + a sixel terminal (foot, iTerm2, mlterm) | Full colour pixel image |
| Kitty | `Pillow` + kitty terminal | Full colour pixel image |
| libcaca | `img2txt` (libcaca-utils) or `python-caca` bindings | Coloured ASCII art fallback |

Install Pillow for sixel/kitty support:

```bash
pip install Pillow
```

Install libcaca for the ASCII art fallback (works in any 256-colour terminal):

```bash
# Debian/Ubuntu
sudo apt install libcaca-utils

# Arch
sudo pacman -S libcaca

# macOS
brew install libcaca
```