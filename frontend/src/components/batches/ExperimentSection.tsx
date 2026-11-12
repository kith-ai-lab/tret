import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import {
  api,
  type CreateBatchBody,
  type CreateExperimentBody,
  type ExperimentArmIn,
  type ExperimentMode,
  OBJECTIVE_DESCRIPTIONS,
  ROUTING_OBJECTIVES,
} from '../../api/client'

/** The objective list is the harness model-policy one (client.ts ROUTING_OBJECTIVES). */
export function ObjectiveSelect({
  value,
  onChange,
  blankLabel = 'harness default',
}: {
  value: string
  onChange: (v: string) => void
  blankLabel?: string
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{blankLabel}</option>
      {ROUTING_OBJECTIVES.map((o) => (
        <option key={o} value={o} title={OBJECTIVE_DESCRIPTIONS[o]}>
          {o}
        </option>
      ))}
    </select>
  )
}

interface ArmDraft {
  label: string
  model: string
  objective: string
  maxIterations: string
  weight: string
}

const BLANK_ARM: ArmDraft = { label: '', model: '', objective: '', maxIterations: '', weight: '' }
const ARM_KEYS = ['A', 'B', 'C']

const MODE_TEXT: Record<ExperimentMode, string> = {
  replicate: 'Replicate: every arm runs the same rows, so the arms are compared row by row.',
  split: 'Split: each row runs once and the arms take turns, so it costs less but compares different rows.',
}

/** Balanced split: counts differ by at most one ("5, 4 and 4 rows per arm"). */
function splitCounts(rows: number, arms: number): string {
  const n = Math.max(arms, 1)
  const base = Math.floor(rows / n)
  const extra = rows % n
  const counts = Array.from({ length: n }, (_, i) => base + (i < extra ? 1 : 0))
  return `${counts.slice(0, -1).join(', ')} and ${counts[counts.length - 1]} rows per arm`
}

/** Resulting percentages of the filled shares, e.g. "A 80% · B 10% · C 10%"; null when none or unusable. */
function sharesText(arms: ArmDraft[]): string | null {
  const ws = arms.map((a) => Number(a.weight))
  if (arms.some((a) => a.weight.trim() === '') || ws.some((w) => !Number.isFinite(w) || w <= 0)) return null
  const total = ws.reduce((x, y) => x + y, 0)
  return ws.map((w, i) => `${ARM_KEYS[i]} ${Math.round((w / total) * 1000) / 10}%`).join(' · ')
}

function armIn(a: ArmDraft, withWeight: boolean): ExperimentArmIn {
  const out: ExperimentArmIn = {}
  if (withWeight && a.weight.trim()) out.weight = Number(a.weight)
  if (a.label.trim()) out.label = a.label.trim()
  if (a.model.trim()) out.model_override = a.model.trim()
  if (a.objective) out.objective = a.objective
  if (a.maxIterations.trim()) out.max_iterations = Number(a.maxIterations)
  return out
}

function armSignature(a: ArmDraft): string {
  return [a.model.trim(), a.objective, a.maxIterations.trim()].join('|')
}

/** State + fields of the "Run as an experiment" section of the New batch
 *  modal. `rowCount` is how many rows the batch will have (for the estimate);
 *  `valid` is false while the section is on and the arms would be refused. */
