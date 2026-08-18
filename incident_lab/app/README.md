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

## Observability

| Component | Port | Purpose |
| --- | --- | --- |
| **Grafana** | 3000 | Dashboards; anonymous access, no login needed |
| **Prometheus** | 9090 | Scrapes `/metrics` from every service every 5s |
| **Loki** | 3100 | Log storage, queryable by service, level, and status |
| **Alloy** | 12345 | Ships container logs into Loki |

Open [localhost:3000](http://localhost:3000) and the **Storefront** dashboard is
already there: request rate, 5xx share, p95 latency, checkout outcomes, and a
live error log.

Useful queries once you are hunting something:

```logql
{service="cart", level="WARNING"}
{service="gateway"} |= "checkout" | json | status >= 500
```

```promql
sum by (service) (rate(http_requests_total{status=~"5.."}[1m]))
histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))
```

## Running it

Use the `sev0-lab` CLI rather than `docker compose` directly. The services build
from a target repository under `runs/target`, which the CLI creates on first
use, so a bare `docker compose up` has nothing to build from.

```bash
sev0-lab up          # materialise the target repo, then start everything
sev0-lab status      # what is running, and whether a fault is injected
sev0-lab down        # stop; add --volumes to wipe the database
```

First build takes a few minutes. After that, `sev0-lab up` is seconds.

## Breaking it

```bash
sev0-lab list                                # available scenarios
sev0-lab inject --scenario checkout-promo-none
sev0-lab restore                             # back to health
```

`inject` commits the fault into the target repository as a real change with a
plausible message and author, then rebuilds only the affected services. It
deliberately tells you nothing about what it changed. When you want the answer
key:

```bash
sev0-lab reveal --scenario checkout-promo-none
```

Never wire `reveal` into anything the agent can read. It is the scoring key.

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

**Metrics are labelled by route template**, not by concrete path. Labelling
`/carts/user-0042` instead of `/carts/{user_id}` would create a new time series
per shopper and take Prometheus down before the benchmark suite finished.

**Uvicorn's own access log is off.** The services already emit a richer
structured line per request, and two log lines per request doubled the volume
Loki had to store for no added information.
