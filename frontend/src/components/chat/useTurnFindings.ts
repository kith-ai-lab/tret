/** The findings one chat turn produced, live and for past turns.
 *
 *  There is no per-conversation findings route and none is needed:
 *  `GET /api/findings?run_id=` already lists a run's findings (workspace-scoped),
 *  and a turn's work is spread over its own run plus the child runs it
 *  delegated to (`run_harness_task` / `delegate_parallel` / `spawn_subagent`
 *  write their findings under the child's run id). So the turn's findings are
 *  the list for `[turn run, ...child runs]`.
 *
 *  Live: the child ids come from the stream's delegations and the lists are
 *  re-fetched whenever a `finding_recorded` frame or a finished delegation
 *  arrives. Past turns: a turn whose persisted tool activity names no tool that
 *  can write a finding costs no request at all. */
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef } from 'react'

import { api, type ChatActivity, type Finding } from '../../api/client'
import type { useRunStream } from '../../api/useRunStream'
import { isResult } from './resultMeta'

type Stream = ReturnType<typeof useRunStream>

const DELEGATION_TOOLS = new Set(['run_harness_task', 'delegate_parallel', 'spawn_subagent'])
const FINDING_TOOLS = new Set([
  'draft_section',
  'record_finding',
  'record_verdict',
  'propose_connected_write',
])

export interface TurnFinding {
  finding: Finding
  /** Approved by workspace policy rather than a person. */
  auto: boolean
}

export function useTurnFindings(opts: {
  runId: string | null
  /** Persisted tool activity of a finished turn. */
  activity?: ChatActivity[]
  /** The live stream of a turn in flight. */
  live?: Stream
}): TurnFinding[] {
  const { runId, activity, live } = opts
  const queryClient = useQueryClient()

  // The stream state is a new object on every streamed token, so nothing below
  // may depend on `live` itself: only on these primitives, which change when a
  // finding is recorded, a delegation is added or one settles, or the turn ends.
  const isLive = !!live
  const liveFindingCount = live ? live.items.filter((i) => i.kind === 'finding_recorded').length : 0
  const liveChildKey = live ? live.delegations.map((d) => d.childRunId).join(',') : ''
  const liveSettledChildren = live ? live.delegations.filter((d) => d.status !== 'running').length : 0
  const liveRefreshKey = isLive ? `${liveFindingCount}:${liveSettledChildren}:${live?.done ? 1 : 0}` : ''

  const pastDelegates = !isLive && (activity ?? []).some((a) => DELEGATION_TOOLS.has(a.tool))
  const pastWrites = !isLive && (activity ?? []).some((a) => FINDING_TOOLS.has(a.tool))

  const childrenQuery = useQuery({
    queryKey: ['run-children', runId],
    queryFn: () => api.listRunChildren(runId as string),
    enabled: !!runId && pastDelegates,
    staleTime: 60_000,
  })

  const runIds = useMemo(() => {
    if (!runId) return []
    const ids: string[] = []
    if (isLive) {
      if (liveFindingCount > 0) ids.push(runId)
      if (liveChildKey) ids.push(...liveChildKey.split(','))
    } else {
      if (pastWrites) ids.push(runId)
      for (const c of childrenQuery.data ?? []) ids.push(c.id)
    }
    return Array.from(new Set(ids))
  }, [runId, isLive, liveFindingCount, liveChildKey, pastWrites, childrenQuery.data])

  // A new finding or a finished child changes what this turn's lists should say:
  // refetch exactly this turn's own run lists, never the whole prefix (other
  // turns' lists have not changed).
  const runIdsRef = useRef(runIds)
  runIdsRef.current = runIds
  useEffect(() => {
    if (!liveRefreshKey) return
    for (const id of runIdsRef.current) {
      void queryClient.invalidateQueries({ queryKey: ['run-findings', id], exact: true })
    }
  }, [liveRefreshKey, queryClient])

  const lists = useQueries({
    queries: runIds.map((id) => ({
      queryKey: ['run-findings', id],
      queryFn: () => api.listFindings({ run_id: id }),
      staleTime: 15_000,
    })),
  })

  const findings = useMemo(() => {
    const byId = new Map<string, Finding>()
    for (const list of lists) for (const f of list.data ?? []) byId.set(f.id, f)
    return Array.from(byId.values())
      .filter(isResult)
      .sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))
    // `lists` is a new array every render; its data identities are what matter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lists.map((l) => l.dataUpdatedAt).join(',')])

  // The list rows carry the latest approval's purpose, so no per-finding fetch.
  return findings.map((finding) => ({
    finding,
    auto: finding.status === 'approved' && finding.approval_purpose === 'auto',
  }))
}
