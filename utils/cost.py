"""
API cost calculator for Claude models.
Prices in USD per token (as of 2025).
"""
from __future__ import annotations

# USD per token
_PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80 / 1_000_000,  "output": 4.00 / 1_000_000},
    "claude-haiku-4-5":           {"input": 0.80 / 1_000_000,  "output": 4.00 / 1_000_000},
    "claude-sonnet-4-6":          {"input": 3.00 / 1_000_000,  "output": 15.00 / 1_000_000},
    "claude-opus-4-7":            {"input": 15.00 / 1_000_000, "output": 75.00 / 1_000_000},
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
