/** Chat card for a batch started from a thread by the `start_batch` tool.
 *  Parses the tool's JSON result defensively, shows name / targets / status /
 *  cost so far with a link to the Batches page, and polls `getBatch` every 10 s
 *  until the batch reaches a terminal status or the card unmounts. */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api, type Batch } from '../../api/client'
import { formatCost } from '../shared/format'
import { StatusBadge } from '../shared/StatusBadge'

const TERMINAL = new Set(['completed', 'finalized', 'failed', 'paused'])
const POLL_MS = 10_000

export interface StartedBatch {
  id: string
  name: string | null
  targets: number | null
  status: string | null
  profile: string | null
  warningsCount: number | null
}

/** `start_batch`'s result as a string or object -> the batch it started, or
 *  null when it is not a successful start (no `batch_id`, bad JSON). */
export function parseStartBatchResult(raw: unknown): StartedBatch | null {
  let v: unknown = raw
  if (typeof raw === 'string') {
    try {
      v = JSON.parse(raw)
    } catch {
      return null
    }
  }
  if (!v || typeof v !== 'object') return null
  const o = v as Record<string, unknown>
  if (typeof o.batch_id !== 'string' || !o.batch_id) return null
  return {
    id: o.batch_id,
    name: typeof o.name === 'string' ? o.name : null,
    targets: typeof o.targets === 'number' ? o.targets : null,
    status: typeof o.status === 'string' ? o.status : null,
    profile: typeof o.profile === 'string' ? o.profile : null,
    warningsCount: typeof o.warnings_count === 'number' ? o.warnings_count : null,
  }
}

export function BatchCard({ started }: { started: StartedBatch }) {
  const { data: batch } = useQuery<Batch>({
    queryKey: ['chat-batch-card', started.id],
    queryFn: () => api.getBatch(started.id),
    refetchInterval: (q) => {
      const status = q.state.data?.status
      return status && TERMINAL.has(status) ? false : POLL_MS
    },
  })
  const status = batch?.status ?? started.status ?? 'running'
  const name = batch?.name ?? started.name ?? 'Batch'
  const targets = batch?.counts.total ?? started.targets
  const profile = batch?.profile ?? started.profile
  const warnings = batch?.warnings?.length ?? started.warningsCount ?? 0
  return (
    <div className="bc-card">
      <div className="bc-head">
        <Link className="bc-name" to={`/batches/${started.id}`}>
          {name}
        </Link>
        <StatusBadge status={status} />
      </div>
      <div className="bc-meta">
        {profile && <span className="bc-profile">{profile}</span>}
        {targets !== null && <span>{targets} {targets === 1 ? 'target' : 'targets'}</span>}
        {batch && <span>{formatCost(batch.cost_usd)} so far</span>}
        {warnings > 0 && (
          <Link className="bc-link-warn" to={`/batches/${started.id}`}>
            {warnings} {warnings === 1 ? 'warning' : 'warnings'}
          </Link>
        )}
        <Link className="bc-link" to={`/batches/${started.id}`}>
          Open batch →
        </Link>
      </div>
    </div>
  )
}

/** Cards for every successful `start_batch` result in a list of results. */
export function BatchCards({ results }: { results: Array<{ tool: string; result: unknown }> }) {
  const seen = new Set<string>()
  const cards: StartedBatch[] = []
  for (const r of results) {
    if (r.tool !== 'start_batch') continue
    const s = parseStartBatchResult(r.result)
    if (s && !seen.has(s.id)) {
      seen.add(s.id)
      cards.push(s)
    }
  }
  if (cards.length === 0) return null
  return (
    <>
      {cards.map((s) => (
        <BatchCard key={s.id} started={s} />
      ))}
    </>
  )
}
