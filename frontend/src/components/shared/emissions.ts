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

/** The methodology write-up. Referenced by path, the way the rest of the app
 *  points at docs (there is no route that serves the repo's markdown). */
export const METHODOLOGY_DOC = 'docs/emissions-methodology.md'

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
