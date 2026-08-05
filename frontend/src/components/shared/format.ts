/** Shared number formatting for token counts, dollars, and the estimated
 *  energy/carbon figures. Energy is never metered — anything derived from it is
 *  labelled "(est.)" at every call site. */

/** Token counts always carry thousands separators. */
export function formatTokens(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('en-US')
}

export function formatCost(usd: number | null | undefined): string {
  return `$${(usd ?? 0).toFixed(4)}`
}

/** Small magnitudes need decimals; large ones read better without. */
function magnitude(value: number): string {
  const abs = Math.abs(value)
  if (abs === 0) return '0'
  if (abs < 1) return value.toFixed(2)
  if (abs < 100) return value.toFixed(1)
  return Math.round(value).toLocaleString('en-US')
}

export function formatWh(wh: number | null | undefined): string | null {
  return wh === null || wh === undefined ? null : `${magnitude(wh)} Wh`
}

export function formatCo2e(grams: number | null | undefined): string | null {
  return grams === null || grams === undefined ? null : `${magnitude(grams)} g CO₂e`
}

/** The placeholder for a figure the backend did not send. Never render a missing
 *  estimate as 0 — "no estimate" and "emitted nothing" are different claims. */
export const NO_ESTIMATE = '—'

export const NO_ESTIMATE_HINT =
  'No estimate recorded — not zero. Runs recorded before this accounting existed carry no figure.'

/** A nullable formatted figure, or the em-dash placeholder. */
export function orDash(text: string | null | undefined): string {
  return text ?? NO_ESTIMATE
}

/** Carbon at window scale: grams → kg → tonnes. The sign is always preserved —
 *  a negative figure (heavier than a comparison) is a result, not an error. */
export function formatCo2eScaled(grams: number | null | undefined): string | null {
  if (grams === null || grams === undefined) return null
  const abs = Math.abs(grams)
  if (abs >= 1_000_000) return `${magnitude(grams / 1_000_000)} t CO₂e`
  if (abs >= 1_000) return `${magnitude(grams / 1_000)} kg CO₂e`
  return `${magnitude(grams)} g CO₂e`
}

/** A carbon unit fixed from a reference magnitude. Figures that are meant to be
 *  read against each other (actual vs baseline) print in one unit instead of
 *  each auto-scaling to a different one. */
export interface Co2eScale {
  divisor: number
  unit: string
}

export function co2eScaleFor(reference: number): Co2eScale {
  const abs = Math.abs(reference)
  if (abs >= 1_000_000) return { divisor: 1_000_000, unit: 't CO₂e' }
  if (abs >= 1_000) return { divisor: 1_000, unit: 'kg CO₂e' }
  return { divisor: 1, unit: 'g CO₂e' }
}

export function formatCo2eAt(grams: number | null | undefined, scale: Co2eScale): string | null {
  if (grams === null || grams === undefined) return null
  return `${magnitude(grams / scale.divisor)} ${scale.unit}`
}

/** Energy at window scale: Wh → kWh → MWh. */
export function formatEnergyScaled(wh: number | null | undefined): string | null {
  if (wh === null || wh === undefined) return null
  const abs = Math.abs(wh)
  if (abs >= 1_000_000) return `${magnitude(wh / 1_000_000)} MWh`
  if (abs >= 1_000) return `${magnitude(wh / 1_000)} kWh`
  return `${magnitude(wh)} Wh`
}

/** A backend-supplied percentage. Never computed here from other figures. */
export function formatPct(pct: number | null | undefined): string | null {
  return pct === null || pct === undefined ? null : `${pct.toFixed(1)}%`
}

/** Plain number with thousands separators and up to `places` decimals — for
 *  factors (PUE, grid intensity, Wh/Mtok) that must print exactly as recorded. */
export function formatFactor(value: number | null | undefined, places = 3): string {
  if (value === null || value === undefined) return NO_ESTIMATE
  return Number(value.toFixed(places)).toLocaleString('en-US', { maximumFractionDigits: places })
}

/** "~1.3 Wh · ~0.5 g CO₂e (est.)", or null when the run carries no estimate. */
export function footprintText(
  energyWh: number | null | undefined,
  co2eG: number | null | undefined,
): string | null {
  const parts = [formatWh(energyWh), formatCo2e(co2eG)].filter((p): p is string => p !== null)
  if (parts.length === 0) return null
  return `${parts.map((p) => `~${p}`).join(' · ')} (est.)`
}

/** "$0.0420 · ~1.3 Wh · ~0.5 g CO₂e (est.)" — the cost line with its footprint. */
export function costWithFootprint(
  costUsd: number | null | undefined,
  energyWh: number | null | undefined,
  co2eG: number | null | undefined,
): string {
  const footprint = footprintText(energyWh, co2eG)
  return footprint ? `${formatCost(costUsd)} · ${footprint}` : formatCost(costUsd)
}
