# Engineering journal

A running log of what was tried, what happened, and what was decided. Newest
entries at the top. Keep entries short — three lines is fine.

Format:

```
## YYYY-MM-DD — short title

**Tried:** what you attempted
**Result:** what actually happened
**Decided:** what changes as a result
```

---

## 2026-08-17 — Observability stack in

**Tried:** Loki, Alloy, Prometheus, and Grafana alongside the storefront, with
prometheus_client middleware on every service.
**Result:** Working. Route labels use the FastAPI template, not the concrete
path — labelling /carts/user-0042 would have made one time series per shopper.
Turned uvicorn's access log off; the structured line already carries more.
**Decided:** Tracing (Tempo + OTel spans) is deferred to 1.2b. Logs and metrics
are enough to diagnose the first fault families, and spans are a bigger lift
than they are worth before the agent exists to consume them.

## 2026-08-17 — Storefront verified end to end

**Tried:** Ran the full stack under Compose and drove a real checkout.
**Result:** Working, but cart crash-looped on startup. FastAPI rejects a 204
route whose handler declares a return type. Symptom presented as gateway 500s,
not as a cart failure — the failing service and the alerting service were
different, which is the whole premise of this project showing up on day one.
**Decided:** Next is task 1.2, the observability stack. Raw `docker compose
logs` is unreadable at 4 rps; the agent needs queryable telemetry, and so do I.

## 2026-08-17 — Demo storefront running

**Tried:** Built the Phase 1 target application: four Python services (gateway,
catalog, cart, payments) on Postgres, plus a load generator.
**Result:** Services import and compute correctly; pricing covered by tests.
Docker Compose brings the whole stack up with one command.
**Decided:** Payments declines 2% of charges and jitters latency on purpose. A
flat baseline would make any injected fault trivially detectable and would
flatter the agent's diagnostic ability. Pricing logic is concentrated in
`compute_total` so planted faults have one clean home and one clean assertion.

## 2026-08-17 — Project scaffolded

**Tried:** Established the repository structure, tooling, and Phase 1 plan.
**Result:** Package installs, CLI runs, CI configured, roadmap written.
**Decided:** Phase 1 targets a human-solvable incident before any agent work
begins. If a person cannot diagnose the scenario from the available telemetry,
the scenario is broken, not the agent.
