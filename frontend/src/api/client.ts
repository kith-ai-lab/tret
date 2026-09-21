/** Typed fetch wrapper + interfaces for every tret API resource.
 *  All requests carry the session cookie (credentials: 'include'). */

// ── Resource types ───────────────────────────────────────────────────────

/** One workspace a user belongs to, and their role in it (backend:
 *  api/auth.py::WorkspaceSummary). */
export interface WorkspaceSummary {
  id: string
  name: string
  kind: 'personal' | 'team'
  role: string // owner | admin | approver | analyst — this user's role in this workspace
}

/** backend: api/auth.py::UserOut, from /api/auth/me, login, and workspace
 *  switch. Breaking change from the pre-tenancy shape (CHANGELOG.md): `role`
 *  now means this user's role *in the current workspace*, and the old
 *  instance-wide meaning moved to `global_role`. */
export interface User {
  id: string
  email: string
  display_name: string
  // Role in the resolved current workspace: owner | admin | approver | analyst.
  role: string
  // The instance-wide legacy role (admin | analyst | approver) — what `role`
  // meant before workspaces had their own roles. Still what gates
  // /api/auth/users* (self-host's instance user management).
  global_role: string
  // False once an admin has deactivated the account.
  active: boolean
  workspaces: WorkspaceSummary[]
  // Null when it cannot be resolved to exactly one workspace (no membership,
  // or more than one with nothing selected) — a client must then call
  // POST /api/auth/workspace before anything workspace-scoped will work.
  current_workspace_id: string | null
}

/** GET /api/auth/config — public, unauthenticated: what the login screen
 *  needs to decide which form(s) to show, before there is any session. */
export interface AuthConfig {
  auth_mode: 'password' | 'oidc' | 'both'
  oidc_configured: boolean
  oidc_login_url: string | null
}

/** One member of the current workspace (backend: api/workspaces.py::MemberOut,
 *  from GET /api/workspaces/{id}/members and the PATCH .../members/{user_id}
 *  role-change response). The backend also carries `joined_at`; unused here. */
export interface WorkspaceMember {
  user_id: string
  email: string
  display_name: string
  role: string // owner | admin | approver | analyst
}

/** One invite into the current workspace, as GET /api/workspaces/{id}/invites
 *  lists it (backend: api/workspaces.py::InviteOut). Every status the
 *  workspace has ever issued comes back — pending, accepted, and revoked
 *  alike — so a caller wanting only the open ones filters on `status`. */
export interface WorkspaceInvite {
  id: string
  email: string
  role: string
  status: string // pending | accepted | revoked
  created_at: string
  expires_at: string
}

/** What POST /api/workspaces/{id}/invites returns on top of WorkspaceInvite
 *  (backend: api/workspaces.py::InviteCreateOut) — shown once, right after
 *  creation, so the link can be copied immediately. The list endpoint
 *  deliberately never repeats `token`/`invite_url` (see workspaces.py's own
 *  comment on InviteCreateOut): a lost link means revoke-and-reinvite, not
 *  "look it up again".
 *
 *  `invite_url` is a *path* (`/invite/{token}`), not an absolute URL — prefix
 *  it with `window.location.origin` before showing or copying it. */
export interface WorkspaceInviteCreated extends WorkspaceInvite {
  invite_url: string
  token: string
  email_sent: boolean
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
  /** "low" | "medium" | "high" | null (a decision from before effort existed).
   *  Recorded on every path, including override/fallback — see
   *  `default_effort` server-side — regardless of whether the chosen model
   *  actually accepts the control. */
  effort?: string | null
  /** The upstream provider that served the router's own LLM call
   *  (`JsonCompletion.served_by` server-side), e.g. "Anthropic". Null on every
   *  path that never contacted a router: a single candidate, the deterministic
   *  fallback, or an override/pin — the same cases `router_prompt` is null for. */
  router_served_by?: string | null
  objective: string // quality | balanced | token_conservation | eco
  /** The shape the fallback table keys on, and the key evidence is grouped by. */
  task_shape?: string
  max_cost_tier?: string
  /** Null when no evidence was read: learning off, no history, or an override. */
  evidence?: RoutingEvidence | null
  /** Exactly what the router was asked, and a sha256 of it. Null wherever no
   *  router was consulted — an override, or a single permitted candidate —
   *  which is the same claim `router_model: null` makes, about the prompt.
   *  Present on the fallback path too: the router was asked and did not answer
   *  usefully, and that is when the question matters most. The hash covers the
   *  stored text, so it is verifiable from this object alone. */
  router_prompt?: string | null
  router_prompt_sha256?: string | null
  /** Whether the chosen model can hold this call. "unchecked" when the caller
   *  didn't know the prompt size yet (e.g. resolving a router model rather
   *  than sizing a run); "best_effort" when nothing in the policy fit and the
   *  largest-window candidate was preferred instead; "fit" otherwise.
   *  `exempt` names local models, which this filter never excludes (see
   *  backend router_llm/router.py `_apply_context_fit`). `basis` says which
   *  prompt size `required` was computed against — set only by the server
   *  harness, which knows about compaction; absent from every other caller. */
  context_fit?: {
    required: number
    mode: 'fit' | 'best_effort' | 'unchecked'
    excluded: string[]
    exempt?: string[]
    basis?: 'prompt_without_history' | 'full_prompt'
  } | null
  fallback_used: boolean
  override: string | null // "user_pin" | "run_override" | null
  latency_ms: number
  decided_at: string
  /** Bandit-style exploration record (`router_llm/router.py`'s
   *  `_maybe_explore`) — null on every path that never reaches the
   *  LLM-router logic (a single candidate, an override, or a pin). Otherwise
   *  always present, whether or not exploration actually fired: `explored:
   *  false` with `eligible: null` means its guardrails (objective ==
   *  "balanced", task_shape == "extraction", a nonzero rate) were not met at
   *  all; `eligible: 0` means they were met but nothing untried survived the
   *  exploration-tier/context-fit filter; a positive `eligible` with
   *  `explored: false` means the coin flip declined this time. */
  exploration?: {
    explored: boolean
    candidate: string | null
    probability: number
    eligible: number | null
    reason: string
  } | null
}

/** `POST /api/routing/preview` request (backend: `api/routing.py`). Either
 *  `harness_id` (preview a saved harness, optionally with `model_policy`
 *  overriding its own — the "preview my unsaved edits" case) or `model_policy`
 *  alone (no harness to save yet) must be given. An inline `model_policy` is
 *  validated the same way a saved one is. Permission rule, exact: with
 *  `harness_id`, a non-admin's override is held to that harness's own
 *  `max_cost_tier`/`allowed` ceiling (403 above it; a `mode: "pinned"` policy
 *  included — the pinned model's own cost_tier and catalog id are checked,
 *  not the policy's own `max_cost_tier` field, which the router ignores for
 *  a pin, and a saved `mode: "pinned"` policy's own ceiling is derived the
 *  same way rather than trusted from its own likely-unset `max_cost_tier`
 *  field); *without* `harness_id`, there is no saved ceiling to check
 *  against, so that path is admin-only outright (403 for anyone below the
 *  workspace's admin role, whatever `model_policy` says) — harness authoring
 *  is already admin-gated, and "preview an unsaved new harness" is that same
 *  workflow. All of this is server-side only; this client type does not
 *  enforce any of it.
 *
 *  `max_output_tokens`/`system_prompt_extra`/`tool_names` each default to the
 *  named harness's own saved value when omitted — send them whenever the
 *  form has unsaved edits to them, or the preview silently reverts to what is
 *  on disk. `system_prompt_extra: null` is a real override (clearing it), not
 *  "omitted" — omit the key entirely to mean "use the harness's own value".
 *  Likewise `pack_id: null` means "no pack" (only a freeform/chat task_type
 *  can run without one); omit the key to have the server resolve the saved
 *  harness's own pack for `task_type`. */
export interface RoutingPreviewBody {
  harness_id?: string
  model_policy?: ModelPolicy
  pack_id?: string | null
  task_type?: string
  n_documents?: number
  max_output_tokens?: number
  system_prompt_extra?: string | null
  // The enabled tool names this preview would carry — the server derives
  // web_tools_enabled from these the same way `api/harnesses.py::get_harness`
  // does (a pack task's own declared tools take precedence), rather than
  // trusting a client-computed boolean.
  tool_names?: string[]
  /** @deprecated superseded by `tool_names`, which the server prefers when
   *  both are sent — kept only for a caller that has already computed the
   *  boolean itself. */
  web_tools_enabled?: boolean
  /** Also routes every other objective in ROUTING_OBJECTIVES (four total,
   *  always) instead of just the policy's own — one router call each.
   *  Requires the workspace's approver role or higher (403 below it). */
  compare?: boolean
}

/** One objective's row of a `POST /api/routing/preview` response. `estimate`
 *  is a range, not a point figure — see `api/routing.py::preview_routing`'s
 *  own comment on `cost_usd_low`/`cost_usd_high` for the assumption behind
 *  each end: the low end prices the whole input as a cache *read* plus only
 *  10% of the output budget used; the high end prices the whole input
 *  uncached plus the full output budget used. */
export interface RoutingPreviewResult {
  objective: RoutingObjective
  decision: RoutingDecision
  estimate: {
    input_tokens: number
    max_output_tokens: number
    cost_usd_low: number
    cost_usd_high: number
    /** The router's own LLM call, if one was made (0 on every deterministic-
     *  fallback/override/single-candidate path — see `RoutingDecision.spend`
     *  server-side, which this is read off). */
    router_overhead_usd: number
  }
}

