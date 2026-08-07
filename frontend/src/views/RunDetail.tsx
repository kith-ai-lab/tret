import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, type Msg, type RunDetail } from '../api/client'
import { type StreamConnection, type StreamItem, useRunStream } from '../api/useRunStream'
import { ContextComposition } from '../components/shared/ContextComposition'
import { EnergyDetail } from '../components/shared/EnergyDetail'
import { BAND_SHORT } from '../components/shared/emissions'
import { footprintText, formatCo2eBand, formatCost, formatTokens } from '../components/shared/format'
import { LiveFootprint } from '../components/shared/LiveFootprint'
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
  // Streamed at second one of the run; the persisted copy is the fallback for a
  // finished run (and for a live one whose composition frame we missed).
  const composition = stream.composition ?? run.context_composition
  const streamedFindings = stream.items.filter((i) => i.kind === 'finding_recorded')
  const findings = findingsQuery.data ?? []

  // Replay path: derive text + tool activity from the persisted transcript.
  const replay = !isLive ? deriveReplay(run) : null
  const text = isLive ? stream.text : (replay?.text ?? '')
  const items: StreamItem[] = isLive ? stream.items : (replay?.items ?? [])

  // Live views prefer the streamed running totals; a finished run reads from the
  // persisted record. Energy figures are estimates in both paths.
  const live = isLive ? stream.usage : null
  const inputTokens = live?.input_tokens ?? run.input_tokens
  const outputTokens = live?.output_tokens ?? run.output_tokens
  const cacheReadTokens = live?.cache_read_tokens ?? run.cache_read_tokens
  const cacheWriteTokens = live?.cache_write_tokens ?? run.cache_write_tokens
  const costUsd = live?.cost_usd ?? run.cost_usd
  const energyWh = live?.energy_wh ?? run.energy_wh
  const co2eG = live?.co2e_g ?? run.co2e_g
  // The judgment band travels with the carbon figure everywhere it is shown, so
  // no surface prints a bare point estimate. Null on runs recorded before it.
  const co2eGLow = live?.co2e_g_low ?? run.co2e_g_low
  const co2eGHigh = live?.co2e_g_high ?? run.co2e_g_high
  const iterations = live?.iteration ?? run.iterations
  const footprint = footprintText(energyWh, co2eG)
  const footprintBand = formatCo2eBand(co2eGLow, co2eGHigh)

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

      {/* Live footprint — always rendered while the run streams, with stable
          numeric widths so an update never reflows what is below it. */}
      {isLive && (
        <div className="panel" style={{ padding: '10px 14px' }}>
          <LiveFootprint usage={stream.usage} label="live footprint (est., cumulative)" />
        </div>
      )}

      {/* A dropped connection is a connection problem, not a failed run: it is
          reported here, in transport language, and never as an error. */}
      {isLive && <StreamConnectionNote connection={stream.connection} />}

      {/* The engine crossed the run's soft output budget and asked the model to
          finalize. Not a failure — but the user should know why an answer is
          being wrapped up. */}
      {stream.budget && (
        <div className="panel" style={{ borderColor: 'var(--amber)', padding: '8px 12px' }}>
          <span className="mono-label" style={{ color: 'var(--amber)' }}>
            output budget reached
          </span>{' '}
          <span className="mono-body" style={{ fontSize: 11.5 }}>
            {formatTokens(stream.budget.output_tokens)} of {formatTokens(stream.budget.budget)}{' '}
            budgeted output tokens — the model has been asked to finalize with what it already
            retrieved.
          </span>
        </div>
      )}

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

      {/* Context composition — where the prompt tokens went */}
      {composition && <ContextComposition composition={composition} />}

      {/* Provenance + footer */}
      <ProvenanceCard
        model={routing?.chosen_model ?? run.model_used}
        doctrineSha={run.doctrine_sha}
        documentCount={run.document_ids.length}
        inputTokens={inputTokens}
        outputTokens={outputTokens}
        cacheReadTokens={cacheReadTokens}
        cacheWriteTokens={cacheWriteTokens}
        costUsd={costUsd}
        energyWh={energyWh}
        co2eG={co2eG}
        co2eGLow={co2eGLow}
        co2eGHigh={co2eGHigh}
        avoidedUsd={live?.avoided_usd ?? run.avoided_usd}
        avoidedUsdPct={live?.avoided_usd_pct ?? run.avoided_usd_pct}
      />

      {run.energy && <EnergyDetail energy={run.energy} />}

      <div className="row mono-label" style={{ gap: 24, flexWrap: 'wrap' }}>
        <span>iterations {iterations}</span>
        <span>
          tokens {formatTokens(inputTokens)} in / {formatTokens(outputTokens)} out
        </span>
        {(cacheReadTokens > 0 || cacheWriteTokens > 0) && (
          <span title="Prompt-cache tokens: reads bill at 0.1x the input price, writes at 1.25x.">
            cache {formatTokens(cacheReadTokens)} read / {formatTokens(cacheWriteTokens)} write
          </span>
        )}
        <span>cost {formatCost(costUsd)}</span>
        {footprint && (
          <span
            title={`Estimated from token counts and the model's calibrated energy class — not a measurement. ${
              footprintBand ? BAND_SHORT : ''
            }`}
          >
            {footprint}
            {footprintBand && ` · range ${footprintBand}`}
          </span>
        )}
        {run.started_at && run.finished_at && (
          <span>
            duration {((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000).toFixed(1)}s
          </span>
        )}
      </div>
    </div>
  )
}

