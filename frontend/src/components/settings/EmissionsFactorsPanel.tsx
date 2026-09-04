/** Settings → Emissions factors: the workspace-level overrides for the grid
 *  intensity, PUE, embodied hardware, judgment band and baseline model that
 *  otherwise come from env vars and tret's own shipped defaults.
 *
 *  Visible to every workspace member (read-only) so anyone can see what will
 *  apply to the next run; editable only by admin/owner, who get the form and
 *  its Save / Clear overrides controls. The backend is the source of truth for
 *  the validation rule ("a block that sets a number requires a non-empty
 *  label") — this form checks the same rule before submitting so a member
 *  finds out immediately rather than on a round trip, and still surfaces the
 *  server's own 422/403 message verbatim, because the server's rule is the
 *  one that actually governs.
 *
 *  The band edited here is a judgment band, never a confidence interval — see
 *  `emissions.ts::BAND_SHORT`. The effective-factors table beneath the form is
 *  "what will apply to the next run", not a history of what already happened;
 *  every value carries the precedence layer that produced it as a small chip.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import {
  api,
  ApiError,
  EMISSIONS_OVERRIDE_PROVIDERS,
  gateRefusalDetail,
  type EmissionsBandOverride,
  type EmissionsEffectiveFactors,
  type EmissionsEmbodiedOverride,
  type EmissionsGridBasisValue,
  type EmissionsGridOverride,
  type EmissionsOverrides,
  type EmissionsPueLocalProfile,
  type EmissionsPueOverride,
  type EmissionsResolvedValue,
  type EmissionsShippedDefault,
} from '../../api/client'
import {
  BAND_SHORT,
  BAND_WHY,
  GRID_BASIS_META,
  gridBasisLabel,
  layerMeta,
  PUE_PROFILE_LABELS,
} from '../shared/emissions'
import { formatDateTime } from '../shared/format'
import { QueryError } from '../shared/MonoTable'

const GRID_BASIS_OPTIONS: EmissionsGridBasisValue[] = ['location_based', 'market_based', 'unspecified']
const LOCAL_PROFILE_OPTIONS: EmissionsPueLocalProfile[] = ['workstation', 'onprem_datacenter']

const PROVIDER_LABELS: Record<string, string> = {
  local: 'local',
  anthropic: 'anthropic',
  kimi: 'kimi',
  openrouter: 'openrouter',
}

// ── draft shapes: every number is edited as a string so a field can be empty
// (meaning "unset", not zero) while it is being typed ──────────────────────

interface GridRowDraft {
  g_per_kwh: string
  basis: EmissionsGridBasisValue
  label: string
  url: string
  as_of: string
}

interface PueDraft {
  cloud: string
  local_profile: EmissionsPueLocalProfile
  local: string
  label: string
}

interface EmbodiedDraft {
  g_per_run: string
  label: string
}

interface BandDraft {
  low: string
  high: string
  label: string
}

interface FormDraft {
  gridDefault: GridRowDraft
  gridProviders: Record<string, GridRowDraft>
  pue: PueDraft
  embodied: EmbodiedDraft
  band: BandDraft
  baselineModel: string
}

function emptyGridRow(): GridRowDraft {
  return { g_per_kwh: '', basis: 'unspecified', label: '', url: '', as_of: '' }
}

function gridRowFromOverride(o: EmissionsGridOverride | undefined): GridRowDraft {
  if (!o) return emptyGridRow()
  return {
    g_per_kwh: String(o.g_per_kwh),
    basis: o.basis,
    label: o.label,
    url: o.url ?? '',
    as_of: o.as_of ?? '',
  }
}

function draftFromOverrides(overrides: EmissionsOverrides | Record<string, never>): FormDraft {
  const o = overrides as EmissionsOverrides
  const gridProviders: Record<string, GridRowDraft> = {}
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    gridProviders[provider] = gridRowFromOverride(o.grid?.providers?.[provider])
  }
  return {
    gridDefault: gridRowFromOverride(o.grid?.default),
    gridProviders,
    pue: {
      cloud: o.pue?.cloud !== undefined ? String(o.pue.cloud) : '',
      local_profile: o.pue?.local_profile ?? 'workstation',
      local: o.pue?.local !== undefined ? String(o.pue.local) : '',
      label: o.pue?.label ?? '',
    },
    embodied: {
      g_per_run: o.embodied?.g_per_run !== undefined ? String(o.embodied.g_per_run) : '',
      label: o.embodied?.label ?? '',
    },
    band: {
      low: o.band?.low !== undefined ? String(o.band.low) : '',
      high: o.band?.high !== undefined ? String(o.band.high) : '',
      label: o.band?.label ?? '',
    },
    baselineModel: o.baseline_model ?? '',
  }
}

/** One "field is required" violation, named the way the server names its own
 *  422s — a dotted path — so the client rule and the server rule read as the
 *  same rule. */
