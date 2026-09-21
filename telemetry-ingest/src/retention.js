// Retention purge (contract §7 / §7 amendments), run from the `scheduled()`
// handler in src/index.js on the cron in wrangler.jsonc.
//
// `reports` and `instances` age out at 13 months; `daily_accepts` at 40
// days (long enough to cover any reasonable admin/debug lookback, short
// enough not to accumulate forever). `totals` and `monthly_totals` are
// intentionally never touched here -- they are running sums maintained
// incrementally as reports arrive (see the INSERT/UPDATE statements in
// src/index.js), not derived from `reports` at read time, so the public
// aggregate history survives long after the underlying per-instance rows
// this purge deletes are gone.

import { addMonthsToDateString, addDaysToDateString, utcTodayString } from './dates.js';

const RETENTION_MONTHS = 13;
const DAILY_ACCEPTS_RETENTION_DAYS = 40;

export async function purgeOldData(db, now = new Date()) {
  const today = utcTodayString(now);
  const cutoff = addMonthsToDateString(today, -RETENTION_MONTHS);
  const dailyAcceptsCutoff = addDaysToDateString(today, -DAILY_ACCEPTS_RETENTION_DAYS);

  // `instances.latest_report_id` is a foreign key into `reports`, and
  // `last_seen_day` is always kept in sync with the received_day of the
  // report it points to (both are set together on every insert -- see
  // src/index.js). So an instance whose last_seen_day has aged out always
  // points at a report that has aged out too, and deleting `instances`
  // first is always enough to clear the FK reference before that report
  // row is deleted below. A *surviving* instance's `latest_report_id`
  // always points at its most recent report, which by definition is not
  // older than `last_seen_day` -- so purging old `reports` rows never
  // orphans a surviving instance's FK.
  //
  // `last_purge_day` is written in the same batch as the deletes (contract
  // §7 amendments S7): either the whole batch commits -- deletes and the
  // updated purge-completion marker together -- or none of it does, so a
  // failed purge is never recorded as having succeeded.
  await db.batch([
    db.prepare('DELETE FROM instances WHERE last_seen_day < ?').bind(cutoff),
    db.prepare('DELETE FROM reports WHERE received_day < ?').bind(cutoff),
    db.prepare('DELETE FROM daily_accepts WHERE day < ?').bind(dailyAcceptsCutoff),
    db.prepare('UPDATE purge_state SET last_purge_day = ? WHERE id = 1').bind(today),
  ]);

  return { cutoff, dailyAcceptsCutoff };
}
