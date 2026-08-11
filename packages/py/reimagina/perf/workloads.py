"""Synthetic indicator workloads for comparing how an ops plane schedules.

The conformance suite proves the ``Orchestrator`` port *contract* holds. It says
nothing about how an engine schedules, because the shipped catalog is four
indicators wide and only one edit (``geometry``) produces more than a single
wave. These workloads supply the DAG shapes the catalog cannot.

**This module must stay byte-identical on both ops-plane worktrees.** It is the
fixed half of the comparison; the per-track harness in ``tests/workloads`` is
the half that differs, and a drift here would silently make the two reports
incomparable.

Two design decisions worth knowing before editing:

*Nodes are ``computation_kind="direct"`` even when they have dependencies.*
``default_runners`` routes on kind before it looks at the slug
(``runners.py:86``), so a ``"composite"`` spec would be handed the real
``compute_composite``, which reads ``indicator_result`` and needs catalog rows.
Declaring dependency-shaped nodes as ``direct`` keeps every scheduling property
— ``compute_dirty_set``, ``_topo_order``, ``plan_wave`` and ``_dep_map`` all read
``depends_on`` and never ``computation_kind`` — while keeping the catalog out of
it entirely. Real composite *computation* is covered by the Family 2 workloads
and by ``tests/conformance``; this file covers composite *scheduling*.

*Names are plausible urban indicators, not ``a``/``b``/``s1``.* Track B names
each engine task after the slug (``hatchet_app.build_dag``), so these strings are
what a person reads in the dashboard while watching a DAG execute. Each name is
chosen so its real-world cost matches the cost class the workload needs: an
isochrone genuinely is slow, an amenity count genuinely is fast. That way the
name carries the intent without a ``_slow``/``_cpu`` suffix.

⚠ Slugs must match ``[a-z0-9_]+``. Two reasons, both load-bearing:
``_skip_unless_dirty`` interpolates the slug into a CEL string
(``f"!('{slug}' in output.dirty)"``), so a quote is an injection rather than a
bad label; and ``_strip_suffix`` strips a trailing ``-<digits>`` because the
engine appends a millisecond timestamp, so a slug ending that way is silently
truncated on read.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.jobs._lifecycle import job_run
from reimagina.storage import backend_for_connection

# ── Scale ───────────────────────────────────────────────────────────────────

SCALES: dict[str, float] = {"smoke": 0.1, "full": 1.0}

_FLOOR_S = 0.005
"""Below ~5 ms a unit measures timer resolution and engine overhead rather than
itself. Without this floor `many_tiny` at smoke scale would be 40 units of
nothing, which reads as a fast engine instead of an absent measurement."""


def scale() -> float:
    """Multiplier for every nominal unit cost, from ``REIMAGINA_BENCH_SCALE``.

    Accepts a named tier or a bare float, so a genuinely long run — the minutes a
    real PostGIS unit takes, which no few-minute suite can afford by default — is
    one env var away rather than a code change.
    """
    raw = os.environ.get("REIMAGINA_BENCH_SCALE", "full").strip()
    if raw in SCALES:
        return SCALES[raw]
    try:
        return float(raw)
    except ValueError:
        return SCALES["full"]


def _seconds(nominal: float) -> float:
    return max(nominal * scale(), _FLOOR_S)


# ── Compute factories ───────────────────────────────────────────────────────
#
# Every synthetic compute opens a real `job_run` row, because that row is the
# entire measurement surface: `started_at`/`completed_at` are where per-unit
# timing, observed parallelism and the head-of-line delay all come from. It also
# means these units exercise the same provenance and cancellation path a real
# indicator does, rather than a shortcut that would prove nothing.
#
# `model_family` carries the slug. `job_run` has no indicator column, and for a
# synthetic node there is no `indicator_result` row to join through, so this is
# the only way to attribute a row to a node. Real indicators put their family
# there instead — the report handles both (see `workload_report._unit_rows`).


class BenchUnitFailed(RuntimeError):
    """Deliberate failure from a workload unit. Picklable, carries no cursor."""


def _io_bound(slug: str, nominal: float) -> Callable[..., object]:
    """A unit that waits on Postgres — real IO on the unit's own connection.

    `pg_sleep` rather than `time.sleep` on purpose: it holds a real connection,
    releases the GIL the way a real PostGIS statement does, and so measures the
    engine's slot behaviour instead of Python's threading.
    """

    def _compute(
        conn,
        *,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
        scenario_id: str | None = None,
        code_version: str = "dev",
    ) -> dict:
        backend = backend_for_connection(conn)
        seconds = _seconds(nominal)
        with job_run(
            backend,
            model_family=slug,
            code_version=code_version,
            scenario_id=scenario_id,
        ):
            with conn.cursor() as cur:
                cur.execute("SELECT pg_sleep(%s)", (seconds,))
        return {"slug": slug, "kind": "io", "nominal_s": seconds}

    return _compute


def _failing(slug: str) -> Callable[..., object]:
    """A unit that always raises, to probe failure isolation at DAG scale."""

    def _compute(
        conn,
        *,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
        scenario_id: str | None = None,
        code_version: str = "dev",
    ) -> dict:
        raise BenchUnitFailed(f"{slug}: deliberate workload-suite failure")

    return _compute


_FACTORIES = {"io": _io_bound, "fail": _failing}


# ── Declaring a workload ────────────────────────────────────────────────────

TRIGGER = "bench_trigger"
"""The attribute that seeds every root. Deliberately not a real feature
attribute, so a workload can never be seeded by a genuine scenario edit."""

BENCH_FEATURE = "00000000-0000-4000-8000-0000000000b1"
"""A fixed feature id to scope the dirty set with. Synthetic computes ignore it;
it exists only so `DirtyUnit.features` is non-empty, which is what the real
runner adapter expects."""


@dataclass(frozen=True)
class Node:
    slug: str
    kind: str = "io"
    cost: float = 0.1
    deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workload:
    """One DAG, its runners, and what submitting it should produce."""

    id: str
    description: str
    nodes: tuple[Node, ...]
    family: str = "synthetic"
    changed_attributes: frozenset[str] = frozenset({TRIGGER})
    catalog_specs: tuple[IndicatorSpec, ...] = ()
    """Specs that need real `indicator_catalog` rows. Empty for Family 1 —
    `plan_run` is pure over the registry and `job_run.model_family` has no FK, so
    a synthetic workload needs a migrated database and nothing else."""
    notes: str = ""
    _registry: tuple[IndicatorSpec, ...] = field(default=(), repr=False)

    @property
    def registry(self) -> tuple[IndicatorSpec, ...]:
        return self._registry

    @property
    def computes(self) -> dict[str, Callable[..., object]]:
        """slug -> compute fn, for installation into `runners._DIRECT_COMPUTE`."""
        return {
            n.slug: _FACTORIES[n.kind](n.slug, n.cost)
            if n.kind != "fail"
            else _FACTORIES[n.kind](n.slug)
            for n in self.nodes
        }

    @property
    def edges(self) -> dict[str, tuple[str, ...]]:
        return {n.slug: n.deps for n in self.nodes}

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(n.slug for n in self.nodes)

    @property
    def expect_failure(self) -> bool:
        return any(n.kind == "fail" for n in self.nodes)

    @property
    def nominal_seconds(self) -> dict[str, float]:
        return {n.slug: _seconds(n.cost) for n in self.nodes if n.kind != "fail"}


def _build(
    workload_id: str,
    description: str,
    nodes: tuple[Node, ...],
    **kwargs: object,
) -> Workload:
    """Turn nodes into specs, keeping declaration order topological.

    Roots read `TRIGGER`; dependent nodes read nothing, exactly as a real
    composite does. That is enough for `_topo_order`, which passes the union of
    every spec's `reads` through `compute_dirty_set` — so a node with no `reads`
    is still ordered correctly as long as it has a dirty ancestor. Only a wholly
    disconnected node with no `reads` would be appended out of order, and this
    builder cannot produce one.
    """
    registry = tuple(
        IndicatorSpec(
            slug=n.slug,
            name=n.slug.replace("_", " ").title(),
            computation_kind="direct",
            value_type="numeric",
            aggregation_method="weighted_mean",
            applies_to=("block",),
            model_family="bench",
            reads=() if n.deps else (TRIGGER,),
            depends_on=tuple((dep, None) for dep in n.deps),
        )
        for n in nodes
    )
    return Workload(
        id=workload_id,
        description=description,
        nodes=nodes,
        _registry=registry,
        **kwargs,  # type: ignore[arg-type]
    )


# ── Family 1: topology and cost ─────────────────────────────────────────────

LATTICE = _build(
    "lattice",
    "Three layers, overlapping edges, one slow root. The complex-DAG flagship.",
    (
        # Roots. One is genuinely expensive — a proximity/nearest-neighbour pass
        # over green space is exactly the kind of unit that dominates a wave.
        Node("green_space_proximity", "io", 1.5),
        Node("building_footprint_ratio", "io", 0.1),
        Node("road_length_density", "io", 0.1),
        Node("population_density", "io", 0.1),
        # Middle layer: each depends on an *overlapping* pair, so this is a
        # lattice rather than four independent chains. Two of the four do not
        # touch the slow root — those are the ones a per-task scheduler can
        # release immediately and a wave scheduler cannot.
        Node(
            "livability_green",
            "io",
            0.1,
            ("green_space_proximity", "population_density"),
        ),
        Node(
            "built_compactness",
            "io",
            0.1,
            ("building_footprint_ratio", "road_length_density"),
        ),
        Node(
            "green_accessibility",
            "io",
            0.1,
            ("green_space_proximity", "road_length_density"),
        ),
        Node(
            "density_composite",
            "io",
            0.1,
            ("population_density", "building_footprint_ratio"),
        ),
        Node(
            "neighbourhood_quality_index",
            "io",
            0.1,
            (
                "livability_green",
                "built_compactness",
                "green_accessibility",
                "density_composite",
            ),
        ),
    ),
    notes=(
        "The CONVERGING case. built_compactness and density_composite have no path "
        "to the slow root, so a per-task scheduler starts them ~1.4s earlier — but "
        "the sink depends on all four mid nodes, so the slow branch gates the "
        "makespan either way. Expect head_of_line_ms to differ sharply and slr to "
        "be near-identical. That contrast with `barrier_probe` IS the finding: the "
        "wave barrier is free when the DAG re-converges and expensive when it does "
        "not. Reading only makespan here would wrongly conclude the barrier costs "
        "nothing."
    ),
)

BARRIER_PROBE = _build(
    "barrier_probe",
    "One slow root beside a deep independent chain — where the wave barrier costs makespan.",
    (
        # One expensive root with no dependents. Nothing in the DAG needs its
        # result, so nothing in the DAG justifies waiting for it.
        Node("walkability_isochrone_15min", "io", 1.5),
        # An eight-hop chain that shares no ancestor with it. Depth is the whole
        # design: a wave scheduler pays one wave per hop *after* the slow root
        # has cleared wave 1, so it spends 1.5 + 7x0.2 = 2.9s. A per-task
        # scheduler runs the chain concurrently with the root and spends
        # max(1.5, 8x0.2) = 1.6s. Expect SLR ~1.8 vs ~1.0.
        Node("street_segment_import", "io", 0.2),
        Node("street_intersection_density", "io", 0.2, ("street_segment_import",)),
        Node("street_connectivity_index", "io", 0.2, ("street_intersection_density",)),
        Node("street_block_length_mean", "io", 0.2, ("street_connectivity_index",)),
        Node("street_network_grade", "io", 0.2, ("street_block_length_mean",)),
        Node("street_route_directness", "io", 0.2, ("street_network_grade",)),
        Node("street_walk_score", "io", 0.2, ("street_route_directness",)),
        Node("street_accessibility_rank", "io", 0.2, ("street_walk_score",)),
    ),
    notes=(
        "The DIVERGING case. Unlike `lattice` and `diamond`, no sink re-joins the "
        "two branches, so the barrier shows up in makespan and not merely in "
        "head-of-line delay. A shallow fast chain would not have shown it: the "
        "first draft used three 0.1s hops and separated the tracks by 0.1s total."
    ),
)

DIAMOND = _build(
    "diamond",
    "One root, two branches of unequal cost, one join. The control.",
    (
        Node("block_geometry_clean", "io", 0.1),
        Node("block_area", "io", 0.1, ("block_geometry_clean",)),
        Node("block_shape_index", "io", 2.0, ("block_geometry_clean",)),
        Node("block_form_score", "io", 0.1, ("block_area", "block_shape_index")),
    ),
    notes=(
        "The join genuinely depends on the slow branch, so head-of-line delay "
        "must be ~0 on BOTH tracks. If this diverges too, the metric is measuring "
        "something other than the wave barrier."
    ),
)


CHAIN_DEEP = _build(
    "chain_deep",
    "Six-link serial chain — per-hop scheduling latency with no parallelism.",
    (
        Node("transit_headway_raw", "io", 0.1),
        Node("transit_frequency", "io", 0.1, ("transit_headway_raw",)),
        Node("transit_service_level", "io", 0.1, ("transit_frequency",)),
        Node("transit_accessibility", "io", 0.1, ("transit_service_level",)),
        Node("transit_equity_score", "io", 0.1, ("transit_accessibility",)),
        Node("mobility_index", "io", 0.1, ("transit_equity_score",)),
    ),
    notes="Serial by construction, so efficiency is ~1/slots even on a perfect scheduler.",
)


MANY_TINY = _build(
    "many_tiny",
    "Forty 10 ms units — per-unit engine bookkeeping as the dominant term.",
    tuple(
        Node(f"amenity_count_{cat}", "io", 0.01)
        for cat in (
            "cafe",
            "school",
            "pharmacy",
            "clinic",
            "bakery",
            "bank",
            "bar",
            "library",
            "museum",
            "cinema",
            "gym",
            "park",
            "playground",
            "market",
            "supermarket",
            "restaurant",
            "hotel",
            "hospital",
            "dentist",
            "vet",
            "post",
            "police",
            "fire_station",
            "kindergarten",
            "university",
            "theatre",
            "nightclub",
            "bicycle_parking",
            "car_wash",
            "fuel",
            "atm",
            "laundry",
            "hairdresser",
            "florist",
            "bookshop",
            "butcher",
            "greengrocer",
            "hardware",
            "optician",
            "toy_shop",
        )
    ),
    notes="At this grain the engine's own overhead exceeds the work. That is the point.",
)

PARTIAL_FAILURE = _build(
    "partial_failure",
    "One raising unit with a dependent, beside independent siblings.",
    (
        Node("flood_risk_zonal", "fail"),
        Node("resilience_index", "io", 0.1, ("flood_risk_zonal",)),
        Node("tree_canopy_share", "io", 0.1),
        Node("heat_island_delta", "io", 0.1),
    ),
    notes=(
        "Track B retries the failing unit (UNIT_RETRIES=2, backoff 2.0) and Track A "
        "does not (retries_allowed=False), so wall clock here compares retry policy, "
        "not scheduling. speedup/efficiency are omitted for this workload."
    ),
)


# ── A seeded random DAG, to catch shape-choice bias ─────────────────────────

_VOCAB = (
    "noise_exposure",
    "permeability_index",
    "canopy_gap",
    "slope_ruggedness",
    "frontage_activity",
    "parcel_fragmentation",
    "junction_load",
    "daylight_factor",
    "facade_porosity",
    "block_perimeter",
    "service_reach",
    "kerb_length",
    "plot_coverage",
    "setback_depth",
    "corner_density",
    "alley_share",
    "stormwater_load",
    "albedo_mean",
    "transit_gap",
    "retail_frontage",
)


def layered_random(
    *,
    seed: int = 20260729,
    n: int = 20,
    width: int = 5,
    density: float = 0.45,
    jump: int = 2,
    slow_every: int = 7,
) -> tuple[Node, ...]:
    """Generate a layered DAG from the standard `daggen` parameter space.

    Every hand-picked shape in this file was chosen because it isolates
    something, which is exactly the bias a reviewer should suspect. A seeded
    random DAG drawn from the parameters the DAG-scheduling literature uses —
    task count, width, edge density, and how many levels an edge may span —
    checks that the conclusions are not an artefact of that choice.

    Seeded and pure, so both tracks schedule byte-identical topologies.

    ponytail: ~30 lines instead of a WfCommons dependency. WfChef synthesises
    from real HPC traces (Montage, CyberShake) whose grain looks nothing like an
    indicator DAG; if that changes, swapping this function is the whole job.
    """
    rng = random.Random(seed)
    levels: list[list[int]] = []
    remaining = list(range(n))
    while remaining:
        take = min(rng.randint(1, width), len(remaining))
        levels.append(remaining[:take])
        remaining = remaining[take:]

    nodes: list[Node] = []
    for depth, level in enumerate(levels):
        for idx in level:
            candidates = [
                other for prev in levels[max(0, depth - jump) : depth] for other in prev
            ]
            deps = [c for c in candidates if rng.random() < density]
            # A node below the first level must attach to something, or it
            # becomes a second root and the generated width stops meaning what
            # the parameters say it means.
            if candidates and not deps:
                deps = [rng.choice(candidates)]
            nodes.append(
                Node(
                    slug=f"{_VOCAB[idx % len(_VOCAB)]}_l{depth}",
                    kind="io",
                    cost=1.2 if idx % slow_every == 0 else 0.1,
                    deps=tuple(
                        f"{_VOCAB[d % len(_VOCAB)]}_l{_depth_of(levels, d)}"
                        for d in deps
                    ),
                )
            )
    return tuple(nodes)


def _depth_of(levels: list[list[int]], idx: int) -> int:
    for depth, level in enumerate(levels):
        if idx in level:
            return depth
    raise ValueError(f"node {idx} is in no level")


LAYERED_RANDOM = _build(
    "layered_random",
    "Twenty nodes from a seeded daggen-style generator — the anti-cherry-pick check.",
    layered_random(),
    notes="Must agree in direction with `lattice`, or the hand-picked shapes do not generalise.",
)


# ── Predicting what each strategy should do ─────────────────────────────────


def simulate(workload: Workload, *, slots: int = 4, strategy: str = "pertask") -> float:
    """Nominal makespan of this DAG under an idealised scheduler, in seconds.

    Two strategies, matching the two tracks' stances:

    ``pertask`` — a node starts as soon as its own parents are done and a slot is
    free. What an engine-declared DAG does.

    ``wave`` — every node whose parents are all satisfied is dispatched together,
    and the *whole wave* must finish before the next is planned. What a
    library-track coordinator does when it awaits each wave's handles before
    re-planning.

    This exists so the suite predicts its own results. A measured SLR that
    diverges from the prediction is a finding either way: either the harness is
    wrong, or the engine is not scheduling the way its source says it does.
    Ignores engine overhead entirely, so measured >= predicted always.
    """
    cost = workload.nominal_seconds
    edges = workload.edges
    done: dict[str, float] = {}
    pending = set(workload.slugs)

    while pending:
        ready = sorted(s for s in pending if all(d in done for d in edges[s]))
        if not ready:  # pragma: no cover - the sanity check rejects cycles
            raise ValueError(f"{workload.id}: cycle or dangling edge")
        gate = max(done.values(), default=0.0) if strategy == "wave" else None
        free_at = [0.0] * slots
        for slug in ready:
            earliest = max([done[d] for d in edges[slug]] or [0.0])
            if gate is not None:
                earliest = max(earliest, gate)
            slot = min(range(slots), key=lambda i: free_at[i])
            start = max(free_at[slot], earliest)
            finish = start + cost.get(slug, 0.0)
            free_at[slot] = finish
            done[slug] = finish
        pending -= set(ready)

    return max(done.values(), default=0.0)


def predictions(workload: Workload, *, slots: int = 4) -> dict[str, float]:
    """Nominal critical path and both strategies' makespans, for the report."""
    cost = workload.nominal_seconds
    edges = workload.edges
    memo: dict[str, float] = {}

    def longest(slug: str) -> float:
        if slug not in memo:
            memo[slug] = cost.get(slug, 0.0) + max(
                [longest(d) for d in edges[slug]] or [0.0]
            )
        return memo[slug]

    critical = max((longest(s) for s in workload.slugs), default=0.0)
    wave = simulate(workload, slots=slots, strategy="wave")
    pertask = simulate(workload, slots=slots, strategy="pertask")
    return {
        "critical_path_s": critical,
        "serial_s": sum(cost.values()),
        "wave_makespan_s": wave,
        "pertask_makespan_s": pertask,
        "wave_slr": wave / critical if critical else 1.0,
        "pertask_slr": pertask / critical if critical else 1.0,
    }


