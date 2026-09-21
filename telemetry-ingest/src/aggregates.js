// Builds the two read responses defined in contract §7 (as overridden by
// §9 AMENDMENTS):
//   - rebuildPublicSnapshot() / shouldRebuildSnapshot() / readPublicSnapshotBody():
//     GET /v1/aggregates -- served verbatim from a stored snapshot, rebuilt
//     by the daily cron, never computed per request.
//   - buildAdminInstances(): GET /v1/admin/instances (unfloored, private,
//     still computed live -- it's authenticated and not the endpoint an
//     unauthenticated observer could poll to difference out one report).
//
// All of these read `reports`/`instances` for "current state" data (who's
// active, what their latest report looks like) and `totals`/`monthly_totals`
// for the running sums that survive the retention purge.

import { utcTodayString, addDaysToDateString, isoTimestampNow, isoWeekOf } from './dates.js';

const ACTIVE_WINDOW_DAYS = 35; // contract §7: "active" = last_seen_day within 35 days
const SEASONING_DAYS = 7; // §9 amendments: an instance counts toward public breakdowns only once first_seen_day <= today - 7
const BREAKDOWNS_MIN_INSTANCES = 10; // public breakdowns are null below this many *seasoned* active instances
const SUPPRESSION_FLOOR = 5; // a breakdown key backed by fewer instances than this is hidden
const PUBLIC_TOTALS_MIN_ALL_TIME = 5; // §9 amendments: public totals/monthly are null until instances.all_time reaches this
const SNAPSHOT_REBUILD_DELTA = 5; // §9 amendments: rebuild only once this many new reports have been accepted since the last snapshot
const WEEKLY_ACTIVE_WEEKS = 12; // admin-only: how many trailing weeks to report

function bump(map, key) {
  map[key] = (map[key] || 0) + 1;
}

/**
 * Remove any key backed by fewer than SUPPRESSION_FLOOR instances, and
 * report how many keys were removed as `_suppressed` (a count of *keys*,
 * not of the instances behind them -- contract §7).
 */
function suppress(counts) {
  const out = {};
  let suppressed = 0;
  for (const [key, count] of Object.entries(counts)) {
    if (count >= SUPPRESSION_FLOOR) out[key] = count;
    else suppressed += 1;
  }
  if (suppressed > 0) out._suppressed = suppressed;
  return out;
}

/**
 * Tally, per breakdown dimension, how many of the given "latest report"
 * rows contribute to each key. For share maps (providers, model_families,
 * factor_rungs) an instance counts toward a key only when its latest
 * report's share for that key is *strictly greater than* 0 (contract §7
 * amendments S3 -- a key present with share 0 is not a real usage signal,
 * and the validator allows share 0 through); for `features` when the flag
 * is true.
 */
function tallyLatestReports(rows) {
  const versions = {};
  const deploy = {};
  const db = {};
  const users_bucket = {};
  const runs_bucket = {};
  const providers = {};
  const model_families = {};
  const factor_rungs = {};
  const features = {};

  for (const row of rows) {
    bump(versions, row.tret_version);
    bump(deploy, row.deploy);
    if (row.db !== undefined) bump(db, row.db);
    if (row.users_bucket !== undefined) bump(users_bucket, row.users_bucket);
    if (row.runs_bucket !== undefined) bump(runs_bucket, row.runs_bucket);

    for (const [key, share] of Object.entries(JSON.parse(row.providers))) {
      if (share > 0) bump(providers, key);
    }
    for (const [key, share] of Object.entries(JSON.parse(row.model_families))) {
      if (share > 0) bump(model_families, key);
    }
    for (const [key, share] of Object.entries(JSON.parse(row.factor_rungs))) {
      if (share > 0) bump(factor_rungs, key);
    }

    const feat = JSON.parse(row.features);
    for (const key of Object.keys(feat)) {
      if (feat[key]) bump(features, key);
    }
  }

  return { versions, deploy, db, users_bucket, runs_bucket, providers, model_families, factor_rungs, features };
}

async function loadTotals(db) {
  const row = await db
    .prepare('SELECT reports, energy_wh, co2e_g, tokens_in, tokens_out FROM totals WHERE id = 1')
    .first();
  return row
    ? {
        reports: row.reports,
        energy_wh: row.energy_wh,
        co2e_g: row.co2e_g,
        tokens_in: row.tokens_in,
        tokens_out: row.tokens_out,
      }
    : { reports: 0, energy_wh: 0, co2e_g: 0, tokens_in: 0, tokens_out: 0 };
}

async function loadMonthly(db) {
  const { results } = await db
    .prepare('SELECT month, reports, energy_wh, co2e_g, tokens_in, tokens_out FROM monthly_totals ORDER BY month')
    .all();
  return results.map((row) => ({
    month: row.month,
    energy_wh: row.energy_wh,
    co2e_g: row.co2e_g,
    reports: row.reports,
  }));
}

