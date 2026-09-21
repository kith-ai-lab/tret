// Payload v1 validator.
//
// Hand-written, dependency-free, allowlist-only: every field of the
// normalised object returned by `validatePayload` is built and checked
// individually. The raw parsed body is never stored or passed through --
// only the object this file constructs field by field ever reaches the
// database. That is the actual privacy boundary of this Worker: even if a
// future core build (or a modified fork) started sending extra data, an
// unknown top-level field is silently dropped and an unknown key *inside* a
// closed-set map is a hard 400, never stored.
//
// The closed sets and numeric caps below are mirrored from the shared
// telemetry contract §3 (payload shape) and §7 (ingest sanity caps). Keep
// them in sync with the contract, not with whatever core happens to send.

import { daysBetween, utcTodayString, addDaysToDateString } from './dates.js';

const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TRET_VERSION_RE = /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$/;
const DB_RE = /^(postgres-[0-9]{1,3}|sqlite)$/;

const DEPLOY_SET = new Set(['docker', 'fly', 'render', 'bare']);

// users_bucket and workspaces_bucket share the same bucket ladder.
const USERS_WORKSPACES_BUCKET_SET = new Set(['0', '1', '2-5', '6-20', '21-100', '100+']);
const RUNS_BUCKET_SET = new Set(['0', '1-10', '11-100', '101-1000', '1001-10000', '10000+']);

const PROVIDERS_SET = new Set(['anthropic', 'kimi', 'openrouter', 'local', 'other']);

const MODEL_FAMILIES_SET = new Set([
  'claude', 'gpt', 'gemini', 'gemma', 'llama', 'mistral', 'qwen', 'deepseek',
  'kimi', 'grok', 'phi', 'command', 'local/other', 'other',
]);

const TASK_TYPES_SET = new Set(['chat', 'freeform', 'pack']);

const RUN_STATUS_SET = new Set([
  'queued', 'running', 'completed', 'completed_without_output', 'failed', 'cancelled', 'other',
]);

// factor_rungs: which layer of core's emission-factor ladder supplied a run's
// grid factor. Core reports only the text BEFORE the first ":" of its stored
// `grid_co2e_source` (so `provider:anthropic` -> `provider`, `managed:<name>`
// -> `managed`; the provider, zone or managed-source name never leaves the
// instance). The names are core's layer names (`LAYER_PRECEDENCE` in
// backend/tret/services/emission_factors.py) plus its older grid rules
// (`GRID_SOURCE_RULES` in backend/tret/services/emissions.py); a run with no
// stored source is `legacy`, anything else core folds to `other`. Keep this
// set identical to core's -- a key outside it is a 400 for the whole report.
const FACTOR_RUNGS_SET = new Set([
  'run_override', 'harness', 'workspace', 'managed', 'env', 'dataset',
  'provider', 'local_setting', 'global_default', 'legacy', 'other',
]);

const FEATURE_KEYS = ['packs', 'connections', 'delegation', 'local_models'];

// window_end must be on/after this date (contract §7) -- a floor that
// predates any real Tret deployment, there purely to reject garbage clocks.
const MIN_WINDOW_END_DATE = '2026-01-01';

// Per-report sanity caps scale with the reporting window (contract §7
// amendments B2): a fixed cap sized for a long window would still let a
// flood of short-window reports claim implausible per-day figures. `days`
// is clamped to at least 1 so a same-day window doesn't zero out the cap.
const TOKENS_PER_DAY_MAX = 2e9;
const ENERGY_CO2E_PER_DAY_MAX = 1e6;
const SHARE_SUM_MAX = 1.05;

class ValidationError extends Error {}

function fail(message) {
  throw new ValidationError(message);
}

function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function validateDateString(value, name) {
  if (typeof value !== 'string' || !DATE_RE.test(value)) fail(`${name}: must be a YYYY-MM-DD string`);
  const [y, m, d] = value.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  // Catches things like 2026-02-30, which Date.UTC silently rolls forward.
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) {
    fail(`${name}: not a real calendar date`);
  }
  return value;
}

function validateInt(value, name, { min, max }) {
  if (typeof value !== 'number' || !Number.isFinite(value) || !Number.isInteger(value)) {
    fail(`${name}: must be an integer`);
  }
  if (value < min || value > max) fail(`${name}: out of range`);
  return value;
}

function validateNullableNumber(value, name, { min, max }) {
  if (value === null) return null;
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(`${name}: must be a number or null`);
  if (value < min || value > max) fail(`${name}: out of range`);
  return value;
}

/** Share maps (providers, model_families, task_types, run_status, factor_rungs). */
function validateShareMap(value, name, allowedSet) {
  if (!isPlainObject(value)) fail(`${name}: must be an object`);
  const out = {};
  let sum = 0;
  for (const [key, share] of Object.entries(value)) {
    // Unknown key inside a closed-set map is a 400, never silently dropped
    // -- unlike unknown top-level fields, which core may legitimately add
    // in a newer version this Worker doesn't know about yet. The key itself
    // is never echoed back in the failure message (contract §7 amendments
    // S4): it's attacker-controlled input, and a 400 body is not the place
    // to reflect up to ~3 KB of whatever a caller sent.
    if (!allowedSet.has(key)) fail(`${name}: unknown key`);
    if (typeof share !== 'number' || !Number.isFinite(share)) fail(`${name}.${key}: must be a number`);
    if (share < 0 || share > 1) fail(`${name}.${key}: share must be in [0, 1]`);
    out[key] = share;
    sum += share;
  }
  if (sum > SHARE_SUM_MAX) fail(`${name}: shares sum to more than ${SHARE_SUM_MAX}`);
  return out;
}

