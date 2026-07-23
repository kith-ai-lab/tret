/** Typed fetch wrapper + interfaces for every bench API resource.
 *  All requests carry the session cookie (credentials: 'include'). */

// ── Resource types ───────────────────────────────────────────────────────

export interface User {
  id: string
  email: string
  display_name: string
  role: string // admin | analyst | approver
}

export interface RoutingDecision {
  router_model: string | null
  routing_prompt_version: string
  candidates: string[]
  chosen_model: string
  reasoning: string
  confidence: string | null
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
  cost_usd: number
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

export interface RunDetail extends RunSummary {
  task_input: Record<string, unknown>
  messages: Msg[]
  document_ids: string[]
  doctrine_sha: string | null
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
  provider: string // anthropic | kimi | openrouter
  display_name: string
  context_window: number
  input_price_per_mtok: number
  output_price_per_mtok: number
  cost_tier: string // economy | standard | premium
  strengths: string[]
  supports_tools: boolean
  curated: boolean
  available: boolean
}

export interface ToolInfo {
  name: string
  description: string
  parameters: Record<string, unknown>
}

export interface ProviderStatus {
  provider: string
  configured: boolean
  source: 'env' | 'db' | null
  last4: string | null
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
}

export interface DeliverableExport {
  markdown: string
  html: string
  sections: DeliverableExportSection[]
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
  // Assistant-only fields, stamped when the turn's run finishes:
  activity?: ChatActivity[]
  status?: string
  model_used?: string | null
  cost_usd?: number
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
  sendChatMessage: (id: string, text: string) =>
    request<{ run_id: string; conversation_id: string }>(`/chat/${id}/messages`, {
      method: 'POST',
      body: { text },
    }),

  // settings + catalog
  providerStatus: () => request<ProviderStatus[]>('/settings/providers'),
  setProviderKey: (provider: string, apiKey: string) =>
    request<{ ok: boolean }>('/settings/providers', {
      method: 'POST',
      body: { provider, api_key: apiKey },
    }),
  listModels: () => request<ModelInfo[]>('/models'),
  listTools: () => request<ToolInfo[]>('/tools'),
  routerSettings: () => request<RouterSettings>('/settings/router'),
}
