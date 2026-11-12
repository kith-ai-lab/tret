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
}

const BLANK_ARM: ArmDraft = { label: '', model: '', objective: '', maxIterations: '' }
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

function armIn(a: ArmDraft): ExperimentArmIn {
  const out: ExperimentArmIn = {}
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
export function useExperimentForm(open: boolean, rowCount: number) {
  const [enabled, setEnabled] = useState(false)
  const [mode, setMode] = useState<ExperimentMode>('replicate')
  const [arms, setArms] = useState<ArmDraft[]>([{ ...BLANK_ARM }, { ...BLANK_ARM }])
  const [sampleN, setSampleN] = useState('')
  const modelsQuery = useQuery({
    queryKey: ['models'],
    queryFn: api.listModels,
    enabled: open && enabled,
    retry: false,
  })
  const modelIds = (modelsQuery.data ?? []).map((m) => m.id)

  const setArm = (i: number, patch: Partial<ArmDraft>) =>
    setArms((prev) => prev.map((a, j) => (j === i ? { ...a, ...patch } : a)))

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
    arms: arms.map(armIn),
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
                  onChange={(e) => setArm(i, { model: e.target.value })}
                />
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
