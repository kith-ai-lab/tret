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
 *
 *  Phase 3 additions, all opt-in and all additive to the shapes above: a
 *  region pin per provider plus an optional `provider@region` row beside the
 *  bare one; named hourly grid tables a grid entry's "Hourly table" select
 *  can reference; a toggle between a flat embodied g/run and a described
 *  hardware profile; and a "derive from evidence" checkbox on the judgment
 *  band. None of these infer anything — a region, a table, a profile and the
 *  evidence used to narrow a band are all operator statements.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, type ReactNode, useEffect, useRef, useState } from 'react'

import {
  api,
  ApiError,
  EMISSIONS_OVERRIDE_PROVIDERS,
  gateRefusalDetail,
  REGION_TOKEN_RE,
  type EmissionsBandOverride,
  type EmissionsEffectiveFactors,
  type EmissionsEmbodiedOverride,
  type EmissionsEmbodiedProfile,
  type EmissionsGridBasisValue,
  type EmissionsGridOverride,
  type EmissionsGridTableOverride,
  type EmissionsOverrides,
  type EmissionsPueLocalProfile,
  type EmissionsPueOverride,
  type EmissionsResolvedValue,
  type EmissionsShippedDefault,
  type EmissionsWaterOverride,
} from '../../api/client'
import {
  BAND_DERIVE_NOTE,
  BAND_SHORT,
  BAND_WHY,
  embodiedProfileText,
  GRID_BASIS_META,
  gridBasisLabel,
  GRID_TABLE_HEADER_HINT,
  gridTableSummaryText,
  layerMeta,
  PUE_PROFILE_LABELS,
  REGION_PIN_NOTE,
  WATER_BAND_SHORT,
  WATER_BASIS_NOTE,
  WATER_HYDRO_NOTE,
  WATER_METHODOLOGY_TRIGGER_HINT,
  TABLE_MISS_NOTE,
} from '../shared/emissions'
import { formatDateTime } from '../shared/format'
import { MethodologyLink } from '../shared/MethodologyDialog'
import { QueryError } from '../shared/MonoTable'
import { EmissionsHistory } from './EmissionsHistory'

const GRID_BASIS_OPTIONS: EmissionsGridBasisValue[] = ['location_based', 'market_based', 'unspecified']
const LOCAL_PROFILE_OPTIONS: EmissionsPueLocalProfile[] = ['workstation', 'onprem_datacenter']
const EMBODIED_MODES = ['per_run', 'profile'] as const
type EmbodiedMode = (typeof EMBODIED_MODES)[number]
const EMBODIED_GPU_MODEL = 'h100' as const
const DEFAULT_PROFILE_BATCH_SIZE = '64'
const TABLE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/
const MAX_TABLE_CSV_CHARS = 600_000
const MAX_TABLES_PER_DOCUMENT = 8
const MAX_TABLES_COMBINED_CSV_CHARS = 2_000_000
/** Shown under every entry's "Hourly table" select — explains why picking a
 *  table can change the basis field right above it (see `checkGridRow`'s
 *  basis check in `validateDraft`, and the auto-set in `GridRowFields`). */
const TABLE_BASIS_SYNC_NOTE = "Picking a table sets this entry's GHG basis to match it."
const DIURNAL_HEADER = 'hour_utc,g_per_kwh'
const SERIES_HEADER = 'timestamp_utc,g_per_kwh'

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
  /** Name of a `grid.tables` entry, or '' for "none". */
  table: string
}

interface GridTableDraft {
  name: string
  label: string
  basis: EmissionsGridBasisValue
  csv: string
  url: string
  as_of: string
}

interface PueDraft {
  cloud: string
  local_profile: EmissionsPueLocalProfile
  local: string
  label: string
}

interface EmbodiedProfileDraft {
  gpus: string
  runs_over_lifetime: string
  batch_size: string
  include_server: boolean
  label: string
}

interface EmbodiedDraft {
  mode: EmbodiedMode
  g_per_run: string
  label: string
  profile: EmbodiedProfileDraft
}

interface BandDraft {
  low: string
  high: string
  label: string
  derived: boolean
}

/** The `water` block as strings. `band_low`/`band_high` are both multipliers on
 *  the central figure (0.33 = divide by 3), unlike the carbon band's divisor. */
interface WaterDraft {
  site_wue: string
  local_site_wue: string
  grid_water: string
  country: string
  band_low: string
  band_high: string
}

interface FormDraft {
  water: WaterDraft
  gridDefault: GridRowDraft
  gridProviders: Record<string, GridRowDraft>
  /** One region text per provider — independent of whether a `provider@region`
   *  row is also open below; a region may be pinned on its own. */
  gridRegions: Record<string, string>
  /** The optional `provider@region` row beside each bare provider row. Only
   *  submitted when `regionalOpen[provider]` is true and it carries a value. */
  gridRegional: Record<string, GridRowDraft>
  regionalOpen: Record<string, boolean>
  gridTables: GridTableDraft[]
  pue: PueDraft
  embodied: EmbodiedDraft
  band: BandDraft
  baselineModel: string
}

function emptyGridRow(): GridRowDraft {
  return { g_per_kwh: '', basis: 'unspecified', label: '', url: '', as_of: '', table: '' }
}

function gridRowFromOverride(o: EmissionsGridOverride | undefined): GridRowDraft {
  if (!o) return emptyGridRow()
  return {
    g_per_kwh: String(o.g_per_kwh),
    basis: o.basis,
    label: o.label,
    url: o.url ?? '',
    as_of: o.as_of ?? '',
    table: o.table ?? '',
  }
}

function emptyGridTable(): GridTableDraft {
  return { name: '', label: '', basis: 'location_based', csv: '', url: '', as_of: '' }
}

function gridTableFromOverride(name: string, t: EmissionsGridTableOverride): GridTableDraft {
  return { name, label: t.label, basis: t.basis, csv: t.csv, url: t.url ?? '', as_of: t.as_of ?? '' }
}

function emptyEmbodiedProfile(): EmbodiedProfileDraft {
  return {
    gpus: '',
    runs_over_lifetime: '',
    batch_size: DEFAULT_PROFILE_BATCH_SIZE,
    include_server: true,
    label: '',
  }
}