async function countInstances(db, { activeCutoff, newCutoff } = {}) {
  const [active, allTime, newRecent] = await Promise.all([
    activeCutoff
      ? db.prepare('SELECT COUNT(*) AS c FROM instances WHERE last_seen_day >= ?').bind(activeCutoff).first()
      : null,
    db.prepare('SELECT COUNT(*) AS c FROM instances').first(),
    newCutoff
      ? db.prepare('SELECT COUNT(*) AS c FROM instances WHERE first_seen_day >= ?').bind(newCutoff).first()
      : null,
  ]);
  return { active: active?.c ?? null, allTime: allTime.c, newRecent: newRecent?.c ?? null };
}

/** How many instances are "seasoned-active": active (35d) AND first seen at least SEASONING_DAYS ago. */
async function countSeasonedActive(db, activeCutoff, seasonedCutoff) {
  const row = await db
    .prepare('SELECT COUNT(*) AS c FROM instances WHERE last_seen_day >= ? AND first_seen_day <= ?')
    .bind(activeCutoff, seasonedCutoff)
    .first();
  return row.c;
}

/**
 * The full `GET /v1/aggregates` response body, computed live against
 * current data. This is never returned directly to a request -- only
 * `rebuildPublicSnapshot` calls it, to produce the next stored snapshot
 * (contract §7 amendments: the public endpoint always serves a snapshot,
 * never a live computation).
 */
async function computePublicAggregates(db, now) {
  const today = utcTodayString(now);
  const activeCutoff = addDaysToDateString(today, -ACTIVE_WINDOW_DAYS);
  const seasonedCutoff = addDaysToDateString(today, -SEASONING_DAYS);

  const { active: active35d, allTime } = await countInstances(db, { activeCutoff });
  const seasonedActive = await countSeasonedActive(db, activeCutoff, seasonedCutoff);

  let breakdowns = null;
  if (seasonedActive >= BREAKDOWNS_MIN_INSTANCES) {
    const { results } = await db
      .prepare(
        `SELECT r.tret_version, r.deploy, r.providers, r.model_families, r.factor_rungs, r.features
         FROM instances i JOIN reports r ON r.id = i.latest_report_id
         WHERE i.last_seen_day >= ? AND i.first_seen_day <= ?`
      )
      .bind(activeCutoff, seasonedCutoff)
      .all();

    const tallies = tallyLatestReports(results);
    breakdowns = {
      versions: suppress(tallies.versions),
      deploy: suppress(tallies.deploy),
      providers: suppress(tallies.providers),
      model_families: suppress(tallies.model_families),
      factor_rungs: suppress(tallies.factor_rungs),
      features: suppress(tallies.features),
    };
  }

  const totalsEligible = allTime >= PUBLIC_TOTALS_MIN_ALL_TIME;

  return {
    schema_version: 1,
    generated_at: isoTimestampNow(now),
    floor: SUPPRESSION_FLOOR,
    breakdowns_min_instances: BREAKDOWNS_MIN_INSTANCES,
    instances: { active_35d: active35d, all_time: allTime },
    totals: totalsEligible ? await loadTotals(db) : null,
    monthly: totalsEligible ? await loadMonthly(db) : null,
    breakdowns,
    snapshot_day: today,
  };
}

/**
 * The all-zero/empty shape `GET /v1/aggregates` serves before any snapshot
 * has ever been built (contract §7 amendments).
 */
export function emptyPublicAggregatesShape(now = new Date()) {
  return {
    schema_version: 1,
    generated_at: isoTimestampNow(now),
    floor: SUPPRESSION_FLOOR,
    breakdowns_min_instances: BREAKDOWNS_MIN_INSTANCES,
    instances: { active_35d: 0, all_time: 0 },
    totals: null,
    monthly: null,
    breakdowns: null,
    snapshot_day: null,
  };
}

/**
 * The rebuild rule (contract §7 amendments): rebuild iff there is no
 * snapshot yet and at least 5 instances exist all-time, OR at least 5 more
 * reports have been accepted since the currently published snapshot was
 * built. Kept as a small, independently testable function of (db,
 * todayString) so the "when do we rebuild" decision can be unit-tested
 * without going through the whole scheduled handler. `todayString` isn't
 * used by the rule itself (the rule is purely about the report-count
 * delta, not the date) -- it's accepted for symmetry with
 * `rebuildPublicSnapshot(db, now)` and so a future date-based rule doesn't
 * need a signature change.
 */
