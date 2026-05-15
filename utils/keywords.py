"""
KeywordManager — persistent search keyword, location, and scoring tier store.

Saved to data/keywords.json; survives restarts and can be edited
live from Telegram without touching code or .env.

Six lists:
  broad     — semantic boards (LinkedIn, Indeed, Stepstone, Xing)
  exact     — exact-match boards (Arbeitsagentur, Workday, Personio)
  locations — all scrapers use this list
  tier1     — Claude scoring: +2 per match (max 4) + pre-filter gate
  tier2     — Claude scoring: +1 per match (max 3) + pre-filter gate
  tier3     — Claude scoring: moderate boost
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

import config

# ── Defaults (used when keywords.json doesn't exist yet) ──────
_DEFAULT_BROAD: List[str] = [
    # ── Vehicle Dynamics ──────────────────────────
    "Vehicle Dynamics",
    "Vehicle Dynamics Engineer",
    "Working Student Vehicle Dynamics",
    "Werkstudent Fahrzeugdynamik",
    "Working Student Chassis",
    "Chassis Development Engineer",
    "Suspension System",
    "Steering System",
    # ── Simulation ────────────────────────────────
    "Working Student Simulation",
    "Werkstudent ANSYS",
    "Werkstudent Adams",
    "Working Student MATLAB",
    # ── EV / Powertrain ───────────────────────────
    "EV Powertrain Engineer",
    "Working Student Battery",
    "Werkstudent Elektromobilität",
    # ── Brake / Steer by Wire ─────────────────────
    "Brake Systems",
    "Brake Systems Engineer",
    "Brake by Wire",
    "Steer by Wire",
    "Thermal Management Engineer",
    # ── Chassis / Suspension ─────────────────────
    "Werkstudent Fahrwerk",
    "CAE Engineer Automotive",
    # ── Thesis / Working Student ──────────────────
    "Masterarbeit Fahrzeugtechnik",
]

_DEFAULT_EXACT: List[str] = config.SEARCH_KEYWORDS

_DEFAULT_TIER1: List[str] = list(config.TIER1_KEYWORDS)
_DEFAULT_TIER2: List[str] = list(config.TIER2_KEYWORDS)
_DEFAULT_TIER3: List[str] = list(config.TIER3_KEYWORDS)

_DEFAULT_LOCATIONS: List[str] = [
    "Germany",
    "Remote",
    "Munich",
    "Stuttgart",
    "Karlsruhe",
    "Ulm",
    "Friedrichshafen",
    "Sindelfingen",
    "Ingolstadt",
    "Nuremberg",
    "Augsburg",
]


class KeywordManager:
    """
    Thread-safe keyword store backed by data/keywords.json.
    Reads on first use; writes immediately on any change.
    """

    def __init__(self, path: Path | None = None):
        self._path = path or (config.BASE_DIR / "data" / "keywords.json")
        self._data: dict | None = None

    # ── Internal ──────────────────────────────────────────────

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                # Backfill tier keys added after initial release
                dirty = False
                for key, default in (
                    ("tier1", _DEFAULT_TIER1),
                    ("tier2", _DEFAULT_TIER2),
                    ("tier3", _DEFAULT_TIER3),
                ):
                    if key not in self._data:
                        self._data[key] = list(default)
                        dirty = True
                if dirty:
                    self._save()
                return self._data
            except Exception:
                pass
        # First run — seed from defaults
        self._data = {
            "broad":     list(_DEFAULT_BROAD),
            "exact":     list(_DEFAULT_EXACT),
            "locations": list(_DEFAULT_LOCATIONS),
            "tier1":     list(_DEFAULT_TIER1),
            "tier2":     list(_DEFAULT_TIER2),
            "tier3":     list(_DEFAULT_TIER3),
        }
        self._save()
        return self._data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── Public API ─────────────────────────────────────────────

    def get_broad(self) -> List[str]:
        """Keywords for semantic boards (LinkedIn, Stepstone, Xing, Indeed)."""
        return list(self._load()["broad"])

    def get_exact(self) -> List[str]:
        """Keywords for exact-match boards (Arbeitsagentur, Workday, Personio)."""
        return list(self._load().get("exact", _DEFAULT_EXACT))

    def get_locations(self) -> List[str]:
        """Locations used by all scrapers."""
        return list(self._load().get("locations", _DEFAULT_LOCATIONS))

    def add(self, keyword: str, list_type: str = "broad") -> bool:
        """
        Add keyword to the specified list.
        When adding to 'broad', also adds to 'exact' so ALL scrapers
        (LinkedIn/Stepstone AND Workday/Personio/Arbeitsagentur) pick it up.
        Returns False if already present in the primary list.
        """
        data = self._load()
        kw   = keyword.strip()

        # Primary list check
        lst = data.setdefault(list_type, [])
        if any(k.lower() == kw.lower() for k in lst):
            return False
        lst.append(kw)

        # Mirror to 'exact' whenever a 'broad' keyword is added via Telegram,
        # so Arbeitsagentur / Workday / Personio / CompanyScraper also search it.
        if list_type == "broad":
            exact_lst = data.setdefault("exact", [])
            if not any(k.lower() == kw.lower() for k in exact_lst):
                exact_lst.append(kw)

        self._save()
        return True

    def remove(self, keyword: str, list_type: str = "broad") -> bool:
        """
        Remove keyword by exact or case-insensitive match.
        When removing from 'broad', also removes from 'exact'.
        Returns False if not found.
        """
        data = self._load()
        lst = data.get(list_type, [])
        matches = [k for k in lst if k.lower() == keyword.strip().lower()]
        if not matches:
            return False
        for m in matches:
            lst.remove(m)
        # Mirror removal to 'exact' when removing from 'broad'
        if list_type == "broad":
            exact_lst = data.get("exact", [])
            for m in matches:
                exact_matches = [k for k in exact_lst if k.lower() == m.lower()]
                for em in exact_matches:
                    exact_lst.remove(em)
        self._save()
        return True

    def remove_by_index(self, index: int, list_type: str = "broad") -> str | None:
        """
        Remove keyword by 1-based index. Returns removed keyword or None.
        When removing from 'broad', also removes from 'exact'.
        """
        data = self._load()
        lst = data.get(list_type, [])
        if index < 1 or index > len(lst):
            return None
        removed = lst.pop(index - 1)
        # Mirror removal to 'exact' when removing from 'broad'
        if list_type == "broad":
            exact_lst = data.get("exact", [])
            exact_matches = [k for k in exact_lst if k.lower() == removed.lower()]
            for em in exact_matches:
                exact_lst.remove(em)
        self._save()
        return removed

    def list_all(self) -> dict:
        return {
            "broad":     list(self._load()["broad"]),
            "exact":     list(self._load().get("exact", [])),
            "locations": list(self._load().get("locations", [])),
            "tier1":     list(self._load().get("tier1", _DEFAULT_TIER1)),
            "tier2":     list(self._load().get("tier2", _DEFAULT_TIER2)),
            "tier3":     list(self._load().get("tier3", _DEFAULT_TIER3)),
        }

    # ── Tier keywords ──────────────────────────────────────────

    def _tier_key(self, n: int) -> str:
        if n not in (1, 2, 3):
            raise ValueError(f"Tier must be 1, 2, or 3 — got {n}")
        return f"tier{n}"

    def _tier_default(self, n: int) -> List[str]:
        return [_DEFAULT_TIER1, _DEFAULT_TIER2, _DEFAULT_TIER3][n - 1]

    def get_tier(self, n: int) -> List[str]:
        key = self._tier_key(n)
        return list(self._load().get(key, self._tier_default(n)))

    def add_tier(self, keyword: str, n: int) -> bool:
        """Add keyword to tierN. Returns False if already present."""
        data = self._load()
        key  = self._tier_key(n)
        kw   = keyword.strip()
        lst  = data.setdefault(key, list(self._tier_default(n)))
        if any(k.lower() == kw.lower() for k in lst):
            return False
        lst.append(kw)
        self._save()
        return True

    def remove_tier(self, keyword: str, n: int) -> bool:
        """Remove keyword from tierN by exact (case-insensitive) match."""
        data = self._load()
        key  = self._tier_key(n)
        lst  = data.get(key, [])
        matches = [k for k in lst if k.lower() == keyword.strip().lower()]
        if not matches:
            return False
        for m in matches:
            lst.remove(m)
        self._save()
        return True

    def remove_tier_by_index(self, index: int, n: int) -> str | None:
        """Remove keyword from tierN by 1-based index. Returns removed keyword or None."""
        data = self._load()
        key  = self._tier_key(n)
        lst  = data.get(key, [])
        if index < 1 or index > len(lst):
            return None
        removed = lst.pop(index - 1)
        self._save()
        return removed

    def reload(self) -> None:
        """Force re-read from disk. Restores previous data if the reload fails."""
        old = self._data
        self._data = None
        try:
            self._load()
        except Exception:
            self._data = old
            raise


# ── Module-level singleton ─────────────────────────────────────
keyword_manager = KeywordManager()
