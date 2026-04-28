"""ruff-fix-and-commit: ruff --fix & git commit.

use `python -m ruff_fix_and_commit` to run the cli
"""

from .cli import app

app()
