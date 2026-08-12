"""
API cost calculator for Claude models.
Prices in USD per million tokens, verified 2026-08-11.

NOTE: the Opus 4.7/4.8 rows were previously set to $15/$75 — that is the OLD
Opus-3-era rate and over-reported every dream application by 3x. The Opus 4.x
family is $5/$25. Haiku 4.5 was likewise under-reported at $0.80/$4.00.
"""
from __future__ import annotations

# USD per token
_PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 1.00 / 1_000_000,  "output": 5.00 / 1_000_000},
    "claude-haiku-4-5":           {"input": 1.00 / 1_000_000,  "output": 5.00 / 1_000_000},
    # Sonnet 5 carries introductory pricing ($2/$10) through 2026-08-31; the
    # standard $3/$15 rate applies after that. Listed at the standard rate so
    # spend is never under-reported — actual bills until then are ~1/3 lower.
    "claude-sonnet-5":            {"input": 3.00 / 1_000_000,  "output": 15.00 / 1_000_000},
    "claude-sonnet-4-6":          {"input": 3.00 / 1_000_000,  "output": 15.00 / 1_000_000},
    "claude-sonnet-4-5":          {"input": 3.00 / 1_000_000,  "output": 15.00 / 1_000_000},
    "claude-opus-5":              {"input": 5.00 / 1_000_000,  "output": 25.00 / 1_000_000},
    "claude-opus-4-8":            {"input": 5.00 / 1_000_000,  "output": 25.00 / 1_000_000},
    "claude-opus-4-7":            {"input": 5.00 / 1_000_000,  "output": 25.00 / 1_000_000},
    "claude-opus-4-1":            {"input": 15.00 / 1_000_000, "output": 75.00 / 1_000_000},
}
_DEFAULT = {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000}


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in USD for a single API call."""
    p = _PRICING.get(model, _DEFAULT)
    return round(input_tokens * p["input"] + output_tokens * p["output"], 6)


def format_cost(usd: float) -> str:
    """Human-readable cost string."""
    if usd < 0.01:
        return f"${usd * 100:.4f}¢"
    return f"${usd:.4f}"
