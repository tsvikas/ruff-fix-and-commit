"""ruff-fix-and-commit: run `ruff check --fix` for selected rules and commit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

import cyclopts
import git

app = cyclopts.App(name="ruff-fix-and-commit")

_RUFF_ENV = {k: v for k, v in os.environ.items() if k != "RUFF_OUTPUT_FORMAT"}

_DEFAULT_SENTINEL = "DEFAULT"


class ExitCode(IntEnum):
    """Process exit codes returned by `main`. Documented in README."""

    OK = 0
    REFUSED = 1
    RUFF_ERROR = 2


# Rules whose violations a ruff fix can introduce as a side effect of fixing
# something else. We post-fix-clean these so a ruff-fix-and-commit run never
# leaves the tree dirtier than it found it.
RUFF_INDUCED_RULES: tuple[str, ...] = ("I001", "F401")


class RuffError(Exception):
    """ruff exited with an unexpected status (config/usage error, not violations)."""


@dataclass(frozen=True)
class RuleStat:
    """One row of ruff's `--statistics` JSON output for a single rule."""

    code: str
    name: str
    count: int
    fixable: bool
    fixable_count: int

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> RuleStat:
        """Build a RuleStat from one ruff `--statistics` JSON entry."""
        return cls(
            code=payload["code"],
            name=payload["name"],
            count=payload["count"],
            fixable=payload["fixable"],
            fixable_count=payload["fixable_count"],
        )


@dataclass(frozen=True)
class Selector:
    """Ruff rule selector for one invocation.

    `select` becomes `--select`; `extend_select` becomes `--extend-select`.
    Either may be `None`. With both `None`, ruff falls back to the repo's
    configured selection.
    """

    select: str | None = None
    extend_select: str | None = None

    @classmethod
    def parse(cls, raw: str) -> Selector:
        """Parse a user-facing selector string with `DEFAULT` semantics.

        `DEFAULT` (case-insensitive) anywhere in a comma list means
        "use the repo's configured selection". When mixed with other
        rules, those rules become `--extend-select` so they're added
        on top of the repo's defaults.

            "B009"          -> Selector(select="B009")
            "DEFAULT"       -> Selector()                  # repo config
            "DEFAULT,E"     -> Selector(extend_select="E") # repo + E
            "A,DEFAULT,B"   -> Selector(extend_select="A,B")
        """
        parts = raw.split(",")
        has_default = any(p.upper() == _DEFAULT_SENTINEL for p in parts)
        rest = ",".join(p for p in parts if p.upper() != _DEFAULT_SENTINEL)
        if has_default:
            return cls(extend_select=rest or None)
        return cls(select=rest or None)


def _to_selector(s: Selector | str | None) -> Selector:
    if s is None:
        return Selector()
    if isinstance(s, Selector):
        return s
    return Selector(select=s)


