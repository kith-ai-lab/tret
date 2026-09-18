/** Where every constant behind a carbon figure came from.
 *
 *  This is the answer to "where do your numbers come from" rendered as a table
 *  rather than as prose: per factor, the value tret applied, its unit, the source
 *  it was taken from with a real link, the date of that source, a confidence
 *  marker, and the setting that changes it. Nothing here is hardcoded in the
 *  frontend — every row, including its source string and its URL, is read from the
 *  run's own `energy_accounting.factors`, so a constant changing in Python changes
 *  this table without anyone editing it.
 *
 *  Three presentation rules carry weight:
 *
 *  1. **Confidence is visible, not buried.** A `placeholder` (the embodied-carbon
 *     figure, which its own source calls unsupported) and an `excluded` factor
 *     (training amortization) are drawn de-emphasised, so they cannot be mistaken
 *     for the `exact` per-token prices sitting three rows below them.
 *  2. **Caveats keep their direction.** "Understates" is the flattering direction
 *     for a carbon number, so it is the one drawn in a warning colour.
 *  3. **The sensitivity rows are not the band.** Their product is deliberately
 *     wider than the headline band; the backend says so in `uncertainty.basis`,
 *     which is rendered verbatim beneath them.
 */
import type {
  EmissionsCaveat,
  EmissionsFactor,
  EmissionsUncertainty,
  EmissionsUncertaintyContribution,
} from '../../api/client'
import {
  bandDerivationText,
  BAND_DERIVE_NOTE,
  caveatDirection,
  confidenceMeta,
  contributionEvidenceLabel,
  embodiedProfileText,
  gridBasisLabel,
  gridSourceLabel,
  gridSourceWhat,
  layerMeta,
  PUE_PROFILE_LABELS,
  TABLE_MISS_NOTE,
} from './emissions'
import { NO_ESTIMATE, formatFactor } from './format'

/** A factor's value, whatever shape it arrived in. The band factor is a
 *  `[low, high]` pair; the per-token price record has no single value because the
 *  prices are per model, and says so rather than printing 0. */
function factorValue(factor: EmissionsFactor): string {
  const { value } = factor
  if (value === null || value === undefined) return NO_ESTIMATE
  if (Array.isArray(value)) {
    return value.map((v) => formatFactor(v, 3)).join(' / ')
  }
  return formatFactor(value, 4)
}

/** The extras a particular factor carries. Rendered as a short suffix so the
 *  interesting ones (which model a class was anchored on, whether a grid factor
 *  was overridden, which facility a PUE describes) are visible without expanding
 *  the note. */
