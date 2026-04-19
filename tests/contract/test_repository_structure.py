from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_expected_top_level_structure_exists():
    expected = [
        ROOT / "apps",
        ROOT / "packages" / "py" / "reimagina",
        ROOT / "packages" / "py" / "reimagina" / "storage",
        ROOT / "packages" / "py" / "reimagina" / "perf",
        ROOT / "packages" / "py" / "reimagina" / "tiles",
        ROOT / "packages" / "ts",
        ROOT / "infra" / "db" / "migrations",
        ROOT / "infra" / "db" / "migrations" / "common",
        ROOT / "infra" / "db" / "migrations" / "duckdb",
        ROOT / "infra" / "db" / "migrations" / "postgres",
        ROOT / "infra" / "db" / "seeds",
        ROOT / "infra" / "db" / "capability_matrix.md",
        ROOT / "infra" / "tiles",
        ROOT / "docs" / "architecture",
        ROOT / "tests" / "contract",
        ROOT / "tests" / "performance",
        ROOT / "tests" / "e2e",
    ]
    for path in expected:
        assert path.exists(), f"Missing expected path: {path}"


def test_api_worker_cli_scaffolds_exist():
    assert (ROOT / "apps" / "api" / "app.py").exists()
    assert (ROOT / "apps" / "worker" / "app.py").exists()
    assert (ROOT / "apps" / "cli" / "app.py").exists()


def test_explore_module_is_not_used_by_domain_logic_outside_cli():
    disallowed_import = re.compile(r"(from\s+reimagina\.data\s+import\s+explore|import\s+reimagina\.data\.explore)")
    allowed_paths = {
        ROOT / "packages" / "py" / "reimagina" / "data" / "explore.py",
        ROOT / "apps" / "cli" / "app.py",
        ROOT / "apps" / "cli" / "explore.py",
    }
    for path in (ROOT / "packages" / "py" / "reimagina").rglob("*.py"):
        if path in allowed_paths:
            continue
        content = path.read_text()
        assert not disallowed_import.search(content), f"Unexpected explore dependency in {path}"
