"""Payments service: charge authorization.

Simulated, but with a realistic latency profile and a small baseline decline
rate so that error-rate dashboards are never perfectly flat. A flat baseline
makes fault injection trivially detectable, which would flatter the agent.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.common import get_logger, instrument

app = FastAPI(title="payments")
instrument(app, "payments")
log = get_logger("payments")

BASE_LATENCY_MS = float(os.getenv("PAYMENTS_BASE_LATENCY_MS", "35"))
LATENCY_JITTER_MS = float(os.getenv("PAYMENTS_LATENCY_JITTER_MS", "25"))
DECLINE_RATE = float(os.getenv("PAYMENTS_DECLINE_RATE", "0.02"))


class ChargeRequest(BaseModel):
    user_id: str
    amount_cents: int = Field(ge=0)
    idempotency_key: str | None = None


@app.post("/charge")
async def charge(payload: ChargeRequest) -> dict[str, object]:
    delay_ms = BASE_LATENCY_MS + random.uniform(0, LATENCY_JITTER_MS)
    await asyncio.sleep(delay_ms / 1000)

    if payload.amount_cents <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")

    if random.random() < DECLINE_RATE:
        log.info(
            "charge declined",
            extra={"extra": {"user_id": payload.user_id, "amount_cents": payload.amount_cents}},
        )
        raise HTTPException(status_code=402, detail="card declined")

    charge_id = f"ch_{uuid.uuid4().hex[:12]}"
    log.info(
        "charge authorized",
        extra={
            "extra": {
                "user_id": payload.user_id,
                "amount_cents": payload.amount_cents,
                "charge_id": charge_id,
            }
        },
    )
    return {"charge_id": charge_id, "status": "authorized", "amount_cents": payload.amount_cents}
