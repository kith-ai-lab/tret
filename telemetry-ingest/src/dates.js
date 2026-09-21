// Small date helpers shared across the Worker.
//
// Every date this Worker deals with is a UTC calendar date (`YYYY-MM-DD`),
// never a timestamp -- see the `received_day` comment in
// migrations/0001_init.sql for why that distinction is privacy-relevant.
// Keeping the arithmetic in one small file makes it easy to audit that
// nothing here quietly starts reading wall-clock time with sub-day
// precision.

/** Today's UTC date as `YYYY-MM-DD`. */
export function utcTodayString(now = new Date()) {
  return now.toISOString().slice(0, 10);
}

/** `dateStr` shifted by `days` (may be negative), as `YYYY-MM-DD`. */
export function addDaysToDateString(dateStr, days) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + days));
  return dt.toISOString().slice(0, 10);
}

/**
 * `dateStr` shifted by `months` calendar months (may be negative), with the
 * day-of-month clamped to the last valid day of the target month.
 *
 * Naively doing `Date.UTC(y, m - 1 + months, d)` lets JS roll an
 * out-of-range day into the *next* month (e.g. 2027-03-31 minus 13 months
 * would silently become 2026-03-03 instead of 2026-02-28) -- wrong for
 * retention-cutoff arithmetic, where that rollover would shift the cutoff
 * by weeks. `Date.UTC(y, targetMonth + 1, 0)` is the standard "last day of
 * targetMonth" trick (day 0 of the following month), and it normalises
 * `targetMonth` correctly no matter how far outside 0-11 it is.
 */
export function addMonthsToDateString(dateStr, months) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const targetMonth = m - 1 + months; // 0-based, may be any integer
  const lastDayOfTargetMonth = new Date(Date.UTC(y, targetMonth + 1, 0)).getUTCDate();
  const dt = new Date(Date.UTC(y, targetMonth, Math.min(d, lastDayOfTargetMonth)));
  return dt.toISOString().slice(0, 10);
}

/** Whole days between two `YYYY-MM-DD` strings (end - start). */
export function daysBetween(startStr, endStr) {
  const start = Date.parse(`${startStr}T00:00:00Z`);
  const end = Date.parse(`${endStr}T00:00:00Z`);
  return Math.round((end - start) / 86400000);
}

/** The `YYYY-MM` a `YYYY-MM-DD` date falls in. */
export function monthOf(dateStr) {
  return dateStr.slice(0, 7);
}

/** `now` as an ISO-8601 timestamp truncated to whole seconds (no millis). */
export function isoTimestampNow(now = new Date()) {
  return now.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** ISO-8601 week label (e.g. `2026-W38`) for a `YYYY-MM-DD` date. */
export function isoWeekOf(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  // Shift to the Thursday of this ISO week, then read the year off that
  // Thursday -- the standard trick for handling ISO week/year boundaries.
  const dayNum = (date.getUTCDay() + 6) % 7; // Mon=0 .. Sun=6
  date.setUTCDate(date.getUTCDate() - dayNum + 3);
  const firstThursday = new Date(Date.UTC(date.getUTCFullYear(), 0, 4));
  const firstDayNum = (firstThursday.getUTCDay() + 6) % 7;
  firstThursday.setUTCDate(firstThursday.getUTCDate() - firstDayNum + 3);
  const week = 1 + Math.round((date - firstThursday) / (7 * 86400000));
  return `${date.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
}