function draftFromOverrides(overrides: EmissionsOverrides | Record<string, never>): FormDraft {
  const o = overrides as EmissionsOverrides
  const gridProviders: Record<string, GridRowDraft> = {}
  const gridRegions: Record<string, string> = {}
  const gridRegional: Record<string, GridRowDraft> = {}
  const regionalOpen: Record<string, boolean> = {}
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    gridProviders[provider] = gridRowFromOverride(o.grid?.providers?.[provider])
    const region = o.grid?.regions?.[provider] ?? ''
    gridRegions[provider] = region
    const regionalOverride = region ? o.grid?.providers?.[`${provider}@${region}`] : undefined
    gridRegional[provider] = gridRowFromOverride(regionalOverride)
    regionalOpen[provider] = Boolean(regionalOverride)
  }
  const gridTables: GridTableDraft[] = Object.entries(o.grid?.tables ?? {}).map(([name, t]) =>
    gridTableFromOverride(name, t),
  )
  const embodiedMode: EmbodiedMode = o.embodied?.profile ? 'profile' : 'per_run'
  return {
    gridDefault: gridRowFromOverride(o.grid?.default),
    gridProviders,
    gridRegions,
    gridRegional,
    regionalOpen,
    gridTables,
    pue: {
      cloud: o.pue?.cloud !== undefined ? String(o.pue.cloud) : '',
      local_profile: o.pue?.local_profile ?? 'workstation',
      local: o.pue?.local !== undefined ? String(o.pue.local) : '',
      label: o.pue?.label ?? '',
    },
    embodied: {
      mode: embodiedMode,
      g_per_run: o.embodied?.g_per_run !== undefined ? String(o.embodied.g_per_run) : '',
      label: o.embodied?.label ?? '',
      profile: o.embodied?.profile
        ? {
            gpus: String(o.embodied.profile.gpus),
            runs_over_lifetime: String(o.embodied.profile.runs_over_lifetime),
            batch_size:
              o.embodied.profile.batch_size !== undefined
                ? String(o.embodied.profile.batch_size)
                : DEFAULT_PROFILE_BATCH_SIZE,
            include_server: o.embodied.profile.include_server ?? true,
            label: o.embodied.profile.label ?? '',
          }
        : emptyEmbodiedProfile(),
    },
    band: {
      low: o.band?.low !== undefined ? String(o.band.low) : '',
      high: o.band?.high !== undefined ? String(o.band.high) : '',
      label: o.band?.label ?? '',
      derived: o.band?.derived ?? false,
    },
    baselineModel: o.baseline_model ?? '',
    water: {
      site_wue: o.water?.site_wue_l_per_kwh !== undefined ? String(o.water.site_wue_l_per_kwh) : '',
      local_site_wue:
        o.water?.local_site_wue_l_per_kwh !== undefined ? String(o.water.local_site_wue_l_per_kwh) : '',
      grid_water: o.water?.grid_water_l_per_kwh !== undefined ? String(o.water.grid_water_l_per_kwh) : '',
      country: o.water?.country ?? '',
      band_low: o.water?.band_low !== undefined ? String(o.water.band_low) : '',
      band_high: o.water?.band_high !== undefined ? String(o.water.band_high) : '',
    },
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

/** The two CSV shapes `grid.tables` accepts. Returns an error string naming
 *  what is wrong (mirroring the server's own parser messages where cheap), or
 *  null when the CSV looks acceptable. Not a full parse — the server's own
 *  `parse_grid_table` is the real validator; this only catches the mistakes a
 *  member would otherwise only find on save. */
function validateTableCsv(csv: string): string | null {
  const lines = csv
    .trim()
    .split(/\r?\n/)
    .filter((l) => l.trim() !== '')
  if (lines.length === 0) return 'csv is empty'
  const header = lines[0].trim()
  if (header === DIURNAL_HEADER) {
    const rows = lines.length - 1
    if (rows !== 24) return `expected exactly 24 rows for ${DIURNAL_HEADER}, got ${rows}`
    return null
  }
  if (header === SERIES_HEADER) {
    if (lines.length < 2) return `expected at least one row for ${SERIES_HEADER}`
    return null
  }
  return `header must be '${DIURNAL_HEADER}' or '${SERIES_HEADER}'`
}

/** Client-side mirror of the server's validation rules: a block that sets a
 *  number requires a non-empty label, a region/table name/CSV must be
 *  well-formed, and embodied is one thing or the other. Runs before every
 *  submit; the server still re-checks and its message wins if the two ever
 *  disagree. */
function validateDraft(draft: FormDraft): DraftError[] {
  const errors: DraftError[] = []
  const checkGridRow = (row: GridRowDraft, field: string) => {
    if (num(row.g_per_kwh) !== undefined && row.label.trim() === '') {
      errors.push({ field: `${field}.label`, message: `${field}.label is required when ${field}.g_per_kwh is set` })
    }
    // Table ref — only matters once the entry itself is set (unset entries
    // are never submitted, so a stale/mismatched pick can't reach the
    // server); mirrors `GridBlock._table_refs_valid` on the backend exactly.
    if (num(row.g_per_kwh) !== undefined && row.table) {
      const table = draft.gridTables.find((t) => t.name.trim() === row.table)
      if (!table) {
        errors.push({
          field: `${field}.table`,
          message: `${field}.table references unknown table '${row.table}'`,
        })
      } else if (table.basis !== row.basis) {
        errors.push({
          field: `${field}.table`,
          message: `${field}.table '${row.table}' has basis ${table.basis} but the entry is ${row.basis}`,
        })
      }
    }
  }
  checkGridRow(draft.gridDefault, 'grid.default')
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    checkGridRow(draft.gridProviders[provider], `grid.providers.${provider}`)
  }

  // Regions — format-checked for every provider with a region typed in,
  // whether or not a regional row is open; a bare pin with no regional entry
  // is still meaningful (it applies to a matching entry in another layer).
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    const region = draft.gridRegions[provider].trim()
    if (region && !REGION_TOKEN_RE.test(region)) {
      errors.push({ field: `grid.regions.${provider}`, message: `grid.regions: invalid region: '${region}'` })
    }
  }
  // Regional rows — only checked while open, and only once a value is set.
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    if (!draft.regionalOpen[provider]) continue
    const region = draft.gridRegions[provider].trim()
    const row = draft.gridRegional[provider]
    if (num(row.g_per_kwh) === undefined) continue
    if (!region) {
      errors.push({
        field: `grid.regions.${provider}`,
        message: `grid.regions.${provider} is required to save a regional entry for ${provider}`,
      })
    }
    checkGridRow(row, `grid.providers.${provider}@${region || '<region>'}`)
  }

  // Tables — count cap, combined size cap, name shape, uniqueness, per-table
  // size cap, and the two accepted header shapes (with the diurnal 24-row
  // rule). Mirrors `GridBlock._tables_valid` on the backend.
  if (draft.gridTables.length > MAX_TABLES_PER_DOCUMENT) {
    errors.push({
      field: 'grid.tables.count',
      message: `grid.tables: at most ${MAX_TABLES_PER_DOCUMENT} tables per document`,
    })
  }
  const combinedCsvChars = draft.gridTables.reduce((sum, t) => sum + t.csv.length, 0)
  if (combinedCsvChars > MAX_TABLES_COMBINED_CSV_CHARS) {
    errors.push({
      field: 'grid.tables.combined',
      message: `grid.tables: combined csv exceeds ${MAX_TABLES_COMBINED_CSV_CHARS} characters`,
    })
  }
  const seenNames = new Set<string>()
  draft.gridTables.forEach((t, i) => {
    const name = t.name.trim()
    const label = name || `#${i + 1}`
    if (!name || !TABLE_NAME_RE.test(name)) {
      errors.push({
        field: `grid.tables.${label}`,
        message: `grid.tables key '${name}' must match ^[a-z0-9][a-z0-9_-]{0,63}$`,
      })
    } else if (seenNames.has(name)) {
      errors.push({ field: `grid.tables.${label}`, message: `grid.tables key '${name}' is already used` })
    }
    seenNames.add(name)
    if (t.csv.length > MAX_TABLE_CSV_CHARS) {
      errors.push({
        field: `grid.tables.${label}.csv`,
        message: `grid.tables.${label}.csv exceeds ${MAX_TABLE_CSV_CHARS} characters`,
      })
    } else {
      const csvError = validateTableCsv(t.csv)
      if (csvError) errors.push({ field: `grid.tables.${label}.csv`, message: `grid.tables.${label}.csv: ${csvError}` })
    }
  })

  if ((num(draft.pue.cloud) !== undefined || num(draft.pue.local) !== undefined) && draft.pue.label.trim() === '') {
    errors.push({ field: 'pue.label', message: 'pue.label is required when pue.cloud or pue.local is set' })
  }

  // Embodied — mode-aware: either a flat g_per_run or a profile's two
  // required numbers; either the shared label or the profile's own label
  // satisfies the "label required" rule, mirroring `EmbodiedBlock` exactly.
  const profileGpusSet = draft.embodied.profile.gpus.trim() !== ''
  const profileRunsSet = draft.embodied.profile.runs_over_lifetime.trim() !== ''
  if (draft.embodied.mode === 'profile' && (profileGpusSet || profileRunsSet)) {
    if (num(draft.embodied.profile.gpus) === undefined) {
      errors.push({ field: 'embodied.profile.gpus', message: 'embodied.profile.gpus must be a number' })
    }
    const runs = num(draft.embodied.profile.runs_over_lifetime)
    if (runs === undefined || runs <= 0) {
      errors.push({
        field: 'embodied.profile.runs_over_lifetime',
        message: 'embodied.profile.runs_over_lifetime must be a positive number',
      })
    }
  }
  const embodiedHasValue =
    draft.embodied.mode === 'per_run'
      ? num(draft.embodied.g_per_run) !== undefined
      : num(draft.embodied.profile.gpus) !== undefined && num(draft.embodied.profile.runs_over_lifetime) !== undefined
  const embodiedHasLabel =
    draft.embodied.label.trim() !== '' ||
    (draft.embodied.mode === 'profile' && draft.embodied.profile.label.trim() !== '')
  if (embodiedHasValue && !embodiedHasLabel) {
    errors.push({
      field: 'embodied.label',
      message: 'embodied.label is required when embodied.g_per_run or embodied.profile is set',
    })
  }

  const bandLow = num(draft.band.low)
  const bandHigh = num(draft.band.high)
  if (bandLow !== undefined || bandHigh !== undefined) {
    if (draft.band.label.trim() === '') {
      errors.push({ field: 'band.label', message: 'band.label is required when band.low or band.high is set' })
    }
  }
  // Water: the same rules the backend enforces (WaterBlock).
  const w = draft.water
  for (const [field, text] of [
    ['water.site_wue_l_per_kwh', w.site_wue],
    ['water.local_site_wue_l_per_kwh', w.local_site_wue],
    ['water.grid_water_l_per_kwh', w.grid_water],
  ] as const) {
    if (text.trim() === '') continue
    const n = num(text)
    if (n === undefined || n < 0) {
      errors.push({ field, message: `${field} must be a non-negative number` })
    }
  }
  if (w.band_low.trim() !== '') {
    const n = num(w.band_low)
    if (n === undefined || !(n > 0 && n <= 1)) {
      errors.push({ field: 'water.band_low', message: 'water.band_low must be above 0 and at most 1 (0.33 means divide by 3)' })
    }
  }
  if (w.band_high.trim() !== '') {
    const n = num(w.band_high)
    if (n === undefined || n < 1) {
      errors.push({ field: 'water.band_high', message: 'water.band_high must be 1 or more (3 means multiply by 3)' })
    }
  }
  if (w.country.trim() !== '' && !/^[A-Za-z]{3}$/.test(w.country.trim())) {
    errors.push({ field: 'water.country', message: 'water.country must be a 3-letter ISO 3166 alpha-3 code, e.g. USA' })
  }
  return errors
}

