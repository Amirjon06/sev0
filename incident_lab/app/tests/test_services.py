"""Behavioural tests for catalog, payments, and gateway logic.

Same job as the pricing suite: these are what a proposed fix has to keep green,
so they assert the rule rather than the implementation. Every function covered
here is somewhere a fault can be planted, and a symbol with no test is a symbol
whose repair cannot be verified.
"""

from __future__ import annotations

import pytest

from services.catalog.main import PRODUCTS, is_available, search_products
from services.gateway.main import charge_error, should_retry
from services.payments.main import _authorised, replay


class TestAvailability:
    def test_the_last_unit_can_be_sold(self) -> None:
        assert is_available({"stock": 1}, 1)

    def test_one_more_than_exists_cannot(self) -> None:
        assert not is_available({"stock": 1}, 2)

    def test_nothing_is_available_from_empty_stock(self) -> None:
        assert not is_available({"stock": 0}, 1)

    def test_a_large_order_against_deep_stock_is_fine(self) -> None:
        assert is_available({"stock": 230}, 99)


class TestSearch:
    def test_an_empty_query_lists_everything(self) -> None:
        assert len(search_products("", limit=99)) == len(PRODUCTS)

    def test_matching_is_case_insensitive(self) -> None:
        assert search_products("KEYBOARD")

    def test_a_substring_matches(self) -> None:
        names = [str(p["name"]) for p in search_products("lamp")]
        assert names == ["Desk lamp"]

    def test_nonsense_matches_nothing(self) -> None:
        assert search_products("wheelbarrow") == []

    def test_the_limit_is_respected(self) -> None:
        assert len(search_products("", limit=2)) == 2


class TestRetryPolicy:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retried(self, status: int) -> None:
        assert should_retry(status)

    @pytest.mark.parametrize("status", [200, 201, 204, 400, 404, 409])
    def test_success_and_client_errors_are_not(self, status: int) -> None:
        assert not should_retry(status)

    def test_a_declined_card_is_never_retried(self) -> None:
        # 402 means the card said no. Sending it again does not change that
        # answer and risks charging a shopper twice for one order.
        assert not should_retry(402)

    def test_rate_limiting_is_not_retried_blindly(self) -> None:
        # 429 needs a backoff strategy the gateway does not have. Hammering it
        # on the same schedule as a 500 makes the problem worse.
        assert not should_retry(429)


class TestIdempotency:
    def setup_method(self) -> None:
        _authorised.clear()

    def test_an_unseen_key_has_no_earlier_authorisation(self) -> None:
        assert replay("fresh-key") is None

    def test_no_key_means_no_replay(self) -> None:
        _authorised["some-key"] = {"charge_id": "ch_1"}
        assert replay(None) is None

    def test_a_repeated_key_returns_the_original_authorisation(self) -> None:
        original = {"charge_id": "ch_original", "status": "authorized"}
        _authorised["repeat"] = original

        assert replay("repeat") is original


class TestChargeErrorMapping:
    def test_a_declined_card_is_a_client_error(self) -> None:
        error = charge_error(402)
        assert error is not None and error.status_code == 402

    def test_a_rejected_amount_is_a_client_error(self) -> None:
        # Payments refusing an amount is an upstream pricing problem, not
        # an outage. Reporting it as a 500 pages someone for nothing.
        error = charge_error(400)
        assert error is not None and error.status_code == 400

    def test_a_successful_charge_maps_to_no_error(self) -> None:
        assert charge_error(200) is None

    def test_an_upstream_failure_is_left_to_the_generic_path(self) -> None:
        assert charge_error(503) is None
