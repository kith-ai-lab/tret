import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'

import {
  api,
  ApiError,
  COST_TIER_DESCRIPTIONS,
  COST_TIERS,
  type CostTier,
  DEFAULT_MAX_COST_TIER,
  DEFAULT_OBJECTIVE,
  type Harness,
  type HarnessBody,
  type LoopConfig,
  type ModelPolicy,
  OBJECTIVE_DESCRIPTIONS,
  ROUTING_OBJECTIVES,
  type RoutingObjective,
} from '../api/client'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { QueryError } from '../components/shared/MonoTable'
import { ModelSelect, modelPriceLabel, modelUnavailableLabel } from './Workbench'

const NEW_ID = '__new__'

const DEFAULT_LOOP: LoopConfig = {
  max_iterations: 24,
  max_output_tokens: 8192,
  temperature: 0.2,
  max_cost_usd: 2,
}

const emptyForm: HarnessBody = {
  name: '',
  description: null,
  pack_id: null,
  task_profile: 'freeform',
  system_prompt_extra: null,
  model_policy: { mode: 'auto', max_cost_tier: DEFAULT_MAX_COST_TIER, objective: DEFAULT_OBJECTIVE },
  tool_names: [],
  loop_config: { ...DEFAULT_LOOP },
}

export function Harnesses() {
  const harnessesQuery = useQuery({ queryKey: ['harnesses'], queryFn: api.listHarnesses })
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const harnesses = harnessesQuery.data ?? []

  useEffect(() => {
    if (harnessesQuery.data === undefined || selectedId === NEW_ID) return
    const list = harnessesQuery.data
    if (list.length > 0 && !list.some((h) => h.id === selectedId)) {
      setSelectedId(list[0].id)
    } else if (list.length === 0) {
      setSelectedId(NEW_ID)
    }
  }, [harnessesQuery.data, selectedId])

  return (
    <div>
      <h1 className="view-title">Harnesses</h1>
      <div className="view-sub">
        A harness binds a pack, a model policy, tools, and loop limits into a runnable unit.
      </div>

      {harnessesQuery.isLoading ? (
        <div className="empty pulse">Loading harnesses…</div>
      ) : harnessesQuery.isError ? (
        // Not an empty list: offering "+ New harness" over a failed fetch invites
        // the user to recreate harnesses that already exist.
        <QueryError error={harnessesQuery.error} what="the harness list" />
      ) : (
        <ListDetail
          listWidth={230}
          list={
            <>
              <button
                className="btn btn-primary btn-sm"
                style={{ marginBottom: 10 }}
                onClick={() => setSelectedId(NEW_ID)}
              >
                + New harness
              </button>
              {harnesses.length === 0 && (
                <div className="empty" style={{ padding: '8px 0' }}>
                  No harnesses yet.
                </div>
              )}
              {harnesses.map((h) => (
                <ListItem
                  key={h.id}
                  active={h.id === selectedId}
                  onClick={() => setSelectedId(h.id)}
                  title={h.name}
                  sub={h.pack_slug ?? 'generic'}
                />
              ))}
            </>
          }
          detail={
            selectedId === NEW_ID ? (
              <HarnessEditor key={NEW_ID} harness={null} onSaved={(h) => setSelectedId(h.id)} />
            ) : selectedId ? (
              <HarnessEditorLoader key={selectedId} harnessId={selectedId} />
            ) : null
          }
        />
      )}
    </div>
  )
}

function HarnessEditorLoader({ harnessId }: { harnessId: string }) {
  const detailQuery = useQuery({
    queryKey: ['harness', harnessId],
    queryFn: () => api.getHarness(harnessId),
  })
  if (detailQuery.isLoading) return <div className="empty pulse">Loading harness…</div>
  if (detailQuery.isError)
    return <div className="error-text">{(detailQuery.error as Error).message}</div>
  if (!detailQuery.data) return null
  return (
    <HarnessEditor
      harness={detailQuery.data}
      assembledPrompt={detailQuery.data.assembled_system_prompt}
    />
  )
}

