"""Which parts of sev0 a run is allowed to use.

An ablation asks whether a component earns its place. The honest way to answer
that is to remove one thing and change nothing else, which means the removal
has to happen at a single, auditable point rather than by maintaining a second
copy of the agent that has drifted from the first.

Two rules hold across every mode. Safety is never ablated: sandbox isolation,
patch limits, protected paths, and the reproduce-before-verify rule apply
identically no matter what is switched off, because a comparison that weakened
them would be measuring a different and more dangerous system. And a disabled
tool is removed from the schema entirely rather than left present and failing,
so the model is never spending turns on a capability it does not have.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tools grouped by the component they belong to. Ablating a component removes
# its whole group; leaving half of one in place would make the result
# uninterpretable.
OBSERVABILITY_TOOLS = frozenset(
    {"metrics_overview", "find_onset", "failure_logs", "service_logs"}
)
HISTORY_TOOLS = frozenset({"recent_commits", "commits_touching", "show_commit", "blame"})
RETRIEVAL_TOOLS = frozenset({"search_code", "file_outline", "read_symbol"})
EXECUTION_TOOLS = frozenset({"run_tests", "run_snippet", "try_patch"})
REASONING_TOOLS = frozenset({"record_hypothesis", "conclude"})


@dataclass(frozen=True)
class Capabilities:
    """What a run may do. Reasoning tools are always available.

    Without `conclude` there is no way to state an answer, so no mode removes
    it; an ablation that made the run unable to report a result would score
    zero for a reason that has nothing to do with the component under test.
    """

    name: str = "full"
    observability: bool = True
    history: bool = True
    retrieval: bool = True
    execution: bool = True

    def allows(self, tool: str) -> bool:
        if tool in OBSERVABILITY_TOOLS:
            return self.observability
        if tool in HISTORY_TOOLS:
            return self.history
        if tool in RETRIEVAL_TOOLS:
            return self.retrieval
        if tool in EXECUTION_TOOLS:
            return self.execution
        return True

    @property
    def missing(self) -> tuple[str, ...]:
        absent = []
        if not self.observability:
            absent.append("observability")
        if not self.history:
            absent.append("history")
        if not self.retrieval:
            absent.append("code retrieval")
        if not self.execution:
            absent.append("execution")
        return tuple(absent)


MODES: dict[str, Capabilities] = {
    # Everything on. The system as it ships.
    "full": Capabilities(name="full"),
    # Does executing hypotheses actually help, or would reading have been
    # enough? This is the ablation the whole project's premise rests on.
    "no-execution": Capabilities(name="no-execution", execution=False),
    # Is correlating against recent commits doing work, or is the code alone
    # enough to find the fault?
    "no-history": Capabilities(name="no-history", history=False),
    # Whole-symbol AST retrieval against no code access at all. The agent can
    # still read commit diffs, which is a deliberately generous floor.
    "no-retrieval": Capabilities(name="no-retrieval", retrieval=False),
}


def mode(name: str) -> Capabilities:
    known = ", ".join(sorted(MODES))
    if name not in MODES:
        raise KeyError(f"unknown mode {name!r}; known modes: {known}")
    return MODES[name]
