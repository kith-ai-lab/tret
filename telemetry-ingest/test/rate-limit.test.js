// Per-sender limit on POST /v1/report (src/index.js `isRateLimited`).
// The Workers Rate Limiting binding is replaced with a small fake so the
// tests are deterministic and can observe exactly what key the Worker hands it.
import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker, { senderKey } from '../src/index.js';
import { validPayload, uuidFor } from './fixtures.js';

function fakeLimiter(allowance) {
  const calls = [];
  return {
    calls,
    async limit({ key }) {
      calls.push(key);
      const n = calls.filter((k) => k === key).length;
      return { success: n <= allowance };
    },
  };
}

async function post(payload, workerEnv, headers = {}) {
  const request = new Request('https://telemetry.kithailab.com/v1/report', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(payload),
  });
  const ctx = createExecutionContext();
  const res = await worker.fetch(request, workerEnv, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

async function reportCount() {
  return (await env.DB.prepare('SELECT COUNT(*) AS n FROM reports').first()).n;
}

describe('per-sender rate limit on /v1/report', () => {
  it('429s a sender over its allowance, with Retry-After, and stores nothing for it', async () => {
    const limiter = fakeLimiter(2);
    const e = { ...env, REPORT_LIMITER: limiter };
    const ip = { 'cf-connecting-ip': '203.0.113.9' };

    expect((await post(validPayload({ instance_id: uuidFor(9001) }), e, ip)).status).toBe(204);
    expect((await post(validPayload({ instance_id: uuidFor(9002) }), e, ip)).status).toBe(204);
    const before = await reportCount();

    // A third *fresh* UUID from the same IP: the fake-instance flood case.
    const res = await post(validPayload({ instance_id: uuidFor(9003) }), e, ip);
    expect(res.status).toBe(429);
    expect(res.headers.get('retry-after')).toBe('60');
    expect(res.headers.get('cache-control')).toBe('no-store');
    expect(await reportCount()).toBe(before);
    const row = await env.DB.prepare('SELECT 1 AS x FROM instances WHERE instance_id = ?').bind(uuidFor(9003)).first();
    expect(row).toBeNull();
  });

  it('counts each sender separately', async () => {
    const e = { ...env, REPORT_LIMITER: fakeLimiter(1) };
    expect((await post(validPayload({ instance_id: uuidFor(9011) }), e, { 'cf-connecting-ip': '198.51.100.1' })).status).toBe(204);
    expect((await post(validPayload({ instance_id: uuidFor(9012) }), e, { 'cf-connecting-ip': '198.51.100.2' })).status).toBe(204);
  });

  it('passes the IP only to the limiter: it is not in any stored column', async () => {
    const limiter = fakeLimiter(5);
    const e = { ...env, REPORT_LIMITER: limiter };
    const payload = validPayload({ instance_id: uuidFor(9021) });
    expect((await post(payload, e, { 'cf-connecting-ip': '192.0.2.77' })).status).toBe(204);
    expect(limiter.calls).toEqual(['192.0.2.77']);
    const dump = JSON.stringify(await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?').bind(payload.instance_id).first())
      + JSON.stringify(await env.DB.prepare('SELECT * FROM instances WHERE instance_id = ?').bind(payload.instance_id).first())
      + JSON.stringify((await env.DB.prepare('SELECT * FROM daily_accepts').all()).results);
    expect(dump).not.toContain('192.0.2.77');
  });

  it('degrades open when the binding is absent', async () => {
    const { REPORT_LIMITER: _drop, ...noBinding } = env;
    const res = await post(validPayload({ instance_id: uuidFor(9031) }), noBinding, { 'cf-connecting-ip': '192.0.2.1' });
    expect(res.status).toBe(204);
  });

  it('degrades open when there is no CF-Connecting-IP header', async () => {
    const limiter = fakeLimiter(0);
    const res = await post(validPayload({ instance_id: uuidFor(9041) }), { ...env, REPORT_LIMITER: limiter });
    expect(res.status).toBe(204);
    expect(limiter.calls).toEqual([]);
  });

  it('lets the report through when the limiter itself throws', async () => {
    const broken = { async limit() { throw new Error('limiter down'); } };
    const res = await post(validPayload({ instance_id: uuidFor(9051) }), { ...env, REPORT_LIMITER: broken }, { 'cf-connecting-ip': '192.0.2.2' });
    expect(res.status).toBe(204);
  });

  it('a limited request does not consume the global daily cap', async () => {
    const e = { ...env, REPORT_LIMITER: fakeLimiter(0) };
    const before = (await env.DB.prepare('SELECT COALESCE(SUM(n),0) AS n FROM daily_accepts').first()).n;
    expect((await post(validPayload({ instance_id: uuidFor(9061) }), e, { 'cf-connecting-ip': '192.0.2.3' })).status).toBe(429);
    const after = (await env.DB.prepare('SELECT COALESCE(SUM(n),0) AS n FROM daily_accepts').first()).n;
    expect(after).toBe(before);
  });

  it('keys IPv6 senders on their /64, so one host cannot rotate through addresses', async () => {
    const e = { ...env, REPORT_LIMITER: fakeLimiter(1) };
    const a = await post(validPayload({ instance_id: uuidFor(9101) }), e, { 'cf-connecting-ip': '2001:db8:1:2::1' });
    const b = await post(validPayload({ instance_id: uuidFor(9102) }), e, { 'cf-connecting-ip': '2001:db8:1:2:ffff::9' });
    const c = await post(validPayload({ instance_id: uuidFor(9103) }), e, { 'cf-connecting-ip': '2001:db8:1:3::1' });
    expect(a.status).toBe(204);
    expect(b.status).toBe(429);
    expect(c.status).toBe(204);
    expect(e.REPORT_LIMITER.calls).toEqual(['2001:db8:1:2::/64', '2001:db8:1:2::/64', '2001:db8:1:3::/64']);
  });

  it('normalises sender keys', () => {
    expect(senderKey('198.51.100.7')).toBe('198.51.100.7');
    expect(senderKey('::ffff:198.51.100.7')).toBe('198.51.100.7');
    expect(senderKey('2001:0db8:0000:0001:aaaa:bbbb:cccc:dddd')).toBe('2001:db8:0:1::/64');
    expect(senderKey('2001:db8::1')).toBe('2001:db8:0:0::/64');
    expect(senderKey('::1')).toBe('0:0:0:0::/64');
  });
});
