/** Typed fetch wrapper + interfaces for every bench API resource.
 *  All requests carry the session cookie (credentials: 'include'). */

// ── Resource types ───────────────────────────────────────────────────────

export interface User {
  id: string
  email: string
  display_name: string
  role: string // admin | analyst | approver
}

/** What the router was asked to optimize for (backend: router_llm/objectives.py). */
export const ROUTING_OBJECTIVES = ['quality', 'balanced', 'token_conservation', 'eco'] as const

export type RoutingObjective = (typeof ROUTING_OBJECTIVES)[number]

export const DEFAULT_OBJECTIVE: RoutingObjective = 'balanced'

/** One-line description per objective, mirroring the backend docstrings. */
export const OBJECTIVE_DESCRIPTIONS: Record<RoutingObjective, string> = {
  quality: 'prefer the most capable candidate within the cost tier — thrift is secondary',
  balanced: 'the cheapest model that will do the job well, newer first (default)',
  token_conservation: 'prefer small-but-sufficient models with disciplined output',
  eco: 'prefer lowest estimated energy — local models first',
}

export function objectiveDescription(objective: string | undefined): string {
  return OBJECTIVE_DESCRIPTIONS[(objective ?? DEFAULT_OBJECTIVE) as RoutingObjective] ?? ''
}

/** A harness's cost ceiling, in the backend's own total order
 *  (`router_llm/objectives.py::TIER_ORDER`, validated by `api/harnesses.py`).
 *
 *  `local` ranks *below* economy rather than above premium, and that inversion is
 *  the point: local inference is already free, so a spending ceiling has nothing
 *  to protect there. Capping at `local` therefore leaves local models as the only
 *  candidates — it is a confidentiality control (no cloud provider may be chosen),
 *  not a budget one. */
export const COST_TIERS = ['local', 'economy', 'standard', 'premium'] as const

export type CostTier = (typeof COST_TIERS)[number]

export const DEFAULT_MAX_COST_TIER: CostTier = 'premium'

export const COST_TIER_DESCRIPTIONS: Record<CostTier, string> = {
  local: 'local models only — nothing leaves the machine, and no cloud provider may be routed to',
  economy: 'the cheapest cloud tier and below',
  standard: 'mid-priced models and below',
  premium: 'no ceiling — any model in the catalog (default)',
}

/** Rank of a cost tier in the backend's total order.
 *
 *  Mirrors `router_llm/objectives.py::TIER_ORDER.get(tier, 2)`: an unrecognized
 *  tier reads as `premium`, on either side of the comparison, exactly as the
 *  backend reads it. */
function costTierRank(tier: string | undefined): number {
  const index = COST_TIERS.indexOf(tier as CostTier)
  return index === -1 ? COST_TIERS.indexOf(DEFAULT_MAX_COST_TIER) : index
}

/** Whether a per-request `model_override` naming this model would be accepted.
 *
 *  This is a **mirror** of `router_llm/router.py::_assert_override_within_policy`,
 *  not a substitute for it. A per-request override — the Workbench picker and the
 *  chat composer both set one — may choose *within* the harness policy and no
 *  further: it must be on the `allowed` list when the harness has one, and at or
 *  below `max_cost_tier`. (A harness's own `mode: pinned` model is exempt from
 *  the ceiling, but that is the harness's statement about itself, not something a
 *  request can borrow, so it does not widen this.) The backend refuses anything
 *  else with `RoutingUnavailable`, which surfaces as a failed run; the picker
 *  filtering to the same set is what stops a user selecting a model that is
 *  guaranteed to fail. If the two ever disagree, the backend wins — it is the one
 *  enforcing the guarantee. */
export function overrideAllowedByPolicy(model: ModelInfo, policy?: ModelPolicy): boolean {
  if (!policy) return true
  const allowed = policy.allowed ?? []
  if (allowed.length > 0 && !allowed.includes(model.id)) return false
  return costTierRank(model.cost_tier) <= costTierRank(policy.max_cost_tier)
}

/** The track record a decision was made against, frozen at decision time.
 *  Snapshotted rather than referenced: priors are a moving aggregate, so
 *  re-deriving them later answers a different question than this run asked. */
export interface RoutingEvidence {
  version: string
  size_band: string
  priors: Record<string, RoutingModelPrior>
  /** Sorted last for a demonstrably poor record — still candidates, not banned. */
  demoted: string[]
  /** Promoted on the pessimistic reading of a good record. */
  proven: string[]
  /** Candidates with no record: untried here, not judged. */
  unrecorded: string[]
}

export interface RoutingDecision {
  router_model: string | null
  routing_prompt_version: string
  candidates: string[]
  chosen_model: string
  reasoning: string
  confidence: string | null
  objective: string // quality | balanced | token_conservation | eco
  /** The shape the fallback table keys on, and the key evidence is grouped by. */
  task_shape?: string
  max_cost_tier?: string
  /** Null when no evidence was read: learning off, no history, or an override. */
  evidence?: RoutingEvidence | null
  fallback_used: boolean
  override: string | null // "user_pin" | "run_override" | null
  latency_ms: number
  decided_at: string
}

