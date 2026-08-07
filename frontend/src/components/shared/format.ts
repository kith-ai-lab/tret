/** Shared number formatting for token counts, dollars, and the estimated
 *  energy/carbon figures. Energy is never metered — anything derived from it is
 *  labelled "(est.)" at every call site. */

/** A timestamp as a short local date-time. Lives here rather than in a view
 *  module because six views render one: it used to be exported from views/Runs,
 *  which made Analytics, Approvals, Deliverables, Documents and SettingsView all
 *  import a *view* for a formatter. Falls back to the raw ISO string rather than
 *  throwing on an unparseable value. */
export function formatDateTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

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

// A `formatPct` helper lived here and had no caller: percentages in this app are
// either the money share (`moneyPctPhrase`/`moneyPctCompact` in ./emissions, which
// carry the precision caveat with them) or the coarse carbon comparison
// (`coarseComparison`, deliberately not a decimal). A general one-decimal
// percentage formatter is the wrong default for both, so it is gone rather than
// sitting here waiting to be reached for.

// ── uncertainty bands ────────────────────────────────────────────────────
// A carbon figure is shown as a range wherever the backend recorded one. Both
// ends print in the *same* unit, chosen from the high end, so the two numbers can
// be read against each other. A range with either end missing is not a range: it
// renders as the central figure alone rather than half a band.

/** "0.42 – 2.6 g CO₂e", or null when the run carries no band. */
export function formatCo2eBand(
  low: number | null | undefined,
  high: number | null | undefined,
): string | null {
  if (low === null || low === undefined || high === null || high === undefined) return null
  const scale = co2eScaleFor(high)
  const lowText = formatCo2eAt(low, scale)
  const highText = formatCo2eAt(high, scale)
  if (lowText === null || highText === null) return null
  // Strip the unit off the low end: one unit for the pair reads as one figure.
  return `${lowText.replace(` ${scale.unit}`, '')} – ${highText}`
}

/** "~1.0 g CO₂e (0.42 – 2.6)" — central figure with its band in brackets, or the
 *  central figure alone when no band was recorded. Never invents a band. */
export function formatCo2eWithBand(
  central: number | null | undefined,
  low: number | null | undefined,
  high: number | null | undefined,
): string {
  const centralText = formatCo2eScaled(central)
  if (centralText === null) return NO_ESTIMATE
  if (low === null || low === undefined || high === null || high === undefined) return centralText
  const scale = co2eScaleFor(Math.max(Math.abs(high), Math.abs(central ?? 0)))
  const lowText = formatCo2eAt(low, scale)?.replace(` ${scale.unit}`, '')
  const highText = formatCo2eAt(high, scale)?.replace(` ${scale.unit}`, '')
  if (!lowText || !highText) return centralText
  return `${centralText} (${lowText} – ${highText})`
}

/** Energy shown as a range, same rules as carbon. */
export function formatWhBand(
  low: number | null | undefined,
  high: number | null | undefined,
): string | null {
  if (low === null || low === undefined || high === null || high === undefined) return null
  const lowText = formatEnergyScaled(low)
  const highText = formatEnergyScaled(high)
  if (lowText === null || highText === null) return null
  // Units may differ across a 6.25x span (e.g. 0.9 Wh … 5.6 Wh stays Wh, but
  // 800 Wh … 5 kWh does not), so both ends keep their own unit here.
  return `${lowText} – ${highText}`
}

// ── money ────────────────────────────────────────────────────────────────

/** A signed dollar figure, sign preserved: "-$0.0120" is a surcharge and reads
 *  as one. Money is the one place a precise figure is defensible — per-token
 *  prices are published — so this keeps four decimals where carbon does not. */
export function formatCostSigned(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return NO_ESTIMATE
  const sign = usd < 0 ? '-' : ''
  return `${sign}$${Math.abs(usd).toFixed(4)}`
}

/** Window-scale money: cents matter at run scale, not at window scale, but the
 *  figure is still exact so it is never rounded to an order of magnitude. */
export function formatCostScaled(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return NO_ESTIMATE
  const sign = usd < 0 ? '-' : ''
  const abs = Math.abs(usd)
  if (abs >= 100) return `${sign}$${abs.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
  if (abs >= 1) return `${sign}$${abs.toFixed(2)}`
  return `${sign}$${abs.toFixed(4)}`
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

// `costWithFootprint` (cost and footprint joined into one string) also had no
// caller: every surface that shows both renders them as separate elements so the
// carbon half can carry its own band and tooltip, which a single string cannot.
