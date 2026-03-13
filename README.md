# skychat-tui

```
  ___________           _________ .__            __      ____/\__ __   
 /   _____/  | _____.__.\\_   ___ \|  |__ _____ _/  |_   /   / /_/ \ \ 
 \_____  \|  |/ <   |  |/    \  \/|  |  \__  \\   __\  \__/ / \   \ \
 /        \    < \___  |\     \___|   Y  \/ __ \|  |    / / /   \  / /
/_______  /__|_ \/ ____| \______  /___|  (____  /__|   /_/ /__  / /_/ 
        \/     \/\/             \/     \/     \/         \/   \/      
```

> a terminal chat client for [skych.at](https://skych.at). no electron. no browser. just vibes and escape codes.

[![WTFPL](http://www.wtfpl.net/wp-content/uploads/2012/12/wtfpl-badge-4.png)](http://www.wtfpl.net/)

---

## install

```bash
pip install .
```

or dev mode (edits take effect immediately):

```bash
pip install -e .
```

## usage

```bash
skychat                      # login screen
skychat username password    # skip the formalities
skychat --debug              # write crashes to /tmp/skychat_crash.log
```

Override the server URL:

```bash
SKYCHAT_URL=wss://localhost:8080/api/ws skychat
```

## layout

```
┌──────────┬─────────────────────────┬──────────┐
│ Channels │      Chat messages      │  Users   │
│          ├─────────────────────────┤          │
│          │    Input box            │          │
└──────────┴─────────────────────────┴──────────┘
```

`Tab` cycles focus between panels. The active panel gets a `▶` in its title.

## keys

### navigation

| Key | Action |
|-----|--------|
| `Tab` / `Shift+Tab` | Cycle focus: Input → Rooms → Users → Input |
| `↑` / `↓` (Rooms) | Move room cursor |
| `Enter` (Rooms) | Join selected room |
| `Backspace` (Rooms) | Leave selected DM / private room |
| `↑` / `↓` (Users) | Move user cursor |
| `Enter` (Users) | Open DM with selected user |

### messages (Input focus)

| Key | Action |
|-----|--------|
| `↑` / `↓` | Enter scroll mode / move message selection |
| `Shift+↑` | Scroll up 5 messages |
| `Shift+↓` | Snap back to bottom |
| `Space` (scrolled) | Quote selected message — prefills `@<id>` in input |
| `e` (scrolled) | Edit selected message (yours only) |
| `Backspace` (scrolled) | Delete selected message (yours only) |
| `<` / `>` (scrolled) | Cycle between URLs/buttons in selected message |
| `o` (scrolled) | Open focused URL / activate button |
| `Esc` | Close image preview (if open), otherwise open/close menu |

### input editing

| Key | Action |
|-----|--------|
| `←` / `→` | Move cursor |
| `Home` / `End` | Jump to start/end |
| `Backspace` / `Del` | Delete character |
| `Enter` | Send message |
| `Alt+Enter` / `Shift+Enter` | New line (multi-line message) |

### image preview

Hover over an image URL in scroll mode and a preview popup appears automatically. Kitty and sixel render full colour pixels; libcaca falls back to coloured ASCII art.

| Key | Action |
|-----|--------|
| `H` / `Esc` | Hide popup (suppressed until focus moves to a different URL) |
| `O` | Open image URL with configured URL opener |
| `<` / `>` | Cycle to prev/next link in the message |

## slash commands

| Command | Action |
|---------|--------|
| `/quit` | get out |
| `/join <id>` | join room by ID |
| `/rooms` | list rooms in the status bar |
| `/who` | list online users in the status bar |
| `/history` | fetch older message history |

## menu

`Esc` opens the menu (when no image preview is open). `↑`/`↓` to navigate, `Enter` to select.

| Item | Action |
|------|--------|
| Cycle theme | Dracula → Nord → Gruvbox → Solarized Dark → Tokyo Night → Monokai |
| Toggle notifications | desktop notifications + terminal bell |
| Image Preview | toggle inline image preview |
| Open URLs with | cycle between available URL openers (xdg-open, browsh, w3m) |
| Pick color | set your username color |
| Logout | nuke saved token and exit |
| Quit | exit without clearing token |

## config

lives at `~/.skychat_tui.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `username` | — | pre-fills the login screen |
| `token` | — | saved auth token |
| `theme` | `Dracula` | active color theme |
| `notifications` | `true` | desktop notifications |
| `image_preview` | `true` | inline image preview |
| `url_opener` | `xdg-open` | URL opener (`xdg-open`, `browsh`, or `w3m`) |

## requirements

- Python 3.10+
- `websockets`
- a terminal with 256-colour support

### optional — image preview

auto-detected at startup, fails gracefully if nothing's available:

| Method | Requirements | Notes |
|--------|--------------|-------|
| **Kitty** | `Pillow` + kitty terminal | best quality, no flicker |
| **Sixel** | `Pillow` + sixel terminal (foot, iTerm2, mlterm) | good quality, minor flicker |
| **libcaca** | `img2txt` or `python-caca` | coloured ASCII art, works anywhere |

```bash
pip install Pillow
```

```bash
# Debian/Ubuntu
sudo apt install libcaca-utils

# Arch
sudo pacman -S libcaca

# macOS
brew install libcaca
```

### optional — URL openers

auto-detected at startup, `xdg-open` always available as fallback:

| Opener | Notes |
|--------|-------|
| **xdg-open** | default — opens in your configured desktop browser |
| **browsh** | full graphical browser rendered in the terminal |
| **w3m** | lightweight terminal browser, good for text-heavy pages |

```bash
# browsh — see https://www.brow.sh/
# w3m
sudo apt install w3m   # Debian/Ubuntu
sudo pacman -S w3m     # Arch
brew install w3m       # macOS
```