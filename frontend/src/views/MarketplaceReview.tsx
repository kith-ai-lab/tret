import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'

import { api, type HarnessPreset, type PackMethod, type ReviewQueueItem, type TaskType } from '../api/client'
import { formatDateTime } from '../components/shared/format'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { StatusBadge } from '../components/shared/StatusBadge'

/** Kith's review queue (Plan Phase B/E) — cloud-only, `require_admin` (Kith
 *  staff) on the tret-cloud side. `GET /api/marketplace/review/queue` 404s
 *  wholesale on a self-hosted build with no marketplace extension loaded, and
 *  403s for any authenticated user who is not Kith staff; either way this view
 *  does not apply, so a direct navigation here leaves rather than renders an
 *  error page — the nav item itself (App.tsx) is hidden by the same probe, so
 *  the ordinary path here is already gated before this component ever mounts. */
export function MarketplaceReview() {
  const queueQuery = useQuery({ queryKey: ['review-queue'], queryFn: api.reviewQueue, retry: false })

  if (queueQuery.isLoading) {
    return <div className="empty pulse">Loading review queue…</div>
  }
  if (queueQuery.isError || !queueQuery.data) {
    return <Navigate to="/" replace />
  }
  return <MarketplaceReviewBody queue={queueQuery.data} />
}

