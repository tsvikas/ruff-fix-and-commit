"""ruff-fix-and-commit: run `ruff check --fix` for selected rules and commit."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import cyclopts
import git

app = cyclopts.App(name="ruff-fix-and-commit")

_RUFF_ENV = {k: v for k, v in os.environ.items() if k != "RUFF_OUTPUT_FORMAT"}

_DEFAULT_SENTINEL = "DEFAULT"


class RuffError(Exception):
    """ruff exited with an unexpected status (config/usage error, not violations)."""


def _run_ruff(
    args: list[str], *, allow_violations: bool = False
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


@app.default
def main(
    rules: str,
    *,
    unsafe_fixes: bool = False,
    statistics: str | None = None,
) -> int:
    """Run `ruff check --fix` for RULES and commit the changes.

    Parameters
    ----------
    rules:
        Comma-separated ruff rule selectors (codes or category prefixes),
        passed verbatim to `ruff --select`. Example: `A,B001,C212`.
    unsafe_fixes:
        Forwarded to ruff as `--unsafe-fixes`.
    statistics:
        After the fix, run `ruff check --select STATISTICS --statistics`
        and print a per-rule count of what's still left. Pass `DEFAULT`
        to omit `--select` and use the repo's configured rule selection.
        Validated up front so a typo here doesn't waste a fix run.
    """
    try:
        repo = git.Repo(".", search_parent_directories=True)
    except git.InvalidGitRepositoryError:
        print("error: not inside a git repository", file=sys.stderr)
        return 1
    if repo.is_dirty(untracked_files=False):
        print(
            "error: working tree has uncommitted changes to tracked files; "
            "commit or stash them first",
            file=sys.stderr,
        )
        return 1

    targets = _tracked_python_files(repo)
    if not targets:
        print("Nothing to fix.")
        return 0

    try:
        if statistics is not None:
            # Validate up front (cheap call); raises RuffError if selector is bad.
            _stats(statistics, targets, unsafe_fixes=unsafe_fixes)
        rc = _do_fix_and_commit(repo, rules, targets, unsafe_fixes=unsafe_fixes)
        if statistics is not None:
            _print_statistics(statistics, targets, unsafe_fixes=unsafe_fixes)
        return rc
    except RuffError as e:
        msg = str(e)
        prefix = "" if msg.lower().startswith("error") else "error: "
        print(f"{prefix}{msg}", file=sys.stderr)
        return 2


def _do_fix_and_commit(
    repo: git.Repo, rules: str, targets: list[str], *, unsafe_fixes: bool
) -> int:
    was_formatted = _format_check_clean(targets)
    had_i001_pre = bool(_stats("I001", targets))
    had_f401_pre = bool(_stats("F401", targets))

    before = _stats(rules, targets)

    # Apply the fix and capture after-stats in the same call.
    fix_args = [
        "check",
        "--select",
        rules,
        "--fix",
        "--statistics",
        "--output-format",
        "json",
    ]
    if unsafe_fixes:
        fix_args.append("--unsafe-fixes")
    fix_args.extend(targets)
    result = _run_ruff(fix_args, allow_violations=True)
    after = _parse_stats(result.stdout)
    fixed: dict[str, int] = {}
    for code, entry in before.items():
        delta = entry["count"] - after.get(code, {"count": 0})["count"]
        if delta > 0:
            fixed[code] = delta

    if not fixed:
        _report_nothing_fixed(rules, targets, after, unsafe_fixes=unsafe_fixes)
        return 0

    silent_codes: list[str] = []
    if not had_i001_pre:
        silent_codes.append("I001")
    if not had_f401_pre:
        silent_codes.append("F401")
    if silent_codes:
        _run_ruff(
            ["check", "--select", ",".join(silent_codes), "--fix", *targets],
            allow_violations=True,
        )

    if was_formatted:
        _run_ruff(["format", *targets])

    repo.git.add(update=True)
    if not repo.is_dirty(working_tree=False, untracked_files=False, index=True):
        print("warning: nothing was staged after running ruff; skipping commit")
        return 0
    names = {code: entry["name"] for code, entry in before.items()}
    message = _build_message(rules, fixed, names)
    repo.index.commit(message)
    print(message)
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


def _format_check_clean(targets: list[str]) -> bool:
    result = _run_ruff(["format", "--check", *targets], allow_violations=True)
    return result.returncode == 0


def _stats(
    select: str, targets: list[str], *, unsafe_fixes: bool = False
) -> dict[str, dict]:
    """Per-rule stats: ``{code: {code, name, count, fixable, fixable_count}}``.

    A `select` of ``"DEFAULT"`` (case-insensitive) omits ``--select`` so
    ruff uses the repo's configured rule selection.
    """
    args = ["check", "--statistics", "--no-fix", "--output-format", "json"]
    if select.upper() != _DEFAULT_SENTINEL:
        args.extend(["--select", select])
    if unsafe_fixes:
        args.append("--unsafe-fixes")
    args.extend(targets)
    result = _run_ruff(args, allow_violations=True)
    return _parse_stats(result.stdout)


def _parse_stats(stdout: str) -> dict[str, dict]:
    if not stdout.strip():
        return {}
    try:
        return {entry["code"]: entry for entry in json.loads(stdout)}
    except json.JSONDecodeError:
        return {}


def _report_nothing_fixed(
    select: str, targets: list[str], after: dict[str, dict], *, unsafe_fixes: bool
) -> None:
    if not after:
        print("Nothing to fix.")
        return
    print("no fixes applied:")
    for entry in sorted(after.values(), key=lambda e: (-e["count"], e["code"])):
        marker = "[*]" if entry["fixable"] else "[ ]"
        print(f"{entry['count']}\t{entry['code']}\t{marker} {entry['name']}")
    if unsafe_fixes:
        return
    unsafe_after = _stats(select, targets, unsafe_fixes=True)
    hidden = sum(e["fixable_count"] for e in unsafe_after.values())
    if hidden > 0:
        plural = "es" if hidden != 1 else ""
        print(f"hint: {hidden} hidden fix{plural} can be enabled with --unsafe-fixes")


def _print_statistics(select: str, targets: list[str], *, unsafe_fixes: bool) -> None:
    stats = _stats(select, targets, unsafe_fixes=unsafe_fixes)
    print()
    if not stats:
        print("remaining: none")
        return
    print("remaining:")
    sorted_entries = sorted(stats.values(), key=lambda s: (-s["count"], s["code"]))
    for s in sorted_entries:
        marker = "[*]" if s["fixable"] else "[ ]"
        print(f"{s['count']}\t{s['code']}\t{marker} {s['name']}")


def _build_message(
    rules_input: str, fixed: dict[str, int], names: dict[str, str]
) -> str:
    items = sorted(fixed.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) == 1:
        code, count = items[0]
        return f"ruff-fix: {code} ({names.get(code, '')}) x{count}"
    lines = [f"ruff-fix: {rules_input}", ""]
    lines.extend(f"- {code} ({names.get(code, '')}) x{count}" for code, count in items)
    return "\n".join(lines)


def _entry() -> None:
    raise SystemExit(app() or 0)


if __name__ == "__main__":
    _entry()
