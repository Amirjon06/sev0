# Contributing to sev0

Thanks for your interest. This document covers the workflow, the commit
convention, and what a reviewable pull request looks like.

## Development setup

```bash
git clone https://github.com/Amirjon06/sev0.git
cd sev0
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the full local check before pushing:

```bash
ruff check . && mypy && pytest --cov=sev0
```

## Branching

Branch from `main` using a `<type>/<short-description>` name.

```bash
git checkout -b feat/loki-log-collector
```

## Commit messages

This project follows the [Conventional Commits](https://www.conventionalcommits.org)
specification, because release notes are generated from the history.

**Format:** `<type>(<scope>): <description>`

| Type | Use for |
| --- | --- |
| `feat` | A new user-facing capability |
| `fix` | A bug fix |
| `docs` | Documentation only |
| `style` | Formatting that does not change logic |
| `refactor` | Restructuring that neither fixes a bug nor adds a feature |
| `test` | Adding or correcting tests |
| `chore` | Build tasks, dependencies, configuration |

**The seven rules:**

1. Separate subject from body with a blank line
2. Limit the subject line to 50 characters
3. Capitalize the first letter of the description
4. Do not end the subject line with a period
5. Use the imperative mood ("Add", not "Added")
6. Wrap body text at 72 characters
7. Explain **what** and **why**, not **how**

**Good:**

```
fix(sandbox): Prevent test runner from reaching the network

Reproduction containers inherited the host bridge network, so a
failing test could hit production endpoints during a replay. The
sandbox now defaults to network mode "none" and requires an explicit
opt-in per scenario.

Resolves: #47
```

**Avoid:** `fixed stuff`, or one commit that refactors, restyles, and fixes a
typo at the same time. One logical change per commit.

## Pull requests

A pull request should:

- Change one thing, and say **why** in the description
- Keep `ruff`, `mypy`, and `pytest` green
- Add a test for any behavior change
- Update `docs/` when it changes an interface or a safety rail

Safety-relevant changes — anything touching `src/sev0/sandbox/` or the limits in
`src/sev0/config.py` — need an explicit note in the description explaining the
blast radius.

## Filing issues

Open an issue with the reproduction steps, the expected behavior, and the
observed behavior. For agent misbehavior, attach the run directory from
`runs/<run-id>/` — it contains the full reasoning trace.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
