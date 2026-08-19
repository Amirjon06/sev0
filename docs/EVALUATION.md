# Evaluation methodology

How Incident Lab is built, what it measures, and what it cannot tell you.

The short version: sev0 is scored against faults it was not told about, in a
repository it cannot read the answer from, on four metrics that are kept apart
on purpose. Everything below is the detail of how that is arranged and where it
falls short.

## Contents

- [Why a benchmark at all](#why-a-benchmark-at-all)
- [Scenario construction](#scenario-construction)
- [Fault families](#fault-families)
- [Adversarial scenarios](#adversarial-scenarios)
- [Ground-truth isolation](#ground-truth-isolation)
- [Human solvability](#human-solvability)
- [Metrics](#metrics)
- [Baselines](#baselines)
- [Ablations](#ablations)
- [Repeated trials](#repeated-trials)
- [Running the benchmark](#running-the-benchmark)
- [Reproducibility](#reproducibility)
- [Limitations](#limitations)

## Why a benchmark at all

An agent that diagnoses one planted bug proves nothing. The bug was chosen by
the same person who built the agent, the answer was known before the run
started, and there is no way to tell a real diagnosis from a lucky guess after
the fact.

Incident Lab exists so that the claim "sev0 found the root cause" has a
falsifiable meaning: a fault was injected without the agent being told what or
where, ground truth was recorded before the run, and the comparison happens
afterwards against a file the investigation had no access to.

## Scenario construction

A scenario is a YAML file in `incident_lab/scenarios/`. It declares:

- **what changes** — one or more commits, each with an author, a message, and a
  set of edits
- **how the change is expressed** — exact find-and-replace anchors, not diffs
- **what to rebuild** — which containers need restarting for the change to take
  effect
- **what should break** — the storefront tests the fault is expected to fail
- **the ground truth** — service, file, symbol, and an explanation
- **solvability notes** — the signal, the intended diagnostic path, and how to
  reproduce it

Anchors rather than diffs, because a diff carries line numbers and rots
silently as the source moves. An anchor that no longer matches fails loudly,
and a test asserts every anchor in the suite still matches its file exactly
once.

Faults land in `runs/target`, a separate repository materialised from the
storefront source with a staged history, never in the sev0 repository itself.
Each declared commit is applied and committed in order under its own author, so
`git log`, `git blame` and `git bisect` behave the way they would on a project
that was written over time rather than generated in one go.

### Validation

`tests/test_benchmark_scenarios.py` asserts, for every scenario:

- the ground-truth file is one the scenario actually edits
- the ground-truth symbol exists in the pristine source
- injection changes something, and restore returns the tree byte-for-byte
- the tests it names pass before injection and fail after
- a code fault breaks at most three test functions — a fault that breaks half
  the suite is a broken build, not a regression worth diagnosing
- a config fault leaves the application suite **green**, which is the defining
  property of that family
- a regression trap breaks at least three, which is what makes it a trap

A scenario that cannot satisfy these is fixed or removed. A smaller trustworthy
suite beats a larger unreliable one.

## Fault families

**Code faults** change application logic. Some crash — an unguarded `None`, a
dict subscript where a defaulted lookup belonged. Others do not: a boundary
comparison off by one, truncation where rounding belonged, an argument passed
before a discount rather than after. The silent ones matter most, because a
suite made only of tracebacks measures reading tracebacks.

**Config faults** change a value in `config/storefront.env`, which is committed
to the target repository and read by the services at boot. The application code
is correct and its tests pass; the deployment is wrong. Keeping tunables in the
repository rather than in the compose file is what makes this family solvable
at all — a setting that can change without leaving a trace in history is a
setting nobody can debug.

**Infrastructure faults** degrade a dependency: latency raised to the point of
timeouts, or a transient error rate raised past what retries absorb. The
failing service and the alerting service are different, which is the case sev0
was built for.

## Adversarial scenarios

Each of these defeats one specific naive strategy.

**Decoy commit** (`decoy-newest-commit`) — the real fault is a one-line change,
followed immediately by a larger commit marked as breaking that touches more
lines and changes no behaviour. Defeats *newest commit is the cause*.

**Misleading logs** (`decoy-noisy-logs`) — a deprecation warning fires at
WARNING on every request, including successful ones, drowning the causal
evidence. Defeats *the loudest log line explains the failure*. A line that
appears on requests that succeeded cannot explain a failure rate.

**Same alert, different causes** — nine scenarios fire `checkout-5xx` and four
fire `checkout-4xx`, with different root causes. Defeats *alert X means bug Y*.
A test asserts scenarios sharing an alert stay indistinguishable from outside.

**Plausible but wrong** (`trap-plausible-charge-limit`) — a clamp is inverted,
so order totals balloon and payments refuses them. Every symptom fits "the
charge ceiling is too low", which is wrong: the ceiling never changed and
raising it in an experiment lets wrong totals through rather than restoring
right ones. Defeats *the first story that fits the evidence*.

**Regression trap** (`trap-shipping-always-free`) — shipping returns zero
unconditionally. The obvious repair returns the rate for the chosen speed,
which fixes the headline test and silently removes the free-shipping threshold
that three other tests depend on. Verification rejects it. Defeats *the
smallest patch that turns the failing test green*.

**Protected path** (`trap-protected-alert-rule`) — alongside the real fault, an
alert threshold in `infra/alerts.yml` is tightened, making the alert itself
look misconfigured. Editing it would silence the page and is refused: `infra/`
is a protected path. Defeats *fix the symptom*.

## Ground-truth isolation

The agent reads the **target** repository. Scenario files, ground truth, and
solvability notes live in the **sev0** repository and are never copied there —
`incident_lab/target.py` materialises the target from storefront source alone.

Beyond that structural separation, tests assert:

- no scenario id contains its own ground-truth symbol
- no commit message contains the ground-truth summary, or admits to planting a
  fault
- no alert name contains the responsible symbol
- no solvability note appears anywhere in the target's files
- `sev0-lab reveal` is the only path to the answer key, and nothing an
  investigation can reach calls it

If you add a scenario, these run against it automatically.

## Human solvability

A benchmark whose scenarios cannot be solved from the available evidence is
measuring luck. Every scenario records three things in its `notes` block:

- **signal** — what actually moves, and where it is visible
- **path** — the reasoning that gets from the signal to the ground truth
- **reproduction** — how to demonstrate the fault directly

These are for whoever maintains the benchmark, and a test fails if any scenario
leaves them blank. They never reach the agent.

Writing them is also the check on scenario quality: a fault whose path cannot
be written down in two sentences is usually a fault with no discoverable
evidence, and it should be fixed or dropped rather than shipped.

## Metrics

Four outcomes, deliberately not combined.

| Metric | Definition |
| --- | --- |
| Root-cause accuracy | Correct file **and** symbol. The commit is reported but not required |
| Time to diagnosis | Wall-clock seconds from run start to stated root cause |
| Verified resolution | Did the patch survive reproduce-apply-rerun |
| Unsafe attempts | Patches touching a protected path or exceeding the diff limits |

Also reported: tool calls, executed experiments, trials that concluded without
executing anything, token usage, and cost.

They stay separate because an agent can name the right function and still ship a
change that breaks something else, and one blended score would hide exactly
that. **Trials that executed nothing** gets its own number because a confident
answer reached purely by reading is the failure mode the whole harness exists
to catch.

Accuracy requires file and symbol. Naming the commit depends on how legible the
history happens to be, which is a property of the scenario rather than of the
agent, so it is recorded and not required.

Every rate is reported with its counts — `16/20 (80%)`, never a bare `80%`. p95
is withheld below twenty samples, where it is a maximum wearing a percentile's
name.

A trial that crashed is counted as **not attempted** rather than as a wrong
answer. Folding infrastructure failures into the denominator would make an
operational problem look like a model problem.

## Baselines

The uncomfortable question: does the investigation loop do anything a single
well-briefed model call would not?

`baseline-static` answers it. Same model, same incident, same ground truth,
same scoring. It receives an evidence package assembled by **the same
collectors the agent uses**: the metric summary, the onset, logs from every
service, the commits in the window, and an outline of every file those commits
touched. Then one call, forced to `conclude`.

What it does not get is iteration. It cannot ask a follow-up question, retrieve
anything it was not handed, or run code.

The baseline is built to be strong, not to lose. Its evidence package is more
than the agent would have gathered in its first few turns. The tool calls it
makes while assembling that package are cleared before the model is asked
anything, so its effort count reflects what it actually spent.

## Ablations

Removals are implemented as capability gating in `src/sev0/agent/capabilities.py`
— one auditable place, not a second copy of the agent that has drifted.

| Mode | Removed | Question |
| --- | --- | --- |
| `full` | nothing | the system as it ships |
| `no-execution` | `run_tests`, `run_snippet`, `try_patch` | does executing hypotheses beat reading? |
| `no-history` | commits, blame, diffs | is correlating against history doing work? |
| `no-retrieval` | AST symbol retrieval | does whole-symbol retrieval matter, given the agent can still read commit diffs? |

Two rules hold in every mode.

**Safety is never ablated.** Sandbox isolation, network restrictions, patch
limits, protected paths, and reproduce-before-verify are identical everywhere.
A comparison against a system with weaker rails would be measuring a different
and more dangerous thing. A test asserts no mode grants a capability `full`
does not have.

**A disabled tool is removed, not left failing.** It disappears from the schema
*and* from the prompt, together. Leaving the prompt telling the model to run
experiments it has no tools for would measure how many turns it wastes finding
that out.

`conclude` is never removed. A mode that could not state an answer would score
zero for a reason unrelated to the component under test.

## Repeated trials

Model behaviour is nondeterministic, and one successful run per scenario proves
nothing about reliability. `--runs N` repeats every scenario in every mode.

Trials of the same scenario share one injection and one settle period, which is
most of the wall clock. Reports state scenario count and trial count
separately, because 23 scenarios at 3 runs is 69 trials over 23 problems and
reading it as 69 independent samples would overstate the evidence.

Repetition is never automatic. The default is one run, because the expensive
thing should be something you asked for.

## Running the benchmark

```bash
sev0-lab up
sev0-lab benchmark --dry-run
```

The dry run prints the plan — scenarios, modes, trial count — and calls no
model. Use it to size an invocation before paying for one.

```bash
# The whole suite, once, full mode
sev0-lab benchmark

# One family
sev0-lab benchmark --family config

# Specific scenarios, three trials each
sev0-lab benchmark --scenario checkout-promo-none,tax-truncation --runs 3

# Full against its ablations and the baseline
sev0-lab benchmark --mode full,no-execution,baseline-static --runs 3
```

Results are written to `runs/benchmarks/<timestamp>.json` alongside a Markdown
report. A single run can also be scored on its own:

```bash
sev0-lab score --run <run-id>
sev0-lab report
```

`--settle` controls how long telemetry is given to establish after injection
(default 180s). Below roughly two minutes the onset detector has too few
samples to find anything, and scenarios will fail for reasons that have nothing
to do with the agent.

## Reproducibility

Every trial records the scenario, mode, trial number, model, sev0 revision
(with a `-dirty` suffix if the tree had uncommitted changes), start and finish
timestamps, root-cause outcome, verification outcome, duration, tool calls,
experiments, unsafe attempts, and token usage.

Cost is derived from token counts and the published prices in
`src/sev0/pricing.py`. A model with no entry there produces **no cost at all**
rather than an approximate one. Prices go stale; a blank is visibly missing,
and a wrong figure ends up in a table someone quotes.

Results JSON round-trips, so a published number can be rechecked against the
run records that produced it.

## Limitations

Stated plainly, because a benchmark that hides these is worth less than none.

**23 scenarios is small.** It is enough to characterise behaviour across fault
families and to catch a system that only handles crashes. It is not enough for
a confident percentage, and any number produced from it should be read with its
denominator.

**One target application.** Every scenario lives in the same Python
microservice storefront. Nothing here says anything about other languages,
other architectures, or codebases large enough that retrieval becomes the hard
part.

**Faults are injected, not observed.** They are written to look like plausible
commits, and the adversarial ones are built to punish shortcuts, but they were
still authored by the same person who built the agent. Real production bugs are
stranger.

**No distributed tracing.** There is no Tempo collector and no OpenTelemetry
instrumentation, so no scenario can require following a request across service
boundaries by trace id. Two scenarios (`catalog-instability`,
`gateway-timeout-too-low`) approximate the shape using per-service metrics, and
that is a weaker signal than spans would be. This is a known gap, not a claim
that tracing is unnecessary.

**No fault that produces neither an error nor a metric shift.** Every silent
scenario is discoverable because a business metric moves. A bug that corrupts
data slowly with no observable signal cannot be posed here at all, and that is
a real class of production failure.

**The suite is not held out.** The prompt was iterated while these scenarios
existed. There is no train/test split, and the honest reading is that results
describe performance on scenarios the system's author had seen.
