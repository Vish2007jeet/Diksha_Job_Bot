"""Persistent on/off toggle for each scraper source."""
from __future__ import annotations

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent / "data" / "scrapers_config.json"

ALL_SOURCES = [
    "linkedin", "stepstone", "xing", "arbeitsagentur",
    "workday", "personio", "company", "target_companies", "bmw",
]


def _load() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except Exception:
            pass
    return {s: True for s in ALL_SOURCES}


def _save(state: dict) -> None:
    _CONFIG_PATH.write_text(json.dumps(state, indent=2))


def get_all() -> dict[str, bool]:
    state = _load()
    for s in ALL_SOURCES:
        state.setdefault(s, True)
    return {s: state[s] for s in ALL_SOURCES}


def toggle(source: str) -> bool:
    """Toggle source on/off. Returns new state."""
    state = _load()
    for s in ALL_SOURCES:
        state.setdefault(s, True)
    state[source] = not state.get(source, True)
    _save(state)
    return state[source]


def get_enabled_sources() -> list[str]:
    state = get_all()
    return [s for s in ALL_SOURCES if state.get(s, True)]
