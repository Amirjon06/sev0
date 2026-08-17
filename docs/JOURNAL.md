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
