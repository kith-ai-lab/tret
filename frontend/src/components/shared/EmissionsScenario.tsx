/** "Compare a scenario" — a what-if drawer on the Emissions page.
 *
 *  A reduced override form (grid default, PUE cloud/local, the judgment band)
 *  posts to `POST /api/analytics/emissions/whatif`, which recomputes the same
 *  window under those hypothetical factors without writing anything anywhere —
 *  it is a comparison, not a save. `recorded` and `scenario` are rendered with
 *  the exact same `TotalsStrip` the page itself uses, side by side, so a
 *  scenario reads as "the same page, twice" rather than a second UI to learn.
 *
 *  Two rules carried over from the rest of the emissions surface:
 *  1. The band is a judgment band here too — never a confidence interval.
 *  2. The delta is never called an offset, a credit, or a saving. It is signed
 *     (scenario minus recorded): negative means the scenario would have been
 *     lighter/cheaper, positive means heavier/dearer.
 */
import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'

import {
  api,
  ApiError,
  EMISSIONS_OVERRIDE_PROVIDERS,
  gateRefusalDetail,
  REGION_TOKEN_RE,
  type EmissionsBandOverride,
  type EmissionsGridBasisValue,
  type EmissionsOverrides,
  type EmissionsWhatifResult,
  type GridBasis,
} from '../../api/client'
import {
  BAND_DERIVE_NOTE,
  BAND_SHORT,
  BAND_WHY,
  GRID_BASIS_META,
  gridBasisLabel,
  NOT_SUMMABLE_WHY,
  REGION_PIN_NOTE,
  SUMMABLE_ACROSS_BASES_NOTE,
} from './emissions'
import { formatCo2eScaled, formatCostScaled, formatEnergyScaled, formatTokens, NO_ESTIMATE, orDash } from './format'
import { Modal } from './Modal'
import { TotalsStrip } from '../../views/Emissions'

const GRID_BASIS_OPTIONS: EmissionsGridBasisValue[] = ['location_based', 'market_based', 'unspecified']

interface ScenarioDraft {
  gridPerKwh: string
  gridBasis: EmissionsGridBasisValue
  gridLabel: string
  pueCloud: string
  pueLocal: string
  pueLabel: string
  bandLow: string
  bandHigh: string
  bandLabel: string
  bandDerived: boolean
  /** A region pin for exactly one provider — '' means no pin in this
   *  scenario. Mirrors `grid.regions` in the real override document, scoped
   *  down to one provider since this drawer is a reduced form. */
  regionProvider: string
  regionValue: string
}

function emptyDraft(): ScenarioDraft {
  return {
    gridPerKwh: '',
    gridBasis: 'unspecified',
    gridLabel: '',
    pueCloud: '',
    pueLocal: '',
    pueLabel: '',
    bandLow: '',
    bandHigh: '',
    bandLabel: '',
    bandDerived: false,
    regionProvider: '',
    regionValue: '',
  }
}

function num(s: string): number | undefined {
  const t = s.trim()
  if (t === '') return undefined
  const n = Number(t)
  return Number.isFinite(n) ? n : undefined
}

/** Same rule as the settings panel: a block that sets a number requires a
 *  non-empty label. Checked client-side before POSTing; the server is free to
 *  422 with its own message too, which is shown verbatim alongside this. */
function validateDraft(draft: ScenarioDraft): string[] {
  const errors: string[] = []
  if (num(draft.gridPerKwh) !== undefined && draft.gridLabel.trim() === '') {
    errors.push('grid.default.label is required when grid.default.g_per_kwh is set')
  }
  if ((num(draft.pueCloud) !== undefined || num(draft.pueLocal) !== undefined) && draft.pueLabel.trim() === '') {
    errors.push('pue.label is required when pue.cloud or pue.local is set')
  }
  const low = num(draft.bandLow)
  const high = num(draft.bandHigh)
  if (low !== undefined || high !== undefined) {
    if (draft.bandLabel.trim() === '') errors.push('band.label is required when band.low or band.high is set')
  }
  if (draft.regionProvider && draft.regionValue.trim() && !REGION_TOKEN_RE.test(draft.regionValue.trim())) {
    errors.push(`grid.regions: invalid region: '${draft.regionValue.trim()}'`)
  }
  return errors
}

