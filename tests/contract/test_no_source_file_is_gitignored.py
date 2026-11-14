"""Every source file in the repository must actually be in the repository.

⚠ Two were not, for a whole day, and nothing noticed.

`.gitignore` carried a bare `data/` to exclude the repo-root cache directory.
A pattern without a leading slash is matched at every depth, so it also
ignored `packages/py/reimagina/data/` — a Python package. The files already
there stayed tracked, because gitignore does not untrack anything, so
`data/feature.py`, `data/aoi.py` and the rest kept working and the breakage
was invisible.

Two *new* modules in that package were silently skipped by `git add -A`:
`data/roads.py` (the street-context projection) and `data/attribution.py`
(licence attribution). Both were written, tested, committed around — their
tests are in the repository and passed, because the files existed on the
machine that wrote them. The first fresh clone to run `network bootstrap`
got:

    ModuleNotFoundError: No module named 'reimagina.data.roads'

Local tests cannot catch this by construction: the file is present locally.
Only asking git can.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Trees whose source is part of the delivered product.
_SOURCE_TREES = ("packages", "apps", "infra", "scripts", "tests")

#: Extensions that are source rather than artifact.
_SOURCE_SUFFIXES = {".py", ".sql", ".sh", ".ts", ".tsx", ".mmd"}

#: Directories that legitimately hold untracked content inside those trees.
_EXCLUDED_PARTS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "test-results",
    "playwright-report",
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def tracked() -> set[str]:
    return set(_git("ls-files").splitlines())


def _candidates() -> list[Path]:
    found = []
    for tree in _SOURCE_TREES:
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
                continue
            if _EXCLUDED_PARTS & set(path.relative_to(REPO_ROOT).parts):
                continue
            found.append(path)
    return found


def test_there_are_source_files_to_check():
    """Guards the guard: a broken walk would pass everything silently."""
    assert len(_candidates()) > 100


def test_every_source_file_on_disk_is_tracked_by_git(tracked):
    missing = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in _candidates()
        if str(p.relative_to(REPO_ROOT)) not in tracked
    )
    assert not missing, (
        "these source files exist locally but are not in the repository, so a "
        f"fresh clone does not have them: {missing}. If `git check-ignore -v "
        "<path>` names a .gitignore rule, the rule is too broad — a bare "
        "`name/` pattern matches at every depth, not just the repo root."
    )


def test_the_data_ignore_rule_is_anchored_to_the_repo_root():
    """The specific rule that caused it, pinned.

    `/data/` is the repo-root cache directory. `data/` is also
    `packages/py/reimagina/data/`, which is a Python package.
    """
    lines = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "/data/" in lines, "the repo-root data cache must still be ignored"
    assert "data/" not in lines, (
        "a bare `data/` pattern matches at every depth and will ignore "
        "packages/py/reimagina/data/ — the Python package. Anchor it as "
        "`/data/`."
    )


def test_the_modules_this_was_found_by_are_tracked(tracked):
    """Named explicitly, because a regex is easy to make pass by accident."""
    for path in (
        "packages/py/reimagina/data/roads.py",
        "packages/py/reimagina/data/attribution.py",
    ):
        assert path in tracked, f"{path} is not in the repository"