function MarketplaceReviewBody({ queue }: { queue: ReviewQueueItem[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(queue[0]?.id ?? null)

  useEffect(() => {
    if (queue.length > 0 && !queue.some((q) => q.id === selectedId)) {
      setSelectedId(queue[0].id)
    } else if (queue.length === 0) {
      setSelectedId(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue])

  return (
    <div>
      <h1 className="view-title">Marketplace review</h1>
      <div className="view-sub">Pending pack submissions awaiting Kith staff review before listing.</div>

      {queue.length === 0 ? (
        <div className="empty">Nothing in the queue.</div>
      ) : (
        <ListDetail
          listWidth={260}
          list={queue.map((q) => (
            <ListItem
              key={q.id}
              active={q.id === selectedId}
              onClick={() => setSelectedId(q.id)}
              title={
                <>
                  {q.slug} v{q.version}
                  {q.has_methods && (
                    <span className="badge badge-red" style={{ marginLeft: 6, fontSize: 'var(--fs-2xs)', padding: '1px 6px' }}>
                      methods
                    </span>
                  )}
                </>
              }
              // No publisher display name anywhere in this API — only the
              // submitting user's id, shown truncated the same way a
              // version id is shown elsewhere in this codebase.
              sub={`submitted by ${q.submitted_by.slice(0, 8)} · ${formatDateTime(q.submitted_at)}`}
            />
          ))}
          detail={selectedId ? <ReviewDetailPane key={selectedId} id={selectedId} /> : null}
        />
      )}
    </div>
  )
}

function MethodsReviewBanner({ methods }: { methods: PackMethod[] }) {
  return (
    <div className="callout callout-warn">
      <span className="callout-title">Executable code — elevated review required</span>
      This submission carries {methods.length} method{methods.length === 1 ? '' : 's'} of executable
      Python — only deterred by a static scan (packs/safety.py), never sandboxed. Read every
      entrypoint before approving.
      {methods.length > 0 && (
        <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
          {methods.map((m) => (
            <li key={m.slug}>
              {m.display_name ?? m.slug}
              {m.entrypoint ? ` — ${m.entrypoint}` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// Presets are config, not code — no elevated-review banner like methods get
// (MethodsReviewBanner above). Still worth a reviewer's eye: a preset's own
// `tools` next to the tools its declared task types actually grant, so a tool
// smuggled in beyond what the task type would allow stands out. This check is
// cosmetic only — computed client-side from data already on the page, nothing
// the backend enforces or that blocks a decision.
function HarnessPresetsReview({ presets, taskTypes }: { presets: HarnessPreset[]; taskTypes: TaskType[] }) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Harnesses ({presets.length})
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Description</th>
              <th>Tools</th>
              <th>Cost tier</th>
              <th>Task types</th>
            </tr>
          </thead>
          <tbody>
            {presets.map((p, i) => {
              const slugs = p.task_types ?? []
              // Union of tools the preset's own referenced task types grant.
              // Empty `slugs` means "no task type named to check against", so
              // nothing is flagged — there is no baseline to exceed.
              const union = new Set<string>()
              for (const slug of slugs) {
                const tt = taskTypes.find((t) => t.slug === slug)
                for (const tool of tt?.tools ?? []) union.add(tool)
              }
              // A referenced task type with an empty `tools: []` is not "this
              // preset may have zero tools" — the engine falls back to the
              // harness's own tool_names whenever a task declares none
              // (engine/harness.py:328: `task.get("tools") or harness.tool_names`).
              // So an empty union here means "no baseline was declared to
              // check against", exactly like the `slugs.length === 0` case
              // above, not "nothing is granted" — without the `union.size > 0`
              // guard, every one of the preset's own tools would be flagged
              // as unauthorized purely because the task type it references
              // happens to leave `tools` empty.
              const extra = new Set(
                slugs.length > 0 && union.size > 0 ? (p.tools ?? []).filter((t) => !union.has(t)) : []
              )
              return (
                <tr key={`${p.name}-${i}`}>
                  <td style={{ whiteSpace: 'nowrap' }}>{p.name}</td>
                  <td style={{ color: 'var(--text-muted)' }}>{p.description ?? '—'}</td>
                  <td>
                    <span className="row" style={{ gap: 4, display: 'inline-flex', flexWrap: 'wrap' }}>
                      {(p.tools ?? []).map((t) => (
                        <span
                          key={t}
                          className="chip"
                          style={
                            extra.has(t)
                              ? { fontSize: 'var(--fs-2xs)', padding: '1px 7px', borderColor: 'var(--red-border)', color: 'var(--red)' }
                              : { fontSize: 'var(--fs-2xs)', padding: '1px 7px' }
                          }
                          title={
                            extra.has(t)
                              ? "Not granted by this preset's own task types — cosmetic check, not enforced"
                              : undefined
                          }
                        >
                          {t}
                        </span>
                      ))}
                    </span>
                  </td>
                  <td>{p.suggested_cost_tier ? <span className="chip">{p.suggested_cost_tier}</span> : '—'}</td>
                  <td style={{ color: 'var(--text-muted)' }}>{slugs.join(', ') || '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ReviewDetailPane({ id }: { id: string }) {
  const detailQuery = useQuery({ queryKey: ['review-detail', id], queryFn: () => api.reviewDetail(id) })
  const queryClient = useQueryClient()
  const [notes, setNotes] = useState('')

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['review-queue'] })
    queryClient.invalidateQueries({ queryKey: ['review-detail', id] })
  }

  const approveMutation = useMutation({
    mutationFn: () => api.reviewApprove(id, notes.trim() || undefined),
    onSuccess: () => {
      invalidate()
      setNotes('')
    },
  })
  const requestChangesMutation = useMutation({
    mutationFn: () => api.reviewRequestChanges(id, notes.trim()),
    onSuccess: () => {
      invalidate()
      setNotes('')
    },
  })
  const rejectMutation = useMutation({
    mutationFn: () => api.reviewReject(id, notes.trim()),
    onSuccess: () => {
      invalidate()
      setNotes('')
    },
  })

  if (detailQuery.isLoading) return <div className="empty pulse">Loading submission…</div>
  if (detailQuery.isError) return <div className="error-text">{(detailQuery.error as Error).message}</div>
  const d = detailQuery.data
  if (!d) return null

  const notesRequired = notes.trim().length === 0
  const anyPending = approveMutation.isPending || requestChangesMutation.isPending || rejectMutation.isPending
  const decisionError = (approveMutation.error ?? requestChangesMutation.error ?? rejectMutation.error) as
    | Error
    | null

  return (
    <div className="stack">
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="view-title" style={{ marginBottom: 2 }}>
            {d.slug} v{d.version}
          </h2>
          <div className="view-sub" style={{ marginBottom: 0 }}>
            submitted by {d.submitted_by.slice(0, 8)} · {formatDateTime(d.submitted_at)}
          </div>
        </div>
        <StatusBadge status={d.state} />
      </div>

      {d.has_methods && <MethodsReviewBanner methods={d.manifest.methods} />}

      {(d.manifest.harnesses ?? []).length > 0 && (
        <HarnessPresetsReview presets={d.manifest.harnesses!} taskTypes={d.manifest.task_types} />
      )}

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Checks
        </div>
        {/* `check_results` is one overall pass/fail plus the collected errors
            and the pinned integrity values (submission_checks.py's own
            output shape) — not a per-check list. */}
        <div className="row" style={{ alignItems: 'center', gap: 8, marginBottom: d.check_results.errors.length > 0 ? 8 : 0 }}>
          <span className={`badge ${d.check_results.valid ? 'badge-green' : 'badge-red'}`}>
            {d.check_results.valid ? 'pass' : 'fail'}
          </span>
          <span style={{ color: 'var(--text-muted)', fontSize: 'var(--fs-sm)' }}>
            {d.check_results.size_bytes.toLocaleString()} bytes · content{' '}
            {d.check_results.content_hash ? d.check_results.content_hash.slice(0, 12) : '—'} · doctrine{' '}
            {d.check_results.doctrine_sha ? d.check_results.doctrine_sha.slice(0, 12) : '—'}
          </span>
        </div>
        {d.check_results.errors.length > 0 && (
          <ul style={{ margin: 0, paddingLeft: 18, fontFamily: 'var(--mono)', fontSize: 'var(--fs-sm)' }}>
            {d.check_results.errors.map((e, i) => (
              <li key={i} style={{ color: 'var(--red)' }}>
                {e}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Manifest diff
        </div>
        {Object.keys(d.diff_against_previous_listed.manifest_changes).length === 0 ? (
          <div className="empty">
            No previously listed version to diff against — this is a first submission.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="mono-table">
              <tbody>
                {Object.entries(d.diff_against_previous_listed.manifest_changes).map(([field, change]) => (
                  <tr key={field}>
                    <td style={{ whiteSpace: 'nowrap' }}>{field}</td>
                    <td style={{ color: 'var(--text-muted)' }}>{JSON.stringify(change.previous ?? null)}</td>
                    <td>→</td>
                    <td>{JSON.stringify(change.current ?? null)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Doctrine diffs
        </div>
        {Object.keys(d.diff_against_previous_listed.doctrine_diffs).length === 0 ? (
          <div className="empty">No doctrine changes.</div>
        ) : (
          <div className="stack" style={{ gap: 10 }}>
            {Object.entries(d.diff_against_previous_listed.doctrine_diffs).map(([rel, diff]) => (
              <div key={rel}>
                <div className="mono-label" style={{ marginBottom: 4, opacity: 0.7 }}>
                  {rel}
                </div>
                <pre className="code-block" style={{ maxHeight: 320 }}>
                  {diff}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 8 }}>
          Decision
        </div>
        <div className="field">
          <label className="mono-label">Notes (required to request changes or reject)</label>
          <textarea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <button type="button" className="btn btn-approve" disabled={anyPending} onClick={() => approveMutation.mutate()}>
            {approveMutation.isPending ? 'Approving…' : 'Approve'}
          </button>
          <button
            type="button"
            className="btn"
            disabled={anyPending || notesRequired}
            onClick={() => requestChangesMutation.mutate()}
          >
            {requestChangesMutation.isPending ? 'Requesting…' : 'Request changes'}
          </button>
          <button
            type="button"
            className="btn btn-reject"
            disabled={anyPending || notesRequired}
            onClick={() => rejectMutation.mutate()}
          >
            {rejectMutation.isPending ? 'Rejecting…' : 'Reject'}
          </button>
        </div>
        {notesRequired && (
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)', marginTop: 6 }}>
            Notes are required before requesting changes or rejecting — approving alone may leave
            them empty.
          </div>
        )}
        {decisionError && (
          <div className="error-text" style={{ marginTop: 8 }}>
            {decisionError.message}
          </div>
        )}
      </div>
    </div>
  )
}
