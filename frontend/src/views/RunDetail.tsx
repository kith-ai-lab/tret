import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, type Msg, type RunDetail } from '../api/client'
import { type StreamItem, useRunStream } from '../api/useRunStream'
import { ProvenanceCard } from '../components/shared/ProvenanceCard'
import { RoutingBadge } from '../components/shared/RoutingBadge'
import { StatusBadge } from '../components/shared/StatusBadge'

const LIVE_STATUSES = ['queued', 'running']

export function RunDetailView() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()

  const runQuery = useQuery({
    queryKey: ['run', id],
    queryFn: () => api.getRun(id!),
    enabled: !!id,
  })

  const run = runQuery.data
  const isLive = !!run && LIVE_STATUSES.includes(run.status)
  const stream = useRunStream(isLive && id ? id : null)

  // When the live stream finishes, refetch the persisted run for the final
  // authoritative record.
  useEffect(() => {
    if (stream.done) {
      queryClient.invalidateQueries({ queryKey: ['run', id] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    }
  }, [stream.done, id, queryClient])

  const cancelMutation = useMutation({
    mutationFn: () => api.cancelRun(id!),
  })

  const harnessesQuery = useQuery({ queryKey: ['harnesses'], queryFn: api.listHarnesses })
  const findingsQuery = useQuery({
    queryKey: ['findings', { run_id: id }],
    queryFn: () => api.listFindings({ run_id: id! }),
    enabled: !!id && !!run && !isLive,
  })

  if (runQuery.isLoading) return <div className="empty pulse">Loading run…</div>
  if (runQuery.isError) return <div className="error-text">{(runQuery.error as Error).message}</div>
  if (!run) return null

  const harnessName =
    harnessesQuery.data?.find((h) => h.id === run.harness_id)?.name ?? run.harness_id.slice(0, 8)

  const routing = stream.routing ?? run.routing
  const status = isLive ? (stream.status ?? run.status) : run.status
  const streamedFindings = stream.items.filter((i) => i.kind === 'finding_recorded')
  const findings = findingsQuery.data ?? []

  // Replay path: derive text + tool activity from the persisted transcript.
  const replay = !isLive ? deriveReplay(run) : null
  const text = isLive ? stream.text : (replay?.text ?? '')
  const items: StreamItem[] = isLive ? stream.items : (replay?.items ?? [])

  const inputTokens = isLive ? (stream.usage?.input_tokens ?? run.input_tokens) : run.input_tokens
  const outputTokens = isLive ? (stream.usage?.output_tokens ?? run.output_tokens) : run.output_tokens
  const costUsd = isLive ? (stream.usage?.cost_usd ?? run.cost_usd) : run.cost_usd
  const iterations = isLive ? (stream.usage?.iteration ?? run.iterations) : run.iterations

  return (
    <div className="stack">
      {/* Header */}
      <div className="row" style={{ flexWrap: 'wrap' }}>
        <h1 className="view-title" style={{ marginBottom: 0 }}>
          {harnessName}
        </h1>
        <span className="chip">{run.task_type}</span>
        <StatusBadge status={status} />
        <RoutingBadge routing={routing} />
        <span style={{ flex: 1 }} />
        {LIVE_STATUSES.includes(status) && (
          <button
            className="btn btn-danger btn-sm"
            onClick={() => cancelMutation.mutate()}
            disabled={cancelMutation.isPending}
          >
            Cancel run
          </button>
        )}
      </div>

      {/* Task input */}
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Task input
        </div>
        <pre className="code-block" style={{ maxHeight: 140 }}>
          {JSON.stringify(run.task_input, null, 2)}
        </pre>
      </div>

      {/* Assistant output + tool activity */}
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          {isLive ? 'Output (streaming)' : 'Output'}
        </div>
        <div className="panel">
          {text ? (
            <pre className="prose-stream">{text}</pre>
          ) : (
            <div className="empty" style={{ padding: '4px 0' }}>
              {isLive ? 'Waiting for model output…' : 'No assistant text.'}
            </div>
          )}
        </div>
      </div>

      {items.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Tool activity ({items.length})
          </div>
          <div className="stack" style={{ gap: 4 }}>
            {items.map((item, i) => (
              <ToolItemRow key={i} item={item} />
            ))}
          </div>
        </div>
      )}

      {(stream.error || run.error) && (
        <div className="panel" style={{ borderColor: 'var(--red)' }}>
          <div className="mono-label" style={{ marginBottom: 6, color: 'var(--red)' }}>
            Error
          </div>
          <pre className="code-block" style={{ border: 'none', padding: 0, color: 'var(--red)' }}>
            {stream.error ?? run.error}
          </pre>
        </div>
      )}

      {/* Findings recorded */}
      {(streamedFindings.length > 0 || findings.length > 0) && (
        <div className="panel">
          <div className="mono-label" style={{ marginBottom: 8 }}>
            Findings recorded
          </div>
          {findings.length > 0 ? (
            <div className="stack" style={{ gap: 6 }}>
              {findings.map((f) => (
                <div key={f.id} className="row">
                  <StatusBadge status={f.status} />
                  <span className="mono-body">{f.schema_slug}</span>
                  <span className="mono-body" style={{ color: 'var(--text-muted)' }}>
                    {JSON.stringify(f.subject)}
                  </span>
                  <Link to="/approvals" className="mono-body">
                    review →
                  </Link>
                </div>
              ))}
            </div>
          ) : (
            <div className="mono-body">
              {streamedFindings.length} draft finding{streamedFindings.length === 1 ? '' : 's'} recorded —{' '}
              <Link to="/approvals">review in Approvals</Link>
            </div>
          )}
        </div>
      )}

      {/* Provenance + footer */}
      <ProvenanceCard
        model={routing?.chosen_model ?? run.model_used}
        doctrineSha={run.doctrine_sha}
        documentCount={run.document_ids.length}
        inputTokens={inputTokens}
        outputTokens={outputTokens}
        costUsd={costUsd}
      />

      <div className="row mono-label" style={{ gap: 24 }}>
        <span>iterations {iterations}</span>
        <span>
          tokens {inputTokens.toLocaleString()} in / {outputTokens.toLocaleString()} out
        </span>
        <span>cost ${costUsd.toFixed(4)}</span>
        {run.started_at && run.finished_at && (
          <span>
            duration {((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000).toFixed(1)}s
          </span>
        )}
      </div>
    </div>
  )
}

function ToolItemRow({ item }: { item: StreamItem }) {
  if (item.kind === 'finding_recorded') {
    return (
      <div className="tool-row" style={{ padding: '6px 10px' }}>
        <span className="badge badge-amber">draft finding</span>{' '}
        <span className="mono-body" style={{ fontSize: 11.5 }}>
          {item.finding_id}
        </span>
      </div>
    )
  }
  const isCall = item.kind === 'tool_call'
  const body = isCall ? JSON.stringify(item.arguments, null, 2) : item.result
  const truncated = body.length > 4000 ? `${body.slice(0, 4000)}\n… (truncated)` : body
  return (
    <details className={`tool-row${!isCall && item.error ? ' is-error' : ''}`}>
      <summary>
        <span style={{ color: 'var(--text-muted)' }}>{isCall ? '→' : '←'}</span>
        <span className="tool-name">{item.tool}</span>
        <span style={{ color: 'var(--text-muted)', fontSize: 10.5 }}>
          {isCall ? 'call' : item.error ? 'result · error' : 'result'}
        </span>
        <span
          style={{
            color: 'var(--text-muted)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            flex: 1,
            fontSize: 11,
          }}
        >
          {body.replace(/\s+/g, ' ').slice(0, 120)}
        </span>
      </summary>
      <div className="tool-body">{truncated}</div>
    </details>
  )
}

/** Rebuild the output text + tool activity from a persisted transcript. */
function deriveReplay(run: RunDetail): { text: string; items: StreamItem[] } {
  const textParts: string[] = []
  const items: StreamItem[] = []
  const toolNames = new Map<string, string>()
  for (const m of run.messages as Msg[]) {
    if (m.role === 'assistant') {
      if (m.content) textParts.push(m.content)
      for (const tc of m.tool_calls ?? []) {
        toolNames.set(tc.id, tc.name)
        items.push({ kind: 'tool_call', id: tc.id, tool: tc.name, arguments: tc.arguments })
      }
    } else if (m.role === 'tool') {
      items.push({
        kind: 'tool_result',
        id: m.tool_call_id ?? '',
        tool: toolNames.get(m.tool_call_id ?? '') ?? 'tool',
        error: Boolean(m.meta?.error),
        result: m.content ?? '',
      })
    }
  }
  return { text: textParts.join('\n\n'), items }
}
