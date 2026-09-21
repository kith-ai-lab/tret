// Tret telemetry ingest Worker.
//
// Routes:
//   POST /v1/report            - accept one Payload v1 report
//   GET  /v1/aggregates         - public, floored aggregates (served from a stored snapshot)
//   OPTIONS /v1/aggregates      - CORS preflight for the above
//   GET  /v1/admin/instances    - private, unfloored fleet view (bearer auth)
//   *    (anything else)        - 404, or 405 for a known path/wrong method
//
// scheduled() runs the daily retention purge and, only when the rebuild
// rule is met, rebuilds the public aggregates snapshot (see
// src/retention.js, src/aggregates.js).
//
// The single most important privacy rule in this file lives in
// `handleReport`: that function is only allowed to read `request.method`,
// the URL path, the `content-length` header, the `content-type` header,
// and the request body. It must never read `cf`, any `CF-*` header,
// `User-Agent`, or anything else -- there is nothing in a telemetry report
// that should ever be joined against request metadata. `checkAdminAuth`
// (src/admin.js) is the one place this Worker reads a header beyond that,
// because the admin endpoint is authenticated and not part of the
// anonymous reporting path.

import { validatePayload } from './schema.js';
import { buildAdminInstances, readPublicSnapshotBody, shouldRebuildSnapshot, rebuildPublicSnapshot } from './aggregates.js';
import { checkAdminAuth } from './admin.js';
import { purgeOldData } from './retention.js';
import { utcTodayString, monthOf } from './dates.js';

// Contract §7: bodies over 8 KB are rejected with 413.
const MAX_BODY_BYTES = 8192;

// Fallback only: wrangler.jsonc declares a `DAILY_ACCEPT_CAP` var with this
// same default, so this is only reached if a deploy somehow drops the var.
const DEFAULT_DAILY_ACCEPT_CAP = 2000;

const AGGREGATES_CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

// Contract §7 amendments S4: every non-2xx response carries these two
// headers. `nosniff` stops a browser from trying to sniff/render an error
// body as something it isn't; `no-store` stops any cache (including the
// requester's own) from holding on to an error response.
const ERROR_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'Cache-Control': 'no-store',
};

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

/**
 * Build a non-2xx response. `body` is never client-supplied input (contract
 * §7 amendments S4: a 400/413/415/429 body must never echo an
 * attacker-controlled key or value) -- always a fixed string or `null`.
 */
function errorResponse(body, status, headers = {}) {
  return new Response(body, { status, headers: { ...ERROR_HEADERS, ...headers } });
}

function methodNotAllowed(allowed) {
  return errorResponse('method not allowed', 405, { Allow: allowed.join(', ') });
}

/**
 * Read the request body as text, enforcing `capBytes` while streaming
 * rather than trusting `Content-Length` (a caller can send any header it
 * likes; only the bytes actually read are ever counted). Cancels the
 * stream as soon as the cap is exceeded instead of buffering the rest. The
 * whole body is concatenated into one buffer before it's ever decoded as
 * text, so a multi-byte UTF-8 character split across two underlying stream
 * chunks decodes correctly.
 */
async function readBodyCapped(request, capBytes) {
  if (!request.body) return { text: '', tooLarge: false };

  const reader = request.body.getReader();
  let received = 0;
  const chunks = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > capBytes) {
      await reader.cancel();
      return { text: '', tooLarge: true };
    }
    chunks.push(value);
  }

  const buf = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    buf.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { text: new TextDecoder().decode(buf), tooLarge: false };
}

/** Does `message` look like a D1/SQLite unique-constraint violation? */
function isUniqueConstraintMessage(message) {
  return typeof message === 'string' && /UNIQUE constraint failed/i.test(message);
}

/**
 * True when `err` is D1's unique-constraint failure. Checked against both
 * `err.message` and `err.cause?.message` (contract §7 amendments N11):
 * depending on the D1/workerd version, the SQLite error text can arrive
 * wrapped in a `cause` rather than on the error itself, and missing that
 * would make a same-day duplicate report throw a 500 instead of the
 * documented 429.
 */
function isUniqueConstraintError(err) {
  return isUniqueConstraintMessage(err?.message) || isUniqueConstraintMessage(err?.cause?.message);
}

