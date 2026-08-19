# sev0

An autonomous software engineer that investigates production incidents, reproduces the failure, repairs it, and proves the repair before asking a human to review it.

[![CI](https://github.com/Amirjon06/sev0/actions/workflows/ci.yml/badge.svg)](https://github.com/Amirjon06/sev0/actions/workflows/ci.yml)

## What This Is

`sev0` takes an incident identifier and works out what broke, without being told.

It reads the same things an on-call engineer would:

- metrics, to find when the error rate left its baseline
- logs from the failing service around that window
- Git history for commits landing just before it
- the source of the functions those commits touched

Then it does the part that most tools skip: it runs code. It calls the suspect
function with the suspect input inside a sandbox, runs the target's test suite,
and finds out whether the failure actually reproduces. A hypothesis that does not
reproduce is recorded as rejected and the investigation moves on.

When it has a cause, it writes a patch and submits it to verification. The
failure has to reproduce first, then the patch is applied to a throwaway copy and
the suite is re-run. A patch that fixes the failing test but breaks another is
reported as not verified and goes no further. A patch that survives becomes a
draft pull request carrying the root cause, the rejected hypotheses, the
verification output, and the diff.

sev0 opens pull requests. It does not merge them.

## Why It Is Not An LLM Wrapper

Two things make the difference.

**Hypotheses are executed, not asserted.** The agent has `run_snippet`,
`run_tests`, and `try_patch`. Every run records how many tool calls actually
executed code, and the scorecard reports runs that concluded without executing
anything as their own number, because a confident answer reached purely by
reading is the failure mode worth catching.

**Verification is deterministic and sits outside the model.** Whether a patch
counts as a fix is decided by reproducing the failure and re-running the suite,
not by the model saying so. There is no flag to skip it.

## How It Works

```
incident → collect evidence → inspect history and code → form hypothesis
         → test by execution → root cause → bounded patch
         → reproduce and verify → draft pull request
```

The loop is iterative. A rejected hypothesis feeds back into the next round.

The agent works through 16 tools:

- **Observability** — `metrics_overview`, `find_onset`, `failure_logs`, `service_logs`, backed by Prometheus and Loki
- **History** — `recent_commits`, `commits_touching`, `show_commit`, `blame`, read-only by construction
- **Code** — `search_code`, `file_outline`, `read_symbol`, using the stdlib `ast`
- **Experiments** — `run_tests`, `run_snippet`, `try_patch`, all sandboxed
- **Reasoning** — `record_hypothesis`, `conclude`

Two details that matter more than they look:

- `find_onset` requires a threshold breach to persist across several samples
  before it reports a start time. A single spike is a restart or one unlucky
  request. Log lines are deduplicated by shape before the model sees them, since
  a minute of traffic is thousands of near-identical lines.
- Code retrieval returns whole definitions, decorators included. A fixed window
  of lines can cut a function in half, and half a function is worse than none —
  the model sees a branch without the guard above it and explains a bug that is
  not there.

Every run writes a complete record to `runs/<run-id>/run.json`: every tool call,
every hypothesis, every rejection, and the verification result.

## The Target Repository

sev0 never investigates itself. It operates on a separate **target repository**,
set by `SEV0_TARGET_REPO` and defaulting to `./runs/target`, which Incident Lab
creates. Pull requests go to the GitHub repository named by `SEV0_REPO`, which
should be that target's remote.

Keeping them apart is what lets the lab plant faults in a real Git history
without touching the tool doing the investigating.

## Incident Lab

Incident Lab is what makes the results checkable instead of anecdotal.

It runs a containerised storefront — gateway, catalog, cart, payments, Postgres —
with a load generator and a Loki/Prometheus/Grafana stack. It injects a hidden
fault and hands sev0 nothing but an alert name.

Each scenario is a YAML file in `incident_lab/scenarios/`. The fault is described
as exact find-and-replace anchors rather than a diff, because line numbers rot
silently as source moves while an anchor that no longer matches fails loudly.
Injecting commits the change under a plausible author and message, so the history
is a haystack rather than one obviously suspicious commit on top of scaffolding.

Ground truth — service, file, symbol — lives in the same file, behind
`sev0-lab reveal`. Nothing an investigation can read ever calls it.

There are 23 scenarios across three fault families: code faults that crash,
code faults that quietly return a wrong number, config faults where the code is
correct and the deployment is not, and dependency faults where the failing
service and the alerting service are different. Six are adversarial, each built
to defeat one specific shortcut — a decoy commit larger and newer than the real
fault, a warning that drowns the causal evidence, a first explanation that fits
everything and reproduces nothing, a patch that fixes its test and breaks three
others, and a tempting edit inside a protected path.

Runs are scored on four things, kept separate on purpose:

- **root-cause accuracy** — correct file *and* symbol; the commit is reported but not required
- **time to diagnosis** — wall-clock seconds from run start to stated root cause
- **resolution** — did the patch survive verification
- **unsafe attempts** — edits touching protected paths or exceeding diff limits

An agent can name the right function and still ship a change that breaks
something else. One combined number would hide exactly that.

Nine scenarios fire the *same* alert with different root causes, so the
benchmark measures diagnosis rather than pattern matching against the symptom.
A test asserts scenarios sharing an alert stay indistinguishable from outside,
and further tests assert that no scenario leaks its answer through its id, its
alert, or its commit messages.

Full methodology, including how each scenario is validated and what the
benchmark cannot tell you, is in [docs/EVALUATION.md](docs/EVALUATION.md).

## Safety

The limits are enforced outside the model and cannot be reached from inside a run.

- Generated code runs in a container with `--network=none`, `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, memory and PID caps, and a hard timeout.
  `--local-sandbox` bypasses all of that for machines without Docker and is not a
  security boundary.
- Experiments and verification work on a throwaway copy. The repository under
  investigation is never modified by a run.
- Patch limits (file count, changed lines, protected paths) are checked before
  anything executes and again inside `apply()` — between a check and a write the
  tree can move.
- sev0 will not commit to the base branch, work on a dirty tree, push a branch
  outside the `sev0/` namespace, or merge anything. A failed commit deletes its
  own branch.

## Requirements

- Linux or macOS
- Python 3.11 or 3.12
- Docker 24.0+
- Git 2.40+
- 8 GB free RAM — Incident Lab runs ten containers
- An Anthropic API key

A GitHub token is needed only if you want pull requests opened.

## Setup

```bash
git clone https://github.com/Amirjon06/sev0.git
cd sev0

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env
```

Put your key in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Then check it:

```bash
sev0 doctor
```

That prints every resolved setting and flags a missing API key. If it renders a
table, you are ready.

## Quick Start

Start the lab and break something:

```bash
sev0-lab up
sev0-lab list
sev0-lab inject --scenario checkout-promo-none
```

Roughly one checkout in seven now fails. Give the error rate a couple of minutes
to establish — you can watch it at http://localhost:3000.

Investigate:

```bash
sev0 investigate --incident checkout-5xx
```

This diagnoses, patches, and verifies, but opens no pull request. It prints a run
id. Score that run against the answer key it never had access to:

```bash
sev0-lab score --run <run-id>
sev0-lab report
```

Put the storefront back:

```bash
sev0-lab restore
sev0-lab down
```

## Running The Benchmark

One scenario tells you very little. The benchmark runs the suite, restoring
between every scenario so a failure cannot contaminate the next measurement.

```bash
sev0-lab benchmark --dry-run
```

That prints the plan and calls no model, which is how you size an invocation
before paying for one. Then:

```bash
sev0-lab benchmark                                  # whole suite, one trial each
sev0-lab benchmark --family config                  # one fault family
sev0-lab benchmark --runs 3                         # three trials per scenario
sev0-lab benchmark --mode full,no-execution         # compare against an ablation
sev0-lab benchmark --mode baseline-static           # compare against no loop at all
```

Results land in `runs/benchmarks/` as JSON and as a Markdown report. Every rate
carries its counts, and each trial records the model, the mode, the sev0
revision, token usage and cost so a number can be traced back to the run that
produced it.

### Baselines and ablations

`baseline-static` gives the same model the same evidence — assembled by the same
collectors — in a single call, with no iteration and no execution. The gap it
leaves is the investigation loop, which is the thing worth measuring.

The ablations remove one component each: `no-execution`, `no-history`,
`no-retrieval`. They are capability gating in one place, not a second copy of
the agent, and none of them weakens the safety rails.

## Opening A Pull Request

The target repository is scratch and has no remote, so publish it once:

```bash
sev0-lab publish
```

That force-pushes the target, including the injected fault, to `SEV0_REPO`. It
needs `GITHUB_TOKEN` set to a token with `Contents: read/write` and
`Pull requests: read/write` on that repository.

Then run without the dry run:

```bash
sev0 investigate --incident checkout-5xx --no-dry-run
```

If the fix verifies, sev0 branches, commits, pushes, and opens a draft pull
request. If it does not verify, nothing is proposed.

## Everyday Commands

```bash
sev0 doctor
sev0 investigate --incident <id> [--alert TEXT] [--no-dry-run] [--local-sandbox] [--mode MODE]

sev0-lab up [--fresh]
sev0-lab down [--volumes]
sev0-lab list
sev0-lab status
sev0-lab inject --scenario <id>
sev0-lab restore
sev0-lab publish [--repo owner/name]
sev0-lab score --run <run-id> [--scenario <id>]
sev0-lab report [--scenario <id>] [--output PATH]
sev0-lab benchmark [--scenario ids] [--family f] [--mode modes] [--runs N] [--dry-run]
sev0-lab reveal --scenario <id>
```

What the less obvious ones do:

- `up --fresh` discards and recreates the target repository before starting
- `status` shows what is injected, whether the tree is at baseline, and which containers are running
- `publish` mirrors the local target repository to GitHub so a fix branch has somewhere to go
- `report` aggregates every scored run into `runs/scorecard.md`
- `benchmark` runs the suite across modes and repeated trials
- `reveal` prints the answer key. Never call it from anything the agent can read

## Configuration

Everything is read from environment variables or a local `.env` file. The full
set is in `.env.example`; the fields are defined in `src/sev0/config.py`.

The ones you are most likely to change:

- `SEV0_MODEL` — model backing the investigation loop, default `claude-sonnet-5`
- `SEV0_TARGET_REPO` — repository under investigation, default `./runs/target`
- `SEV0_REPO` — pull request destination, as `owner/name`
- `SEV0_MAX_FILES_CHANGED` / `SEV0_MAX_LINES_CHANGED` — hard caps on patch size
- `SEV0_MAX_TOOL_CALLS` — budget per investigation, default 60
- `SEV0_PROTECTED_PATHS` — paths the agent may never edit

Never commit `.env`.

## Project Status

All four planned phases have working, tested code: 484 tests, ruff clean, mypy
strict. The full pipeline has run end to end against a live model — alert to root
cause to verified patch to draft pull request — and the evaluation harness around
it is complete: 23 scenarios, a reproducible runner with repeated trials, one
baseline, and three ablations.

**The benchmark has not been run.** The suite, the runner, the baseline and the
ablations are implemented and tested, and no aggregate result exists because
running 23 scenarios across modes and repeated trials costs real money and has
not been done. There are no numbers on this page for that reason, and there
will not be until they come from actual runs.

What has been measured is four single runs from before the suite existed: they
diagnosed correctly and one produced a verified fix and a draft pull request.
That is a demonstration that the pipeline works end to end. It is not a
characterisation of anything.

Known gaps:

- **No distributed tracing.** There is no Tempo collector and no OpenTelemetry
  instrumentation, so no scenario can require following a request across a
  service boundary by trace id. Two scenarios approximate that shape with
  per-service metrics, which is a weaker signal. This is unrelated to the
  per-run execution records under `runs/`, which are fully implemented and are a
  different thing with a confusingly similar name.
- **One target application, and faults that were authored rather than
  observed.** Everything is one Python microservice storefront, and the bugs
  were written by the same person who built the agent. Real ones are stranger.
- **No held-out set.** The prompt was iterated while these scenarios existed,
  so any result describes performance on problems the author had seen.
- **Confidence is unvalidated.** The agent reports a confidence level with every
  conclusion. Whether it correlates with being right has not been measured.

Detailed future work is in [docs/ROADMAP.md](docs/ROADMAP.md). The reasoning
behind each design decision, including the ones that turned out to be wrong, is
in [docs/JOURNAL.md](docs/JOURNAL.md).

## Troubleshooting

### `sev0 doctor` says the API key is missing

The key goes in `.env`, not in the shell. `.env` is read at startup and
`ANTHROPIC_API_KEY` is loaded into the process environment from there.

### A fault is already injected

`inject` refuses to stack faults. Run `sev0-lab restore` first.

### Docker is unavailable

Experiments need a sandbox. `sev0 investigate --local-sandbox` will run them
directly on your machine instead, which is fine for development and is not
isolation — read anything the model produced before trusting it.

### The run says no verified fix

That is the intended outcome when a patch could not be proved. Check
`runs/<run-id>/run.json` for what was attempted and what verification reported.

### Scoring picks the wrong scenario

Runs are matched to whichever fault was live when the run started, using the
injection ledger at `runs/injections.jsonl`. Runs predating that ledger need
`--scenario` passed explicitly.

## Notes

- The load generator sends a differently-capitalised shipping speed some of the
  time, because an older front end would. A code path no real request exercises
  produces no symptom when it breaks.
- Payments declines 2% of charges and jitters latency on purpose. A flat baseline
  would make any injected fault trivially detectable and would flatter the agent.
- Faults are deliberately partial. A total outage would be trivial to find.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers branch naming, the Conventional Commits
format, and what a reviewable pull request looks like. Open an issue before
starting significant work.

## License

MIT. See [LICENSE](LICENSE).
