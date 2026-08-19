# sev0

An autonomous AI software engineer that diagnoses production incidents, repairs them, and proves the fix before a human sees it.

## What This Is

`sev0` takes an alert and returns a named root cause: service, file, symbol, and the commit that introduced it. When it can, it also returns a patch that has been reproduced, applied, and re-run inside a sandbox before anyone is asked to look at it.

It is built to behave like an on-call engineer rather than a chat interface:

- it reads metrics and logs to find which service is affected and when it broke
- it reads the git history around that moment, not the whole repository
- it forms hypotheses and records each as confirmed, rejected, or unverified
- it runs code to test them, in an isolated container
- it proposes a patch only after reproducing the failure that patch claims to fix
- it opens a draft pull request and stops there

The repository also contains **Incident Lab**: a running microservice application, a fault injector, and a benchmark that scores sev0 against ground truth the agent cannot read.

## Stack

Python 3.11+ throughout, typed and checked under `mypy --strict`.

**Agent.** The Anthropic Messages API, driven as a hand-rolled tool-calling loop rather than an agent framework — the loop owns turn accounting, block round-tripping, and the tool-call budget, because all three are places where a framework's defaults would be wrong here. `pydantic` and `pydantic-settings` for configuration, `typer` and `rich` for the CLI, `pygit2` for history reads, `PyGithub` for pull requests, `httpx` for the Prometheus and Loki APIs, `docker` for the sandbox.

**Target application.** FastAPI services on `uvicorn`, `httpx` between them, Postgres 16 behind `psycopg` with a connection pool, `prometheus-client` for metrics. Four services, one database, one load generator, all under Docker Compose.

**Observability.** Prometheus 3.1 scrapes metrics, Grafana Alloy ships logs to Loki 3.3, Grafana 11.4 renders both. Alert rules are committed as YAML and are themselves a fault surface.

**Tooling.** `pytest` for tests, `ruff` for lint and import order at 100 columns, `mypy` in strict mode over `src/` and `incident_lab/` — the storefront is excluded, since it is a separate deployable that fault injection deliberately breaks. `hatchling` builds. CI runs the whole thing on 3.11 and 3.12.

## Repository Layout

```
src/sev0/
  agent/          investigation loop, toolbox, capabilities, baseline, run state
  collectors/     Prometheus and Loki clients, onset detection, log shaping
  retrieval/      symbol-level code reading over the target repository
  sandbox/        Docker runner, patch construction, validation, verification
  git_ops/        branch and push handling, pull request rendering
  config.py       every setting and safety limit, in one place
  pricing.py      per-model token pricing, or None where none is published

incident_lab/
  app/            the storefront: services, config, alert rules, compose stack
  scenarios/      23 fault definitions plus the model that applies them
  target.py       materialises the target repo and its synthetic history
  benchmark.py    trial sequencing, aggregation, reporting
  scoring.py      per-component scoring against ground truth
  cli.py          sev0-lab, including the injection ledger

docs/EVALUATION.md   protocol, metric definitions, and limitations
```

## How It Works

Four stages, in order.

**Collect.** Prometheus and Loki are queried for error rate, request rate, p95 latency, and failing log lines deduplicated by shape. `find_onset` locates the moment the error rate left its baseline and stayed there, which bounds which commits are worth reading at all.

**Retrieve.** Git history is read around the onset window — recent commits, commits touching a path, individual diffs, blame. Code is read by symbol rather than by file, so a 2,000-line module costs the function that matters and not the other 1,900 lines.

**Experiment.** Hypotheses are tested by execution, inside a container with the network switched off. Each is recorded as confirmed, rejected, or unverified, and the trace keeps all of them including the ones that were wrong.

**Repair.** A candidate patch is applied to a throwaway copy of the repository, never the working tree. The suite runs before and after. If the failure reproduced, the patch fixed it, and nothing new broke, the fix is marked verified and a draft pull request is opened.

The pull request is always a draft, and there is no code path that merges it.

## What The Loop Guarantees

Four properties hold on every run, enforced in code rather than asked for in the prompt.

**A hypothesis is executed or marked unverified.** `run_snippet` executes Python against the repository under investigation and `run_tests` runs the real suite. Anything the agent could not test is recorded as unverified and carries that label into the trace and the pull request body.

**The failure is reproduced before the patch is applied.** `try_patch` runs the suite first. If it is already green, the patch is rejected with `the suite is already green, so this failure is not the one in production`. Tests passing afterwards is not sufficient — the failure has to have been present first.

