-- Reclaim run plans past the retention horizon.
--
-- Run by the platform's scheduler, NOT by the application. This deliberately
-- does not live on the submit path: an age sweep there deleted the input of
-- live runs, because a busy afternoon crosses any horizon short enough to be
-- worth sweeping opportunistically.
--
-- ⚠ THE HORIZON MUST EXCEED HATCHET'S RUN RETENTION.
--   Hatchet keeps runs for SERVER_LIMITS_DEFAULT_TENANT_RETENTION_PERIOD
--   (720h / 30d by default) and `runs.replay()` re-executes from the original
--   input — which on this track is a `plan_id` handle into this table. A plan
--   that expires before the run it belongs to turns a replayable run into one
--   that fails every unit with "no run_plan row for plan …".
--
--   Set :horizon to engine retention + margin. With the 720h default that is
--   45 days, not 30 — the margin absorbs a long-running unit that started just
--   before the boundary (the unit timeout alone is REIMAGINA_UNIT_TIMEOUT_MIN,
--   120 minutes by default).
--
-- Usage:
--   psql "$DATABASE_URL_DIRECT" -v horizon="'45 days'" -f scripts/reclaim_run_plans.sql
--
-- Uses the r051 index on created_at. At three rows per run this is cheap; it is
-- scheduled rather than opportunistic because *when* it runs must be a decision
-- someone made, not a side effect of whoever submitted last.

\set ON_ERROR_STOP on

DELETE FROM jobs_runtime.run_plan
WHERE created_at < now() - :horizon::interval;
