# ruff-fix-and-commit

[![Tests][tests-badge]][tests-link]
[![uv][uv-badge]][uv-link]
[![Ruff][ruff-badge]][ruff-link]
[![codecov][codecov-badge]][codecov-link]
\
[![Made Using tsvikas/python-template][template-badge]][template-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]
[![PRs Welcome][prs-welcome-badge]][prs-welcome-link]

## Overview

Run `ruff check --fix` for selected rule(s) and produce a single git commit with a per-rule summary.

## Install

Install this tool using pipx (or uv):

```bash
pipx install git+https://github.com/tsvikas/ruff-fix-and-commit.git
```

Or, from a checkout:

```bash
uv tool install .
```

## Usage

```bash
ruff-fix-and-commit [TARGET] [--select RULES] [--unsafe-fixes] [--statistics SELECTOR] [--ignore RULES]
```

`TARGET` is a path that scopes the run to tracked Python files under it. Defaults to the current directory.

`--select RULES` is a comma-separated ruff rule selector (codes or category prefixes), passed verbatim to `ruff --select`. If omitted, the tool runs in **status mode** (see below) instead of fixing.

The `--statistics` value accepts the same syntax plus a `DEFAULT` token: bare `DEFAULT` means "use the repo's configured selection", and `DEFAULT,X` means "the repo's configuration plus X" (translated to `--extend-select X`).

Use `ruff-fix-and-commit --help` to learn more.

### Examples

```bash
# fix all auto-fixable B-rules and the specific C212
ruff-fix-and-commit --select B,C212

# scope the fix to a subdirectory
ruff-fix-and-commit src/ --select A001

# include unsafe fixes (e.g. for E731 lambda-assignment)
ruff-fix-and-commit --select E731 --unsafe-fixes

# after fixing B009, show stats for everything in the repo's lint config
ruff-fix-and-commit --select B009 --statistics DEFAULT

# repo's lint config plus extra rules (DEFAULT,X mixes --extend-select)
ruff-fix-and-commit --select B009 --statistics DEFAULT,E

# after fixing B009, show stats for any selector, suppressing noisy families
ruff-fix-and-commit --select B009 --statistics ALL --ignore D,ANN

# status mode: report whether the tree is formatted and induced rules are clear
ruff-fix-and-commit
```

### Commit message format

Single rule fixed:

```
lint: fix B009 (get-attr-with-constant) x2

Auto-fixed with ruff: https://docs.astral.sh/ruff/
Committed by ruff-fix-and-commit: https://github.com/tsvikas/ruff-fix-and-commit
```

Multiple rules fixed (header shows the rules input verbatim and the total fix count):

```
lint: fix A,B001,C212 x20

- A123 (builtin-attribute-shadowing) x10
- B001 (mutable-default-value) x7
- C212 (unnecessary-iterable-comprehension) x3

Auto-fixed with ruff: https://docs.astral.sh/ruff/
Committed by ruff-fix-and-commit: https://github.com/tsvikas/ruff-fix-and-commit
```

## Guarantees

The tool **refuses to run a fix** when:

- The current directory is not inside a git repository.
- Any tracked file has uncommitted changes (untracked files are ignored).

The dirty-tree gate is **skipped in status mode** (no `--select`) since that path is read-only.

The tool **never modifies**:

- Untracked files (only tracked Python files are passed to ruff).
- Files inside git submodules.

`--unsafe-fixes` is forwarded explicitly to ruff in both directions: when the flag is omitted, the tool sends `--no-unsafe-fixes` so a repo's `[tool.ruff] unsafe-fixes = true` cannot silently apply unsafe fixes.

### Silent cleanup of induced rules

A ruff fix can introduce I001 (unsorted-imports) or F401 (unused-import) violations as a side effect of fixing something else. To avoid leaving the tree dirtier than it was found, the tool runs a follow-up `--fix` pass for these "induced rules" — but **only** under conditions that match user intent:

- The induced rule had **zero** violations before the fix (so any new violations were introduced by the run), **or**
- The induced rule was **included in the user's selector** (so the user opted into fixing it).

Otherwise, pre-existing I001/F401 violations are left alone.

After the silent cleanup, if the tree was already formatted before the fix, `ruff format` is re-run so the resulting commit stays formatted.

## Status mode

When invoked **without** `--select`, the tool reports:

```
formatted: yes
I001 unsorted-imports: clean
F401 unused-import: clean
```

Each induced rule shows either `clean` or its current violation count:

```
formatted: no
I001 unsorted-imports: 3
F401 unused-import: 1
```

Status mode never fixes and never commits. If `--statistics` is passed, the stats block runs after the status report.

## Output cases

The tool's output is intentionally minimal. The exact stdout depends on the outcome:

| Outcome                                           | Output                                                                                                         |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Repo has no tracked Python files (under `TARGET`) | `No Python files to check.`                                                                                    |
| Files exist, no violations of `--select` rules    | `No matching violations.`                                                                                      |
| Files exist, violations exist but none fixed      | `no fixes applied:` + per-rule counts; `hint: N hidden fixes can be enabled with --unsafe-fixes` if applicable |
| Some violations fixed, others remain              | Commit message + `N violations remain.` (or `1 violation remains.`) footer                                     |
| All violations fixed                              | Commit message only                                                                                            |

Add `--statistics SELECTOR` for a per-rule breakdown of what's left after the fix; combine with `--ignore D,ANN` to drop noisy families from that view.

Example of the unfixable + hint path:

```
$ ruff-fix-and-commit --select E731
no fixes applied:
9       E731    [ ] lambda-assignment
hint: 9 hidden fixes can be enabled with --unsafe-fixes
```

The marker on each row indicates how many of that rule's violations are auto-fixable: `[*]` all of them, `[~]` some of them, `[ ]` none of them.

## Exit codes

| Code | Meaning                                      |
| ---- | -------------------------------------------- |
| `0`  | Success, or nothing to fix                   |
| `1`  | Refused (not in a repo, dirty tracked files) |
| `2`  | ruff failed (invalid selector, etc.)         |

## Contributing

Interested in contributing?
See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guideline.

[codecov-badge]: https://codecov.io/gh/tsvikas/ruff-fix-and-commit/graph/badge.svg
[codecov-link]: https://codecov.io/gh/tsvikas/ruff-fix-and-commit
[github-discussions-badge]: https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github
[github-discussions-link]: https://github.com/tsvikas/ruff-fix-and-commit/discussions
[prs-welcome-badge]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg
[prs-welcome-link]: https://opensource.guide/how-to-contribute/
[ruff-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff-link]: https://github.com/astral-sh/ruff
[template-badge]: https://img.shields.io/badge/%F0%9F%9A%80_Made_Using-tsvikas%2Fpython--template-gold
[template-link]: https://github.com/tsvikas/python-template
[tests-badge]: https://github.com/tsvikas/ruff-fix-and-commit/actions/workflows/ci.yml/badge.svg
[tests-link]: https://github.com/tsvikas/ruff-fix-and-commit/actions/workflows/ci.yml
[uv-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json
[uv-link]: https://github.com/astral-sh/uv
