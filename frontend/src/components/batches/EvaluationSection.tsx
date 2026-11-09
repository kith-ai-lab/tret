import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import {
  api,
  type BatchEvaluationBody,
  type BatchRubric,
  type BatchRubricCriterion,
  type RubricApplies,
} from '../../api/client'
import { JUDGE_PLACEHOLDER, keyFromLabel } from './evalHelpers'

const APPLIES: RubricApplies[] = ['all', 'written', 'gap']

/** Prompt rubric edits live as a list of rows without keys; keys derive from labels. */
function withKeys(rows: BatchRubricCriterion[]): BatchRubricCriterion[] {
  const taken = new Set<string>()
  return rows.map((r) => {
    const key = keyFromLabel(r.label || 'criterion', taken)
    taken.add(key)
    return { ...r, key }
  })
}

/** "Evaluate runs" section of the New batch forms (off by default).
 *  Returns the fields and the `evaluation` body to merge into the create
 *  request (null when off). The rubric is only sent when the prompt rubric was
 *  edited; otherwise the server uses its default. */
export function useEvaluationForm(open: boolean, profile: 'disclosure' | 'prompt') {
  const [enabled, setEnabled] = useState(false)
  const [judge, setJudge] = useState('')
  const [budget, setBudget] = useState('')
  const [edited, setEdited] = useState<BatchRubricCriterion[] | null>(null)

  const rubricQuery = useQuery<BatchRubric>({
    queryKey: ['batch-default-rubric', profile],
    queryFn: () => api.getDefaultRubric(profile),
    enabled: open && enabled,
    retry: false,
    staleTime: 5 * 60_000,
  })
  const base = rubricQuery.data ?? null
  const criteria = profile === 'prompt' ? (edited ?? base?.criteria ?? []) : (base?.criteria ?? [])

  const setRow = (i: number, patch: Partial<BatchRubricCriterion>) =>
    setEdited(criteria.map((c, j) => (j === i ? { ...c, ...patch } : c)))

  const budgetBad = budget.trim() !== '' && !(Number(budget) > 0)
  const rubricBad =
    enabled &&
    profile === 'prompt' &&
    edited !== null &&
    (edited.length < 2 ||
      edited.length > 8 ||
      edited.some(
        (c) =>
          !c.label.trim() ||
          !c.five.trim() ||
          !c.one.trim() ||
          !Number.isInteger(c.weight) ||
          c.weight < 1 ||
          c.weight > 100,
      ))
  const valid = !enabled || (!budgetBad && !rubricBad)

  let value: BatchEvaluationBody | null = null
  if (enabled) {
    value = {
      enabled: true,
      judge_model: judge.trim() || null,
      budget_usd: budget.trim() ? Number(budget) : null,
    }
    if (profile === 'prompt' && edited && base) {
      value.rubric = {
        acceptable_quality: base.acceptable_quality,
        criteria: withKeys(edited).map((c) => ({ ...c, label: c.label.trim() })),
        flags: base.flags,
      }
    }
  }

  const fields = (
    <div className="panel" style={{ marginBottom: 14 }}>
      <label className="check-row">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        <span>
          <strong>Evaluate runs</strong> — score each finished target against a rubric with a judge
          model
        </span>
      </label>
      {enabled && (
        <div className="stack" style={{ gap: 10, marginTop: 8 }}>
          <div className="row" style={{ gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
            <div className="field">
              <label className="mono-label">Judge model</label>
              <input
                type="text"
                value={judge}
                placeholder={JUDGE_PLACEHOLDER}
                onChange={(e) => setJudge(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="mono-label">Budget (USD, optional)</label>
              <input
                type="number"
                min={0}
                step="0.1"
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
              />
              <span className="fine-print">
                default: enough for every target at the expected cost, with 50% headroom
              </span>
              {budgetBad && <div className="error-text">Budget must be a positive number.</div>}
            </div>
          </div>

          <div>
            <div className="mono-label">
              Rubric{base ? ` · ${base.title}` : ''}
              {profile === 'prompt' && edited ? ' (edited)' : ''}
            </div>
            {rubricQuery.isLoading ? (
              <span className="fine-print pulse">Loading rubric…</span>
            ) : rubricQuery.error || !base ? (
              <span className="fine-print">
                The server will use its default rubric
                {profile === 'prompt' ? '; editing needs a server that serves it' : ''}.
              </span>
            ) : profile === 'disclosure' ? (
              <RubricTable criteria={criteria} acceptable={base.acceptable_quality} />
            ) : (
              <div className="stack" style={{ gap: 8, marginTop: 4 }}>
                {criteria.map((c, i) => (
                  <div key={i} className="panel" style={{ padding: '8px 10px' }}>
                    <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
                      <div className="field" style={{ flex: '2 1 160px' }}>
                        <label className="mono-label">Label</label>
                        <input
                          type="text"
                          maxLength={60}
                          value={c.label}
                          onChange={(e) => setRow(i, { label: e.target.value })}
                        />
                        <span className="fine-print">key {keyFromLabel(c.label || 'criterion')}</span>
                      </div>
                      <div className="field" style={{ width: 90 }}>
                        <label className="mono-label">Weight</label>
                        <input
                          type="number"
                          min={1}
                          max={100}
                          value={c.weight}
                          onChange={(e) => setRow(i, { weight: Math.round(Number(e.target.value)) })}
                        />
                      </div>
                      <div className="field" style={{ width: 110 }}>
                        <label className="mono-label">Applies</label>
                        <select
                          value={c.applies}
                          onChange={(e) => setRow(i, { applies: e.target.value as RubricApplies })}
                        >
                          {APPLIES.map((a) => (
                            <option key={a} value={a}>
                              {a}
                            </option>
                          ))}
                        </select>
                      </div>
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={criteria.length <= 2}
                        onClick={() => setEdited(criteria.filter((_, j) => j !== i))}
                      >
                        Remove
                      </button>
                    </div>
                    <div className="field">
                      <label className="mono-label">A 5 means</label>
                      <input
                        type="text"
                        maxLength={300}
                        value={c.five}
                        onChange={(e) => setRow(i, { five: e.target.value })}
                      />
                    </div>
                    <div className="field">
                      <label className="mono-label">A 1 means</label>
                      <input
                        type="text"
                        maxLength={300}
                        value={c.one}
                        onChange={(e) => setRow(i, { one: e.target.value })}
                      />
                    </div>
                  </div>
                ))}
                <div>
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={criteria.length >= 8}
                    onClick={() =>
                      setEdited([
                        ...criteria,
                        { key: '', label: '', weight: 10, applies: 'all', five: '', one: '' },
                      ])
                    }
                  >
                    Add criterion
                  </button>{' '}
                  <span className="fine-print">2–8 criteria; weights are relative.</span>
                </div>
                {rubricBad && (
                  <div className="error-text">
                    Each criterion needs a label, both anchors and an integer weight from 1 to 100.
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )

  return { fields, value, valid }
}

function RubricTable({
  criteria,
  acceptable,
}: {
  criteria: BatchRubricCriterion[]
  acceptable: number
}) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="mono-table">
        <thead>
          <tr>
            <th>Criterion</th>
            <th className="num">Weight</th>
            <th>Applies</th>
            <th>A 5 means</th>
            <th>A 1 means</th>
          </tr>
        </thead>
        <tbody>
          {criteria.map((c) => (
            <tr key={c.key}>
              <td>{c.label}</td>
              <td className="num">{c.weight}</td>
              <td>{c.applies}</td>
              <td style={{ overflowWrap: 'anywhere' }}>{c.five}</td>
              <td style={{ overflowWrap: 'anywhere' }}>{c.one}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="fine-print">Acceptable quality: {acceptable} and above.</div>
    </div>
  )
}