export interface RunSummary {
  id: string
  project_id: string
  harness_id: string
  /** Delegation lineage. All null for a run nothing delegated to. `root_run_id`
   *  is null on the root itself: a root's tree is `id == X || root_run_id == X`. */
  parent_run_id: string | null
  root_run_id: string | null
  /** "task" (a specialist pack task) or "subagent" (a brief another run's model
   *  wrote); null for a run a person, a schedule or the API started. */
  delegation_kind: 'task' | 'subagent' | null
  /** Shared by the children of one `delegate_parallel` call. */
  delegation_batch_id: string | null
  /** What this run caused other runs to spend, all the way down. Never part of
   *  `cost_usd`, which stays this run's own model spend. Optional: a backend
   *  older than this frontend does not send it. */
  delegated_cost_usd?: number
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

/** `GET /api/runs?limit=…&cursor=…`'s paged shape: keyset pagination on
 *  (created_at desc, id desc). `next_cursor` is an opaque token — pass it
 *  straight back as `cursor` for the next page — and is null once there is
 *  no next page. */
export interface RunsPage {
  items: RunSummary[]
  next_cursor: string | null
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

/** GHG Protocol split for one run, from the tret operator's perspective.
 *  scope1_g is always 0 and always present — reported as an explicit zero rather
 *  than omitted. `basis` states the reasoning and is rendered verbatim. */
export interface EmissionScopes {
  // Nullable for the same reason as `co2e_g`: scope figures are carbon, so a
  // roll-up spanning two GHG Protocol bases withholds them rather than adding
  // location-based and market-based grams together.
  scope1_g: number | null
  scope2_g: number | null
  scope3_g: number | null
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

/** `GridTable.summary()` — the shape and value range of an hourly grid table
 *  actually behind a `table`/`table_summary` field. `kind` is `"diurnal"` (24
 *  hour-of-day rows) or `"series"` (an ascending ISO-8601 timestamp series);
 *  `first_timestamp`/`last_timestamp` are only ever set for `"series"`. */
export interface EmissionsGridTableSummary {
  kind: 'diurnal' | 'series'
  label: string
  basis: EmissionsGridBasisValue
  row_count: number
  first_timestamp: string | null
  last_timestamp: string | null
  min_g_per_kwh: number | null
  max_g_per_kwh: number | null
  mean_g_per_kwh: number | null
}

/** `profile_summary()` — a named hardware profile's inputs, the resulting
 *  grams/run, and the same cited constants/caveat every embodied figure
 *  carries, whether or not it came from a profile. Overloads
 *  `EmissionsFactor.profile` (a plain string on the `pue` factor). */
export interface EmissionsEmbodiedProfileSummary {
  gpus: number
  runs_over_lifetime: number
  batch_size: number
  include_server: boolean
  gpu_model: string // "h100" — the only cited GPU model today
  label: string | null
  grams_per_run: number
  gpu_h100_kg: number
  server_excluding_gpus_kg: number
  lifetime_years: number
  source: string
  url: string | null
  caveat: string
}

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
  /** Which precedence layer chose this factor's value: `run_override` |
   *  `harness` | `workspace` | `managed` | `env` | `global_default`. Added
   *  alongside per-workspace emissions overrides — absent on any factor
   *  recorded before that existed, which renders with no layer chip rather
   *  than a guessed one (see `emissions.ts::layerMeta`). */
  layer?: string
  // pue: the deployment profile name (hyperscaler_cloud | workstation |
  // onprem_datacenter). embodied_hardware: overloaded to the profile
  // *summary* object instead — see `profile` below and
  // `factorEmbodiedProfile`/`factorPueProfile` in `shared/emissions.ts` for
  // the two ways this same key is read back out.
  profile?: string | EmissionsEmbodiedProfileSummary
  // embodied_hardware — reference constants cited on every embodied record
  // regardless of whether it came from a plain g_per_run or a profile.
  gpu_h100_kg?: number
  server_excluding_gpus_kg?: number
  lifetime_years?: number
  batch_size?: number
  // grid_intensity, Phase 3 additive — an operator-pinned region
  // (tret.services.grid_regions), and whether this value came from an
  // hourly table or the plain annual figure (tret.services.grid_tables).
  // All undefined/null on a run recorded before either feature existed.
  grid_region?: string | null
  temporal?: 'annual_average' | 'hourly' | 'interval_weighted' | null
  requested_region?: string | null
  region_resolution_status?: string | null
  table?: string | null
  table_summary?: EmissionsGridTableSummary | null
  table_miss?: boolean | null
  // uncertainty_band — whether this run's headline band was narrowed by
  // evidence rather than left at the plain configured low/high.
  derived?: boolean
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
  /** Which evidence flag narrowed this row (`energy_measured` |
   *  `pue_metered` | `grid_sourced_dated` | `embodied_profiled`) — only
   *  present on a row `adjust_contributions` actually touched, i.e. only
   *  when `band.derived` is set and this run has that evidence. */
  evidence?: string
}

/** What actually governed an evidence-derived band — present only when
 *  `EmissionsUncertainty.derivation` is present, i.e. `band.derived` was set
 *  and at least one contribution row narrowed. `rule` is `"configured"` when
 *  no row narrowed past the configured band, `"dominant_contribution"` when
 *  one did; `dominant_key` names that row's `key`, null when `rule` is
 *  `"configured"`. `configured_low`/`configured_high` are the band that would
 *  have applied without evidence — the ceiling `low`/`high` can never cross. */
export interface EmissionsUncertaintyDerivation {
  low: number
  high: number
  rule: 'configured' | 'dominant_contribution'
  dominant_key: string | null
  narrowed: boolean
  configured_low: number
  configured_high: number
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
  /** Present only when `band.derived` was set for this run — see
   *  `EmissionsUncertaintyDerivation`. Absent (not null) otherwise, same
   *  rule as every other Phase 3 addition here. */
  derivation?: EmissionsUncertaintyDerivation
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
  /** Missing on historical rows: unknown coverage, not verified node IT. */
  energy_boundary?: 'gpu' | 'node_it' | 'facility' | 'partial' | 'unknown' | 'mixed'
  energy_boundary_complete?: boolean
  energy_source?: 'estimated' | 'measured' | 'mixed'
  method_id?: string | null
  method_version?: number | null
  grid_factor_boundary?: string | null
  grid_gas_coverage?: string | null
  grid_gwp_horizon_years?: number | null
  grid_gwp_assessment_basis?: string | null
  grid_dataset_version?: string | null
  grid_observation_year?: number | null
  energy_method_shadow?: {
    method_id: string
    energy_wh?: number
    co2e_g?: number
    difference_kind?: string
  } | null
  coverage?: {
    functional_unit: string
    missing: string[]
    covered_subtotal: Record<string, number>
    complete_total: Record<string, number> | null
    components: {component_id: string; status: string; value: number | null; unit: string; reason?: string}[]
    alignment?: {status: string; conformity_claim?: string | null}
  }
  configured_pue?: number | null
  pue_applied?: boolean | null
  model: string | null
  /** Present only on a multi-model roll-up: every model the run used, in order. */
  models?: string[]
  /** Roll-up only: the GHG Protocol bases the segments were accounted under. */
  grid_bases?: (string | null)[]
  /** Roll-up only. False when the segments span more than one basis, in which
   *  case every carbon field above is null — location-based and market-based
   *  figures answer different questions and may not be added. Energy, tokens
   *  and cost are unaffected. Per-basis carbon subtotals are in `by_basis`. */
  carbon_summable?: boolean
  by_basis?: {
    grid_co2e_basis: string | null
    grid_factor_signature?: unknown[]
    co2e_g: number | null
    energy_wh: number | null
    models: (string | null)[]
  }[]
  energy_class: string | null // S | M | L | XL | R
  energy_wh_per_mtok: number | null // per million *output-equivalent* tokens
  weighted_tokens: number
  cache_read_weight: number | null
  cache_write_weight: number | null
  energy_wh: number // raw energy within energy_boundary; legacy coverage may be unknown
  grid_co2e_g_per_kwh: number | null
  /** Run total; equals scope1_g + scope2_g + scope3_g. Null on a roll-up whose
   *  segments span more than one GHG Protocol basis — see `carbon_summable`. */
  co2e_g: number | null
  basis: string
  pue?: number
  energy_wh_total?: number // energy used for carbon conversion; facility readings already include PUE
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
  energy_wh_by_bucket?: TokenCounts // modeled allocation of energy_wh; sums to energy_wh
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
  /** Phase 3, additive: an operator-pinned region for this provider
   *  (tret.services.grid_regions), or null when none applied to this run's
   *  win. Undefined on a run recorded before regions existed. */
  grid_region?: string | null
  /** "annual_average" (the plain per-provider/default figure, or an hourly
   *  table referenced but not evaluated/missed) or "hourly" (an hourly
   *  grid.tables entry had a value for this run's actual start time).
   *  Undefined on a run recorded before hourly tables existed. */
  grid_temporal?: 'annual_average' | 'hourly' | 'interval_weighted'
  grid_requested_region?: string | null
  grid_region_resolution_status?: string | null
  // ── money, uncertainty, provenance ──
  cost?: EmissionsCost
  uncertainty?: EmissionsUncertainty
  factors?: EmissionsFactor[]
  caveats?: EmissionsCaveat[]
  /** Every precedence layer that contributed a factor on this run/roll-up, in
   *  no particular order. Added alongside per-workspace emissions overrides;
   *  absent on runs recorded before that existed. */
  factor_layers?: string[]
  /** Which layer chose this run's grid factor specifically — the layer half
   *  of what `grid_co2e_source` already names the rule for. */
  grid_co2e_layer?: string
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
  /** "initial" | "context_exhausted" | "capability_stall" | "quality_signal" */
  reason: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  /** "low" | "medium" | "high" | null — null when this segment's model
   *  doesn't accept the control (`ModelInfo.supports_effort` was false), or
   *  on a segment from before effort existed. */
  effort?: string | null
  /** Every reasoning-effort change the quality trigger's Rung 1 made to this
   *  segment while it was live, oldest first — a raise updates `effort`
   *  above in place rather than starting a new segment, so this is the only
   *  place a segment's own effort history survives. `reason` is always
   *  "quality_signal" (`REASON_QUALITY` in engine/supervisor.py — the rung is
   *  only ever reached from that trigger); it names *why* the raise
   *  happened, not the `effort_raised` RunEvent type published alongside it,
   *  which names the event itself. */
  effort_history?: {
    at_iteration: number
    from_effort: string | null
    to_effort: string
    reason: string
  }[]
  /** The upstream provider OpenRouter actually routed this segment's calls to
   *  (e.g. "Anthropic", "Together"), the constant "anthropic" for
   *  AnthropicProvider, or null — Kimi and other OpenAI-compatible servers
   *  report nothing, and so does a segment from before this field existed. */
  served_by?: string | null
  /** How many of this segment's turns came back with no cache read for a
   *  reason the engine itself caused (the segment's first turn, a compaction
   *  pass that changed the wire, or a top-level effort raise on Anthropic)
   *  versus one with no such explanation. Both are real, paid-for rebuilds —
   *  the distinction is only whether tret can account for why the prefix
   *  changed. Counted only once caching has shown itself live on this segment
   *  (a write, or an earlier read back above 0) — so always 0/0 for a segment
   *  that never shows that evidence, whether because its provider reports no
   *  cache figure at all (a local deployment, or Kimi's own native API — an
   *  OpenRouter-hosted Kimi endpoint is not excluded by name, only by the
   *  same live-activity test any OpenRouter upstream is held to), because
   *  every prompt so far has been below its cacheable minimum, or because the
   *  segment predates this ledger. */
  cache_rebuilds_expected?: number
  cache_misses_unexpected?: number
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
 *  which is not the run's model: the router runs on TRET_ROUTER_MODEL and the
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

/** Totals over a run's whole delegation tree (root plus every descendant),
 *  computed on read. The same figures whichever run of the tree is asked. */
export interface RunTree {
  root_run_id: string
  run_count: number
  cost_usd: number
  reported_cost_usd: number | null
  energy_wh: number | null
}

export interface RunDetail extends RunSummary {
  /** Null for a run that was never delegated to and delegated nothing. */
  tree: RunTree | null
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
  // Doctrine selectors this task narrows to (backend: packs/schema.py::TaskType.doctrine)
  // — file paths from the pack's own `doctrine:` list, optionally `#`-narrowed to
  // one heading. Empty means "every doctrine file", so existing packs are unaffected.
  doctrine?: string[]
  // Only set on the entries in `HarnessDetail.task_types` — that list is the
  // union across every pack a harness links, so each entry names which pack
  // it came from. Absent everywhere else (a pack's own `task_types`, the
  // registry manifest, the pack builder draft).
  pack_slug?: string
  pack_id?: string
}

export const COMPACTION_MODES = ['auto', 'off'] as const
export const ESCALATION_MODES = ['off', 'on_stall', 'on_quality'] as const
export type CompactionMode = (typeof COMPACTION_MODES)[number]
export type EscalationMode = (typeof ESCALATION_MODES)[number]

/** What a harness lets tret do about what it learns.
 *
 *  Every field is optional and every default is ON — an omitted block means
 *  "all of it", which is what `tret/adaptive.py::adaptive_of` returns for a
 *  policy that has never been edited. Mirrors that dataclass field for field;
 *  the backend refuses unknown keys rather than ignoring them, so a name that
 *  drifts here fails loudly at save rather than silently doing nothing. */
export interface AdaptivePolicy {
  /** Whether recorded outcomes steer routing at all. */
  learn_from_outcomes?: boolean
  /** Share of the model's context window a run may fill before the engine
   *  intervenes. Backend range 0.3–0.95, default 0.8. */
  context_headroom?: number
  compaction?: CompactionMode
  escalation?: EscalationMode
  /** How many times one run may change model. Backend range 0–3, default 1. */
  max_switches?: number
  /** Probability that a `balanced`/`extraction` decision with an untried
   *  candidate picks it directly instead of asking the router — see
   *  `tret/router_llm/router.py::ModelRouter._maybe_explore`. Backend range
   *  0.0–0.2, default 0.05. `0` turns exploration off outright. */
  exploration?: number
  /** The cost ceiling exploration itself will gamble on, independent of (and
   *  never wider than) this policy's own `max_cost_tier`. Default `economy`. */
  exploration_max_cost_tier?: CostTier
}

export const ADAPTIVE_DEFAULTS: Required<AdaptivePolicy> = {
  learn_from_outcomes: true,
  context_headroom: 0.8,
  compaction: 'auto',
  escalation: 'on_quality',
  max_switches: 1,
  exploration: 0.05,
  exploration_max_cost_tier: 'economy',
}

export const ADAPTIVE_LIMITS = {
  context_headroom: { min: 0.3, max: 0.95 },
  max_switches: { min: 0, max: 3 },
  exploration: { min: 0, max: 0.2 },
} as const

export interface ModelPolicy {
  mode: 'auto' | 'pinned'
  model?: string
  allowed?: string[]
  max_cost_tier?: string
  objective?: RoutingObjective
  adaptive?: AdaptivePolicy
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
  // Legacy single-pack fields — still populated (primary pack, i.e. the first
  // of `pack_ids`, or null for a generic harness) for callers that have not
  // moved to the ordered lists, but no longer written to by this frontend.
  pack_id?: string | null
  pack_slug?: string | null
  // Every pack this harness links, in link order — the first is primary.
  // Same order and length; `pack_slugs[i]` names `pack_ids[i]`.
  pack_ids: string[]
  pack_slugs: string[]
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
  // The union of task types across every linked pack — each entry names its
  // own `pack_slug`/`pack_id` (see `TaskType`) since more than one pack can
  // contribute now.
  task_types?: TaskType[]
}

export interface HarnessBody {
  name: string
  description: string | null
  /** @deprecated superseded by `pack_ids` — kept optional only so a body built
   *  against the older single-pack shape still type-checks; the backend still
   *  accepts it, but this frontend always sends `pack_ids` instead. */
  pack_id?: string | null
  // Ordered list of linked packs; first is primary. Empty for a generic
  // harness. Replaces `pack_id` as of the multi-pack harness API.
  pack_ids: string[]
  task_profile: string
  system_prompt_extra: string | null
  model_policy: ModelPolicy
  tool_names: string[]
  loop_config: LoopConfig
}

export interface TretDocument {
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

export interface TretDocumentDetail extends TretDocument {
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

/** `Finding.payload` for `schema_slug: "connected_write"` — a proposed
 *  write-back to an m365 output folder, awaiting approval. `Finding.subject`
 *  on the same finding is `{ target: string; filename: string }`. `upload`
 *  is null until a decision is made (approving triggers the write; rejecting
 *  never populates it); once populated it never disappears, even on retry —
 *  a retry replaces it with a fresh result. */
export interface ConnectedWriteUpload {
  status: 'uploaded' | 'failed'
  item_id: string | null
  web_url: string | null
  uploaded_at: string | null
  error: string | null
  approver_id: string | null
}

export type ConnectedWriteSource =
  | { kind: 'inline'; content: string }
  | { kind: 'deliverable'; slug: string; format: string }

export interface ConnectedWritePayload {
  target_slug: string
  target_label: string
  target_path: string
  filename: string
  content_type: string
  source: ConnectedWriteSource
  // Both null for a deliverable-sourced proposal — its bytes are rendered
  // fresh at approval time, not fixed when the finding was proposed, so
  // there is nothing yet to size or hash. Only an inline-content proposal
  // carries real values here.
  size: number | null
  content_sha256: string | null
  upload: ConnectedWriteUpload | null
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

/** One `harnesses:` entry in a pack manifest — installing the pack turns each
 *  of these into a real, editable `Harness` row in the workspace (same shape
 *  `POST /api/harnesses` would create by hand), not a read-only template.
 *  Verified against packs/schema.py — `tools` is required, everything else
 *  optional, `suggested_cost_tier` is free text from `COST_TIERS` below rather
 *  than a `Harness.model_policy` object, since a preset only *suggests* a
 *  ceiling and never pins a model. `task_types` holds at most one slug —
 *  `Harness.task_profile` is single-valued, so `loader.validate_pack` rejects
 *  more than one entry; empty installs as `task_profile="freeform"`. */
export interface HarnessPreset {
  name: string
  description?: string
  task_types?: string[]
  tools: string[]
  suggested_cost_tier?: string
}

export interface Pack {
  id: string
  slug: string
  version: string
  display_name: string
  description: string
  // Provenance/marketplace metadata (Plan Phase 0) — optional, free-text, never
  // read by the engine at run time. Absent on a pack installed before this
  // landed, same as `content_hash` below.
  author: string | null
  license: string | null
  homepage: string | null
  tags: string[]
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
  // Verified against packs/schema.py, defaulting to [] like every other
  // manifest list here. Absent (older backend) reads identically to empty
  // everywhere this is used.
  harnesses?: HarnessPreset[]
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
 *  credentialed by `TRET_LOCAL_BASE_URL` rather than a key: it has no key to
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
 *  tested is always the server's own TRET_LOCAL_BASE_URL; the client cannot
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

/** POST /api/deliverables/{slug}/publish's body — assembles the deliverable
 *  the same way `exportDeliverableJson`/the export endpoints do (`format`)
 *  and writes the result into an m365 write-back target (`target_slug`,
 *  matching a `WriteTarget.slug`) as `filename`. Unlike the export
 *  endpoints, there is no `include_draft`: this route ships something, so
 *  it is always approved-only content. */
export interface PublishDeliverableBody {
  target_slug: string
  filename: string
  format: 'markdown' | 'html' | 'pdf'
}

export interface PublishDeliverableResult {
  web_url: string
  item_id: string
  name: string
  size: number
  target_slug: string
  path: string
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
  // The prose grounding check's verdict on this reply (backend
  // engine/grounding.py) — null for anything the check does not apply to
  // (a divergence/verdict task, or a run predating the check).
  grounding?: {
    checked: boolean
    status: 'clean' | 'repaired' | 'unresolved'
    attempts: number
    unsupported: string[]
    first_unsupported?: string[]
  } | null
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

/** One host's research-class traffic over the window — top 50, sorted by
 *  the backend by total calls (allowed + denied). */
export interface GuardrailEgressHost {
  host: string
  allowed: number
  denied: number
  bytes: number
}

/** Research-class egress over the window, plus the network-access status this
 *  deployment is currently running under (reuses the `EgressMode`/
 *  `EgressClassStatus` shapes the settings view already defines). `classes` is
 *  keyed by whatever egress classes the backend reports — never assume a fixed
 *  set of names. `scope` is the backend's own wording on what this audit does
 *  and does not cover, and is meant to travel with the numbers verbatim. */
export interface GuardrailEgressStats {
  allowed: number
  denied: number
  bytes: number
  hosts: GuardrailEgressHost[]
  denials_by_reason: Record<string, number>
  master: EgressMode
  proxy: boolean
  classes: Record<string, EgressClassStatus>
  scope: string
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
  egress: GuardrailEgressStats
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
  /** Per upstream endpoint (an OpenRouter provider slug), only present when at
   *  least two distinct endpoints have served this model — a bad quantized
   *  endpoint can otherwise hide behind, or be hidden by, the model's average. */
  endpoints?: Record<
    string,
    {
      runs: number
      effective_n: number
      quality_mean: number
      quality_ci_low: number
      delivered_rate: number
    }
  >
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

// ── Routing history ──────────────────────────────────────────────────────
// GET /api/analytics/routing/history. The standings view answers "which model
// is best now"; this answers "did the router change its mind, and when". Same
// table, no extra recording.

export interface RoutingHistoryModel {
  /** Runs where the router PICKED this model (the run's first segment). */
  picked: number
  share: number
  /** Averages every scored segment, including the abandoned half of a switch. */
  mean_quality: number | null
  scored_segments: number
}

export interface RoutingHistoryBucket {
  start: string
  runs: number
  top_pick: string | null
  switched_runs: number
  switch_rate: number
  models: Record<string, RoutingHistoryModel>
}

export interface TopPickChange {
  at: string
  from_model: string
  to_model: string
}

export interface RoutingHistoryGroup {
  task_shape: string
  objective: string
  runs: number
  /** Every model in the series, so colours stay stable across buckets. */
  model_ids: string[]
  buckets: RoutingHistoryBucket[]
  top_pick_changes: TopPickChange[]
}

export interface RoutingHistory {
  window_days: number | null
  bucket_days: number
  rows_scanned: number
  rows_scan_limit: number
  score_version: string
  groups: RoutingHistoryGroup[]
  basis: { observational: boolean; share_counts: string; quality_counts: string }
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
  runs_without_carbon_total?: number
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

/** One GHG Protocol basis present in the window. Separating by basis is what
 *  makes summing carbon meaningful at all, but a row's carbon can still be
 *  withheld (`carbon_is_summable: false`) when its runs were priced under
 *  different grid factors within that one basis — see `not_summable_note`. */
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

// ── Workspace emissions factor overrides ─────────────────────────────────
// Per-workspace overrides for the grid/PUE/embodied/band/baseline factors that
// otherwise come from env vars and tret's own shipped defaults — Settings →
// Emissions factors (`EmissionsFactorsPanel.tsx`) and the what-if scenario
// drawer on the Emissions page (`EmissionsScenario.tsx`). GET is open to any
// workspace member (read-only); PUT/DELETE require admin/owner and 403 for
// anyone else. Every top-level key of `EmissionsOverrides` is optional, and a
// block that sets a number requires a non-empty `label` — the server 422s
// naming the field on violation, and the client validates the same rule before
// submitting so the round trip is not the first place the error shows up.

export type EmissionsGridBasisValue = 'location_based' | 'market_based' | 'unspecified'

/** One grid factor an operator or workspace configured, with its provenance.
 *  `label` is required by the server whenever `g_per_kwh` is set. `table`
 *  names an entry in this same document's `grid.tables` whose hourly value
 *  stands in for `g_per_kwh` at a run's actual start time — `g_per_kwh`
 *  remains required and is the fallback when the table has no value for
 *  that hour (a "table miss"). */
export interface EmissionsGridOverride {
  g_per_kwh: number
  basis: EmissionsGridBasisValue
  label: string
  url?: string | null
  as_of?: string | null
  table?: string | null
}

/** One named entry in `grid.tables`: an operator-pasted CSV of grid carbon
 *  intensity, either a 24-row diurnal profile (`hour_utc,g_per_kwh`) or an
 *  ascending ISO-8601 hourly series (`timestamp_utc,g_per_kwh`). `name` (the
 *  map key in `grid.tables`) must match `^[a-z0-9][a-z0-9_-]{0,63}$`; `csv`
 *  is capped at 600,000 characters — both enforced server-side, and cheaply
 *  mirrored client-side before submitting. */
export interface EmissionsGridTableOverride {
  label: string
  basis: EmissionsGridBasisValue
  csv: string
  url?: string | null
  as_of?: string | null
}

/** The provider roster the settings form offers rows for. Not exhaustive of
 *  every provider tret can route to — just the ones with their own grid
 *  factor slot in the override schema. */
export const EMISSIONS_OVERRIDE_PROVIDERS = ['local', 'anthropic', 'kimi', 'openrouter'] as const
export type EmissionsOverrideProvider = (typeof EMISSIONS_OVERRIDE_PROVIDERS)[number]

/** A region token, as `tret.services.grid_regions` validates it:
 *  lowercase-start, alphanumeric plus `-`. Declared by the operator, never
 *  inferred — see `GRID_NO_INFERENCE_NOTE`. */
export const REGION_TOKEN_RE = /^[a-z0-9][a-z0-9-]*$/

/** A `grid.providers` key: a bare provider or a provider pinned to a region
 *  (`"anthropic@us-east"`). */
export const PROVIDER_KEY_RE = /^[a-z][a-z0-9_-]*(@[a-z0-9][a-z0-9-]*)?$/

export type EmissionsPueLocalProfile = 'workstation' | 'onprem_datacenter'

/** `label` required whenever `cloud` or `local` is set. */
export interface EmissionsPueOverride {
  cloud?: number
  local_profile?: EmissionsPueLocalProfile
  local?: number
  label: string
}

/** A named hardware setup `embodied.profile` describes instead of a flat
 *  `g_per_run` figure — the same fields
 *  `tret.services.embodied_profiles.EmbodiedProfile` validates.
 *  `batch_size` defaults to 64 and `include_server` to true server-side when
 *  omitted; `gpu_model` is fixed to `"h100"`, the only GPU with a cited
 *  embodied figure. */
export interface EmissionsEmbodiedProfile {
  gpus: number
  runs_over_lifetime: number
  batch_size?: number
  include_server?: boolean
  gpu_model?: 'h100'
  label?: string
}

/** `g_per_run` and `profile` are mutually exclusive (the server 422s
 *  "embodied: set g_per_run or profile, not both"); `label` is required
 *  whenever either is set. */
export interface EmissionsEmbodiedOverride {
  g_per_run?: number
  profile?: EmissionsEmbodiedProfile
  label: string
}

/** The judgment band an operator configures — never a confidence interval; see
 *  `emissions.ts::BAND_SHORT`. `label` required whenever `low`/`high` are set.
 *  `derived`, when true, narrows this run's headline band toward whatever
 *  evidence the run actually has (a metered PUE, a sourced/dated grid
 *  factor, measured energy, a hardware profile) — never past the configured
 *  `low`/`high`, only ever at or inside them. */
export interface EmissionsBandOverride {
  low?: number
  high?: number
  label: string
  derived?: boolean
}

/** The full override document, one per workspace. GET/PUT/DELETE
 *  `/api/workspace/settings/emissions` all exchange this shape (empty object
 *  when the workspace has cleared or never set any overrides).
 *
 *  `grid.regions` pins a bare provider to a region for lookup purposes
 *  (`{"anthropic": "us-east"}`) — declared by the operator, never inferred.
 *  Pinning a provider makes a `provider@region` entry in `grid.providers`
 *  apply ahead of that provider's bare entry; the pin has no effect on its
 *  own without a matching regional entry somewhere in the ladder.
 *  `grid.tables` holds named hourly CSVs `grid.default.table` / a provider
 *  entry's own `table` can reference. */
export interface EmissionsOverrides {
  version?: number
  grid?: {
    default?: EmissionsGridOverride
    providers?: Partial<Record<string, EmissionsGridOverride>>
    regions?: Partial<Record<string, string>>
    tables?: Record<string, EmissionsGridTableOverride>
  }
  pue?: EmissionsPueOverride
  embodied?: EmissionsEmbodiedOverride
  band?: EmissionsBandOverride
  baseline_model?: string
  updated_by?: string
  updated_at?: string
}

/** One resolved value in `effective`: what will actually apply to the next
 *  run, plus which precedence layer produced it and its full citation. */
export interface EmissionsResolvedValue {
  value: number | string
  layer: string
  source: string
  label: string | null
  url: string | null
  as_of: string | null
  /** The dotted setting path that changes this value, e.g.
   *  `workspace.emissions.pue.cloud` — mirrors `EmissionsFactor.setting`. */
  setting: string
  /** Grid only: the region actually applied to this win (the part after "@"
   *  in a matched `provider@region` key), or null when no region pinned it. */
  region?: string | null
  /** Grid only: "annual_average" (no table, or the GET/PUT/DELETE settings
   *  endpoint computes without a run time, so a table reference always
   *  reads as annual here — see `table` below) or "hourly". */
  temporal?: 'annual_average' | 'hourly' | 'interval_weighted' | null
  /** Grid only: the name of the `grid.tables` entry the winning grid entry
   *  referenced, or null if it referenced none. Set even when `temporal`
   *  reads "annual_average" here — meaning "hourly table `<table>` will
   *  apply at run time", not "no table is configured". */
  table?: string | null
  /** Grid only: `GridTable.summary()` for `table`, or null/undefined when no
   *  table was referenced. */
  table_summary?: EmissionsGridTableSummary | null
  /** Grid only: true when a table lookup actually missed and fell back to
   *  the entry's own annual figure. Always false from the settings
   *  endpoint (it resolves without a run time, so no lookup is attempted). */
  table_miss?: boolean | null
  /** Embodied only: the named hardware profile behind this value, when it
   *  came from one rather than a flat `g_per_run`. */
  profile?: EmissionsEmbodiedProfileSummary | null
  /** Band only (mirrored onto both `band_low` and `band_high`): whether this
   *  workspace's band is configured to derive from evidence. */
  derived?: boolean | null
}

/** The resolved factor set for one provider — "what will apply to the next
 *  run" — as GET/PUT/DELETE return it keyed by provider name. */
export interface EmissionsEffectiveFactors {
  deployment: 'cloud' | 'local'
  grid: EmissionsResolvedValue
  grid_basis: string
  pue: EmissionsResolvedValue
  pue_profile: string
  embodied_g: EmissionsResolvedValue
  band_low: EmissionsResolvedValue
  band_high: EmissionsResolvedValue
  baseline_model: EmissionsResolvedValue
}

/** One shipped-default figure with the citation text the form shows beside its
 *  input ("default 458.49 gCO2e/kWh, Ember World 2025"). */
export interface EmissionsShippedDefault {
  value: number
  label: string
  source?: string
  url?: string | null
}

/** `shipped_defaults` on the GET/PUT/DELETE response: tret's own numeric
 *  defaults, mirrored with their citation so the form can show what an empty
 *  field would fall back to. Keyed defensively (optional) since the exact
 *  field set is the backend's to define — read what is present, do not assume
 *  a key that is missing means zero. */
export interface EmissionsShippedDefaults {
  grid_default?: EmissionsShippedDefault
  grid_providers?: Partial<Record<string, EmissionsShippedDefault>>
  pue_cloud?: EmissionsShippedDefault
  pue_local?: EmissionsShippedDefault
  /** The on-prem-facility counterpart to `pue_local`, shown beside the Local
   *  PUE input when `pue.local_profile` is `onprem_datacenter` — `pue_local`
   *  stays the hint for the `workstation` profile. */
  pue_onprem?: EmissionsShippedDefault
  embodied_g_per_run?: EmissionsShippedDefault
  band_low?: EmissionsShippedDefault
  band_high?: EmissionsShippedDefault
  baseline_model?: EmissionsShippedDefault
}

/** GET/PUT/DELETE `/api/workspace/settings/emissions` all return this shape —
 *  PUT and DELETE differ only in what `overrides` holds afterward.
 *
 *  `effective` is null and `error` is set when the stored override document no
 *  longer validates against the current schema (e.g. a field a later version
 *  removed) — the backend fails open rather than 500ing: `overrides` still
 *  carries the raw, un-resolved document so the operator can see and fix or
 *  clear it, but nothing can be safely resolved to "what applies next", so
 *  `effective` is withheld rather than guessed. */
export interface EmissionsSettingsResponse {
  overrides: EmissionsOverrides | Record<string, never>
  effective: Record<string, EmissionsEffectiveFactors> | null
  shipped_defaults: EmissionsShippedDefaults
  error?: string
}

/** POST `/api/analytics/emissions/whatif` body — a scenario is a partial
 *  override document, evaluated over the same window the Emissions page is
 *  already showing. */
export interface EmissionsWhatifBody {
  project_id?: string | null
  days: number
  factors: Partial<EmissionsOverrides>
  mode?: 'estimate_both_sides' | 'preserve_measured_energy'
}

/** The difference the scenario would have made. `co2e_g`/`co2e_pct` are null
 *  under the same rule as `carbon_is_summable` elsewhere: when either side
 *  spans more than one GHG Protocol basis, there is no single carbon figure to
 *  difference. Energy and money are unaffected — see `SUMMABLE_ACROSS_BASES_NOTE`. */
export interface EmissionsWhatifDelta {
  co2e_g: number | null
  co2e_pct: number | null
  energy_wh: number
  avoided_usd: number
}

export interface EmissionsWhatifResult {
  recorded: EmissionsAnalytics
  scenario: EmissionsAnalytics
  delta: EmissionsWhatifDelta
  runs_recomputed: number
  runs_skipped: number
  runs_preserved?: number
  mode?: 'estimate_both_sides' | 'preserve_measured_energy'
  mode_note?: string
  exclusions?: { reason: string; runs: number }[]
  basis: string
  /** Present when the workspace's own stored override document could not be
   *  applied to this scenario (e.g. it no longer validates) — the recompute
   *  still ran, but layered on less than the workspace normally configures.
   *  Absent when there is nothing to warn about. */
  warnings?: string[]
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

// ── Billing ───────────────────────────────────────────────────────────────
// GET /api/billing/status 404s when the proprietary tret-cloud extension is not
// loaded into this backend — that is a capability gate, not an error, and the
// UI renders nothing at all for it. `enabled: false` is a different state: the
// extension is present but this workspace has not activated billing yet.

export interface BillingStatus {
  enabled: boolean
  plan: 'none' | 'solo' | 'team'
  subscription_status: string
  balance_usd: number
  seats: number
}

/** One entry in the billing ledger — a credit purchase, a subscription charge,
 *  or a run's metered spend. `run_id` is null unless the entry was incurred by
 *  a specific run; `stripe_ref` is null for entries with no Stripe object
 *  behind them. */
export interface LedgerEntry {
  id: string
  kind: string
  amount_usd: number
  run_id: string | null
  stripe_ref: string | null
  balance_after: number
  created_at: string
}

export type CheckoutKind = 'credits_small' | 'credits_large' | 'solo' | 'team'

/** One entry in the emissions-factor change log — GET /api/billing/emissions/history,
 *  admin/owner only (403 otherwise), 404 when tret-cloud is not loaded. `before`/`after`
 *  are the full overrides document as it stood before/after the change (null for a
 *  `put` with nothing prior, or a `delete` that cleared everything); any
 *  `grid.tables[*].csv` inside them is replaced server-side by `{ chars, sha256 }` so
 *  the log never carries a full CSV blob. */
export interface EmissionsFactorHistoryEntry {
  id: string
  created_at: string
  action: 'put' | 'delete'
  user_id: string | null
  user_email: string | null
  changed_keys: string[]
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
}

/** GET /api/billing/footprint — a billing period's emissions rollup plus what it
 *  cost in credits. Same totals shape as the Emissions page (`EmissionsTotals`),
 *  including the basis-mixed-window rule: `carbon_is_summable: false` nulls the
 *  carbon figures and `by_basis` carries the per-basis subtotals instead. Any
 *  member may read it; 404s when tret-cloud is not loaded, 422 on a malformed
 *  `period`. */
export interface BillingFootprint {
  period: string
  from: string
  to: string
  runs: number
  totals: EmissionsTotals
  by_basis?: EmissionsByBasis[]
  credits_usd_consumed: number
  /** The single GHG Protocol basis every rolled-up run in this window shares
   *  — null when the window has no runs, no run recorded a basis, or (see
   *  `totals.carbon_is_summable`) it mixes more than one, in which case
   *  `by_basis` carries the per-basis breakdown instead. Never the fixed
   *  disclosure line below. */
  basis: GridBasis
  /** The endpoint's fixed disclosure line. Rendered verbatim, never as a
   *  basis name. */
  disclaimer: string
}

// ── Connections (Google Drive / Microsoft 365 workspace integrations) ──────
// backend: api/connections.py, mounted under /api/connections. Provider keys
// are the two exact strings the backend and OAuth config use throughout:
// 'gdrive' and 'm365' — never a display label.

export type ConnectionProvider = 'gdrive' | 'm365'

/** One workspace's connection to a provider, as GET /api/connections lists it.
 *  Never carries a token — `status: 'error'` + `error_detail` is how a dead
 *  refresh token (revoked access, an `invalid_grant` on refresh) surfaces here
 *  rather than as a 401 the next time something tries to use it. */
export interface WorkspaceConnection {
  provider: ConnectionProvider
  status: 'active' | 'error'
  account_label: string | null
  granted_scopes: string[]
  connected_at: string | null
  refreshed_at: string | null
  error_detail: string | null
  /** Per-capability resource narrowing (read-access allowlist picker, m365
   *  only for now). Optional because a connection fetched before this landed
   *  carries no such field at all — read it the same way as an empty `read`:
   *  both mean "everything the connected account can see", never "nothing". */
  selected_resources?: SelectedResources
  /** Whether this connection has been re-authorized with write scopes
   *  (Files.ReadWrite.All / Sites.ReadWrite.All for m365). Optional because a
   *  connection fetched before write-back landed carries no such field —
   *  treat a missing key the same as `false`, never as "on". */
  write_enabled?: boolean
}

/** Per-capability resource narrowing on a connection. `read` is the only key
 *  this frontend acts on today; a missing key and an empty array both mean
 *  "everything the connected account can see" (unrestricted) — neither may
 *  ever be read as an allowlist that blocks everything. `write` is the m365
 *  write-back allowlist — unlike `read` there is no "everything" mode for
 *  writes, so an empty/missing `write` means "no output folders configured
 *  yet", not "write anywhere". Other keys are reserved for capabilities this
 *  frontend does not yet narrow. */
export interface SelectedResources {
  read?: M365ReadEntry[]
  write?: WriteTarget[]
  [key: string]: unknown
}

/** One SharePoint/OneDrive folder an m365 connection is allowed to write
 *  into — exactly what PUT /api/connections/m365/resources's `write` array
 *  takes, and what `WorkspaceConnection.selected_resources.write` and
 *  GET /api/connections/m365/write-targets echo back. `slug` is this
 *  frontend's own stable identifier for the folder (unique among an m365
 *  connection's write targets, not a Graph id) — findings and the publish
 *  flow address a target by slug rather than by drive/item id. `site_id` is
 *  null for a OneDrive folder (no site); `web_url` is the Graph-reported
 *  link, null where the source item lacked one. */
export interface WriteTarget {
  slug: string
  label: string
  path: string
  site_id: string | null
  drive_id: string
  item_id: string
  web_url: string | null
}

/** One location an m365 connection's read-access allowlist can name — a
 *  SharePoint site's document library drive, or the connected account's
 *  OneDrive. Exactly what PUT /api/connections/m365/resources's `read` array
 *  takes, and what `WorkspaceConnection.selected_resources.read` echoes back.
 *  `site_id` is null for `kind: 'onedrive'` (OneDrive has no site); `web_url`
 *  is the Graph-reported link, null wherever the source item lacked one. */
export interface M365ReadEntry {
  site_id: string | null
  drive_id: string
  label: string
  kind: 'site_drive' | 'onedrive'
  web_url: string | null
}

/** GET /api/connections/m365/sources — the read side of what the allowlist
 *  picker ultimately writes back through PUT .../resources: every location
 *  available to restrict to, keyed by a stable `slug` this frontend does not
 *  otherwise use. `restricted` mirrors whether `selected_resources.read` is
 *  currently non-empty. Unused by the picker itself (which drives its tree
 *  off the existing m365 browse endpoint instead), kept here because it is
 *  part of the read-access contract other frontend code may draw on. */
export interface M365Source {
  slug: string
  provider: 'm365'
  kind: 'site_drive' | 'onedrive'
  label: string
  site_id: string | null
  drive_id: string
  web_url: string | null
}

export interface M365SourcesResult {
  sources: M365Source[]
  restricted: boolean
}

/** The Google Picker's own client-side credentials — present only for gdrive,
 *  and only once `TRET_GDRIVE_PICKER_API_KEY`/`TRET_GDRIVE_APP_ID` are both
 *  set. Absence (null) means the provider can still be connected, but Import
 *  from Google Drive has nothing to build a Picker with. */
export interface ConnectionPickerConfig {
  api_key: string
  app_id: string
}

/** One provider's availability, as GET /api/connections/providers lists it —
 *  independent of whether *this* workspace has connected it (`connected`
 *  mirrors WorkspaceConnection's existence; `configured` is whether the
 *  deployment has OAuth client credentials for it at all, from env vars or
 *  the extension hook). An unconfigured provider has no Connect flow to
 *  offer — the UI shows the env var hint instead. */
export interface ConnectionProviderInfo {
  provider: ConnectionProvider
  configured: boolean
  connected: boolean
  picker: ConnectionPickerConfig | null
}

export interface ConnectionAuthorizeResult {
  authorize_url: string
}

/** GET /api/connections/{provider}/token — a short-lived provider access
 *  token for client-side use (the Google Picker only, for now). Never stored;
 *  fetched fresh immediately before opening the Picker. */
export interface ConnectionTokenResult {
  access_token: string
  expires_in: number
}

/** One row in the m365 tree browser (GET /api/connections/m365/browse):
 *  a SharePoint site, one of its drives (or the pseudo "OneDrive" entry at
 *  the root), a folder, or a file. Only `kind: 'file'` is importable —
 *  the other kinds are drilled into, never picked directly. `drive_id` is
 *  present on drives, folders and files (what an import of a file needs
 *  alongside its own `id`); `site_id` only on sites. */
export interface M365BrowseItem {
  id: string
  name: string
  kind: 'site' | 'drive' | 'folder' | 'file'
  mime_type?: string | null
  size?: number | null
  modified_at?: string | null
  site_id?: string | null
  drive_id?: string | null
  /** Graph's own link to the item — populated on `kind: 'site'` rows for the
   *  read-access picker (`M365ReadAccessPicker`), which threads a site's
   *  `web_url` onto the `M365ReadEntry` it builds for that site's drives.
   *  Absent/null elsewhere, and on a backend predating this field. */
  web_url?: string | null
}

export interface M365BrowseResult {
  items: M365BrowseItem[]
}

/** GET /api/connections/m365/write-targets — the write-back counterpart to
 *  `M365SourcesResult`: every folder the connection is currently allowed to
 *  write into, plus whether write-back is enabled at all (mirrors
 *  `WorkspaceConnection.write_enabled`). Used by the Deliverables "Publish to
 *  SharePoint" flow, which needs the target list without fetching the whole
 *  connections list. A 404 (endpoint not deployed yet) or a 409 (m365 not
 *  connected/write-enabled) both mean "nothing to publish to" — callers hide
 *  the publish affordance rather than surfacing either as an error. */
export interface M365WriteTargetsResult {
  targets: WriteTarget[]
  write_enabled: boolean
}

/** One row of GET /api/connections/activity?limit=N (admin only) — an audit
 *  log entry for a connection read or write (a search hit, a file read, a
 *  write-back upload, an allowlist change). `target` and `detail` are free
 *  text the backend formats for display; `actor_run_id` is null for an
 *  admin-driven action (e.g. changing the allowlist) rather than something a
 *  run did. */
export interface ConnectionActivityItem {
  id: string
  provider: ConnectionProvider
  action: string
  actor_user_id: string | null
  actor_run_id: string | null
  target: string | null
  bytes: number | null
  detail: string | null
  created_at: string | null
}

/** One file picked from either provider, exactly as
 *  POST /api/projects/{project_id}/documents/import wants it. `drive_id` is
 *  always null for gdrive (a Drive file id is self-sufficient); for m365 it
 *  names which drive `id` lives in, same as the browse item it came from. */
export interface ImportItem {
  id: string
  name: string
  drive_id: string | null
}

/** One item the import endpoint could not bring in — shown per-item
 *  alongside whichever items it did import, never in place of them. */
export interface ImportError {
  id: string
  name: string
  detail: string
}

export interface ImportDocumentsResult {
  documents: TretDocument[]
  errors: ImportError[]
}

// ── Marketplace: registry (Find / Install) ───────────────────────────────
// Everything below proxies through the backend's own registry client
// (api/packs.py's `/registry/*` routes -> `_registry_get`/`_download_pack_archive`),
// which itself proxies tret-cloud's `/api/marketplace/*` registry API. Shapes
// verified against tret_cloud/marketplace/{api.py,models.py} (2026-08-25).

/** One pack as a search result, or the base of a per-slug summary
 *  (`GET /api/packs/registry/search`'s `items`, `GET /api/packs/registry/{slug}`
 *  — `api.py::_pack_summary_out`). Note there is no `author` field here —
 *  a pack's author lives only in each version's `manifest` (see
 *  `RegistryPackManifest` below), not on the pack row itself. */
export interface RegistryPackSummary {
  id: string
  slug: string
  display_name: string
  description: string
  tags: string[]
  frameworks: string[]
  download_count: number
  // The pack's currently-listed **version row's id** — NOT a version string
  // ("vN available" logic and Find must resolve it against `versions` below
  // to get the actual "vN" to show/fetch/install; see UpdateBadge/
  // FindDetailPane in Packs.tsx). Null when nothing has ever been listed.
  latest_listed_version: string | null
  // Present only on the per-slug summary (`GET /packs/{slug}` ->
  // `get_pack`, which adds this key); absent on a search result row.
  versions?: RegistryPackVersionSummary[]
}

/** One listed/delisted version, as summarized in a per-slug summary's
 *  `versions` list (`api.py::_version_summary_out`). */
export interface RegistryPackVersionSummary {
  id: string
  version: string
  state: string
  has_methods: boolean
  size_bytes: number
  submitted_at: string
  listed_at: string | null
}

/** `GET /api/packs/registry/search` -> the registry's `GET /packs`
 *  (`api.py::list_packs`) — an object, cursor-paginated. */
export interface RegistrySearchResult {
  items: RegistryPackSummary[]
  next_cursor: string | null
}

/** `PackManifest.model_dump()` (backend: packs/schema.py; same shape as
 *  `DraftManifest`) plus `doctrine_contents` — every doctrine file's full
 *  text, inlined at submission time so Find/review never re-read the
 *  archive (`submission_checks.py::run_submission_checks`). Unlike
 *  `DraftManifest.methods` (always empty on a draft), `methods` here can be
 *  non-empty for a submitted/listed pack. */
export interface RegistryPackManifest {
  pack: string
  version: string
  display_name: string
  description: string
  author: string | null
  license: string | null
  homepage: string | null
  tags: string[]
  frameworks: string[]
  doctrine: string[]
  task_types: TaskType[]
  datasets: PackDatasetRef[]
  methods: PackMethod[]
  doctrine_contents: Record<string, string>
  // Verified against packs/schema.py — see `PackDetail.harnesses` above, same
  // manifest field, mirrored onto the registry's own copy of the manifest.
  harnesses?: HarnessPreset[]
}

/** One pack version's full detail — `api.py::_version_detail_out`. This is
 *  the one shape shared by four different endpoints: the registry's
 *  `GET /packs/{slug}/{version}` (Find/Install), `GET /my-submissions` and
 *  `GET /review/queue` (arrays of this), and every review-decision response
 *  (approve/request-changes/reject/delist) — none of those return a
 *  narrower `{id, state}`-shaped body. Note the field is `slug`, not
 *  `pack_slug`, and there is no `publisher_display_name` anywhere on it —
 *  only `submitted_by`/`reviewed_by` user ids. */
export interface MarketplacePackVersion {
  id: string
  pack_id: string
  slug: string
  display_name: string
  version: string
  state: string
  manifest: RegistryPackManifest
  content_hash: string
  doctrine_sha: string
  has_methods: boolean
  size_bytes: number
  review_notes: string | null
  submitted_by: string
  reviewed_by: string | null
  submitted_at: string
  reviewed_at: string | null
  listed_at: string | null
}

/** Same object as `MarketplacePackVersion` — kept as its own name in the
 *  registry (Find/Install) call sites, which never touch review/submission
 *  fields, so those reads stay obviously scoped to what Find actually shows. */
export type RegistryPackVersion = MarketplacePackVersion

// ── Pack builder drafts (Plan Phase D) ────────────────────────────────────
// backend: api/pack_builder.py. A draft's file content is `str` (text) or
// `{b64}` (binary) — see DraftPack's own model docstring.
export type DraftFileContent = string | { b64: string }

export interface PackDatasetRef {
  name: string
  file: string // CSV path relative to the pack root, e.g. "datasets/foo.csv"
}

/** Mirrors `PackManifest.model_dump()` (backend: packs/schema.py) — the same
 *  shape `pack.yaml` parses to. `methods` must always stay empty on a draft;
 *  the builder has no methods editor and the backend rejects a non-empty one
 *  with 422 (see pack_builder.py::_reject_methods). That rejection only
 *  looks at this object — it is `DraftFileContent`'s own guard
 *  (`files["pack.yaml"]` is a reserved name PATCH refuses, backend:
 *  tret.packs.draft.RESERVED_ROOT_NAMES) that stops `files{}` from smuggling
 *  a whole replacement manifest, methods included, past this check. */
export interface DraftManifest {
  pack: string
  version: string
  display_name: string
  description: string
  author: string | null
  license: string | null
  homepage: string | null
  tags: string[]
  frameworks: string[]
  doctrine: string[]
  task_types: TaskType[]
  datasets: PackDatasetRef[]
  methods: unknown[]
  // Verified against packs/schema.py — same as `PackDetail.harnesses`/
  // `RegistryPackManifest.harnesses` above — needed here too so the builder's
  // Harnesses sub-tab has a slot in `manifest_json` to write to and PATCH,
  // the same way every other sub-tab writes its own slice of this object.
  harnesses?: HarnessPreset[]
}

export interface DraftSummary {
  id: string
  slug: string
  version: string | null
  display_name: string
  file_count: number
  created_by: string | null
  created_at: string | null
  updated_at: string | null
}

export interface DraftDetail extends DraftSummary {
  manifest_json: DraftManifest
  files: Record<string, DraftFileContent>
  test_install_seq: number
}

/** `POST /api/packs/drafts/{id}/validate` — mirrors `POST /api/packs/validate`'s
 *  own response shape (same underlying `validate_pack`), plus a `summary` block. */
export interface ValidateDraftResult {
  valid: boolean
  errors: string[]
  summary: {
    pack: string | null
    version: string | null
    task_types: string[]
    schemas: string[]
    doctrine_files: string[]
  }
}

export interface TestInstallResult {
  id: string
  slug: string
  version: string // "{version}+draft.{n}"
}

/** `POST /api/packs/drafts/{id}/submit` (tret_cloud/marketplace/submit.py
 *  ::submit_draft) — cloud-only: the route does not exist until the
 *  tret_cloud extension is loaded, so calling it on self-host 404s, which is
 *  exactly the capability gate the UI reads. Its own small dict, deliberately
 *  narrower than `MarketplacePackVersion` below (verified against
 *  tret_cloud/marketplace). */
export interface SubmitDraftResult {
  version_id: string
  pack_slug: string
  version: string
  state: string
}

// ── Marketplace submissions + review (cloud-only) ─────────────────────────
// `/api/marketplace/*`, mounted via the same extension seam as `/api/billing/*`
// — 404s entirely on a self-hosted instance with no tret-cloud extension
// loaded. Shapes verified against tret_cloud/marketplace/api.py.

/** `GET /api/marketplace/my-submissions` — an array of the caller's own full
 *  `MarketplacePackVersion` objects (`api.py::my_submissions`). */
export type MarketplaceSubmission = MarketplacePackVersion

/** `GET /api/marketplace/review/queue` — same full-object shape, oldest
 *  `in_review` submission first (`api.py::review_queue`). There is no
 *  `publisher_display_name` field anywhere in this API — only
 *  `submitted_by`/`reviewed_by` user ids. */
export type ReviewQueueItem = MarketplacePackVersion

/** `check_results` on `GET /api/marketplace/review/{id}` — the verbatim
 *  output of `submission_checks.py::SubmissionCheckResult.as_check_results()`:
 *  one overall pass/fail plus the collected errors and the pinned integrity
 *  values, not a per-check list. */
export interface MarketplaceCheckResults {
  valid: boolean
  errors: string[]
  has_methods: boolean
  size_bytes: number
  content_hash: string | null
  doctrine_sha: string | null
}

/** One manifest field that differs from the previously listed version
 *  (`diff.py::manifest_field_changes`) — `previous` is null on a pack's
 *  first-ever submission. */
export interface ManifestFieldChange {
  previous: unknown
  current: unknown
}

/** `diff_against_previous_listed` on `GET /api/marketplace/review/{id}`
 *  (`diff.py::build_diff`). */
export interface MarketplaceDiff {
  manifest_changes: Record<string, ManifestFieldChange>
  // relpath -> unified diff text against the previously listed version;
  // every doctrine file's full text (as an "add") on a pack's first
  // submission, since there is nothing to diff against yet.
  doctrine_diffs: Record<string, string>
}

/** `GET /api/marketplace/review/{id}` (`api.py::review_detail`) — the full
 *  version object plus the two review-only blocks. `methods` for the
 *  methods-review banner live at `manifest.methods` (inherited from
 *  `MarketplacePackVersion`), not as a top-level field. */
export interface ReviewDetail extends MarketplacePackVersion {
  check_results: MarketplaceCheckResults
  diff_against_previous_listed: MarketplaceDiff
}

// ── Fetch wrapper ────────────────────────────────────────────────────────

export class ApiError extends Error {
  status: number
  /** The raw `detail` value from a JSON error body, when the response was JSON
   *  and carried one. Usually a plain string (already folded into `message`),
   *  but some 403s shape it as an object instead — an extension workspace-gate
   *  refusal sends `{ reason, detail }` rather than a string (see
   *  `EmissionsFactorsPanel.tsx`'s gate-vs-role 403 handling) — so a caller
   *  that needs more than the stringified `message` can inspect this.
   *  `undefined` when the body was not JSON or had no `detail` key. */
  detail?: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
    this.name = 'ApiError'
  }
}

/** A 403's own text, when it came from a registered extension's workspace-gate
 *  refusal (`{ detail: { reason, detail } }`) rather than a plain role check
 *  (`{ detail: "<string>" }`, already folded into `error.message`). `null` for
 *  every other shape, so a caller falls back to its own generic role message. */
export function gateRefusalDetail(error: ApiError | null | undefined): string | null {
  if (!error || error.status !== 403) return null
  const detail = error.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const text = (detail as Record<string, unknown>).detail
    if (typeof text === 'string' && text.trim() !== '') return text
  }
  return null
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
    let detail: unknown
    try {
      const data: unknown = await res.json()
      if (data && typeof data === 'object' && 'detail' in data) {
        detail = (data as { detail: unknown }).detail
        // An extension workspace-gate refusal shapes `detail` as `{ reason,
        // detail }` (a 403 from the connections and emissions routes); its
        // inner `detail` is the sentence meant for a person, so that is what
        // `message` carries. The raw shape still rides on `ApiError.detail`
        // for `gateRefusalDetail` below. Anything else non-string is
        // stringified as before.
        const nested =
          detail && typeof detail === 'object' && !Array.isArray(detail)
            ? (detail as Record<string, unknown>).detail
            : undefined
        message =
          typeof detail === 'string'
            ? detail
            : typeof nested === 'string' && nested.trim() !== ''
              ? nested
              : JSON.stringify(detail)
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message, detail)
  }
  return (await res.json()) as T
}

// ── API functions ────────────────────────────────────────────────────────

export const api = {
  // auth
  authConfig: () => request<AuthConfig>('/auth/config'),
  login: (email: string, password: string) =>
    request<User>('/auth/login', { method: 'POST', body: { email, password } }),
  // `oidc_logout_url`, when present, is where the browser should go next —
  // tret's own cookie is already cleared by this call either way.
  logout: () => request<{ ok: boolean; oidc_logout_url?: string }>('/auth/logout', { method: 'POST' }),
  me: () => request<User>('/auth/me'),
  switchWorkspace: (workspaceId: string) =>
    request<User>('/auth/workspace', { method: 'POST', body: { workspace_id: workspaceId } }),
  acceptInvite: (token: string) =>
    // Mirrors switch_workspace, by design (backend: api/workspaces.py
    // ::accept_invite): joins + switches to the invited workspace and
    // re-mints the cookie, returning the same UserOut shape.
    request<User>(`/auth/invites/${encodeURIComponent(token)}/accept`, { method: 'POST' }),
  listUsers: () => request<User[]>('/auth/users'),
  createUser: (body: { email: string; display_name: string; password: string; role: string }) =>
    request<User>('/auth/users', { method: 'POST', body }),

  // workspaces (team creation, membership and invites).
  createWorkspace: (name: string) =>
    // Mirrors switch_workspace, by design (backend: api/workspaces.py
    // ::create_team_workspace): creates the team workspace, makes this user
    // its owner, switches the session to it, and returns UserOut.
    request<User>('/workspaces', { method: 'POST', body: { name } }),
  workspaceMembers: (workspaceId: string) =>
    request<WorkspaceMember[]>(`/workspaces/${workspaceId}/members`),
  createInvite: (workspaceId: string, email: string, role: string) =>
    request<WorkspaceInviteCreated>(`/workspaces/${workspaceId}/invites`, {
      method: 'POST',
      body: { email, role },
    }),
  listInvites: (workspaceId: string) =>
    request<WorkspaceInvite[]>(`/workspaces/${workspaceId}/invites`),
  revokeInvite: (workspaceId: string, inviteId: string) =>
    request<{ ok: boolean }>(`/workspaces/${workspaceId}/invites/${inviteId}`, {
      method: 'DELETE',
    }),
  updateMemberRole: (workspaceId: string, userId: string, role: string) =>
    request<WorkspaceMember>(`/workspaces/${workspaceId}/members/${userId}`, {
      method: 'PATCH',
      body: { role },
    }),
  removeMember: (workspaceId: string, userId: string) =>
    request<{ ok: boolean }>(`/workspaces/${workspaceId}/members/${userId}`, {
      method: 'DELETE',
    }),

  // runs
  createRun: (body: CreateRunBody) =>
    request<{ run_id: string }>('/runs', { method: 'POST', body }),
  /** `topLevelOnly` hides delegated runs (those with a `parent_run_id`). */
  listRuns: (limit = 50, cursor?: string, topLevelOnly = false) =>
    request<RunsPage>(
      `/runs?limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}${
        topLevelOnly ? '&top_level_only=true' : ''
      }`,
    ),
  getRun: (id: string) => request<RunDetail>(`/runs/${id}`),
  /** Direct children only (runs this one delegated to), oldest first. */
  listRunChildren: (id: string) => request<RunSummary[]>(`/runs/${id}/children`),
  cancelRun: (id: string) => request<{ ok: boolean }>(`/runs/${id}/cancel`, { method: 'POST' }),

  // harnesses
  listHarnesses: () => request<Harness[]>('/harnesses'),
  getHarness: (id: string) => request<HarnessDetail>(`/harnesses/${id}`),
  createHarness: (body: HarnessBody) => request<Harness>('/harnesses', { method: 'POST', body }),
  updateHarness: (id: string, body: HarnessBody) =>
    request<Harness>(`/harnesses/${id}`, { method: 'PUT', body }),
  archiveHarness: (id: string) =>
    request<{ ok: boolean }>(`/harnesses/${id}`, { method: 'DELETE' }),

  // routing preview (dry run — see api/routing.py; never persists, never runs)
  previewRouting: (body: RoutingPreviewBody) =>
    request<{ results: RoutingPreviewResult[] }>('/routing/preview', { method: 'POST', body }),

  // documents + datasets
  uploadDocument: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<TretDocument>('/documents', { method: 'POST', form })
  },
  listDocuments: () => request<TretDocument[]>('/documents'),
  getDocument: (id: string) => request<TretDocumentDetail>(`/documents/${id}`),
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
  // Re-attempts a failed connected_write upload (payload.upload.status ===
  // 'failed') without re-approving. Approver-role only, same as deciding the
  // finding itself; callers show the 403 inline rather than hiding the
  // button, matching decideFinding's pattern.
  retryFindingUpload: (id: string) =>
    request<Finding>(`/findings/${id}/upload-retry`, { method: 'POST' }),
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
  deletePack: (id: string) => request<{ deleted: boolean }>(`/packs/${id}`, { method: 'DELETE' }),
  installPackArchive: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Pack>('/packs/install/archive', { method: 'POST', form })
  },

