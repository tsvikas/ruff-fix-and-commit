"""ruff-fix-and-commit: run `ruff check --fix` for selected rules and commit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import cyclopts
import git

app = cyclopts.App(name="ruff-fix-and-commit")

_RUFF_ENV = {k: v for k, v in os.environ.items() if k != "RUFF_OUTPUT_FORMAT"}

_DEFAULT_SENTINEL = "DEFAULT"

# Rules whose violations a ruff fix can introduce as a side effect of fixing
# something else. We post-fix-clean these so a ruff-fix-and-commit run never
# leaves the tree dirtier than it found it.
RUFF_INDUCED_RULES: tuple[str, ...] = ("I001", "F401")


class RuffError(Exception):
    """ruff exited with an unexpected status (config/usage error, not violations)."""


@dataclass(frozen=True)
class RuleStat:
    code: str
    name: str
    count: int
    fixable: bool
    fixable_count: int

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> RuleStat:
        return cls(
            code=payload["code"],
            name=payload["name"],
            count=payload["count"],
            fixable=payload["fixable"],
            fixable_count=payload["fixable_count"],
        )


class Ruff:
    """Adapter for invoking the ruff CLI against a fixed set of targets."""

    def __init__(self, targets: list[str], *, unsafe_fixes: bool = False) -> None:
        self.targets = targets
        self.unsafe_fixes = unsafe_fixes

    def check(
        self,
        select: str | None,
        *,
        fix: bool = False,
        ignore: str | None = None,
    ) -> dict[str, RuleStat]:
        """Run ``ruff check --statistics`` and return per-rule stats.

        ``select=None`` omits ``--select`` so ruff uses the repo's configured
        rule selection. With ``fix=True``, applies fixes in the same call;
        the returned stats are the post-fix remaining violations.
        ``ignore`` forwards to ruff as ``--ignore`` to drop matching rules
        from the result.
        """
        args = ["check", "--statistics", "--output-format", "json"]
        args.append("--fix" if fix else "--no-fix")
        if select is not None:
            args.extend(["--select", select])
        if ignore is not None:
            args.extend(["--ignore", ignore])
        # Be explicit either way so a repo's `unsafe-fixes = true` config
        # cannot override our intent.
        args.append("--unsafe-fixes" if self.unsafe_fixes else "--no-unsafe-fixes")
        args.extend(self.targets)
        result = self._run(args, allow_violations=True)
        return _parse_stats(result.stdout)

    def format_check(self) -> bool:
        result = self._run(["format", "--check", *self.targets], allow_violations=True)
        return result.returncode == 0

    def format(self) -> None:
        self._run(["format", *self.targets])

    def _run(
        self, args: list[str], *, allow_violations: bool = False
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["ruff", *args],
            capture_output=True,
            text=True,
            check=False,
            env=_RUFF_ENV,
        )
        allowed = {0, 1} if allow_violations else {0}
        if result.returncode not in allowed:
            msg = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"ruff exited with code {result.returncode}"
            )
            raise RuffError(msg)
        return result


def _parse_stats(stdout: str) -> dict[str, RuleStat]:
    if not stdout.strip():
        return {}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return {entry["code"]: RuleStat.from_json(entry) for entry in payload}


def _resolve_select(select: str) -> str | None:
    """Translate the ``DEFAULT`` sentinel to ``None`` (use repo config)."""
    return None if select.upper() == _DEFAULT_SENTINEL else select


@app.default
def main(
    rules: str | None = None,
    *,
    unsafe_fixes: bool = False,
    statistics: str | None = None,
    ignore: str | None = None,
) -> int:
    """Run `ruff check --fix` for RULES and commit the changes.

    Parameters
    ----------
    rules:
        Comma-separated ruff rule selectors (codes or category prefixes),
        passed verbatim to `ruff --select`. Example: `A,B001,C212`. If
        omitted, the tool runs in status mode: it reports whether the
        repo is formatted and whether the induced rules (I001, F401)
        are clear, without fixing or committing.
    unsafe_fixes:
        Forwarded to ruff as `--unsafe-fixes`.
    statistics:
        After the fix, run `ruff check --select STATISTICS --statistics`
        and print a per-rule count of what's still left. Pass `DEFAULT`
        to omit `--select` and use the repo's configured rule selection.
        Validated up front so a typo here doesn't waste a fix run.
    ignore:
        Forwarded to ruff as `--ignore` for the post-fix `--statistics`
        run only. Example: `D,ANN`.
    """
    try:
        repo = git.Repo(".", search_parent_directories=True)
    except git.InvalidGitRepositoryError:
        print("error: not inside a git repository", file=sys.stderr)
        return 1
    # Dirty-tree gate only applies when we plan to fix + commit; status
    # mode is read-only and safe to run on a dirty tree.
    if rules is not None and repo.is_dirty(untracked_files=False):
        print(
            "error: working tree has uncommitted changes to tracked files; "
            "commit or stash them first",
            file=sys.stderr,
        )
        return 1

    targets = _tracked_python_files(repo)
    if not targets:
        print("No Python files to check.")
        return 0

    ruff = Ruff(targets, unsafe_fixes=unsafe_fixes)

    try:
        if statistics is not None:
            # Validate up front (cheap call); raises RuffError if selector is bad.
            ruff.check(_resolve_select(statistics), ignore=ignore)
        if rules is None:
            _print_status(ruff)
            if statistics is not None:
                _print_statistics(ruff, _resolve_select(statistics), ignore=ignore)
            return 0
        rc = _do_fix_and_commit(repo, ruff, rules)
        if statistics is not None:
            _print_statistics(ruff, _resolve_select(statistics), ignore=ignore)
        return rc
    except RuffError as e:
        msg = str(e)
        prefix = "" if msg.lower().startswith("error") else "error: "
        print(f"{prefix}{msg}", file=sys.stderr)
        return 2


def _print_status(ruff: Ruff) -> None:
    """Status output for the no-rules path: format + induced-rules cleanliness."""
    formatted = ruff.format_check()
    induced = ruff.check(",".join(RUFF_INDUCED_RULES))
    print(f"formatted: {'yes' if formatted else 'no'}")
    if not induced:
        print(f"induced rules ({', '.join(RUFF_INDUCED_RULES)}): clear")
        return
    print(f"induced rules ({', '.join(RUFF_INDUCED_RULES)}): not clear")
    for entry in sorted(induced.values(), key=lambda e: (-e.count, e.code)):
        print(f"  {entry.count}\t{entry.code}\t{entry.name}")


def _do_fix_and_commit(repo: git.Repo, ruff: Ruff, rules: str) -> int:
    was_formatted = ruff.format_check()
    before = ruff.check(rules)
    before_induced = ruff.check(",".join(RUFF_INDUCED_RULES))

    after = ruff.check(rules, fix=True)
    fixed: dict[str, int] = {}
    for code, entry in before.items():
        after_entry = after.get(code)
        delta = entry.count - (after_entry.count if after_entry else 0)
        if delta > 0:
            fixed[code] = delta

    if not fixed:
        _report_nothing_fixed(ruff, rules, after)
        return 0

    # Clean up induced rules either if they were absent before the fix
    # (so the fix could have introduced them) or if they were in the user's
    # selection (so the user opted into fixing them, and the main fix may
    # have left newly-introduced violations behind).
    silent_codes = [
        code
        for code in RUFF_INDUCED_RULES
        if code not in before_induced or code in before
    ]
    if silent_codes:
        ruff.check(",".join(silent_codes), fix=True)

    if was_formatted:
        ruff.format()

    repo.git.add(update=True)
    if not repo.is_dirty(working_tree=False, untracked_files=False, index=True):
        print("warning: nothing was staged after running ruff; skipping commit")
        return 0
    names = {code: entry.name for code, entry in before.items()}
    message = _build_message(rules, fixed, names)
    repo.index.commit(message)
    print(message)
    _print_remaining(ruff, rules)
    return 0


def _tracked_python_files(repo: git.Repo) -> list[str]:
    suffixes = (".py", ".pyi", ".ipynb")
    root = repo.working_dir
    submodule_prefixes = tuple(f"{sm.path}/" for sm in repo.submodules)
    paths = repo.git.ls_files().splitlines()
    return [
        os.path.join(root, p)
        for p in paths
        if p.endswith(suffixes) and not p.startswith(submodule_prefixes)
    ]


def _report_nothing_fixed(ruff: Ruff, select: str, after: dict[str, RuleStat]) -> None:
    if not after:
        print("No matching violations.")
        return
    print("no fixes applied:")
    for entry in sorted(after.values(), key=lambda e: (-e.count, e.code)):
        marker = "[*]" if entry.fixable else "[ ]"
        print(f"{entry.count}\t{entry.code}\t{marker} {entry.name}")
    if ruff.unsafe_fixes:
        return
    unsafe_after = Ruff(ruff.targets, unsafe_fixes=True).check(select)
    hidden = sum(e.fixable_count for e in unsafe_after.values())
    if hidden > 0:
        plural = "es" if hidden != 1 else ""
        print(f"hint: {hidden} hidden fix{plural} can be enabled with --unsafe-fixes")


def _print_remaining(ruff: Ruff, rules: str) -> None:
    """Re-query post-fix state and report any violations of `rules` that remain."""
    remaining = sum(s.count for s in ruff.check(rules).values())
    if remaining == 0:
        return
    if remaining == 1:
        print("1 violation remains.")
    else:
        print(f"{remaining} violations remain.")


def _print_statistics(
    ruff: Ruff, select: str | None, *, ignore: str | None = None
) -> None:
    stats = ruff.check(select, ignore=ignore)
    print()
    if not stats:
        print("remaining: none")
        return
    print("remaining:")
    sorted_entries = sorted(stats.values(), key=lambda s: (-s.count, s.code))
    for s in sorted_entries:
        marker = "[*]" if s.fixable else "[ ]"
        print(f"{s.count}\t{s.code}\t{marker} {s.name}")


def _build_message(
    rules_input: str, fixed: dict[str, int], names: dict[str, str]
) -> str:
    items = sorted(fixed.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) == 1:
        code, count = items[0]
        return f"ruff-fix: {code} ({names.get(code, '')}) x{count}"
    total = sum(count for _, count in items)
    lines = [f"ruff-fix: {rules_input} x{total}", ""]
    lines.extend(f"- {code} ({names.get(code, '')}) x{count}" for code, count in items)
    return "\n".join(lines)


if __name__ == "__main__":
    app()
