"""Gateway service: the public entry point.

Fans out to catalog, cart, and payments. This is where user-visible failures
surface, which makes it the natural place for an alert to fire even when the
underlying fault lives in another service.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.common import env_float, env_int, get_logger, instrument, service_url

app = FastAPI(title="gateway")
instrument(app, "gateway")
log = get_logger("gateway")

REQUEST_TIMEOUT_SECONDS = env_float("GATEWAY_TIMEOUT_SECONDS", 5.0)
CONNECT_TIMEOUT_SECONDS = env_float("GATEWAY_CONNECT_TIMEOUT_SECONDS", 2.0)
TIMEOUT = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)

MAX_ATTEMPTS = env_int("GATEWAY_MAX_ATTEMPTS", 3)
RETRY_BACKOFF_SECONDS = env_float("GATEWAY_RETRY_BACKOFF_SECONDS", 0.05)


class CheckoutRequest(BaseModel):
    promo_code: str | None = None
    shipping_speed: str | None = None


def charge_error(status_code: int) -> HTTPException | None:
    """Translate a charge response into the error the shopper should see.

    A declined card and a rejected amount are both the shopper's problem, not
    ours, and reporting either as an internal error pages someone for something
    no engineer can fix. Anything else is genuinely our failure.
    """
    if status_code == 402:
        return HTTPException(status_code=402, detail="payment declined")
    if status_code == 400:
        return HTTPException(status_code=400, detail="payment rejected the amount")
    return None


def should_retry(status_code: int) -> bool:
    """Whether a response is worth trying again.

    Only server-side failures are. A 4xx means the request itself was wrong,
    and sending it again produces the same answer while doubling the load on a
    service that is already saying no. 402 in particular is a declined card:
    retrying it charges a shopper twice for one order.
    """
    return status_code >= 500


async def call_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: object,
) -> httpx.Response:
    """Issue a request, retrying only what is worth retrying."""
    last: httpx.Response | None = None

    for attempt in range(MAX_ATTEMPTS):
        response = await client.request(method, url, **kwargs)  # type: ignore[arg-type]
        if not should_retry(response.status_code):
            return response

        last = response
        log.warning(
            "upstream returned a retryable status",
            extra={"extra": {"url": url, "status": response.status_code, "attempt": attempt + 1}},
        )
        if attempt + 1 < MAX_ATTEMPTS:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

    assert last is not None
    return last


@app.get("/")
async def index() -> dict[str, str]:
    return {"service": "storefront gateway", "docs": "/docs"}


@app.get("/products")
async def products() -> dict[str, object]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(f"{service_url('catalog')}/products")
    response.raise_for_status()
    return response.json()


@app.post("/checkout/{user_id}")
async def checkout(user_id: str, payload: CheckoutRequest) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        params = {
            key: value
            for key, value in (
                ("promo_code", payload.promo_code),
                ("shipping_speed", payload.shipping_speed),
            )
            if value
        }
        cart_response = await call_with_retry(
            client,
            "GET",
            f"{service_url('cart')}/carts/{user_id}",
            params=params or None,
        )
        cart_response.raise_for_status()
        cart = cart_response.json()

        if not cart["items"]:
            raise HTTPException(status_code=400, detail="cart is empty")

        # One key per checkout attempt, so a retry of a charge that already
        # succeeded returns the original authorisation instead of taking the
        # money again.
        charge_response = await call_with_retry(
            client,
            "POST",
            f"{service_url('payments')}/charge",
            json={
                "user_id": user_id,
                "amount_cents": cart["total_cents"],
                "idempotency_key": f"{user_id}:{uuid.uuid4().hex[:12]}",
            },
        )

    if error := charge_error(charge_response.status_code):
        raise error
    charge_response.raise_for_status()
    charge = charge_response.json()

    log.info(
        "checkout completed",
        extra={
            "extra": {
                "user_id": user_id,
                "total_cents": cart["total_cents"],
                "charge_id": charge["charge_id"],
            }
        },
    )
    return {
        "user_id": user_id,
        "items": cart["items"],
        "subtotal_cents": cart["subtotal_cents"],
        "discount_cents": cart["discount_cents"],
        "shipping_cents": cart["shipping_cents"],
        "tax_cents": cart.get("tax_cents", 0),
        "total_cents": cart["total_cents"],
        "charge_id": charge["charge_id"],
    }