export interface RunSummary {
  id: string
  project_id: string
  harness_id: string
  task_type: string
  status: string // queued | running | completed | failed | cancelled
  model_used: string | null
  provider_used: string | null
  routing: RoutingDecision | null
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  // Estimated, never metered. null (not 0) for runs predating eco accounting.
  energy_wh: number | null
  co2e_g: number | null
  // Read as recorded from the run's own accounting block, never recomputed at
  // today's factors. null wherever the run has no figure — a run recorded before
  // scopes/baseline existed reports null, not 0.
  scope2_g: number | null
  scope3_g: number | null
  avoided_co2e_g: number | null
  // Money against the same-token baseline, signed: negative is a surcharge.
  // Firmer footing than the carbon figure — per-token prices are exact — but the
  // counterfactual behind it is the same assumption. null on older runs.
  avoided_usd: number | null
  // Share of frontier spend avoided, one decimal place (exact, unlike the
  // carbon comparison's coarse multiple). null — never 0% — when there is no
  // baseline, the baseline itself costs nothing, or this run predates it.
  avoided_usd_pct: number | null
  // The judgment band around co2e_g. NOT a confidence interval and not a sigma;
  // `uncertainty.is_confidence_interval` is false and the wording must match.
  // null on runs recorded before the band existed.
  co2e_g_low: number | null
  co2e_g_high: number | null
  iterations: number
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface MsgToolCall {
  id: string
  name: string
  arguments: Record<string, unknown>
}

export interface Msg {
  role: string // user | assistant | tool
  content: string | null
  tool_calls: MsgToolCall[]
  tool_call_id: string | null
  meta: Record<string, unknown>
}

/** One accounted component of a run's assembled context. */
export interface ContextBlock {
  kind: string // platform_preamble | doctrine | task_instructions | output_schema | tool_specs | …
  label: string
  chars: number
  est_tokens: number
  sha256?: string
  sections?: string[]
  parts?: Record<string, number> // sub-breakdown, e.g. est tokens per tool
  note?: string
}

export interface ContextComposition {
  estimator: string // e.g. "chars/4"
  total_est_tokens: number
  total_chars: number
  by_kind: Record<string, number>
  blocks: ContextBlock[]
}

/** GHG Protocol split for one run, from the bench operator's perspective.
 *  scope1_g is always 0 and always present — reported as an explicit zero rather
 *  than omitted. `basis` states the reasoning and is rendered verbatim. */
export interface EmissionScopes {
  scope1_g: number
  scope2_g: number
  scope3_g: number
  basis: string
}

/** The same-token counterfactual against a frontier baseline model. An
 *  efficiency indicator, never an offset or a reduction claim. Every numeric
 *  field is null when no baseline could be resolved; `basis` always explains. */
export interface EmissionsBaseline {
  model: string | null
  energy_class: string | null
  energy_wh: number | null
  energy_wh_total: number | null
  co2e_g: number | null
  // Signed on purpose: a run heavier than the baseline reports a negative
  // figure. Never render its absolute value.
  avoided_co2e_g: number | null
  avoided_pct: number | null
  // Money on the same signed same-token basis. Added with the money comparison,
  // so absent on runs recorded before it.
  cost_usd?: number | null
  avoided_usd?: number | null
  avoided_usd_pct?: number | null
  /** The grid factor the counterfactual side was priced at, and its GHG Protocol
   *  basis and source rule. Recorded because a comparison whose two sides sit on
   *  different bases is not a GHG Protocol difference — such a run also carries
   *  the `baseline_crosses_grid_basis` caveat. Absent on older runs. */
  grid_co2e_g_per_kwh?: number | null
  grid_co2e_basis?: string | null
  grid_co2e_source?: string | null
  basis: string
}

/** How much a factor's value is worth believing. The backend's vocabulary,
 *  reproduced exactly — see `emissions.py::_factor`. A `placeholder` or
 *  `excluded` factor must never be presented as solidly as an `exact` one. */
export type FactorConfidence =
  | 'exact'
  | 'structural'
  | 'calibrated'
  | 'low'
  | 'placeholder'
  | 'excluded'

/** One constant that went into a run, with where it came from.
 *
 *  Arrives as a **list** rather than an object because `energy_accounting` is
 *  JSONB and Postgres does not preserve object key order — the array order is
 *  meaningful and is the order the provenance table renders in. Each record
 *  carries its own `key`.
 *
 *  `value` is a number for most factors, a two-element `[low, high]` array for
 *  the uncertainty band factor, and null for the per-token price record (whose
 *  values are per-model, not a single figure). Extras after `setting` are
 *  factor-specific and only present on the factor they belong to. */
export interface EmissionsFactor {
  key: string
  label: string
  value: number | number[] | null
  unit: string | null
  source: string
  url: string | null
  date: string | null
  confidence: FactorConfidence
  note: string
  setting: string | null
  // energy_class
  anchor_model?: string | null
  measured_anchor?: boolean
  reasoning_tier?: boolean
  // grid_intensity
  basis?: string
  overridden?: boolean
  /** Which precedence rule chose the factor: `provider:<name>` | `local_setting`
   *  | `global_default` | `run_override`. Absent on runs recorded before
   *  per-provider factors existed — read as unknown, never as `global_default`. */
  source_key?: string
  /** The rule half of `source_key` (`provider:anthropic` -> `provider`). */
  source_rule?: string
  /** The operator's own note about the factor, when they set one. */
  source_label?: string | null
  // pue
  profile?: string
  // embodied_hardware
  gpu_h100_kg?: number
  server_excluding_gpus_kg?: number
  lifetime_years?: number
  batch_size?: number
}

/** What moves if this one input is wrong. A sensitivity view — the product of
 *  these multipliers is deliberately NOT the headline band. */
export interface EmissionsUncertaintyContribution {
  key: string
  label: string
  low_multiplier: number
  high_multiplier: number
  dominant: boolean
  note: string
}

/** The band around a run's figures.
 *
 *  `is_confidence_interval` is false and stays false: this is a multiplicative
 *  judgment band matching field practice. No renderer may call it a confidence
 *  interval, a standard deviation, or a margin of error. `basis` says so at
 *  length and is rendered verbatim wherever the band is explained. */
export interface EmissionsUncertainty {
  kind: string // "judgment_band"
  is_confidence_interval: boolean // false
  band_factor_low: number
  band_factor_high: number
  co2e_g_low: number
  co2e_g_high: number
  energy_wh_low: number
  energy_wh_high: number
  energy_wh_total_low: number
  energy_wh_total_high: number
  contributions: EmissionsUncertaintyContribution[]
  basis: string
}

/** Money against the same-token baseline. The firmest figure in the block:
 *  `prices_are_exact` is true of the *prices*, not of the counterfactual. */
export interface EmissionsCost {
  usd: number
  baseline_model: string | null
  baseline_usd: number | null
  avoided_usd: number | null
  avoided_pct: number | null
  prices_are_exact: boolean
  basis: string
}

/** A named, directional bias that applies to this run. `direction` is
 *  "understates" | "overstates" | "either" — which way the figure is wrong. */
export interface EmissionsCaveat {
  key: string
  label: string
  direction: string
  applies: boolean
  note: string
}

export type TokenBucket = 'input' | 'output' | 'cache_read' | 'cache_write'
export type TokenCounts = Record<TokenBucket, number>

/** The auditable energy/carbon derivation on a run. Every field is an estimate.
 *  The keys after `basis` were added with scope accounting; the keys after
 *  `baseline` were added with the calibrated model (token weighting, money,
 *  uncertainty and provenance). All are optional because runs recorded before
 *  each addition genuinely do not carry them — and a missing field must render as
 *  an em-dash, never as 0.
 *
 *  A run that used more than one model carries a *roll-up* here: the additive
 *  quantities are summed, `models` lists what it used, and each per-model factor
 *  is null wherever the segments disagreed. Null means "these segments were
 *  accounted differently", never zero — the un-nulled per-model detail is in
 *  `RunDetail.model_timeline`. Fields that a roll-up can null are typed nullable
 *  for that reason. */
export interface EnergyAccounting {
  estimated: boolean
  model: string | null
  /** Present only on a multi-model roll-up: every model the run used, in order. */
  models?: string[]
  energy_class: string | null // S | M | L | XL | R
  energy_wh_per_mtok: number | null // per million *output-equivalent* tokens
  weighted_tokens: number
  cache_read_weight: number | null
  cache_write_weight: number | null
  energy_wh: number // compute / IT load only — excludes facility overhead
  grid_co2e_g_per_kwh: number | null
  co2e_g: number // run total; equals scope1_g + scope2_g + scope3_g
  basis: string
  pue?: number
  energy_wh_total?: number // compute x PUE
  deployment?: string // cloud | local
  embodied_g?: number
  scopes?: EmissionScopes
  baseline?: EmissionsBaseline | null
  // ── the input/output split: buckets are NOT equally expensive ──
  input_weight?: number
  output_weight?: number
  energy_wh_per_mtok_input?: number
  energy_wh_per_mtok_output?: number
  output_to_input_energy_ratio?: number
  tokens?: TokenCounts
  energy_wh_by_bucket?: TokenCounts // compute Wh per bucket; sums to energy_wh
  reasoning_tier?: boolean
  // ── factor resolution ──
  pue_profile?: string // hyperscaler_cloud | workstation | onprem_datacenter
  grid_co2e_basis?: string // location_based | market_based | unspecified
  /** Which precedence rule chose the grid factor: `provider:<name>` |
   *  `local_setting` | `global_default` | `run_override`. Undefined on runs
   *  recorded before per-provider factors existed. */
  grid_co2e_source?: string
  /** The operator's own label for that factor, when they set one. */
  grid_co2e_label?: string | null
  // ── money, uncertainty, provenance ──
  cost?: EmissionsCost
  uncertainty?: EmissionsUncertainty
  factors?: EmissionsFactor[]
  caveats?: EmissionsCaveat[]
}

/** One contiguous stretch of a run spent on one model, with its own accounting.
 *  Energy is a per-model calculation — class, PUE, grid factor and baseline all
 *  come from the model — so a run that changed model keeps them separated here
 *  rather than restating one segment's factors over the other's tokens. */
export interface ModelSegment {
  model: string
  provider: string
  from_iteration: number
  to_iteration: number
  /** "initial" | "context_exhausted" | "capability_stall" */
  reason: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  energy_wh: number
  energy_accounting: EnergyAccounting
}

/** One time a run had to shrink what it sends the model to stay inside its
 *  window. `messages` is always the complete transcript; this is the record of
 *  what the model stopped being shown. */
export interface CompactionRecord {
  kind: string // "elision" | "history_trim" | "no_op"
  iteration: number
  before_est_tokens?: number
  after_est_tokens?: number
  elided_messages?: number
  elided_tools?: string[]
  summarized?: boolean
  summarizer_model?: string | null
  dropped_history_turns?: number
  note?: string
  estimator?: string
}

/** One model call a run made *about itself* — choosing its model, or
 *  summarizing what compaction elided. Metered against the model that ran it,
 *  which is not the run's model: the router runs on BENCH_ROUTER_MODEL and the
 *  summarizer resolves its own cheap model, so energy class and grid factor are
 *  each call's own. */
export interface OverheadCall {
  kind: string // routing | compaction_summary
  model: string
  provider: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  energy_wh: number
  energy_accounting: EnergyAccounting
}

/** A run's overhead, reported beside `cost_usd`/`energy_wh` and never added
 *  into them — folding it in would leave every stored value unchanged while
 *  changing what it means. `accounting` nulls any factor the calls disagreed on. */
export interface RunOverhead {
  calls: OverheadCall[]
  total_cost_usd: number
  accounting: EnergyAccounting | null
}

export interface RunDetail extends RunSummary {
  task_input: Record<string, unknown>
  messages: Msg[]
  document_ids: string[]
  doctrine_sha: string | null
  context_composition: ContextComposition | null
  energy: EnergyAccounting | null
  /** Null for the ordinary single-model run, where `energy` is already that
   *  model's own block. When present, `energy` is a roll-up whose per-model
   *  factors are null wherever the segments disagreed. */
  model_timeline: ModelSegment[] | null
  compactions: CompactionRecord[] | null
  overhead: RunOverhead | null
}

export interface CreateRunBody {
  harness_id: string
  task_type: string
  task_input: Record<string, unknown>
  document_ids: string[]
  model_override?: string
}

export interface InputFieldSchema {
  type?: string
  enum?: string[]
  description?: string
}

export interface TaskType {
  slug: string
  display_name?: string
  shape?: string
  input_schema?: Record<string, InputFieldSchema>
  output_schema?: string
  terminal_tool?: string
  tools?: string[]
  output_contract?: string
  instructions?: string
}

export interface ModelPolicy {
  mode: 'auto' | 'pinned'
  model?: string
  allowed?: string[]
  max_cost_tier?: string
  objective?: RoutingObjective
}

export interface LoopConfig {
  max_iterations?: number
  max_output_tokens?: number
  temperature?: number
  max_cost_usd?: number
}

export interface Harness {
  id: string
  name: string
  description: string | null
  pack_id: string | null
  pack_slug: string | null
  task_profile: string
  system_prompt_extra: string | null
  model_policy: ModelPolicy
  tool_names: string[]
  loop_config: LoopConfig
  is_archived: boolean
  updated_at: string | null
}

export interface HarnessDetail extends Harness {
  assembled_system_prompt: string
  task_types?: TaskType[]
}

export interface HarnessBody {
  name: string
  description: string | null
  pack_id: string | null
  task_profile: string
  system_prompt_extra: string | null
  model_policy: ModelPolicy
  tool_names: string[]
  loop_config: LoopConfig
}

export interface BenchDocument {
  id: string
  project_id: string
  filename: string
  content_type: string
  byte_size: number
  // 'upload' (a person provided it) | 'web' (an agent fetched it). The trust
  // tier, not a detail: web documents are unverified third-party text.
  source_kind: string
  extraction_status: string // pending | done | failed
  meta: Record<string, unknown>
  text_chars: number
  created_at: string | null
}

export interface BenchDocumentDetail extends BenchDocument {
  extracted_text: string
}

export interface Dataset {
  id: string
  name: string
  columns: string[]
  row_count: number
  pack_seeded: boolean
}

export type DatasetRow = Record<string, unknown>

export interface ApprovalRecord {
  action: string // approve | reject
  approver_id: string
  note: string | null
  created_at: string | null
}

export interface RetrievedValue {
  dataset: string
  row_ref: string
  column: string
  value: string
}

export interface Provenance {
  model?: string
  doctrine_sha?: string | null
  retrieved_values?: RetrievedValue[]
  document_ids?: string[]
}

export interface Finding {
  id: string
  run_id: string
  schema_slug: string
  subject: Record<string, unknown>
  payload: Record<string, unknown>
  provenance: Provenance
  status: string // draft | approved | rejected
  created_at: string | null
}

export interface FindingDetail extends Finding {
  approvals: ApprovalRecord[]
}

export interface DataRequest {
  id: string
  run_id: string
  subject: Record<string, unknown>
  what_is_missing: string
  why_needed: string
  status: string // open | fulfilled | dismissed
  created_at: string | null
}

export interface PackMethod {
  slug: string
  display_name?: string
  description?: string
  entrypoint?: string
  params_schema?: Record<string, InputFieldSchema>
  inputs?: string[]
  timeout_seconds?: number
}

/** From GET /api/packs/methods/all — a PackMethod annotated with its pack. */
export interface PackMethodRef extends PackMethod {
  pack_slug: string
  pack_id: string
}

export interface Pack {
  id: string
  slug: string
  version: string
  display_name: string
  description: string
  frameworks: string[]
  doctrine_sha: string
  /** The integrity pin: sha256 over the pack's files, **recorded at install** and
   *  stored, not recomputed per request. It is what an operator compares against
   *  the hash the pack author published, so it answers "is this the pack I think I
   *  installed?" — not "has anything changed on disk since?" (nothing in this
   *  response is re-read from disk). Null for packs installed before integrity
   *  pinning landed; must never be rendered as if absence meant "unpinned but
   *  fine". */
  content_hash: string | null
  doctrine_files: string[]
  task_types: TaskType[]
  schemas: Record<string, unknown>
  installed_at: string | null
}

export interface PackDetail extends Pack {
  doctrine_contents: Record<string, string>
}

export interface ModelInfo {
  id: string
  provider: string // anthropic | kimi | openrouter | local
  display_name: string
  context_window: number
  input_price_per_mtok: number
  output_price_per_mtok: number
  cost_tier: string // economy | standard | premium | local
  strengths: string[]
  supports_tools: boolean
  curated: boolean
  released: string | null // YYYY-MM
  energy_class: string // S | M | L | XL — heuristic estimate
  energy_wh_per_mtok: number
  available: boolean
}

export interface ToolInfo {
  name: string
  description: string
  parameters: Record<string, unknown>
  // Registered but switchable: the web tools exist on every deployment and are
  // withheld where egress is off, so the builder can say why rather than hide them.
  available: boolean
  unavailable_reason: string | null
}

export interface ProviderStatus {
  provider: string
  configured: boolean
  source: 'env' | 'db' | null
  last4: string | null
}

/** Providers credentialed by something other than an API key.
 *
 *  The provider *list* is not duplicated here — it comes from
 *  `GET /api/settings/providers`, which the backend derives from
 *  `providers/catalog.py::PROVIDER_SPECS`, so adding a provider stays a
 *  one-place change. What the frontend still has to know is that "local" is
 *  credentialed by `BENCH_LOCAL_BASE_URL` rather than a key: it has no key to
 *  submit, an unset one is a normal state rather than a misconfiguration, and it
 *  must not appear in the write-only key form. That is a fact about the kind of
 *  credential, not a second copy of the roster. */
export const KEY_OPTIONAL_PROVIDERS = ['local']

/** Can this provider be given an API key through the settings UI? */
export function takesApiKey(provider: string): boolean {
  return !KEY_OPTIONAL_PROVIDERS.includes(provider)
}

/** One model found on the local server, as reported by the connection test. */
export interface LocalTestModel {
  id: string
  display_name: string
  supports_tools: boolean
  context_window: number // 0 = the server didn't report one (never guessed)
}

/** POST /api/settings/providers/local/test — a read-only diagnostic. The URL
 *  tested is always the server's own BENCH_LOCAL_BASE_URL; the client cannot
 *  supply one (that would make this an SSRF hole). */
export interface LocalProviderTest {
  configured: boolean
  base_url: string | null
  reachable: boolean
  error: string | null
  models: LocalTestModel[]
  counts: { models: number; tool_capable: number; no_tools: number }
}

export interface DeliverableSection {
  section: string
  status: string // draft | approved | rejected
  finding_id: string
  updated_at: string | null
}

export interface Deliverable {
  slug: string
  sections: DeliverableSection[]
  approved_count: number
  draft_count: number
  updated_at: string | null
}

export interface DeliverableExportSection {
  section: string
  finding_id: string
  status: string
  model: string | null
  doctrine_sha: string
  /** The run that drafted this section, and that run's estimated compute
   *  footprint. Null where the run predates eco accounting — never 0. Sections
   *  drafted in one run all report that run's whole figure, so these do not sum:
   *  the document-level total in `energy` is over *distinct* runs. */
  run_id?: string | null
  energy_wh?: number | null
  co2e_g?: number | null
}

/** The whole document's estimated compute footprint, summed over the distinct
 *  runs behind it. `runs` counts the runs that carried an estimate; runs without
 *  one contribute nothing and are counted separately rather than as zero. The
 *  same figure is printed inside the exported document itself. */
export interface DeliverableExportEnergy {
  estimated: boolean
  runs: number
  runs_without_estimate: number
  energy_wh: number | null
  co2e_g: number | null
  grid_co2e_g_per_kwh: number
}

export interface DeliverableExport {
  markdown: string
  html: string
  sections: DeliverableExportSection[]
  /** Absent when the deliverable has no sections at all (the empty-export shape). */
  energy?: DeliverableExportEnergy
}

export interface ChatActivity {
  tool: string
  summary: string
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  run_id: string | null
  ts: string
  // Assistant-only fields, stamped when the turn's run finishes. Same shape as
  // a run summary/detail (RunSummary/RunDetail) so a chat turn is as legible
  // as the run behind it — nullable fields are null (never 0) when the turn
  // carries no estimate or was never routed.
  activity?: ChatActivity[]
  status?: string
  model_used?: string | null
  cost_usd?: number
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
  cache_write_tokens?: number
  energy_wh?: number | null
  co2e_g?: number | null
  scope2_g?: number | null
  scope3_g?: number | null
  avoided_co2e_g?: number | null
  // Money saved and the judgment band, same nullability rule as the above.
  avoided_usd?: number | null
  // Share of frontier spend avoided, one decimal place. null — never 0% — when
  // there is no baseline, the baseline itself costs nothing, or this turn
  // predates the money comparison.
  avoided_usd_pct?: number | null
  co2e_g_low?: number | null
  co2e_g_high?: number | null
  // Full derivation and full routing decision, exactly as recorded — for the
  // expanded/click-through view (EmissionsCalc/EnergyDetail, RoutingBadge).
  energy?: EnergyAccounting | null
  routing?: RoutingDecision | null
}

export interface ConversationSummary {
  id: string
  title: string | null
  harness_id: string
  message_count: number
  updated_at: string | null
}

export interface ConversationDetail extends ConversationSummary {
  messages: ChatMessage[]
}

export interface RouterSettings {
  router_model: string
  routing_prompt_version: string
  timeout_seconds: number
}

// ── Egress ────────────────────────────────────────────────────────────────
export type EgressMode = 'off' | 'replay' | 'on'

export interface EgressClassStatus {
  mode: EgressMode
  configured: EgressMode
  runtime_override: EgressMode | null
  allow_hosts: string[]
}

export interface EgressStatus {
  master: EgressMode
  proxy: boolean
  classes: Record<string, EgressClassStatus>
  search_backend: string
  web_tools: string[]
  note: string
}

// ── Guardrail analytics ──────────────────────────────────────────────────

export interface GuardrailMethodStat {
  method_slug: string
  runs: number
  failed: number
  completed: number
  failure_rate_pct: number
}

export interface GuardrailMethodError {
  method_slug: string
  error: string
  at: string | null
}

export interface GuardrailHarnessStat {
  harness_id: string
  harness_name: string
  runs: number
  runs_with_validation_error: number
  validation_errors: number
  unrecovered_validation_errors: number
  run_error_rate_pct: number
}

/** Estimated compute energy per harness over the window.
 *
 *  Unlike the validation-pressure rows, this is a grouped SUM over **every** run
 *  in the window, not a bounded sample — `runs.energy_wh` is a plain numeric
 *  column. Only runs carrying an estimate are counted (`runs_with_energy`): a run
 *  storing NULL is not a run that drew no power. */
export interface GuardrailEnergyStat {
  harness_id: string
  harness_name: string
  runs_with_energy: number
  energy_wh: number
  /** Derived here from the *current* grid factor applied to the summed compute
   *  energy — see `GuardrailEnergyBasis`. Not the as-recorded figure the Emissions
   *  view reports, and smaller than it (no PUE, no embodied hardware, no scopes). */
  co2e_g: number
  energy_wh_per_run: number
}

/** What the energy rollup's carbon column actually is, travelling with it so the
 *  number cannot be quoted without its basis. `co2e_basis` is rendered verbatim. */
export interface GuardrailEnergyBasis {
  estimated: boolean
  grid_co2e_g_per_kwh: number
  co2e_basis: string
}

export interface GuardrailTotals {
  method_runs: number
  method_failures: number
  method_failure_rate_pct: number
  runs_scanned: number
  runs_scan_limit: number
  validation_errors: number
  unrecovered_validation_errors: number
  // Energy covers every run in the window, not the bounded transcript scan above.
  runs_with_energy: number
  energy_wh: number
  co2e_g: number
}

export interface GuardrailAnalytics {
  window_days: number | null
  project_id: string | null
  totals: GuardrailTotals
  methods: GuardrailMethodStat[]
  recent_method_errors: GuardrailMethodError[]
  harnesses: GuardrailHarnessStat[]
  energy: GuardrailEnergyStat[]
  energy_basis: GuardrailEnergyBasis
}

// ── Routing track record ─────────────────────────────────────────────────
// GET /api/analytics/routing. How each model has actually performed, grouped by
// the tuple routing groups by. Read within a group only: the response's `basis`
// says why, and the UI repeats it.

/** One model's record on one (task shape, objective) key. */
export interface RoutingModelPrior {
  model_id: string
  runs: number
  /** Sample count after time decay and off-band discounting — not `runs`. */
  effective_n: number
  /** Shrunk toward the pooled mean of this key. */
  quality_mean: number
  quality_raw: number
  /** Conservative lower bound. What a caller reads before overriding a rule. */
  quality_ci_low: number
  delivered_rate: number
  failure_rate: number
  mean_cost_usd: number
  mean_output_tokens: number
  mean_iterations: number
  mean_energy_wh: number | null
  approvals: number
  rejections: number
  error_kinds: Record<string, number>
  last_seen: string | null
}

export interface RoutingGroup {
  task_shape: string
  objective: string
  runs: number
  models: RoutingModelPrior[]
  /** Seen in the window but still under the evidence floor — named, not hidden. */
  models_below_evidence_floor: string[]
  /** Rows recorded but excluded from quality evidence: a model handed off for
   *  running out of context window was the wrong size, not a poor performer. */
  not_quality_evidence: number
}

export interface RoutingBasis {
  observational: boolean
  note: string
  half_life_days: number
  minimum_effective_samples: number
  quality_ignores_cost: string
}

export interface RoutingAnalytics {
  window_days: number | null
  project_id: string | null
  size_band: string | null
  rows_scanned: number
  rows_scan_limit: number
  score_version: string
  priors_version: string
  groups: RoutingGroup[]
  basis: RoutingBasis
}

// ── Emissions analytics ──────────────────────────────────────────────────
// GET /api/analytics/emissions. Every total is a plain sum of each run's stored
// figures, frozen at the factors in force when that run ran — nothing is
// recomputed at current settings, and runs without an estimate are excluded from
// the sums and counted separately.

/** The GHG Protocol basis a bucket's carbon was accounted under. `null` where the
 *  runs recorded none (they predate the label) — its own group, never folded in
 *  with a location-based figure it cannot be shown to share. */
export type GridBasis = string | null

/** Shared metric shape for every rollup: the window totals, the per-basis
 *  subtotals, and the by_model / by_harness rows.
 *
 *  **Carbon is nullable and that is the point.** Location-based and market-based
 *  figures may not be summed under the GHG Protocol, so a bucket spanning more
 *  than one basis reports every carbon figure — including the scope split and the
 *  baseline comparison — as `null`, sets `carbon_is_summable: false`, and explains
 *  itself in `not_summable_note`. Energy (Wh) and dollars stay populated: those
 *  sum across bases legitimately. Read the subtotals in `by_basis` instead. */
export interface EmissionsBucket {
  runs: number
  energy_wh: number // total, PUE-inclusive — always populated
  energy_wh_compute: number // IT load only — always populated
  co2e_g: number | null
  baseline_co2e_g: number | null
  avoided_co2e_g: number | null // signed
  avoided_usd: number
  baseline_usd: number
  // Computed from this bucket's summed dollars, not an average of per-run
  // percentages. null (never 0%) when the bucket has no baseline spend.
  // Unaffected by the basis rule: a dollar has no Scope 2 accounting method.
  avoided_usd_pct: number | null
  // The summed judgment band — low-with-low, high-with-high. Not an interval.
  // A run with no band contributes its central figure to both ends.
  co2e_g_low: number | null
  co2e_g_high: number | null
  // Scope figures are carbon, so they follow the same rule.
  scope1_g: number | null
  scope2_g: number | null
  scope3_g: number | null
  runs_without_scope_split: number
  /** The bases behind this bucket, in presentation order. More than one entry
   *  means its carbon was withheld. */
  grid_bases: GridBasis[]
  carbon_is_summable: boolean
  /** Why the carbon figures are null. Null when they are not. */
  not_summable_note: string | null
}

export interface EmissionsTotals extends EmissionsBucket {
  runs_with_estimate: number
  runs_without_estimate: number
  runs_without_baseline: number
  // Runs carrying a carbon figure but no money comparison / no band / no
  // recorded GHG Protocol basis. Counted, never back-filled with zeros.
  runs_without_money_comparison: number
  runs_without_uncertainty_band: number
  runs_without_grid_basis: number
  avoided_pct: number | null // signed; null across a basis-mixed window
}

/** One GHG Protocol basis present in the window. Each row IS summable — that is
 *  the whole point of separating them — so its carbon is always a figure. */
export interface EmissionsByBasis extends EmissionsBucket {
  basis: GridBasis
}

export interface EmissionsByModel extends EmissionsBucket {
  model: string
  energy_class: string | null
}

export interface EmissionsByHarness extends EmissionsBucket {
  harness_id: string
  harness_name: string
}

export interface EmissionsByDay {
  date: string // YYYY-MM-DD
  // Null on a day that mixed bases (a factor changed mid-day). Energy always
  // survives, so a mixed day still has something to plot.
  co2e_g: number | null
  avoided_co2e_g: number | null
  energy_wh: number
  grid_bases: GridBasis[]
  carbon_is_summable: boolean
}

/** One (deployment, grid factor, PUE, basis, source) combination actually present
 *  in the window — what makes `mixed_factors` inspectable rather than just
 *  flagged. */
export interface EmissionsRecordedFactor {
  deployment: string | null
  grid_co2e_g_per_kwh: number | null
  pue: number | null
  /** The GHG Protocol basis the factor was recorded under. Part of the
   *  combination key: a window mixing location-based with market-based factors is
   *  not summable at all, which is stronger than merely "mixed". */
  grid_co2e_basis: string | null
  /** Which precedence rule chose that factor (`provider:<name>`,
   *  `local_setting`, `global_default`, `run_override`), and the operator's own
   *  label for it. Both null on runs recorded before per-provider factors. */
  grid_co2e_source: string | null
  grid_co2e_label: string | null
  runs: number
}

/** One configured per-provider override, as it stands right now. Reference only,
 *  like everything in `EmissionsFactors`: a run that predates an entry was not
 *  recorded under it. */
export interface EmissionsGridFactor {
  g_per_kwh: number
  basis: string
  label: string | null
}

export interface EmissionsFactors {
  grid_co2e_g_per_kwh: number
  local_grid_co2e_g_per_kwh: number | null
  /** Per-provider grid factors currently configured, keyed by provider name. */
  grid_factors?: Record<string, EmissionsGridFactor>
  datacenter_pue: number
  local_pue: number
  baseline_model: string | null
  mixed_factors: boolean
  /** The stronger, separate flag: `mixed_factors` means "no single factor sits
   *  behind these totals"; `mixed_grid_bases` means "there is no total". */
  mixed_grid_bases?: boolean
  grid_bases?: GridBasis[]
  note: string
  recorded: EmissionsRecordedFactor[]
  // Reference-only, like the rest of this block — nothing above was computed
  // from these; each run carries the factors it was recorded under.
  grid_co2e_basis?: string
  local_grid_co2e_basis?: string
  onprem_pue?: number
  local_deployment_profile?: string
  uncertainty_band_low?: number
  uncertainty_band_high?: number
  provenance_note?: string
}

export interface EmissionsScan {
  limit: number
  rows_scanned: number
  truncated: boolean
}

export interface EmissionsAnalytics {
  window_days: number | null
  project_id: string | null
  totals: EmissionsTotals
  /** Carbon subtotals grouped by GHG Protocol basis. Where the window mixes
   *  bases, these are the only carbon figures the response contains. */
  by_basis: EmissionsByBasis[]
  by_model: EmissionsByModel[]
  by_harness: EmissionsByHarness[]
  by_day: EmissionsByDay[]
  factors: EmissionsFactors
  scan: EmissionsScan
  estimated: boolean
  /** Rendered verbatim, never paraphrased. */
  disclaimer: string
}

// ── Reference documentation ──────────────────────────────────────────────
// GET /api/docs/{slug}. Markdown read out of the repository's docs/ directory on
// every request, so the prose shown in-product is the prose in the repo — there
// is no bundled copy to go stale. The *numbers* never come from here: the
// methodology dialog renders its factor table from a run's own
// energy_accounting.factors, so a constant changing in Python cannot leave the UI
// quoting an old value out of this prose.

export interface DocPage {
  slug: string
  title: string
  summary: string
  /** Where the same file lives in the repository. */
  repo_path: string
  format: string // "markdown"
  /** False when this build does not carry docs/ — render `note`, not an error. */
  available: boolean
  markdown: string | null
  bytes: number | null
  /** Lets a reader pin exactly which revision they read. */
  sha256: string | null
  note: string | null
}

/** The one document the emissions UI links to, everywhere it shows a figure. */
export const EMISSIONS_METHODOLOGY_SLUG = 'emissions-methodology'

// ── Fetch wrapper ────────────────────────────────────────────────────────

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  form?: FormData
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const init: RequestInit = {
    method: options.method ?? 'GET',
    credentials: 'include',
  }
  if (options.form) {
    init.body = options.form
  } else if (options.body !== undefined) {
    init.body = JSON.stringify(options.body)
    init.headers = { 'Content-Type': 'application/json' }
  }
  const res = await fetch(`/api${path}`, init)
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const data: unknown = await res.json()
      if (data && typeof data === 'object' && 'detail' in data) {
        const detail = (data as { detail: unknown }).detail
        message = typeof detail === 'string' ? detail : JSON.stringify(detail)
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message)
  }
  return (await res.json()) as T
}

