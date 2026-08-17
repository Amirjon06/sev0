"""Pricing tests for the demo storefront.

These are the assertions a planted pricing fault must break. They double as the
regression suite the agent runs to prove a proposed fix actually works.
"""

from __future__ import annotations

import pytest

from services.cart.main import compute_total

ITEMS = [
    {"price_cents": 1000, "quantity": 2},
    {"price_cents": 500, "quantity": 1},
]


def test_subtotal_multiplies_price_by_quantity() -> None:
    assert compute_total(ITEMS, None)["subtotal_cents"] == 2500


def test_no_promo_code_applies_no_discount() -> None:
    result = compute_total(ITEMS, None)
    assert result["discount_cents"] == 0
    assert result["total_cents"] == 2500


def test_known_promo_code_applies_percentage_discount() -> None:
    result = compute_total(ITEMS, "SAVE10")
    assert result["discount_cents"] == 250
    assert result["total_cents"] == 2250


def test_unknown_promo_code_is_ignored_rather_than_failing() -> None:
    result = compute_total(ITEMS, "NOT-A-REAL-CODE")
    assert result["discount_cents"] == 0
    assert result["total_cents"] == 2500


def test_empty_cart_totals_zero() -> None:
    assert compute_total([], "SAVE25") == {
        "subtotal_cents": 0,
        "discount_cents": 0,
        "total_cents": 0,
    }


@pytest.mark.parametrize(("code", "expected_total"), [("SAVE10", 2250), ("SAVE25", 1875), ("WELCOME", 2375)])
def test_each_promotion_matches_its_advertised_percentage(code: str, expected_total: int) -> None:
    assert compute_total(ITEMS, code)["total_cents"] == expected_total