/** Transport state, in transport words. `connecting` and `open` say nothing —
 *  they are the normal case. `reconnecting` explains why the panel above just
 *  emptied. `closed` on a run still marked live means the live view stopped
 *  updating, which is not the same claim as "the run failed". */
function StreamConnectionNote({ connection }: { connection: StreamConnection }) {
  if (connection === 'connecting' || connection === 'open') return null
  const reconnecting = connection === 'reconnecting'
  return (
    <div className="panel" style={{ borderColor: 'var(--amber)', padding: '8px 12px' }}>
      <span className={`mono-label${reconnecting ? ' pulse' : ''}`} style={{ color: 'var(--amber)' }}>
        {reconnecting ? 'reconnecting to the live stream' : 'live stream disconnected'}
      </span>{' '}
      <span className="mono-body" style={{ fontSize: 11.5 }}>
        {reconnecting
          ? 'The run is unaffected — it keeps executing on the server. Streamed output is replayed from the start once the connection is back.'
          : 'The run is unaffected and keeps executing on the server; this page has simply stopped receiving updates. Reload to catch up.'}
      </span>
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
  const manifest = !isCall && item.tool === 'run_method' ? methodManifest(item.result) : null
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
      {manifest && (
        <div
          style={{
            padding: '5px 10px',
            borderTop: '1px solid var(--border-subtle)',
            fontFamily: 'var(--mono)',
            fontSize: 10.5,
            color: 'var(--text-muted)',
          }}
        >
          manifest: {manifest.code_sha} → {manifest.output_hash} ({manifest.duration_ms}ms)
        </div>
      )}
    </details>
  )
}

/** Parse a run_method tool result for its manifest pin; null if unparseable. */
function methodManifest(
  result: string,
): { code_sha: string; output_hash: string; duration_ms: number } | null {
  try {
    const parsed: unknown = JSON.parse(result)
    if (!parsed || typeof parsed !== 'object' || !('method_run_id' in parsed)) return null
    const p = parsed as Record<string, unknown>
    return {
      code_sha: String(p.code_sha ?? '?'),
      output_hash: String(p.output_hash ?? '?'),
      duration_ms: Number(p.duration_ms ?? 0),
    }
  } catch {
    return null
  }
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
