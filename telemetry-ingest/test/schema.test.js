import { describe, it, expect } from 'vitest';
import { validatePayload } from '../src/schema.js';
import { validPayload } from './fixtures.js';
import { utcTodayString, addDaysToDateString } from '../src/dates.js';

describe('validatePayload', () => {
  it('accepts a well-formed payload and returns a normalised clone', () => {
    const payload = validPayload();
    const result = validatePayload(payload);
    expect(result.ok).toBe(true);
    expect(result.value).toEqual(payload);
    // Must be a fresh object, not the same reference as the input -- the
    // validator is documented to never hand back the raw body.
    expect(result.value).not.toBe(payload);
  });

  it('drops unknown top-level fields instead of rejecting them', () => {
    const payload = validPayload({ some_future_field: 'from a newer core' });
    const result = validatePayload(payload);
    expect(result.ok).toBe(true);
    expect(result.value.some_future_field).toBeUndefined();
    expect(Object.keys(result.value)).not.toContain('some_future_field');
  });

  it('rejects an unknown key inside a closed-set share map', () => {
    const payload = validPayload({ providers: { anthropic: 0.6, some_new_provider: 0.4 } });
    const result = validatePayload(payload);
    expect(result.ok).toBe(false);
  });

  it('rejects an unknown key inside factor_rungs', () => {
    const payload = validPayload({ factor_rungs: { provider: 0.5, zone_xx: 0.5 } });
    expect(validatePayload(payload).ok).toBe(false);
  });

  it('rejects a features object with an extra key', () => {
    const payload = validPayload({
      features: { packs: true, connections: false, delegation: true, local_models: true, extra: true },
    });
    expect(validatePayload(payload).ok).toBe(false);
  });

  it('rejects a features object missing a required key', () => {
    const payload = validPayload({ features: { packs: true, connections: false, delegation: true } });
    expect(validatePayload(payload).ok).toBe(false);
  });

  it.each([
    ['tokens_in above 1e12', { tokens_in: 1e12 + 1 }],
    ['tokens_out above 1e12', { tokens_out: 1e12 + 1 }],
    ['tokens_in negative', { tokens_in: -1 }],
    ['energy_wh negative', { energy_wh: -1 }],
    ['energy_wh above 1e9', { energy_wh: 1e9 + 1 }],
    ['co2e_g negative', { co2e_g: -1 }],
    ['co2e_g above 1e9', { co2e_g: 1e9 + 1 }],
    ['a share above 1', { providers: { anthropic: 1.2 } }],
    ['a share map summing above 1.05', { providers: { anthropic: 0.8, local: 0.8 } }],
    ['schema_version not 1', { schema_version: 2 }],
    ['instance_id not a UUIDv4', { instance_id: 'not-a-uuid' }],
    ['deploy outside the closed set', { deploy: 'kubernetes' }],
    ['db malformed', { db: 'mysql-8' }],
    ['users_bucket outside the closed set', { users_bucket: '5' }],
    ['runs_bucket outside the closed set', { runs_bucket: '10' }],
  ])('rejects %s', (_name, overrides) => {
    const result = validatePayload(validPayload(overrides));
    expect(result.ok).toBe(false);
  });

  it('accepts energy_wh and co2e_g as null', () => {
    const result = validatePayload(validPayload({ energy_wh: null, co2e_g: null }));
    expect(result.ok).toBe(true);
    expect(result.value.energy_wh).toBeNull();
    expect(result.value.co2e_g).toBeNull();
  });

  it('accepts empty share maps ({} when no runs in window)', () => {
    const result = validatePayload(
      validPayload({ providers: {}, model_families: {}, task_types: {}, run_status: {}, factor_rungs: {} })
    );
    expect(result.ok).toBe(true);
  });

  const fixedNow = new Date('2026-09-21T12:00:00Z');

  it('rejects a window longer than 36 days', () => {
    const result = validatePayload(
      validPayload({ window_start: '2026-08-01', window_end: '2026-09-10' }),
      { now: fixedNow }
    );
    expect(result.ok).toBe(false);
  });

  it('accepts a window exactly 36 days long', () => {
    const result = validatePayload(
      validPayload({ window_start: '2026-08-01', window_end: '2026-09-06' }),
      { now: fixedNow }
    );
    expect(result.ok).toBe(true);
  });

  it('rejects window_end more than 1 day in the future', () => {
    const result = validatePayload(validPayload({ window_end: '2026-09-23' }), { now: fixedNow });
    expect(result.ok).toBe(false);
  });

  it('rejects window_end before 2026-01-01', () => {
    const result = validatePayload(
      validPayload({ window_start: '2025-12-20', window_end: '2025-12-31' }),
      { now: fixedNow }
    );
    expect(result.ok).toBe(false);
  });

  it('rejects a body that is not a JSON object', () => {
    expect(validatePayload(null).ok).toBe(false);
    expect(validatePayload([1, 2, 3]).ok).toBe(false);
    expect(validatePayload('a string').ok).toBe(false);
  });

  it('never echoes an attacker-supplied unknown share-map key in the error message', () => {
    const suspiciousKey = 'x'.repeat(50);
    const result = validatePayload(validPayload({ providers: { anthropic: 0.6, [suspiciousKey]: 0.4 } }));
    expect(result.ok).toBe(false);
    expect(result.error).not.toContain(suspiciousKey);
  });

  describe('window-scaled per-report caps (contract §7 amendments B2)', () => {
    // A same-day window is treated as 1 day, so the cap is
    // 2e9 tokens / 1e6 Wh-or-g -- far below the old fixed 1e12 / 1e9 caps.
    it('accepts tokens/energy right at the 1-day cap and rejects one unit over it', () => {
      const base = { window_start: utcTodayString(), window_end: utcTodayString() };
      expect(validatePayload(validPayload({ ...base, tokens_in: 2e9 })).ok).toBe(true);
      expect(validatePayload(validPayload({ ...base, tokens_in: 2e9 + 1 })).ok).toBe(false);
      expect(validatePayload(validPayload({ ...base, energy_wh: 1e6 })).ok).toBe(true);
      expect(validatePayload(validPayload({ ...base, energy_wh: 1e6 + 1 })).ok).toBe(false);
    });

    it('rejects a value that is under the old fixed cap but over the window-scaled cap', () => {
      const base = { window_start: utcTodayString(), window_end: utcTodayString() };
      // 2,000,000 Wh is well under the old fixed 1e9 cap, but over the
      // 1-day window-scaled cap of 1e6.
      const result = validatePayload(validPayload({ ...base, energy_wh: 2_000_000 }));
      expect(result.ok).toBe(false);
    });

    it('scales the cap up for a longer window', () => {
      const base = { window_start: addDaysToDateString(utcTodayString(), -10), window_end: utcTodayString() };
      // 10-day window: cap is 2e9 * 10.
      const atCap = validatePayload(validPayload({ ...base, tokens_in: 2e9 * 10 }));
      const overCap = validatePayload(validPayload({ ...base, tokens_in: 2e9 * 10 + 1 }));
      expect(atCap.ok).toBe(true);
      expect(overCap.ok).toBe(false);
    });
  });
});
