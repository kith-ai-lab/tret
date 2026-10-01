/** Shared emissions vocabulary: scope labels, colours, and the wording tret is
 *  allowed to use about the frontier-baseline comparison.
 *
 *  Two rules govern everything in here, and both are product commitments rather
 *  than style preferences:
 *
 *  1. **Nothing is computed here that the backend did not send.** Shares and
 *     sums of figures the API returned are fine (they are checkable identities);
 *     extrapolations, projections and "equivalent to N trees" conversions are
 *     not, and the backend deliberately does not provide them.
 *  2. **The avoided figure is never framed as an offset.** It is a same-token
 *     counterfactual and an efficiency indicator. Not a credit, not a reduction,
 *     not a saving anyone can report. It is signed, and a negative value is
 *     rendered as the surcharge it is.
 */

/** Where the methodology lives on disk. Shown *inside* the dialog as a pointer
 *  to the same file in the repository — it is no longer printed as a bare
 *  filename next to a figure, because a filename is not readable in-product. The
 *  dialog itself fetches the file from `GET /api/docs/emissions-methodology`, so
 *  the prose on screen is the prose in the repo. */
import type {
  EmissionsEmbodiedProfileSummary,
  EmissionsGridTableSummary,
  EmissionsUncertaintyDerivation,
} from '../../api/client'
import type { MethodV3 } from '../../api/client'
import { formatFactor, formatGrams } from './format'

export const METHODOLOGY_DOC = 'docs/emissions-methodology.md'

/** The trigger label used everywhere a carbon figure appears. One wording, so a
 *  reader learns it once. */
export const METHODOLOGY_TRIGGER = 'methodology'

export const METHODOLOGY_TRIGGER_HINT =
  'Read the full emissions methodology — the chain, every constant with its source, the exclusions, and what these numbers may not be used for.'

export const ESTIMATE_NOTE =
  'Estimated from token counts, a heuristic per-model energy class, a heuristic PUE and a grid intensity — never measured.'

/** The long-form framing for the baseline comparison. Rendered wherever an
 *  avoided figure appears at more than ticker size. */
export const COUNTERFACTUAL_NOTE =
  'Same-token counterfactual: the identical token counts re-priced through the baseline model’s energy class. It is an efficiency indicator for choosing between models — not an offset, not a carbon credit, not an emissions reduction, and not a saving that can be reported. A different model would not have produced identical token counts.'

/** The one-line version, for tooltips and tickers. */
export const COUNTERFACTUAL_SHORT =
  'Same-token counterfactual against the baseline model — an efficiency indicator, not an offset, credit, or reduction claim.'

export const MIXED_FACTORS_CAVEAT =
  'Runs in this window were recorded under different emission factors, so no single grid intensity or PUE sits behind these totals. Each run keeps the factors in force when it ran; the combinations present are listed below.'

export type ScopeKey = 'scope1_g' | 'scope2_g' | 'scope3_g'

export interface ScopeMeta {
  key: ScopeKey
  label: string
  color: string
  /** What tret assigns to this scope. The authoritative reasoning is the
   *  backend's own `scopes.basis`, which is always rendered verbatim alongside. */
  what: string
}

/** Ordered so Scope 1 always comes first and is always shown, including at zero:
 *  "we report zero, and here is why" is the credible presentation. */
export const SCOPE_META: ScopeMeta[] = [
  {
    key: 'scope1_g',
    label: 'Scope 1',
    color: 'var(--gray)',
    what: 'Direct emissions from sources the operator owns or controls. Inference burns no fuel on the operator’s own site, so this is always an explicit zero rather than an omitted line.',
  },
  {
    key: 'scope2_g',
    label: 'Scope 2',
    color: 'var(--accent)',
    what: 'Purchased electricity for self-hosted (local) inference, where the operator buys the power. Cloud inference contributes nothing here.',
  },
  {
    key: 'scope3_g',
    label: 'Scope 3',
    color: 'var(--violet)',
    what: 'Cloud inference as a purchased service (Cat. 1) — the provider’s own Scope 1/2 becomes the operator’s Scope 3 — plus amortized embodied hardware for self-hosted inference (Cat. 2).',
  },
]

