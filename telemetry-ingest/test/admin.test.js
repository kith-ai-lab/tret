import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker from '../src/index.js';

// ADMIN_TOKEN isn't declared as a wrangler.jsonc binding (it's a secret,
// set out of band in production -- see README DEPLOY). These tests build
// their own `env` per case by spreading the real `env` (so env.DB is still
// the live local D1) and overriding ADMIN_TOKEN, since that's the only
// clean way to exercise "unset" vs "set" without a real secret.
async function callAdminInstances(testEnv, headers = {}) {
  const request = new Request('https://telemetry.kithailab.com/v1/admin/instances', { headers });
  const ctx = createExecutionContext();
  const res = await worker.fetch(request, testEnv, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

function expectNoCacheAdminHeaders(res) {
  expect(res.headers.get('cache-control')).toBe('no-store');
  expect(res.headers.get('vary')).toBe('Authorization');
  expect(res.headers.get('access-control-allow-origin')).toBeNull();
}

describe('GET /v1/admin/instances', () => {
  it('503s when ADMIN_TOKEN is not configured', async () => {
    const testEnv = { ...env, ADMIN_TOKEN: undefined };
    const res = await callAdminInstances(testEnv, { authorization: 'Bearer anything' });
    expect(res.status).toBe(503);
    expectNoCacheAdminHeaders(res);
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
  });

  it('401s with a wrong token', async () => {
    const testEnv = { ...env, ADMIN_TOKEN: 'correct-secret' };
    const res = await callAdminInstances(testEnv, { authorization: 'Bearer wrong-secret' });
    expect(res.status).toBe(401);
    expectNoCacheAdminHeaders(res);
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
  });

  it('401s with no Authorization header at all', async () => {
    const testEnv = { ...env, ADMIN_TOKEN: 'correct-secret' };
    const res = await callAdminInstances(testEnv);
    expect(res.status).toBe(401);
  });

  it('200s with the right bearer token and returns the fleet shape', async () => {
    const testEnv = { ...env, ADMIN_TOKEN: 'correct-secret' };
    const res = await callAdminInstances(testEnv, { authorization: 'Bearer correct-secret' });
    expect(res.status).toBe(200);
    expectNoCacheAdminHeaders(res);
    const body = await res.json();
    expect(body).toHaveProperty('instances.active_35d');
    expect(body).toHaveProperty('instances.all_time');
    expect(body).toHaveProperty('instances.new_30d');
    expect(body).toHaveProperty('weekly_active');
    expect(body).toHaveProperty('totals.reports');
    // Contract §7 amendments S7: exposed so a stuck purge/snapshot is
    // visible rather than silent. No data yet in this test -> both null.
    expect(body).toHaveProperty('last_purge_day', null);
    expect(body).toHaveProperty('snapshot_day', null);
  });

  it('accepts the bearer scheme case-insensitively and trims the token', async () => {
    const testEnv = { ...env, ADMIN_TOKEN: 'correct-secret' };
    const lower = await callAdminInstances(testEnv, { authorization: 'bearer correct-secret' });
    expect(lower.status).toBe(200);

    const mixed = await callAdminInstances(testEnv, { authorization: 'BEARER   correct-secret  ' });
    expect(mixed.status).toBe(200);
  });

  it('GET /v1/admin/instances via POST is 405', async () => {
    const testEnv = { ...env, ADMIN_TOKEN: 'correct-secret' };
    const request = new Request('https://telemetry.kithailab.com/v1/admin/instances', {
      method: 'POST',
      headers: { authorization: 'Bearer correct-secret' },
    });
    const ctx = createExecutionContext();
    const res = await worker.fetch(request, testEnv, ctx);
    await waitOnExecutionContext(ctx);
    expect(res.status).toBe(405);
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(res.headers.get('cache-control')).toBe('no-store');
  });
});
