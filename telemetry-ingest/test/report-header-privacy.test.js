// Directly exercises the privacy rule at the heart of the report handler:
// it must read `request.method`, the URL, `content-type`, `content-length`,
// and the body -- and nothing else. Calls src/index.js's fetch() in-process (bypassing
// SELF's request/response round trip) so a Proxy over the request's headers
// can observe every `.get()` call the handler makes.
import { describe, it, expect } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker from '../src/index.js';
import { validPayload, uuidFor } from './fixtures.js';

function spyHeaders(init) {
  const real = new Headers(init);
  const seen = new Set();
  return {
    seen,
    headers: {
      get(name) {
        seen.add(String(name).toLowerCase());
        return real.get(name);
      },
      // Nothing in src/index.js should need anything beyond `.get()`, but
      // provide a couple of harmless pass-throughs in case that changes.
      has(name) {
        seen.add(String(name).toLowerCase());
        return real.has(name);
      },
    },
  };
}

function fakeRequestFor(payload, extraHeaders) {
  const text = JSON.stringify(payload);
  const bytes = new TextEncoder().encode(text);
  const { headers, seen } = spyHeaders({
    'content-length': String(bytes.byteLength),
    'content-type': 'application/json',
    // These must never be read by the report handler (contract §7/§0):
    // never store IP, user agent, country or any other request header.
    'cf-connecting-ip': '203.0.113.7',
    'user-agent': 'evil-ua/1.0',
    'x-forwarded-for': '203.0.113.7',
    ...extraHeaders,
  });

  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  });

  const request = {
    method: 'POST',
    url: 'https://telemetry.kithailab.com/v1/report',
    headers,
    body,
  };

  return { request, seen };
}

describe('report handler header access', () => {
  it('never reads any header other than content-type and content-length', async () => {
    const payload = validPayload({ instance_id: uuidFor(101) });
    const { request, seen } = fakeRequestFor(payload);

    const ctx = createExecutionContext();
    const res = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);

    expect(res.status).toBe(204);
    expect([...seen]).toEqual(['content-type', 'content-length']);
  });

  it('stores no column containing the injected CF-Connecting-IP / User-Agent values', async () => {
    const payload = validPayload({ instance_id: uuidFor(102) });
    const { request } = fakeRequestFor(payload, {
      'cf-connecting-ip': '198.51.100.42',
      'user-agent': 'suspicious-agent/9',
    });

    const ctx = createExecutionContext();
    const res = await worker.fetch(request, env, ctx);
    await waitOnExecutionContext(ctx);
    expect(res.status).toBe(204);

    const report = await env.DB.prepare('SELECT * FROM reports WHERE instance_id = ?')
      .bind(payload.instance_id)
      .first();
    const serialized = JSON.stringify(report);
    expect(serialized).not.toContain('198.51.100.42');
    expect(serialized).not.toContain('suspicious-agent/9');
  });
});