export type AvoidedTone = 'saving' | 'surcharge' | 'even' | 'unknown'

export interface AvoidedFraming {
  tone: AvoidedTone
  /** Label that names what the figure actually is in this direction. */
  label: string
  color?: string
  note: string
}

/** How to present a signed avoided figure. A negative result is a surcharge and
 *  says so; it is never turned into a positive "saving" by dropping the sign. */
export function avoidedFraming(avoided: number | null | undefined): AvoidedFraming {
  if (avoided === null || avoided === undefined) {
    return {
      tone: 'unknown',
      label: 'Avoided vs baseline (est.)',
      note: 'No baseline comparison was recorded — reported as no figure rather than as zero.',
    }
  }
  if (avoided > 0) {
    return {
      tone: 'saving',
      label: 'Avoided vs baseline (est.)',
      color: 'var(--green)',
      note: 'Lighter than the baseline would have been for the same tokens. An efficiency indicator only — nothing was offset and no emissions were removed.',
    }
  }
  if (avoided < 0) {
    return {
      tone: 'surcharge',
      label: 'Carbon surcharge vs baseline (est.)',
      color: 'var(--red)',
      note: 'Heavier than the baseline would have been for the same tokens. This is a surcharge, not a saving.',
    }
  }
  return {
    tone: 'even',
    label: 'Level with baseline (est.)',
    note: 'The same as the baseline for these tokens — either the baseline model ran, or the comparison came out even.',
  }
}

/** Share of a subtotal, in percent. A checkable identity over figures the
 *  backend sent, not a derived estimate. Zero denominator yields 0. */
export function share(part: number, whole: number): number {
  return whole > 0 ? (100 * part) / whole : 0
}

// ── the band ─────────────────────────────────────────────────────────────
// `uncertainty.is_confidence_interval` is false, and it is false deliberately.
// Nothing in this file — or anywhere downstream of it — may call the band a
// confidence interval, a standard deviation, a margin of error, or a ±. It is a
// multiplicative judgment band, and the words used for it say that.

/** The band's name, in the only wording tret uses for it. */
export const BAND_LABEL = 'judgment band'

/** The one-line disclaimer that travels with any rendered range. The long-form
 *  version is the backend's own `uncertainty.basis`, which is rendered verbatim
 *  wherever there is room for it — never paraphrased. */
export const BAND_SHORT =
  'Range is a multiplicative judgment band, not a confidence interval and not a standard deviation — no credible methodology in this field publishes an interval.'

/** Why a range replaced a single number. Used above a band the first time it
 *  appears on a page. */
export const BAND_WHY =
  'A single figure would imply precision this model does not have. The range either side of the central estimate is what the published validation work supports, and it is a judgment about how wrong this can be — not a statistic.'

/** How to describe a band factor pair, e.g. "central ÷ 2.5 … central × 2.5". */
export function bandFactorText(low: number | null | undefined, high: number | null | undefined): string | null {
  if (low === null || low === undefined || high === null || high === undefined) return null
  return `central ÷ ${low} … central × ${high}`
}

// ── coarse comparison language ───────────────────────────────────────────
// The avoided-emissions percentage used to render to one decimal ("98.3% lighter
// than frontier"). That figure is the ratio of two estimated constants, so a
// decimal place in it is false precision — the underlying estimate is only
// order-of-magnitude reliable, and both sides of the ratio carry the same 2.5x
// band. The comparison is therefore stated coarsely, and stated as coarse.

/** Placeholder when there is no comparison to state. Distinct from a zero. */
export const NO_COMPARISON = '—'

export const COARSE_COMPARISON_NOTE =
  'Deliberately coarse: this comparison is the ratio of two estimated figures, each carrying the same order-of-magnitude judgment band, so only its rough size is meaningful. A decimal place here would be false precision.'

/** Round a multiple to something a reader should trust: nearest 100 above 100,
 *  nearest 10 above 10, otherwise a whole number. Never a decimal. */