export async function shouldRebuildSnapshot(db, todayString) { // eslint-disable-line no-unused-vars
  const [snapshotRow, totalsRow, allTimeRow] = await Promise.all([
    db.prepare('SELECT reports_at_build FROM public_snapshot WHERE id = 1').first(),
    db.prepare('SELECT reports FROM totals WHERE id = 1').first(),
    db.prepare('SELECT COUNT(*) AS c FROM instances').first(),
  ]);
  const totalReports = totalsRow?.reports ?? 0;
  const allTime = allTimeRow?.c ?? 0;

  if (!snapshotRow) return allTime >= PUBLIC_TOTALS_MIN_ALL_TIME;
  return totalReports - snapshotRow.reports_at_build >= SNAPSHOT_REBUILD_DELTA;
}

/**
 * Recomputes the public aggregates and stores them as the new snapshot
 * (single row, id=1). Called from the scheduled handler, and only when
 * `shouldRebuildSnapshot` says to.
 */
export async function rebuildPublicSnapshot(db, now = new Date()) {
  const body = await computePublicAggregates(db, now);
  const totalsRow = await db.prepare('SELECT reports FROM totals WHERE id = 1').first();

  await db
    .prepare(
      `INSERT INTO public_snapshot (id, snapshot_day, built_at, reports_at_build, body)
       VALUES (1, ?, ?, ?, ?)
       ON CONFLICT (id) DO UPDATE SET
         snapshot_day = excluded.snapshot_day,
         built_at = excluded.built_at,
         reports_at_build = excluded.reports_at_build,
         body = excluded.body`
    )
    .bind(body.snapshot_day, isoTimestampNow(now), totalsRow?.reports ?? 0, JSON.stringify(body))
    .run();

  return body;
}

/**
 * The exact JSON text `GET /v1/aggregates` should serve: the stored
 * snapshot's `body` verbatim, or the empty shape if no snapshot exists yet.
 */
export async function readPublicSnapshotBody(db, now = new Date()) {
  const row = await db.prepare('SELECT body FROM public_snapshot WHERE id = 1').first();
  if (row?.body) return row.body;
  return JSON.stringify(emptyPublicAggregatesShape(now));
}

/** GET /v1/admin/instances -- private, unfloored fleet view. */
export async function buildAdminInstances(db, now = new Date()) {
  const today = utcTodayString(now);
  const activeCutoff = addDaysToDateString(today, -ACTIVE_WINDOW_DAYS);
  const newCutoff = addDaysToDateString(today, -30);

  const { active: active35d, allTime, newRecent: new30d } = await countInstances(db, { activeCutoff, newCutoff });

  // Unlike the public breakdowns (which only count seasoned, active
  // instances -- contract §7 amendments), admin's fleet-wide dimensions
  // cover every instance's latest report, with no activity or seasoning
  // filter at all: this is the private "whole fleet" view, so there's no
  // reason to narrow it the way the public floor does.
  const { results: latestRows } = await db
    .prepare(
      `SELECT r.tret_version, r.deploy, r.db, r.users_bucket, r.runs_bucket,
              r.providers, r.model_families, r.factor_rungs, r.features
       FROM instances i JOIN reports r ON r.id = i.latest_report_id`
    )
    .all();
  const tallies = tallyLatestReports(latestRows);

  // Weekly active reads straight from `reports.received_day` (not
  // `instances.last_seen_day`, which only remembers the *latest* day) so
  // each week's count reflects who actually sent a report that week.
  const weekStart = addDaysToDateString(today, -7 * WEEKLY_ACTIVE_WEEKS);
  const { results: recentReports } = await db
    .prepare('SELECT instance_id, received_day FROM reports WHERE received_day >= ?')
    .bind(weekStart)
    .all();
  const byWeek = new Map();
  for (const row of recentReports) {
    const week = isoWeekOf(row.received_day);
    if (!byWeek.has(week)) byWeek.set(week, new Set());
    byWeek.get(week).add(row.instance_id);
  }
  const weekly_active = [...byWeek.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([week, set]) => ({ week, instances: set.size }));

  // last_purge_day / snapshot_day (contract §7 amendments S7): surfaced
  // here so a stuck retention purge or a snapshot that stopped rebuilding
  // is visible to whoever operates the fleet, instead of failing silently.
  const [purgeRow, snapshotRow] = await Promise.all([
    db.prepare('SELECT last_purge_day FROM purge_state WHERE id = 1').first(),
    db.prepare('SELECT snapshot_day FROM public_snapshot WHERE id = 1').first(),
  ]);

  return {
    generated_at: isoTimestampNow(now),
    instances: { active_35d: active35d, all_time: allTime, new_30d: new30d },
    weekly_active,
    versions: tallies.versions,
    deploy: tallies.deploy,
    db: tallies.db,
    users_bucket: tallies.users_bucket,
    runs_bucket: tallies.runs_bucket,
    providers: tallies.providers,
    model_families: tallies.model_families,
    factor_rungs: tallies.factor_rungs,
    features: tallies.features,
    totals: await loadTotals(db),
    last_purge_day: purgeRow?.last_purge_day ?? null,
    snapshot_day: snapshotRow?.snapshot_day ?? null,
  };
}
