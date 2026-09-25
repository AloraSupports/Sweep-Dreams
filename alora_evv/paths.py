"""Where the app keeps its private working files.

Everything with visit data lives in the per-user data folder, never inside the
code folder. That keeps it out of Git and out of anything an AI coding tool reads.
  Mac:     ~/Library/Application Support/AloraEVV
  Windows: %LOCALAPPDATA%\\AloraEVV
"""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "AloraEVV"
CODE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("ALORA_EVV_CONFIG") or CODE_ROOT / "config")


def data_dir() -> Path:
    p = Path(os.environ.get("ALORA_EVV_DATA") or user_data_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def sub(name: str) -> Path:
    p = data_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_private(path: Path) -> None:
    """Owner-only permissions on Mac/Linux. Windows user folders are already per-user."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
