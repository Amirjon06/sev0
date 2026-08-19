"""Turning token counts into a dollar figure, when that can be done honestly.

Prices are not in the API response, so they have to be written down here, and
anything written down goes stale. The rule this module follows is that an
unknown model produces no cost at all rather than an approximate one: a
benchmark table quoting a made-up number is worse than one quoting nothing,
because a blank is obviously missing and a wrong figure is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from sev0.agent.state import Usage


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


# Published list prices, in US dollars per million tokens. Update these when
# the pricing page changes; a stale entry is a wrong number, which is the one
# thing this module exists to avoid.
PRICES: dict[str, Price] = {
    "claude-opus-5": Price(15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-5": Price(3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": Price(1.0, 5.0, 0.10, 1.25),
}


def price_for(model: str) -> Price | None:
    """The rate card for a model, or None if it is not one we have priced."""
    return PRICES.get(model.strip())


def estimate_cost(usage: Usage, model: str) -> float | None:
    """What this run cost, or None if the model has no published price here.

    Cache reads and writes are charged at their own rates rather than folded
    into the input rate, because on a long tool-use loop they dominate and
    pretending otherwise would overstate the cost several times over.
    """
    price = price_for(model)
    if price is None:
        return None

    return round(
        usage.input_tokens / 1_000_000 * price.input_per_mtok
        + usage.output_tokens / 1_000_000 * price.output_per_mtok
        + usage.cache_read_tokens / 1_000_000 * price.cache_read_per_mtok
        + usage.cache_write_tokens / 1_000_000 * price.cache_write_per_mtok,
        6,
    )
