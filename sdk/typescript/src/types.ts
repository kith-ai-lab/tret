/**
 * Request and response types.
 *
 * Where the backend declares a pydantic model, the type comes straight from
 * the generated OpenAPI types (`./generated/openapi.ts`, built from the
 * committed `openapi.json` snapshot by `npm run generate`). Most tret routes
 * return plain dicts, which OpenAPI documents as an empty schema; those
 * responses are written out here by hand against the backend function named
 * on each type, with an index signature so an additive backend field never
 * breaks a consumer's build.
 */
import type { components } from './generated/openapi.js'

type Schemas = components['schemas']

// ── generated (pydantic models) ─────────────────────────────────────────────
export type AuthConfig = Schemas['AuthConfigOut']
export type User = Schemas['UserOut']
export type WorkspaceSummary = Schemas['WorkspaceSummary']
export type LoginBody = Schemas['LoginBody']
export type VersionInfo = Schemas['VersionOut']
export type CreateRunBody = Schemas['CreateRunBody']
export type ApprovalBody = Schemas['ApprovalBody']

// ── hand-written (dict responses) ──────────────────────────────────────────

/** Run status values the engine writes. */
export type RunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'completed_without_output'
  | 'failed'
  | 'cancelled'
  | (string & {})

/** Statuses after which a run never changes again. */
export const TERMINAL_RUN_STATUSES: readonly string[] = [
  'completed',
  'completed_without_output',
  'failed',
  'cancelled',
]

/** `api/runs.py::_run_summary` — an item of `GET /api/runs`. */
export interface RunSummary {
  id: string
  project_id: string
  harness_id: string
  conversation_id: string | null
  parent_run_id: string | null
  root_run_id: string | null
  delegation_kind: string | null
  delegation_batch_id: string | null
  delegation_label: string | null
  task_type: string
  status: RunStatus
  model_used: string | null
  provider_used: string | null
  routing: Record<string, unknown> | null
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  cost_usd: number
  /** Provider-reported cost where one exists, else the catalog price. Null
   *  only when the run spent nothing. */
  reported_cost_usd: number | null
  delegated_cost_usd: number
  /** Null = no estimate, never 0. Same for every carbon field below. */
  energy_wh: number | null
  co2e_g: number | null
  scope2_g: number | null
  scope3_g: number | null
  avoided_co2e_g: number | null
  avoided_usd: number | null
  avoided_usd_pct: number | null
  co2e_g_low: number | null
  co2e_g_high: number | null
  water_ml: number | null
  iterations: number | null
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  [key: string]: unknown
}

/** `api/runs.py::get_run` — `GET /api/runs/{id}`. */
export interface RunDetail extends RunSummary {
  task_input: Record<string, unknown>
  messages: unknown[] | null
  tree: {
    root_run_id: string
    run_count: number
    cost_usd: number
    reported_cost_usd: number | null
    energy_wh: number | null
  } | null
  document_ids: string[]
  doctrine_sha: string | null
  context_composition: Record<string, unknown> | null
  /** The full energy/carbon accounting block, as recorded. */
  energy: Record<string, unknown> | null
  model_timeline: unknown[] | null
  compactions: unknown[] | null
  overhead: unknown[] | null
  method_v3: Record<string, unknown> | null
  grounding: Record<string, unknown> | null
}

/** `GET /api/runs?limit=&cursor=` (the paged shape the SDK always asks for). */
export interface RunPage {
  items: RunSummary[]
  next_cursor: string | null
}

/** `api/documents.py::_doc_out`. */
export interface DocumentSummary {
  id: string
  project_id: string
  filename: string
  content_type: string
  byte_size: number | null
  /** Trust tier: who/what supplied the file (`upload`, `web`, a connector...). */
  source_kind: string | null
  extraction_status: string | null
  meta: Record<string, unknown> | null
  text_chars: number
  created_at: string | null
  [key: string]: unknown
}

/** `GET /api/documents/{id}` adds the extracted text (first 100k chars). */
export interface DocumentDetail extends DocumentSummary {
  extracted_text: string
}

/** `api/harnesses.py::_out`. */
export interface HarnessSummary {
  id: string
  name: string
  description: string | null
  pack_id: string | null
  pack_slug: string | null
  pack_ids: string[]
  pack_slugs: string[]
  task_profile: string | null
  system_prompt_extra: string | null
  model_policy: Record<string, unknown> | null
  tool_names: string[] | null
  loop_config: Record<string, unknown> | null
  is_archived: boolean
  updated_at: string | null
  [key: string]: unknown
}

export interface HarnessTaskType {
  slug: string
  pack_slug: string
  pack_id: string
  [key: string]: unknown
}

/** `GET /api/harnesses/{id}`. */
export interface HarnessDetail extends HarnessSummary {
  assembled_system_prompt: string
  /** Every linked pack's task types; absent when no pack is linked. */
  task_types?: HarnessTaskType[]
}

/** `api/packs.py::_out`. */
export interface PackSummary {
  id: string
  slug: string
  version: string
  display_name: string
  description: string
  author: string | null
  license: string | null
  homepage: string | null
  tags: string[]
  frameworks: string[]
  doctrine_sha: string | null
  content_hash: string | null
  doctrine_files: string[]
  task_types: Array<{ slug: string; [key: string]: unknown }>
  harnesses: unknown[]
  schemas: Record<string, unknown>
  installed_at: string | null
  [key: string]: unknown
}

/** `GET /api/packs/{id}` adds the doctrine text, keyed by file. */
export interface PackDetail extends PackSummary {
  doctrine_contents: Record<string, string>
}

export interface FindingApproval {
  action: 'approve' | 'reject' | (string & {})
  approver_id: string
  note: string | null
  created_at: string | null
}

/** `api/findings.py::_finding_out`. */
export interface Finding {
  id: string
  run_id: string
  schema_slug: string
  subject: string | null
  payload: Record<string, unknown>
  provenance: unknown
  status: 'draft' | 'approved' | 'rejected' | (string & {})
  created_at: string | null
  /** Present on `GET /api/findings/{id}` only. */
  approvals?: FindingApproval[]
  [key: string]: unknown
}

/** `POST /api/findings/{id}/approval`. */
export interface FindingDecision {
  ok: boolean
  status: string
  approver: string
}