function draftToFactors(draft: ScenarioDraft): Partial<EmissionsOverrides> {
  const factors: Partial<EmissionsOverrides> = {}
  const gridVal = num(draft.gridPerKwh)
  if (gridVal !== undefined) {
    factors.grid = {
      default: { g_per_kwh: gridVal, basis: draft.gridBasis, label: draft.gridLabel.trim() },
    }
  }
  const pueCloud = num(draft.pueCloud)
  const pueLocal = num(draft.pueLocal)
  if (pueCloud !== undefined || pueLocal !== undefined) {
    factors.pue = { label: draft.pueLabel.trim() }
    if (pueCloud !== undefined) factors.pue.cloud = pueCloud
    if (pueLocal !== undefined) factors.pue.local = pueLocal
  }
  const low = num(draft.bandLow)
  const high = num(draft.bandHigh)
  if (low !== undefined || high !== undefined || draft.bandDerived) {
    const band: EmissionsBandOverride = { label: draft.bandLabel.trim(), derived: draft.bandDerived }
    if (low !== undefined) band.low = low
    if (high !== undefined) band.high = high
    factors.band = band
  }
  if (draft.regionProvider && draft.regionValue.trim()) {
    factors.grid = { ...(factors.grid ?? {}), regions: { [draft.regionProvider]: draft.regionValue.trim() } }
  }
  return factors
}

/** The button that opens the drawer, plus the drawer itself. Self-contained:
 *  the Emissions page only needs to render `<EmissionsScenarioButton
 *  projectId={...} days={days} />` next to its window selector. */
export function EmissionsScenarioButton({ projectId, days }: { projectId: string | null; days: number }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="btn btn-sm" onClick={() => setOpen(true)}>
        Compare a scenario
      </button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Compare a scenario"
        subtitle={`Recomputes the last ${days} days under hypothetical factors — nothing here is saved.`}
        width={980}
      >
        <ScenarioContent projectId={projectId} days={days} />
      </Modal>
    </>
  )
}