function coarseMultiple(ratio: number): number {
  if (ratio >= 100) return Math.round(ratio / 100) * 100
  if (ratio >= 10) return Math.round(ratio / 10) * 10
  return Math.round(ratio)
}

export interface CoarseComparison {
  /** e.g. "~50x lighter", "~30% lighter", "~3x heavier", "level with baseline". */
  text: string
  tone: AvoidedTone
  color?: string
  /** Why it is phrased this way. Always rendered alongside or as a tooltip. */
  note: string
}

/** The baseline comparison in coarse language, from the two figures the backend
 *  sent. Ratio arithmetic over returned values is a checkable identity, which is
 *  what the no-client-side-numbers rule allows; the *wording* is what this
 *  function exists to control.
 *
 *  Sub-2x differences read as a percentage rounded to the nearest 10, because
 *  "~1x lighter" is nonsense; anything at or above 2x reads as a multiple, which
 *  is the honest unit for a figure that moves by factors. */
export function coarseComparison(
  actualCo2eG: number | null | undefined,
  baselineCo2eG: number | null | undefined,
): CoarseComparison {
  if (
    actualCo2eG === null ||
    actualCo2eG === undefined ||
    baselineCo2eG === null ||
    baselineCo2eG === undefined
  ) {
    return {
      text: NO_COMPARISON,
      tone: 'unknown',
      note: 'No baseline comparison was recorded — reported as no figure rather than as zero.',
    }
  }
  if (baselineCo2eG <= 0 || actualCo2eG <= 0) {
    return {
      text: NO_COMPARISON,
      tone: 'unknown',
      note: 'One side of the comparison is zero or missing, so no ratio can be stated.',
    }
  }
  const ratio = baselineCo2eG / actualCo2eG
  if (Math.abs(ratio - 1) < 0.005) {
    return {
      text: 'level with baseline',
      tone: 'even',
      note: `${avoidedFraming(0).note} ${COARSE_COMPARISON_NOTE}`,
    }
  }
  if (ratio > 1) {
    const text =
      ratio >= 2
        ? `~${coarseMultiple(ratio)}x lighter`
        : `~${Math.round((100 * (ratio - 1)) / ratio / 10) * 10}% lighter`
    return {
      text,
      tone: 'saving',
      color: 'var(--green)',
      note: `${avoidedFraming(1).note} ${COARSE_COMPARISON_NOTE}`,
    }
  }
  const inverse = 1 / ratio
  const text =
    inverse >= 2
      ? `~${coarseMultiple(inverse)}x heavier`
      : `~${Math.round((100 * (inverse - 1)) / inverse / 10) * 10}% heavier`
  return {
    text,
    tone: 'surcharge',
    color: 'var(--red)',
    note: `${avoidedFraming(-1).note} ${COARSE_COMPARISON_NOTE}`,
  }
}

// ── money ────────────────────────────────────────────────────────────────
// Money is the exception to everything above. Per-token prices are published, so
// `cost.usd`, `cost.baseline_usd` and `avoided_usd` are arithmetic and a precise
// dollar figure IS defensible. What is still an assumption is the counterfactual,
// which is the same assumption the carbon comparison makes.

export const MONEY_EXACT_NOTE =
  'Dollar figures are exact arithmetic, not estimates: per-token list prices are published, so this is the one figure here carried to the cent. The counterfactual behind the comparison is still an assumption — the same one the carbon comparison makes.'

export const MONEY_SHORT = 'Exact prices, assumed counterfactual — not booked savings.'

/** How to present a signed `avoided_usd`. Mirrors `avoidedFraming` deliberately:
 *  money and carbon travel together and are signed the same way, so a negative
 *  figure is a surcharge in both and is never shown as a positive saving. */