function validateFeatures(value) {
  if (!isPlainObject(value)) fail('features: must be an object');
  const keys = Object.keys(value);
  if (keys.length !== FEATURE_KEYS.length || !FEATURE_KEYS.every((k) => k in value)) {
    fail('features: must have exactly packs, connections, delegation, local_models');
  }
  const out = {};
  for (const key of FEATURE_KEYS) {
    if (typeof value[key] !== 'boolean') fail(`features.${key}: must be a boolean`);
    out[key] = value[key];
  }
  return out;
}

/**
 * Validate and normalise a parsed JSON body as Payload v1.
 *
 * Returns `{ ok: true, value }` with a clean, allowlisted object, or
 * `{ ok: false, error }` with a human-readable reason (safe to put in a 400
 * body -- it only ever describes the shape of the *rejected* payload, never
 * echoes request metadata).
 *
 * `now` is injectable for tests; production code should leave it as the
 * default so "today" and "window_end must not be too far in the future"
 * are evaluated against the real clock.
 */
export function validatePayload(raw, { now = new Date() } = {}) {
  if (!isPlainObject(raw)) return { ok: false, error: 'body must be a JSON object' };

  try {
    const out = {};

    if (raw.schema_version !== 1) fail('schema_version: must be 1');
    out.schema_version = 1;

    if (typeof raw.instance_id !== 'string' || !UUID_V4_RE.test(raw.instance_id)) {
      fail('instance_id: must be a UUIDv4 string');
    }
    out.instance_id = raw.instance_id.toLowerCase();

    out.window_start = validateDateString(raw.window_start, 'window_start');
    out.window_end = validateDateString(raw.window_end, 'window_end');

    const windowLength = daysBetween(out.window_start, out.window_end);
    if (windowLength < 0 || windowLength > 36) fail('window: length must be 0-36 days');
    // Window-scaled per-report caps (contract §7 amendments B2): a 0-day
    // window is treated as 1 day so the cap is never zeroed out.
    const windowDays = Math.max(1, windowLength);
    const tokensMax = TOKENS_PER_DAY_MAX * windowDays;
    const energyCo2eMax = ENERGY_CO2E_PER_DAY_MAX * windowDays;

    const today = utcTodayString(now);
    const tomorrow = addDaysToDateString(today, 1);
    if (out.window_end > tomorrow) fail('window_end: cannot be more than 1 day in the future');
    if (out.window_end < MIN_WINDOW_END_DATE) fail(`window_end: cannot be before ${MIN_WINDOW_END_DATE}`);

    if (typeof raw.tret_version !== 'string' || raw.tret_version.length > 32 || !TRET_VERSION_RE.test(raw.tret_version)) {
      fail('tret_version: must be a semver-like string');
    }
    out.tret_version = raw.tret_version;

    if (typeof raw.deploy !== 'string' || !DEPLOY_SET.has(raw.deploy)) fail('deploy: unknown value');
    out.deploy = raw.deploy;

    if (typeof raw.db !== 'string' || !DB_RE.test(raw.db)) fail('db: must be postgres-<major> or sqlite');
    out.db = raw.db;

    if (typeof raw.users_bucket !== 'string' || !USERS_WORKSPACES_BUCKET_SET.has(raw.users_bucket)) {
      fail('users_bucket: unknown bucket');
    }
    out.users_bucket = raw.users_bucket;

    if (typeof raw.workspaces_bucket !== 'string' || !USERS_WORKSPACES_BUCKET_SET.has(raw.workspaces_bucket)) {
      fail('workspaces_bucket: unknown bucket');
    }
    out.workspaces_bucket = raw.workspaces_bucket;

    if (typeof raw.runs_bucket !== 'string' || !RUNS_BUCKET_SET.has(raw.runs_bucket)) {
      fail('runs_bucket: unknown bucket');
    }
    out.runs_bucket = raw.runs_bucket;

    out.tokens_in = validateInt(raw.tokens_in, 'tokens_in', { min: 0, max: tokensMax });
    out.tokens_out = validateInt(raw.tokens_out, 'tokens_out', { min: 0, max: tokensMax });

    out.providers = validateShareMap(raw.providers, 'providers', PROVIDERS_SET);
    out.model_families = validateShareMap(raw.model_families, 'model_families', MODEL_FAMILIES_SET);
    out.task_types = validateShareMap(raw.task_types, 'task_types', TASK_TYPES_SET);
    out.run_status = validateShareMap(raw.run_status, 'run_status', RUN_STATUS_SET);

    out.energy_wh = validateNullableNumber(raw.energy_wh, 'energy_wh', { min: 0, max: energyCo2eMax });
    out.co2e_g = validateNullableNumber(raw.co2e_g, 'co2e_g', { min: 0, max: energyCo2eMax });

    out.factor_rungs = validateShareMap(raw.factor_rungs, 'factor_rungs', FACTOR_RUNGS_SET);
    out.features = validateFeatures(raw.features);

    // Every key of `raw` not assigned above (i.e. not one of the fields
    // Payload v1 defines) is simply never read -- that's the "unknown
    // top-level fields dropped silently" rule from contract §7. `out` never
    // contains anything beyond what this function explicitly wrote to it.
    return { ok: true, value: out };
  } catch (err) {
    if (err instanceof ValidationError) return { ok: false, error: err.message };
    throw err;
  }
}