**Regressions are reported, not absorbed.** Verification compares the failing set before and after and returns the tests it fixed and the tests it newly broke, separately. A patch that fixes one test and breaks two is reported as exactly that.

**Limits are checked before execution, not after.** Patch size, file count, and protected paths are validated up front. A patch that violates a limit never runs, so there is no result to weigh against the rule.

## The Sixteen Tools

| Group | Tools |
| --- | --- |
| Observability | `metrics_overview`, `find_onset`, `failure_logs`, `service_logs` |
| History | `recent_commits`, `commits_touching`, `show_commit`, `blame` |
| Retrieval | `search_code`, `file_outline`, `read_symbol` |
| Execution | `run_tests`, `run_snippet`, `try_patch` |
| Reasoning | `record_hypothesis`, `conclude` |

The groups matter beyond documentation. Each is a capability that can be withheld as an ablation, which is how the benchmark measures what any one of them is worth.

## Incident Lab

Benchmarking an incident responder needs incidents. Incident Lab is a real four-service storefront — gateway, cart, catalog, payments — behind Postgres, with a load generator, Prometheus, Loki, Grafana, and committed alert rules. It runs under Docker Compose and produces genuine telemetry under genuine load.

The target is materialised as a git repository with a synthetic but plausible history, built one real diff at a time, so `git log -p` and `git bisect` behave the way they would on a project someone actually wrote.

### Scenarios

Twenty-three scenarios across three fault families:

- **code** (16) — logic errors, boundary conditions, mutation of shared state, ordering bugs, error mapping
- **config** (5) — a tunable changed in a committed config file: a timeout too low, a pool too small, a feature flag flipped off
- **infra** (2) — alert rules and deployment configuration

Faults are expressed as exact find-and-replace anchors rather than diffs. Line numbers rot silently against an evolving target; an anchor that no longer matches fails loudly, and the test suite injects every scenario on every CI run to prove they all still bite.

Four scenarios are adversarial by design:

- a decoy commit that lands *after* the real fault and looks more suspicious than it
- log noise that names the wrong service
- a plausible-looking limit that is not the cause, sitting next to the one that is
- a fault in a protected path, which the agent may diagnose but must not patch

Four more raise no exception at all. They are visible only in the business metrics — orders that stopped, order value that moved — because an incident that throws a 500 is the easy case.

### Ground Truth Isolation

The agent cannot reach the answer key, and this is enforced rather than assumed:

- ground truth lives in scenario YAML under `incident_lab/`, which is not the repository the agent is given
- `sev0-lab reveal` is the only command that prints it, and nothing the agent can invoke calls it
- a test asserts that no scenario's alert name, log output, or injected diff contains its own ground-truth symbol

That test has already earned its place. One scenario named its ground-truth symbol `checkout` while firing an alert called `checkout-5xx`, handing the agent the answer in its first tool call. It was caught here rather than in a result.

### Scoring

Runs are matched to faults through an append-only injection ledger recording what was live when each run started. Matching by alert name was tried first and was wrong: several scenarios fire `checkout-5xx` by design, and a correct run was scored as wrong because of it. The alert fallback was removed rather than kept as a guess.

Diagnosis and repair are scored separately and never blended into one number. Root-cause accuracy is reported per component — file, symbol, commit — because a run that names the right file and the wrong function is not half correct in any way that matters operationally.

## Evaluation

`docs/EVALUATION.md` documents the full protocol. The short version:

**Modes.** `full`, three ablations — `no-execution`, `no-history`, `no-retrieval` — and `baseline-static`. Ablations are implemented as capability gating in a single place, not as forked agents, so there is one auditable answer to what a mode removed. The matching section of the system prompt is removed alongside the tools; instructing a model to run experiments it has no tools for measures how many turns it wastes finding that out.

**Baseline.** `baseline-static` receives the same evidence from the same collectors and gets exactly one forced call to `conclude`. Evidence gathering is cleared from its tool-call count first, so the comparison is not flattered by investigation it never did.

**Safety is not ablatable.** No mode relaxes the sandbox, the patch limits, the protected paths, or the draft-only pull request. A comparison against a more dangerous system is not a comparison of the system that ships.

**Statistics.** Every rate carries its denominator. An empty slice reports `n/a` rather than `0%`. A trial that crashed is missing data, not a wrong answer, and is excluded from the accuracy denominator — folding it in would make an infrastructure problem read as a model problem. p95 is withheld below twenty samples, where it is a maximum wearing a percentile's name. Cost is derived only for models with a published price and left blank otherwise.

