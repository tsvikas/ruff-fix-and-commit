"""ruff-fix-and-commit: run `ruff check --fix` for selected rules and commit."""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
from collections import Counter

import cyclopts
import git

app = cyclopts.App(name="ruff-fix-and-commit")

_RUFF_ENV = {k: v for k, v in os.environ.items() if k != "RUFF_OUTPUT_FORMAT"}


@app.default
def main(rules: str, *, unsafe_fixes: bool = False) -> int:
    """Run `ruff check --fix` for RULES and commit the changes.

    Parameters
    ----------
    rules:
        Comma-separated ruff rule selectors (codes or category prefixes),
        passed verbatim to `ruff --select`. Example: `A,B001,C212`.
    unsafe_fixes:
        Forwarded to ruff as `--unsafe-fixes`.
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

    was_formatted = _format_check_clean(targets)
    had_i001_pre = bool(_violations("I001", targets))
    had_f401_pre = bool(_violations("F401", targets))

    before_counts = Counter(v["code"] for v in _violations(rules, targets))

    fix_cmd = ["ruff", "check", "--select", rules, "--fix"]
    if unsafe_fixes:
        fix_cmd.append("--unsafe-fixes")
    fix_cmd.extend(targets)
    subprocess.run(fix_cmd, check=False, env=_RUFF_ENV)

    after_counts = Counter(v["code"] for v in _violations(rules, targets))
    fixed: dict[str, int] = {}
    for code, before in before_counts.items():
        delta = before - after_counts.get(code, 0)
        if delta > 0:
            fixed[code] = delta

    if not fixed:
        print("Nothing to fix.")
        return 0

    silent_codes: list[str] = []
    if not had_i001_pre:
        silent_codes.append("I001")
    if not had_f401_pre:
        silent_codes.append("F401")
    if silent_codes:
        subprocess.run(
            ["ruff", "check", "--select", ",".join(silent_codes), "--fix", *targets],
            check=False,
            env=_RUFF_ENV,
        )

    if was_formatted:
        subprocess.run(["ruff", "format", *targets], check=False, env=_RUFF_ENV)

    repo.git.add(update=True)
    if not repo.is_dirty(working_tree=False, untracked_files=False, index=True):
        print("warning: nothing was staged after running ruff; skipping commit")
        return 0
    message = _build_message(rules, fixed)
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
    result = subprocess.run(
        ["ruff", "format", "--check", *targets],
        capture_output=True,
        text=True,
        check=False,
        env=_RUFF_ENV,
    )
    return result.returncode == 0


def _violations(select: str, targets: list[str]) -> list[dict]:
    result = subprocess.run(
        [
            "ruff",
            "check",
            "--select",
            select,
            "--output-format",
            "json",
            "--no-fix",
            *targets,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_RUFF_ENV,
    )
    if not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


@functools.cache
def _rule_name(code: str) -> str:
    result = subprocess.run(
        ["ruff", "rule", code, "--output-format", "json"],
        capture_output=True,
        text=True,
        check=False,
        env=_RUFF_ENV,
    )
    if result.returncode != 0:
        return ""
    try:
        return json.loads(result.stdout).get("name", "")
    except json.JSONDecodeError:
        return ""


def _build_message(rules_input: str, fixed: dict[str, int]) -> str:
    items = sorted(fixed.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) == 1:
        code, count = items[0]
        return f"ruff-fix: {code} ({_rule_name(code)}) x{count}"
    lines = [f"ruff-fix: {rules_input}", ""]
    lines.extend(f"- {code} ({_rule_name(code)}) x{count}" for code, count in items)
    return "\n".join(lines)


def _entry() -> None:
    raise SystemExit(app() or 0)


if __name__ == "__main__":
    _entry()
