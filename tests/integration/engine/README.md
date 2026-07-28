# Engine-coupled integration tests

Tests under `engine/<name>/` bind to one specific ops-plane implementation —
they import its client library, its task module, or the tables only it writes.

They are quarantined here so an ops-plane branch can drop one subtree without
touching the rest of the integration suite. A pytest marker would not be
enough: these modules import engine code **at module scope**, so once that code
is deleted the failure is a *collection* error, not a skip.

To exclude a subtree on a branch that no longer ships that engine:

```bash
pytest tests/integration --ignore=tests/integration/engine/procrastinate
```

`tests/integration/*.py` (outside `engine/`) must stay engine-agnostic — they
exercise the domain through the `Orchestrator` port or through the storage
layer, and every branch is expected to keep them green.