**The benchmark has not been run.** The infrastructure is implemented and tested; no results are published, because producing them costs real money and inventing them would defeat the purpose of building any of this.

## Safety

Enforced in code, not in the prompt:

- **Sandboxed execution.** `--network=none`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, memory and PID caps, and a hard wall-clock timeout. Containers are `--rm`.
- **Throwaway copies.** Verification runs against a scratch copy. The working tree is never patched to find out whether a patch works.
- **Patch limits.** Five files and 120 lines by default. Violations are rejected before anything executes.
- **Protected paths.** `migrations/`, `infra/`, and `.github/` are refused. The agent may diagnose a fault in them and may not fix it.
- **Escape rejection.** Absolute paths and `..` traversal are rejected outright.
- **Branch restriction.** Pushes are refused unless the branch carries the sev0 prefix, and refused outright to the base branch.
- **Credential hygiene.** Push URLs are built per call and never written to `.git/config`. Git's stderr is redacted before display, because a failed push echoes the URL it was handed.
- **Human review.** Pull requests are drafts. Nothing merges automatically, and there is no flag that changes this.

`--local-sandbox` exists for machines without Docker and is not isolated. It runs subprocesses on your filesystem with your network. The tool prints that every time it is used.

## Requirements

- Python 3.11 or 3.12 — both are covered by CI
- Docker and Docker Compose — required for the lab and for isolated execution
- An Anthropic API key
- A GitHub fine-grained token, if you want pull requests

## Setup

```bash
git clone https://github.com/Amirjon06/sev0.git
cd sev0
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Put your key in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
SEV0_MODEL=claude-sonnet-5
```

Then confirm the environment is sound:

```bash
sev0 doctor
```

`doctor` reports the sandbox runtime, whether the key is set, the model, the target repository, and which observability endpoints are configured. Fix anything it flags before running the agent — each of those is a failure that would otherwise surface halfway through a run.

## Quick Start

Bring up the lab, break it, and let the agent work it out:

```bash
sev0-lab up
sev0-lab inject --scenario checkout-promo-none
sev0 investigate --incident checkout-5xx
```

`inject` does not say what it changed. Score the run afterwards, then read the answer key:

```bash
sev0-lab score --run <run-id>
sev0-lab reveal --scenario checkout-promo-none
```

Then put the storefront back:

```bash
sev0-lab restore
```

Each run writes a full trace to `runs/<run-id>/run.json`: every tool call, every hypothesis and its verdict, every experiment, token usage, and the conclusion. The trace is the artifact; the terminal output is a summary of it.

## Repairing And Opening A Pull Request

By default `investigate` is a dry run and stops at diagnosis. If it produced a verified fix it says so, and you re-run with `--no-dry-run` to open the pull request.

The target repository needs somewhere to go first. It is scratch — `up --fresh` rebuilds it — so publishing it is what gives a verified fix a branch a reviewer can actually open:

```bash
sev0-lab publish --repo owner/sev0-target
sev0 investigate --incident checkout-5xx --no-dry-run
```

The pull request body carries the root cause, the diff, the verification result — tests fixed and tests newly broken, separately — and a link to the trace. It opens as a draft.

## Running The Benchmark

Always start with the plan:

```bash
sev0-lab benchmark --dry-run
```

This prints every scenario, family, alert, and mode, plus the total trial count. Nothing is injected and no model is called. Read it before spending anything.

A real run:

```bash
sev0-lab benchmark --runs 3 --mode full,no-execution --output results/bench.json
```

Each trial restores the storefront before injecting and again afterwards, so a scenario that fails to clean up cannot silently become part of the next one's measurement. A trial that crashes is recorded and the suite continues. Results serialise to JSON so a published number can be rechecked by someone who did not run it.

Scope the run while you are still calibrating:

```bash
sev0-lab benchmark --family config --runs 1
sev0-lab benchmark --scenario tax-truncation,retries-disabled
```

## Everyday Commands

```bash
sev0 version
sev0 doctor
sev0 investigate --incident <alert> [--mode MODE] [--no-dry-run] [--local-sandbox]

sev0-lab up [--fresh] [--no-build]
sev0-lab down [--volumes]
sev0-lab status
sev0-lab list
sev0-lab inject --scenario <id>
sev0-lab restore
sev0-lab publish --repo owner/name
sev0-lab benchmark [--dry-run] [--runs N] [--mode ...] [--family ...]
sev0-lab score --run <run-id>
sev0-lab report [--output PATH]
sev0-lab reveal --scenario <id>
```

What the less obvious ones do:

