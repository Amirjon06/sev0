# Demo storefront

The application Incident Lab breaks. Four Python services plus Postgres and a
load generator, chosen to be small enough to reason about and realistic enough
that a fault is not obvious from a glance.

## Services

| Service | Port | Responsibility |
| --- | --- | --- |
| **gateway** | 8000 | Public entry point; fans out to the others |
| **catalog** | 8001 | Product listing, in-memory |
| **cart** | 8002 | Line items, promotions, order totals; backed by Postgres |
| **payments** | 8003 | Simulated charge authorization |
| **loadgen** | — | Drives ~4 requests/second of baseline shopper traffic |

## Running it

```bash
cd incident_lab/app
docker compose up --build
```

First build takes a few minutes. After that, `docker compose up` is seconds.

Check it is alive:

```bash
curl localhost:8000/products | head
curl -X POST localhost:8002/carts/demo/items \
  -H 'content-type: application/json' \
  -d '{"product_id":"sku-001","quantity":2}'
curl -X POST localhost:8000/checkout/demo \
  -H 'content-type: application/json' \
  -d '{"promo_code":"SAVE10"}'
```

Watch the traffic:

```bash
docker compose logs -f gateway cart
```

Stop it, keeping the database:

```bash
docker compose down
```

Stop it and wipe the database:

```bash
docker compose down -v
```

## Design notes

**Baseline noise is deliberate.** Payments declines 2% of charges and adds
35–60ms of jitter. A perfectly flat error rate would make any injected fault
trivially detectable, which would flatter the agent's diagnostic ability.

**Every service logs the same JSON shape**, so a log collector can filter on
`service`, `level`, `status`, and `request_id` without per-service parsing.

**Totals live in one function.** `services/cart/main.py::compute_total` is the
only place pricing logic exists, which makes it a clean target for planted
pricing faults and a clean thing to assert against in ground truth.

**The pool is configurable** via `DB_POOL_MAX_SIZE` and `DB_POOL_TIMEOUT`, so
config faults can starve the cart service without touching any code.