  // marketplace registry (Find / Install) — backend proxy, api/packs.py's own
  // `/registry/*` routes, passing tret_cloud/marketplace's response bodies
  // through verbatim. Shapes verified against tret_cloud/marketplace.
  registrySearch: (params: { q?: string; tags?: string; framework?: string } = {}) => {
    const qs = new URLSearchParams()
    if (params.q) qs.set('q', params.q)
    if (params.tags) qs.set('tags', params.tags)
    if (params.framework) qs.set('framework', params.framework)
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return request<RegistrySearchResult>(`/packs/registry/search${suffix}`)
  },
  registryPackSummary: (slug: string) =>
    request<RegistryPackSummary>(`/packs/registry/${encodeURIComponent(slug)}`),
  registryPackVersion: (slug: string, version: string) =>
    request<RegistryPackVersion>(
      `/packs/registry/${encodeURIComponent(slug)}/${encodeURIComponent(version)}`,
    ),
  registryInstall: (slug: string, version: string) =>
    request<Pack>('/packs/registry/install', { method: 'POST', body: { slug, version } }),

  // pack builder drafts (Plan Phase D) — backend: api/pack_builder.py
  listDrafts: () => request<DraftSummary[]>('/packs/drafts'),
  createDraft: (slug: string) => request<DraftDetail>('/packs/drafts', { method: 'POST', body: { slug } }),
  getDraft: (id: string) => request<DraftDetail>(`/packs/drafts/${id}`),
  patchDraft: (
    id: string,
    body: { manifest_json?: DraftManifest; files?: Record<string, DraftFileContent | null> },
  ) => request<DraftDetail>(`/packs/drafts/${id}`, { method: 'PATCH', body }),
  deleteDraft: (id: string) => request<{ deleted: boolean }>(`/packs/drafts/${id}`, { method: 'DELETE' }),
  validateDraft: (id: string) =>
    request<ValidateDraftResult>(`/packs/drafts/${id}/validate`, { method: 'POST' }),
  testInstallDraft: (id: string) =>
    request<TestInstallResult>(`/packs/drafts/${id}/test-install`, { method: 'POST' }),
  // Not a fetch: a plain URL for an <a href download> — the browser handles the
  // streamed tar.gz (and the session cookie) on its own, same-origin.
  draftExportUrl: (id: string) => `/api/packs/drafts/${id}/export`,
  // Cloud-only — see SubmitDraftResult's own doc comment. 404s on a self-hosted
  // build with no registry client + marketplace API behind it yet, which is
  // exactly the capability gate the UI reads (it never probes this directly;
  // see PackBuilder.tsx's use of `mySubmissions` as the combined probe).
  submitDraft: (id: string) => request<SubmitDraftResult>(`/packs/drafts/${id}/submit`, { method: 'POST' }),