class Ruff:
    """Adapter for invoking the ruff CLI against a fixed set of targets."""

    def __init__(self, targets: list[Path]) -> None:
        """Bind the adapter to the file paths ruff should be invoked on."""
        self.targets = targets
        executable = shutil.which("ruff")
        if executable is None:
            msg = "`ruff` not found on PATH"
            raise RuffError(msg)
        self._executable = executable

    def stats(
        self,
        selector: Selector | str | None = None,
        *,
        unsafe_fixes: bool = False,
        ignore: str | None = None,
    ) -> dict[str, RuleStat]:
        """Read-only: query per-rule violation counts.

        ``selector=None`` omits selectors so ruff uses the repo's
        configured rule selection. ``ignore`` forwards as ``--ignore``.
        """
        return self._invoke(
            _to_selector(selector),
            fix=False,
            unsafe_fixes=unsafe_fixes,
            ignore=ignore,
        )

    def fix(
        self, selector: Selector | str, *, unsafe_fixes: bool = False
    ) -> dict[str, RuleStat]:
        """Apply fixes for ``selector`` and return post-fix remaining stats."""
        return self._invoke(
            _to_selector(selector), fix=True, unsafe_fixes=unsafe_fixes, ignore=None
        )

    def format_check(self) -> bool:
        """Return True iff `ruff format --check` reports the targets are formatted."""
        result = self._run(["format", "--check", *self.targets], allow_violations=True)
        return result.returncode == 0

    def format(self) -> None:
        """Apply `ruff format` in place to the targets."""
        self._run(["format", *self.targets])

    def _invoke(
        self,
        selector: Selector,
        *,
        fix: bool,
        unsafe_fixes: bool,
        ignore: str | None,
    ) -> dict[str, RuleStat]:
        args = ["check", "--statistics", "--output-format", "json"]
        args.append("--fix" if fix else "--no-fix")
        if selector.select is not None:
            args.extend(["--select", selector.select])
        if selector.extend_select is not None:
            args.extend(["--extend-select", selector.extend_select])
        if ignore is not None:
            args.extend(["--ignore", ignore])
        # Be explicit either way so a repo's `unsafe-fixes = true` config
        # cannot override our intent.
        args.append("--unsafe-fixes" if unsafe_fixes else "--no-unsafe-fixes")
        args.extend(self.targets)
        result = self._run(args, allow_violations=True)
        return _parse_stats(result.stdout)

    def _run(
        self, args: list[str], *, allow_violations: bool = False
    ) -> subprocess.CompletedProcess[str]:
        # `args` is a list (no shell), and `self._executable` is the
        # shutil.which-resolved absolute path to ruff. S603 has no
        # programmatic fix -- it's a "review for untrusted input" rule;
        # the input here is internal.
        result = subprocess.run(  # noqa: S603
            [self._executable, *args],
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


def _fixability_marker(entry: RuleStat) -> str:
    """Render an at-a-glance marker for how many of an entry's violations are fixable.

    [*] all of them, [~] some of them, [ ] none of them.
    """
    if entry.fixable_count == 0:
        return "[ ]"
    if entry.fixable_count == entry.count:
        return "[*]"
    return "[~]"


def _parse_stats(stdout: str) -> dict[str, RuleStat]:
    if not stdout.strip():
        return {}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return {entry["code"]: RuleStat.from_json(entry) for entry in payload}


@app.default
def main(
    rules: str | None = None,
    *,
    unsafe_fixes: bool = False,
    statistics: str | None = None,
    ignore: str | None = None,
) -> ExitCode:
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
        return ExitCode.REFUSED
    # Dirty-tree gate only applies when we plan to fix + commit; status
    # mode is read-only and safe to run on a dirty tree.
    if rules is not None and repo.is_dirty(untracked_files=False):
        print(
            "error: working tree has uncommitted changes to tracked files; "
            "commit or stash them first",
            file=sys.stderr,
        )
        return ExitCode.REFUSED

    targets = _tracked_python_files(repo)
    if not targets:
        print("No Python files to check.")
        return ExitCode.OK

    ruff = Ruff(targets)

    stats_selector = Selector.parse(statistics) if statistics is not None else None
    try:
        if stats_selector is not None:
            # Validate up front (cheap call); raises RuffError if selector is bad.
            ruff.stats(stats_selector, unsafe_fixes=unsafe_fixes, ignore=ignore)
        if rules is None:
            _print_status(ruff)
            if stats_selector is not None:
                _print_statistics(
                    ruff.stats(stats_selector, unsafe_fixes=unsafe_fixes, ignore=ignore)
                )
            return ExitCode.OK
        rc = _do_fix_and_commit(repo, ruff, rules, unsafe_fixes=unsafe_fixes)
        if stats_selector is not None:
            _print_statistics(
                ruff.stats(stats_selector, unsafe_fixes=unsafe_fixes, ignore=ignore)
            )
    except RuffError as e:
        msg = str(e)
        prefix = "" if msg.lower().startswith("error") else "error: "
        print(f"{prefix}{msg}", file=sys.stderr)
        return ExitCode.RUFF_ERROR
    else:
        return rc


def _print_status(ruff: Ruff) -> None:
    """Status output for the no-rules path: format + induced-rules cleanliness."""
    formatted = ruff.format_check()
    induced = ruff.stats(",".join(RUFF_INDUCED_RULES))
    print(f"formatted: {'yes' if formatted else 'no'}")
    if not induced:
        print(f"induced rules ({', '.join(RUFF_INDUCED_RULES)}): clear")
        return
    print(f"induced rules ({', '.join(RUFF_INDUCED_RULES)}): not clear")
    for entry in sorted(induced.values(), key=lambda e: (-e.count, e.code)):
        print(f"  {entry.count}\t{entry.code}\t{entry.name}")


def _do_fix_and_commit(
    repo: git.Repo, ruff: Ruff, rules: str, *, unsafe_fixes: bool
) -> ExitCode:
    was_formatted = ruff.format_check()
    before = ruff.stats(rules, unsafe_fixes=unsafe_fixes)
    before_induced = ruff.stats(",".join(RUFF_INDUCED_RULES))

    after = ruff.fix(rules, unsafe_fixes=unsafe_fixes)
    fixed: dict[str, int] = {}
    for code, entry in before.items():
        after_entry = after.get(code)
        delta = entry.count - (after_entry.count if after_entry else 0)
        if delta > 0:
            fixed[code] = delta

    if not fixed:
        _report_nothing_fixed(ruff, rules, after, unsafe_fixes=unsafe_fixes)
        return ExitCode.OK

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
        ruff.fix(",".join(silent_codes), unsafe_fixes=unsafe_fixes)

    if was_formatted:
        ruff.format()

    repo.git.add(update=True)
    if not repo.is_dirty(working_tree=False, untracked_files=False, index=True):
        print("warning: nothing was staged after running ruff; skipping commit")
        return ExitCode.OK
    names = {code: entry.name for code, entry in before.items()}
    message = _build_message(rules, fixed, names)
    repo.index.commit(message)
    print(message)
    _print_remaining(after)
    return ExitCode.OK


def _tracked_python_files(repo: git.Repo) -> list[Path]:
    suffixes = (".py", ".pyi", ".ipynb")
    root = Path(repo.working_dir)
    submodule_prefixes = tuple(f"{sm.path}/" for sm in repo.submodules)
    paths = repo.git.ls_files().splitlines()
    return [
        root / p
        for p in paths
        if p.endswith(suffixes) and not p.startswith(submodule_prefixes)
    ]


def _report_nothing_fixed(
    ruff: Ruff, select: str, after: dict[str, RuleStat], *, unsafe_fixes: bool
) -> None:
    if not after:
        print("No matching violations.")
        return
    print("no fixes applied:")
    for entry in sorted(after.values(), key=lambda e: (-e.count, e.code)):
        print(f"{entry.count}\t{entry.code}\t{_fixability_marker(entry)} {entry.name}")
    if unsafe_fixes:
        return
    unsafe_after = ruff.stats(select, unsafe_fixes=True)
    hidden = sum(e.fixable_count for e in unsafe_after.values())
    if hidden > 0:
        plural = "es" if hidden != 1 else ""
        print(f"hint: {hidden} hidden fix{plural} can be enabled with --unsafe-fixes")


def _print_remaining(after: dict[str, RuleStat]) -> None:
    """Report any violations of the selected rules that remain after the fix.

    Uses the post-main-fix snapshot directly. The silent induced cleanup
    runs only on I001/F401 (typically not in the user's `rules`) and
    `ruff format` is shape-only, so re-querying would yield the same
    number for normal selectors. Format-sensitive rules in `rules`
    (e.g., E501) are an acceptable inaccuracy.
    """
    remaining = sum(s.count for s in after.values())
    if remaining == 0:
        return
    if remaining == 1:
        print("1 violation remains.")
    else:
        print(f"{remaining} violations remain.")


def _print_statistics(stats: dict[str, RuleStat]) -> None:
    print()
    if not stats:
        print("remaining: none")
        return
    print("remaining:")
    sorted_entries = sorted(stats.values(), key=lambda s: (-s.count, s.code))
    for s in sorted_entries:
        print(f"{s.count}\t{s.code}\t{_fixability_marker(s)} {s.name}")


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