export function avoidedMoneyFraming(avoided: number | null | undefined): AvoidedFraming {
  if (avoided === null || avoided === undefined) {
    return {
      tone: 'unknown',
      label: 'Money vs baseline',
      note: 'No money comparison was recorded — reported as no figure rather than as zero. Runs recorded before the money comparison existed carry none.',
    }
  }
  if (avoided > 0) {
    return {
      tone: 'saving',
      label: 'Money not spent vs baseline',
      color: 'var(--green)',
      note: `Cheaper than the baseline model would have been for the same tokens. ${MONEY_EXACT_NOTE} It is a model-selection indicator, not money booked anywhere.`,
    }
  }
  if (avoided < 0) {
    return {
      tone: 'surcharge',
      label: 'Cost surcharge vs baseline',
      color: 'var(--red)',
      note: `Dearer than the baseline model would have been for the same tokens — a surcharge, not a saving. ${MONEY_EXACT_NOTE}`,
    }
  }
  return {
    tone: 'even',
    label: 'Level with baseline (cost)',
    note: `The same cost as the baseline for these tokens — either the baseline model ran, or the comparison came out even. ${MONEY_EXACT_NOTE}`,
  }
}

// ── the money percentage ─────────────────────────────────────────────────
// "N.N% cheaper than frontier" — the one place on this page a decimal place is
// honest rather than false precision. It divides two arithmetic figures (list
// price x exact token count), not two estimates, so it earns the precision the
// carbon comparison above deliberately does not get.

export const MONEY_PCT_PRECISION_NOTE =
  'Precise on purpose: this percentage is arithmetic on published list prices, not an estimate — unlike the carbon comparison, which is stated as a coarse multiple because it rests on estimated energy constants carrying the same judgment band on both sides of the ratio.'

/** "N.N% cheaper than frontier" / "N.N% more expensive than frontier" / "even
 *  with frontier" / em dash when there is no baseline spend to compare
 *  against. Never renders a null percentage as 0%. */
export function moneyPctPhrase(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return NO_COMPARISON
  if (pct > 0) return `${pct.toFixed(1)}% cheaper than frontier`
  if (pct < 0) return `${Math.abs(pct).toFixed(1)}% more expensive than frontier`
  return 'even with frontier'
}

/** Compact signed form for table cells and tickers: "+70.0%" / "-200.0%" /
 *  "0.0%" / em dash. Pair with `moneyPctPhrase` in a tooltip for the full
 *  wording. */
export function moneyPctCompact(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return NO_COMPARISON
  const sign = pct > 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
}

// ── factor provenance ────────────────────────────────────────────────────

export interface ConfidenceMeta {
  label: string
  /** Badge class. A placeholder or excluded factor must not look as solid as an
   *  exact one, so the palette is deliberately not uniform. */
  badge: string
  /** What the marker means, in the backend's own terms. */
  what: string
  /** True where the factor should be visually de-emphasised in a table. */
  weak: boolean
}

/** The backend's confidence vocabulary, rendered. Keys match `emissions.py`. */
export const CONFIDENCE_META: Record<string, ConfidenceMeta> = {
  exact: {
    label: 'exact',
    badge: 'badge-green',
    what: 'Arithmetic on a published price. No estimation involved — the only figures here that are not estimates.',
    weak: false,
  },
  structural: {
    label: 'structural',
    badge: 'badge-blue',
    what: 'Follows from how inference works rather than from a measurement — e.g. an output token being the unit by definition.',
    weak: false,
  },
  calibrated: {
    label: 'calibrated',
    badge: 'badge-blue',
    what: 'Fitted to measured data, then generalised beyond it. The strongest kind of estimate here, and still an estimate.',
    weak: false,
  },
  low: {
    label: 'low',
    badge: 'badge-amber',
    what: 'Judgement anchored on something published, but not fitted to it.',
    weak: false,
  },
  placeholder: {
    label: 'placeholder',
    badge: 'badge-red',
    what: 'A stand-in the source itself describes as unsupported. Offered because zero is worse, not because it is good — read the note before using it.',
    weak: true,
  },
  excluded: {
    label: 'excluded',
    badge: 'badge-gray',
    what: 'Deliberately not counted, with the reason stated. Its value is 0 because it is out of scope, not because it is negligible.',
    weak: true,
  },
}

export function confidenceMeta(confidence: string | null | undefined): ConfidenceMeta {
  return (
    CONFIDENCE_META[confidence ?? ''] ?? {
      label: confidence || 'unrecorded',
      badge: 'badge-gray',
      what: 'No confidence marker was recorded for this factor.',
      weak: true,
    }
  )
}

