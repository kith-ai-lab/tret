import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'

import {
  ADAPTIVE_DEFAULTS,
  ADAPTIVE_LIMITS,
  type AdaptivePolicy,
  api,
  ApiError,
  COMPACTION_MODES,
  type CompactionMode,
  COST_TIER_DESCRIPTIONS,
  COST_TIERS,
  type CostTier,
  DEFAULT_MAX_COST_TIER,
  DEFAULT_OBJECTIVE,
  ESCALATION_MODES,
  type EscalationMode,
  type Harness,
  type HarnessBody,
  type LoopConfig,
  type ModelPolicy,
  OBJECTIVE_DESCRIPTIONS,
  type Pack,
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

/** The muted one-line explanation under a control. Matches the objective and
 *  cost-tier fields, which have always used exactly these three properties. */
const HINT = { fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--mono)' } as const

const ESCALATION_DESCRIPTIONS: Record<string, string> = {
  off: 'A run never changes model, however stuck it gets.',
  on_stall:
    'A run that has stopped making progress — repeated schema failures, a retrieval loop, or most of its iteration budget spent with nothing recorded — may move to a better-performing model within this harness policy.',
}

const COMPACTION_DESCRIPTIONS: Record<string, string> = {
  auto: 'When a run approaches its context window, older bulk tool results are replaced by markers and optionally summarized. Retrieved values and recorded results are never elided, and the stored transcript is never edited — only what the model is shown.',
  off: 'A run that outgrows its context window fails there, rather than shrinking what it sends.',
}

const emptyForm: HarnessBody = {
  name: '',
  description: null,
  pack_ids: [],
  task_profile: 'freeform',
  system_prompt_extra: null,
  model_policy: { mode: 'auto', max_cost_tier: DEFAULT_MAX_COST_TIER, objective: DEFAULT_OBJECTIVE },
  tool_names: [],
  loop_config: { ...DEFAULT_LOOP },
}

export function Harnesses() {
  const harnessesQuery = useQuery({ queryKey: ['harnesses'], queryFn: api.listHarnesses })
  // Authoring (create/update/archive) needs workspace-admin or higher — the
  // server enforces this (require_workspace_admin in api/harnesses.py); this
  // is purely so an analyst isn't shown controls that would just 403. Same
  // ['owner', 'admin'].includes(me.role) check SettingsView uses for its own
  // admin-only member-management controls. Defaults to false (hidden) while
  // `me` is still loading, rather than flashing enabled controls first.
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const canAuthor = ['owner', 'admin'].includes(meQuery.data?.role ?? '')
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
              {canAuthor && (
                <button
                  className="btn btn-primary btn-sm"
                  style={{ marginBottom: 10 }}
                  onClick={() => setSelectedId(NEW_ID)}
                >
                  + New harness
                </button>
              )}
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
                  sub={h.pack_slugs.length > 0 ? h.pack_slugs.join(', ') : 'generic'}
                />
              ))}
            </>
          }
          detail={
            selectedId === NEW_ID ? (
              <HarnessEditor
                key={NEW_ID}
                harness={null}
                canAuthor={canAuthor}
                onSaved={(h) => setSelectedId(h.id)}
              />
            ) : selectedId ? (
              <HarnessEditorLoader key={selectedId} harnessId={selectedId} canAuthor={canAuthor} />
            ) : null
          }
        />
      )}
    </div>
  )
}

function HarnessEditorLoader({
  harnessId,
  canAuthor,
}: {
  harnessId: string
  canAuthor: boolean
}) {
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
      canAuthor={canAuthor}
    />
  )
}

