-- Tret telemetry ingest: initial schema.
--
-- Privacy notes (see the shared contract, bench/telemetry-ingest task spec
-- §3/§7, for the full picture):
--   * `received_day` is a UTC *date* (YYYY-MM-DD), never a timestamp. A
--     report is stamped with the day it was received, not a time, so it
--     can never be correlated with a precise moment in server logs (which
--     this Worker doesn't keep anyway -- observability is off, see
--     wrangler.jsonc).
--   * Nothing here stores IP, user agent, hostnames, free text, or any
--     identifier other than the caller-supplied random `instance_id`.
--   * `totals` and `monthly_totals` are running sums, kept in their own
--     tables specifically so the public aggregate history survives the
--     13-month retention purge of `reports`/`instances` (contract §7).

CREATE TABLE reports (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  -- Server-assigned UTC calendar date the report was accepted on. Used
  -- both for the one-report-per-instance-per-day rule and for retention.
  received_day       TEXT NOT NULL,
  instance_id        TEXT NOT NULL,
  schema_version     INTEGER NOT NULL,
  window_start       TEXT NOT NULL,
  window_end         TEXT NOT NULL,
  tret_version       TEXT NOT NULL,
  deploy             TEXT NOT NULL,
  db                 TEXT NOT NULL,
  users_bucket       TEXT NOT NULL,
  workspaces_bucket  TEXT NOT NULL,
  runs_bucket        TEXT NOT NULL,
  tokens_in          INTEGER NOT NULL,
  tokens_out         INTEGER NOT NULL,
  -- Nullable: a run window with no run carrying an energy/emissions figure
  -- sends `null` rather than a made-up zero (contract §3).
  energy_wh          REAL,
  co2e_g             REAL,
  -- Share maps and the features object are stored as JSON text. They were
  -- already validated field-by-field against closed sets in src/schema.js
  -- before this insert runs, so the JSON here is always one of the shapes
  -- the validator allows -- never raw client input.
  providers          TEXT NOT NULL,
  model_families     TEXT NOT NULL,
  task_types         TEXT NOT NULL,
  run_status         TEXT NOT NULL,
  factor_rungs       TEXT NOT NULL,
  features           TEXT NOT NULL
);

-- Enforces the one-report-per-instance-per-UTC-day rule (contract §7: a
-- second report for the same instance_id on the same day is a 429, not a
-- second row). Also the natural index for "does this instance already have
-- a report today".
CREATE UNIQUE INDEX idx_reports_instance_day ON reports (instance_id, received_day);

-- Needed by the weekly retention purge (delete reports older than 13
-- months) and by admin's weekly-active tally.
CREATE INDEX idx_reports_received_day ON reports (received_day);

CREATE TABLE instances (
  instance_id        TEXT PRIMARY KEY,
  first_seen_day     TEXT NOT NULL,
  last_seen_day      TEXT NOT NULL,
  tret_version       TEXT NOT NULL,
  -- Points at this instance's most recent report row. Aggregate breakdowns
  -- (contract §7) are built from each active instance's *latest* report
  -- only, not from every report it has ever sent.
  latest_report_id   INTEGER NOT NULL REFERENCES reports (id)
);

-- Needed for the "active in the last 35 days" filter used by both the
-- public and admin aggregate endpoints, and for the retention purge
-- (instances not seen for 13 months).
CREATE INDEX idx_instances_last_seen ON instances (last_seen_day);
-- Needed for admin's "new in the last 30 days" figure.
CREATE INDEX idx_instances_first_seen ON instances (first_seen_day);

-- Running totals, bumped by one row per accepted report inside the same
-- `env.DB.batch([...])` that inserts the report (see src/index.js). Single
-- seeded row (id=1) so `UPDATE totals SET reports = reports + 1, ...`
-- always has a row to update; nothing here is ever recomputed from
-- `reports`, which is what lets these survive the retention purge.
-- Typed per-column (contract §7 amendments S9): INTEGER for counts that
-- must stay exact (reports/tokens), REAL for energy/CO2e -- a single
-- `value REAL` column loses integer precision on large token counts.
CREATE TABLE totals (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  reports     INTEGER NOT NULL DEFAULT 0,
  energy_wh   REAL NOT NULL DEFAULT 0,
  co2e_g      REAL NOT NULL DEFAULT 0,
  tokens_in   INTEGER NOT NULL DEFAULT 0,
  tokens_out  INTEGER NOT NULL DEFAULT 0
);

INSERT INTO totals (id, reports, energy_wh, co2e_g, tokens_in, tokens_out) VALUES (1, 0, 0, 0, 0, 0);

-- Same idea as `totals`, but bucketed by month (contract §7 `monthly`
-- array). `month` is the `YYYY-MM` prefix of the *report's* `window_end`,
-- not of `received_day` -- a report describes activity in its window, so
-- that's the month its numbers belong to.
CREATE TABLE monthly_totals (
  month       TEXT PRIMARY KEY,
  reports     INTEGER NOT NULL DEFAULT 0,
  energy_wh   REAL NOT NULL DEFAULT 0,
  co2e_g      REAL NOT NULL DEFAULT 0,
  tokens_in   INTEGER NOT NULL DEFAULT 0,
  tokens_out  INTEGER NOT NULL DEFAULT 0
);

-- Global daily accept counter (contract §7 amendments B2): admission
-- control against a flood of fresh-UUID fake reports. Incremented inside
-- the same `env.DB.batch()` that inserts an accepted report (see
-- src/index.js's `insertReport`); the accept path pre-reads this row and
-- rejects with 429 *before* running that batch when the count already
-- meets `DAILY_ACCEPT_CAP`, so a small overshoot under concurrent requests
-- is possible (two requests can both pass the pre-read before either
-- commits) and is an accepted tradeoff -- see the comment in
-- src/index.js. Rows older than 40 days are deleted by the retention
-- purge.
CREATE TABLE daily_accepts (
  day  TEXT PRIMARY KEY,
  n    INTEGER NOT NULL DEFAULT 0
);

-- Single-row (id=1) stored snapshot of `GET /v1/aggregates` (contract §7
-- amendments): the public endpoint returns `body` verbatim rather than
-- computing the response per request. `body` is the exact JSON text
-- served. Rebuilt by the daily cron only when the rebuild rule in
-- src/aggregates.js (`shouldRebuildSnapshot`) is met, so every published
-- change blends at least 5 newly accepted reports -- this is what stops a
-- polling observer from differencing out one instance's individual
-- report. No row exists until the first rebuild; the endpoint serves a
-- documented all-zero/empty shape until then.
CREATE TABLE public_snapshot (
  id                 INTEGER PRIMARY KEY CHECK (id = 1),
  snapshot_day       TEXT,
  built_at           TEXT,
  reports_at_build   INTEGER NOT NULL DEFAULT 0,
  body               TEXT
);

-- Single-row (id=1) record of when the retention purge last completed
-- (contract §7 amendments S7): exposed on `GET /v1/admin/instances` so a
-- purge that silently stopped running is visible instead of invisible.
-- Written in the same `env.DB.batch()` as the purge's deletes (see
-- src/retention.js), so it only advances when the deletes actually
-- committed.
CREATE TABLE purge_state (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  last_purge_day  TEXT
);

INSERT INTO purge_state (id, last_purge_day) VALUES (1, NULL);
