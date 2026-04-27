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
ruff-fix-and-commit RULES [--unsafe-fixes] [--statistics SELECTOR]
```

`RULES` is a comma-separated ruff rule selector (codes or category prefixes), passed verbatim to `ruff --select`.

### Examples

```bash
# fix all auto-fixable B-rules and the specific C212
ruff-fix-and-commit B,C212

# include unsafe fixes (e.g. for E731 lambda-assignment)
ruff-fix-and-commit E731 --unsafe-fixes

# after fixing B009, show stats for everything in the repo's lint config
ruff-fix-and-commit B009 --statistics DEFAULT

# after fixing B009, show stats for any selector
ruff-fix-and-commit B009 --statistics ALL
```

### Commit message format

Single rule fixed:

```
ruff-fix: B009 (get-attr-with-constant) x2
```

Multiple rules fixed:

```
ruff-fix: A,B001,C212

- A123 (builtin-attribute-shadowing) x10
- B001 (mutable-default-value) x7
- C212 (unnecessary-iterable-comprehension) x3
```

## Guarantees

The tool **refuses to run** when:

- The current directory is not inside a git repository.
- Any tracked file has uncommitted changes (untracked files are ignored).

The tool **never modifies**:

- Untracked files (only tracked Python files are passed to ruff).
- Files inside git submodules.

After a successful fix, the tool:

- Runs `ruff format` **only if** the codebase was already formatted before the fix.
- Silently fixes new I001 (unsorted-imports) or F401 (unused-import) violations introduced by the fix — but only if the codebase had **zero** of those beforehand. Pre-existing I001/F401 are left alone.
- Stages only modified tracked files (`git add -u`) and creates one commit.

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Success, or nothing to fix |
| `1`  | Refused (not in a repo, dirty tracked files) |
| `2`  | ruff failed (invalid selector, etc.) |

## Output

When the fix succeeds, the only output is the commit message. When the user's selector matches violations but none could be auto-fixed, the per-rule counts are shown along with a hint about `--unsafe-fixes` if it would unlock fixes:

```
$ ruff-fix-and-commit E731
no fixes applied:
9	E731	[ ] lambda-assignment
hint: 9 hidden fixes can be enabled with --unsafe-fixes
```

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
pre-commit run --all-files
```
