import { describe, it, expect } from 'vitest';
import { SELF, env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker from '../src/index.js';
import { validPayload, uuidFor, postReport } from './fixtures.js';
import { utcTodayString, monthOf, addDaysToDateString } from '../src/dates.js';

async function loadTotals() {
  const row = await env.DB.prepare('SELECT reports, energy_wh, co2e_g, tokens_in, tokens_out FROM totals WHERE id = 1').first();
  return row;
}

describe('POST /v1/report', () => {
  it('stores a valid report: 204, and a row in all four tables', async () => {
    const payload = validPayload({ instance_id: uuidFor(1) });
    const res = await postReport(SELF, payload);
    expect(res.status).toBe(204);
    expect(await res.text()).toBe('');

    const today = utcTodayString();

    const report = await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    expect(report).toBeTruthy();
    expect(report.received_day).toBe(today);
    expect(report.tokens_in).toBe(payload.tokens_in);
    expect(report.tokens_out).toBe(payload.tokens_out);
    expect(report.energy_wh).toBe(payload.energy_wh);
    expect(JSON.parse(report.providers)).toEqual(payload.providers);

    const instance = await env.DB.prepare('SELECT * FROM instances WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    expect(instance).toBeTruthy();
    expect(instance.first_seen_day).toBe(today);
    expect(instance.last_seen_day).toBe(today);
    expect(instance.latest_report_id).toBe(report.id);

    const totalsByKey = await loadTotals();
    expect(totalsByKey.reports).toBe(1);
    expect(totalsByKey.tokens_in).toBe(payload.tokens_in);
    expect(totalsByKey.tokens_out).toBe(payload.tokens_out);
    expect(totalsByKey.energy_wh).toBe(payload.energy_wh);
    expect(totalsByKey.co2e_g).toBe(payload.co2e_g);

    const dailyAccepts = await env.DB.prepare('SELECT n FROM daily_accepts WHERE day = ?').bind(today).first();
    expect(dailyAccepts.n).toBe(1);

    const month = monthOf(payload.window_end);
    const monthly = await env.DB.prepare('SELECT * FROM monthly_totals WHERE month = ?').bind(month).first();
    expect(monthly).toBeTruthy();
    expect(monthly.reports).toBe(1);
    expect(monthly.tokens_in).toBe(payload.tokens_in);
  });

  it('bumps totals by null-as-zero when energy_wh/co2e_g are null', async () => {
    const payload = validPayload({ instance_id: uuidFor(2), energy_wh: null, co2e_g: null });
    const res = await postReport(SELF, payload);
    expect(res.status).toBe(204);

    const totalsByKey = await loadTotals();
    expect(totalsByKey.energy_wh).toBe(0);
    expect(totalsByKey.co2e_g).toBe(0);
  });

  it('drops an unknown top-level field and never stores it', async () => {
    const payload = validPayload({ instance_id: uuidFor(3), unexpected_field: 'must not be stored anywhere' });
    const res = await postReport(SELF, payload);
    expect(res.status).toBe(204);

    const report = await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    expect(JSON.stringify(report)).not.toContain('must not be stored anywhere');
  });

  it('rejects an unknown key inside a closed-set map with 400, stores nothing, and never echoes the key', async () => {
    const payload = validPayload({ instance_id: uuidFor(4), providers: { anthropic: 0.5, mystery_provider_xyz: 0.5 } });
    const res = await postReport(SELF, payload);
    expect(res.status).toBe(400);
    const bodyText = await res.text();
    expect(bodyText).not.toContain('mystery_provider_xyz');

    const report = await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    expect(report).toBeFalsy();
  });

  it.each([
    ['tokens_in over the 1e12 cap', { tokens_in: 1e12 + 1 }],
    ['tokens_out over the 1e12 cap', { tokens_out: 1e12 + 1 }],
    ['energy_wh negative', { energy_wh: -1 }],
    ['energy_wh over the 1e9 cap', { energy_wh: 1e9 + 1 }],
    ['co2e_g negative', { co2e_g: -1 }],
    ['co2e_g over the 1e9 cap', { co2e_g: 1e9 + 1 }],
    ['a share outside [0,1]', { providers: { anthropic: 1.5 } }],
    ['a share map summing over 1.05', { providers: { anthropic: 0.9, local: 0.9 } }],
    ['window_end before 2026-01-01', { window_start: '2025-12-01', window_end: '2025-12-20' }],
  ])('rejects a report with %s: 400', async (_name, overrides) => {
    const payload = validPayload({ instance_id: uuidFor(5), ...overrides });
    const res = await postReport(SELF, payload);
    expect(res.status).toBe(400);
  });

  it('rejects a window longer than 36 days: 400', async () => {
    const payload = validPayload({ instance_id: uuidFor(6), window_start: '2026-01-01', window_end: '2026-02-10' });
    const res = await postReport(SELF, payload);
    expect(res.status).toBe(400);
  });

  it('rejects invalid JSON: 400', async () => {
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{not valid json',
    });
    expect(res.status).toBe(400);
  });

  it('rejects an empty body: 400', async () => {
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '',
    });
    expect(res.status).toBe(400);
  });

  it.each([
    ['missing Content-Type', {}],
    ['a non-JSON Content-Type', { 'content-type': 'text/plain' }],
  ])('%s: 415', async (_name, headers) => {
    const payload = validPayload({ instance_id: uuidFor(8) });
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
    expect(res.status).toBe(415);
  });

  it('413s a body over 8KB when Content-Length is honest', async () => {
    const big = 'x'.repeat(9000);
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': String(new TextEncoder().encode(big).byteLength) },
      body: big,
    });
    expect(res.status).toBe(413);
  });

  it('413s a body over 8KB even when Content-Length understates the real size', async () => {
    const big = 'x'.repeat(9000);
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', {
      method: 'POST',
      // Deliberately dishonest: claims a tiny body while actually sending
      // ~9000 bytes, so the only way this test passes is if the handler
      // enforces the cap against bytes it actually reads, not this header.
      headers: { 'content-type': 'application/json', 'content-length': '5' },
      body: big,
    });
    expect(res.status).toBe(413);
  });

  /** A valid Payload v1 body, padded with an unknown (dropped) top-level
   * field until its JSON encoding is exactly `targetBytes` long. */
  function payloadOfSize(targetBytes, instanceId) {
    const base = validPayload({ instance_id: instanceId });
    let padLen = 0;
    for (let attempt = 0; attempt < 5; attempt++) {
      const candidate = JSON.stringify({ ...base, padding: 'x'.repeat(padLen) });
      const size = new TextEncoder().encode(candidate).byteLength;
      if (size === targetBytes) return candidate;
      if (size > targetBytes) throw new Error(`overshot target size (${size} > ${targetBytes})`);
      padLen += targetBytes - size;
    }
    throw new Error('payloadOfSize did not converge');
  }

  it('accepts a body of exactly 8192 bytes', async () => {
    const body = payloadOfSize(8192, uuidFor(9));
    expect(new TextEncoder().encode(body).byteLength).toBe(8192);
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': '8192' },
      body,
    });
    expect(res.status).toBe(204);
  });

  it('rejects a body of exactly 8193 bytes with 413', async () => {
    const body = payloadOfSize(8193, uuidFor(10));
    expect(new TextEncoder().encode(body).byteLength).toBe(8193);
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': '8193' },
      body,
    });
    expect(res.status).toBe(413);
  });

  it('accepts a body whose bytes split a multi-byte UTF-8 character across chunks', async () => {
    // "café" -- the "é" (U+00E9) encodes as 2 UTF-8 bytes (0xC3 0xA9), and
    // an unknown top-level field carrying it is dropped by the validator
    // but still has to survive the *body read* as valid UTF-8 for the JSON
    // to parse at all. Split the stream mid-way through those two bytes;
    // decoding chunk-by-chunk (rather than once over the whole
    // concatenated body, as readBodyCapped does) would mangle it.
    const payload = validPayload({ instance_id: uuidFor(11), note: 'café' });
    const text = JSON.stringify(payload);
    const bytes = new TextEncoder().encode(text);
    const prefixLen = new TextEncoder().encode(text.slice(0, text.indexOf('café') + 3)).byteLength; // up to and including "caf"
    const splitIdx = prefixLen + 1; // one byte into "é" (0xC3), before its second byte (0xA9)

    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(bytes.slice(0, splitIdx));
        controller.enqueue(bytes.slice(splitIdx));
        controller.close();
      },
    });

    const request = {
      method: 'POST',
      url: 'https://telemetry.kithailab.com/v1/report',
      headers: new Headers({ 'content-type': 'application/json' }),
      body,
    };

    const ctx = createExecutionContext();
    const res = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);
    expect(res.status).toBe(204);

    const report = await env.DB.prepare('SELECT instance_id FROM reports WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    expect(report).toBeTruthy();
  });

  it('a second report for the same instance on the same UTC day is 429 and leaves totals unchanged', async () => {
    const payload = validPayload({ instance_id: uuidFor(7) });

    const first = await postReport(SELF, payload);
    expect(first.status).toBe(204);

    const totalsAfterFirst = await loadTotals();
    expect(totalsAfterFirst.reports).toBe(1);

    const second = await postReport(SELF, validPayload({ instance_id: payload.instance_id, tokens_in: 999 }));
    expect(second.status).toBe(429);
    expect(second.headers.get('x-content-type-options')).toBe('nosniff');
    expect(second.headers.get('cache-control')).toBe('no-store');

    const totalsAfterSecond = await loadTotals();
    expect(totalsAfterSecond.reports).toBe(1);

    const reportCount = await env.DB.prepare('SELECT COUNT(*) AS c FROM reports WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    expect(reportCount.c).toBe(1);

    const instance = await env.DB.prepare('SELECT tret_version FROM instances WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    // Confirms the *whole* batch rolled back, not just the reports insert:
    // the instance row still reflects the first report, not the second's
    // (different) tret_version, and there's still only one row for it.
    expect(instance.tret_version).toBe(payload.tret_version);
  });

  it('a same-instance report on a later UTC day advances last_seen_day/latest_report_id and preserves first_seen_day', async () => {
    const instanceId = uuidFor(12);
    const first = await postReport(SELF, validPayload({ instance_id: instanceId }));
    expect(first.status).toBe(204);

    const firstReport = await env.DB.prepare('SELECT id, received_day FROM reports WHERE instance_id = ?')
      .bind(instanceId)
      .first();
    const before = await env.DB.prepare('SELECT first_seen_day, last_seen_day, latest_report_id FROM instances WHERE instance_id = ?')
      .bind(instanceId)
      .first();
    expect(before.latest_report_id).toBe(firstReport.id);

    // Backdate the first report/instance row by a few days -- the same
    // technique retention.test.js uses to simulate the passage of time
    // without mocking the clock. That frees up "today" as a genuinely
    // different `received_day` for this instance, so posting again goes
    // through the real handler/insertReport upsert path exactly as a
    // later-day report from the same instance would.
    const backdated = addDaysToDateString(before.last_seen_day, -3);
    await env.DB.prepare('UPDATE reports SET received_day = ? WHERE id = ?').bind(backdated, firstReport.id).run();
    await env.DB.prepare('UPDATE instances SET first_seen_day = ?, last_seen_day = ? WHERE instance_id = ?')
      .bind(backdated, backdated, instanceId)
      .run();

    const second = await postReport(SELF, validPayload({ instance_id: instanceId, tret_version: '0.2.0' }));
    expect(second.status).toBe(204);

    const after = await env.DB.prepare('SELECT first_seen_day, last_seen_day, latest_report_id, tret_version FROM instances WHERE instance_id = ?')
      .bind(instanceId)
      .first();
    expect(after.first_seen_day).toBe(backdated); // preserved, not overwritten by the upsert
    expect(after.last_seen_day).toBe(before.last_seen_day); // back to "today"
    expect(after.latest_report_id).not.toBe(firstReport.id);
    expect(after.tret_version).toBe('0.2.0');

    // Both reports still exist -- this was a genuine second row, not an
    // overwrite of the first.
    const reportCount = await env.DB.prepare('SELECT COUNT(*) AS c FROM reports WHERE instance_id = ?')
      .bind(instanceId)
      .first();
    expect(reportCount.c).toBe(2);
  });

  it('the global daily accept cap rejects further reports with 429 and stores nothing once reached', async () => {
    const testEnv = { ...env, DAILY_ACCEPT_CAP: '2' };
    async function post(payload) {
      const request = new Request('https://telemetry.kithailab.com/v1/report', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const ctx = createExecutionContext();
      const res = await worker.fetch(request, testEnv, ctx);
      await waitOnExecutionContext(ctx);
      return res;
    }

    const first = await post(validPayload({ instance_id: uuidFor(701) }));
    const second = await post(validPayload({ instance_id: uuidFor(702) }));
    expect(first.status).toBe(204);
    expect(second.status).toBe(204);

    const third = await post(validPayload({ instance_id: uuidFor(703) }));
    expect(third.status).toBe(429);
    expect(third.headers.get('x-content-type-options')).toBe('nosniff');
    expect(third.headers.get('cache-control')).toBe('no-store');

    const stored = await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?').bind(uuidFor(703)).first();
    expect(stored).toBeFalsy();
  });

  it.each([
    ['400 (invalid JSON)', () =>
      SELF.fetch('https://telemetry.kithailab.com/v1/report', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: 'not json',
      })],
    ['413 (too large)', () =>
      SELF.fetch('https://telemetry.kithailab.com/v1/report', {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'content-length': String(9000) },
        body: 'x'.repeat(9000),
      })],
    ['415 (wrong content type)', () =>
      SELF.fetch('https://telemetry.kithailab.com/v1/report', { method: 'POST', body: '{}' })],
    ['404 (unknown route)', () => SELF.fetch('https://telemetry.kithailab.com/v1/nope')],
    ['405 (wrong method)', () => SELF.fetch('https://telemetry.kithailab.com/v1/report', { method: 'GET' })],
  ])('%s carries nosniff + no-store', async (_name, makeRequest) => {
    const res = await makeRequest();
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(res.headers.get('cache-control')).toBe('no-store');
  });

  it('GET /v1/report is 405', async () => {
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/report', { method: 'GET' });
    expect(res.status).toBe(405);
  });
});

describe('unknown routes and methods', () => {
  it('404s an unknown path', async () => {
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/nope');
    expect(res.status).toBe(404);
  });

  it('OPTIONS /v1/aggregates is a CORS preflight, not 404/405', async () => {
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/aggregates', { method: 'OPTIONS' });
    expect(res.status).toBe(204);
    expect(res.headers.get('access-control-allow-origin')).toBe('*');
  });

  it('POST /v1/aggregates is 405', async () => {
    const res = await SELF.fetch('https://telemetry.kithailab.com/v1/aggregates', { method: 'POST' });
    expect(res.status).toBe(405);
  });
});
