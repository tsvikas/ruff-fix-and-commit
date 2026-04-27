# ruff-fix-and-commit

Run `ruff check --fix` for selected rule(s) and produce a single git commit with a per-rule summary.

## Install

```bash
uv tool install git+https://github.com/tsvikas/ruff-fix-and-commit
```

Or, from a checkout:

```bash
uv tool install .
```

## Usage

```bash
ruff-fix-and-commit [TARGET] [--select RULES] [--unsafe-fixes] [--statistics SELECTOR] [--ignore SELECTOR]
```

`TARGET` is a path that scopes the run to tracked Python files under it. Defaults to the current directory.

`--select RULES` is a comma-separated ruff rule selector (codes or category prefixes), passed verbatim to `ruff --select`. If omitted, the tool runs in **status mode** (see below) instead of fixing.

The `--statistics` value accepts the same syntax plus a `DEFAULT` token: bare `DEFAULT` means "use the repo's configured selection", and `DEFAULT,X` means "the repo's configuration plus X" (translated to `--extend-select X`).

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
ruff-fix: B009 (get-attr-with-constant) x2
```

Multiple rules fixed (header shows the rules input verbatim and the total fix count):

```
ruff-fix: A,B001,C212 x20

- A123 (builtin-attribute-shadowing) x10
- B001 (mutable-default-value) x7
- C212 (unnecessary-iterable-comprehension) x3
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
induced rules (I001, F401): clear
```

Or, if either is dirty:

```
formatted: no
induced rules (I001, F401): not clear
  3       I001    unsorted-imports
  1       F401    unused-import
```

Status mode never fixes and never commits. If `--statistics` is passed, the stats block runs after the status report.

## Output cases

The tool's output is intentionally minimal. The exact stdout depends on the outcome:

| Outcome | Output |
|---|---|
| Repo has no tracked Python files (under `TARGET`) | `No Python files to check.` |
| Files exist, no violations of `--select` rules | `No matching violations.` |
| Files exist, violations exist but none fixed | `no fixes applied:` + per-rule counts; `hint: N hidden fixes can be enabled with --unsafe-fixes` if applicable |
| Some violations fixed, others remain | Commit message + `N violations remain.` (or `1 violation remains.`) footer |
| All violations fixed | Commit message only |

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

| Code | Meaning |
|------|---------|
| `0`  | Success, or nothing to fix |
| `1`  | Refused (not in a repo, dirty tracked files) |
| `2`  | ruff failed (invalid selector, etc.) |

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
pre-commit run --all-files
```