  // marketplace submissions + review (cloud-only; 404s where the extension is
  // not loaded — same capability-gate convention as billing)
  mySubmissions: () => request<MarketplaceSubmission[]>('/marketplace/my-submissions'),
  reviewQueue: () => request<ReviewQueueItem[]>('/marketplace/review/queue'),
  reviewDetail: (id: string) => request<ReviewDetail>(`/marketplace/review/${id}`),
  // Each decision endpoint returns the full updated version object
  // (`api.py`'s approve/request-changes/reject all end in
  // `_version_detail_out(pack, pv)`), not a narrow `{id, state}` — verified
  // against tret_cloud/marketplace.
  reviewApprove: (id: string, notes?: string) =>
    request<MarketplacePackVersion>(`/marketplace/review/${id}/approve`, {
      method: 'POST',
      body: { notes: notes ?? '' },
    }),
  reviewRequestChanges: (id: string, notes: string) =>
    request<MarketplacePackVersion>(`/marketplace/review/${id}/request-changes`, {
      method: 'POST',
      body: { notes },
    }),
  reviewReject: (id: string, notes: string) =>
    request<MarketplacePackVersion>(`/marketplace/review/${id}/reject`, {
      method: 'POST',
      body: { notes },
    }),

  // deliverables
  listDeliverables: () => request<Deliverable[]>('/deliverables'),
  exportDeliverableJson: (slug: string, includeDraft = false) =>
    request<DeliverableExport>(
      `/deliverables/${encodeURIComponent(slug)}/export?format=json${includeDraft ? '&include_draft=true' : ''}`,
    ),
  // Writes an export straight into an m365 output folder instead of
  // downloading it. 409s (refusal — target/connection not writable, name
  // collision, etc.) carry a `detail` string; callers show it inline rather
  // than treating it as a generic failure.
  publishDeliverable: (slug: string, body: PublishDeliverableBody) =>
    request<PublishDeliverableResult>(`/deliverables/${encodeURIComponent(slug)}/publish`, {
      method: 'POST',
      body,
    }),

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
  routingHistory: (days = 180, bucketDays = 7, projectId?: string) => {
    const qs = new URLSearchParams({ days: String(days), bucket_days: String(bucketDays) })
    if (projectId) qs.set('project_id', projectId)
    return request<RoutingHistory>(`/analytics/routing/history?${qs.toString()}`)
  },
  emissionsAnalytics: (days = 30, projectId?: string) => {
    const qs = new URLSearchParams({ days: String(days) })
    if (projectId) qs.set('project_id', projectId)
    return request<EmissionsAnalytics>(`/analytics/emissions?${qs.toString()}`)
  },

