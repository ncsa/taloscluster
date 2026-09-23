"""Guards that CI enforces the project's own checks.

The tests workflow once ran only pytest on a single Python: ruff, mypy and the
strict docs build stayed local-only, so lint, typing and documentation
regressions reached main, and the supported Python floor (``requires-python
>=3.10``) was never exercised. Both workflows installed the environment with a
bare ``uv sync``, so a lockfile left behind by a dependency edit went unnoticed
until the next resolve. mkdocs floated unbounded although MkDocs 2.0 will break
the plugins mkdocs-material drives. Each test below pins one of those shapes.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS_WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
DOCS_WORKFLOW = (ROOT / ".github" / "workflows" / "docs.yml").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()


def test_tests_workflow_runs_the_project_checks():
    # pytest alone let ruff, mypy and docs-build regressions reach main
    assert "uv run ruff check ." in TESTS_WORKFLOW
    assert "uv run mypy" in TESTS_WORKFLOW
    assert "uv run mkdocs build --strict" in TESTS_WORKFLOW


def test_workflows_install_from_the_lockfile():
    # a bare `uv sync` re-resolves instead of failing on a stale uv.lock
    unlocked = {
        name: [
            line.strip()
            for line in text.splitlines()
            if "uv sync" in line and "--locked" not in line
        ]
        for name, text in (("tests.yml", TESTS_WORKFLOW), ("docs.yml", DOCS_WORKFLOW))
    }
    assert not any(unlocked.values()), f"unlocked uv sync steps: {unlocked}"


def test_tests_workflow_runs_the_supported_python_floor():
    # requires-python is >=3.10; the matrix must exercise that floor, not only
    # the interpreter the maintainers happen to run
    match = re.search(r"python-version:\s*\[([^\]]+)\]", TESTS_WORKFLOW)
    assert match, "tests workflow has no python-version matrix"
    assert "3.10" in match.group(1), match.group(1)


def test_dev_extra_bounds_mkdocs_below_mkdocs_2():
    # MkDocs 2.0 will break the plugins mkdocs-material drives; with the upper
    # bound declared, `uv lock` refuses a resolve that floats onto it
    assert re.search(r'"mkdocs(?!-)[^"]*<2"', PYPROJECT)
