/** Settings → "Anonymous usage statistics". Instance-admin only — the same
 *  `global_role === 'admin'` instance-level check InstanceUsersSection uses,
 *  as opposed to a per-workspace `role` (see that section, and the `User`
 *  type's own doc comment in api/client.ts, for why those two are not the
 *  same thing here). The query itself is the actual gate: a non-admin's GET
 *  403s and the whole card renders nothing, same as InstanceUsersSection —
 *  `isInstanceAdmin` below just keeps that request from firing at all for an
 *  account that cannot see this card.
 *
 *  No first-run banner, no nagging elsewhere: this card is the only place
 *  telemetry is ever mentioned in the product.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import {
  api,
  ApiError,
  telemetryApi,
  type TelemetryLockedReason,
  type TelemetryRecentEntry,
} from '../../api/client'
import { formatDateTime } from '../shared/format'
import { Modal } from '../shared/Modal'
import { QueryError } from '../shared/MonoTable'
import { StatusBadge } from '../shared/StatusBadge'

/** Mirrors the backend's closed set exactly (contract §1's `locked_reason`
 *  resolution order). A reason outside this set (a newer backend) still locks
 *  the checkbox — `locked` alone decides that — it just falls back to a
 *  generic sentence below. */
const LOCKED_REASON_TEXT: Record<TelemetryLockedReason, string> = {
  env_off: 'Turned off by the operator (TRET_TELEMETRY=off).',
  env_on: 'Turned on by the operator (TRET_TELEMETRY=on).',
  do_not_track: 'DO_NOT_TRACK is set for this instance.',
  no_url: 'No telemetry endpoint is configured.',
  extension: 'Disabled by an installed extension.',
  egress_off: 'Outbound network access is off for this instance.',
}

/** `endpoint` is a full URL (e.g. `https://telemetry.kithailab.com/v1/report`)
 *  — the status line only ever shows the host, never the path. Falls back to
 *  the raw string rather than throwing on something unparseable. */
function endpointHost(endpoint: string): string {
  try {
    return new URL(endpoint).host
  } catch {
    return endpoint
  }
}

export function TelemetrySection() {
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const isInstanceAdmin = meQuery.data?.global_role === 'admin'
  const queryClient = useQueryClient()
  const [previewOpen, setPreviewOpen] = useState(false)

  const statusQuery = useQuery({
    queryKey: ['telemetry-status'],
    queryFn: telemetryApi.getTelemetryStatus,
    enabled: isInstanceAdmin,
    retry: false,
  })

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) => telemetryApi.setTelemetryEnabled(enabled),
    onSuccess: (res) => queryClient.setQueryData(['telemetry-status'], res),
  })

  const statusError = statusQuery.error as ApiError | null
  // Not an instance admin, or (defensively) the backend disagrees with a
  // stale `me` cache — either way this account does not get to see the card.
  if (!isInstanceAdmin || (statusQuery.isError && statusError?.status === 403)) return null

  const status = statusQuery.data
  const toggleError = toggleMutation.error as ApiError | null

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Anonymous usage statistics
      </div>
      <div className="panel stack" style={{ gap: 14 }}>
        <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
          Off by default. When on, this instance sends Kith a small weekly report of aggregate,
          bucketed numbers — version, run counts as ranges, token and energy/CO₂e totals, provider
          mix, size bands for users and workspaces, and which optional features are in use. It
          never includes prompts, outputs, names, emails, hostnames, keys or costs. Kith publishes
          the combined totals from every reporting instance. See <code>docs/telemetry.md</code>{' '}
          for the exact payload.
        </div>

        {statusQuery.isLoading ? (
          <div className="empty pulse">Loading telemetry status…</div>
        ) : statusQuery.isError ? (
          <QueryError error={statusQuery.error} what="the telemetry status" />
        ) : status ? (
          <>
            <label className="check-row">
              <input
                type="checkbox"
                checked={status.enabled}
                disabled={status.locked || toggleMutation.isPending}
                onChange={(e) => toggleMutation.mutate(e.target.checked)}
              />
              <span>Send anonymous usage statistics to Kith</span>
            </label>
            {status.locked && (
              <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
                {(status.locked_reason && LOCKED_REASON_TEXT[status.locked_reason]) ??
                  'Locked by this deployment.'}
              </div>
            )}
            {toggleError && <div className="error-text">{toggleError.message}</div>}

            <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
              {status.last_sent_at ? `Last sent: ${formatDateTime(status.last_sent_at)}` : 'Nothing has been sent.'}
              {endpointHost(status.endpoint) && (
                <>
                  {' · '}
                  {endpointHost(status.endpoint)}
                </>
              )}
              {status.instance_id && (
                <>
                  {' · '}
                  <code>{status.instance_id}</code>
                </>
              )}
            </div>

            <div>
              <button type="button" className="btn btn-sm" onClick={() => setPreviewOpen(true)}>
                View exactly what is sent
              </button>
            </div>
          </>
        ) : null}
      </div>

      <TelemetryPreviewModal
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
        recent={status?.recent ?? []}
      />
    </div>
  )
}

/** The preview dialog: the exact next report (fetched fresh on every open —
 *  it is cheap to build and staleTime: 0 means an admin who just toggled the
 *  switch sees a preview that reflects it), plus the local send log. Preview
 *  and send share one code path server-side, so `payload` here is byte-for-byte
 *  what a real send would POST. */
function TelemetryPreviewModal({
  open,
  onClose,
  recent,
}: {
  open: boolean
  onClose: () => void
  recent: TelemetryRecentEntry[]
}) {
  const previewQuery = useQuery({
    queryKey: ['telemetry-preview'],
    queryFn: telemetryApi.getTelemetryPreview,
    enabled: open,
    staleTime: 0,
  })

  return (
    <Modal open={open} onClose={onClose} title="What telemetry sends" width={720}>
      <div className="stack" style={{ gap: 18 }}>
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Next report
          </div>
          {previewQuery.isLoading ? (
            <div className="empty pulse">Loading preview…</div>
          ) : previewQuery.isError ? (
            <QueryError error={previewQuery.error} what="the telemetry preview" />
          ) : previewQuery.data ? (
            <>
              <div className="mono-body" style={{ marginBottom: 8, color: 'var(--text-muted)' }}>
                {previewQuery.data.would_send
                  ? 'This would be sent on the next report.'
                  : 'Telemetry is off, so nothing would be sent — this is the report that would be built if it were on.'}
              </div>
              <pre className="code-block">{JSON.stringify(previewQuery.data.payload, null, 2)}</pre>
            </>
          ) : null}
        </div>

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Recently sent
          </div>
          {recent.length === 0 ? (
            <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
              No reports have been sent from this instance.
            </div>
          ) : (
            <div className="stack" style={{ gap: 6 }}>
              {recent.map((entry, i) => (
                <details key={`${entry.sent_at}-${i}`} className="tool-row">
                  <summary>
                    <span style={{ color: 'var(--text-muted)' }}>{formatDateTime(entry.sent_at)}</span>
                    <StatusBadge status={entry.status} />
                    <span style={{ color: 'var(--text-muted)', fontSize: 10.5 }}>
                      {entry.http_status ?? '—'}
                    </span>
                  </summary>
                  <div className="tool-body">
                    <pre className="code-block" style={{ margin: 0 }}>
                      {JSON.stringify(entry.payload, null, 2)}
                    </pre>
                  </div>
                </details>
              ))}
            </div>
          )}
        </div>
      </div>
    </Modal>
  )
}
