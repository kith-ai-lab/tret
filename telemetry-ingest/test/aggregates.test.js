import { describe, it, expect } from 'vitest';
import { SELF, env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker from '../src/index.js';
import { validPayload, uuidFor, postReport } from './fixtures.js';
import { utcTodayString, addDaysToDateString } from '../src/dates.js';

async function getAggregates() {
  const res = await SELF.fetch('https://telemetry.kithailab.com/v1/aggregates');
  expect(res.status).toBe(200);
  expect(res.headers.get('access-control-allow-origin')).toBe('*');
  expect(res.headers.get('cache-control')).toBe('public, max-age=3600');
  return res.json();
}

async function runScheduled() {
  const ctx = createExecutionContext();
  await worker.scheduled({ cron: '0 4 * * *' }, env, ctx);
  await waitOnExecutionContext(ctx);
}

/** Backdates an instance's first_seen_day so it counts as "seasoned"
 * (first_seen_day <= today - 7) for the public breakdowns gate. */
async function backdateFirstSeen(instanceId, daysAgo) {
  const backdated = addDaysToDateString(utcTodayString(), -daysAgo);
  await env.DB.prepare('UPDATE instances SET first_seen_day = ? WHERE instance_id = ?')
    .bind(backdated, instanceId)
    .run();
}

describe('GET /v1/aggregates', () => {
  it('before any snapshot has ever been built: the documented empty shape, not a live computation', async () => {
    const body = await getAggregates();
    expect(body.instances.active_35d).toBe(0);
    expect(body.instances.all_time).toBe(0);
    expect(body.breakdowns_min_instances).toBe(10);
    expect(body.floor).toBe(5);
    expect(body.totals).toBeNull();
    expect(body.monthly).toBeNull();
    expect(body.breakdowns).toBeNull();
    expect(body.snapshot_day).toBeNull();
  });

  it('posting reports never changes the aggregates response by itself -- only a scheduled rebuild does', async () => {
    const before = await getAggregates();
    for (let i = 0; i < 4; i++) {
      expect((await postReport(SELF, validPayload({ instance_id: uuidFor(200 + i) }))).status).toBe(204);
    }
    const after = await getAggregates();
    expect(after).toEqual(before);
  });

  it('the rebuild rule: no snapshot yet needs all_time >= 5; once built, needs >=5 more reports since', async () => {
    for (let i = 0; i < 5; i++) {
      expect((await postReport(SELF, validPayload({ instance_id: uuidFor(300 + i) }))).status).toBe(204);
    }
    await runScheduled();
    const firstBuild = await getAggregates();
    expect(firstBuild.snapshot_day).not.toBeNull();
    expect(firstBuild.totals).not.toBeNull();
    expect(firstBuild.totals.reports).toBe(5);

    // 4 more reports: below the >=5-new-since-snapshot rebuild threshold,
    // so even after running scheduled(), the snapshot must be unchanged.
    for (let i = 0; i < 4; i++) {
      expect((await postReport(SELF, validPayload({ instance_id: uuidFor(310 + i) }))).status).toBe(204);
    }
    await runScheduled();
    const stillUnchanged = await getAggregates();
    expect(stillUnchanged.snapshot_day).toBe(firstBuild.snapshot_day);
    expect(stillUnchanged.totals.reports).toBe(5);

    // A 5th new report crosses the threshold; scheduled() now rebuilds.
    expect((await postReport(SELF, validPayload({ instance_id: uuidFor(319) }))).status).toBe(204);
    await runScheduled();
    const rebuilt = await getAggregates();
    expect(rebuilt.totals.reports).toBe(10);
  });

  it('breakdowns stay null below 10 *seasoned* active instances, even once totals are populated', async () => {
    // 9 fresh (unseasoned -- first_seen_day is today) instances: well past
    // the all_time >= 5 gate, so totals populate on rebuild, but
    // breakdowns still require 10 *seasoned* instances.
    for (let i = 0; i < 9; i++) {
      expect((await postReport(SELF, validPayload({ instance_id: uuidFor(400 + i) }))).status).toBe(204);
    }
    await runScheduled();
    const body = await getAggregates();
    expect(body.totals).not.toBeNull();
    expect(body.breakdowns).toBeNull();
  });

  it('breakdowns only tally seasoned, active instances, and only share-map keys with share > 0', async () => {
    // 10 seasoned instances (first_seen_day backdated past the 7-day
    // seasoning window), all reporting providers: { anthropic: 1, local: 0 }
    // -- "local" has a zero share and must never be tallied.
    const seasonedIds = [];
    for (let i = 0; i < 10; i++) {
      const instanceId = uuidFor(500 + i);
      seasonedIds.push(instanceId);
      const res = await postReport(
        SELF,
        validPayload({ instance_id: instanceId, deploy: 'fly', providers: { anthropic: 1, local: 0 } })
      );
      expect(res.status).toBe(204);
      await backdateFirstSeen(instanceId, 10);
    }

    // 2 unseasoned instances that must be excluded from the breakdowns
    // tally entirely, even though they're active.
    for (let i = 0; i < 2; i++) {
      expect(
        (await postReport(SELF, validPayload({ instance_id: uuidFor(520 + i), deploy: 'render' }))).status
      ).toBe(204);
    }

    await runScheduled();
    const body = await getAggregates();
    expect(body.breakdowns).not.toBeNull();
    expect(body.breakdowns.deploy.fly).toBe(10);
    expect(body.breakdowns.deploy.render).toBeUndefined(); // only 2 backing instances, and unseasoned besides
    expect(body.breakdowns.providers.anthropic).toBe(10);
    expect(body.breakdowns.providers.local).toBeUndefined(); // zero-share key, never tallied
  });

  it('the public response never contains admin-only fields', async () => {
    const body = await getAggregates();
    const serialized = JSON.stringify(body);
    expect(serialized).not.toContain('users_bucket');
    expect(serialized).not.toContain('workspaces_bucket');
    expect(serialized).not.toContain('runs_bucket');
    expect(serialized).not.toMatch(/"db"/);
  });
});