/** Which way a named bias pushes the figure. Colour follows the direction of the
 *  error, not its severity: understating carbon is the flattering direction, so
 *  it is the one drawn in a warning colour. */
export const CAVEAT_DIRECTION_META: Record<string, { label: string; color?: string; what: string }> = {
  understates: {
    label: 'understates',
    color: 'var(--amber)',
    what: 'The real figure is higher than reported. This is the flattering direction, so it is called out rather than absorbed.',
  },
  overstates: {
    label: 'overstates',
    color: 'var(--blue)',
    what: 'The real figure is lower than reported.',
  },
  either: {
    label: 'either direction',
    color: 'var(--text-muted)',
    what: 'The error could go either way; its size is not bounded by this caveat.',
  },
}

export function caveatDirection(direction: string | null | undefined) {
  return (
    CAVEAT_DIRECTION_META[direction ?? ''] ?? {
      label: direction || 'unrecorded',
      color: 'var(--text-muted)',
      what: 'No direction was recorded for this bias.',
    }
  )
}

/** Readable names for the GHG Protocol grid basis labels. Location- and
 *  market-based factors answer different questions and may never be summed, so
 *  the label always travels with the value. */
export const GRID_BASIS_META: Record<string, { label: string; what: string }> = {
  location_based: {
    label: 'location-based',
    what: 'The physical grid that served the load. What most disclosure frameworks expect for a Scope 2 location-based figure.',
  },
  market_based: {
    label: 'market-based',
    what: "Contractual renewable claims (PPAs, RECs, GOs). Can be several times below the location-based figure for the same electricity — a different accounting question, never summable with one.",
  },
  unspecified: {
    label: 'unspecified',
    what: 'tret was handed a factor without a provenance, so it will not guess a basis on the operator’s behalf.',
  },
}

export function gridBasisLabel(basis: string | null | undefined): string {
  return GRID_BASIS_META[basis ?? '']?.label ?? (basis || 'not recorded')
}

/** What a basis means, for a tooltip. Handles the unrecorded case, which is not a
 *  basis but is its own group and has to be explained rather than hidden. */
export function gridBasisWhat(basis: string | null | undefined): string {
  return (
    GRID_BASIS_META[basis ?? '']?.what ??
    'No basis was recorded on these runs — they predate the label. They are kept as their own group rather than folded in with a location-based figure they cannot be shown to share.'
  )
}

// ── which rule chose the factor ──────────────────────────────────────────
// A grid factor can now come from four places, and "why this number" is the more
// interesting half once an operator has configured several. The backend records a
// stable source key per run; this is the only place it is put into words.

export const GRID_SOURCE_META: Record<string, { label: string; what: string }> = {
  provider: {
    label: 'per-provider factor',
    what: 'Configured by the operator for this run’s provider (TRET_GRID_FACTORS). The most specific rule, so it outranks both the self-hosted setting and the global default.',
  },
  local_setting: {
    label: 'self-hosted factor',
    what: 'The operator’s factor for self-hosted inference (TRET_LOCAL_GRID_CO2E_G_PER_KWH — the legacy setting, still honoured), applied because this run ran locally and its provider has no per-provider entry.',
  },
  global_default: {
    label: 'global default',
    what: 'TRET_GRID_CO2E_G_PER_KWH — the single factor applied wherever nothing more specific is configured. Ships as Ember’s World 2025 lifecycle CO2e intensity (458.49 gCO2e/kWh, CC BY 4.0).',
  },
  run_override: {
    label: 'supplied for this run',
    what: 'A factor handed straight to the accounting call. tret cannot state its provenance and will not claim a GHG Protocol basis for it.',
  },
  dataset: {
    label: 'published zone average',
    what: 'The bundled Electricity Maps yearly average for the grid zone the workspace pinned this provider to. Reached only because an operator pinned the region — tret never infers one.',
  },
}

/** "per-provider factor (anthropic)" / "global default" / em dash on a run
 *  recorded before the source key existed — never a guessed "global default". */