/** The form rebuilds `grid`, `pue`, `embodied`, `band`, `water` and
 *  `baseline_model` from its inputs. Top-level keys the backend schema accepts that this form does not edit; they
 *  are carried through untouched. Anything else is dropped, because the backend
 *  rejects unknown keys and carrying one would make every save fail. */
const CARRIED_KEYS = ['version', 'energy_strategy', 'model_overrides', 'source_name', 'updated_by', 'updated_at'] as const

/** Builds the PUT body from the draft. The PUT replaces the whole document, so
 *  the body starts from the document as loaded (`base`) with only the blocks this
 *  form manages removed and rebuilt from the draft; saving can never silently
 *  delete a block the form does not show. A blank field omits its override
 *  rather than sending an explicit zero. */
function overridesFromDraft(draft: FormDraft, base: EmissionsOverrides): EmissionsOverrides {
  const baseDoc = base as Record<string, unknown>
  const carried: Record<string, unknown> = {}
  for (const key of CARRIED_KEYS) if (baseDoc[key] !== undefined) carried[key] = baseDoc[key]
  const body: EmissionsOverrides = carried as EmissionsOverrides

  const buildGridEntry = (row: GridRowDraft, v: number): EmissionsGridOverride => ({
    g_per_kwh: v,
    basis: row.basis,
    label: row.label.trim(),
    url: row.url.trim() || undefined,
    as_of: row.as_of.trim() || undefined,
    table: row.table || undefined,
  })

  const gridDefaultVal = num(draft.gridDefault.g_per_kwh)
  const providers: Record<string, EmissionsGridOverride> = {}
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    const row = draft.gridProviders[provider]
    const v = num(row.g_per_kwh)
    if (v !== undefined) providers[provider] = buildGridEntry(row, v)

    if (draft.regionalOpen[provider]) {
      const region = draft.gridRegions[provider].trim()
      const regionalRow = draft.gridRegional[provider]
      const rv = num(regionalRow.g_per_kwh)
      if (rv !== undefined && region) {
        providers[`${provider}@${region}`] = buildGridEntry(regionalRow, rv)
      }
    }
  }
  // Rows the form has no field for (another provider, or a region other than the
  // one shown) are kept as stored rather than silently dropped.
  const knownProviders = new Set<string>(EMISSIONS_OVERRIDE_PROVIDERS)
  for (const [key, entry] of Object.entries(base.grid?.providers ?? {})) {
    const [provider, region] = key.split('@')
    const shownRegion = knownProviders.has(provider) ? draft.gridRegions[provider as keyof typeof draft.gridRegions]?.trim() : undefined
    const managed = knownProviders.has(provider) && (region === undefined || (draft.regionalOpen[provider as keyof typeof draft.regionalOpen] && region === shownRegion))
    if (!managed && entry && !(key in providers)) providers[key] = entry
  }
  const regions: Record<string, string> = {}
  for (const provider of EMISSIONS_OVERRIDE_PROVIDERS) {
    const region = draft.gridRegions[provider].trim()
    if (region) regions[provider] = region
  }
  const tables: Record<string, EmissionsGridTableOverride> = {}
  for (const t of draft.gridTables) {
    const name = t.name.trim()
    if (!name) continue
    tables[name] = {
      label: t.label.trim(),
      basis: t.basis,
      csv: t.csv,
      url: t.url.trim() || undefined,
      as_of: t.as_of.trim() || undefined,
    }
  }
  if (
    gridDefaultVal !== undefined ||
    Object.keys(providers).length > 0 ||
    Object.keys(regions).length > 0 ||
    Object.keys(tables).length > 0
  ) {
    body.grid = {}
    if (gridDefaultVal !== undefined) body.grid.default = buildGridEntry(draft.gridDefault, gridDefaultVal)
    if (Object.keys(providers).length > 0) body.grid.providers = providers
    if (Object.keys(regions).length > 0) body.grid.regions = regions
    if (Object.keys(tables).length > 0) body.grid.tables = tables
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
  // Upstream PUE disclosures have no form field; keep them.
  const upstreams = (base.pue as Record<string, unknown> | undefined)?.upstreams
  if (upstreams !== undefined) {
    body.pue = { ...(body.pue ?? { label: base.pue?.label ?? '' }), upstreams } as EmissionsPueOverride
  }

  const embodiedHasValue =
    draft.embodied.mode === 'per_run'
      ? num(draft.embodied.g_per_run) !== undefined
      : num(draft.embodied.profile.gpus) !== undefined && num(draft.embodied.profile.runs_over_lifetime) !== undefined
  if (embodiedHasValue) {
    const embodied: EmissionsEmbodiedOverride = { label: draft.embodied.label.trim() }
    if (draft.embodied.mode === 'per_run') {
      embodied.g_per_run = num(draft.embodied.g_per_run)
    } else {
      const profile: EmissionsEmbodiedProfile = {
        gpus: num(draft.embodied.profile.gpus) as number,
        runs_over_lifetime: num(draft.embodied.profile.runs_over_lifetime) as number,
        gpu_model: base.embodied?.profile?.gpu_model ?? EMBODIED_GPU_MODEL,
      }
      const batchSize = num(draft.embodied.profile.batch_size)
      if (batchSize !== undefined) profile.batch_size = batchSize
      profile.include_server = draft.embodied.profile.include_server
      if (draft.embodied.profile.label.trim()) profile.label = draft.embodied.profile.label.trim()
      embodied.profile = profile
    }
    body.embodied = embodied
  }

  const bandLow = num(draft.band.low)
  const bandHigh = num(draft.band.high)
  if (bandLow !== undefined || bandHigh !== undefined || draft.band.derived) {
    const band: EmissionsBandOverride = { label: draft.band.label.trim(), derived: draft.band.derived }
    if (bandLow !== undefined) band.low = bandLow
    if (bandHigh !== undefined) band.high = bandHigh
    body.band = band
  }

  if (draft.baselineModel.trim() !== '') body.baseline_model = draft.baselineModel.trim()

  const water: EmissionsWaterOverride = {}
  const wn = (t: string) => num(t)
  if (wn(draft.water.site_wue) !== undefined) water.site_wue_l_per_kwh = wn(draft.water.site_wue)
  if (wn(draft.water.local_site_wue) !== undefined) water.local_site_wue_l_per_kwh = wn(draft.water.local_site_wue)
  if (wn(draft.water.grid_water) !== undefined) water.grid_water_l_per_kwh = wn(draft.water.grid_water)
  if (draft.water.country.trim() !== '') water.country = draft.water.country.trim().toUpperCase()
  if (wn(draft.water.band_low) !== undefined) water.band_low = wn(draft.water.band_low)
  if (wn(draft.water.band_high) !== undefined) water.band_high = wn(draft.water.band_high)
  // Upstream water disclosures have no form field; keep them, as for PUE.
  if (base.water?.upstreams !== undefined) water.upstreams = base.water.upstreams
  if (Object.keys(water).length > 0) body.water = water

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
    <div className="error-text" style={{ marginTop: 2, fontSize: 'var(--fs-xs)' }}>
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
  inputId,
  tables,
}: {
  row: GridRowDraft
  onChange: (row: GridRowDraft) => void
  disabled: boolean
  errors: DraftError[]
  field: string
  shipped: EmissionsShippedDefault | undefined
  /** Id for the gCO₂e/kWh input — the jump target the effective-factors table's
   *  "Override for <provider>" action focuses. Only the per-provider rows get
   *  one; the single default row isn't a jump target. */
  inputId?: string
  /** Named `grid.tables` entries this document has (basis included), for the
   *  "Hourly table" select — every grid entry (default, provider, regional)
   *  offers the same list plus "none". Picking one whose basis differs from
   *  this row's basis updates the row's basis to match — see the select's
   *  onChange below. */
  tables: GridTableDraft[]
}) {
  const selectTable = (name: string) => {
    if (!name) {
      onChange({ ...row, table: '' })
      return
    }
    const table = tables.find((t) => t.name.trim() === name)
    if (table && table.basis !== row.basis) {
      onChange({ ...row, table: name, basis: table.basis })
    } else {
      onChange({ ...row, table: name })
    }
  }
  return (
    <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
      <div className="field" style={{ marginBottom: 0, width: 140 }}>
        <label className="mono-label">gCO₂e/kWh</label>
        <input
          id={inputId}
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
      <div className="field" style={{ marginBottom: 0, width: 160 }}>
        <label className="mono-label">Hourly table</label>
        <select value={row.table} disabled={disabled} onChange={(e) => selectTable(e.target.value)}>
          <option value="">none</option>
          {tables.map((t) => t.name.trim()).filter((name) => name !== '').map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        {row.table && (
          <div className="fine-print" style={{ marginTop: 2 }}>
            {TABLE_BASIS_SYNC_NOTE}
          </div>
        )}
        <FieldError errors={errors} field={`${field}.table`} />
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
  const disabled = !canEdit || saveMutation.isPending
  const combinedTableCsvChars = draft.gridTables.reduce((sum, t) => sum + t.csv.length, 0)

  const submit = () => {
    const errors = validateDraft(draft)
    setClientErrors(errors)
    if (errors.length > 0) return
    saveMutation.mutate(overridesFromDraft(draft, overrides))
  }

  const clear = () => {
    if (!window.confirm('Clear every emissions override for this workspace? Runs will fall back to the managed/env/global defaults.')) return
    clearMutation.mutate()
  }

  const updateTable = (i: number, next: GridTableDraft) => {
    const oldName = draft.gridTables[i]?.name.trim()
    const newName = next.name.trim()
    const gridTables = draft.gridTables.slice()
    gridTables[i] = next
    if (!oldName || oldName === newName) {
      setDraft({ ...draft, gridTables })
      return
    }
    // Renamed — rewrite every entry that pointed at the old name so the
    // draft never carries a dangling `table` ref the server would 422 on.
    const renameRef = (row: GridRowDraft) => (row.table === oldName ? { ...row, table: newName } : row)
    setDraft({
      ...draft,
      gridTables,
      gridDefault: renameRef(draft.gridDefault),
      gridProviders: Object.fromEntries(
        EMISSIONS_OVERRIDE_PROVIDERS.map((p) => [p, renameRef(draft.gridProviders[p])]),
      ),
      gridRegional: Object.fromEntries(
        EMISSIONS_OVERRIDE_PROVIDERS.map((p) => [p, renameRef(draft.gridRegional[p])]),
      ),
    })
  }
  const addTable = () => {
    if (draft.gridTables.length >= MAX_TABLES_PER_DOCUMENT) return
    setDraft({ ...draft, gridTables: [...draft.gridTables, emptyGridTable()] })
  }
  const removeTable = (i: number) => {
    const removedName = draft.gridTables[i]?.name
    const gridTables = draft.gridTables.filter((_, idx) => idx !== i)
    // Clear any grid entry that referenced the removed table so the form
    // never submits a dangling `table` reference the server would 422 on.
    const clearRef = (row: GridRowDraft) => (row.table === removedName ? { ...row, table: '' } : row)
    setDraft({
      ...draft,
      gridTables,
      gridDefault: clearRef(draft.gridDefault),
      gridProviders: Object.fromEntries(
        EMISSIONS_OVERRIDE_PROVIDERS.map((p) => [p, clearRef(draft.gridProviders[p])]),
      ),
      gridRegional: Object.fromEntries(
        EMISSIONS_OVERRIDE_PROVIDERS.map((p) => [p, clearRef(draft.gridRegional[p])]),
      ),
    })
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

        {/* ── grid: default + regions + per-provider + tables ── */}
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Grid intensity — default
          </div>
          <GridRowFields
            row={draft.gridDefault}
            onChange={(row) => setDraft({ ...draft, gridDefault: row })}
            disabled={disabled}
            errors={clientErrors}
            field="grid.default"
            shipped={shipped.grid_default}
            tables={draft.gridTables}
          />
        </div>

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Grid intensity — regions
          </div>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            {REGION_PIN_NOTE}
          </div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
            {EMISSIONS_OVERRIDE_PROVIDERS.map((provider) => (
              <div key={provider} className="field" style={{ marginBottom: 0, width: 160 }}>
                <label className="mono-label">{PROVIDER_LABELS[provider] ?? provider}</label>
                <input
                  type="text"
                  placeholder="us-east"
                  value={draft.gridRegions[provider]}
                  disabled={disabled}
                  onChange={(e) =>
                    setDraft({ ...draft, gridRegions: { ...draft.gridRegions, [provider]: e.target.value } })
                  }
                />
                <FieldError errors={clientErrors} field={`grid.regions.${provider}`} />
              </div>
            ))}
          </div>
        </div>

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Grid intensity — per provider
          </div>
          <div className="stack" style={{ gap: 10 }}>
            {EMISSIONS_OVERRIDE_PROVIDERS.map((provider) => {
              const region = draft.gridRegions[provider].trim()
              const isOpen = draft.regionalOpen[provider]
              return (
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
                    disabled={disabled}
                    errors={clientErrors}
                    field={`grid.providers.${provider}`}
                    shipped={shipped.grid_providers?.[provider]}
                    inputId={gridProviderInputId(provider)}
                    tables={draft.gridTables}
                  />
                  {isOpen ? (
                    <div
                      style={{
                        marginTop: 8,
                        paddingLeft: 14,
                        borderLeft: '2px solid var(--border-subtle)',
                      }}
                    >
                      <div className="fine-print" style={{ marginBottom: 4 }}>
                        regional entry — key: <code>{provider}@{region || '…'}</code>
                        {!region && ' (enter a region above to enable this row)'}
                      </div>
                      <GridRowFields
                        row={draft.gridRegional[provider]}
                        onChange={(row) =>
                          setDraft({ ...draft, gridRegional: { ...draft.gridRegional, [provider]: row } })
                        }
                        disabled={disabled}
                        errors={clientErrors}
                        field={`grid.providers.${provider}@${region || '<region>'}`}
                        shipped={undefined}
                        tables={draft.gridTables}
                      />
                      {canEdit && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() =>
                            setDraft({ ...draft, regionalOpen: { ...draft.regionalOpen, [provider]: false } })
                          }
                        >
                          Remove regional row
                        </button>
                      )}
                    </div>
                  ) : (
                    canEdit && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        style={{ marginTop: 6 }}
                        onClick={() =>
                          setDraft({ ...draft, regionalOpen: { ...draft.regionalOpen, [provider]: true } })
                        }
                      >
                        + Add regional row
                      </button>
                    )
                  )}
                </div>
              )
            })}
          </div>
        </div>

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Hourly grid tables
          </div>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            Named CSV tables any grid entry's "Hourly table" select above can reference. Two shapes
            accepted: {GRID_TABLE_HEADER_HINT}. Up to {MAX_TABLES_PER_DOCUMENT} tables, combined{' '}
            {MAX_TABLES_COMBINED_CSV_CHARS.toLocaleString('en-US')} characters.
          </div>
          <FieldError errors={clientErrors} field="grid.tables.count" />
          <FieldError errors={clientErrors} field="grid.tables.combined" />
          <div className="stack" style={{ gap: 12 }}>
            {draft.gridTables.map((t, i) => (
              <div key={i} className="panel stack" style={{ gap: 8, padding: 10 }}>
                <div className="row" style={{ flexWrap: 'wrap', gap: 10, alignItems: 'flex-start' }}>
                  <div className="field" style={{ marginBottom: 0, width: 150 }}>
                    <label className="mono-label">Name (key)</label>
                    <input
                      type="text"
                      placeholder="e.g. ontario_hourly"
                      value={t.name}
                      disabled={disabled}
                      onChange={(e) => updateTable(i, { ...t, name: e.target.value })}
                    />
                    <FieldError errors={clientErrors} field={`grid.tables.${t.name.trim() || `#${i + 1}`}`} />
                  </div>
                  <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
                    <label className="mono-label">Label (source)</label>
                    <input
                      type="text"
                      value={t.label}
                      disabled={disabled}
                      onChange={(e) => updateTable(i, { ...t, label: e.target.value })}
                    />
                  </div>
                  <div className="field" style={{ marginBottom: 0, width: 150 }}>
                    <label className="mono-label">GHG basis</label>
                    <select
                      value={t.basis}
                      disabled={disabled}
                      onChange={(e) => updateTable(i, { ...t, basis: e.target.value as EmissionsGridBasisValue })}
                    >
                      {GRID_BASIS_OPTIONS.map((b) => (
                        <option key={b} value={b}>
                          {GRID_BASIS_META[b]?.label ?? b}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field" style={{ marginBottom: 0, width: 160 }}>
                    <label className="mono-label">URL (optional)</label>
                    <input
                      type="text"
                      value={t.url}
                      disabled={disabled}
                      onChange={(e) => updateTable(i, { ...t, url: e.target.value })}
                    />
                  </div>
                  <div className="field" style={{ marginBottom: 0, width: 120 }}>
                    <label className="mono-label">As of (optional)</label>
                    <input
                      type="text"
                      placeholder="YYYY-MM-DD"
                      value={t.as_of}
                      disabled={disabled}
                      onChange={(e) => updateTable(i, { ...t, as_of: e.target.value })}
                    />
                  </div>
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label className="mono-label">CSV</label>
                  <textarea
                    rows={6}
                    placeholder={GRID_TABLE_HEADER_HINT}
                    value={t.csv}
                    disabled={disabled}
                    onChange={(e) => updateTable(i, { ...t, csv: e.target.value })}
                  />
                  <div className="fine-print" style={{ marginTop: 2 }}>
                    {t.csv.length.toLocaleString('en-US')} / {MAX_TABLE_CSV_CHARS.toLocaleString('en-US')} characters
                    {' · combined '}
                    {combinedTableCsvChars.toLocaleString('en-US')} /{' '}
                    {MAX_TABLES_COMBINED_CSV_CHARS.toLocaleString('en-US')} characters
                  </div>
                  <FieldError errors={clientErrors} field={`grid.tables.${t.name.trim() || `#${i + 1}`}.csv`} />
                </div>
                {canEdit && (
                  <div>
                    <button type="button" className="btn btn-sm btn-danger" onClick={() => removeTable(i)}>
                      Remove table
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
          {canEdit && (
            <div className="row" style={{ alignItems: 'center', gap: 8, marginTop: 8 }}>
              <button
                type="button"
                className="btn btn-sm"
                disabled={draft.gridTables.length >= MAX_TABLES_PER_DOCUMENT}
                onClick={addTable}
              >
                + Add table
              </button>
              {draft.gridTables.length >= MAX_TABLES_PER_DOCUMENT && (
                <span className="fine-print" style={{ margin: 0 }}>
                  maximum of {MAX_TABLES_PER_DOCUMENT} tables reached
                </span>
              )}
            </div>
          )}
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
                id="pue-cloud"
                type="number"
                step="any"
                min={1}
                value={draft.pue.cloud}
                disabled={disabled}
                onChange={(e) => setDraft({ ...draft, pue: { ...draft.pue, cloud: e.target.value } })}
              />
              <ShippedDefaultHint shipped={shipped.pue_cloud} />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 160 }}>
              <label className="mono-label">Local profile</label>
              <select
                value={draft.pue.local_profile}
                disabled={disabled}
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
                id="pue-local"
                type="number"
                step="any"
                min={1}
                value={draft.pue.local}
                disabled={disabled}
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
                disabled={disabled}
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
          <div className="row" style={{ marginBottom: 10 }}>
            {EMBODIED_MODES.map((mode) => (
              <label key={mode} className="check-row" style={{ padding: 0 }}>
                <input
                  type="radio"
                  name="embodied-mode"
                  checked={draft.embodied.mode === mode}
                  disabled={disabled}
                  onChange={() => setDraft({ ...draft, embodied: { ...draft.embodied, mode } })}
                />
                <span>{mode === 'per_run' ? 'grams per run' : 'hardware profile'}</span>
              </label>
            ))}
          </div>
          {draft.embodied.mode === 'per_run' ? (
            <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
              <div className="field" style={{ marginBottom: 0, width: 140 }}>
                <label className="mono-label">g CO₂e / run</label>
                <input
                  id="embodied-g_per_run"
                  type="number"
                  step="any"
                  min={0}
                  value={draft.embodied.g_per_run}
                  disabled={disabled}
                  onChange={(e) =>
                    setDraft({ ...draft, embodied: { ...draft.embodied, g_per_run: e.target.value } })
                  }
                />
                <ShippedDefaultHint shipped={shipped.embodied_g_per_run} unit="g/run" />
              </div>
            </div>
          ) : (
            <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
              <div className="field" style={{ marginBottom: 0, width: 90 }}>
                <label className="mono-label">GPUs</label>
                <input
                  id="embodied-profile-gpus"
                  type="number"
                  step="1"
                  min={0}
                  value={draft.embodied.profile.gpus}
                  disabled={disabled}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      embodied: { ...draft.embodied, profile: { ...draft.embodied.profile, gpus: e.target.value } },
                    })
                  }
                />
                <FieldError errors={clientErrors} field="embodied.profile.gpus" />
              </div>
              <div className="field" style={{ marginBottom: 0, width: 150 }}>
                <label className="mono-label">Runs over lifetime</label>
                <input
                  type="number"
                  step="1"
                  min={1}
                  value={draft.embodied.profile.runs_over_lifetime}
                  disabled={disabled}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      embodied: {
                        ...draft.embodied,
                        profile: { ...draft.embodied.profile, runs_over_lifetime: e.target.value },
                      },
                    })
                  }
                />
                <FieldError errors={clientErrors} field="embodied.profile.runs_over_lifetime" />
              </div>
              <div className="field" style={{ marginBottom: 0, width: 110 }}>
                <label className="mono-label">Batch size</label>
                <input
                  type="number"
                  step="1"
                  min={1}
                  placeholder={DEFAULT_PROFILE_BATCH_SIZE}
                  value={draft.embodied.profile.batch_size}
                  disabled={disabled}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      embodied: {
                        ...draft.embodied,
                        profile: { ...draft.embodied.profile, batch_size: e.target.value },
                      },
                    })
                  }
                />
              </div>
              <div className="field" style={{ marginBottom: 0, width: 110 }}>
                <label className="mono-label">GPU model</label>
                <input type="text" value={EMBODIED_GPU_MODEL} disabled />
              </div>
              <label className="check-row" style={{ paddingTop: 22 }}>
                <input
                  type="checkbox"
                  checked={draft.embodied.profile.include_server}
                  disabled={disabled}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      embodied: {
                        ...draft.embodied,
                        profile: { ...draft.embodied.profile, include_server: e.target.checked },
                      },
                    })
                  }
                />
                <span>include server chassis</span>
              </label>
              <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
                <label className="mono-label">Profile label (optional)</label>
                <input
                  type="text"
                  placeholder="e.g. our own H100 pool"
                  value={draft.embodied.profile.label}
                  disabled={disabled}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      embodied: { ...draft.embodied, profile: { ...draft.embodied.profile, label: e.target.value } },
                    })
                  }
                />
              </div>
            </div>
          )}
          <div className="field" style={{ marginBottom: 0, marginTop: 10, maxWidth: 420 }}>
            <label className="mono-label">Label (source)</label>
            <input
              type="text"
              value={draft.embodied.label}
              disabled={disabled}
              onChange={(e) => setDraft({ ...draft, embodied: { ...draft.embodied, label: e.target.value } })}
            />
            <FieldError errors={clientErrors} field="embodied.label" />
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
          <label className="check-row" style={{ padding: 0, marginBottom: 6 }}>
            <input
              type="checkbox"
              checked={draft.band.derived}
              disabled={disabled}
              onChange={(e) => setDraft({ ...draft, band: { ...draft.band, derived: e.target.checked } })}
            />
            <span>Derive from evidence</span>
          </label>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            {BAND_DERIVE_NOTE}
          </div>
          <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
            <div className="field" style={{ marginBottom: 0, width: 110 }}>
              <label className="mono-label">Low (÷)</label>
              <input
                id="band-low"
                type="number"
                step="any"
                min={1}
                value={draft.band.low}
                disabled={disabled}
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
                disabled={disabled}
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
                disabled={disabled}
                onChange={(e) => setDraft({ ...draft, band: { ...draft.band, label: e.target.value } })}
              />
              <FieldError errors={clientErrors} field="band.label" />
            </div>
          </div>
        </div>

        {/* ── water ── */}
        <WaterSection
          draft={draft.water}
          onChange={(water) => setDraft({ ...draft, water })}
          disabled={disabled}
          errors={clientErrors}
          effective={data.effective}
        />

        {/* ── baseline model ── */}
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Baseline model
          </div>
          <div className="field" style={{ marginBottom: 0, maxWidth: 320 }}>
            <input
              id="baseline-model"
              type="text"
              placeholder={shipped.baseline_model ? String(shipped.baseline_model.value) : 'model id'}
              value={draft.baselineModel}
              disabled={disabled}
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
        <EffectiveFactorsTable effective={data.effective} canEdit={canEdit} />
      ) : (
        <div className="fine-print" style={{ marginTop: 18 }}>
          Effective factors are unavailable until the stored overrides above validate again — fix
          the offending field and save, or clear the overrides.
        </div>
      )}

      <EmissionsHistory />
    </div>
  )
}

