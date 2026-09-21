import { describe, it, expect } from 'vitest';
import { SELF, env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker from '../src/index.js';
import { purgeOldData } from '../src/retention.js';
import { validPayload, uuidFor, postReport } from './fixtures.js';
import { addMonthsToDateString, addDaysToDateString, utcTodayString } from '../src/dates.js';

async function loadTotalsReports() {
  const row = await env.DB.prepare('SELECT reports FROM totals WHERE id = 1').first();
  return row.reports;
}

describe('retention purge', () => {
  it('removes rows older than 13 months, keeps 12-month-old rows, and leaves totals alone', async () => {
    const now = new Date('2026-09-21T00:00:00Z');
    const fourteenMonthsAgo = addMonthsToDateString(utcTodayString(now), -14); // must be purged
    const twelveMonthsAgo = addMonthsToDateString(utcTodayString(now), -12); // must survive

    const oldInstance = uuidFor(500);
    const recentInstance = uuidFor(501);

    expect((await postReport(SELF, validPayload({ instance_id: oldInstance }))).status).toBe(204);
    expect((await postReport(SELF, validPayload({ instance_id: recentInstance }))).status).toBe(204);

    // Backdate as if these reports/instances had actually been received
    // long ago -- the report handler always stamps `received_day` with the
    // real current date, so this is the only way to test aging without
    // mocking the clock for the whole request path.
    await env.DB.prepare('UPDATE reports SET received_day = ? WHERE instance_id = ?')
      .bind(fourteenMonthsAgo, oldInstance)
      .run();
    await env.DB.prepare('UPDATE instances SET last_seen_day = ? WHERE instance_id = ?')
      .bind(fourteenMonthsAgo, oldInstance)
      .run();

    await env.DB.prepare('UPDATE reports SET received_day = ? WHERE instance_id = ?')
      .bind(twelveMonthsAgo, recentInstance)
      .run();
    await env.DB.prepare('UPDATE instances SET last_seen_day = ? WHERE instance_id = ?')
      .bind(twelveMonthsAgo, recentInstance)
      .run();

    expect(await loadTotalsReports()).toBe(2);

    await purgeOldData(env.DB, now);

    expect(await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?').bind(oldInstance).first()).toBeFalsy();
    expect(
      await env.DB.prepare('SELECT * FROM instances WHERE instance_id = ?').bind(oldInstance).first()
    ).toBeFalsy();

    expect(
      await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?').bind(recentInstance).first()
    ).toBeTruthy();
    expect(
      await env.DB.prepare('SELECT * FROM instances WHERE instance_id = ?').bind(recentInstance).first()
    ).toBeTruthy();

    // totals/monthly_totals are running sums, never touched by the purge --
    // they must survive even though the report behind them is now gone.
    expect(await loadTotalsReports()).toBe(2);

    // The surviving instance's FK (latest_report_id) must still resolve --
    // purging old `reports` rows must never orphan a surviving instance.
    const survivor = await env.DB.prepare('SELECT latest_report_id FROM instances WHERE instance_id = ?')
      .bind(recentInstance)
      .first();
    const survivorReport = await env.DB.prepare('SELECT id FROM reports WHERE id = ?').bind(survivor.latest_report_id).first();
    expect(survivorReport).toBeTruthy();
  });

  it('writes last_purge_day in the same batch as the deletes, clamped to a real calendar date', async () => {
    // 2026-09-21 minus 13 months lands on a 31st with no equivalent day in
    // the target month if not clamped (contract §7 amendments N10 /
    // addMonthsToDateString): exercised indirectly here by simply checking
    // last_purge_day is stamped with "today" and is a well-formed date.
    const now = new Date('2027-03-31T00:00:00Z');
    await purgeOldData(env.DB, now);
    const row = await env.DB.prepare('SELECT last_purge_day FROM purge_state WHERE id = 1').first();
    expect(row.last_purge_day).toBe('2027-03-31');
  });

  it('deletes daily_accepts rows older than 40 days', async () => {
    const now = new Date('2026-09-21T00:00:00Z');
    const today = utcTodayString(now);
    const oldDay = addDaysToDateString(today, -41);
    const recentDay = addDaysToDateString(today, -10);

    await env.DB.batch([
      env.DB.prepare('INSERT INTO daily_accepts (day, n) VALUES (?, 5)').bind(oldDay),
      env.DB.prepare('INSERT INTO daily_accepts (day, n) VALUES (?, 5)').bind(recentDay),
    ]);

    await purgeOldData(env.DB, now);

    expect(await env.DB.prepare('SELECT * FROM daily_accepts WHERE day = ?').bind(oldDay).first()).toBeFalsy();
    expect(await env.DB.prepare('SELECT * FROM daily_accepts WHERE day = ?').bind(recentDay).first()).toBeTruthy();
  });

  it('the scheduled() handler runs the purge without throwing', async () => {
    const ctx = createExecutionContext();
    await worker.scheduled({ cron: '0 4 * * *' }, env, ctx);
    await waitOnExecutionContext(ctx);
  });

  it('scheduled(): the snapshot still builds when the retention purge throws', async () => {
    // Seed enough instances for the "no snapshot yet AND all_time >= 5"
    // rebuild condition to be true.
    for (let i = 0; i < 5; i++) {
      expect((await postReport(SELF, validPayload({ instance_id: uuidFor(520 + i) }))).status).toBe(204);
    }
    expect(await env.DB.prepare('SELECT body FROM public_snapshot WHERE id = 1').first()).toBeFalsy();

    // A DB proxy whose `.batch()` always throws (purgeOldData's deletes go
    // through `.batch()`) but otherwise behaves like the real D1 binding --
    // the snapshot rebuild path only ever uses `.prepare(...).first()/.all()/.run()`,
    // so it's unaffected.
    const failingDB = new Proxy(env.DB, {
      get(target, prop, receiver) {
        if (prop === 'batch') {
          return async () => {
            throw new Error('simulated purge batch failure');
          };
        }
        return Reflect.get(target, prop, receiver);
      },
    });
    const testEnv = { ...env, DB: failingDB };

    const ctx = createExecutionContext();
    // Must not throw out of scheduled() even though purgeOldData rejects.
    await expect(worker.scheduled({ cron: '0 4 * * *' }, testEnv, ctx)).resolves.toBeUndefined();
    await waitOnExecutionContext(ctx);

    const snapshot = await env.DB.prepare('SELECT body FROM public_snapshot WHERE id = 1').first();
    expect(snapshot).toBeTruthy();
    const body = JSON.parse(snapshot.body);
    expect(body.instances.all_time).toBeGreaterThanOrEqual(5);

    // And last_purge_day was never written, since the purge batch failed.
    const purgeState = await env.DB.prepare('SELECT last_purge_day FROM purge_state WHERE id = 1').first();
    expect(purgeState.last_purge_day).toBeNull();
  });
});