export function gridSourceLabel(source: string | null | undefined): string {
  if (!source) return NO_COMPARISON
  const parts = source.split(':')
  const rule = parts[0]
  const name = parts.length > 1 ? parts[parts.length - 1] : undefined
  const meta = GRID_SOURCE_META[rule]
  if (!meta) return source
  return name ? `${meta.label} (${name})` : meta.label
}

export function gridSourceWhat(source: string | null | undefined): string {
  if (!source)
    return 'No factor source was recorded on this run — it predates per-provider grid factors. Which rule applied is unknown, which is not the same as the global default.'
  return GRID_SOURCE_META[source.split(':')[0]]?.what ?? `Recorded source key: ${source}.`
}

/** Why tret asks the operator instead of looking the region up. The question a
 *  reviewer always asks, answered where the numbers are. */
export const GRID_NO_INFERENCE_NOTE =
  'Grid regions come from explicit configuration. Caller location and provider brand do not establish where inference ran. Response geography is retained as evidence, but broad labels such as “us” or “global” do not select a regional grid factor.'

// ── basis separation ─────────────────────────────────────────────────────
// The GHG Protocol rule with teeth: energy and dollars sum across bases, carbon
// does not. Per-provider factors make a mixed window ordinary, so this is the
// normal presentation rather than an edge case.

export const NOT_SUMMABLE_TITLE = 'These runs have no single carbon total'

export const NOT_SUMMABLE_WHY =
  'These records have incompatible grid accounting bases or factor methods, or lack a combined carbon figure. Location-based and market-based results answer different questions. Factors also need compatible boundaries and gas coverage. Carbon totals are withheld where compatibility is not established.'

export const SUMMABLE_ACROSS_BASES_NOTE =
  'Energy in Wh and dollars are still totalled across the whole window: a kWh is a kWh however its carbon is accounted, and a price has no Scope 2 accounting method. Only carbon — including the scope split, the baseline comparison and the judgment band — is held back.'

export const BASIS_SUBTOTAL_HINT =
  'Subtotals retain their recorded accounting basis. A row with incompatible factor methods has no combined carbon figure. Rows with different bases or methods must not be added.'

/** The em-dash placeholder wording for a carbon figure withheld because it would
 *  cross a basis boundary. Distinct from "no estimate recorded". */
export const NOT_SUMMABLE_CELL_HINT =
  'No single figure: carbon coverage or compatibility is incomplete. Inspect the recorded subtotals and factor methods.'

// ── which precedence layer set a factor ──────────────────────────────────
// Per-workspace emissions overrides (Settings → Emissions factors) added a
// fourth-and-fifth precedence layer on top of the existing env/global-default
// pair: a run override still wins over everything, a harness-level setting
// outranks the workspace, and a workspace override outranks both an operator's
// managed default (a hosting extension) and the plain env/global-default pair. This is
// the vocabulary for rendering that layer wherever a factor is shown — the
// provenance table (`FactorProvenance.tsx`) and the effective-factors table in
// Settings. Absent on any factor recorded before layered overrides existed, in
// which case the chip is omitted rather than guessed.

export const FACTOR_LAYERS = [
  'run_override',
  'harness',
  'workspace',
  'managed',
  'env',
  'dataset',
  'global_default',
] as const

export type FactorLayer = (typeof FACTOR_LAYERS)[number]

export interface LayerMeta {
  label: string
  /** Badge class — the same palette used for confidence, kept distinct in tone
   *  (violet/blue for something an operator or workspace chose, gray for a
   *  plain default) so the two chips read as different questions. */
  badge: string
  what: string
}