function HarnessEditor({
  harness,
  assembledPrompt,
  canAuthor,
  onSaved,
}: {
  harness: Harness | null
  assembledPrompt?: string
  canAuthor: boolean
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
          pack_ids: [...harness.pack_ids],
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
  // Linked packs, in link order — the first is primary. Filtered against the
  // installed-packs list so a pack uninstalled since this harness was last
  // saved just quietly drops out of the picker rather than crashing it.
  const linkedPacks = form.pack_ids
    .map((id) => packs.find((p) => p.id === id))
    .filter((p): p is Pack => !!p)
  const availablePacks = packs.filter((p) => !form.pack_ids.includes(p.id))
  // Task types offered on "Task profile" are the union across every linked
  // pack, deduped by slug — if two packs declare the same slug, save will
  // 422 and surface the server's own message rather than this list silently
  // picking one. The seeded Chat Assistant's profile is fixed at "chat" (the
  // server 422s any attempt to change it) — listed first, ahead of
  // "freeform", only on that one harness, so its own select still shows a
  // sensible current value while disabled below.
  const isChatHarness = form.task_profile === 'chat'
  const taskProfiles = [
    ...(isChatHarness ? ['chat'] : []),
    'freeform',
    ...new Set(linkedPacks.flatMap((p) => p.task_types.map((t) => t.slug))),
  ]

  const addPack = (id: string) => {
    if (!id || form.pack_ids.includes(id)) return
    setForm((f) => ({
      ...f,
      pack_ids: [...f.pack_ids, id],
      task_profile: f.task_profile === 'chat' ? 'chat' : 'freeform',
    }))
  }
  const removePack = (id: string) => {
    setForm((f) => ({
      ...f,
      pack_ids: f.pack_ids.filter((pid) => pid !== id),
      task_profile: f.task_profile === 'chat' ? 'chat' : 'freeform',
    }))
  }

  const costTier = form.model_policy.max_cost_tier ?? DEFAULT_MAX_COST_TIER
  // An absent block means every default, which is what the backend's
  // `adaptive_of` returns for a policy nobody has edited — so the form shows the
  // real effective settings rather than a row of blanks.
  const adaptive = { ...ADAPTIVE_DEFAULTS, ...(form.model_policy.adaptive ?? {}) }
  const isPinned = form.model_policy.mode === 'pinned'
  const knownEscalation = (ESCALATION_MODES as readonly string[]).includes(adaptive.escalation)
  const knownCompaction = (COMPACTION_MODES as readonly string[]).includes(adaptive.compaction)
  // A stored tier is a plain string on the wire, so narrowing is a check, not a
  // cast: a value this build does not know about must reach the "as stored"
  // option rather than be assumed to be one of the four.
  const knownTier = (COST_TIERS as readonly string[]).includes(costTier)
    ? (costTier as CostTier)
    : null

  const setPolicy = (patch: Partial<ModelPolicy>) =>
    setForm((f) => ({ ...f, model_policy: { ...f.model_policy, ...patch } }))
  const setAdaptive = (patch: Partial<AdaptivePolicy>) =>
    // Written out in full once anything is touched. The backend refuses unknown
    // keys rather than dropping them, so a partial write of a block it has never
    // seen is the one shape that cannot go wrong quietly.
    setForm((f) => ({
      ...f,
      model_policy: {
        ...f.model_policy,
        adaptive: { ...ADAPTIVE_DEFAULTS, ...(f.model_policy.adaptive ?? {}), ...patch },
      },
    }))
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
            disabled={!canAuthor || archiveMutation.isPending}
            title={canAuthor ? undefined : 'Only workspace admins and owners can archive harnesses.'}
          >
            Archive
          </button>
        )}
      </div>

      {!canAuthor && (
        <div className="empty" style={{ padding: '4px 0' }}>
          Only workspace admins and owners can create or edit harnesses — the server enforces this
          too, so changes below won&rsquo;t save.
        </div>
      )}

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
          <label className="mono-label">
            Packs{linkedPacks.length > 0 && ` (${linkedPacks.length} linked, order matters — first is primary)`}
          </label>
          {linkedPacks.length === 0 ? (
            <div className="empty" style={{ padding: '4px 0' }}>
              No packs linked — generic harness.
            </div>
          ) : (
            <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
              {linkedPacks.map((p, i) => (
                <span
                  key={p.id}
                  className="chip"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, borderRadius: 4 }}
                >
                  {i === 0 && (
                    <span className="badge badge-gray" style={{ padding: '1px 5px', fontSize: 9 }}>
                      primary
                    </span>
                  )}
                  {p.slug}
                  <button
                    type="button"
                    onClick={() => removePack(p.id)}
                    aria-label={`Remove ${p.slug}`}
                    title={`Remove ${p.slug}`}
                    style={{
                      border: 'none',
                      background: 'none',
                      padding: 0,
                      lineHeight: 1,
                      color: 'var(--text-muted)',
                      cursor: 'pointer',
                      font: 'inherit',
                    }}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          {availablePacks.length > 0 && (
            <select
              value=""
              onChange={(e) => addPack(e.target.value)}
              aria-label="Add a pack"
            >
              <option value="">+ add pack…</option>
              {availablePacks.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name} ({p.slug} v{p.version})
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label className="mono-label">Task profile</label>
          <select
            value={form.task_profile}
            disabled={isChatHarness}
            onChange={(e) => setForm((f) => ({ ...f, task_profile: e.target.value }))}
          >
            {taskProfiles.map((slug) => (
              <option key={slug} value={slug}>
                {slug}
              </option>
            ))}
          </select>
          {isChatHarness && (
            <div style={{ ...HINT, marginTop: 6 }}>Chat front door — profile is fixed</div>
          )}
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

      {/* Adaptive behavior — what this harness lets tret do about what it
          learns. Part of model_policy on the wire; its own panel here because
          two of the five govern the engine mid-run rather than routing, and
          burying "this run may change model on its own" inside "Model policy"
          undersells what it does. */}
      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 4 }}>
          Adaptive behavior
        </div>
        <div style={{ ...HINT, marginBottom: 10 }}>
          All of these default to on. A fresh install has no recorded outcomes, so
          nothing changes until runs accumulate — the defaults are a no-op on day
          one, not a silent behavior change.
        </div>

        <label className="check-row" style={{ padding: 0, marginBottom: 10 }}>
          <input
            type="checkbox"
            checked={adaptive.learn_from_outcomes}
            onChange={(e) => setAdaptive({ learn_from_outcomes: e.target.checked })}
          />
          <span>
            Learn from recorded outcomes
            <span className="desc">
              {' '}
              — past runs of this task shape steer which model is chosen.
              {isPinned && ' Applies when routing is automatic.'}
            </span>
          </span>
        </label>

        <div className="field">
          <label className="mono-label">Escalation</label>
          <select
            value={adaptive.escalation}
            onChange={(e) => setAdaptive({ escalation: e.target.value as EscalationMode })}
          >
            {ESCALATION_MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            {/* A stored value this build does not recognize keeps its own
                option: a select that renders blank invites overwriting a
                setting the operator cannot see. Same rule as Max cost tier. */}
            {!knownEscalation && <option value={adaptive.escalation}>{adaptive.escalation} (as stored)</option>}
          </select>
          <div style={HINT}>
            {ESCALATION_DESCRIPTIONS[adaptive.escalation] ??
              'Not a mode this build recognizes — kept exactly as stored unless you change it.'}
            {isPinned && ' Applies when routing is automatic: a pinned model is never switched away from.'}
          </div>
        </div>

        <div className="field">
          <label className="mono-label">Context compaction</label>
          <select
            value={adaptive.compaction}
            onChange={(e) => setAdaptive({ compaction: e.target.value as CompactionMode })}
          >
            {COMPACTION_MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            {!knownCompaction && <option value={adaptive.compaction}>{adaptive.compaction} (as stored)</option>}
          </select>
          <div style={HINT}>
            {COMPACTION_DESCRIPTIONS[adaptive.compaction] ??
              'Not a mode this build recognizes — kept exactly as stored unless you change it.'}
          </div>
        </div>

        <div className="row" style={{ alignItems: 'flex-start' }}>
          <NumberField
            label="Context headroom"
            value={adaptive.context_headroom}
            step={0.05}
            onChange={(v) => setAdaptive({ context_headroom: v })}
          />
          <NumberField
            label="Max switches"
            value={adaptive.max_switches}
            onChange={(v) => setAdaptive({ max_switches: v })}
          />
        </div>
        <div style={{ ...HINT, marginTop: 6 }}>
          Headroom is the share of the model&rsquo;s context window a run may fill
          before the engine compacts ({ADAPTIVE_LIMITS.context_headroom.min}–
          {ADAPTIVE_LIMITS.context_headroom.max}); the remainder holds the answer
          the model has yet to write, so it is not slack. Max switches bounds how
          often one run may change model ({ADAPTIVE_LIMITS.max_switches.min}–
          {ADAPTIVE_LIMITS.max_switches.max}) — each one voids the prompt cache and
          re-sends the transcript at full price.
          {isPinned && ' Switching applies when routing is automatic.'}
        </div>
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
                {/* Switched off is not the same as nonexistent: the harness config
                    is valid and portable, this deployment just will not run it. */}
                {t.available ? t.description : `OFF HERE — ${t.unavailable_reason}`}
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
        <button
          className="btn btn-primary"
          type="submit"
          disabled={!canAuthor || saveMutation.isPending || !form.name.trim()}
          title={canAuthor ? undefined : 'Only workspace admins and owners can create or edit harnesses.'}
        >
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
