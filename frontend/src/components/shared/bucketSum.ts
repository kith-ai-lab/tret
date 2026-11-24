// The token-bucket consistency check behind the calculator's "treat the split as
// unreliable" warning. No imports on purpose (tests/bucketSum.test.ts loads this
// file directly under node).
//
// The backend persists every figure rounded to 6 decimal places
// (tret/services/emissions.py `_PLACES`/`_f`), the four buckets and `energy_wh`
// each rounded independently. The rounded buckets therefore do not sum exactly to
// the rounded total: each of the five numbers is off by up to half a unit in the
// last place, so the gap can legitimately reach 2.5e-6 Wh. A fixed 1e-6 tolerance
// flagged roughly a quarter of ordinary runs.

export const ENERGY_DECIMAL_PLACES = 6

/** True when the rounded buckets add up to the rounded total, to within the
 *  rounding the backend actually applied (half a unit per number, plus a hair
 *  for float arithmetic). */
export function bucketsMatchTotal(
  buckets: ReadonlyArray<number>,
  energyWh: number,
  places: number = ENERGY_DECIMAL_PLACES,
): boolean {
  const sum = buckets.reduce((total, b) => total + b, 0)
  const halfUnit = 0.5 * 10 ** -places
  const tolerance = (buckets.length + 1) * halfUnit + 1e-9
  return Math.abs(sum - energyWh) <= tolerance
}
