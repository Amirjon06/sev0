"""Cart service: line items, promotions, and order totals.

Backed by Postgres so that connection pool behavior is realistic. Totals are
computed in `compute_total`, which is the single place a pricing bug can hide.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from services.common import (
    ORDER_CENTS,
    ORDERS,
    env_flag,
    env_int,
    get_logger,
    instrument,
    service_url,
)

log = get_logger("cart")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cart_items (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT    NOT NULL,
    product_id  TEXT    NOT NULL,
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    price_cents INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cart_items_user_idx ON cart_items (user_id);
"""

# Active promotions, keyed by the code a shopper enters at checkout. Codes that
# are not in here are not errors: expired codes get typed in constantly.
PROMOTIONS: dict[str, int] = {
    "SAVE10": 10,
    "SAVE25": 25,
    "WELCOME": 5,
}

# Shipping is charged per order, not per item. Anything at or above the free
# threshold ships at no cost, which is the rule most shoppers actually notice.
FREE_SHIPPING_THRESHOLD_CENTS = 5000
SHIPPING_RATES: dict[str, int] = {
    "standard": 499,
    "express": 1299,
}
DEFAULT_SHIPPING_SPEED = "standard"

# Sales tax, in basis points so the rate is exact. Tax applies to goods after
# any discount, and to shipping, which is what most jurisdictions expect.
TAX_BASIS_POINTS = env_int("TAX_BASIS_POINTS", 875)

# Promotions can be switched off wholesale during an incident. When disabled,
# codes are accepted and ignored rather than rejected, because failing a
# checkout over a discount is worse than not applying it.
PROMOTIONS_ENABLED = env_flag("PROMOTIONS_ENABLED", True)

# A single line cannot exceed this. Anything above it is a client bug or an
# abusive request, and charging for it would be worse than refusing.
MAX_LINE_QUANTITY = env_int("MAX_LINE_QUANTITY", 99)

pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global pool
    dsn = os.environ["DATABASE_URL"]
    pool = ConnectionPool(
        dsn,
        min_size=2,
        max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")),
        timeout=float(os.getenv("DB_POOL_TIMEOUT", "5")),
        kwargs={"row_factory": dict_row},
    )
    pool.wait(timeout=30)
    with pool.connection() as conn:
        conn.execute(SCHEMA)
    log.info("cart service ready", extra={"extra": {"pool_max_size": pool.max_size}})
    yield
    pool.close()


app = FastAPI(title="cart", lifespan=lifespan)
instrument(app, "cart")


class AddItem(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1, le=99)


def get_pool() -> ConnectionPool:
    if pool is None:
        raise HTTPException(status_code=503, detail="cart database not ready")
    return pool


def shipping_cents(payable_cents: int, speed: str | None) -> int:
    """What shipping costs on an order of this size.

    Speed is whatever the shopper picked, which may be nothing at all. An
    unrecognised or missing speed falls back to standard rather than failing:
    a checkout should not break because of how the front end spelled it.
    """
    if payable_cents >= FREE_SHIPPING_THRESHOLD_CENTS:
        return 0

    chosen = speed or DEFAULT_SHIPPING_SPEED
    return SHIPPING_RATES.get(chosen, SHIPPING_RATES[DEFAULT_SHIPPING_SPEED])


