/** What was checked on an assistant reply, and the result (Learning round R26),
 *  plus the saved-file chips (R24). Replaces the old footer lines. Each badge
 *  says what was checked; clicking it expands the details. */
import { type ReactNode, useState } from 'react'
import { Link } from 'react-router-dom'

import type { ChatMessage, ChatSavedFile, GroundingDetail } from '../../api/client'
import './chat-thread.css'

export type CheckTone = 'ok' | 'warn' | 'bad' | 'muted'

export interface CheckBadge {
  key: string
  label: string
  tone: CheckTone
  detail: ReactNode | null
}

const WHERE_LABEL: Record<string, string> = {
  prose: 'in the text',
  table: 'in a table',
  list: 'in a list',
  html: "in the file's visible text",
}

function describeUnsupported(detail: GroundingDetail): string {
  return `${detail.value} ${WHERE_LABEL[detail.where] ?? detail.where}, line ${detail.line}`
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** The badges for one message, in display order. Pure: tested directly. */
export function buildChecks(message: ChatMessage): CheckBadge[] {
  const badges: CheckBadge[] = []
  const g = message.grounding
  const guard = message.guardrails

  // ── Number check ──
  if (g) {
    if (g.status === 'off') {
      badges.push({
        key: 'numbers',
        label: 'Number check: off',
        tone: 'muted',
        detail: 'The number check was turned off in Settings → Guardrails, so figures in this reply were not checked against retrieved data.',
      })
    } else if (g.status === 'skipped' || !g.checked) {
      badges.push({
        key: 'numbers',
        label: 'Number check: skipped (nothing retrieved)',
        tone: 'muted',
        detail: 'No data was retrieved in this turn, so there was nothing to check figures against.',
      })
    } else if (g.status === 'unresolved') {
      const details = g.unsupported_detail ?? []
      const first = details[0]
      const count = details.length || g.unsupported.length
      badges.push({
        key: 'numbers',
        label: `Number check: ${count} unverified${first ? ` (${describeUnsupported(first)})` : ''}`,
        tone: 'bad',
        detail: (
          <>
            These figures were not found in any retrieved data:
            <ul>
              {details.length > 0
                ? details.map((d, i) => (
                    <li key={i}>
                      <code>{d.value}</code> {WHERE_LABEL[d.where] ?? d.where}, line {d.line}
                      {d.context && <> — “{d.context}”</>}
                    </li>
                  ))
                : g.unsupported.map((v, i) => (
                    <li key={i}>
                      <code>{v}</code>
                    </li>
                  ))}
            </ul>
          </>
        ),
      })
    } else if (g.status === 'repaired') {
      badges.push({
        key: 'numbers',
        label: 'Number check: rewritten once',
        tone: 'warn',
        detail: `An earlier draft cited figures that were not retrieved; the reply was rewritten (${plural(g.attempts, 'attempt')}).${
          g.first_unsupported && g.first_unsupported.length > 0 ? ` Removed: ${g.first_unsupported.join(', ')}.` : ''
        }`,
      })
    } else {
      badges.push({
        key: 'numbers',
        label: 'Number check: clean',
        tone: 'ok',
        detail: 'Every figure in the reply was found in retrieved data or a method result.',
      })
    }
  }

  // ── HTML check ──
  const html = guard?.html_check
  if (html && html.status !== 'skipped') {
    const issues = html.issues ?? []
    if (html.status === 'ok') {
      badges.push({ key: 'html', label: 'HTML check: ok', tone: 'ok', detail: `${plural(html.blocks ?? 1, 'HTML block')} checked.` })
    } else {
      const fixed = !!html.repaired
      badges.push({
        key: 'html',
        label: fixed ? `HTML check: ${plural(issues.length, 'issue')} fixed` : `HTML check: ${plural(issues.length, 'issue')}`,
        tone: fixed ? 'warn' : 'bad',
        detail: (
          <>
            {fixed ? 'Found and corrected by a repair pass:' : 'Left in the file:'}
            <ul>
              {issues.map((it, i) => (
                <li key={i}>
                  line {it.line}: {it.detail}
                </li>
              ))}
            </ul>
          </>
        ),
      })
    }
  }

  // ── Stop reasons ──
  if (message.truncated || message.stop_reason === 'max_tokens') {
    const limit = guard?.truncated?.max_output_tokens
    badges.push({
      key: 'cutoff',
      label: 'Cut off at the output limit',
      tone: 'bad',
      detail: `The model hit its output limit${limit ? ` (${limit.toLocaleString()} tokens)` : ''} before finishing. Use Continue to ask for the rest.`,
    })
  }
  if (message.stop_reason === 'out_of_steps') {
    badges.push({
      key: 'steps',
      label: 'Ran out of steps',
      tone: 'warn',
      detail: 'The run reached its step limit. Partial results were kept.',
    })
  }
  if (message.stop_reason === 'degenerate_output' || guard?.degenerate) {
    const d = guard?.degenerate
    badges.push({
      key: 'degenerate',
      label: message.stop_reason === 'degenerate_output' ? 'Garbled output — run stopped' : 'Garbled output — retried',
      tone: message.stop_reason === 'degenerate_output' ? 'bad' : 'warn',
      detail: d
        ? `The model started repeating “${d.pattern}” (${d.count}×)${d.retried_on ? `; the turn was retried on ${d.retried_on}` : ''}.`
        : 'The model produced repeating output.',
    })
  }

  // ── Unvetted calculations ──
  const unvettedRuns = message.analysis_runs ?? []
  const unvettedFigures = g?.unvetted ?? []
  if (unvettedRuns.length > 0 || unvettedFigures.length > 0) {
    badges.push({
      key: 'unvetted',
      label: `Unvetted calculation (${unvettedRuns.length || unvettedFigures.length})`,
      tone: 'warn',
      detail: (
        <>
          Computed by model-written code, not a vetted method. Treat these figures as unvetted.
          {unvettedRuns.length > 0 && (
            <ul>
              {unvettedRuns.map((r) => (
                <li key={r.id}>
                  {r.purpose} — {plural(r.row_count, 'row')}, {r.status}
                  {message.run_id && (
                    <>
                      {' '}
                      · <Link to={`/runs/${message.run_id}`}>view run</Link>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
          {unvettedFigures.length > 0 && (
            <div>
              Figures: {unvettedFigures.map((v, i) => (
                <code key={i}>{i > 0 ? `, ${v}` : v}</code>
              ))}
            </div>
          )}
        </>
      ),
    })
  }

  // ── Saved files ──
  const saved = message.saved_files ?? []
  if (saved.length > 0) {
    // Only an attempted upload that failed is a problem; waiting for approval
    // or a declined save is a neutral state, not an error.
    const anyFailed = saved.some((f) => f.status === 'failed')
    const anyWaiting = saved.some((f) => f.status === 'pending_approval')
    const anyDeclined = saved.some((f) => f.status === 'rejected')
    badges.push({
      key: 'saved',
      label: anyFailed
        ? 'Save to folder: problem'
        : anyWaiting
          ? 'Save to folder: waiting for approval'
          : anyDeclined
            ? 'Save to folder: declined'
            : `Saved to folder (${saved.length})`,
      tone: anyFailed ? 'bad' : anyWaiting || anyDeclined ? 'muted' : 'ok',
      detail: (
        <ul>
          {saved.map((f) => (
            <li key={f.finding_id}>
              <code>{f.filename}</code>{' '}
              {f.status === 'pending_approval'
                ? '(waiting for approval)'
                : f.status === 'rejected'
                  ? '(declined)'
                  : f.status !== 'uploaded'
                    ? `(${f.status})`
                    : ''}
              {f.path && <> — {f.path}</>}
              {f.sha256 && <> · sha256 <code>{f.sha256.slice(0, 12)}…</code></>}
            </li>
          ))}
        </ul>
      ),
    })
  }

  return badges
}

export function ChecksBar({ message }: { message: ChatMessage }) {
  const [open, setOpen] = useState<string | null>(null)
  const badges = buildChecks(message)
  if (badges.length === 0) return null
  const expanded = badges.find((b) => b.key === open)
  return (
    <div className="checks-bar">
      <div className="checks-badges">
        {badges.map((b) => (
          <button
            key={b.key}
            type="button"
            className={`check-badge ${b.tone}`}
            aria-expanded={open === b.key}
            onClick={() => setOpen(open === b.key ? null : b.key)}
            title="Click for details"
          >
            {b.label}
          </button>
        ))}
      </div>
      {expanded?.detail && <div className="check-detail">{expanded.detail}</div>}
    </div>
  )
}

/** "Saved week3.html to tret/ · audit" chips under the reply (R24). */
export function SavedFileChips({ files }: { files: ChatSavedFile[] | undefined }) {
  if (!files || files.length === 0) return null
  return (
    <div className="saved-files">
      {files.map((f) => {
        const failed = f.status === 'failed'
        const waiting = f.status === 'pending_approval'
        const declined = f.status === 'rejected'
        return (
          <span key={f.finding_id} className={`saved-file${failed ? ' failed' : ''}`}>
            {failed ? (
              <>Could not save {f.filename}</>
            ) : waiting ? (
              <>Waiting for approval to save {f.filename}</>
            ) : declined ? (
              <>Save of {f.filename} was declined</>
            ) : (
              <>
                Saved{' '}
                {f.web_url ? (
                  <a href={f.web_url} target="_blank" rel="noopener noreferrer">
                    {f.filename}
                  </a>
                ) : (
                  f.filename
                )}{' '}
                to tret/
                {f.audit_filename && (
                  <>
                    {' · '}
                    {f.audit_web_url ? (
                      <a
                        href={f.audit_web_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        title={f.audit_filename}
                      >
                        audit
                      </a>
                    ) : (
                      <span title={f.audit_filename}>audit</span>
                    )}
                  </>
                )}
              </>
            )}
          </span>
        )
      })}
    </div>
  )
}
