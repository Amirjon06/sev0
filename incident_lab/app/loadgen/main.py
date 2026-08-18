"""Load generator: keeps baseline traffic flowing through the storefront.

Without steady traffic the metrics are flat, and a flat baseline makes any
injected fault trivially visible. Realistic evaluation needs realistic noise.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

import httpx

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")
CART_URL = os.getenv("CART_URL", "http://cart:8002")
REQUESTS_PER_SECOND = float(os.getenv("LOADGEN_RPS", "4"))
CONCURRENCY = int(os.getenv("LOADGEN_CONCURRENCY", "4"))

SKUS = ["sku-001", "sku-002", "sku-004", "sku-005"]

# SUMMER25 is not a real promotion. Shoppers type stale codes off old emails all
# the time, and a healthy cart ignores them. Keeping that traffic in the mix
# means the unknown-code path is exercised continuously rather than only when a
# test happens to cover it.
PROMO_CODES = [None, None, None, "SAVE10", "WELCOME", "SAVE25", "SUMMER25"]


async def one_session(client: httpx.AsyncClient, user_id: str) -> None:
    """Simulate a shopper: browse, add items, then check out."""
    await client.get(f"{GATEWAY_URL}/products")

    for _ in range(random.randint(1, 3)):
        await client.post(
            f"{CART_URL}/carts/{user_id}/items",
            json={"product_id": random.choice(SKUS), "quantity": random.randint(1, 3)},
        )

    await client.post(
        f"{GATEWAY_URL}/checkout/{user_id}",
        json={"promo_code": random.choice(PROMO_CODES)},
    )
    await client.delete(f"{CART_URL}/carts/{user_id}")


async def worker(worker_id: int, interval: float) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            user_id = f"user-{random.randint(1, 200):04d}"
            try:
                await one_session(client, user_id)
            except Exception as exc:  # noqa: BLE001 - load generators must not die
                print(f"worker {worker_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            await asyncio.sleep(interval * random.uniform(0.6, 1.4))


async def main() -> None:
    interval = CONCURRENCY / REQUESTS_PER_SECOND
    print(
        f"load generator starting: {REQUESTS_PER_SECOND} rps target, "
        f"{CONCURRENCY} workers, {interval:.2f}s interval",
        flush=True,
    )
    await asyncio.gather(*(worker(i, interval) for i in range(CONCURRENCY)))


if __name__ == "__main__":
    asyncio.run(main())