def merge_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeat additions of the same product into one line.

    Shoppers add the same thing twice constantly. Two rows of one each and one
    row of two have to price identically, and a line that has grown past the
    per-line cap is clamped rather than rejected — the shopper still gets an
    order, just not an unbounded one.
    """
    merged: dict[str, dict[str, Any]] = {}

    for row in rows:
        product_id = row["product_id"]
        existing = merged.get(product_id)
        if existing is None:
            merged[product_id] = dict(row)
            continue
        existing["quantity"] += row["quantity"]

    for line in merged.values():
        line["quantity"] = min(line["quantity"], MAX_LINE_QUANTITY)

    return list(merged.values())


def tax_cents(taxable_cents: int) -> int:
    """Tax owed on an amount, rounded to the nearest cent.

    Rounds half up rather than truncating. Truncation over a day of orders
    costs real money, and the direction of the error is always the same one.
    """
    if taxable_cents <= 0:
        return 0
    return (taxable_cents * TAX_BASIS_POINTS + 5000) // 10000


def discount_cents(subtotal: int, promo_code: str | None) -> int:
    """What a promotion code is worth on this subtotal.

    An unknown code is worth nothing rather than being an error: stale codes
    get pasted out of old emails every day and a checkout must survive them.
    """
    if not PROMOTIONS_ENABLED or not promo_code:
        return 0

    percent = PROMOTIONS.get(promo_code)
    if percent is None:
        return 0

    return subtotal * percent // 100


def compute_total(
    items: list[dict[str, Any]],
    promo_code: str | None,
    shipping_speed: str | None = None,
) -> dict[str, int]:
    """Compute the order total in cents.

    Returns the subtotal, the discount applied, shipping, tax, and the final
    total. Shipping is judged on what is actually payable after the discount,
    so an order discounted below the threshold owes delivery again.
    """
    lines = merge_lines(items) if items and "product_id" in items[0] else list(items)
    subtotal = sum(item["price_cents"] * item["quantity"] for item in lines)

    discount = discount_cents(subtotal, promo_code)
    payable = subtotal - discount
    shipping = shipping_cents(payable, shipping_speed)
    tax = tax_cents(payable + shipping)

    ORDERS.labels("true" if shipping == 0 else "false").inc()
    ORDER_CENTS.labels("subtotal").inc(subtotal)
    ORDER_CENTS.labels("discount").inc(discount)
    ORDER_CENTS.labels("shipping").inc(shipping)
    ORDER_CENTS.labels("tax").inc(tax)

    return {
        "subtotal_cents": subtotal,
        "discount_cents": discount,
        "shipping_cents": shipping,
        "tax_cents": tax,
        "total_cents": payable + shipping + tax,
    }


@app.post("/carts/{user_id}/items", status_code=201)
async def add_item(user_id: str, payload: AddItem) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=3.0) as client:
        response = await client.get(
            f"{service_url('catalog')}/products/{payload.product_id}",
            params={"quantity": payload.quantity},
        )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="product not found")
    if response.status_code == 409:
        raise HTTPException(status_code=409, detail="insufficient stock")
    response.raise_for_status()
    product = response.json()

    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO cart_items (user_id, product_id, quantity, price_cents)"
            " VALUES (%s, %s, %s, %s)",
            (user_id, payload.product_id, payload.quantity, product["price_cents"]),
        )

    log.info(
        "item added",
        extra={"extra": {"user_id": user_id, "product_id": payload.product_id}},
    )
    return {"user_id": user_id, "product_id": payload.product_id, "quantity": payload.quantity}


@app.get("/carts/{user_id}")
async def get_cart(
    user_id: str,
    promo_code: str | None = None,
    shipping_speed: str | None = None,
) -> dict[str, object]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT product_id, quantity, price_cents FROM cart_items WHERE user_id = %s",
            (user_id,),
        ).fetchall()

    totals = compute_total(list(rows), promo_code, shipping_speed)
    return {
        "user_id": user_id,
        "items": rows,
        "promo_code": promo_code,
        "shipping_speed": shipping_speed,
        **totals,
    }


@app.delete("/carts/{user_id}", status_code=204, response_class=Response)
async def clear_cart(user_id: str) -> Response:
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM cart_items WHERE user_id = %s", (user_id,))
    return Response(status_code=204)


@app.get("/stats")
async def stats() -> dict[str, object]:
    """Pool utilization, so pool exhaustion is observable rather than inferred."""
    p = get_pool()
    return {
        "pool_max_size": p.max_size,
        "pool_min_size": p.min_size,
        "promotions_enabled": PROMOTIONS_ENABLED,
        "tax_basis_points": TAX_BASIS_POINTS,
        "max_line_quantity": MAX_LINE_QUANTITY,
    }
