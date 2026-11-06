import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { api, type CreatePromptBatchBody } from '../../api/client'
import {
  MAX_ROWS,
  parseCsv,
  renderTemplate,
  templatePlaceholders,
  templateProblems,
  type ParsedCsv,
} from './csv'
import { errText } from './shared'

interface Shared {
  name: string
  runSlug: string
}

/** State + fields of the prompt profile in the New batch modal. The modal owns
 *  name / run slug (shared with the disclosure form) and the submit button;
 *  this hook owns everything else and yields the request body once valid. */
export function usePromptBatchForm(open: boolean, shared: Shared) {
  const harnessesQuery = useQuery({
    queryKey: ['batch-harnesses'],
    queryFn: api.listHarnesses,
    enabled: open,
    retry: false,
  })
  const targetsQuery = useQuery({
    queryKey: ['m365-write-targets'],
    queryFn: api.m365WriteTargets,
    enabled: open,
    retry: false,
  })
  const harnesses = (harnessesQuery.data ?? []).filter((h) => !h.is_archived)
  const targets = targetsQuery.data?.targets ?? []
  const writeEnabled = targetsQuery.data?.write_enabled ?? false

  const [harnessId, setHarnessId] = useState('')
  const [template, setTemplate] = useState('')
  const [fileName, setFileName] = useState<string | null>(null)
  const [csv, setCsv] = useState<ParsedCsv | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [labelColumn, setLabelColumn] = useState('')
  const [slugColumn, setSlugColumn] = useState('')
  const [review, setReview] = useState(false)
  const [reviewSchema, setReviewSchema] = useState('any')
  const [concurrency, setConcurrency] = useState(3)
  const [firstBatch, setFirstBatch] = useState(0)
  const [targetSlug, setTargetSlug] = useState('')
  const [writeRoot, setWriteRoot] = useState('runs')
  const [model, setModel] = useState('')
  const [maxIterations, setMaxIterations] = useState('')

  const effectiveHarness = harnessId || harnesses[0]?.id || ''
  const columns = csv?.columns ?? []
  const effectiveLabel = columns.includes(labelColumn) ? labelColumn : (columns[0] ?? '')
  const effectiveSlugCol = columns.includes(slugColumn) ? slugColumn : ''
  const rows = csv?.rows ?? []

  const placeholders = useMemo(() => templatePlaceholders(template), [template])
  const syntaxProblems = useMemo(() => templateProblems(template), [template])
  const missing = csv ? placeholders.filter((p) => !columns.includes(p)) : []
  const preview = template.trim() && csv ? renderTemplate(template, rows[0]) : ''

  const loadFile = (file: File | undefined) => {
    if (!file) return
    setFileError(null)
    file.text().then(
      (text) => {
        setFileName(file.name)
        setCsv(parseCsv(text))
        setLabelColumn('')
        setSlugColumn('')
      },
      (e: unknown) => setFileError(`Could not read file: ${errText(e)}`),
    )
  }

  const reviewOk = !review || reviewSchema.trim() !== ''
  const valid =
    shared.name.trim() !== '' &&
    shared.runSlug.trim() !== '' &&
    effectiveHarness !== '' &&
    template.trim() !== '' &&
    syntaxProblems.length === 0 &&
    csv !== null &&
    csv.problems.length === 0 &&
    rows.length >= 1 &&
    rows.length <= MAX_ROWS &&
    missing.length === 0 &&
    reviewOk

  const body: CreatePromptBatchBody | null = valid
    ? {
        profile: 'prompt',
        name: shared.name.trim(),
        run_slug: shared.runSlug.trim(),
        harness_id: effectiveHarness,
        template,
        rows,
        label_column: effectiveLabel || null,
        slug_column: effectiveSlugCol || null,
        review_schema: review ? reviewSchema.trim() : null,
        concurrency,
        first_batch_size: firstBatch,
        target_slug: targetSlug || null,
        write_root: targetSlug ? writeRoot.trim() || 'runs' : null,
        model_override: model.trim() || null,
        max_iterations: maxIterations.trim() ? Number(maxIterations) : null,
      }
    : null

  const fields = (
    <>
      <div className="field">
        <label className="mono-label">Harness</label>
        {harnessesQuery.isLoading ? (
          <span className="fine-print pulse">Loading harnesses…</span>
        ) : harnesses.length === 0 ? (
          <span className="fine-print">No active harnesses in this workspace.</span>
        ) : (
          <select value={effectiveHarness} onChange={(e) => setHarnessId(e.target.value)}>
            {harnesses.map((h) => (
              <option key={h.id} value={h.id}>
                {h.name} — {h.task_profile}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="field">
        <label className="mono-label">Prompt template</label>
        <textarea
          rows={5}
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          spellCheck={false}
          placeholder={
            'Assess {company} ({country}) against our supplier criteria.\n\n' +
            'Put a CSV column name in braces to insert that row’s value. ' +
            'Write {{ and }} for a literal brace.'
          }
          style={{ fontFamily: 'var(--font-mono, monospace)' }}
        />
      </div>

      <div className="field">
        <label className="mono-label">Rows (CSV with a header row)</label>
        <input
          type="file"
          accept=".csv,text/csv"
          aria-label="Load rows from a CSV file"
          onChange={(e) => loadFile(e.target.files?.[0])}
        />
        {fileError && <div className="error-text">{fileError}</div>}
        {csv && (
          <div style={{ marginTop: 6 }}>
            <div className="fine-print">
              {fileName} · {rows.length} {rows.length === 1 ? 'row' : 'rows'} (max {MAX_ROWS})
            </div>
            <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginTop: 4 }}>
              {columns.map((c) => (
                <span key={c} className="chip">
                  {c || '(empty)'}
                </span>
              ))}
            </div>
            {csv.problems.map((p) => (
              <div key={p} className="error-text">
                {p}
              </div>
            ))}
          </div>
        )}
      </div>

      {csv && columns.length > 0 && (
        <div className="row" style={{ gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <div className="field">
            <label className="mono-label">Label column</label>
            <select value={effectiveLabel} onChange={(e) => setLabelColumn(e.target.value)}>
              {columns.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="mono-label">Slug column (optional)</label>
            <select value={effectiveSlugCol} onChange={(e) => setSlugColumn(e.target.value)}>
              <option value="">Derive from label</option>
              {columns.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      {template.trim() !== '' && (
        <div className="field">
          <label className="mono-label">First prompt</label>
          {csv && rows.length > 0 ? (
            <pre
              className="mono-body"
              style={{
                margin: 0,
                whiteSpace: 'pre-wrap',
                overflowWrap: 'anywhere',
                maxHeight: 200,
                overflowY: 'auto',
              }}
            >
              {preview}
            </pre>
          ) : (
            <span className="fine-print">Load a CSV to preview row 1.</span>
          )}
          {missing.length > 0 && (
            <div className="error-text">
              Not a column in the CSV:{' '}
              {missing.map((m) => `{${m}}`).join(', ')}
            </div>
          )}
          {syntaxProblems.map((p) => (
            <div key={p} className="error-text">
              {p}
            </div>
          ))}
        </div>
      )}

      <label className="check-row" style={{ marginBottom: 10 }}>
        <input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} />
        <span>Pause each target for review at its first draft finding</span>
      </label>
      {review && (
        <div className="field">
          <label className="mono-label">Finding type to review</label>
          <input
            type="text"
            value={reviewSchema}
            onChange={(e) => setReviewSchema(e.target.value)}
          />
          <span className="fine-print">
            any = pause at the first finding the run proposes (its approval requests, e.g. connected_write, or a pack finding type); or name one finding type.
          </span>
        </div>
      )}

      <div className="row" style={{ gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div className="field">
          <label className="mono-label">Concurrency (1–6)</label>
          <input
            type="number"
            min={1}
            max={6}
            value={concurrency}
            onChange={(e) => setConcurrency(Math.min(6, Math.max(1, Number(e.target.value) || 1)))}
          />
        </div>
        <div className="field">
          <label className="mono-label">First batch (0 = no gate)</label>
          <input
            type="number"
            min={0}
            value={firstBatch}
            onChange={(e) => setFirstBatch(Math.max(0, Number(e.target.value) || 0))}
          />
        </div>
        <div className="field">
          <label className="mono-label">Max iterations (1–50, blank = harness)</label>
          <input
            type="number"
            min={1}
            max={50}
            value={maxIterations}
            onChange={(e) => {
              const raw = e.target.value
              setMaxIterations(raw === '' ? '' : String(Math.min(50, Math.max(1, Number(raw) || 1))))
            }}
          />
        </div>
      </div>

      <div className="field">
        <label className="mono-label">Model override (optional)</label>
        <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
      </div>

      <div className="row" style={{ gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div className="field">
          <label className="mono-label">Write records to</label>
          {targetsQuery.isLoading ? (
            <span className="fine-print pulse">Loading write targets…</span>
          ) : (
            <select value={targetSlug} onChange={(e) => setTargetSlug(e.target.value)}>
              <option value="">Keep records as documents</option>
              {writeEnabled &&
                targets.map((t) => (
                  <option key={t.slug} value={t.slug}>
                    {t.label} — {t.path}
                  </option>
                ))}
            </select>
          )}
        </div>
        {targetSlug && (
          <div className="field">
            <label className="mono-label">Write root</label>
            <input type="text" value={writeRoot} onChange={(e) => setWriteRoot(e.target.value)} />
          </div>
        )}
      </div>
    </>
  )

  return { fields, body }
}
