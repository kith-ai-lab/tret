/** The Water view of the Emissions dashboard: window totals and the by-model,
 *  by-harness, by-day and by-basis breakdowns, from the `water_*` fields the
 *  analytics endpoint already returns. A bucket with no water figure shows
 *  "Not recorded", never 0, and `runs_without_water` is always stated. The
 *  window has no water band (the backend does not sum one), so none is drawn. */
import type { EmissionsAnalytics, EmissionsBucket } from '../../api/client'
import { MethodologyLink } from './MethodologyDialog'
import { type Column, MonoTable } from './MonoTable'
import {
  WATER_BASIS_NOTE,
  WATER_ESTIMATE_NOTE,
  WATER_EVERYDAY_NOTE,
  WATER_HYDRO_NOTE,
  WATER_PART_META,
  share,
  waterEveryday,
} from './emissions'
import { NO_WATER, NO_WATER_HINT, formatTokens, formatWaterScaled } from './format'

type WaterBucket = Pick<
  EmissionsBucket,
  'runs' | 'water_ml' | 'water_onsite_ml' | 'water_offsite_ml' | 'runs_without_water'
>

function cell(ml: number | null | undefined) {
  const text = formatWaterScaled(ml)
  return text ?? <span title={NO_WATER_HINT}>{NO_WATER}</span>
}

/** "N runs not counted (no water recorded)", or null when every run counted. */
export function WaterUncounted({ n }: { n: number | undefined }) {
  if (!n) return null
  return (
    <span className="badge badge-amber" title={NO_WATER_HINT}>
      {formatTokens(n)} run{n === 1 ? '' : 's'} not counted (no water recorded)
    </span>
  )
}

function Section({ title, hint, children }: { title: string; hint: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        {title}
      </div>
      <div className="fine-print" style={{ marginBottom: 8 }}>
        {hint}
      </div>
      {children}
    </div>
  )
}

function columns<T extends WaterBucket>(first: Column<T>): Column<T>[] {
  return [
    first,
    { key: 'runs', header: 'Runs', align: 'right', render: (r) => formatTokens(r.runs) },
    { key: 'water', header: 'Water (est.)', align: 'right', render: (r) => cell(r.water_ml) },
    { key: 'on', header: WATER_PART_META[0].label, align: 'right', render: (r) => cell(r.water_onsite_ml) },
    { key: 'off', header: WATER_PART_META[1].label, align: 'right', render: (r) => cell(r.water_offsite_ml) },
    {
      key: 'unc',
      header: 'Not counted',
      align: 'right',
      render: (r) => (r.runs_without_water ? formatTokens(r.runs_without_water) : '0'),
    },
  ]
}

export function WaterDashboard({ data }: { data: EmissionsAnalytics }) {
  const t = data.totals
  const total = t.water_ml
  const on = t.water_onsite_ml
  const off = t.water_offsite_ml
  const everyday = waterEveryday(total)
  return (
    <div className="stack" style={{ gap: 26 }}>
      <div className="callout callout-note">
        <span className="callout-title">Water is consumption, and estimated</span>
        {WATER_ESTIMATE_NOTE} {WATER_BASIS_NOTE} {WATER_HYDRO_NOTE}
        <div style={{ marginTop: 6 }}>
          Methodology: <MethodologyLink topic="water" label="water methodology" /> (the full document).
        </div>
      </div>

      <div className="panel stack" style={{ gap: 10 }}>
        <div className="config-stats" style={{ gap: 34 }}>
          <div className="config-stat">
            <div className="mono-label">Total water (est.)</div>
            <div className="mono-body">{cell(total)}</div>
            {everyday && (
              <span className="band-under" title={WATER_EVERYDAY_NOTE}>
                {everyday}
              </span>
            )}
          </div>
          <div className="config-stat">
            <div className="mono-label">{WATER_PART_META[0].label}</div>
            <div className="mono-body" title={WATER_PART_META[0].what}>
              {cell(on)}
            </div>
          </div>
          <div className="config-stat">
            <div className="mono-label">{WATER_PART_META[1].label}</div>
            <div className="mono-body" title={WATER_PART_META[1].what}>
              {cell(off)}
            </div>
          </div>
          <div className="config-stat">
            <div className="mono-label">Runs counted</div>
            <div className="mono-body">{formatTokens(t.runs - (t.runs_without_water ?? 0))}</div>
          </div>
        </div>
        {total !== null && total !== undefined && on != null && off != null && total > 0 && (
          <div
            className="stacked-bar"
            style={{ height: 10 }}
            role="img"
            aria-label="Window water split: on-site cooling and off-site power generation"
          >
            <span style={{ width: `${share(on, on + off)}%`, background: 'var(--blue)', opacity: 0.8 }} title={`${WATER_PART_META[0].label}`} />
            <span style={{ width: `${share(off, on + off)}%`, background: 'var(--amber)', opacity: 0.8 }} title={`${WATER_PART_META[1].label}`} />
          </div>
        )}
        <div className="fine-print">
          <WaterUncounted n={t.runs_without_water} />{' '}
          {total === null || total === undefined
            ? 'No run in this window has a water figure, or the runs mix water bases, so no total is shown. That is not zero.'
            : 'Runs recorded before water accounting are left out of every figure here, never counted as zero.'}
        </div>
      </div>

      <Section title="Water by model" hint="Where the window's water went, by model.">
        <MonoTable
          columns={columns<EmissionsAnalytics['by_model'][number]>({ key: 'model', header: 'Model', render: (r) => r.model })}
          rows={data.by_model}
          rowKey={(r) => r.model}
          empty="No model carried an estimate in this window."
        />
      </Section>
      <Section title="Water by harness" hint="Which workflows account for the window's water.">
        <MonoTable
          columns={columns<EmissionsAnalytics['by_harness'][number]>({ key: 'harness', header: 'Harness', render: (r) => r.harness_name })}
          rows={data.by_harness}
          rowKey={(r) => r.harness_id}
          empty="No harness carried an estimate in this window."
        />
      </Section>
      {data.by_basis.length > 1 && (
        <Section title="Water by carbon basis" hint="The same buckets the carbon view groups by, shown for reference. Water itself sums within one water basis (consumption).">
          <MonoTable
            columns={columns<EmissionsAnalytics['by_basis'][number]>({ key: 'basis', header: 'Basis', render: (r) => r.basis ?? 'not recorded' })}
            rows={data.by_basis}
            rowKey={(r) => r.basis ?? 'unrecorded'}
            empty="No data."
          />
        </Section>
      )}
      <WaterByDay rows={data.by_day} />
    </div>
  )
}

function WaterByDay({ rows }: { rows: EmissionsAnalytics['by_day'] }) {
  if (rows.length === 0) return null
  const max = Math.max(...rows.map((r) => r.water_ml ?? 0), 0)
  return (
    <Section title="Water by day" hint="Daily totals as recorded, scaled to the largest day. A day with no water figure is an empty slot, not zero.">
      <div className="panel">
        <div className="spark" role="img" aria-label={`Estimated water per day across ${rows.length} days`}>
          {rows.map((r) => (
            <div
              key={r.date}
              className="spark-col"
              title={`${r.date}: ${formatWaterScaled(r.water_ml) ?? NO_WATER}${r.runs_without_water ? ` · ${r.runs_without_water} run(s) not counted` : ''}`}
            >
              <i style={{ height: `${r.water_ml ? share(r.water_ml, max) : 0}%` }} />
            </div>
          ))}
        </div>
      </div>
    </Section>
  )
}