// ── API functions ────────────────────────────────────────────────────────

export const api = {
  // auth
  login: (email: string, password: string) =>
    request<User>('/auth/login', { method: 'POST', body: { email, password } }),
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  me: () => request<User>('/auth/me'),
  listUsers: () => request<User[]>('/auth/users'),
  createUser: (body: { email: string; display_name: string; password: string; role: string }) =>
    request<User>('/auth/users', { method: 'POST', body }),

  // runs
  createRun: (body: CreateRunBody) =>
    request<{ run_id: string }>('/runs', { method: 'POST', body }),
  listRuns: (limit = 100) => request<RunSummary[]>(`/runs?limit=${limit}`),
  getRun: (id: string) => request<RunDetail>(`/runs/${id}`),
  cancelRun: (id: string) => request<{ ok: boolean }>(`/runs/${id}/cancel`, { method: 'POST' }),

  // harnesses
  listHarnesses: () => request<Harness[]>('/harnesses'),
  getHarness: (id: string) => request<HarnessDetail>(`/harnesses/${id}`),
  createHarness: (body: HarnessBody) => request<Harness>('/harnesses', { method: 'POST', body }),
  updateHarness: (id: string, body: HarnessBody) =>
    request<Harness>(`/harnesses/${id}`, { method: 'PUT', body }),
  archiveHarness: (id: string) =>
    request<{ ok: boolean }>(`/harnesses/${id}`, { method: 'DELETE' }),

  // documents + datasets
  uploadDocument: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<BenchDocument>('/documents', { method: 'POST', form })
  },
  listDocuments: () => request<BenchDocument[]>('/documents'),
  getDocument: (id: string) => request<BenchDocumentDetail>(`/documents/${id}`),
  listDatasets: () => request<Dataset[]>('/datasets'),
  getDatasetRows: (id: string, limit = 10) =>
    request<DatasetRow[]>(`/datasets/${id}/rows?limit=${limit}`),

  // findings + approvals + data requests
  listFindings: (params: { status?: string; schema_slug?: string; run_id?: string } = {}) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.schema_slug) qs.set('schema_slug', params.schema_slug)
    if (params.run_id) qs.set('run_id', params.run_id)
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return request<Finding[]>(`/findings${suffix}`)
  },
  getFinding: (id: string) => request<FindingDetail>(`/findings/${id}`),
  decideFinding: (id: string, action: 'approve' | 'reject', note?: string) =>
    request<{ ok: boolean; status: string; approver: string }>(`/findings/${id}/approval`, {
      method: 'POST',
      body: { action, note: note || null },
    }),
  listDataRequests: (status?: string) =>
    request<DataRequest[]>(`/data-requests${status ? `?status=${status}` : ''}`),
  updateDataRequest: (id: string, status: 'open' | 'fulfilled' | 'dismissed') =>
    request<{ ok: boolean }>(`/data-requests/${id}/status`, { method: 'POST', body: { status } }),

  // packs
  listPacks: () => request<Pack[]>('/packs'),
  getPack: (id: string) => request<PackDetail>(`/packs/${id}`),
  listPackMethods: () => request<PackMethodRef[]>('/packs/methods/all'),
  validatePack: (path: string) =>
    request<{ valid: boolean; errors: string[]; pack: string | null; task_types: string[]; schemas: string[] }>(
      '/packs/validate',
      { method: 'POST', body: { path } },
    ),

  // deliverables
  listDeliverables: () => request<Deliverable[]>('/deliverables'),
  exportDeliverableJson: (slug: string, includeDraft = false) =>
    request<DeliverableExport>(
      `/deliverables/${encodeURIComponent(slug)}/export?format=json${includeDraft ? '&include_draft=true' : ''}`,
    ),

  // chat
  listConversations: () => request<ConversationSummary[]>('/chat'),
  createConversation: (harnessId?: string) =>
    request<ConversationDetail>('/chat', {
      method: 'POST',
      body: { harness_id: harnessId ?? null },
    }),
  getConversation: (id: string) => request<ConversationDetail>(`/chat/${id}`),
  sendChatMessage: (
    id: string,
    text: string,
    overrides: { model_override?: string; objective?: string } = {},
  ) =>
    request<{ run_id: string; conversation_id: string }>(`/chat/${id}/messages`, {
      method: 'POST',
      body: { text, ...overrides },
    }),

  // settings + catalog
  providerStatus: () => request<ProviderStatus[]>('/settings/providers'),
  setProviderKey: (provider: string, apiKey: string) =>
    request<{ ok: boolean }>('/settings/providers', {
      method: 'POST',
      body: { provider, api_key: apiKey },
    }),
  testLocalProvider: () =>
    request<LocalProviderTest>('/settings/providers/local/test', { method: 'POST' }),
  listModels: () => request<ModelInfo[]>('/models'),
  listTools: () => request<ToolInfo[]>('/tools'),
  routerSettings: () => request<RouterSettings>('/settings/router'),
  egressSettings: () => request<EgressStatus>('/settings/egress'),
  // Narrowing only — the API accepts a wider request and reports the mode
  // actually in force, so the UI must render the response, not the request.
  setEgress: (egressClass: string, mode: EgressMode) =>
    request<{ egress_class: string; requested: EgressMode; mode: EgressMode }>('/settings/egress', {
      method: 'POST',
      body: { egress_class: egressClass, mode },
    }),
  clearEgressOverride: (egressClass: string) =>
    request<{ egress_class: string; mode: EgressMode }>(
      `/settings/egress/${encodeURIComponent(egressClass)}`,
      { method: 'DELETE' },
    ),

  // reference docs
  doc: (slug: string) => request<DocPage>(`/docs/${encodeURIComponent(slug)}`),

  // analytics
  guardrailAnalytics: (days = 30, projectId?: string) => {
    const qs = new URLSearchParams({ days: String(days) })
    if (projectId) qs.set('project_id', projectId)
    return request<GuardrailAnalytics>(`/analytics/guardrails?${qs.toString()}`)
  },
  routingAnalytics: (days = 90, projectId?: string) => {
    const qs = new URLSearchParams({ days: String(days) })
    if (projectId) qs.set('project_id', projectId)
    return request<RoutingAnalytics>(`/analytics/routing?${qs.toString()}`)
  },
  emissionsAnalytics: (days = 30, projectId?: string) => {
    const qs = new URLSearchParams({ days: String(days) })
    if (projectId) qs.set('project_id', projectId)
    return request<EmissionsAnalytics>(`/analytics/emissions?${qs.toString()}`)
  },
}