// ── water ────────────────────────────────────────────────────────────────

const WATER_FIELDS: {
  key: keyof WaterDraft
  field: string
  label: string
  hint: string
  record?: string
  unit?: string
  type: 'number' | 'text'
}[] = [
  { key: 'site_wue', field: 'water.site_wue_l_per_kwh', label: 'Cloud site WUE', hint: 'Cooling water per kWh of IT energy at a cloud data centre.', record: 'site_wue_l_per_kwh', unit: 'L/kWh', type: 'number' },
  { key: 'local_site_wue', field: 'water.local_site_wue_l_per_kwh', label: 'Local site WUE', hint: 'Same, for runs on your own hardware. Stays 0 unless you set it (for example an evaporatively cooled room).', unit: 'L/kWh', type: 'number' },
  { key: 'grid_water', field: 'water.grid_water_l_per_kwh', label: 'Grid water factor', hint: 'Water used to generate each kWh of electricity.', record: 'grid_water_l_per_kwh', unit: 'L/kWh', type: 'number' },
  { key: 'country', field: 'water.country', label: 'Country (ISO3)', hint: 'Picks a country grid water factor, e.g. USA, DEU, IND. Blank uses the world average.', type: 'text' },
  { key: 'band_low', field: 'water.band_low', label: 'Low multiplier (0.33 = ÷3)', hint: 'Low end of the range as a multiplier on the central figure. Above 0, at most 1.', type: 'number' },
  { key: 'band_high', field: 'water.band_high', label: 'High multiplier (3 = ×3)', hint: 'High end of the range as a multiplier on the central figure. 1 or more.', type: 'number' },
]

