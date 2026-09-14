"""Config loading. Files hold behavior; environment holds secrets."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATE_PATH = ROOT / "state" / "seen.json"


def _load(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def load_settings() -> dict:
    return _load(CONFIG_DIR / "settings.yml")


def load_watchlist() -> dict:
    return _load(CONFIG_DIR / "watchlist.yml")