interface DraftError {
  field: string
  message: string
}

function num(s: string): number | undefined {
  const t = s.trim()
  if (t === '') return undefined
  const n = Number(t)
  return Number.isFinite(n) ? n : undefined
}

/** Client-side mirror of the server's one validation rule: a block that sets a
 *  number requires a non-empty label. Runs before every submit; the server
 *  still re-checks and its message wins if the two ever disagree. */
function validateDraft(draft: FormDraft): DraftError[] {
  const errors: DraftError[] = []
  const checkGridRow = (row: GridRowDraft, field: string) => {
    if (num(row.g_per_kwh) !== undefined && row.label.trim() === '') {
      errors.push({ field: `${field}.label`, message: `${field}.label is required when ${field}.g_per_kwh is set` })
    }
  }
  checkGridRow(draft.gridDefault, 'grid.default')
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    checkGridRow(draft.gridProviders[provider], `grid.providers.${provider}`)
  }
  if ((num(draft.pue.cloud) !== undefined || num(draft.pue.local) !== undefined) && draft.pue.label.trim() === '') {
    errors.push({ field: 'pue.label', message: 'pue.label is required when pue.cloud or pue.local is set' })
  }
  if (num(draft.embodied.g_per_run) !== undefined && draft.embodied.label.trim() === '') {
    errors.push({ field: 'embodied.label', message: 'embodied.label is required when embodied.g_per_run is set' })
  }
  const bandLow = num(draft.band.low)
  const bandHigh = num(draft.band.high)
  if (bandLow !== undefined || bandHigh !== undefined) {
    if (draft.band.label.trim() === '') {
      errors.push({ field: 'band.label', message: 'band.label is required when band.low or band.high is set' })
    }
  }
  return errors
}

/** Builds the `EmissionsOverrides` body from the draft — omitting any block
 *  or row whose number was left blank, so clearing a field removes the
 *  override rather than sending it as an explicit zero. */
function overridesFromDraft(draft: FormDraft): EmissionsOverrides {
  const body: EmissionsOverrides = {}

  const gridDefaultVal = num(draft.gridDefault.g_per_kwh)
  const providers: Record<string, EmissionsGridOverride> = {}
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    const row = draft.gridProviders[provider]
    const v = num(row.g_per_kwh)
    if (v !== undefined) {
      providers[provider] = {
        g_per_kwh: v,
        basis: row.basis,
        label: row.label.trim(),
        url: row.url.trim() || undefined,
        as_of: row.as_of.trim() || undefined,
      }
    }
  }
  if (gridDefaultVal !== undefined || Object.keys(providers).length > 0) {
    body.grid = {}
    if (gridDefaultVal !== undefined) {
      body.grid.default = {
        g_per_kwh: gridDefaultVal,
        basis: draft.gridDefault.basis,
        label: draft.gridDefault.label.trim(),
        url: draft.gridDefault.url.trim() || undefined,
        as_of: draft.gridDefault.as_of.trim() || undefined,
      }
    }
    if (Object.keys(providers).length > 0) body.grid.providers = providers
  }

  const pueCloud = num(draft.pue.cloud)
  const pueLocal = num(draft.pue.local)
  if (pueCloud !== undefined || pueLocal !== undefined) {
    const pue: EmissionsPueOverride = { label: draft.pue.label.trim() }
    if (pueCloud !== undefined) pue.cloud = pueCloud
    if (pueLocal !== undefined) pue.local = pueLocal
    pue.local_profile = draft.pue.local_profile
    body.pue = pue
  }

  const embodiedVal = num(draft.embodied.g_per_run)
  if (embodiedVal !== undefined) {
    const embodied: EmissionsEmbodiedOverride = {
      g_per_run: embodiedVal,
      label: draft.embodied.label.trim(),
    }
    body.embodied = embodied
  }

  const bandLow = num(draft.band.low)
  const bandHigh = num(draft.band.high)
  if (bandLow !== undefined || bandHigh !== undefined) {
    const band: EmissionsBandOverride = { label: draft.band.label.trim() }
    if (bandLow !== undefined) band.low = bandLow
    if (bandHigh !== undefined) band.high = bandHigh
    body.band = band
  }

  if (draft.baselineModel.trim() !== '') body.baseline_model = draft.baselineModel.trim()

  return body
}