  // billing (tret-cloud, optional — /billing/status 404s when not loaded)
  billingStatus: () => request<BillingStatus>('/billing/status'),
  createCheckout: (kind: string) =>
    request<{ url: string }>('/billing/checkout', { method: 'POST', body: { kind } }),
  createPortalSession: () => request<{ url: string }>('/billing/portal', { method: 'POST' }),
  billingUsage: (cursor?: string) => {
    const qs = new URLSearchParams({ limit: '50' })
    if (cursor) qs.set('cursor', cursor)
    return request<{ items: LedgerEntry[]; next_cursor: string | null }>(
      `/billing/usage?${qs.toString()}`,
    )
  },
  getEmissionsFactorHistory: (limit = 50) =>
    request<{ entries: EmissionsFactorHistoryEntry[] }>(
      `/billing/emissions/history?limit=${limit}`,
    ),
  getBillingFootprint: (period: string) =>
    request<BillingFootprint>(`/billing/footprint?period=${encodeURIComponent(period)}`),

  // connections (Google Drive / Microsoft 365 workspace integrations)
  listConnections: () => request<{ connections: WorkspaceConnection[] }>('/connections'),
  connectionProviders: () =>
    request<{ providers: ConnectionProviderInfo[] }>('/connections/providers'),
  // scopeSet omitted keeps the original call shape/behavior (no body) —
  // pass 'write' to re-authorize with write scopes (Files.ReadWrite.All /
  // Sites.ReadWrite.All for m365) without disturbing existing callers.
  authorizeConnection: (provider: ConnectionProvider, scopeSet?: 'read' | 'write') =>
    request<ConnectionAuthorizeResult>(`/connections/${provider}/authorize`, {
      method: 'POST',
      body: scopeSet ? { scope_set: scopeSet } : undefined,
    }),
  disconnectConnection: (provider: ConnectionProvider) =>
    request<{ ok: boolean }>(`/connections/${provider}`, { method: 'DELETE' }),
  // gdrive only — a short-lived Picker token, fetched fresh right before the
  // Picker opens. 404s for any provider without a client-side use.
  gdriveToken: () => request<ConnectionTokenResult>('/connections/gdrive/token'),
  browseM365: (
    params: {
      scope?: 'sites' | 'drive_children'
      site_id?: string
      drive_id?: string
      item_id?: string
    } = {},
  ) => {
    const qs = new URLSearchParams()
    if (params.scope) qs.set('scope', params.scope)
    if (params.site_id) qs.set('site_id', params.site_id)
    if (params.drive_id) qs.set('drive_id', params.drive_id)
    if (params.item_id) qs.set('item_id', params.item_id)
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return request<M365BrowseResult>(`/connections/m365/browse${suffix}`)
  },
  // m365 read-access allowlist (Connections view). sources() is the read side
  // of the contract, kept here even though the picker itself walks the browse
  // tree above; setM365ReadAccess() is what the picker's Save and the card's
  // remove/"Allow everything" controls all funnel through — an empty array
  // clears the restriction. Admin only; a non-admin PUT 403s. 409s (and, until
  // the backend lands, 404s) on a connection that isn't usable — callers show
  // `ApiError.message` inline rather than treating either as "unrestricted".
  m365Sources: () => request<M365SourcesResult>('/connections/m365/sources'),
  setM365ReadAccess: (read: M365ReadEntry[]) =>
    request<WorkspaceConnection>('/connections/m365/resources', { method: 'PUT', body: { read } }),
  // m365 write-back allowlist (Connections view's M365WriteTargetPicker).
  // Same PUT as the read allowlist — the body's `read`/`write` keys are
  // independent, absent keys left untouched server-side — so a write-only
  // save never disturbs the read allowlist and vice versa. 409s on a
  // connection without write scopes granted; callers show `ApiError.message`
  // inline rather than treating it as "no output folders".
  setM365WriteTargets: (write: WriteTarget[]) =>
    request<WorkspaceConnection>('/connections/m365/resources', { method: 'PUT', body: { write } }),
  // Read side of the write allowlist, kept separate from listConnections()
  // for the Deliverables publish flow, which only needs the target list —
  // not the whole connections roster — to decide whether to show "Publish to
  // SharePoint" at all. 404 (endpoint not deployed yet) and 409 (m365 not
  // connected/write-enabled) both mean "nothing to publish to".
  m365WriteTargets: () => request<M365WriteTargetsResult>('/connections/m365/write-targets'),
  // Admin-only audit log across both providers' reads and writes.
  connectionActivity: (limit = 50) =>
    request<{ items: ConnectionActivityItem[] }>(`/connections/activity?limit=${limit}`),
  importDocuments: (projectId: string, provider: ConnectionProvider, items: ImportItem[]) =>
    request<ImportDocumentsResult>(`/projects/${projectId}/documents/import`, {
      method: 'POST',
      body: { provider, items },
    }),