/** Water factor inputs, with what is in force now and where each value came
 *  from. Blank falls through to the next layer, exactly as for the carbon
 *  factors. Effective values come from the cloud and local providers' `water`
 *  blocks; their records carry the layer and source. */
function WaterSection({
  draft,
  onChange,
  disabled,
  errors,
  effective,
}: {
  draft: WaterDraft
  onChange: (next: WaterDraft) => void
  disabled: boolean
  errors: DraftError[]
  effective: Record<string, EmissionsEffectiveFactors> | null
}) {
  const cloud = effective?.anthropic?.water ?? Object.values(effective ?? {}).find((e) => e.deployment === 'cloud')?.water
  const local = effective?.local?.water
  const recordLine = (w: typeof cloud, key: string) => {
    const r = w?.records.find((x) => x.key === key)
    if (!r) return null
    const layer = layerMeta(r.layer)
    const num = (v: unknown) => (typeof v === 'number' ? String(Number(v.toPrecision(3))) : String(v))
    const value = typeof r.value === 'object' && r.value ? `${num(r.value.low)} / ${num(r.value.high)}` : num(r.value)
    return (
      <span key={key} title={`${r.source}${r.setting ? ` · change it with ${r.setting}` : ''}`}>
        {value} {r.unit ?? ''} {layer && <span className={`badge ${layer.badge}`}>{layer.label}</span>}
      </span>
    )
  }
  return (
    <div>
      <div className="row" style={{ marginBottom: 6, flexWrap: 'wrap' }}>
        <div className="mono-label">Water</div>
        <span style={{ flex: 1 }} />
        <span className="fine-print">
          <MethodologyLink topic="water" label="water methodology" />
        </span>
      </div>
      <div className="fine-print" style={{ marginBottom: 6 }} title={WATER_METHODOLOGY_TRIGGER_HINT}>
        What powers each run's water estimate. Blank falls back to a managed default, an environment
        variable, a country dataset or tret's shipped default. {WATER_BASIS_NOTE} {WATER_HYDRO_NOTE}{' '}
        {WATER_BAND_SHORT}
      </div>
      <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-start', gap: 10 }}>
        {WATER_FIELDS.map((f) => (
          <div key={f.key} className="field" style={{ marginBottom: 0, width: 190 }}>
            <label className="mono-label" htmlFor={`water-${f.key}`}>
              {f.label}
            </label>
            <input
              id={`water-${f.key}`}
              type={f.type === 'number' ? 'number' : 'text'}
              step="any"
              min={f.type === 'number' ? 0 : undefined}
              maxLength={f.type === 'text' ? 3 : undefined}
              value={draft[f.key]}
              disabled={disabled}
              onChange={(e) => onChange({ ...draft, [f.key]: e.target.value })}
            />
            <div className="fine-print" style={{ marginTop: 2 }}>
              {f.hint}
            </div>
            {f.record && cloud && (
              <div className="fine-print" style={{ marginTop: 2 }}>
                in force (cloud): {recordLine(cloud, f.record)}
              </div>
            )}
            {f.key === 'local_site_wue' && local && (
              <div className="fine-print" style={{ marginTop: 2 }}>
                in force (local): {recordLine(local, 'site_wue_l_per_kwh')}
              </div>
            )}
            {f.key === 'band_low' && cloud && (
              <div className="fine-print" style={{ marginTop: 2 }}>
                in force: {recordLine(cloud, 'water_band')}
              </div>
            )}
            <FieldError errors={errors} field={f.field} />
          </div>
        ))}
      </div>
    </div>
  )
}