WORKLOADS: tuple[Workload, ...] = (
    LATTICE,
    BARRIER_PROBE,
    DIAMOND,
    CHAIN_DEEP,
    MANY_TINY,
    PARTIAL_FAILURE,
    LAYERED_RANDOM,
)

BY_ID: dict[str, Workload] = {w.id: w for w in WORKLOADS}


if __name__ == "__main__":  # pragma: no cover - a shape sanity check, not a test
    import graphlib

    for wl in WORKLOADS:
        graphlib.TopologicalSorter(
            {slug: set(deps) for slug, deps in wl.edges.items()}
        ).prepare()
        known = set(wl.slugs)
        dangling = {d for deps in wl.edges.values() for d in deps} - known
        assert not dangling, f"{wl.id}: edges point at unknown nodes {dangling}"
        assert len(known) == len(wl.nodes), f"{wl.id}: duplicate slugs"
        for slug in known:
            assert slug.replace("_", "").isalnum() and slug.islower(), (
                f"{wl.id}: {slug!r} is not [a-z0-9_]+ — it would break the CEL "
                "skip expression or the -<digits> suffix strip"
            )
        p = predictions(wl)
        sep = p["wave_makespan_s"] / p["pertask_makespan_s"]
        print(
            f"{wl.id:16} {len(wl.nodes):3}n  cp={p['critical_path_s']:5.2f}s  "
            f"wave={p['wave_makespan_s']:5.2f}s (slr {p['wave_slr']:4.2f})  "
            f"pertask={p['pertask_makespan_s']:5.2f}s (slr {p['pertask_slr']:4.2f})  "
            f"wave/pertask={sep:4.2f}x"
        )