  // workspace emissions factor overrides (Settings → Emissions factors, and
  // the what-if scenario drawer on the Emissions page). GET is any member;
  // PUT/DELETE require admin/owner and 403 otherwise.
  getEmissionsSettings: () =>
    request<EmissionsSettingsResponse>('/workspace/settings/emissions'),
  setEmissionsSettings: (body: EmissionsOverrides) =>
    request<EmissionsSettingsResponse>('/workspace/settings/emissions', {
      method: 'PUT',
      body,
    }),
  clearEmissionsSettings: () =>
    request<EmissionsSettingsResponse>('/workspace/settings/emissions', { method: 'DELETE' }),
  emissionsWhatif: (body: EmissionsWhatifBody) =>
    request<EmissionsWhatifResult>('/analytics/emissions/whatif', { method: 'POST', body }),
}

// ── workspace spend budgets (Settings → Spend budget) ───────────────────────
// A per-workspace period cap on top of a harness's own per-run cost cap,
// enforced softly by tret's own `budget_pre_run_gate`
// (`backend/tret/services/budgets.py`) and surfaced here from the same GET
// that gate itself reads. Kept as a second exported object rather than more
// entries on `api` above, so this addition is a pure end-of-file append —
// see that (already enormous) object's own growth for why every other
// feature in this file just adds a key to it instead; budgets is the one
// exception, added under an explicit instruction to touch only the end of
// this file. GET is any member; PUT/DELETE require admin/owner and 403
// otherwise — the same role gating `getEmissionsSettings` and friends above
// use for their own workspace settings.
export type BudgetPeriod = 'daily' | 'weekly' | 'monthly'

export interface BudgetSettings {
  period: BudgetPeriod
  cap_usd: number
  alerts: number[]
}

/** `BudgetSettings` plus the live numbers computed from it right now.
 *
 *  `spent_usd`/`remaining_usd`/`fraction`/`alerts_crossed` are `null` on the
 *  rare response where the config itself (period/cap_usd/alerts) is known
 *  good — just read back, or just written by a PUT that already committed —
 *  but computing the live spend against it failed (a DB hiccup in the
 *  underlying query). `window_start`/`window_end` are pure date math off
 *  `period` and are always present. */
export interface BudgetStatus extends BudgetSettings {
  window_start: string
  window_end: string
  spent_usd: number | null
  remaining_usd: number | null
  fraction: number | null
  alerts_crossed: number[] | null
}

export interface BudgetStatusResponse {
  /** `null` when this workspace has no spend budget configured. */
  budget: BudgetStatus | null
}

export const budgetApi = {
  getBudgetSettings: () => request<BudgetStatusResponse>('/workspace/settings/budget'),
  setBudgetSettings: (body: BudgetSettings) =>
    request<BudgetStatusResponse>('/workspace/settings/budget', { method: 'PUT', body }),
  clearBudgetSettings: () =>
    request<BudgetStatusResponse>('/workspace/settings/budget', { method: 'DELETE' }),
}

/** One row of a pack's per-workspace "lessons" memory (backend:
 *  api/lessons.py::_lesson_out). `status` moves proposed -> approved|rejected
 *  (a human reviewer's one-time decision, `POST .../review`) and, only from
 *  `approved`, -> retired (admin-only, `POST .../retire`) — never back.
 *  Keyed on `pack_slug`, not `pack_id`: a lesson approved under one installed
 *  version stays visible (and, if `in_effect`, still live in the prompt)
 *  after the pack is upgraded to a new version/id — `pack_id` is only
 *  provenance of which install first produced it and can be `null` once that
 *  install is gone. Only `approved` lessons are ever read into a run's
 *  prompt (`engine/context.py`'s `pack_lessons` block), and even among those
 *  only the ones `in_effect` — the same 40-item/4,000-char cap
 *  `services/lessons.py::approved_lessons` applies; an approved lesson past
 *  the cap is stored and reviewable but silently never sent. This endpoint's
 *  list is the full history, every status, for a human reviewing the memory
 *  itself. */
export interface PackLesson {
  id: string
  pack_id: string | null
  pack_slug: string
  ordinal: number
  status: 'proposed' | 'approved' | 'rejected' | 'retired'
  text: string
  rationale: string
  in_effect: boolean
  proposed_by_run_id: string | null
  reviewed_by_user_id: string | null
  created_at: string | null
  reviewed_at: string | null
}

export const lessonsApi = {
  listLessons: (packId: string, status?: PackLesson['status']) =>
    request<PackLesson[]>(`/packs/${packId}/lessons${status ? `?status=${status}` : ''}`),
  // Approver role or higher, same gate as decideFinding. `approve: false` is
  // "reject", not "delete" — the row (and the model's original wording)
  // survives as a rejected lesson, visible in the full history above.
  reviewLesson: (packId: string, lessonId: string, approve: boolean) =>
    request<PackLesson>(`/packs/${packId}/lessons/${lessonId}/review`, {
      method: 'POST',
      body: { approve },
    }),
  // Admin-only, and only an `approved` lesson accepts it (409 otherwise) —
  // see PackLesson's own doc comment on why retiring is a one-way exit from
  // `approved` rather than a delete.
  retireLesson: (packId: string, lessonId: string) =>
    request<PackLesson>(`/packs/${packId}/lessons/${lessonId}/retire`, { method: 'POST' }),
}
