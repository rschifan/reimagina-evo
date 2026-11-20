"""Phase 3 job orchestration — the domain core.

See docs/architecture/JOB_LAYER.md. This package owns the
domain-truth components (impact engine, intake, coordinator) that no library
supplies; the compute graph and ops-plane are borrowed (Hamilton, Postgres).
"""
