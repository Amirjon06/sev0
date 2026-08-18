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

from services.common import get_logger, instrument, service_url

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

# Promotions are looked up by code. A missing code yields None, which callers
# must handle -- this is intentional surface area for a null-handling fault.
PROMOTIONS: dict[str, int] = {
    "SAVE10": 10,
    "SAVE25": 25,
    "WELCOME": 5,
}

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


def compute_total(items: list[dict[str, Any]], promo_code: str | None) -> dict[str, int]:
    """Compute the order total in cents.

    Returns the subtotal, the discount applied, and the final total.
    """
    subtotal = sum(item["price_cents"] * item["quantity"] for item in items)

    discount_percent = PROMOTIONS.get(promo_code) if promo_code else None
    discount = 0
    if discount_percent is not None:
        discount = subtotal * discount_percent // 100

    return {
        "subtotal_cents": subtotal,
        "discount_cents": discount,
        "total_cents": subtotal - discount,
    }


@app.post("/carts/{user_id}/items", status_code=201)
async def add_item(user_id: str, payload: AddItem) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=3.0) as client:
        response = await client.get(f"{service_url('catalog')}/products/{payload.product_id}")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="product not found")
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
async def get_cart(user_id: str, promo_code: str | None = None) -> dict[str, object]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT product_id, quantity, price_cents FROM cart_items WHERE user_id = %s",
            (user_id,),
        ).fetchall()

    totals = compute_total(list(rows), promo_code)
    return {"user_id": user_id, "items": rows, "promo_code": promo_code, **totals}


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
    }