export function useExperimentForm(open: boolean, rowCount: number, harnessId: string | null = null) {
  const [enabled, setEnabled] = useState(false)
  const [mode, setMode] = useState<ExperimentMode>('replicate')
  const [arms, setArms] = useState<ArmDraft[]>([{ ...BLANK_ARM }, { ...BLANK_ARM }])
  const [sampleN, setSampleN] = useState('')
  // Per arm: the router's suggestion note or error, set only when the person asks.
  const [hints, setHints] = useState<Record<number, string>>({})
  const [asking, setAsking] = useState<number | null>(null)
  const modelsQuery = useQuery({
    queryKey: ['models'],
    queryFn: api.listModels,
    enabled: open && enabled,
    retry: false,
  })
  const modelIds = (modelsQuery.data ?? []).map((m) => m.id)

  const setArm = (i: number, patch: Partial<ArmDraft>) =>
    setArms((prev) => prev.map((a, j) => (j === i ? { ...a, ...patch } : a)))

  const askRouter = async (i: number) => {
    if (!harnessId) return
    setAsking(i)
    try {
      const r = await api.previewRouting({ harness_id: harnessId })
      const d = r.results?.[0]?.decision
      const candidate = d?.scored?.exploration?.candidate ?? d?.shadow?.exploration?.candidate ?? null
      if (!d?.scored && !d?.shadow) {
        setHints((h) => ({
          ...h,
          [i]: 'The scored ranking is not on for this agent, so the router has no candidate to suggest.',
        }))
      } else if (candidate) {
        setArm(i, { model: candidate })
        setHints((h) => ({ ...h, [i]: "the router's next untried model for this agent" }))
      } else {
        setHints((h) => ({ ...h, [i]: 'The router has no untried model to suggest for this agent.' }))
      }
    } catch {
      setHints((h) => ({ ...h, [i]: 'The router could not be asked just now.' }))
    } finally {
      setAsking(null)
    }
  }

  const sampleValue = sampleN.trim() === '' ? null : Number(sampleN)
  const problems: string[] = []
  if (enabled) {
    if (arms.length < 2 || arms.length > 3) problems.push('An experiment needs two or three arms.')
    arms.forEach((a, i) => {
      const k = ARM_KEYS[i]
      if (!a.model.trim() && !a.objective && !a.maxIterations.trim())
        problems.push(`Arm ${k} must set a model, an objective or max iterations.`)
      if (a.maxIterations.trim()) {
        const n = Number(a.maxIterations)
        if (!Number.isInteger(n) || n < 1 || n > 50)
          problems.push(`Arm ${k}: max iterations must be 1 to 50.`)
      }
    })
    const sigs = arms.map(armSignature)
    if (new Set(sigs).size !== sigs.length && arms.every((a) => armSignature(a) !== '||'))
      problems.push('Arms must differ from each other.')
    if (mode === 'split') {
      const filled = arms.filter((a) => a.weight.trim() !== '')
      if (filled.length > 0 && filled.length < arms.length)
        problems.push('Give every arm a share or none.')
      if (filled.some((a) => !(Number(a.weight) > 0)))
        problems.push('Each share must be a number greater than 0.')
      if (filled.some((a) => Number(a.weight) > 1000)) problems.push('Each share must be at most 1000.')
    }
    if (mode === 'split' && sampleValue !== null)
      problems.push('A sample size is only for replicate mode.')
    if (mode === 'split' && rowCount > 0 && rowCount < arms.length)
      problems.push(`A split experiment needs at least as many rows as arms (${arms.length}).`)
    if (sampleValue !== null) {
      if (!Number.isInteger(sampleValue) || sampleValue < 2)
        problems.push('Sample size must be a whole number of at least 2.')
      else if (rowCount > 0 && sampleValue >= rowCount)
        problems.push(`Sample size must be smaller than the number of rows (${rowCount}).`)
    }
  }

  const rowsPerArm = mode === 'replicate' && sampleValue && sampleValue >= 2 ? Math.min(sampleValue, rowCount) : rowCount
  const estimate =
    mode === 'replicate'
      ? `This creates ${arms.length} batches (one per arm) of ${rowsPerArm} rows each: ${arms.length * rowsPerArm} runs in all.`
      : `This creates 1 batch of ${rowCount} rows, ${splitCounts(rowCount, arms.length)}: ${rowCount} runs in all.`

  const buildBody = (batch: CreateBatchBody): CreateExperimentBody => ({
    name: batch.name,
    mode,
    arms: arms.map((a) => armIn(a, mode === 'split')),
    ...(mode === 'replicate' && sampleValue !== null ? { sample_n: sampleValue } : {}),
    batch,
  })

  const fields = (
    <div className="panel" style={{ margin: '0 0 14px' }}>
      <label className="check-row">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        <span className="mono-label">Run as an experiment</span>
      </label>
      <div className="fine-print">
        Compare two or three ways of running the same work (a model, an objective, an iteration cap).
        Nothing changes your harness until a person makes an arm the default.
      </div>
      {enabled && (
        <div style={{ marginTop: 10 }}>
          <div className="stack" style={{ gap: 6, marginBottom: 10 }}>
            {(['replicate', 'split'] as const).map((m) => (
              <label key={m} className="check-row">
                <input
                  type="radio"
                  name="experiment-mode"
                  checked={mode === m}
                  onChange={() => setMode(m)}
                />
                <span>{MODE_TEXT[m]}</span>
              </label>
            ))}
          </div>
          <datalist id="experiment-models">
            {modelIds.map((id) => (
              <option key={id} value={id} />
            ))}
          </datalist>
          {arms.map((a, i) => (
            <div
              key={i}
              className="row"
              style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 8 }}
            >
              <div className="mono-label" style={{ width: 44 }}>
                Arm {ARM_KEYS[i]}
              </div>
              <div className="field">
                <label className="mono-label">Label</label>
                <input
                  type="text"
                  value={a.label}
                  placeholder="auto"
                  onChange={(e) => setArm(i, { label: e.target.value })}
                />
              </div>
              <div className="field">
                <label className="mono-label">Model override</label>
                <input
                  type="text"
                  list="experiment-models"
                  value={a.model}
                  onChange={(e) => {
                    setArm(i, { model: e.target.value })
                    setHints((h) => {
                      const { [i]: _drop, ...rest } = h
                      return rest
                    })
                  }}
                />
                {harnessId && (
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={asking !== null}
                    onClick={() => void askRouter(i)}
                  >
                    {asking === i ? 'Asking the router…' : "Use the router's next candidate"}
                  </button>
                )}
                {hints[i] && <span className="fine-print">{hints[i]}</span>}
              </div>
              <div className="field">
                <label className="mono-label">Objective</label>
                <ObjectiveSelect value={a.objective} onChange={(v) => setArm(i, { objective: v })} />
              </div>
              <div className="field">
                <label className="mono-label">Max iterations</label>
                <input
                  type="number"
                  min={1}
                  max={50}
                  style={{ width: 90 }}
                  value={a.maxIterations}
                  onChange={(e) => setArm(i, { maxIterations: e.target.value })}
                />
              </div>
              {mode === 'split' && (
                <div className="field">
                  <label className="mono-label">Share</label>
                  <input
                    type="number"
                    min={0}
                    step="any"
                    style={{ width: 90 }}
                    placeholder="equal"
                    value={a.weight}
                    onChange={(e) => setArm(i, { weight: e.target.value })}
                  />
                </div>
              )}
              {arms.length > 2 && (
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => setArms((prev) => prev.filter((_, j) => j !== i))}
                >
                  Remove
                </button>
              )}
            </div>
          ))}
          {arms.length < 3 && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setArms((prev) => [...prev, { ...BLANK_ARM }])}
            >
              Add arm C
            </button>
          )}
          {mode === 'split' && (
            <div className="fine-print" style={{ marginTop: 6 }}>
              {sharesText(arms) && <div>{sharesText(arms)}</div>}
              A small share makes that arm a challenger. The page recommends nothing for an arm with fewer than
              five scored rows.
            </div>
          )}
          {mode === 'replicate' && (
            <div className="field" style={{ marginTop: 10 }}>
              <label className="mono-label">Sample size (optional)</label>
              <input
                type="number"
                min={2}
                style={{ width: 120 }}
                value={sampleN}
                onChange={(e) => setSampleN(e.target.value)}
              />
              <span className="fine-print">
                Run only this many rows in every arm, spread evenly across the list. Blank runs all rows.
              </span>
            </div>
          )}
          <div className="mono-body" style={{ marginTop: 8 }}>
            {rowCount > 0 ? estimate : 'Add rows to see how many runs this creates.'}
          </div>
          {problems.map((p) => (
            <div key={p} className="error-text">
              {p}
            </div>
          ))}
        </div>
      )}
    </div>
  )

  return { enabled, valid: !enabled || problems.length === 0, fields, buildBody }
}
