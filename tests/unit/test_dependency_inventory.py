"""The dependency inventory must describe the tree it claims to describe.

Two ways it could lie quietly. A nested npm package whose manifest omits
`license` was looked up at the wrong path, so its licence file was never read
and the row said `UNRESOLVED` with the file sitting right there. And an
ambiguous `.venv` — two interpreter directories after a Python bump — was
resolved by taking the first in sorted order, which is the *older* one.

Both are "wrong but plausible" outputs, which is the failure mode a licence
inventory can least afford.
"""

from __future__ import annotations

import json

import pytest

import scripts.dependency_inventory as inventory


def test_a_nested_package_resolves_its_licence_file(tmp_path, monkeypatch):
    """A lockfile key for a nested dependency is `node_modules/a/node_modules/b`.

    The old lookup joined `node_modules` to the bare *name*, so it checked
    `node_modules/b` — which for a nested install does not exist — and reported
    `UNRESOLVED` while the licence file sat at the key's real path. The key is
    already the path relative to `apps/web`, so it is what must be used.

    Unreachable on today's lockfile: exactly one of its 194 packages omits
    `license`, and that one is a top-level key where the old path happened to be
    correct. So this test is the only thing that proves the fix.

    `LICENCE`, with the British spelling, because the helper's patterns had to
    widen for that too — the Article 4.1 generator already handled it and this
    script did not.
    """
    web = tmp_path / "apps" / "web"
    nested = web / "node_modules" / "a" / "node_modules" / "b"
    nested.mkdir(parents=True)
    (nested / "LICENCE").write_text("MIT License\n\nCopyright (c) nobody\n")
    (web / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "root"},
                    "node_modules/a": {"version": "2.0.0", "license": "ISC"},
                    "node_modules/a/node_modules/b": {"version": "1.0.0"},
                }
            }
        )
    )
    monkeypatch.setattr(inventory, "ROOT", tmp_path)

    production, _dev = inventory.npm_packages()

    row = next(r for r in production if r[0] == "b")
    assert "MIT License" in row[2], row
    assert "from LICENSE file" in row[2], (
        "the entry must say the licence came from a file, not from the manifest"
    )


def test_two_interpreter_directories_are_an_error(tmp_path, monkeypatch):
    """`sorted(...)[0]` picked `python3.12` over `python3.13` after a bump.

    String order puts the older directory first, so the inventory was generated
    from the stale tree and said so nowhere. Picking by `sys.version_info`
    instead would hide the staleness rather than report it; an ambiguous venv is
    an operator problem with a one-line fix.
    """
    for version in ("python3.12", "python3.13"):
        (tmp_path / ".venv" / "lib" / version / "site-packages").mkdir(parents=True)
    monkeypatch.setattr(inventory, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as caught:
        inventory._site_packages()

    message = str(caught.value)
    assert "python3.12" in message and "python3.13" in message, (
        f"the error must name every candidate it refused to choose between: {message}"
    )


def test_one_interpreter_directory_is_fine(tmp_path, monkeypatch):
    """The control: the unambiguous case must not have become an error."""
    expected = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    expected.mkdir(parents=True)
    monkeypatch.setattr(inventory, "ROOT", tmp_path)

    assert inventory._site_packages() == expected
