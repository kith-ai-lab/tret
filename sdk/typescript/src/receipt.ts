/**
 * The Receipt: what a run cost, in dollars and estimated carbon, and how that
 * compares to the frontier-model counterfactual. Mirrors the Python SDK's
 * `tret.Receipt` (docs/embedding.md, "The Receipt"), camelCased, built from a
 * finished run's `routing` / `usage` events and its final run record.
 *
 * **`null` always means "estimate unavailable" — never zero.** A run with no
 * reported token usage gets `usd: null`, not `0`; a run recorded before an
 * accounting field existed reports that field as `null`.
 */
import type { RoutingDecision, UsageEventData } from './events.js'
import type { RunDetail } from './types.js'

export interface ReceiptUsage {
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheWriteTokens: number | null
}

export interface ReceiptRouting {
  chosenModel: string
  reasoning: string
  candidates: string[]
  fallbackUsed: boolean
  objective: string
}

export interface Receipt {
  /** The tret model id that ran last (after any mid-run switch). */
  model: string | null
  /** Catalog-priced cost of the run's own tokens. */
  usd: number | null
  /** Provider-reported cost where one exists (OpenRouter), else the catalog
   *  price; `null` only when nothing was spent. */
  reportedUsd: number | null
  co2eG: number | null
  /** Judgment band around `co2eG`. */
  co2eGLow: number | null
  co2eGHigh: number | null
  /** Compute energy, watt-hours (no PUE). */
  energyWh: number | null
  waterMl: number | null
  /** The frontier model the run is compared against. */
  baselineModel: string | null
  /** Signed: negative when the run was dearer/heavier than the baseline. A
   *  model-selection signal, never a booked saving. */
  avoidedUsd: number | null
  avoidedUsdPct: number | null
  avoidedCo2eG: number | null
  avoidedCo2ePct: number | null
  usage: ReceiptUsage
  routing: ReceiptRouting | null
  /** The router call's own spend (`RoutingDecision.spend`), kept out of
   *  `usd` on purpose. `null` when no router model was contacted. */
  overhead: Record<string, unknown> | null
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

export interface ReceiptSources {
  run: RunDetail
  routing?: RoutingDecision | null
  lastUsage?: UsageEventData | null
}

/** Build a Receipt from the final run record and the run's stream events. */
export function buildReceipt({ run, routing = null, lastUsage = null }: ReceiptSources): Receipt {
  const usage: ReceiptUsage = {
    inputTokens: num(run.input_tokens) ?? num(lastUsage?.input_tokens),
    outputTokens: num(run.output_tokens) ?? num(lastUsage?.output_tokens),
    cacheReadTokens: num(run.cache_read_tokens) ?? num(lastUsage?.cache_read_tokens),
    cacheWriteTokens: num(run.cache_write_tokens) ?? num(lastUsage?.cache_write_tokens),
  }
  // The run row stores `cost_usd` as 0 for "nothing recorded"; the receipt
  // must not turn that into a confident $0.0000. Priced only when some usage
  // was actually reported.
  const anyUsage =
    lastUsage !== null ||
    [usage.inputTokens, usage.outputTokens, usage.cacheReadTokens, usage.cacheWriteTokens].some(
      (n) => n !== null && n > 0,
    )
  const energy = record(run.energy)
  const baseline = record(energy?.baseline)
  const decision = routing ?? (record(run.routing) as RoutingDecision | null)

  return {
    model: str(run.model_used) ?? str(decision?.chosen_model),
    usd: anyUsage ? num(run.cost_usd) ?? num(lastUsage?.cost_usd) : null,
    reportedUsd: num(run.reported_cost_usd),
    co2eG: num(run.co2e_g),
    co2eGLow: num(run.co2e_g_low),
    co2eGHigh: num(run.co2e_g_high),
    energyWh: num(run.energy_wh),
    waterMl: num(run.water_ml),
    baselineModel: str(baseline?.model),
    avoidedUsd: num(run.avoided_usd),
    avoidedUsdPct: num(run.avoided_usd_pct),
    avoidedCo2eG: num(run.avoided_co2e_g),
    avoidedCo2ePct: num(baseline?.avoided_pct),
    usage,
    routing:
      decision && typeof decision.chosen_model === 'string'
        ? {
            chosenModel: decision.chosen_model,
            reasoning: typeof decision.reasoning === 'string' ? decision.reasoning : '',
            candidates: Array.isArray(decision.candidates) ? decision.candidates.map(String) : [],
            fallbackUsed: Boolean(decision.fallback_used),
            objective: typeof decision.objective === 'string' ? decision.objective : '',
          }
        : null,
    overhead: record(decision?.spend),
  }
}

/** One line, the same shape as the Python `str(receipt)`:
 *  `receipt · $0.0006 (+$0.0026 routing) · 0.02 gCO₂e · model`. */
export function formatReceipt(receipt: Receipt): string {
  const shortModel = (receipt.model ?? 'unknown model').split('/').pop() ?? 'unknown model'
  if (receipt.usd === null) return `receipt · estimate unavailable · ${shortModel}`
  let usd = `$${receipt.usd.toFixed(4)}`
  const overheadUsd = num(receipt.overhead?.cost_usd)
  if (overheadUsd !== null) usd += ` (+$${overheadUsd.toFixed(4)} routing)`
  const segments = ['receipt', usd]
  if (receipt.co2eG !== null) segments.push(`${receipt.co2eG.toFixed(2)} gCO₂e`)
  segments.push(shortModel)
  return segments.join(' · ')
}
