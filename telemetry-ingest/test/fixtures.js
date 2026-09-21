// Shared test fixtures. Kept out of the individual test files so every test
// that needs "a valid Payload v1 body" builds it from the same base shape as
// the contract's own example (contract §3).

import { addDaysToDateString, utcTodayString } from '../src/dates.js';

export function validPayload(overrides = {}) {
  const today = utcTodayString();
  const windowStart = addDaysToDateString(today, -7);
  return {
    schema_version: 1,
    instance_id: '11111111-1111-4111-8111-111111111111',
    window_start: windowStart,
    window_end: today,
    tret_version: '0.1.0',
    deploy: 'docker',
    db: 'postgres-16',
    users_bucket: '2-5',
    workspaces_bucket: '1',
    runs_bucket: '101-1000',
    tokens_in: 1200000,
    tokens_out: 340000,
    providers: { anthropic: 0.6, local: 0.4 },
    model_families: { claude: 0.6, llama: 0.4 },
    task_types: { chat: 0.7, pack: 0.3 },
    run_status: { completed: 0.94, failed: 0.06 },
    energy_wh: 5300.0,
    co2e_g: 2100.0,
    factor_rungs: { provider: 0.7, global_default: 0.3 },
    features: { packs: true, connections: false, delegation: true, local_models: true },
    ...overrides,
  };
}

/** A format-valid (not cryptographically random) UUIDv4 string, one per `n`. */
export function uuidFor(n) {
  const hex = n.toString(16).padStart(8, '0');
  return `${hex}-0000-4000-8000-000000000000`;
}

export async function postReport(self, payload, extraHeaders = {}) {
  return self.fetch('https://telemetry.kithailab.com/v1/report', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...extraHeaders },
    body: JSON.stringify(payload),
  });
}