export const LAYER_META: Record<string, LayerMeta> = {
  run_override: {
    label: 'run override',
    badge: 'badge-violet',
    what: 'Supplied directly for this run. The most specific layer there is — it outranks every configured setting.',
  },
  harness: {
    label: 'harness',
    badge: 'badge-blue',
    what: "Set on the harness that ran this. Outranks the workspace's own setting, a managed default, and the plain env/global-default pair.",
  },
  workspace: {
    label: 'workspace',
    badge: 'badge-blue',
    what: 'Configured in this workspace’s emissions settings (Settings → Emissions factors) — an override the workspace itself chose.',
  },
  managed: {
    label: 'managed',
    badge: 'badge-violet',
    what: "Set by the hosting operator for every workspace on this deployment, ahead of the plain env/global-default pair but behind the workspace's own choice.",
  },
  env: {
    label: 'env',
    badge: 'badge-gray',
    what: 'Set by an environment variable on this deployment.',
  },
  dataset: {
    label: 'zone dataset',
    badge: 'badge-gray',
    what: 'A published yearly grid average for the zone this workspace pinned the provider to (bundled Electricity Maps data, ODbL). Ahead of only tret’s shipped global default — anything an operator actually set, in a document or an environment variable, still wins.',
  },
  global_default: {
    label: 'global default',
    badge: 'badge-gray',
    what: 'tret’s own shipped default — nothing more specific is configured anywhere.',
  },
}

/** Meta for a recorded layer, or `null` when there is none to show — the caller
 *  omits the chip entirely rather than rendering an "unrecorded" placeholder,
 *  so old runs (recorded before layered overrides existed) render exactly as
 *  they did before this concept existed. */
export function layerMeta(layer: string | null | undefined): LayerMeta | null {
  if (!layer) return null
  return LAYER_META[layer] ?? { label: layer, badge: 'badge-gray', what: 'Recorded precedence layer.' }
}

/** Readable names for the PUE deployment profiles. */
export const PUE_PROFILE_LABELS: Record<string, string> = {
  hyperscaler_cloud: 'hyperscaler cloud',
  workstation: 'workstation',
  onprem_datacenter: 'on-prem facility',
}

/** Human labels for the token buckets, which no longer weigh the same. */
export const TOKEN_BUCKET_LABELS: Record<string, string> = {
  input: 'input',
  output: 'output',
  cache_read: 'cache read',
  cache_write: 'cache write',
}

// ── regions, hourly grid tables, hardware profiles, evidence-derived band ──
// Four independent, opt-in additions to the factor layers above. Each one is
// an operator statement, never an inference — see `GRID_NO_INFERENCE_NOTE`,
// which already says this for the grid factor itself and applies just as
// much to a region pin.

/** Explains, in one line, what pinning a provider to a region actually does
 *  — shown once above the Regions sub-block in the settings form. */
export const REGION_PIN_NOTE =
  'Pinning a provider to a region makes that provider’s provider@region entry apply ahead of its bare entry, wherever one is configured. The region is declared here by the operator — tret never infers it from where a request originated.'

/** The two CSV header shapes `grid.tables` accepts, shown as placeholder
 *  text on the table's CSV textarea. */
export const GRID_TABLE_HEADER_HINT =
  'hour_utc,g_per_kwh (exactly 24 rows, one per hour 0–23) — or — timestamp_utc,g_per_kwh (ascending ISO-8601 hourly series)'

export const GRID_TABLE_KIND_LABELS: Record<string, string> = {
  diurnal: 'diurnal (24-hour profile)',
  series: 'hourly series',
}

export function gridTableKindLabel(kind: string | null | undefined): string {
  return GRID_TABLE_KIND_LABELS[kind ?? ''] ?? (kind || 'unrecorded')
}

/** A one-line summary of an hourly grid table from `GridTable.summary()` —
 *  what the effective-factors detail row and the provenance table show
 *  instead of the raw pasted CSV. */
export function gridTableSummaryText(summary: EmissionsGridTableSummary): string {
  const range =
    summary.min_g_per_kwh !== null && summary.max_g_per_kwh !== null
      ? `${formatFactor(summary.min_g_per_kwh, 1)}–${formatFactor(summary.max_g_per_kwh, 1)} gCO₂e/kWh (mean ${formatFactor(
          summary.mean_g_per_kwh,
          1,
        )})`
      : null
  const span =
    summary.kind === 'series' && summary.first_timestamp && summary.last_timestamp
      ? `${summary.first_timestamp} … ${summary.last_timestamp}`
      : null
  return [gridTableKindLabel(summary.kind), `${summary.row_count} row${summary.row_count === 1 ? '' : 's'}`, range, span]
    .filter((p): p is string => Boolean(p))
    .join(' · ')
}

