# Roadmap

The delivery plan for sev0, in four phases. Each phase ends with something
demonstrable, so the project is never in a state where there is nothing to show.

Estimates assume **10–15 hours per week**.

---

## Phase 1 — Incident Lab foundation

**Goal:** a real application you can break on demand, with the telemetry to
observe it breaking. No agent yet.

**Duration:** 2–3 weeks

| # | Task | Deliverable |
| --- | --- | --- |
| 1.1 | ~~Build a multi-service demo app in `incident_lab/app/`~~ **Done** | `docker compose up` serves a working storefront |
| 1.2 | ~~Add the observability stack (Loki, Prometheus, Grafana)~~ **Done** | Dashboards show live traffic |
| 1.2b | Add distributed tracing (Tempo + OpenTelemetry spans) | Deferred — see journal |
| 1.3 | ~~Write a load generator so the app has steady baseline traffic~~ **Done** | Metrics are non-flat at rest |
| 1.4 | ~~Build the fault injection interface (`inject` / `restore`)~~ **Done** | `sev0-lab inject --scenario X` breaks the app reproducibly |
| 1.5 | Author the first three code-fault scenarios with ground truth — **1 of 3** | `incident_lab/scenarios/*.yaml` with commit, file, line |
| 1.6 | ~~Build the `sev0-lab` CLI (`up`, `down`, `inject`, `restore`, `status`)~~ **Done** | One command to break and unbreak |

**Exit criterion:** you can run `sev0-lab inject --scenario checkout-5xx`, watch
the error rate climb in Grafana, and diagnose it yourself from the logs in under
ten minutes. If a human cannot solve it from the available telemetry, the agent
never will — that is the real test of this phase.

---

## Phase 2 — Evidence collection and investigation

**Goal:** the agent states a root cause. It does not fix anything yet.

**Duration:** 3–4 weeks

| # | Task | Deliverable |
| --- | --- | --- |
| 2.1 | ~~Loki log collector with incident-window filtering~~ **Done** | `collectors/logs.py` returns deduplicated log lines |
| 2.2 | ~~Prometheus metrics collector with anomaly windowing~~ **Done** | `collectors/metrics.py` identifies onset time |
| 2.3 | Tempo trace collector for the failing request path | Blocked on 1.2b |
| 2.4 | ~~Git history collector: commits, blame, diffs in window~~ **Done** | `collectors/history.py`, read-only by construction |
| 2.5 | ~~AST-aware code retrieval~~ **Done** | `retrieval/` returns whole functions, not chunks |
| 2.6 | ~~Agent loop with tool calling and persisted run state~~ **Done** | `agent/loop.py`, traces written to `runs/` |
| 2.7 | ~~Structured root-cause output with a confidence signal~~ **Done** | Typed dataclass, not free text |

**Exit criterion:** the agent names the correct file for at least one scenario,
end to end, without hints.

---

## Phase 3 — Repair, verification, and pull requests

**Goal:** the loop closes. Alert in, reviewed pull request out.

**Duration:** 3–4 weeks

| # | Task | Deliverable |
| --- | --- | --- |
| 3.1 | Docker sandbox with no network and a hard timeout | `sandbox/runner.py` |
| 3.2 | Failure reproduction: confirm the bug before patching | Hypotheses are tested, not assumed |
| 3.3 | Patch generation bounded by the diff limits | Oversized patches rejected pre-flight |
| 3.4 | Test execution and regression comparison | Before/after test results captured |
| 3.5 | Branch, commit, and pull request automation | `git_ops/`, Conventional Commit messages |
| 3.6 | Pull request body: evidence, rejected hypotheses, confidence | The reasoning is the review artifact |
| 3.7 | Enforce protected paths and human-approval gate | Safety rails tested, not just configured |

**Exit criterion:** a pull request you would actually merge, opened without you
touching the keyboard. Record the GIF for the README here.

---

## Phase 4 — Benchmark and published results

**Goal:** turn performance into a number.

**Duration:** 4–6 weeks

| # | Task | Deliverable |
| --- | --- | --- |
| 4.1 | Expand to 15–20 scenarios across code, config, and infra faults | `--suite core` |
| 4.2 | Scoring harness for the four metrics | `sev0-lab score` |
| 4.3 | Deterministic replay so runs are comparable | Seeded, versioned scenarios |
| 4.4 | Ablation studies (no traces, no git history, smaller model) | Which evidence actually matters |
| 4.5 | Markdown and HTML scorecards | `sev0-lab report` |
| 4.6 | Publish results and a write-up | Numbers in the README |

**Exit criterion:** a table of measured results, and an honest account of the
failure modes.

---

## Working conventions

**One logical change per commit.** A commit that adds a collector and also
reformats an unrelated file is two commits.

**Commit sequence for Phase 1**, as an illustration of the granularity to aim for:

```
chore: Scaffold project structure and tooling
docs: Add README, roadmap, and contributing guide
feat(lab): Add containerized demo application
feat(lab): Add Loki and Prometheus to the stack
feat(lab): Add load generator for baseline traffic
feat(lab): Add fault injection interface
feat(lab): Add checkout-5xx scenario with ground truth
test(lab): Verify inject and restore are reversible
docs(lab): Document scenario authoring format
```

Push at the end of each working session rather than batching a week into one
commit. The history should read as a record of decisions, not a single drop.

**Write down what failed.** Keep `docs/JOURNAL.md` as a running log of
approaches tried and results. It costs five minutes per session and it is the
difference between resuming work in one minute and re-deriving context for an
hour.