/**
 * Insert one validated report and, in the same atomic `env.DB.batch()`,
 * upsert its instance row, bump the running totals, and bump the global
 * daily accept counter. D1 batches run as a single implicit transaction:
 * if the `reports` insert fails its unique constraint (a second report for
 * this instance today), every statement in the batch is rolled back, so
 * the instance/totals/daily-accepts updates never happen -- this is what
 * makes the 429 path leave the database untouched.
 */
async function insertReport(db, value, receivedDay) {
  const month = monthOf(value.window_end);
  const energyDelta = value.energy_wh ?? 0;
  const co2eDelta = value.co2e_g ?? 0;

  const statements = [
    db
      .prepare(
        `INSERT INTO reports (
           received_day, instance_id, schema_version, window_start, window_end,
           tret_version, deploy, db, users_bucket, workspaces_bucket, runs_bucket,
           tokens_in, tokens_out, energy_wh, co2e_g,
           providers, model_families, task_types, run_status, factor_rungs, features
         ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
      )
      .bind(
        receivedDay,
        value.instance_id,
        value.schema_version,
        value.window_start,
        value.window_end,
        value.tret_version,
        value.deploy,
        value.db,
        value.users_bucket,
        value.workspaces_bucket,
        value.runs_bucket,
        value.tokens_in,
        value.tokens_out,
        value.energy_wh,
        value.co2e_g,
        JSON.stringify(value.providers),
        JSON.stringify(value.model_families),
        JSON.stringify(value.task_types),
        JSON.stringify(value.run_status),
        JSON.stringify(value.factor_rungs),
        JSON.stringify(value.features)
      ),

    // `latest_report_id` is looked up by (instance_id, received_day), which
    // the unique index above guarantees resolves to exactly the row just
    // inserted -- no dependency on last_insert_rowid() across statements.
    // `first_seen_day` is only ever written on the initial INSERT: the
    // ON CONFLICT clause below deliberately has no `first_seen_day =
    // excluded.first_seen_day`, so a same-instance/different-day upsert
    // leaves it untouched while `last_seen_day` and `latest_report_id`
    // always advance to this report.
    db
      .prepare(
        `INSERT INTO instances (instance_id, first_seen_day, last_seen_day, tret_version, latest_report_id)
         VALUES (?, ?, ?, ?, (SELECT id FROM reports WHERE instance_id = ? AND received_day = ?))
         ON CONFLICT (instance_id) DO UPDATE SET
           last_seen_day = excluded.last_seen_day,
           tret_version = excluded.tret_version,
           latest_report_id = excluded.latest_report_id`
      )
      .bind(value.instance_id, receivedDay, receivedDay, value.tret_version, value.instance_id, receivedDay),

    db
      .prepare(
        'UPDATE totals SET reports = reports + 1, energy_wh = energy_wh + ?, co2e_g = co2e_g + ?, tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE id = 1'
      )
      .bind(energyDelta, co2eDelta, value.tokens_in, value.tokens_out),

    db
      .prepare(
        `INSERT INTO monthly_totals (month, reports, energy_wh, co2e_g, tokens_in, tokens_out)
         VALUES (?, 1, ?, ?, ?, ?)
         ON CONFLICT (month) DO UPDATE SET
           reports = monthly_totals.reports + 1,
           energy_wh = monthly_totals.energy_wh + excluded.energy_wh,
           co2e_g = monthly_totals.co2e_g + excluded.co2e_g,
           tokens_in = monthly_totals.tokens_in + excluded.tokens_in,
           tokens_out = monthly_totals.tokens_out + excluded.tokens_out`
      )
      .bind(month, energyDelta, co2eDelta, value.tokens_in, value.tokens_out),

    // Global daily accept cap (contract §7 amendments B2): incremented
    // here, inside the same batch, so it only ever counts *accepted*
    // reports -- if the batch rolls back (duplicate instance/day), this
    // never advances either.
    db
      .prepare(
        `INSERT INTO daily_accepts (day, n) VALUES (?, 1)
         ON CONFLICT (day) DO UPDATE SET n = daily_accepts.n + 1`
      )
      .bind(receivedDay),
  ];

  await db.batch(statements);
}

async function handleReport(request, env) {
  // Contract §7 amendments B2: a CORS preflight the Worker never
  // satisfies, so a browser can't be used as a drive-by reporter.
  const contentType = request.headers.get('content-type') || '';
  if (!contentType.toLowerCase().startsWith('application/json')) {
    return errorResponse('unsupported content type', 415);
  }

  // Cheap early-out using the declared length (still not trusted alone --
  // readBodyCapped enforces the real cap against bytes actually read).
  const declaredLength = Number(request.headers.get('content-length'));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
    return errorResponse('payload too large', 413);
  }

  const { text, tooLarge } = await readBodyCapped(request, MAX_BODY_BYTES);
  if (tooLarge) return errorResponse('payload too large', 413);

  let json;
  try {
    json = JSON.parse(text);
  } catch {
    return errorResponse('invalid JSON body', 400);
  }

  const result = validatePayload(json);
  if (!result.ok) return errorResponse(result.error, 400);

  const receivedDay = utcTodayString();

  // Global daily accept cap (contract §7 amendments B2): pre-read the
  // counter and reject *before* touching the batch when it already meets
  // the cap. This is a read-then-decide check outside the transaction, so
  // concurrent requests can both pass it before either commits -- a small
  // overshoot is possible and is an accepted tradeoff (see the comment on
  // the `daily_accepts` table in migrations/0001_init.sql) in exchange for
  // not serializing every report through a single lock.
  const cap = Number(env.DAILY_ACCEPT_CAP ?? DEFAULT_DAILY_ACCEPT_CAP);
  const acceptedToday = await env.DB.prepare('SELECT n FROM daily_accepts WHERE day = ?').bind(receivedDay).first();
  if ((acceptedToday?.n ?? 0) >= cap) {
    return errorResponse(null, 429);
  }

  try {
    await insertReport(env.DB, result.value, receivedDay);
  } catch (err) {
    if (isUniqueConstraintError(err)) {
      // Contract §7: a report for this instance_id was already accepted
      // today (UTC). The batch rolled back, so nothing else changed.
      return errorResponse(null, 429);
    }
    throw err;
  }

  return new Response(null, { status: 204 });
}