function ScenarioContent({ projectId, days }: { projectId: string | null; days: number }) {
  const [draft, setDraft] = useState<ScenarioDraft>(emptyDraft())
  const [clientErrors, setClientErrors] = useState<string[]>([])

  const whatifMutation = useMutation({
    mutationFn: () =>
      api.emissionsWhatif({ project_id: projectId, days, factors: draftToFactors(draft) }),
  })
  const apiError = whatifMutation.error as ApiError | null

  const run = () => {
    const errors = validateDraft(draft)
    setClientErrors(errors)
    if (errors.length > 0) return
    whatifMutation.mutate()
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="panel stack" style={{ gap: 14 }}>
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Grid intensity — default
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-end', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 140 }}>
              <label className="mono-label">gCO₂e/kWh</label>
              <input
                type="number"
                step="any"
                min={0}
                value={draft.gridPerKwh}
                onChange={(e) => setDraft({ ...draft, gridPerKwh: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 150 }}>
              <label className="mono-label">GHG basis</label>
              <select
                value={draft.gridBasis}
                onChange={(e) => setDraft({ ...draft, gridBasis: e.target.value as EmissionsGridBasisValue })}
              >
                {GRID_BASIS_OPTIONS.map((b) => (
                  <option key={b} value={b}>
                    {GRID_BASIS_META[b]?.label ?? b}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label className="mono-label">Label (source)</label>
              <input
                type="text"
                placeholder="e.g. a candidate PPA-backed grid figure"
                value={draft.gridLabel}
                onChange={(e) => setDraft({ ...draft, gridLabel: e.target.value })}
              />
            </div>
          </div>
        </div>

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Region pin
          </div>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            {REGION_PIN_NOTE}
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-end', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 160 }}>
              <label className="mono-label">Provider</label>
              <select
                value={draft.regionProvider}
                onChange={(e) => setDraft({ ...draft, regionProvider: e.target.value })}
              >
                <option value="">none</option>
                {EMISSIONS_OVERRIDE_PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ marginBottom: 0, width: 150 }}>
              <label className="mono-label">Region</label>
              <input
                type="text"
                placeholder="us-east"
                disabled={!draft.regionProvider}
                value={draft.regionValue}
                onChange={(e) => setDraft({ ...draft, regionValue: e.target.value })}
              />
            </div>
          </div>
        </div>

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            PUE
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-end', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">Cloud</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.pueCloud}
                onChange={(e) => setDraft({ ...draft, pueCloud: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">Local</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.pueLocal}
                onChange={(e) => setDraft({ ...draft, pueLocal: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label className="mono-label">Label (source)</label>
              <input
                type="text"
                value={draft.pueLabel}
                onChange={(e) => setDraft({ ...draft, pueLabel: e.target.value })}
              />
            </div>
          </div>
        </div>

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Judgment band
          </div>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            {BAND_WHY} {BAND_SHORT}
          </div>
          <label className="check-row" style={{ padding: 0, marginBottom: 6 }}>
            <input
              type="checkbox"
              checked={draft.bandDerived}
              onChange={(e) => setDraft({ ...draft, bandDerived: e.target.checked })}
            />
            <span>Derive from evidence</span>
          </label>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            {BAND_DERIVE_NOTE}
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-end', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">Low (÷)</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.bandLow}
                onChange={(e) => setDraft({ ...draft, bandLow: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">High (×)</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.bandHigh}
                onChange={(e) => setDraft({ ...draft, bandHigh: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label className="mono-label">Label (source)</label>
              <input
                type="text"
                value={draft.bandLabel}
                onChange={(e) => setDraft({ ...draft, bandLabel: e.target.value })}
              />
            </div>
          </div>
        </div>

        {clientErrors.length > 0 && (
          <div className="error-text">
            Fix the following before comparing:
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {clientErrors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </div>
        )}
        {apiError && (
          <div className="error-text">
            {apiError.status === 403 ? (gateRefusalDetail(apiError) ?? 'Not permitted.') : apiError.message}
          </div>
        )}

        <div>
          <button
            type="button"
            className="btn btn-primary"
            onClick={run}
            disabled={whatifMutation.isPending}
          >
            {whatifMutation.isPending ? 'Comparing…' : 'Compare'}
          </button>
        </div>
      </div>

      {whatifMutation.data && <ScenarioResult result={whatifMutation.data} />}
    </div>
  )
}

function DeltaStat({
  label,
  value,
  hint,
}: {
  label: string
  value: string
  hint?: string
}) {
  return (
    <div className="config-stat">
      <div className="mono-label">{label}</div>
      <div className="mono-body" title={hint}>
        {value}
      </div>
    </div>
  )
}

/** The bases behind a totals bucket, in words — same presentation the
 *  Emissions page's own `basisList` uses for recorded totals ("location-based",
 *  or "location-based and market-based" for more than one). Kept local rather
 *  than shared: the source list is tiny and specific to a heading, not a
 *  computed figure. */
function basisList(bases: GridBasis[]): string {
  const labels = bases.map(gridBasisLabel)
  if (labels.length <= 1) return labels[0] ?? NO_ESTIMATE
  return `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`
}

function ScenarioResult({ result }: { result: EmissionsWhatifResult }) {
  const { recorded, scenario, delta } = result
  const notSummable = delta.co2e_g === null
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Recorded (as it actually ran)
        </div>
        <TotalsStrip totals={recorded.totals} />
      </div>
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Scenario ({basisList(scenario.totals.grid_bases)} accounting)
        </div>
        <TotalsStrip totals={scenario.totals} />
      </div>

      <div className="panel stack" style={{ gap: 10 }}>
        <div className="mono-label">Difference (scenario − recorded, signed)</div>
        <div className="config-stats" style={{ gap: 34 }}>
          <DeltaStat
            label="Δ CO₂e (est.)"
            value={notSummable ? NO_ESTIMATE : orDash(formatCo2eScaled(delta.co2e_g))}
            hint={
              notSummable
                ? 'No single figure: recorded and/or scenario span more than one GHG Protocol basis.'
                : 'Negative means the scenario would have been lighter than what was actually recorded; positive means heavier. Not an offset, a credit, or a saving — an efficiency indicator only.'
            }
          />
          <DeltaStat
            label="Δ CO₂e (%)"
            value={notSummable || delta.co2e_pct === null ? NO_ESTIMATE : `${delta.co2e_pct > 0 ? '+' : ''}${delta.co2e_pct}%`}
            hint="A whole number on purpose: carbon percentages are deliberately coarse in this methodology, so a decimal place here would be false precision."
          />
          <DeltaStat
            label="Δ energy (est.)"
            value={orDash(formatEnergyScaled(delta.energy_wh))}
          />
          <DeltaStat
            label="Δ money"
            value={formatCostScaled(delta.avoided_usd)}
            hint="Exact arithmetic on published list prices — the one figure here that is not an estimate."
          />
          <DeltaStat label="Runs recomputed" value={formatTokens(result.runs_recomputed)} />
          <DeltaStat
            label="Runs skipped"
            value={formatTokens(result.runs_skipped)}
            hint="Runs skipped only because their model is no longer in the catalog — there is nothing to recompute them against, so they are excluded from both totals above rather than left one-sided."
          />
        </div>

        <div className="fine-print">
          {result.basis} Nothing here is written anywhere — this is a comparison, not a save. Change
          the workspace's actual settings above to make it apply to the next real run.
        </div>

        {result.warnings && result.warnings.length > 0 && (
          <div className="callout callout-warn">
            <span className="callout-title">Scenario warnings</span>
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {result.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </div>
        )}

        {notSummable && (
          <div className="callout callout-warn">
            <span className="callout-title">No single difference to show</span>
            {NOT_SUMMABLE_WHY} {SUMMABLE_ACROSS_BASES_NOTE}
          </div>
        )}
      </div>
    </div>
  )
}