// ── small form pieces ────────────────────────────────────────────────────

function ShippedDefaultHint({ shipped, unit }: { shipped: EmissionsShippedDefault | undefined; unit?: string }) {
  if (!shipped) return null
  return (
    <div className="fine-print" style={{ marginTop: 2 }}>
      default {shipped.value}
      {unit ? ` ${unit}` : ''} — {shipped.label}
    </div>
  )
}

function FieldError({ errors, field }: { errors: DraftError[]; field: string }) {
  const hit = errors.find((e) => e.field === field)
  if (!hit) return null
  return (
    <div className="error-text" style={{ marginTop: 2, fontSize: 10.5 }}>
      {hit.message}
    </div>
  )
}

function GridRowFields({
  row,
  onChange,
  disabled,
  errors,
  field,
  shipped,
}: {
  row: GridRowDraft
  onChange: (row: GridRowDraft) => void
  disabled: boolean
  errors: DraftError[]
  field: string
  shipped: EmissionsShippedDefault | undefined
}) {
  return (
    <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
      <div className="field" style={{ marginBottom: 0, width: 140 }}>
        <label className="mono-label">gCO₂e/kWh</label>
        <input
          type="number"
          step="any"
          min={0}
          value={row.g_per_kwh}
          disabled={disabled}
          onChange={(e) => onChange({ ...row, g_per_kwh: e.target.value })}
        />
        <ShippedDefaultHint shipped={shipped} unit="gCO₂e/kWh" />
      </div>
      <div className="field" style={{ marginBottom: 0, width: 150 }}>
        <label className="mono-label">GHG basis</label>
        <select
          value={row.basis}
          disabled={disabled}
          onChange={(e) => onChange({ ...row, basis: e.target.value as EmissionsGridBasisValue })}
        >
          {GRID_BASIS_OPTIONS.map((b) => (
            <option key={b} value={b}>
              {GRID_BASIS_META[b]?.label ?? b}
            </option>
          ))}
        </select>
      </div>
      <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
        <label className="mono-label">Label (source)</label>
        <input
          type="text"
          placeholder="e.g. Ontario grid, IESO 2024"
          value={row.label}
          disabled={disabled}
          onChange={(e) => onChange({ ...row, label: e.target.value })}
        />
        <FieldError errors={errors} field={`${field}.label`} />
      </div>
      <div className="field" style={{ marginBottom: 0, width: 160 }}>
        <label className="mono-label">URL (optional)</label>
        <input
          type="text"
          value={row.url}
          disabled={disabled}
          onChange={(e) => onChange({ ...row, url: e.target.value })}
        />
      </div>
      <div className="field" style={{ marginBottom: 0, width: 120 }}>
        <label className="mono-label">As of (optional)</label>
        <input
          type="text"
          placeholder="YYYY-MM-DD"
          value={row.as_of}
          disabled={disabled}
          onChange={(e) => onChange({ ...row, as_of: e.target.value })}
        />
      </div>
    </div>
  )
}

// ── the panel ────────────────────────────────────────────────────────────