function factorDetail(factor: EmissionsFactor): string | null {
  const parts: string[] = []
  if (factor.key === 'energy_class') {
    parts.push(
      factor.anchor_model
        ? `anchored on ${factor.anchor_model}`
        : 'no measured anchor — interpolated',
    )
    if (factor.reasoning_tier) parts.push('reasoning tier')
  }
  if (factor.key === 'pue' && typeof factor.profile === 'string') {
    parts.push(`profile: ${PUE_PROFILE_LABELS[factor.profile] ?? factor.profile}`)
  }
  if (factor.key === 'grid_intensity') {
    parts.push(`GHG Protocol basis: ${gridBasisLabel(factor.basis)}`)
    // Which precedence rule chose this factor — the half of the provenance that
    // answers "why this number", not just "what number". Runs recorded before the
    // source key existed carry none, and say nothing rather than guessing.
    if (factor.source_key) parts.push(gridSourceLabel(factor.source_key))
    if (factor.source_label) parts.push(`“${factor.source_label}”`)
    if (factor.overridden) parts.push('supplied for this run')
    // Phase 3, additive: an operator-pinned region, and whether this run's
    // value came from an hourly table or the plain annual figure.
    if (factor.grid_region) parts.push(`region: ${factor.grid_region}`)
    if (factor.region_resolution_status === 'fallback_unknown_region') {
      parts.push(`unsupported pin: ${factor.requested_region}; fallback applied`)
    }
    if (factor.temporal === 'interval_weighted' && factor.table) {
      parts.push(`time-weighted: ${factor.table} (constant power within each call)`)
    } else if (factor.temporal === 'hourly' && factor.table) {
      parts.push(`hourly: ${factor.table}`)
    } else if (factor.table) {
      parts.push(`annual (hourly table ${factor.table} configured)`)
    }
    if (factor.table_miss) parts.push('uncovered table periods use the annual fallback')
  }
  if (factor.key === 'embodied_hardware' && factor.profile && typeof factor.profile === 'object') {
    parts.push(embodiedProfileText(factor.profile))
  }
  if (factor.key === 'uncertainty_band' && factor.derived) {
    parts.push('derived from evidence')
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

/** The tooltip for that suffix. The grid factor and the band both have one
 *  worth writing: which configuration rule applied (and whether a table
 *  lookup missed) is not self-explanatory from its name, and neither is what
 *  "derived" means for the band. */
function factorDetailHint(factor: EmissionsFactor): string | undefined {
  if (factor.key === 'grid_intensity') {
    const source = gridSourceWhat(factor.source_key)
    const withLabel = factor.source_label
      ? `${source} The operator's label for it: “${factor.source_label}”.`
      : source
    return factor.table_miss ? `${withLabel} ${TABLE_MISS_NOTE}` : withLabel
  }
  if (factor.key === 'uncertainty_band' && factor.derived) return BAND_DERIVE_NOTE
  return undefined
}

/** Per-factor provenance. The heart of deliverable "where did this come from". */
export function FactorTable({ factors }: { factors: EmissionsFactor[] }) {
  if (factors.length === 0) {
    return (
      <div className="empty" style={{ padding: '4px 0' }}>
        This run carries no factor provenance — it was recorded before tret stored it.
      </div>
    )
  }
  return (
    <div className="md-table-wrap">
      <table className="mono-table factor-table">
        <thead>
          <tr>
            <th>Factor</th>
            <th className="num">Value</th>
            <th>Unit</th>
            <th>Source</th>
            <th>Date</th>
            <th>Confidence</th>
          </tr>
        </thead>
        <tbody>
          {factors.map((factor) => {
            const meta = confidenceMeta(factor.confidence)
            const detail = factorDetail(factor)
            return (
              <tr key={factor.key} className={meta.weak ? 'factor-weak' : undefined}>
                <td>
                  <div>{factor.label}</div>
                  {detail && (
                    <div
                      className="fine-print"
                      style={{ marginTop: 2 }}
                      title={factorDetailHint(factor)}
                    >
                      {detail}
                    </div>
                  )}
                </td>
                <td className="num" style={{ whiteSpace: 'nowrap' }}>
                  {factorValue(factor)}
                </td>
                <td style={{ color: 'var(--text-muted)' }}>{factor.unit ?? NO_ESTIMATE}</td>
                <td>
                  {factor.url ? (
                    <a href={factor.url} target="_blank" rel="noopener noreferrer">
                      {factor.source}
                    </a>
                  ) : (
                    <span style={{ color: 'var(--text-muted)' }}>{factor.source}</span>
                  )}
                  <div className="fine-print" style={{ marginTop: 3 }}>
                    {factor.note}
                  </div>
                  {factor.setting && (
                    <div className="fine-print" style={{ marginTop: 3 }}>
                      change it with <code>{factor.setting}</code>
                    </div>
                  )}
                </td>
                <td style={{ color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                  {factor.date ?? NO_ESTIMATE}
                </td>
                <td>
                  <span className={`badge ${meta.badge}`} title={meta.what}>
                    {meta.label}
                  </span>
                  {/* Which precedence layer chose this factor — a different
                      question from confidence, so its own chip rather than
                      folded into the one above. Omitted entirely on a run
                      recorded before layered overrides existed. */}
                  {(() => {
                    const layer = layerMeta(factor.layer)
                    return layer ? (
                      <span
                        className={`badge ${layer.badge}`}
                        style={{ marginLeft: 4 }}
                        title={layer.what}
                      >
                        {layer.label}
                      </span>
                    ) : null
                  })()}
                  {/* The band's own "was this narrowed by evidence" marker —
                      a different question from confidence or precedence
                      layer, so its own chip. */}
                  {factor.key === 'uncertainty_band' && factor.derived && (
                    <span className="badge badge-violet" style={{ marginLeft: 4 }} title={BAND_DERIVE_NOTE}>
                      derived
                    </span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** The named biases that apply to this run, with the direction each one pushes. */
export function CaveatList({ caveats }: { caveats: EmissionsCaveat[] }) {
  if (caveats.length === 0) {
    return (
      <div className="empty" style={{ padding: '4px 0' }}>
        This run carries no recorded caveats — it predates them. That is not the same as having
        none.
      </div>
    )
  }
  return (
    <div className="stack" style={{ gap: 8 }}>
      {caveats.map((caveat) => {
        const direction = caveatDirection(caveat.direction)
        return (
          <div key={caveat.key} className="caveat-row">
            <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 2 }}>
              <span className="mono-label" style={{ textTransform: 'none' }}>
                {caveat.label}
              </span>
              <span
                className="badge badge-gray"
                style={{ color: direction.color, borderColor: 'var(--border)' }}
                title={direction.what}
              >
                {direction.label}
              </span>
            </div>
            <div className="fine-print">{caveat.note}</div>
          </div>
        )
      })}
    </div>
  )
}

/** What moves if one input alone is wrong. Dominant contributors first, because
 *  the point of the decomposition is that grid intensity and the energy class are
 *  where the width comes from — everything else is second order. */
export function SensitivityTable({ uncertainty }: { uncertainty: EmissionsUncertainty }) {
  const rows = [...uncertainty.contributions].sort(
    (a, b) => Number(b.dominant) - Number(a.dominant),
  )
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="md-table-wrap">
        <table className="mono-table">
          <thead>
            <tr>
              <th>If this input alone is wrong</th>
              <th className="num">Low</th>
              <th className="num">High</th>
              <th>Weight</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td>{row.label}</td>
                <td className="num" style={{ whiteSpace: 'nowrap' }}>
                  {formatFactor(row.low_multiplier, 2)}x
                </td>
                <td className="num" style={{ whiteSpace: 'nowrap' }}>
                  {formatFactor(row.high_multiplier, 2)}x
                </td>
                <td>
                  {row.dominant ? (
                    <span
                      className="badge badge-amber"
                      title="One of the inputs that dominates the width of the range. Refining this one is what actually narrows the estimate."
                    >
                      dominant
                    </span>
                  ) : (
                    <span style={{ color: 'var(--text-muted)' }}>second order</span>
                  )}
                </td>
                <td style={{ color: 'var(--text-muted)' }}>
                  {row.note}
                  {contributionEvidenceLabel(row.evidence) && (
                    <span
                      className="badge badge-blue"
                      style={{ marginLeft: 6 }}
                      title="This row was narrowed because the run actually has this evidence — see the band's own derivation note below."
                    >
                      {contributionEvidenceLabel(row.evidence)}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="fine-print">{oneSidedNote(rows)}</div>
      {/* Present only when `band.derived` was set and at least one row
          actually narrowed past the configured band — see
          `emissions.ts::bandDerivationText`. */}
      {uncertainty.derivation && (
        <div className="fine-print">{bandDerivationText(uncertainty.derivation)}</div>
      )}
    </div>
  )
}

/** Names the one-sided rows explicitly: a contributor whose low multiplier is 1
 *  can only push the real figure up, which is the direction that matters. */
function oneSidedNote(rows: EmissionsUncertaintyContribution[]): string {
  const oneSided = rows.filter((r) => r.low_multiplier >= 1 && r.high_multiplier > 1)
  const base =
    'These multipliers are a sensitivity view, one input at a time. Their product is deliberately NOT the headline range — multiplying them would give a span wider than any published methodology claims.'
  if (oneSided.length === 0) return base
  return `${base} One-sided (can only push the real figure up, never down): ${oneSided
    .map((r) => r.label.toLowerCase())
    .join(', ')}.`
}
