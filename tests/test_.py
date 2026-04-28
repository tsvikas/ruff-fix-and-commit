import importlib.metadata

import ruff_fix_and_commit


def test_version() -> None:
    assert (
        importlib.metadata.version("ruff_fix_and_commit")
        == ruff_fix_and_commit.__version__
    )
