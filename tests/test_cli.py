"""End-to-end tests for the ruff-fix-and-commit CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import git
import pytest

RFC = str(Path(sys.executable).parent / "ruff-fix-and-commit")


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
def repo(tmp_path: Path) -> git.Repo:
    r = _make_repo(tmp_path)
    (tmp_path / ".gitkeep").write_text("")
    r.index.add([".gitkeep"])
    r.index.commit("init")
    return r


def run_rfc(repo: git.Repo, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [RFC, *args],
        cwd=repo.working_dir,
        capture_output=True,
        text=True,
        check=False,
    )


def add_file(repo: git.Repo, name: str, content: str) -> Path:
    p = Path(repo.working_dir) / name
    p.write_text(content)
    repo.index.add([name])
    repo.index.commit(f"add {name}")
    return p


def test_single_rule(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n    getattr(o, "b")\n')
    r = run_rfc(repo, "B009")
    assert r.returncode == 0, r.stderr
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
    r = run_rfc(repo, "B009,UP008")
    assert r.returncode == 0, r.stderr
    assert repo.head.commit.message.strip() == (
        "ruff-fix: B009,UP008\n"
        "\n"
        "- B009 (get-attr-with-constant) x3\n"
        "- UP008 (super-call-with-parameters) x2"
    )


def test_unsafe_fixes_gating(repo: git.Repo) -> None:
    add_file(repo, "t.py", "def f():\n    z = dict()\n")
    initial = repo.head.commit.hexsha
    r = run_rfc(repo, "C408")
    assert r.returncode == 0, r.stderr
    assert repo.head.commit.hexsha == initial
    r = run_rfc(repo, "C408", "--unsafe-fixes")
    assert r.returncode == 0, r.stderr
    assert repo.head.commit.hexsha != initial
    assert (
        repo.head.commit.message.strip()
        == "ruff-fix: C408 (unnecessary-collection-call) x1"
    )


def test_unformatted_pre_state_preserved(repo: git.Repo) -> None:
    p = add_file(repo, "t.py", 'def f( ):\n    getattr( o , "a" )\n')
    r = run_rfc(repo, "B009")
    assert r.returncode == 0, r.stderr
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
    r = run_rfc(repo, "B009")
    assert r.returncode == 0, r.stderr
    assert p.read_text().startswith("import sys\nimport os\n")


def test_preexisting_i001_fixed_and_credited_when_selected(repo: git.Repo) -> None:
    p = add_file(
        repo,
        "t.py",
        "import sys\nimport os\n\ndef f():\n    return sys.path, os.getcwd()\n",
    )
    r = run_rfc(repo, "I001")
    assert r.returncode == 0, r.stderr
    assert repo.head.commit.message.strip() == "ruff-fix: I001 (unsorted-imports) x1"
    assert p.read_text().startswith("import os\nimport sys\n")


def test_dirty_tree_refused(repo: git.Repo) -> None:
    p = add_file(repo, "t.py", "def f():\n    pass\n")
    initial = repo.head.commit.hexsha
    p.write_text("def f():\n    pass\n# extra\n")
    r = run_rfc(repo, "B009")
    assert r.returncode == 1
    assert "uncommitted changes" in r.stderr
    assert repo.head.commit.hexsha == initial


def test_untracked_file_isolation(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    extra = Path(repo.working_dir) / "extra.py"
    extra.write_text('def g():\n    getattr(o, "b")\n')
    extra_before = extra.read_text()
    r = run_rfc(repo, "B009")
    assert r.returncode == 0, r.stderr
    assert extra.read_text() == extra_before
    diff = repo.git.show("--stat", "HEAD")
    assert "t.py" in diff
    assert "extra.py" not in diff


def test_no_fixable_violations(repo: git.Repo) -> None:
    add_file(repo, "t.py", "def f(x):\n    return x + 1\n")
    initial = repo.head.commit.hexsha
    r = run_rfc(repo, "B009")
    assert r.returncode == 0, r.stderr
    assert "Nothing to fix" in r.stdout
    assert repo.head.commit.hexsha == initial


def test_statistics_shows_what_remains(repo: git.Repo) -> None:
    add_file(
        repo,
        "t.py",
        'def f():\n    getattr(o, "a")\n    getattr(o, "b")\n    1\n    2\n',
    )
    r = run_rfc(repo, "B009", "--statistics", "B")
    assert r.returncode == 0, r.stderr
    assert "ruff-fix: B009 (get-attr-with-constant) x2" in r.stdout
    assert "remaining:" in r.stdout
    # B018 useless-expression isn't auto-fixable, so it should remain.
    stats_section = r.stdout.split("remaining:", 1)[1]
    assert "B018" in stats_section


def test_statistics_when_nothing_left(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    r = run_rfc(repo, "B009", "--statistics", "B009")
    assert r.returncode == 0, r.stderr
    assert "remaining: none" in r.stdout


def test_invalid_statistics_selector_runs_no_fix(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    initial = repo.head.commit.hexsha
    file_before = (Path(repo.working_dir) / "t.py").read_text()
    r = run_rfc(repo, "B009", "--statistics", "F731")
    assert r.returncode == 2
    assert "F731" in r.stderr
    # The fix run is gated on the stats selector being valid.
    assert repo.head.commit.hexsha == initial
    assert (Path(repo.working_dir) / "t.py").read_text() == file_before


def test_invalid_selector_returns_error(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n')
    initial = repo.head.commit.hexsha
    r = run_rfc(repo, "F731")  # F731 is not a valid selector
    assert r.returncode == 2, r.stderr
    assert "F731" in r.stderr
    assert repo.head.commit.hexsha == initial


def test_ruff_chatter_silenced(repo: git.Repo) -> None:
    add_file(repo, "t.py", 'def f():\n    getattr(o, "a")\n    getattr(o, "b")\n')
    r = run_rfc(repo, "B009")
    assert r.returncode == 0, r.stderr
    # Our user-facing output should be just the commit message; ruff's own
    # progress chatter should not leak through.
    for noise in ("Found ", "All checks passed", "files left unchanged"):
        assert noise not in r.stdout, f"unexpected ruff output: {noise!r}"
        assert noise not in r.stderr, f"unexpected ruff output: {noise!r}"


def test_submodule_files_not_modified(tmp_path: Path) -> None:
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

    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(inner),
            "sub",
        ],
        cwd=str(outer),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add submodule"],
        cwd=str(outer),
        check=True,
        capture_output=True,
    )

    inner_path = outer / "sub" / "inner.py"
    inner_before = inner_path.read_text()

    r = subprocess.run(
        [RFC, "B009"],
        cwd=str(outer),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert inner_path.read_text() == inner_before
    diff = outer_repo.git.show("--stat", "HEAD")
    assert "outer.py" in diff
    assert "inner.py" not in diff
