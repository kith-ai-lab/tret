/** Shared emissions vocabulary: scope labels, colours, and the wording bench is
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
  /** What bench assigns to this scope. The authoritative reasoning is the
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

/** The band's name, in the only wording bench uses for it. */
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
    what: 'bench was handed a factor without a provenance, so it will not guess a basis on the operator’s behalf.',
  },
}

export function gridBasisLabel(basis: string | null | undefined): string {
  return GRID_BASIS_META[basis ?? '']?.label ?? (basis || 'not recorded')
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