// ── effective factors: "what will apply to the next run" ─────────────────

/** Id for a provider's grid-override input in the form above — the jump
 *  target the "Override for <provider>" action in the effective-factors
 *  table below focuses. */
function gridProviderInputId(provider: string): string {
  return `grid-provider-${provider}-g_per_kwh`
}

/** One factor cell in the effective-factors table — matches the disclosure
 *  it can expand beneath the row it's in. "Band" covers both the low and
 *  high multiplier, which the table renders as a single cell. */
type FactorField = 'grid' | 'pue' | 'embodied' | 'band' | 'baseline'

const FACTOR_FIELD_LABELS: Record<FactorField, string> = {
  grid: 'Grid intensity',
  pue: 'PUE (power usage effectiveness)',
  embodied: 'Embodied hardware',
  band: 'Judgment band',
  baseline: 'Baseline model',
}

/** Which form input "Override for <provider>" jumps to. Mirrors the layout
 *  of the form above: PUE has one shared cloud input and one shared local
 *  input rather than one per provider, so it branches on deployment instead
 *  of provider; everything else has exactly one input regardless of which
 *  provider's cell was clicked. */
function overrideInputId(field: FactorField, provider: string, deployment: 'cloud' | 'local'): string {
  switch (field) {
    case 'grid':
      return gridProviderInputId(provider)
    case 'pue':
      return deployment === 'local' ? 'pue-local' : 'pue-cloud'
    case 'embodied':
      return 'embodied-g_per_run'
    case 'band':
      return 'band-low'
    case 'baseline':
      return 'baseline-model'
  }
}

