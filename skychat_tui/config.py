"""
Config file load/save helpers.

Layout:
  ~/.config/skychat/config.json  — all settings except the auth token
  ~/.config/skychat/token.json   — auth token only, chmod 0o600

Both files are written atomically (tmp → rename) so a crash mid-write
never corrupts an existing file.  The config directory is created with
0o700 so other users on the same machine cannot list its contents.

If the legacy ~/.skychat_tui.json exists it is migrated automatically
on the first load and then deleted.
"""

import json
import os
from typing import Any, Optional

from .constants import CONFIG_DIR, CONFIG_FILE, TOKEN_FILE, _LEGACY_CONFIG


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    """Create the config directory with restricted permissions if needed."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)


def _secure_write(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* atomically with mode 0o600."""
    _ensure_dir()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)   # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _migrate() -> None:
    """
    One-time migration from ~/.skychat_tui.json.

    Splits the old monolithic file into config.json (settings) and
    token.json (auth token), writes both to the new XDG location,
    then removes the legacy file.  Silently skipped if the legacy
    file does not exist or cannot be parsed.
    """
    if not os.path.isfile(_LEGACY_CONFIG):
        return
    # Skip migration if new config already exists (migration already ran)
    if os.path.isfile(CONFIG_FILE):
        try:
            os.unlink(_LEGACY_CONFIG)
        except Exception:
            pass
        return
    try:
        with open(_LEGACY_CONFIG) as f:
            old: dict = json.load(f)
    except Exception:
        return

    token = old.pop("token", None)

    # Write settings (token already popped out)
    try:
        _secure_write(CONFIG_FILE, old)
    except Exception:
        return  # don't delete legacy file if we couldn't write the new one

    # Write token only if it was present
    if token is not None:
        try:
            _secure_write(TOKEN_FILE, {"token": token})
        except Exception:
            pass

    # Remove legacy file now that migration succeeded
    try:
        os.unlink(_LEGACY_CONFIG)
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Return the settings dict.  Never contains the auth token."""
    _migrate()
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        data.pop("token", None)   # defensive: never leak token via config
        return data
    except Exception:
        return {}


def save_config(data: dict) -> None:
    """
    Persist *data* into config.json, merging with any existing settings.
    The ``token`` key is silently stripped — use :func:`save_token` for that.
    """
    try:
        existing = load_config()
        clean = {k: v for k, v in data.items() if k != "token"}
        existing.update(clean)
        _secure_write(CONFIG_FILE, existing)
    except Exception:
        pass


def load_token() -> Optional[Any]:
    """Return the stored auth token, or ``None`` if absent or unreadable."""
    _migrate()
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get("token")
    except Exception:
        return None


def save_token(token: Optional[Any]) -> None:
    """
    Persist *token* to token.json (chmod 0o600).
    Pass ``None`` to delete the token file (i.e. logout).
    """
    try:
        if token is None:
            try:
                os.unlink(TOKEN_FILE)
            except FileNotFoundError:
                pass
        else:
            _secure_write(TOKEN_FILE, {"token": token})
    except Exception:
        pass