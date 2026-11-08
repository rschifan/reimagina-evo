# Spec: layer-bootstrap de-duplication

Extract the two blocks of bookkeeping that the ingest entry points repeat
verbatim, and close the test gap the extraction makes visible.

**Status:** proposed. No code is written until this is approved.

**Scope:** `packages/py/reimagina/pipeline/` and one new integration test.
No migration, no schema change, no change to `serving_geo` or `explore`, so
the maintenance checklist in `AGENTS.md` reduces to step 5 (add tests) and
step 6 (this document).

**Honest scale, stated up front:** about 15 lines of production code go away.
The deliverable worth having is the missing `cdmx.bootstrap_layers` test.
If the test is not part of this change, the change is not worth making.

---

## Why this and nothing else

Approach A — extract only what is provably identical — was chosen after
measuring, not before:

- dead code across roughly 36k lines came to **one** unused variable, since
  fixed
- duplication in `apps/web` measured **0.28%**

Both figures are carried over from that earlier measurement pass; the dead
variable is gone, so neither is re-runnable from this tree as it stands. The
duplication claims in the next section were re-verified against the current
tree and are not inherited.

So there is no large de-duplication available in this repository, and a spec
that promised one would be fiction. What is left is a single real cluster:
the bookkeeping on top of the ingest entry points.

---

## What is actually duplicated

Two blocks. Both verified against the current tree, not assumed.

### 1. Validate-and-order — three sites

`cdmx.py:280-284`, `inegi_marco.py:338-342`, `points.py:228-232`:

```python
requested = list(layers) if layers else list(DEFAULT_LOAD_ORDER)
unknown = [k for k in requested if k not in LAYER_SPECS]
if unknown:
    raise ValueError(f"Unknown CDMX layer key(s): {unknown}")
ordered = [k for k in DEFAULT_LOAD_ORDER if k in requested]
```

Character-identical at all three sites but for the `ValueError` wording:
`"Unknown CDMX layer key(s)"`, `"Unknown INEGI marco layer key(s)"`,
`"Unknown amenity layer key(s)"`. The wording is load-bearing — it is what
tells an operator which pipeline rejected their `--layers` — so it becomes a
parameter, not a casualty.

`inegi_censo.bootstrap` is **not** a site: it takes no `layers` argument.

### 2. Source-root resolution — three sites

`points.py:234-239`, `inegi_marco.py:344-349`, `inegi_censo.py:304-309`:

```python
if source_root is not None:
    root = Path(source_root)
else:
    root = DEFAULT_SOURCE_ROOT
if not root.is_absolute():
    root = REPO_ROOT / root
```

Byte-identical at all three sites, confirmed by `diff`. Each module supplies
its own `DEFAULT_SOURCE_ROOT` (`data/sources/azure`,
`.../marco_geostadistico/2024`, `.../censo/manzanas_2020.parquet`), while
`REPO_ROOT` is the same symbol imported from `reimagina.paths` in all three —
so only the default needs to be a parameter.

> ⚠ **Correction to the brief this spec came from,** which put this block at
> two sites and named only `points` and `inegi_marco`. It is three:
> `inegi_censo` carries it too. The helper therefore has one more caller than
> the approved sketch assumed, which is the one fact that makes the
> extraction slightly more worthwhile than advertised — not less.

---

## The change

A new package-private module, `packages/py/reimagina/pipeline/_layer_bootstrap.py`:

```python
def select_layers(
    requested: list[str] | None,
    *,
    specs: Mapping[str, object],
    load_order: Sequence[str],
    label: str,
) -> list[str]:
    """Validate requested layer keys and return them in load order."""


def resolve_source_root(source_root: Path | None, default_root: Path) -> Path:
    """Resolve a source root to an absolute path, relative to the repo root."""
```

Constraints on the change:

- **nothing is added to `pipeline/__init__.py`.** The leading underscore and
  the absent export are the whole statement: this is bookkeeping shared
  between siblings, not a public surface.
