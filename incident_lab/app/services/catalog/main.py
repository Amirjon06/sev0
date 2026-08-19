"""Catalog service: the product listing.

Read-only and in-memory. Prices are integer cents to avoid float rounding
noise polluting the fault scenarios.
"""

from __future__ import annotations

import asyncio
import random

from fastapi import FastAPI, HTTPException

from services.common import env_float, get_logger, instrument

app = FastAPI(title="catalog")
instrument(app, "catalog")
log = get_logger("catalog")

# A read-only in-memory service is unrealistically reliable. These let the
# deployment dial in the sort of transient failure and latency a real
# dependency has, so the retry path upstream is exercised by real traffic.
ERROR_RATE = env_float("CATALOG_ERROR_RATE", 0.0)
LATENCY_MS = env_float("CATALOG_LATENCY_MS", 0.0)


async def simulate_dependency() -> None:
    """Apply the configured latency, and fail sometimes if configured to."""
    if LATENCY_MS > 0:
        await asyncio.sleep(LATENCY_MS / 1000)
    if ERROR_RATE > 0 and random.random() < ERROR_RATE:
        log.warning("catalog backend unavailable")
        raise HTTPException(status_code=503, detail="catalog temporarily unavailable")

PRODUCTS: dict[str, dict[str, object]] = {
    "sku-001": {"id": "sku-001", "name": "Desk lamp", "price_cents": 3400, "stock": 42},
    "sku-002": {"id": "sku-002", "name": "Mechanical keyboard", "price_cents": 12900, "stock": 17},
    "sku-003": {"id": "sku-003", "name": "USB-C hub", "price_cents": 5900, "stock": 0},
    "sku-004": {"id": "sku-004", "name": "Monitor arm", "price_cents": 8750, "stock": 8},
    "sku-005": {"id": "sku-005", "name": "Notebook", "price_cents": 1200, "stock": 230},
}


def is_available(product: dict[str, object], wanted: int) -> bool:
    """Whether this many units can be sold right now.

    Selling the last unit is allowed; selling one more than exists is not.
    """
    stock = int(product["stock"])  # type: ignore[call-overload]
    return wanted <= stock


def search_products(query: str, limit: int = 10) -> list[dict[str, object]]:
    """Case-insensitive substring match over product names.

    An empty query lists everything rather than nothing, because that is what
    an unfiltered browse looks like from the front end.
    """
    needle = query.strip().lower()
    matches = [
        product
        for product in PRODUCTS.values()
        if not needle or needle in str(product["name"]).lower()
    ]
    return matches[:limit]


@app.get("/products")
async def list_products(q: str = "", limit: int = 10) -> dict[str, object]:
    await simulate_dependency()
    return {"products": search_products(q, limit)}


@app.get("/products/{product_id}")
async def get_product(product_id: str, quantity: int = 1) -> dict[str, object]:
    await simulate_dependency()

    product = PRODUCTS.get(product_id)
    if product is None:
        log.info("product not found", extra={"extra": {"product_id": product_id}})
        raise HTTPException(status_code=404, detail="product not found")

    if not is_available(product, quantity):
        log.info(
            "insufficient stock",
            extra={"extra": {"product_id": product_id, "wanted": quantity}},
        )
        raise HTTPException(status_code=409, detail="insufficient stock")

    return product
