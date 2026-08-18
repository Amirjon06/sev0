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

## 2026-08-18 — Pull requests

**Tried:** git_ops — branch, commit, and the pull request body.
**Result:** 22 more tests, 160 in total. Phase 3 is code-complete.
**Decided:** The refusals are the design. sev0 may create a branch and commit
to it. It may not commit to the default branch, may not merge, may not work on
a dirty tree, and opens as a draft. None of that is reachable from inside a
run. A failed commit deletes its own branch — a half-made branch is worse than
none, because the next run trips over it.
**Decided:** The body leads with the root cause, then lists what was ruled out
and why. A pull request that shows only the answer looks more confident and is
worth less to whoever has to review it. Verification output is quoted verbatim
rather than summarised, and the count of tool calls that actually executed code
is stated separately from the total.
**Decided:** No verified fix means no pull request. sev0 does not propose
changes it could not prove, and there is no flag to override that.

## 2026-08-18 — The agent can run experiments

**Tried:** Exposed the sandbox to the investigation loop as three tools:
`run_snippet`, `run_tests`, and `try_patch`.
**Result:** 16 more tests, 138 in total. Demonstrated end to end against the
real planted fault with no model involved. Calling compute_total with an
inactive code raises TypeError in the sandbox; the suite names the one
assertion it breaks; a "just skip discounts" patch repairs that test and breaks
three others and is reported as not verified; the correct guard takes it from
6 passed 1 failed to 7 passed.
**Decided:** This is the line between the project and an LLM wrapper. A
hypothesis that has not been executed is a guess however well it reads, so the
prompt now tells the model to test rather than assume, and the state counts
experiments separately from reads. An investigation that ran nothing only ever
formed opinions, and the summary should say so.
**Decided:** Failed patch attempts are recorded, not discarded. What was tried
and rejected is evidence for whoever reviews the fix.
**Note:** experiments still run in throwaway copies, so the tree under
investigation cannot be damaged by the code being tested against it. There is a
test asserting a snippet that writes to cart.py leaves the real file untouched.

## 2026-08-18 — Sandbox and verification

**Tried:** Built the sandbox, the patch limits, and the verifier. No API credit
yet, so this is the half of Phase 3 that needs no model.
**Result:** 26 more tests, 122 in total. All of it runs against a local runner
so CI needs no Docker daemon.
**Decided:** Reproduce before patching, always. If the suite is already green
then whatever was found is not what broke production, and applying a patch
would mean changing working code to fit a story. The verifier refuses to call
anything verified unless the failure was demonstrably present first.
**Decided:** A patch that breaks a limit does not get to run code just to find
out whether it would have worked. Validation happens before the scratch copy is
even made, and again inside apply() — between a check and a write the tree can
move, and the limits have to hold regardless of call order.
**Decided:** Everything happens in a throwaway copy. The repository under
investigation is never modified, so a run that dies halfway leaves nothing.
**Needed:** the storefront now carries its own test suite. Verification has to
run something, and a suite that lives in the sev0 repo cannot travel with the
target the agent is investigating.
**Caught by a test I wrote badly:** my own "harmless" patch used an anchor that
matched twice, and the ambiguity rule rejected it. The rule was right and the
test was wrong — an edit that matches in two places is not a deliberate edit.

## 2026-08-18 — The agent investigates

**Tried:** Code retrieval, the toolbox, and the investigation loop. `sev0
investigate` now runs end to end against a live model.
**Result:** 38 more tests, 96 in total. The loop is exercised against a
scripted client, so CI needs neither an API key nor Docker.
**Decided:** Retrieval uses the stdlib `ast` rather than tree-sitter. The
target is single-language by design, so the dependency buys nothing, and
returning a fixed window of lines around a match would cut functions in half —
half a function is worse than none, because the model sees a branch without the
guard above it and confidently explains a bug that is not there.
**Decided:** Tools return strings and never raise. A malformed regex should
cost one turn and a correction, not the run. Failures go back as tool results
with is_error set, so the model can see what it did wrong.
**Decided:** The loop owns what a model cannot be trusted with — a hard call
budget, a stop condition, and the trace. Everything else lives in the prompt;
a loop that second-guesses the model just fights it.
**Caught by a test:** the loop was passing its live message list to every call,
so anything recording requests saw the conversation's final state rather than
what was sent that turn. It now sends a copy.
**Open question:** the system prompt tells the model to record rejected
hypotheses. Whether it actually does, under budget pressure, is exactly the
kind of thing Phase 4 needs to measure rather than assume.

## 2026-08-18 — Evidence collectors

**Tried:** Built the log, metric, and git-history collectors. No model involved
yet; this is the plumbing the agent will stand on.
**Result:** 26 new tests, all against a mocked transport so CI needs no Docker.
Two real bugs surfaced while writing them. The git log parser put each commit's
file list into the *next* commit's record, because --name-only writes files
after the whole format string, so the record separator has to lead rather than
trail. And Prometheus renders an empty-denominator ratio as the string "NaN",
which float() accepts happily — left in, it poisoned the baseline average.
**Decided:** Onset detection requires a breach to persist across several
samples. A single spike is a restart or one unlucky request, and an agent that
chases blips is worse than no agent. The collectors deduplicate log lines by
shape before returning them: a minute of traffic is thousands of near-identical
lines, and handing those to a model buries the signal it is meant to find.
**Note:** history.py is read-only by construction — no checkout, no branch, no
commit. An investigation must not be able to damage what it is investigating.

## 2026-08-17 — Fault injection working

**Tried:** Built the sev0-lab CLI, the target repository, and the first
scenario: a discount refactor that drops a None guard.
**Result:** Inject commits a real change into a separate repo under runs/ and
rebuilds cart; restore resets to the baseline tag. Verified end to end, and the
harness is covered by 15 tests.
**Decided:** Faults land in a target repo, never in this one. Planting bugs
here would push broken code to the remote, and the agent needs a log with a
plausible haystack rather than one suspicious commit on top of scaffolding.
Edits are find-and-replace, not diffs — a diff carries line numbers and would
rot silently as the source moves, while an exact anchor fails loudly. A test
asserts every anchor still matches the pristine source exactly once.
**Also:** the fault is deliberately partial. Valid promo codes keep working, so
roughly one checkout in seven fails. A fault that broke everything would be
trivially detectable and would flatter the agent.

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
