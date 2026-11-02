"""List prices in USD per 1M tokens, used for an *estimate*. Check against provider pricing pages.

Models absent from this table report `estimated_cost_usd: null` rather than a made-up number.
"""

from __future__ import annotations

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    name = model.split(":", 1)[-1]
    if model.startswith("ollama:"):
        return 0.0
    # Providers return dated names such as gpt-4o-mini-2024-07-18; longest prefix wins.
    for known in sorted(PRICES_PER_MTOK, key=len, reverse=True):
        if name == known or name.startswith(known + "-"):
            prompt_price, completion_price = PRICES_PER_MTOK[known]
            return round((input_tokens * prompt_price + output_tokens * completion_price) / 1_000_000, 8)
    return None
