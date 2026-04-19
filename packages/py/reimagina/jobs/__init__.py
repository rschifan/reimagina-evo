"""Job orchestration.

Hosts the workers and pure helpers used by `jobs_runtime`-backed async jobs.
Worker bodies that read from Layer-2 tables (e.g., indicator_result) live in
submodules and are imported lazily so that Layer 1 callers can use the
overlap + aggregation helpers without pulling in unbuilt dependencies.

Public submodules:

- `overlap` — feature-to-feature overlap compute orchestration
- `aggregation` — pure math helpers for the rollup worker
"""
