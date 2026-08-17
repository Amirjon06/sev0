"""Catalog service: the product listing.

Read-only and in-memory. Prices are integer cents to avoid float rounding
noise polluting the fault scenarios.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from services.common import get_logger, instrument

app = FastAPI(title="catalog")
instrument(app, "catalog")
log = get_logger("catalog")

PRODUCTS: dict[str, dict[str, object]] = {
    "sku-001": {"id": "sku-001", "name": "Desk lamp", "price_cents": 3400, "stock": 42},
    "sku-002": {"id": "sku-002", "name": "Mechanical keyboard", "price_cents": 12900, "stock": 17},
    "sku-003": {"id": "sku-003", "name": "USB-C hub", "price_cents": 5900, "stock": 0},
    "sku-004": {"id": "sku-004", "name": "Monitor arm", "price_cents": 8750, "stock": 8},
    "sku-005": {"id": "sku-005", "name": "Notebook", "price_cents": 1200, "stock": 230},
}


@app.get("/products")
async def list_products() -> dict[str, object]:
    return {"products": list(PRODUCTS.values())}


@app.get("/products/{product_id}")
async def get_product(product_id: str) -> dict[str, object]:
    product = PRODUCTS.get(product_id)
    if product is None:
        log.info("product not found", extra={"extra": {"product_id": product_id}})
        raise HTTPException(status_code=404, detail="product not found")
    return product