- `doctor`: checks the sandbox runtime, the API key, the model, and the configured endpoints
- `up --fresh`: discards and recreates the target repository, including its synthetic history
- `status`: shows whether the tree is at baseline or faulted, and what is currently injected
- `inject`: breaks the storefront without saying how, and rebuilds the affected services
- `restore`: undoes whatever is injected and returns the storefront to health
- `publish`: force-pushes the scratch target to GitHub so pull requests have a destination
- `score`: scores one run against ground truth, inferred from the injection ledger
- `report`: aggregates every run under `runs/` into a scorecard
- `reveal`: prints the answer key — never call it from anything the agent can read

## Configuration

Everything is read from `.env` or the environment. Settings take the `SEV0_` prefix; the two credentials do not.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required. Checked before a run starts, not on the first request. |
| `GITHUB_TOKEN` | — | Required only for pull requests. |
| `SEV0_MODEL` | `claude-sonnet-5` | Haiku is roughly a tenth the cost and useful for shaking out prompt problems first. |
| `SEV0_REPO` | — | Where pull requests are opened. The target, not this repository. |
| `SEV0_TARGET_REPO` | `./runs/target` | The repository under investigation. |
| `SEV0_PROMETHEUS_URL` | — | Metrics source. |
| `SEV0_LOKI_URL` | — | Log source. |
| `SEV0_SANDBOX_RUNTIME` | `docker` | Execution runtime. |
| `SEV0_SANDBOX_NETWORK` | `none` | Sandbox network mode. |
| `SEV0_SANDBOX_TIMEOUT_SECONDS` | `600` | Hard wall clock on any sandboxed command. |
| `SEV0_MAX_FILES_CHANGED` | `5` | Patch limit. |
| `SEV0_MAX_LINES_CHANGED` | `120` | Patch limit. |
| `SEV0_MAX_TOOL_CALLS` | `60` | Per-run budget. |
| `SEV0_PROTECTED_PATHS` | `migrations/,infra/,.github/` | Paths the agent may never modify. |

The default target is the Incident Lab scratch copy rather than this project, so a misconfigured run reads a throwaway repository instead of real source.

## Project Status

Alpha, and honest about it.

Working: the investigation loop, all sixteen tools, the sandbox, patch verification, draft pull requests, twenty-three scenarios with fault injection and restore, the scoring pipeline, ablation and baseline modes, and the benchmark runner.

Not done: the benchmark has not been run, so there are no published accuracy or resolution numbers. Distributed tracing is deliberately deferred — the collectors are metrics and logs. The evaluation covers one target application, which bounds what any result from it would generalise to. `docs/EVALUATION.md` says all of this at greater length.

## Troubleshooting

### `No API key`

The key is missing from `.env`, or still holds the `sk-ant-...` placeholder. `sev0 doctor` shows its last four characters when it is set correctly.

### `Docker is unavailable`

Start Docker, or pass `--local-sandbox`. Local execution is not isolated — it runs on your filesystem with your network — and its results are not comparable to a sandboxed run.

### `No repository at runs/target`

Run `sev0-lab up`, which materialises the target repository and starts the stack.

### `A fault is already injected`

Run `sev0-lab restore` first. Two faults at once produce a run nothing can score, because the ledger records one scenario as live.

### The run scored as wrong but the answer looks right

Check `sev0-lab reveal --scenario <id>`. Scoring is exact on file and symbol. A run that names the calling function instead of the faulting one is a real miss — that distinction is the difference between an on-call engineer reading the right function and reading its caller.

### The suite is slow

`tests/test_benchmark_scenarios.py` injects all twenty-three scenarios into a scratch target and runs the storefront suite against each one. It is the slowest file by a wide margin, and it is the one that proves the scenarios still break what they claim to break.

## Notes

- Faults are anchors, not diffs, so a scenario that no longer applies fails loudly instead of injecting nothing.
- The injection ledger is append-only. Runs are matched to faults by start time, never by alert name.
- Ablations are capability gating in one place. There is no forked agent per mode, because the moment there is, a result can no longer be attributed to what the mode removed.
- Thinking blocks are round-tripped whole rather than field by field. They carry a signature the API verifies on return, and a hand-written converter drops what it does not know about.
- Verification always uses a scratch copy. Nothing in the repair path writes to the working tree.
- `runs/` is scratch and is not tracked.

## Contributing

See `CONTRIBUTING.md`. In short: `ruff check .`, `mypy`, and `pytest` all pass before a pull request; new scenarios come with ground truth and a solvability note; and nothing that weakens a safety rail lands without a reason that survives review.

## License

MIT. See `LICENSE`.