/** Shown on a run whose grid win named an hourly table but the lookup
 *  missed — a series table's gap past its lookup window — so the entry's
 *  own annual figure applied instead. */
export const TABLE_MISS_NOTE =
  'This run’s hourly table had no value within its lookup window at the run’s actual start time — the entry’s own annual figure applied instead.'

/** A named hardware profile's inputs, as one line — "4x h100 · 100,000 runs
 *  lifetime · batch 64 · server included". */
export function embodiedProfileText(profile: EmissionsEmbodiedProfileSummary): string {
  return [
    `${profile.gpus}x ${profile.gpu_model}`,
    `${formatFactor(profile.runs_over_lifetime, 0)} runs lifetime`,
    `batch ${profile.batch_size}`,
    profile.include_server ? 'server included' : 'GPUs only',
  ].join(' · ')
}

/** The one sentence explaining what "Derive from evidence" does to the
 *  judgment band — shown once above the checkbox in both the settings form
 *  and the what-if scenario drawer. */
export const BAND_DERIVE_NOTE =
  'The band narrows when this run has measured energy, a labeled PUE from an operator layer, a sourced and dated grid factor, or a hardware profile. It never widens beyond the configured band and stays a judgment band, not a confidence interval.'

const BAND_DERIVATION_RULE_LABELS: Record<string, string> = {
  configured: 'configured',
  dominant_contribution: 'dominant contribution',
}

/** "Band derived from evidence: dominant contribution, binding factor
 *  grid_intensity; configured 2.5×/2.5×, derived 1.8×/2.5×" — the line the
 *  provenance table shows beneath a run whose band was evidence-derived. */
export function bandDerivationText(derivation: EmissionsUncertaintyDerivation): string {
  const rule = BAND_DERIVATION_RULE_LABELS[derivation.rule] ?? derivation.rule
  const binding = derivation.dominant_key ? `, binding factor ${derivation.dominant_key}` : ''
  return (
    `Band derived from evidence: ${rule}${binding}; ` +
    `configured ${formatFactor(derivation.configured_low, 2)}×/${formatFactor(derivation.configured_high, 2)}×, ` +
    `derived ${formatFactor(derivation.low, 2)}×/${formatFactor(derivation.high, 2)}×`
  )
}

/** Which evidence flag narrowed one uncertainty contribution row, in words —
 *  the small tag a contribution row with `evidence` set gets in
 *  `FactorProvenance.tsx::SensitivityTable`. */
export const CONTRIBUTION_EVIDENCE_LABELS: Record<string, string> = {
  energy_measured: 'measured energy',
  pue_metered: 'metered PUE',
  grid_sourced_dated: 'sourced & dated grid factor',
  embodied_profiled: 'hardware profile',
}

export function contributionEvidenceLabel(evidence: string | null | undefined): string | null {
  if (!evidence) return null
  return CONTRIBUTION_EVIDENCE_LABELS[evidence] ?? evidence
}

// ── method v3 (preview) ──────────────────────────────────────────────────────
/** The provenance line on the parallel v3 estimate — never the reported figure. */
export const METHOD_V3_NOTE =
  'Parallel estimate under the revised method (Kith method lab, 2026-10-01). Not the reported figure.'

/** "Method v3 (preview): 3.5 g CO2e · band 0.81–17.6 g". */
export function methodV3Summary(v3: MethodV3): string {
  return `Method v3 (preview): ${formatGrams(v3.total_g)} g CO2e · band ${formatGrams(v3.band.low_g)}–${formatGrams(v3.band.high_g)} g`
}

/** One line per component, for the tooltip/expander. */
export function methodV3Parts(v3: MethodV3): { label: string; value: string }[] {
  return [
    { label: 'operational', value: `${formatGrams(v3.parts.operational_g)} g` },
    { label: 'embodied', value: `${formatGrams(v3.parts.embodied_g)} g` },
    { label: 'router', value: `${formatGrams(v3.parts.router_g)} g` },
  ]
}
