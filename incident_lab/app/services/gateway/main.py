"""Gateway service: the public entry point.

Fans out to catalog, cart, and payments. This is where user-visible failures
surface, which makes it the natural place for an alert to fire even when the
underlying fault lives in another service.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.common import get_logger, instrument, service_url

app = FastAPI(title="gateway")
instrument(app, "gateway")
log = get_logger("gateway")

TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class CheckoutRequest(BaseModel):
    promo_code: str | None = None


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
        cart_response = await client.get(
            f"{service_url('cart')}/carts/{user_id}",
            params={"promo_code": payload.promo_code} if payload.promo_code else None,
        )
        cart_response.raise_for_status()
        cart = cart_response.json()

        if not cart["items"]:
            raise HTTPException(status_code=400, detail="cart is empty")

        charge_response = await client.post(
            f"{service_url('payments')}/charge",
            json={"user_id": user_id, "amount_cents": cart["total_cents"]},
        )

    if charge_response.status_code == 402:
        raise HTTPException(status_code=402, detail="payment declined")
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
        "total_cents": cart["total_cents"],
        "charge_id": charge["charge_id"],
    }
