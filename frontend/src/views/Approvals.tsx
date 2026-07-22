import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { ProvenanceCard } from '../components/shared/ProvenanceCard'
import { StatusBadge } from '../components/shared/StatusBadge'
import { formatDateTime } from './Runs'

const TABS = ['draft', 'approved', 'rejected'] as const

export function Approvals() {
  const [tab, setTab] = useState<(typeof TABS)[number]>('draft')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const findingsQuery = useQuery({
    queryKey: ['findings', tab],
    queryFn: () => api.listFindings({ status: tab }),
  })

  const findings = findingsQuery.data ?? []

  // Keep a valid selection as the list changes.
  useEffect(() => {
    if (findings.length === 0) {
      setSelectedId(null)
    } else if (!findings.some((f) => f.id === selectedId)) {
      setSelectedId(findings[0].id)
    }
  }, [findings, selectedId])

  return (
    <div>
      <h1 className="view-title">Approvals</h1>
      <div className="view-sub">
        The blessing queue: drafts stay drafts until a named human decides.
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <button key={t} className={`tab${tab === t ? ' active' : ''}`} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>

      {findingsQuery.isLoading ? (
        <div className="empty pulse">Loading findings…</div>
      ) : findings.length === 0 ? (
        <div className="empty">
          {tab === 'draft'
            ? 'No draft findings awaiting review — run a task from the Workbench.'
            : `No ${tab} findings yet.`}
        </div>
      ) : (
        <ListDetail
          listWidth={260}
          list={findings.map((f) => (
            <ListItem
              key={f.id}
              active={f.id === selectedId}
              onClick={() => setSelectedId(f.id)}
              title={f.schema_slug}
              sub={`${subjectLine(f.subject)} · ${f.created_at ? formatDateTime(f.created_at) : ''}`}
            />
          ))}
          detail={selectedId ? <FindingDetailPane findingId={selectedId} /> : null}
        />
      )}
    </div>
  )
}

function subjectLine(subject: Record<string, unknown>): string {
  const parts = Object.entries(subject).map(([k, v]) => `${k}=${String(v)}`)
  return parts.join(' ') || '(no subject)'
}

function FindingDetailPane({ findingId }: { findingId: string }) {
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')

  const findingQuery = useQuery({
    queryKey: ['finding', findingId],
    queryFn: () => api.getFinding(findingId),
  })

  const decideMutation = useMutation({
    mutationFn: (action: 'approve' | 'reject') => api.decideFinding(findingId, action, note || undefined),
    onSuccess: () => {
      setNote('')
      queryClient.invalidateQueries({ queryKey: ['findings'] })
      queryClient.invalidateQueries({ queryKey: ['finding', findingId] })
    },
  })

  if (findingQuery.isLoading) return <div className="empty pulse">Loading finding…</div>
  if (findingQuery.isError)
    return <div className="error-text">{(findingQuery.error as Error).message}</div>
  const f = findingQuery.data
  if (!f) return null

  const retrieved = f.provenance.retrieved_values ?? []
  const decideError = decideMutation.error as ApiError | null

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: 'wrap' }}>
        <h2 className="view-title" style={{ marginBottom: 0 }}>
          {f.schema_slug}
        </h2>
        <StatusBadge status={f.status} />
        <span className="chip">{subjectLine(f.subject)}</span>
        <span style={{ flex: 1 }} />
        <Link to={`/runs/${f.run_id}`} className="mono-body">
          view run →
        </Link>
      </div>

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Payload
        </div>
        <pre className="code-block" style={{ maxHeight: 320 }}>
          {JSON.stringify(f.payload, null, 2)}
        </pre>
      </div>

      <ProvenanceCard
        model={f.provenance.model}
        doctrineSha={f.provenance.doctrine_sha}
        documentCount={(f.provenance.document_ids ?? []).length}
        retrievedCount={retrieved.length}
      />

      {retrieved.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Retrieved values ({retrieved.length})
          </div>
          <div style={{ maxHeight: 240, overflowY: 'auto' }}>
            <table className="mono-table">
              <thead>
                <tr>
                  <th>Dataset</th>
                  <th>Row</th>
                  <th>Column</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {retrieved.map((v, i) => (
                  <tr key={i}>
                    <td>{v.dataset}</td>
                    <td>{v.row_ref}</td>
                    <td>{v.column}</td>
                    <td>{v.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Approval history */}
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Approval history
        </div>
        {f.approvals.length === 0 ? (
          <div className="empty" style={{ padding: '4px 0' }}>
            No decisions yet.
          </div>
        ) : (
          <table className="mono-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Action</th>
                <th>Approver</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {f.approvals.map((a, i) => (
                <tr key={i}>
                  <td>{a.created_at ? formatDateTime(a.created_at) : '—'}</td>
                  <td>
                    <StatusBadge status={a.action === 'approve' ? 'approved' : 'rejected'} />
                  </td>
                  <td title={a.approver_id}>{a.approver_id.slice(0, 8)}</td>
                  <td>{a.note ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Decision controls */}
      {f.status === 'draft' && (
        <div className="panel">
          <div className="mono-label" style={{ marginBottom: 8 }}>
            Decide
          </div>
          <div className="field">
            <textarea
              rows={2}
              placeholder="Optional note for the audit record…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>
          {decideError && (
            <div className="error-text" style={{ marginBottom: 10 }}>
              {decideError.status === 403 ? 'Requires approver role.' : decideError.message}
            </div>
          )}
          <div className="row">
            <button
              className="btn btn-approve"
              onClick={() => decideMutation.mutate('approve')}
              disabled={decideMutation.isPending}
            >
              Approve
            </button>
            <button
              className="btn btn-reject"
              onClick={() => decideMutation.mutate('reject')}
              disabled={decideMutation.isPending}
            >
              Reject
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
