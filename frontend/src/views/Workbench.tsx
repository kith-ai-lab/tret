import { useMutation, useQuery } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import {
  api,
  DEFAULT_MAX_COST_TIER,
  DEFAULT_OBJECTIVE,
  type InputFieldSchema,
  type ModelInfo,
  type ModelPolicy,
  objectiveDescription,
  overrideAllowedByPolicy,
  type TaskType,
} from '../api/client'
import { QueryError } from '../components/shared/MonoTable'
import { shortModelName } from '../components/shared/RoutingBadge'

const FREEFORM: TaskType = { slug: 'freeform', display_name: 'Freeform', shape: 'freeform' }

export function Workbench() {
  const navigate = useNavigate()

  const harnessesQuery = useQuery({ queryKey: ['harnesses'], queryFn: api.listHarnesses })
  const modelsQuery = useQuery({ queryKey: ['models'], queryFn: api.listModels })
  const documentsQuery = useQuery({ queryKey: ['documents'], queryFn: api.listDocuments })

  const [harnessId, setHarnessId] = useState('')
  const [taskType, setTaskType] = useState('freeform')
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [freeText, setFreeText] = useState('')
  const [docIds, setDocIds] = useState<Set<string>>(new Set())
  const [modelOverride, setModelOverride] = useState('')

  // Default to the first harness once loaded.
  useEffect(() => {
    const list = harnessesQuery.data
    if (list && list.length > 0) {
      setHarnessId((curr) => curr || list[0].id)
    }
  }, [harnessesQuery.data])

  const harnessDetailQuery = useQuery({
    queryKey: ['harness', harnessId],
    queryFn: () => api.getHarness(harnessId),
    enabled: !!harnessId,
  })

  const taskTypes: TaskType[] = useMemo(() => {
    const packTypes = harnessDetailQuery.data?.task_types ?? []
    return [...packTypes, FREEFORM]
  }, [harnessDetailQuery.data])

  // When the harness changes, pick its task_profile if it names a pack task
  // type, else freeform; and reset per-task inputs.
  useEffect(() => {
    const detail = harnessDetailQuery.data
    if (!detail) return
    const available = (detail.task_types ?? []).map((t) => t.slug)
    setTaskType(available.includes(detail.task_profile) ? detail.task_profile : 'freeform')
    setInputs({})
  }, [harnessDetailQuery.data])

  const selectedTask = taskTypes.find((t) => t.slug === taskType) ?? FREEFORM
  const schema: Record<string, InputFieldSchema> = selectedTask.input_schema ?? {}
  const isFreeform = selectedTask.slug === 'freeform'

  const runMutation = useMutation({
    mutationFn: () => {
      const task_input: Record<string, unknown> = isFreeform ? { message: freeText } : {}
      if (!isFreeform) {
        for (const [field, spec] of Object.entries(schema)) {
          const raw = inputs[field] ?? ''
          if (raw === '') continue
          task_input[field] = spec.type === 'number' || spec.type === 'integer' ? Number(raw) : raw
        }
      }
      return api.createRun({
        harness_id: harnessId,
        task_type: selectedTask.slug,
        task_input,
        document_ids: [...docIds],
        model_override: modelOverride || undefined,
      })
    },
    onSuccess: (data) => navigate(`/runs/${data.run_id}`),
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!harnessId) return
    runMutation.mutate()
  }

  const models = modelsQuery.data ?? []
  const documents = documentsQuery.data ?? []
  // The policy a per-request override is confined to. Undefined until the
  // harness detail loads, which is the honest state: filtering the picker
  // against a policy we have not read yet would be guessing.
  const harnessPolicy = harnessDetailQuery.data?.model_policy
  const overrideProblem = overrideWarning(modelOverride, models, harnessPolicy)
  const canRun =
    !!harnessId && !runMutation.isPending && (isFreeform ? freeText.trim().length > 0 : true)

  return (
    <div>
      <h1 className="view-title">Workbench</h1>
      <div className="view-sub">
        Launch a harness task. Output is a draft until a human approves it.
      </div>

      {harnessesQuery.isLoading ? (
        <div className="empty pulse">Loading harnesses…</div>
      ) : harnessesQuery.isError ? (
        <QueryError error={harnessesQuery.error} what="the harness list" />
      ) : (harnessesQuery.data ?? []).length === 0 ? (
        <div className="empty">
          {/* A router Link, not a raw anchor: an <a href> here reloaded the whole
              SPA — new bundle, lost react-query cache, re-auth round trip — to
              reach a route the router already owns. */}
          No harnesses yet — create one under <Link to="/harnesses">Harnesses</Link> first.
        </div>
      ) : (
        <form onSubmit={submit} style={{ maxWidth: 720 }}>
          <div className="panel stack" style={{ gap: 0 }}>
            <div className="field">
              <label className="mono-label">Harness</label>
              <select value={harnessId} onChange={(e) => setHarnessId(e.target.value)}>
                {(harnessesQuery.data ?? []).map((h) => (
                  <option key={h.id} value={h.id}>
                    {h.name}
                    {h.pack_slugs.length > 0 ? ` — ${h.pack_slugs.join(', ')}` : ''}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label className="mono-label">Task type</label>
              <select
                value={taskType}
                onChange={(e) => {
                  setTaskType(e.target.value)
                  setInputs({})
                }}
              >
                {taskTypes.map((t) => (
                  <option key={t.slug} value={t.slug}>
                    {t.display_name ?? t.slug}
                    {t.shape ? ` (${t.shape})` : ''}
                  </option>
                ))}
              </select>
              {selectedTask.output_contract && (
                <div style={{ fontSize: 11.5, color: 'var(--text-muted)', fontFamily: 'var(--mono)' }}>
                  {selectedTask.output_contract}
                </div>
              )}
            </div>

            {isFreeform ? (
              <div className="field">
                <label className="mono-label">Message</label>
                <textarea
                  rows={5}
                  value={freeText}
                  onChange={(e) => setFreeText(e.target.value)}
                  placeholder="What should the harness do?"
                />
              </div>
            ) : (
              <SchemaFields schema={schema} values={inputs} onChange={setInputs} />
            )}

            <div className="field">
              <label className="mono-label">Attached documents ({docIds.size})</label>
              {documents.length === 0 ? (
                <div className="empty" style={{ padding: '6px 0' }}>
                  No documents uploaded — add some under Documents.
                </div>
              ) : (
                <div style={{ maxHeight: 160, overflowY: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 5, padding: '6px 10px' }}>
                  {documents.map((d) => (
                    <label key={d.id} className="check-row">
                      <input
                        type="checkbox"
                        checked={docIds.has(d.id)}
                        onChange={(e) => {
                          setDocIds((prev) => {
                            const next = new Set(prev)
                            if (e.target.checked) next.add(d.id)
                            else next.delete(d.id)
                            return next
                          })
                        }}
                      />
                      <span>{d.filename}</span>
                      <span className="desc">
                        {d.extraction_status === 'done' ? `${d.text_chars.toLocaleString()} chars` : d.extraction_status}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>

            <div className="field">
              <label className="mono-label">Model override (optional)</label>
              <ModelSelect
                models={models}
                value={modelOverride}
                onChange={setModelOverride}
                emptyLabel="— route automatically —"
                policy={harnessPolicy}
              />
              {overrideProblem && (
                <div className="error-text" style={{ marginTop: 4 }}>
                  {overrideProblem}
                </div>
              )}
            </div>

            {runMutation.isError && (
              <div className="error-text" style={{ marginBottom: 12 }}>
                {(runMutation.error as Error).message}
              </div>
            )}

            <div className="row">
              <button className="btn btn-primary" type="submit" disabled={!canRun}>
                {runMutation.isPending ? 'Starting…' : 'Run'}
              </button>
              {harnessDetailQuery.data && (
                <span
                  className="mono-label"
                  title={objectiveDescription(harnessDetailQuery.data.model_policy.objective)}
                >
                  policy: {harnessDetailQuery.data.model_policy.mode}
                  {harnessDetailQuery.data.model_policy.mode === 'pinned' &&
                    harnessDetailQuery.data.model_policy.model &&
                    ` → ${shortModelName(harnessDetailQuery.data.model_policy.model)}`}
                  {' · '}
                  {harnessDetailQuery.data.model_policy.objective ?? DEFAULT_OBJECTIVE}
                </span>
              )}
            </div>
          </div>
        </form>
      )}
    </div>
  )
}

/** Form fields generated from a task type's flat input_schema. */
function SchemaFields({
  schema,
  values,
  onChange,
}: {
  schema: Record<string, InputFieldSchema>
  values: Record<string, string>
  onChange: (v: Record<string, string>) => void
}) {
  const entries = Object.entries(schema)
  if (entries.length === 0) {
    return <div className="empty" style={{ padding: '4px 0 12px' }}>This task type takes no parameters.</div>
  }
  const set = (field: string, value: string) => onChange({ ...values, [field]: value })
  return (
    <>
      {entries.map(([field, spec]) => (
        <div className="field" key={field}>
          <label className="mono-label">
            {field}
            {spec.type ? ` · ${spec.type}` : ''}
          </label>
          {spec.enum ? (
            <select value={values[field] ?? ''} onChange={(e) => set(field, e.target.value)}>
              <option value="">— select —</option>
              {spec.enum.map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          ) : spec.type === 'number' || spec.type === 'integer' ? (
            <input
              type="number"
              value={values[field] ?? ''}
              onChange={(e) => set(field, e.target.value)}
              placeholder={spec.description}
            />
          ) : (
            <input
              type="text"
              value={values[field] ?? ''}
              onChange={(e) => set(field, e.target.value)}
              placeholder={spec.description}
            />
          )}
          {spec.description && (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--mono)' }}>
              {spec.description}
            </div>
          )}
        </div>
      ))}
    </>
  )
}

/** Price line for a model. Local models are free in dollars, which must read as
 *  "free", not as a missing price. */
export function modelPriceLabel(m: ModelInfo): string {
  if (m.input_price_per_mtok === 0 && m.output_price_per_mtok === 0) {
    return m.cost_tier === 'local' ? 'free (local weights)' : 'no published price'
  }
  return `$${m.input_price_per_mtok}/${m.output_price_per_mtok} per Mtok`
}

/** Why a model can't be selected — "local" has a base URL, not a key. */
export function modelUnavailableLabel(m: ModelInfo): string {
  return m.provider === 'local' ? 'no local base URL' : 'no key'
}

/** Why a selected per-request override would be refused by the router, or null.
 *
 *  Rendered next to the picker rather than discovered after a wasted run. The
 *  cases are distinct on purpose: a model the catalog no longer has fails
 *  differently from one the harness policy excludes, and telling the user which
 *  is the difference between a fixable message and a mystery. */
export function overrideWarning(
  value: string,
  models: ModelInfo[],
  policy?: ModelPolicy,
): string | null {
  if (!value) return null
  const model = models.find((m) => m.id === value)
  if (!model) {
    return `'${value}' is not in the model catalog — the run would fail. Pick another model.`
  }
  if (!model.available) {
    return `'${model.display_name}' has no configured credential (${modelUnavailableLabel(model)}) — the run would fail.`
  }
  if (!overrideAllowedByPolicy(model, policy)) {
    const allowed = policy?.allowed ?? []
    const reason =
      allowed.length > 0 && !allowed.includes(model.id)
        ? "it is not on this harness's allowed model list"
        : `its cost tier '${model.cost_tier}' is above this harness's ceiling '${policy?.max_cost_tier ?? DEFAULT_MAX_COST_TIER}'`
    return (
      `'${model.display_name}' cannot be used for this harness: ${reason}. A per-request ` +
      'override may only choose among the models the harness policy already permits — ' +
      'widening it is a harness setting. The run would fail with RoutingUnavailable.'
    )
  }
  return null
}

/** Model picker grouped by provider; unavailable providers are disabled.
 *
 *  With `policy`, the list is narrowed to what a per-request `model_override` may
 *  actually name under that harness policy (see `overrideAllowedByPolicy`), so a
 *  user cannot pick a model the router is going to refuse. Omit `policy` where
 *  the selection is *not* a per-request override — the harness builder's own
 *  `mode: pinned` model is written on the harness and is deliberately allowed to
 *  exceed that harness's ceiling, so it must not be filtered here.
 *
 *  A value that is already selected but no longer permitted stays visible as a
 *  disabled option instead of vanishing: dropping it would make the select fall
 *  back to whatever sits first in the list, silently running a different model
 *  than the one the user chose. */
export function ModelSelect({
  models,
  value,
  onChange,
  emptyLabel,
  policy,
}: {
  models: ModelInfo[]
  value: string
  onChange: (v: string) => void
  emptyLabel?: string
  policy?: ModelPolicy
}) {
  const permitted = policy ? models.filter((m) => overrideAllowedByPolicy(m, policy)) : models
  const selectionExcluded = !!value && !permitted.some((m) => m.id === value)
  const excluded = selectionExcluded ? models.find((m) => m.id === value) : undefined
  const listed = excluded ? [...permitted, excluded] : permitted
  const providers = [...new Set(listed.map((m) => m.provider))]
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{emptyLabel ?? '— none —'}</option>
      {/* A stored id the catalog no longer has: kept as its own disabled option
          so the select still reflects the stored choice rather than appearing to
          have been set to something else. */}
      {selectionExcluded && !excluded && (
        <option value={value} disabled>
          {value} (not in the catalog)
        </option>
      )}
      {providers.map((p) => (
        <optgroup key={p} label={p}>
          {listed
            .filter((m) => m.provider === p)
            .map((m) => {
              const outsidePolicy = m.id === excluded?.id
              return (
                <option key={m.id} value={m.id} disabled={!m.available || outsidePolicy}>
                  {m.display_name} · {m.cost_tier} · {modelPriceLabel(m)} · energy {m.energy_class}
                  {outsidePolicy
                    ? ' (outside the harness policy)'
                    : m.available
                      ? ''
                      : ` (${modelUnavailableLabel(m)})`}
                </option>
              )
            })}
        </optgroup>
      ))}
    </select>
  )
}
