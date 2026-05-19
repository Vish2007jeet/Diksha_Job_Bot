"""
Persistent runtime settings — stored in data/bot_settings.json.

These are user-controlled toggles that:
  - survive bot restarts
  - override config.py defaults
  - are changed via Telegram commands

Usage:
    from utils.bot_settings import bot_settings
    bot_settings.get("humanize_enabled")   # → True/False
    bot_settings.set("humanize_enabled", False)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SETTINGS_FILE = Path(__file__).parent.parent / "data" / "bot_settings.json"

_DEFAULTS: dict[str, Any] = {
    "humanize_enabled": True,
}


class _BotSettings:
    def _load(self) -> dict:
        if _SETTINGS_FILE.exists():
            try:
                return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save(self, data: dict) -> None:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Return the persisted value, or the hardcoded default, or `default`."""
        data = self._load()
        if key in data:
            return data[key]
        if key in _DEFAULTS:
            return _DEFAULTS[key]
        return default

    def set(self, key: str, value: Any) -> None:
        """Persist a setting and sync to config module if the attribute exists."""
        data = self._load()
        data[key] = value
        self._save(data)

        # Keep the live config module in sync so pipeline/handlers see the change
        import config as _cfg
        attr = key.upper()
        if hasattr(_cfg, attr):
            setattr(_cfg, attr, value)

    def all(self) -> dict:
        """Return all settings (persisted values merged over defaults)."""
        merged = dict(_DEFAULTS)
        merged.update(self._load())
        return merged


bot_settings = _BotSettings()