- **all four public signatures stay exactly as they are**, so `apps/cli`
  does not move and the CLI contract is untouched.
- `select_layers` keeps `specs` typed as a `Mapping` of key to *something*,
  because it only ever asks whether a key is present. It never touches a
  spec's fields, so no per-module spec class enters the helper.

---

## What is deliberately not merged

The per-layer loops stay where they are, in all four modules.

They look similar and are not. `ensure_layer_source`, `stage_layer` and
`load_layer` are per-module functions typed against per-module spec classes
(`CDMXLayerSpec` and its siblings), and the loop bodies genuinely differ:
`cdmx` fetches and resolves an archive and writes a bronze URI;
`inegi_marco` threads `cve_ent_filter` through staging; `points` does
neither.

Merging them needs roughly four callbacks and forces `Any` through the
helper, which trades away mypy coverage the repo currently keeps clean across
147 source files, in exchange for collapsing loops that do different work.
That is a worse codebase with a better duplication metric.

---

## The deliverable: the missing `cdmx` test

`cdmx.bootstrap_layers` is called from `apps/cli/cdmx.py:52` and **from no
test at all**. Its siblings are covered: `inegi_marco.bootstrap` has ten-plus
call sites in `tests/integration/test_inegi_marco_ingest.py` (ten exactly),
and `points`
and `inegi_censo` each have their own ingest test.

So the most-used ingest path in the project is the only one with no
end-to-end coverage, and this change touches it. That is the gap to close.

**How to test it without the network.** `bootstrap_layers` already takes
`from_paths`, which replaces the download per layer. Use the `colonias`
layer: it is the only one whose `shp_suffix` is `None`, so
`resolve_source_file` returns the given path unchanged — no zip to extract,
no INEGI request, no cache directory. The other three layers share the
`09_ciudaddemexico.zip` archive and would need a real zip fixture; they are
out of scope here.

Shape:

```python
result = cdmx.bootstrap_layers(
    conn,
    layers=["colonias"],
    from_paths={"colonias": fake_colonias_geojson},
)
```

Follow the fixture pattern already in
`tests/integration/test_inegi_marco_ingest.py:68` (`fake_marco_root`), and
give the new file its own `isolated_silver_dir` and `isolated_source_version`
so the test writes nothing into `data/silver/`.

Note those two are **module-local** fixtures, not shared ones: each of
`test_inegi_marco_ingest.py:41,:51`, `test_inegi_censo_ingest.py:46` and
`test_points_ingest.py:53` defines its own copy. The new file follows suit.
Promoting them to `tests/integration/conftest.py` would be the better design
and is explicitly **out of scope** — it would touch three passing test files
for no behaviour change, and belongs in its own change if it is wanted.

Assert what the function promises and nothing more: the layer loaded, the
`neighborhood` feature type, records written, the silver artifact inside the
isolated directory, the catalog row registered, and idempotence on a second
call.

---

## Order of work

TDD, narrowest layer first:

1. `tests/unit` for `select_layers` — unknown keys raise with the caller's
   label; ordering follows `load_order`, not the caller's argument order; an
   empty or `None` request means every layer
2. `tests/unit` for `resolve_source_root` — `None` falls back to the default;
   a relative path resolves against `REPO_ROOT`; an absolute path is returned
   untouched
3. extract the helper, switch all six call sites over (three and three)
4. the `cdmx` integration test
5. gates

---

## Acceptance

- new unit tests pass and the unit+contract count rises from its current
  **505**
- integration rises from its current **322 passed, 1 skipped** by the new
  `cdmx` test
- `ruff check`, `ruff format --check` and `mypy` over
  `packages/py/reimagina apps scripts` all clean, with no new `Any` and no
  new `# type: ignore`
- no public signature changed; `apps/cli` has no diff
- grepping for either block finds only `_layer_bootstrap.py`
- nothing new exported from `pipeline/__init__.py`
