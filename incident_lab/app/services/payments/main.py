"""Payments service: charge authorization.

Simulated, but with a realistic latency profile and a small baseline decline
rate so that error-rate dashboards are never perfectly flat. A flat baseline
makes fault injection trivially detectable, which would flatter the agent.
"""

from __future__ import annotations

import asyncio
import random
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.common import CHARGES, env_float, env_int, get_logger, instrument

app = FastAPI(title="payments")
instrument(app, "payments")
log = get_logger("payments")

BASE_LATENCY_MS = env_float("PAYMENTS_BASE_LATENCY_MS", 35)
LATENCY_JITTER_MS = env_float("PAYMENTS_LATENCY_JITTER_MS", 25)
DECLINE_RATE = env_float("PAYMENTS_DECLINE_RATE", 0.02)

# The largest single charge the processor will accept. Anything above it is a
# pricing bug upstream, and authorising it would move real money.
MAX_CHARGE_CENTS = env_int("PAYMENTS_MAX_CHARGE_CENTS", 500_000)

# Charges already authorised, keyed by idempotency key. A retry of a charge
# that already went through must return the original authorisation rather than
# taking the money a second time.
_authorised: dict[str, dict[str, object]] = {}


class ChargeRequest(BaseModel):
    user_id: str
    amount_cents: int = Field(ge=0)
    idempotency_key: str | None = None


def replay(idempotency_key: str | None) -> dict[str, object] | None:
    """The earlier authorisation for this key, if there was one."""
    if not idempotency_key:
        return None
    return _authorised.get(idempotency_key)


@app.post("/charge")
async def charge(payload: ChargeRequest) -> dict[str, object]:
    delay_ms = BASE_LATENCY_MS + random.uniform(0, LATENCY_JITTER_MS)
    await asyncio.sleep(delay_ms / 1000)

    previous = replay(payload.idempotency_key)
    if previous is not None:
        CHARGES.labels("replayed").inc()
        return previous

    if payload.amount_cents <= 0:
        CHARGES.labels("invalid").inc()
        raise HTTPException(status_code=400, detail="amount must be positive")

    if payload.amount_cents > MAX_CHARGE_CENTS:
        CHARGES.labels("invalid").inc()
        log.warning(
            "charge exceeds the per-transaction ceiling",
            extra={"extra": {"user_id": payload.user_id, "amount_cents": payload.amount_cents}},
        )
        raise HTTPException(status_code=400, detail="amount exceeds limit")

    if random.random() < DECLINE_RATE:
        CHARGES.labels("declined").inc()
        log.info(
            "charge declined",
            extra={"extra": {"user_id": payload.user_id, "amount_cents": payload.amount_cents}},
        )
        raise HTTPException(status_code=402, detail="card declined")

    charge_id = f"ch_{uuid.uuid4().hex[:12]}"
    authorisation: dict[str, object] = {
        "charge_id": charge_id,
        "status": "authorized",
        "amount_cents": payload.amount_cents,
    }
    if payload.idempotency_key:
        _authorised[payload.idempotency_key] = authorisation

    CHARGES.labels("authorized").inc()
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
    return authorisation
