"""End-to-end tests for the ruff-fix-and-commit CLI."""

from __future__ import annotations

from functools import partial
from pathlib import Path

import git
import pytest

from ruff_fix_and_commit.cli import ExitCode, app

# `result_action="return_value"` makes cyclopts hand back the command's int
# instead of `sys.exit`-ing on us; bind it once so tests just call `run([...])`.
run = partial(app, result_action="return_value")


def _make_repo(path: Path) -> git.Repo:
    repo = git.Repo.init(path, initial_branch="main")
    cw = repo.config_writer()
    cw.set_value("user", "name", "Test")
    cw.set_value("user", "email", "test@example.com")
    cw.set_value("commit", "gpgsign", "false")
    cw.set_value("tag", "gpgsign", "false")
    cw.release()
    return repo


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> git.Repo:
    monkeypatch.chdir(tmp_path)
    r = _make_repo(tmp_path)
    (tmp_path / ".gitkeep").write_text("")
    r.index.add([".gitkeep"])
    r.index.commit("init")
    return r


def add_file(repo: git.Repo, name: str, content: str) -> Path:
    p = Path(repo.working_dir) / name
    p.write_text(content)
    repo.index.add([name])
    repo.index.commit(f"add {name}")
    return p


def test_single_rule(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n    getattr(o, "b")\n')
    assert run(["--select", "B009"]) == 0
    assert (
        repo.head.commit.message.strip() == "ruff-fix: B009 (get-attr-with-constant) x2"
    )


def test_multi_rule_body_sorted_by_count(repo: git.Repo) -> None:
    add_file(
        repo,
        "t.py",
        "class C:\n"
        "    def f(self):\n"
        "        super(C, self).a\n"
        "        super(C, self).b\n"
        '        getattr(o, "x")\n'
        '        getattr(o, "y")\n'
        '        getattr(o, "z")\n',
    )
    assert run(["--select", "B009,UP008"]) == 0
    assert repo.head.commit.message.strip() == (
        "ruff-fix: B009,UP008 x5\n"
        "\n"
        "- B009 (get-attr-with-constant) x3\n"
        "- UP008 (super-call-with-parameters) x2"
    )


def test_unsafe_fixes_gating(repo: git.Repo) -> None:
    add_file(repo, "t.py", "def f():\n    z = dict()\n")
    initial = repo.head.commit.hexsha
    assert run(["--select", "C408"]) == 0
    assert repo.head.commit.hexsha == initial
    assert run(["--select", "C408", "--unsafe-fixes"]) == 0
    assert repo.head.commit.hexsha != initial
    assert (
        repo.head.commit.message.strip()
        == "ruff-fix: C408 (unnecessary-collection-call) x1"
    )


def test_unformatted_pre_state_preserved(repo: git.Repo) -> None:
    p = add_file(repo, "t.py", 'def f( ):\n    getattr( o , "a" )\n')
    assert run(["--select", "B009"]) == 0
    assert "def f( ):" in p.read_text()


def test_preexisting_i001_left_alone_when_not_selected(repo: git.Repo) -> None:
    p = add_file(
        repo,
        "t.py",
        "import sys\n"
        "import os\n"
        "\n"
        "def f():\n"
        "    print(sys.path, os.getcwd())\n"
        '    getattr(o, "a")\n',
    )
    assert run(["--select", "B009"]) == 0
    assert p.read_text().startswith("import sys\nimport os\n")


def test_preexisting_i001_fixed_and_credited_when_selected(repo: git.Repo) -> None:
    p = add_file(
        repo,
        "t.py",
        "import sys\nimport os\n\ndef f():\n    return sys.path, os.getcwd()\n",
    )
    assert run(["--select", "I001"]) == 0
    assert repo.head.commit.message.strip() == "ruff-fix: I001 (unsorted-imports) x1"
    assert p.read_text().startswith("import os\nimport sys\n")


def test_dirty_tree_refused(repo: git.Repo, capsys: pytest.CaptureFixture[str]) -> None:
    p = add_file(repo, "t.py", "def f():\n    pass\n")
    initial = repo.head.commit.hexsha
    p.write_text("def f():\n    pass\n# extra\n")
    rc = run(["--select", "B009"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "uncommitted changes" in captured.err
    assert repo.head.commit.hexsha == initial


def test_untracked_file_isolation(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    extra = Path(repo.working_dir) / "extra.py"
    extra.write_text('def g():\n    getattr(o, "b")\n')
    extra_before = extra.read_text()
    assert run(["--select", "B009"]) == 0
    assert extra.read_text() == extra_before
    diff = repo.git.show("--stat", "HEAD")
    assert "t.py" in diff
    assert "extra.py" not in diff


def test_no_fixable_violations(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", "def f(x):\n    return x + 1\n")
    initial = repo.head.commit.hexsha
    assert run(["--select", "B009"]) == 0
    assert "No matching violations" in capsys.readouterr().out
    assert repo.head.commit.hexsha == initial


def test_partial_fix_reports_remaining(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(
        repo,
        "t.py",
        'def f():\n    getattr(o, "a")\n    getattr(o, "b")\n\n'
        "g = lambda x: x + 1\nh = lambda y: y + 2\n",
    )
    assert run(["--select", "B009,E731"]) == 0
    out = capsys.readouterr().out
    # B009 was fixable (safe); E731 is unsafe-only and stays.
    assert "ruff-fix: B009 (get-attr-with-constant) x2" in out
    assert "2 violations remain" in out


def test_full_fix_no_remaining_footer(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    assert run(["--select", "B009"]) == 0
    assert "remain" not in capsys.readouterr().out


def test_unsafe_only_violations_hint_at_unsafe_fixes(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", "f = lambda x: x + 1\ng = lambda y: y * 2\n")
    initial = repo.head.commit.hexsha
    assert run(["--select", "E731"]) == 0  # E731's fix is always unsafe
    out = capsys.readouterr().out
    assert "no fixes applied" in out
    assert "E731" in out
    assert "--unsafe-fixes" in out
    assert repo.head.commit.hexsha == initial


def test_unfixable_violations_no_unsafe_fixes_hint(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", "def f(l):\n    return l + 1\n")
    initial = repo.head.commit.hexsha
    assert run(["--select", "E741"]) == 0  # E741 has no fix at all
    out = capsys.readouterr().out
    assert "no fixes applied" in out
    assert "E741" in out
    assert "--unsafe-fixes" not in out
    assert repo.head.commit.hexsha == initial


def test_statistics_shows_what_remains(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(
        repo,
        "t.py",
        'def f():\n    getattr(o, "a")\n    getattr(o, "b")\n    1\n    2\n',
    )
    assert run(["--select", "B009", "--statistics", "B"]) == 0
    out = capsys.readouterr().out
    assert "ruff-fix: B009 (get-attr-with-constant) x2" in out
    assert "remaining:" in out
    # B018 useless-expression isn't auto-fixable, so it should remain.
    stats_section = out.split("remaining:", 1)[1]
    assert "B018" in stats_section


def test_statistics_when_nothing_left(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    assert run(["--select", "B009", "--statistics", "B009"]) == 0
    assert "remaining: none" in capsys.readouterr().out


def test_statistics_default_uses_repo_selection(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(repo.working_dir)
    (root / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["B"]\n')
    repo.index.add(["pyproject.toml"])
    repo.index.commit("add config")
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n    1\n')
    assert run(["--select", "B009", "--statistics", "DEFAULT"]) == 0
    # B009 fixed; B018 (in repo's "B" selection) remains.
    assert "B018" in capsys.readouterr().out


def test_statistics_ignore_drops_matching_rules(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(
        repo,
        "t.py",
        'def f():\n    getattr(o, "a")\n    1\n',
    )
    # Without --ignore, B018 (useless-expression) would remain.
    # With --ignore B018, the stats output should show "remaining: none".
    assert run(["--select", "B009", "--statistics", "B", "--ignore", "B018"]) == 0
    assert "remaining: none" in capsys.readouterr().out


def test_statistics_default_with_extend(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(repo.working_dir)
    (root / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["B"]\n')
    repo.index.add(["pyproject.toml"])
    repo.index.commit("add config")
    add_file(
        repo,
        "t.py",
        'def f(l):\n    getattr(o, "a")\n    1\n    return l\n',
    )
    assert run(["--select", "B009", "--statistics", "DEFAULT,E"]) == 0
    out = capsys.readouterr().out
    # B018 stays from the repo's "B" selection (DEFAULT branch);
    # E741 (ambiguous variable name `l`) is added via extend.
    assert "B018" in out
    assert "E741" in out


def test_invalid_statistics_selector_runs_no_fix(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    initial = repo.head.commit.hexsha
    file_before = (Path(repo.working_dir) / "t.py").read_text()
    rc = run(["--select", "B009", "--statistics", "F731"])
    err = capsys.readouterr().err
    assert rc == ExitCode.RUFF_ERROR
    assert "F731" in err
    # The fix run is gated on the stats selector being valid.
    assert repo.head.commit.hexsha == initial
    assert (Path(repo.working_dir) / "t.py").read_text() == file_before


def test_invalid_selector_returns_error(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    initial = repo.head.commit.hexsha
    rc = run(["--select", "F731"])  # F731 is not a valid selector
    err = capsys.readouterr().err
    assert rc == ExitCode.RUFF_ERROR
    assert "F731" in err
    assert repo.head.commit.hexsha == initial


def test_ruff_chatter_silenced(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n    getattr(o, "b")\n')
    assert run(["--select", "B009"]) == 0
    captured = capsys.readouterr()
    # Our user-facing output should be just the commit message; ruff's own
    # progress chatter should not leak through.
    for noise in ("Found ", "All checks passed", "files left unchanged"):
        assert noise not in captured.out, f"unexpected ruff output: {noise!r}"
        assert noise not in captured.err, f"unexpected ruff output: {noise!r}"


def test_status_mode_clean_repo(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", "def f():\n    return 1\n")
    initial = repo.head.commit.hexsha
    assert run([]) == 0
    out = capsys.readouterr().out
    assert "formatted: yes" in out
    assert "I001 unsorted-imports: clean" in out
    assert "F401 unused-import: clean" in out
    assert repo.head.commit.hexsha == initial


def test_status_mode_unformatted_and_induced_violations(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(
        repo,
        "t.py",
        "import sys\nimport os\n\n\ndef f( ):\n    return sys.path, os.getcwd()\n",
    )
    initial = repo.head.commit.hexsha
    assert run([]) == 0
    out = capsys.readouterr().out
    assert "formatted: no" in out
    assert "I001 unsorted-imports: 1" in out
    assert "F401 unused-import: clean" in out
    assert repo.head.commit.hexsha == initial


def test_status_mode_with_statistics(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n    1\n')
    initial = repo.head.commit.hexsha
    assert run(["--statistics", "B"]) == 0
    out = capsys.readouterr().out
    assert "formatted:" in out
    assert "remaining:" in out
    # No fix happened, so B009 should still be there.
    assert "B009" in out
    assert repo.head.commit.hexsha == initial


def test_status_mode_runs_on_dirty_tree(
    repo: git.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    p = add_file(repo, "t.py", "def f():\n    return 1\n")
    p.write_text("def f():\n    return 1\n# extra\n")
    initial = repo.head.commit.hexsha
    assert run([]) == 0
    assert "formatted:" in capsys.readouterr().out
    assert repo.head.commit.hexsha == initial


def test_target_restricts_to_subdirectory(repo: git.Repo) -> None:
    root = Path(repo.working_dir)
    (root / "src").mkdir()
    (root / "other").mkdir()
    add_file(repo, "src/a.py", 'def f():\n    getattr(o, "a")\n')
    add_file(repo, "other/b.py", 'def g():\n    getattr(o, "b")\n')
    other_before = (root / "other" / "b.py").read_text()
    assert run(["src/", "--select", "B009"]) == 0
    assert (root / "other" / "b.py").read_text() == other_before
    diff = repo.git.show("--stat", "HEAD")
    assert "src/a.py" in diff
    assert "other/b.py" not in diff


def test_submodule_files_not_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = tmp_path / "inner"
    inner.mkdir()
    inner_repo = _make_repo(inner)
    (inner / "inner.py").write_text('def g():\n    getattr(o, "inner")\n')
    inner_repo.index.add(["inner.py"])
    inner_repo.index.commit("init")

    outer = tmp_path / "outer"
    outer.mkdir()
    outer_repo = _make_repo(outer)
    (outer / "outer.py").write_text('def f():\n    getattr(o, "outer")\n')
    outer_repo.index.add(["outer.py"])
    outer_repo.index.commit("init")

    # `-c protocol.file.allow=always` is git-level (not a submodule
    # subcommand option), so go through GitPython's low-level execute().
    outer_repo.git.execute(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(inner),
            "sub",
        ],
    )
    outer_repo.git.commit("-m", "add submodule")

    inner_path = outer / "sub" / "inner.py"
    inner_before = inner_path.read_text()
    # Capture the submodule gitlink (a "160000 commit <sha> sub" entry) so
    # we can assert `git add -u` did NOT move it after the fix.
    sub_gitlink_before = outer_repo.git.ls_tree("HEAD", "sub")

    monkeypatch.chdir(outer)
    assert run(["--select", "B009"]) == 0
    assert inner_path.read_text() == inner_before
    diff = outer_repo.git.show("--stat", "HEAD")
    assert "outer.py" in diff
    assert "inner.py" not in diff
    # `git add -u` must not touch the submodule gitlink.
    sub_gitlink_after = outer_repo.git.ls_tree("HEAD", "sub")
    assert sub_gitlink_before == sub_gitlink_after