export function EmissionsFactorsPanel({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient()
  const settingsQuery = useQuery({ queryKey: ['emissions-settings'], queryFn: api.getEmissionsSettings })

  const [draft, setDraft] = useState<FormDraft | null>(null)
  const [clientErrors, setClientErrors] = useState<DraftError[]>([])

  // Sync the draft from the server once per load — not on every background
  // refetch, which would clobber whatever the member is mid-typing.
  const [syncedFrom, setSyncedFrom] = useState<unknown>(null)
  useEffect(() => {
    if (settingsQuery.data && settingsQuery.data !== syncedFrom) {
      setDraft(draftFromOverrides(settingsQuery.data.overrides))
      setSyncedFrom(settingsQuery.data)
    }
  }, [settingsQuery.data, syncedFrom])

  const saveMutation = useMutation({
    mutationFn: (body: EmissionsOverrides) => api.setEmissionsSettings(body),
    onSuccess: (result) => {
      queryClient.setQueryData(['emissions-settings'], result)
      setDraft(draftFromOverrides(result.overrides))
      setSyncedFrom(result)
      setClientErrors([])
    },
  })
  const clearMutation = useMutation({
    mutationFn: () => api.clearEmissionsSettings(),
    onSuccess: (result) => {
      queryClient.setQueryData(['emissions-settings'], result)
      setDraft(draftFromOverrides(result.overrides))
      setSyncedFrom(result)
      setClientErrors([])
    },
  })

  if (settingsQuery.isLoading || !draft) {
    return (
      <div>
        <div className="mono-label" style={{ marginBottom: 8 }}>
          Emissions factors
        </div>
        <div className="empty pulse">Loading emissions factors…</div>
      </div>
    )
  }
  if (settingsQuery.isError) {
    return (
      <div>
        <div className="mono-label" style={{ marginBottom: 8 }}>
          Emissions factors
        </div>
        <QueryError error={settingsQuery.error} what="this workspace's emissions factors" />
      </div>
    )
  }

  const data = settingsQuery.data!
  const overrides = data.overrides as EmissionsOverrides
  const shipped = data.shipped_defaults
  const saveError = saveMutation.error as ApiError | null
  const clearError = clearMutation.error as ApiError | null

  const submit = () => {
    const errors = validateDraft(draft)
    setClientErrors(errors)
    if (errors.length > 0) return
    saveMutation.mutate(overridesFromDraft(draft))
  }

  const clear = () => {
    if (!window.confirm('Clear every emissions override for this workspace? Runs will fall back to the managed/env/global defaults.')) return
    clearMutation.mutate()
  }

  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
        <div className="mono-label">Emissions factors</div>
        {!canEdit && (
          <span className="fine-print" style={{ margin: 0 }}>
            read-only — admin or owner can edit
          </span>
        )}
      </div>

      {data.error && (
        <div className="callout callout-warn" style={{ marginBottom: 12 }}>
          <span className="callout-title">Stored overrides did not validate</span>
          {data.error} The form below still shows the raw overrides on file — fix the offending
          field and save, or use "Clear overrides" to reset the workspace to its managed/env/global
          defaults.
        </div>
      )}

      <div className="panel stack" style={{ gap: 16 }}>
        <div className="fine-print">
          What powers each run's carbon estimate for this workspace: the grid intensity (overall and
          per provider), the PUE overhead, embodied hardware, the judgment band, and the baseline
          model for the same-token comparison. Anything left blank falls back to a managed default,
          an environment variable, or tret's own shipped default — see the effective factors table
          below for what is in force right now.
        </div>

        {/* ── grid: default + per-provider ── */}
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Grid intensity — default
          </div>
          <GridRowFields
            row={draft.gridDefault}
            onChange={(row) => setDraft({ ...draft, gridDefault: row })}
            disabled={!canEdit || saveMutation.isPending}
            errors={clientErrors}
            field="grid.default"
            shipped={shipped.grid_default}
          />
        </div>
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Grid intensity — per provider
          </div>
          <div className="stack" style={{ gap: 10 }}>
            {EMISSIONS_OVERRIDE_PROVIDERS.map((provider) => (
              <div key={provider}>
                <div className="fine-print" style={{ marginBottom: 4 }}>
                  {PROVIDER_LABELS[provider] ?? provider}
                </div>
                <GridRowFields
                  row={draft.gridProviders[provider]}
                  onChange={(row) =>
                    setDraft({
                      ...draft,
                      gridProviders: { ...draft.gridProviders, [provider]: row },
                    })
                  }
                  disabled={!canEdit || saveMutation.isPending}
                  errors={clientErrors}
                  field={`grid.providers.${provider}`}
                  shipped={shipped.grid_providers?.[provider]}
                />
              </div>
            ))}
          </div>
        </div>

        {/* ── PUE ── */}
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            PUE (power usage effectiveness)
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">Cloud</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.pue.cloud}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) => setDraft({ ...draft, pue: { ...draft.pue, cloud: e.target.value } })}
              />
              <ShippedDefaultHint shipped={shipped.pue_cloud} />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 160 }}>
              <label className="mono-label">Local profile</label>
              <select
                value={draft.pue.local_profile}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    pue: { ...draft.pue, local_profile: e.target.value as EmissionsPueLocalProfile },
                  })
                }
              >
                {LOCAL_PROFILE_OPTIONS.map((p) => (
                  <option key={p} value={p}>
                    {PUE_PROFILE_LABELS[p] ?? p}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">Local</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.pue.local}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) => setDraft({ ...draft, pue: { ...draft.pue, local: e.target.value } })}
              />
              <ShippedDefaultHint
                shipped={draft.pue.local_profile === 'onprem_datacenter' ? shipped.pue_onprem : shipped.pue_local}
              />
            </div>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label className="mono-label">Label (source)</label>
              <input
                type="text"
                placeholder="e.g. our colo's own PUE report"
                value={draft.pue.label}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) => setDraft({ ...draft, pue: { ...draft.pue, label: e.target.value } })}
              />
              <FieldError errors={clientErrors} field="pue.label" />
            </div>
          </div>
        </div>

        {/* ── embodied hardware ── */}
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Embodied hardware
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 140 }}>
              <label className="mono-label">g CO₂e / run</label>
              <input
                type="number"
                step="any"
                min={0}
                value={draft.embodied.g_per_run}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) =>
                  setDraft({ ...draft, embodied: { ...draft.embodied, g_per_run: e.target.value } })
                }
              />
              <ShippedDefaultHint shipped={shipped.embodied_g_per_run} unit="g/run" />
            </div>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label className="mono-label">Label (source)</label>
              <input
                type="text"
                value={draft.embodied.label}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) =>
                  setDraft({ ...draft, embodied: { ...draft.embodied, label: e.target.value } })
                }
              />
              <FieldError errors={clientErrors} field="embodied.label" />
            </div>
          </div>
        </div>

        {/* ── judgment band ── */}
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Judgment band
          </div>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            {BAND_WHY} {BAND_SHORT}
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">Low (÷)</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.band.low}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) => setDraft({ ...draft, band: { ...draft.band, low: e.target.value } })}
              />
              <ShippedDefaultHint shipped={shipped.band_low} />
              <FieldError errors={clientErrors} field="band.low" />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">High (×)</label>
              <input
                type="number"
                step="any"
                min={1}
                value={draft.band.high}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) => setDraft({ ...draft, band: { ...draft.band, high: e.target.value } })}
              />
              <ShippedDefaultHint shipped={shipped.band_high} />
              <FieldError errors={clientErrors} field="band.high" />
            </div>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label className="mono-label">Label (source)</label>
              <input
                type="text"
                placeholder="e.g. our own validation study"
                value={draft.band.label}
                disabled={!canEdit || saveMutation.isPending}
                onChange={(e) => setDraft({ ...draft, band: { ...draft.band, label: e.target.value } })}
              />
              <FieldError errors={clientErrors} field="band.label" />
            </div>
          </div>
        </div>

        {/* ── baseline model ── */}
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Baseline model
          </div>
          <div className="field" style={{ marginBottom: 0, maxWidth: 320 }}>
            <input
              type="text"
              placeholder={shipped.baseline_model ? String(shipped.baseline_model.value) : 'model id'}
              value={draft.baselineModel}
              disabled={!canEdit || saveMutation.isPending}
              onChange={(e) => setDraft({ ...draft, baselineModel: e.target.value })}
            />
          </div>
          <ShippedDefaultHint shipped={shipped.baseline_model} />
        </div>

        {(overrides.updated_by || overrides.updated_at) && (
          <div className="fine-print">
            Last changed{overrides.updated_by ? ` by ${overrides.updated_by}` : ''}
            {overrides.updated_at ? ` on ${formatDateTime(overrides.updated_at)}` : ''}.
          </div>
        )}

        {clientErrors.length > 0 && (
          <div className="error-text">
            Fix the following before saving:
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {clientErrors.map((e) => (
                <li key={e.field}>{e.message}</li>
              ))}
            </ul>
          </div>
        )}
        {saveError && (
          <div className="error-text">
            {saveError.status === 403
              ? (gateRefusalDetail(saveError) ?? 'Requires admin or owner role.')
              : saveError.message}
          </div>
        )}
        {clearError && (
          <div className="error-text">
            {clearError.status === 403
              ? (gateRefusalDetail(clearError) ?? 'Requires admin or owner role.')
              : clearError.message}
          </div>
        )}

        {canEdit && (
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submit}
              disabled={saveMutation.isPending}
            >
              {saveMutation.isPending ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={clear}
              disabled={clearMutation.isPending}
            >
              {clearMutation.isPending ? 'Clearing…' : 'Clear overrides'}
            </button>
          </div>
        )}
      </div>

      {data.effective ? (
        <EffectiveFactorsTable effective={data.effective} />
      ) : (
        <div className="fine-print" style={{ marginTop: 18 }}>
          Effective factors are unavailable until the stored overrides above validate again — fix
          the offending field and save, or clear the overrides.
        </div>
      )}
    </div>
  )
}

