"""Pricing tests that travel with the storefront.

These live inside the application rather than in the sev0 repository because
they have a second job: they are what a proposed fix has to turn green. A test
suite the agent cannot run is no use as proof.
"""

from __future__ import annotations

import pytest

from services.cart.main import PROMOTIONS, compute_total

ITEMS = [
    {"price_cents": 12900, "quantity": 1},
    {"price_cents": 1200, "quantity": 2},
]

SUBTOTAL = 15300


def test_subtotal_multiplies_price_by_quantity() -> None:
    assert compute_total(ITEMS, None)["subtotal_cents"] == SUBTOTAL


def test_no_promo_code_applies_no_discount() -> None:
    result = compute_total(ITEMS, None)
    assert result["discount_cents"] == 0
    assert result["total_cents"] == SUBTOTAL


@pytest.mark.parametrize("code", sorted(PROMOTIONS))
def test_active_codes_apply_their_advertised_percentage(code: str) -> None:
    expected = SUBTOTAL * PROMOTIONS[code] // 100
    assert compute_total(ITEMS, code)["discount_cents"] == expected


def test_an_expired_code_is_ignored_rather_than_failing() -> None:
    # Shoppers paste stale codes off old emails constantly. An unknown code
    # means no discount, not a failed checkout.
    result = compute_total(ITEMS, "SUMMER25")
    assert result["discount_cents"] == 0
    assert result["total_cents"] == SUBTOTAL


def test_an_empty_cart_totals_zero() -> None:
    assert compute_total([], "SAVE25") == {
        "subtotal_cents": 0,
        "discount_cents": 0,
        "total_cents": 0,
    }