function HarnessEditor({
  harness,
  assembledPrompt,
  onSaved,
}: {
  harness: Harness | null
  assembledPrompt?: string
  onSaved?: (h: Harness) => void
}) {
  const queryClient = useQueryClient()
  const packsQuery = useQuery({ queryKey: ['packs'], queryFn: api.listPacks })
  const modelsQuery = useQuery({ queryKey: ['models'], queryFn: api.listModels })
  const toolsQuery = useQuery({ queryKey: ['tools'], queryFn: api.listTools })

  const [form, setForm] = useState<HarnessBody>(() =>
    harness
      ? {
          name: harness.name,
          description: harness.description,
          pack_id: harness.pack_id,
          task_profile: harness.task_profile,
          system_prompt_extra: harness.system_prompt_extra,
          model_policy: { ...harness.model_policy },
          tool_names: [...harness.tool_names],
          loop_config: { ...DEFAULT_LOOP, ...harness.loop_config },
        }
      : { ...emptyForm, model_policy: { ...emptyForm.model_policy }, loop_config: { ...DEFAULT_LOOP } },
  )
  const [showPrompt, setShowPrompt] = useState(false)

  const saveMutation = useMutation({
    mutationFn: () => (harness ? api.updateHarness(harness.id, form) : api.createHarness(form)),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['harnesses'] })
      queryClient.invalidateQueries({ queryKey: ['harness', saved.id] })
      onSaved?.(saved)
    },
  })

  const archiveMutation = useMutation({
    mutationFn: () => api.archiveHarness(harness!.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['harnesses'] }),
  })

  const packs = packsQuery.data ?? []
  const models = modelsQuery.data ?? []
  const tools = toolsQuery.data ?? []
  const selectedPack = packs.find((p) => p.id === form.pack_id) ?? null
  const taskProfiles = ['freeform', ...(selectedPack?.task_types ?? []).map((t) => t.slug)]

  const costTier = form.model_policy.max_cost_tier ?? DEFAULT_MAX_COST_TIER
  // A stored tier is a plain string on the wire, so narrowing is a check, not a
  // cast: a value this build does not know about must reach the "as stored"
  // option rather than be assumed to be one of the four.
  const knownTier = (COST_TIERS as readonly string[]).includes(costTier)
    ? (costTier as CostTier)
    : null

  const setPolicy = (patch: Partial<ModelPolicy>) =>
    setForm((f) => ({ ...f, model_policy: { ...f.model_policy, ...patch } }))
  const setLoop = (patch: Partial<LoopConfig>) =>
    setForm((f) => ({ ...f, loop_config: { ...f.loop_config, ...patch } }))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    saveMutation.mutate()
  }

  const saveError = saveMutation.error as ApiError | null

  return (
    <form onSubmit={submit} className="stack" style={{ maxWidth: 680 }}>
      <div className="row">
        <h2 className="view-title" style={{ marginBottom: 0 }}>
          {harness ? harness.name : 'New harness'}
        </h2>
        <span style={{ flex: 1 }} />
        {harness && (
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => archiveMutation.mutate()}
            disabled={archiveMutation.isPending}
          >
            Archive
          </button>
        )}
      </div>

      <div className="panel">
        <div className="field">
          <label className="mono-label">Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            required
          />
        </div>
        <div className="field">
          <label className="mono-label">Description</label>
          <input
            type="text"
            value={form.description ?? ''}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value || null }))}
          />
        </div>
        <div className="field">
          <label className="mono-label">Pack</label>
          <select
            value={form.pack_id ?? ''}
            onChange={(e) => {
              const pack_id = e.target.value || null
              setForm((f) => ({ ...f, pack_id, task_profile: 'freeform' }))
            }}
          >
            <option value="">— none (generic) —</option>
            {packs.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name} ({p.slug} v{p.version})
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label className="mono-label">Task profile</label>
          <select
            value={form.task_profile}
            onChange={(e) => setForm((f) => ({ ...f, task_profile: e.target.value }))}
          >
            {taskProfiles.map((slug) => (
              <option key={slug} value={slug}>
                {slug}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Model policy */}
      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 10 }}>
          Model policy
        </div>
        <div className="row" style={{ marginBottom: 12 }}>
          {(['auto', 'pinned'] as const).map((mode) => (
            <label key={mode} className="check-row" style={{ padding: 0 }}>
              <input
                type="radio"
                name="policy-mode"
                checked={form.model_policy.mode === mode}
                onChange={() => setPolicy({ mode })}
              />
              <span>{mode === 'auto' ? 'auto (LLM router)' : 'pinned'}</span>
            </label>
          ))}
        </div>

        {/* Routing objective — what the router optimizes for. Stored on the
            policy regardless of mode, since a harness can be un-pinned later. */}
        <div className="field">
          <label className="mono-label">Routing objective</label>
          <select
            value={form.model_policy.objective ?? DEFAULT_OBJECTIVE}
            onChange={(e) => setPolicy({ objective: e.target.value as RoutingObjective })}
          >
            {ROUTING_OBJECTIVES.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--mono)' }}>
            {OBJECTIVE_DESCRIPTIONS[form.model_policy.objective ?? DEFAULT_OBJECTIVE]}
            {form.model_policy.mode === 'pinned' && ' — applies when routing is automatic'}
          </div>
        </div>

        {form.model_policy.mode === 'pinned' ? (
          <div className="field" style={{ marginBottom: 0 }}>
            <label className="mono-label">Pinned model</label>
            <ModelSelect
              models={models}
              value={form.model_policy.model ?? ''}
              onChange={(v) => setPolicy({ model: v || undefined })}
              emptyLabel="— choose a model —"
            />
          </div>
        ) : (
          <>
            {/* Every tier the backend accepts is offered, `local` included: it is
                the documented zero-cloud policy (docs/local-models.md), and a
                select whose value matches no option renders blank and invites the
                user to overwrite a setting they cannot see. A stored value the
                catalog does not know about gets its own option for the same
                reason — the control must never be the thing that loses it. */}
            <div className="field">
              <label className="mono-label">Max cost tier</label>
              <select
                value={costTier}
                onChange={(e) => setPolicy({ max_cost_tier: e.target.value })}
              >
                {COST_TIERS.map((tier) => (
                  <option key={tier} value={tier}>
                    {tier}
                  </option>
                ))}
                {!knownTier && <option value={costTier}>{costTier} (as stored)</option>}
              </select>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--mono)' }}>
                {knownTier
                  ? COST_TIER_DESCRIPTIONS[knownTier]
                  : 'Not a tier this build recognizes — kept exactly as stored unless you change it.'}
              </div>
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">
                Allowed models (optional — empty allows all candidates)
              </label>
              <div
                style={{
                  maxHeight: 180,
                  overflowY: 'auto',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 5,
                  padding: '6px 10px',
                }}
              >
                {models.map((m) => {
                  const allowed = form.model_policy.allowed ?? []
                  const checked = allowed.includes(m.id)
                  return (
                    <label key={m.id} className="check-row">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...allowed, m.id]
                            : allowed.filter((id) => id !== m.id)
                          setPolicy({ allowed: next.length > 0 ? next : undefined })
                        }}
                      />
                      <span>{m.display_name}</span>
                      {m.cost_tier === 'local' && <span className="badge badge-green">local</span>}
                      <span className="desc">
                        {m.provider} · {modelPriceLabel(m)} · energy class {m.energy_class} (est.)
                        {m.available ? '' : ` · ${modelUnavailableLabel(m)}`}
                      </span>
                    </label>
                  )
                })}
              </div>
            </div>
          </>
        )}
      </div>

      {/* Tools */}
      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 10 }}>
          Tools ({form.tool_names.length} enabled)
        </div>
        {tools.length === 0 ? (
          <div className="empty" style={{ padding: '4px 0' }}>
            Loading tool list…
          </div>
        ) : (
          tools.map((t) => (
            <label key={t.name} className="check-row">
              <input
                type="checkbox"
                checked={form.tool_names.includes(t.name)}
                onChange={(e) => {
                  setForm((f) => ({
                    ...f,
                    tool_names: e.target.checked
                      ? [...f.tool_names, t.name]
                      : f.tool_names.filter((n) => n !== t.name),
                  }))
                }}
              />
              <span>{t.name}</span>
              <span className="desc" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {t.description}
              </span>
            </label>
          ))
        )}
      </div>

      {/* Loop config */}
      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 10 }}>
          Loop config
        </div>
        <div className="row" style={{ alignItems: 'flex-start' }}>
          <NumberField
            label="Max iterations"
            value={form.loop_config.max_iterations}
            onChange={(v) => setLoop({ max_iterations: v })}
          />
          <NumberField
            label="Max output tokens"
            value={form.loop_config.max_output_tokens}
            onChange={(v) => setLoop({ max_output_tokens: v })}
          />
          <NumberField
            label="Temperature"
            value={form.loop_config.temperature}
            step={0.1}
            onChange={(v) => setLoop({ temperature: v })}
          />
          <NumberField
            label="Max cost USD"
            value={form.loop_config.max_cost_usd}
            step={0.5}
            onChange={(v) => setLoop({ max_cost_usd: v })}
          />
        </div>
      </div>

      <div className="panel">
        <div className="field" style={{ marginBottom: 0 }}>
          <label className="mono-label">System prompt extra</label>
          <textarea
            rows={4}
            value={form.system_prompt_extra ?? ''}
            onChange={(e) => setForm((f) => ({ ...f, system_prompt_extra: e.target.value || null }))}
            placeholder="Appended after the platform preamble, doctrine, and task instructions."
          />
        </div>
      </div>

      {/* Assembled prompt preview */}
      {assembledPrompt !== undefined && (
        <div>
          <button type="button" className="btn btn-sm" onClick={() => setShowPrompt((s) => !s)}>
            {showPrompt ? 'Hide' : 'Show'} assembled system prompt ({assembledPrompt.length.toLocaleString()} chars)
          </button>
          {showPrompt && (
            <pre className="code-block" style={{ maxHeight: 420, marginTop: 8 }}>
              {assembledPrompt}
            </pre>
          )}
        </div>
      )}

      {saveError && (
        <div className="error-text">
          {saveError.status === 409
            ? `Name conflict: ${saveError.message}`
            : saveError.status === 422
              ? `Invalid config: ${saveError.message}`
              : saveError.message}
        </div>
      )}
      {archiveMutation.isError && (
        <div className="error-text">{(archiveMutation.error as Error).message}</div>
      )}

      <div className="row">
        <button className="btn btn-primary" type="submit" disabled={saveMutation.isPending || !form.name.trim()}>
          {saveMutation.isPending ? 'Saving…' : harness ? 'Save changes' : 'Create harness'}
        </button>
        {saveMutation.isSuccess && <span className="mono-label" style={{ color: 'var(--green)' }}>saved</span>}
      </div>
    </form>
  )
}

function NumberField({
  label,
  value,
  step,
  onChange,
}: {
  label: string
  value: number | undefined
  step?: number
  onChange: (v: number | undefined) => void
}) {
  return (
    <div className="field" style={{ marginBottom: 0, width: 150 }}>
      <label className="mono-label">{label}</label>
      <input
        type="number"
        step={step ?? 1}
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
      />
    </div>
  )
}