// ── effective factors: "what will apply to the next run" ─────────────────

function resolvedTitle(r: EmissionsResolvedValue): string {
  const parts = [`source: ${r.source}`]
  if (r.label) parts.push(`label: “${r.label}”`)
  if (r.as_of) parts.push(`as of ${r.as_of}`)
  parts.push(`change it with ${r.setting}`)
  return parts.join(' · ')
}

function ResolvedCell({ resolved, text }: { resolved: EmissionsResolvedValue; text: string }) {
  const layer = layerMeta(resolved.layer)
  const body = resolved.url ? (
    <a href={resolved.url} target="_blank" rel="noopener noreferrer" title={resolvedTitle(resolved)}>
      {text}
    </a>
  ) : (
    <span title={resolvedTitle(resolved)}>{text}</span>
  )
  return (
    <span style={{ whiteSpace: 'nowrap' }}>
      {body}
      {layer && (
        <span className={`badge ${layer.badge}`} style={{ marginLeft: 4 }} title={layer.what}>
          {layer.label}
        </span>
      )}
    </span>
  )
}

function EffectiveFactorsTable({ effective }: { effective: Record<string, EmissionsEffectiveFactors> }) {
  const rows = Object.entries(effective)
  return (
    <div style={{ marginTop: 18 }}>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        Effective factors
      </div>
      <div className="fine-print" style={{ marginBottom: 8 }}>
        What will apply to the next run, per provider — after run/harness overrides, this workspace's
        own settings above, any managed default, the environment, and finally tret's shipped default,
        in that order of precedence. Each value carries a chip naming which of those actually won.
      </div>
      {rows.length === 0 ? (
        <div className="empty">No providers configured yet.</div>
      ) : (
        <div className="md-table-wrap">
          <table className="mono-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th>Deployment</th>
                <th>Grid</th>
                <th>PUE</th>
                <th>Embodied</th>
                <th>Band</th>
                <th>Baseline</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([provider, f]) => (
                <tr key={provider}>
                  <td>{PROVIDER_LABELS[provider] ?? provider}</td>
                  <td>{f.deployment}</td>
                  <td>
                    <ResolvedCell
                      resolved={f.grid}
                      text={`${f.grid.value} gCO₂e/kWh · ${gridBasisLabel(f.grid_basis)}`}
                    />
                  </td>
                  <td>
                    <ResolvedCell
                      resolved={f.pue}
                      text={`${f.pue.value} (${PUE_PROFILE_LABELS[f.pue_profile] ?? f.pue_profile})`}
                    />
                  </td>
                  <td>
                    <ResolvedCell resolved={f.embodied_g} text={`${f.embodied_g.value} g/run`} />
                  </td>
                  <td>
                    <ResolvedCell resolved={f.band_low} text={`÷${f.band_low.value}`} />
                    {' … '}
                    <ResolvedCell resolved={f.band_high} text={`×${f.band_high.value}`} />
                  </td>
                  <td>
                    <ResolvedCell resolved={f.baseline_model} text={String(f.baseline_model.value)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
