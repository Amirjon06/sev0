"""Pricing tests that travel with the storefront.

These live inside the application rather than in the sev0 repository because
they have a second job: they are what a proposed fix has to turn green. A test
suite the agent cannot run is no use as proof.

Every assertion here states a rule a shopper would recognise, not an
implementation detail. A benchmark whose tests encode the current code rather
than the intended behaviour would pass any patch that preserved the bug.
"""

from __future__ import annotations

import pytest

from services.cart.main import (
    FREE_SHIPPING_THRESHOLD_CENTS,
    MAX_LINE_QUANTITY,
    PROMOTIONS,
    SHIPPING_RATES,
    TAX_BASIS_POINTS,
    compute_total,
    discount_cents,
    merge_lines,
    shipping_cents,
    tax_cents,
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

    def test_a_discount_never_exceeds_the_subtotal(self) -> None:
        assert discount_cents(SMALL_SUBTOTAL, "SAVE25") <= SMALL_SUBTOTAL

    def test_a_blank_code_is_not_a_discount(self) -> None:
        assert discount_cents(SMALL_SUBTOTAL, "") == 0


class TestShipping:
    def test_a_large_order_ships_free(self) -> None:
        assert shipping_cents(LARGE_SUBTOTAL, "standard") == 0

    def test_exactly_the_threshold_ships_free(self) -> None:
        # The advertised rule is "free over $50", and shoppers read that as
        # including $50. An off-by-one here is a support ticket, not a crash.
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


class TestTax:
    def test_tax_is_charged_at_the_configured_rate(self) -> None:
        assert tax_cents(10_000) == (10_000 * TAX_BASIS_POINTS + 5000) // 10_000

    def test_nothing_owed_on_nothing(self) -> None:
        assert tax_cents(0) == 0

    def test_a_credit_is_not_taxed(self) -> None:
        assert tax_cents(-500) == 0

    def test_rounding_goes_to_the_nearest_cent(self) -> None:
        # Truncating instead of rounding loses money in one direction on every
        # order, which adds up to a real number over a day of traffic.
        assert tax_cents(1) == 0
        assert tax_cents(100) == 9

    def test_tax_covers_shipping_as_well_as_goods(self) -> None:
        result = compute_total(SMALL_ORDER, None, "standard")
        assert result["tax_cents"] == tax_cents(SMALL_SUBTOTAL + 499)


class TestLineMerging:
    def test_repeat_additions_of_one_product_become_one_line(self) -> None:
        lines = merge_lines(
            [
                {"product_id": "sku-001", "price_cents": 3400, "quantity": 1},
                {"product_id": "sku-001", "price_cents": 3400, "quantity": 2},
            ]
        )

        assert len(lines) == 1
        assert lines[0]["quantity"] == 3

    def test_different_products_stay_separate(self) -> None:
        lines = merge_lines(
            [
                {"product_id": "sku-001", "price_cents": 3400, "quantity": 1},
                {"product_id": "sku-002", "price_cents": 12900, "quantity": 1},
            ]
        )

        assert len(lines) == 2

    def test_a_merged_line_is_clamped_rather_than_rejected(self) -> None:
        lines = merge_lines(
            [{"product_id": "sku-005", "price_cents": 1200, "quantity": 60}] * 3
        )

        assert lines[0]["quantity"] == MAX_LINE_QUANTITY

    def test_merging_does_not_mutate_the_rows_it_was_given(self) -> None:
        rows = [{"product_id": "sku-001", "price_cents": 3400, "quantity": 1}]
        merge_lines(rows + rows)

        assert rows[0]["quantity"] == 1

    def test_two_additions_price_the_same_as_one_combined_line(self) -> None:
        split = compute_total(
            [
                {"product_id": "sku-001", "price_cents": 1200, "quantity": 1},
                {"product_id": "sku-001", "price_cents": 1200, "quantity": 1},
            ],
            None,
            "standard",
        )
        combined = compute_total(
            [{"product_id": "sku-001", "price_cents": 1200, "quantity": 2}],
            None,
            "standard",
        )

        assert split["total_cents"] == combined["total_cents"]


class TestOrderTotal:
    def test_shipping_is_added_to_the_total(self) -> None:
        result = compute_total(SMALL_ORDER, None, "standard")

        assert result["shipping_cents"] == 499
        assert result["total_cents"] == SMALL_SUBTOTAL + 499 + result["tax_cents"]

    def test_a_large_order_pays_no_shipping(self) -> None:
        result = compute_total(LARGE_ORDER, None, "express")

        assert result["shipping_cents"] == 0
        assert result["total_cents"] == LARGE_SUBTOTAL + result["tax_cents"]

    def test_shipping_is_judged_on_what_is_actually_payable(self) -> None:
        # A discount that drops an order below the threshold means shipping is
        # owed. Charging on the pre-discount subtotal would give free delivery
        # to orders that no longer qualify.
        items = [{"price_cents": 5200, "quantity": 1}]
        result = compute_total(items, "SAVE10", "standard")

        assert result["discount_cents"] == 520
        assert result["shipping_cents"] == 499

    def test_the_total_is_the_sum_of_its_parts(self) -> None:
        result = compute_total(SMALL_ORDER, "SAVE10", "express")

        expected = (
            result["subtotal_cents"]
            - result["discount_cents"]
            + result["shipping_cents"]
            + result["tax_cents"]
        )
        assert result["total_cents"] == expected

    def test_a_total_is_never_negative(self) -> None:
        assert compute_total([], "SAVE25", "standard")["total_cents"] >= 0