/** Scrolls the target override input into view and focuses it. A no-op if
 *  the input isn't on the page for some reason (it always should be). */
function focusOverrideInput(id: string) {
  const el = document.getElementById(id)
  if (!(el instanceof HTMLElement)) return
  el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  if ('focus' in el) (el as HTMLInputElement).focus({ preventScroll: true })
}

function resolvedTitle(r: EmissionsResolvedValue): string {
  const parts = [`source: ${r.source}`]
  if (r.label) parts.push(`label: “${r.label}”`)
  if (r.as_of) parts.push(`as of ${r.as_of}`)
  parts.push(`change it with ${r.setting}`)
  return parts.join(' · ')
}

/** A factor's value and its layer chip — the always-visible content of both
 *  the collapsed cell and its expanded detail. The chip used to be pinned
 *  `whiteSpace: nowrap` to the value, which is what let it run off the edge
 *  of a narrow table instead of wrapping onto its own line.
 *
 *  `tags` are additional small badges beyond the layer chip — Phase 3's
 *  region pin and "hourly: <table>" marker on a grid cell, "profile" on an
 *  embodied cell, "derived" on a band cell. */
function ResolvedCell({
  resolved,
  text,
  tags,
}: {
  resolved: EmissionsResolvedValue
  text: string
  tags?: { text: string; title?: string }[]
}) {
  const layer = layerMeta(resolved.layer)
  const body = resolved.url ? (
    <a href={resolved.url} target="_blank" rel="noopener noreferrer" title={resolvedTitle(resolved)}>
      {text}
    </a>
  ) : (
    <span title={resolvedTitle(resolved)}>{text}</span>
  )
  return (
    <span style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 4 }}>
      {body}
      {layer && (
        <span className={`badge ${layer.badge}`} title={layer.what}>
          {layer.label}
        </span>
      )}
      {tags?.map((tag, i) => (
        <span key={i} className="badge badge-gray" title={tag.title}>
          {tag.text}
        </span>
      ))}
    </span>
  )
}

/** The full provenance for one resolved value — what the cell's detail row
 *  shows once it's expanded. */
function ResolvedDetail({ resolved, text }: { resolved: EmissionsResolvedValue; text: string }) {
  const layer = layerMeta(resolved.layer)
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <span className="mono-body">{text}</span>
        {layer && (
          <span className={`badge ${layer.badge}`} title={layer.what}>
            {layer.label}
          </span>
        )}
      </div>
      <div className="fine-print">source: {resolved.source}</div>
      <div className="fine-print">
        setting: <code>{resolved.setting}</code>
      </div>
      {resolved.label && <div className="fine-print">label: “{resolved.label}”</div>}
      {resolved.as_of && <div className="fine-print">as of {resolved.as_of}</div>}
      {resolved.url && (
        <div className="fine-print">
          <a href={resolved.url} target="_blank" rel="noopener noreferrer">
            {resolved.url}
          </a>
        </div>
      )}
    </div>
  )
}

