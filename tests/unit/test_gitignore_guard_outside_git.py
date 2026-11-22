"""The gitignore contract skips, not errors, in a tree without git metadata.

A source archive has no `.git`; `git ls-files` then fails and the DB-free
gate the README points to would be red on a pristine tree.
"""

import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "contract"
    / "test_no_source_file_is_gitignored.py"
)


def test_git_backed_checks_skip_outside_a_git_checkout(tmp_path):
    (tmp_path / "tests" / "contract").mkdir(parents=True)
    copy = tmp_path / "tests" / "contract" / SOURCE.name
    shutil.copy(SOURCE, copy)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(copy),
            "-k",
            "tracked",
            "--rootdir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
    )
    assert "2 skipped" in result.stdout, result.stdout + result.stderr