async function handleAggregates(env) {
  // Contract §7 amendments: served verbatim from the stored snapshot,
  // never computed against live data for this request.
  const bodyText = await readPublicSnapshotBody(env.DB);
  return new Response(bodyText, {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...AGGREGATES_CORS_HEADERS, 'Cache-Control': 'public, max-age=3600' },
  });
}

async function handleAdminInstances(request, env) {
  // Contract §7 amendments S5: every admin response -- 200, 401 or 503 --
  // carries `Cache-Control: no-store` and `Vary: Authorization`, so
  // nothing (an intermediate cache, the browser) ever serves a cached
  // admin response to a request with a different (or missing) token.
  const adminHeaders = { 'Cache-Control': 'no-store', Vary: 'Authorization' };

  const auth = await checkAdminAuth(request, env);
  if (!auth.ok) {
    return errorResponse(auth.error, auth.status, adminHeaders);
  }

  const data = await buildAdminInstances(env.DB);
  return jsonResponse(data, { headers: adminHeaders });
}

/**
 * Runs both scheduled steps -- retention purge and (conditionally) the
 * public snapshot rebuild -- each in its own try/catch (contract §7
 * amendments S7), so a failure in one (e.g. the purge throwing) never
 * prevents the other from running.
 */
export async function runScheduledTasks(env, now = new Date()) {
  try {
    await purgeOldData(env.DB, now);
  } catch (err) {
    console.error('retention purge failed', err);
  }

  try {
    const today = utcTodayString(now);
    if (await shouldRebuildSnapshot(env.DB, today)) {
      await rebuildPublicSnapshot(env.DB, now);
    }
  } catch (err) {
    console.error('public snapshot rebuild failed', err);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === '/v1/report') {
      if (request.method !== 'POST') return methodNotAllowed(['POST']);
      return handleReport(request, env);
    }

    if (path === '/v1/aggregates') {
      if (request.method === 'OPTIONS') {
        return new Response(null, { status: 204, headers: AGGREGATES_CORS_HEADERS });
      }
      if (request.method !== 'GET') return methodNotAllowed(['GET', 'OPTIONS']);
      return handleAggregates(env);
    }

    if (path === '/v1/admin/instances') {
      if (request.method !== 'GET') return methodNotAllowed(['GET']);
      return handleAdminInstances(request, env);
    }

    return errorResponse('not found', 404);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(runScheduledTasks(env));
  },
};