/** The row that opens beneath a provider's row when one of its cells is
 *  expanded. Read-only members see the same provenance without the "Override
 *  for <provider>" action, since they have no form to jump to it in. */
function EffectiveDetailRow({
  provider,
  field,
  factors,
  canEdit,
}: {
  provider: string
  field: FactorField
  factors: EmissionsEffectiveFactors
  canEdit: boolean
}) {
  const providerLabel = PROVIDER_LABELS[provider] ?? provider
  return (
    <tr>
      <td colSpan={7} style={{ background: 'var(--bg-input)' }}>
        <div className="mono-label" style={{ marginBottom: 8 }}>
          {FACTOR_FIELD_LABELS[field]} — {providerLabel}
        </div>
        {field === 'grid' && (
          <>
            <ResolvedDetail
              resolved={factors.grid}
              text={`${factors.grid.value} gCO₂e/kWh · ${gridBasisLabel(factors.grid_basis)}`}
            />
            {factors.grid.table && (
              <div className="fine-print" style={{ marginTop: 8 }}>
                Hourly table <code>{factors.grid.table}</code>
                {factors.grid.table_summary ? `: ${gridTableSummaryText(factors.grid.table_summary)}` : ''}
                {factors.grid.temporal === 'annual_average'
                  ? ' — hourly table will apply at run time; effective factors here are computed without a run time, so this shows the annual fallback.'
                  : ''}
                {factors.grid.table_miss ? ` ${TABLE_MISS_NOTE}` : ''}
              </div>
            )}
          </>
        )}
        {field === 'pue' && (
          <ResolvedDetail
            resolved={factors.pue}
            text={`${factors.pue.value} (${PUE_PROFILE_LABELS[factors.pue_profile] ?? factors.pue_profile})`}
          />
        )}
        {field === 'embodied' && (
          <>
            <ResolvedDetail resolved={factors.embodied_g} text={`${factors.embodied_g.value} g/run`} />
            {factors.embodied_g.profile && (
              <div className="fine-print" style={{ marginTop: 8 }}>
                Hardware profile: {embodiedProfileText(factors.embodied_g.profile)}
                {factors.embodied_g.profile.label ? ` — “${factors.embodied_g.profile.label}”` : ''}
              </div>
            )}
          </>
        )}
        {field === 'band' && (
          <>
            <div className="row" style={{ gap: 28, flexWrap: 'wrap', alignItems: 'flex-start' }}>
              <div>
                <div className="fine-print" style={{ marginBottom: 4 }}>
                  Low (÷)
                </div>
                <ResolvedDetail resolved={factors.band_low} text={`÷${factors.band_low.value}`} />
              </div>
              <div>
                <div className="fine-print" style={{ marginBottom: 4 }}>
                  High (×)
                </div>
                <ResolvedDetail resolved={factors.band_high} text={`×${factors.band_high.value}`} />
              </div>
            </div>
            {(factors.band_low.derived || factors.band_high.derived) && (
              <div className="fine-print" style={{ marginTop: 8 }}>
                {BAND_DERIVE_NOTE}
              </div>
            )}
          </>
        )}
        {field === 'baseline' && (
          <ResolvedDetail resolved={factors.baseline_model} text={String(factors.baseline_model.value)} />
        )}
        {canEdit && (
          <button
            type="button"
            className="btn btn-sm"
            style={{ marginTop: 10 }}
            onClick={() => focusOverrideInput(overrideInputId(field, provider, factors.deployment))}
          >
            Override for {providerLabel}
          </button>
        )}
      </td>
    </tr>
  )
}

function EffectiveFactorsTable({
  effective,
  canEdit,
}: {
  effective: Record<string, EmissionsEffectiveFactors>
  canEdit: boolean
}) {
  const rows = Object.entries(effective)
  // Only one detail row open at a time, table-wide — expanding a second cell
  // collapses whichever one was already open, rather than stacking rows.
  const [open, setOpen] = useState<{ provider: string; field: FactorField } | null>(null)
  const lastTriggerRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(null)
      lastTriggerRef.current?.focus()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  const toggle = (provider: string, field: FactorField, trigger: HTMLButtonElement) => {
    lastTriggerRef.current = trigger
    setOpen((cur) => (cur && cur.provider === provider && cur.field === field ? null : { provider, field }))
  }

  return (
    <div style={{ marginTop: 18 }}>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        Effective factors
      </div>
      <div className="fine-print" style={{ marginBottom: 8 }}>
        What will apply to the next run, per provider — after run/harness overrides, this workspace's
        own settings above, any managed default, the environment, and finally tret's shipped default,
        in that order of precedence. Each value carries a chip naming which of those actually won —
        click a value to see the rest of its provenance.
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
              {rows.map(([provider, f]) => {
                const providerLabel = PROVIDER_LABELS[provider] ?? provider
                const isOpen = (field: FactorField) => open?.provider === provider && open.field === field
                const cellButton = (field: FactorField, content: ReactNode) => (
                  <button
                    type="button"
                    className="factor-cell-btn"
                    aria-expanded={isOpen(field)}
                    onClick={(e) => toggle(provider, field, e.currentTarget)}
                  >
                    {content}
                  </button>
                )
                const gridTags = [
                  ...(f.grid.region ? [{ text: `${provider}@${f.grid.region}`, title: 'Region pinned by the operator for this provider.' }] : []),
                  ...(f.grid.table
                    ? [
                        {
                          text: `hourly: ${f.grid.table}`,
                          title:
                            f.grid.temporal === 'hourly'
                              ? 'An hourly table applied to this value.'
                              : 'An hourly table is configured and will apply at run time.',
                        },
                      ]
                    : []),
                ]
                return (
                  <Fragment key={provider}>
                    <tr>
                      <td>{providerLabel}</td>
                      <td>{f.deployment}</td>
                      <td>
                        {cellButton(
                          'grid',
                          <ResolvedCell
                            resolved={f.grid}
                            text={`${f.grid.value} gCO₂e/kWh · ${gridBasisLabel(f.grid_basis)}`}
                            tags={gridTags.length > 0 ? gridTags : undefined}
                          />,
                        )}
                      </td>
                      <td>
                        {cellButton(
                          'pue',
                          <ResolvedCell
                            resolved={f.pue}
                            text={`${f.pue.value} (${PUE_PROFILE_LABELS[f.pue_profile] ?? f.pue_profile})`}
                          />,
                        )}
                      </td>
                      <td>
                        {cellButton(
                          'embodied',
                          <ResolvedCell
                            resolved={f.embodied_g}
                            text={`${f.embodied_g.value} g/run`}
                            tags={f.embodied_g.profile ? [{ text: 'profile', title: 'Computed from a named hardware profile.' }] : undefined}
                          />,
                        )}
                      </td>
                      <td>
                        {cellButton(
                          'band',
                          <>
                            <ResolvedCell
                              resolved={f.band_low}
                              text={`÷${f.band_low.value}`}
                              tags={f.band_low.derived ? [{ text: 'derived', title: BAND_DERIVE_NOTE }] : undefined}
                            />
                            {' … '}
                            <ResolvedCell
                              resolved={f.band_high}
                              text={`×${f.band_high.value}`}
                              tags={f.band_high.derived ? [{ text: 'derived', title: BAND_DERIVE_NOTE }] : undefined}
                            />
                          </>,
                        )}
                      </td>
                      <td>
                        {cellButton(
                          'baseline',
                          <ResolvedCell resolved={f.baseline_model} text={String(f.baseline_model.value)} />,
                        )}
                      </td>
                    </tr>
                    {open?.provider === provider && (
                      <EffectiveDetailRow provider={provider} field={open.field} factors={f} canEdit={canEdit} />
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
