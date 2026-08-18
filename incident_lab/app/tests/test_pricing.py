"""Pricing tests that travel with the storefront.

These live inside the application rather than in the sev0 repository because
they have a second job: they are what a proposed fix has to turn green. A test
suite the agent cannot run is no use as proof.
"""

from __future__ import annotations

import pytest

from services.cart.main import (
    FREE_SHIPPING_THRESHOLD_CENTS,
    PROMOTIONS,
    SHIPPING_RATES,
    compute_total,
    shipping_cents,
)

# Comfortably below the free shipping threshold, so shipping is always charged
# and a shipping bug cannot hide behind a free order.
SMALL_ORDER = [
    {"price_cents": 1200, "quantity": 2},
    {"price_cents": 400, "quantity": 1},
]
SMALL_SUBTOTAL = 2800

LARGE_ORDER = [{"price_cents": 12900, "quantity": 1}]
LARGE_SUBTOTAL = 12900


class TestSubtotalAndDiscounts:
    def test_subtotal_multiplies_price_by_quantity(self) -> None:
        assert compute_total(SMALL_ORDER, None)["subtotal_cents"] == SMALL_SUBTOTAL

    def test_no_promo_code_applies_no_discount(self) -> None:
        assert compute_total(SMALL_ORDER, None)["discount_cents"] == 0

    @pytest.mark.parametrize("code", sorted(PROMOTIONS))
    def test_active_codes_apply_their_advertised_percentage(self, code: str) -> None:
        expected = SMALL_SUBTOTAL * PROMOTIONS[code] // 100
        assert compute_total(SMALL_ORDER, code)["discount_cents"] == expected

    def test_an_expired_code_is_ignored_rather_than_failing(self) -> None:
        # Shoppers paste stale codes off old emails constantly. An unknown code
        # means no discount, not a failed checkout.
        assert compute_total(SMALL_ORDER, "SUMMER25")["discount_cents"] == 0

    def test_an_empty_cart_totals_zero_before_shipping(self) -> None:
        result = compute_total([], "SAVE25")
        assert result["subtotal_cents"] == 0
        assert result["discount_cents"] == 0


class TestShipping:
    def test_a_large_order_ships_free(self) -> None:
        assert shipping_cents(LARGE_SUBTOTAL, "standard") == 0

    def test_exactly_the_threshold_ships_free(self) -> None:
        # The advertised rule is "free over £50", and shoppers read that as
        # including £50. An off-by-one here is a support ticket, not a crash.
        assert shipping_cents(FREE_SHIPPING_THRESHOLD_CENTS, "standard") == 0

    def test_a_penny_under_the_threshold_is_charged(self) -> None:
        assert shipping_cents(FREE_SHIPPING_THRESHOLD_CENTS - 1, "standard") == 499

    @pytest.mark.parametrize("speed", sorted(SHIPPING_RATES))
    def test_each_speed_charges_its_rate(self, speed: str) -> None:
        assert shipping_cents(1000, speed) == SHIPPING_RATES[speed]

    def test_no_chosen_speed_falls_back_to_standard(self) -> None:
        # Most shoppers never touch the selector, so this is the common path.
        assert shipping_cents(1000, None) == SHIPPING_RATES["standard"]

    def test_an_unrecognised_speed_falls_back_rather_than_failing(self) -> None:
        # A checkout should not break because the front end sent "Express"
        # instead of "express".
        assert shipping_cents(1000, "overnight") == SHIPPING_RATES["standard"]


class TestOrderTotal:
    def test_shipping_is_added_to_the_total(self) -> None:
        result = compute_total(SMALL_ORDER, None, "standard")

        assert result["shipping_cents"] == 499
        assert result["total_cents"] == SMALL_SUBTOTAL + 499

    def test_a_large_order_pays_no_shipping(self) -> None:
        result = compute_total(LARGE_ORDER, None, "express")

        assert result["shipping_cents"] == 0
        assert result["total_cents"] == LARGE_SUBTOTAL

    def test_shipping_is_judged_on_what_is_actually_payable(self) -> None:
        # A discount that drops an order below the threshold means shipping is
        # owed. Charging on the pre-discount subtotal would give free delivery
        # to orders that no longer qualify.
        items = [{"price_cents": 5200, "quantity": 1}]
        result = compute_total(items, "SAVE10", "standard")

        assert result["discount_cents"] == 520
        assert result["shipping_cents"] == 499
