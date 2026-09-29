import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'

import { api, ApiError, type BillingStatus, type CheckoutKind, type LedgerEntry } from '../../api/client'
import { formatDateTime } from './format'
import { type Column, MonoTable } from './MonoTable'

/** Billing is a capability the backend may or may not carry — a hosting
 *  extension. `GET /api/billing/status` 404s when it is not loaded,
 *  and that is not an error to surface, it is "this UI does not exist on this
 *  deployment": the section renders nothing at all for it. Any other failure
 *  (network hiccup, 500) renders nothing too — billing must never be the thing
 *  that breaks Settings for a workspace that cannot reach it. */
export function BillingSection() {
  const statusQuery = useQuery({
    queryKey: ['billing-status'],
    queryFn: api.billingStatus,
    retry: false,
  })

  if (statusQuery.isLoading || statusQuery.isError || !statusQuery.data) return null

  return <BillingPanel status={statusQuery.data} />
}

function BillingPanel({ status }: { status: BillingStatus }) {
  // Same pattern the other Settings sections use: the signed-in user's own
  // role in the current workspace, cached independently of whether some other
  // admin-only request happened to succeed. `me` is already fetched elsewhere
  // in Settings with staleTime: Infinity, so this is normally a cache hit,
  // not a second request. Billing is workspace-scoped, so this is
  // owner/admin — not the instance-wide `global_role`.
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const isAdmin = ['owner', 'admin'].includes(meQuery.data?.role ?? '')

  const checkoutMutation = useMutation({
    mutationFn: (kind: CheckoutKind) => api.createCheckout(kind),
    onSuccess: (res) => {
      window.location.href = res.url
    },
  })
  const portalMutation = useMutation({
    mutationFn: () => api.createPortalSession(),
    onSuccess: (res) => {
      window.location.href = res.url
    },
  })

  const checkoutError = checkoutMutation.error as ApiError | null
  const portalError = portalMutation.error as ApiError | null
  const pendingKind = checkoutMutation.isPending ? checkoutMutation.variables : null

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Billing
      </div>
      <div className="panel stack" style={{ gap: 14 }}>
        {status.enabled ? (
          <BillingActive
            status={status}
            isAdmin={isAdmin}
            onBuy={(kind) => checkoutMutation.mutate(kind)}
            buyPending={pendingKind}
            onManage={() => portalMutation.mutate()}
            managePending={portalMutation.isPending}
          />
        ) : (
          <BillingInactive
            status={status}
            isAdmin={isAdmin}
            onSubscribe={(kind) => checkoutMutation.mutate(kind)}
            pending={pendingKind}
          />
        )}
        {checkoutError && (
          <div className="error-text">
            {checkoutError.status === 403 ? 'Requires admin role.' : checkoutError.message}
          </div>
        )}
        {portalError && (
          <div className="error-text">
            {portalError.status === 403 ? 'Requires admin role.' : portalError.message}
          </div>
        )}
      </div>
      {status.enabled && <UsageHistory />}
    </div>
  )
}

// ── Not yet activated ────────────────────────────────────────────────────

function BillingInactive({
  status,
  isAdmin,
  onSubscribe,
  pending,
}: {
  status: BillingStatus
  isAdmin: boolean
  onSubscribe: (kind: CheckoutKind) => void
  pending: CheckoutKind | null
}) {
  // `offers` absent = older backend: keep the subscription-only UI.
  const offers = status.offers
  const offerCredits = offers?.credits === true
  const plans: Array<'solo' | 'team'> = offers ? offers.subscriptions : ['solo', 'team']
  const nothingOffered = !offerCredits && plans.length === 0
  return (
    <div>
      <div className="mono-body" style={{ marginBottom: 12 }}>
        {!offers || (!offerCredits && !nothingOffered)
          ? 'Billing is not active for this workspace. Subscribe to a plan to pay by card instead of bringing your own provider keys.'
          : nothingOffered
            ? 'Billing is not active for this workspace. Bring your own provider keys to run models.'
            : plans.length === 0
              ? 'Billing is not active for this workspace. Add prepaid credits to run on Kith-managed models, or bring your own provider keys.'
              : 'Billing is not active for this workspace. Add prepaid credits or subscribe to a plan to pay by card, or bring your own provider keys.'}
      </div>
      {isAdmin ? (
        nothingOffered ? (
          <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
            Card payments are not available on this deployment yet.
          </div>
        ) : (
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            {offerCredits && (
              <>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={pending !== null}
                  onClick={() => onSubscribe('credits_small')}
                >
                  {pending === 'credits_small' ? 'Redirecting…' : 'Add $20 credits'}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={pending !== null}
                  onClick={() => onSubscribe('credits_large')}
                >
                  {pending === 'credits_large' ? 'Redirecting…' : 'Add $100 credits'}
                </button>
              </>
            )}
            {plans.includes('solo') && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={pending !== null}
                onClick={() => onSubscribe('solo')}
              >
                {pending === 'solo' ? 'Redirecting…' : 'Activate billing — Solo'}
              </button>
            )}
            {plans.includes('team') && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={pending !== null}
                onClick={() => onSubscribe('team')}
              >
                {pending === 'team' ? 'Redirecting…' : 'Activate billing — Team'}
              </button>
            )}
          </div>
        )
      ) : (
        <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
          Ask an admin to activate billing.
        </div>
      )}
    </div>
  )
}

// ── Active ────────────────────────────────────────────────────────────────

function formatBalance(usd: number): string {
  return `$${usd.toFixed(2)}`
}

function BillingActive({
  status,
  isAdmin,
  onBuy,
  buyPending,
  onManage,
  managePending,
}: {
  status: BillingStatus
  isAdmin: boolean
  onBuy: (kind: CheckoutKind) => void
  buyPending: CheckoutKind | null
  onManage: () => void
  managePending: boolean
}) {
  // Credits-only workspaces (plan 'none') have no subscription to show or
  // manage. Older backends that send no `offers` keep the full stats/portal UI.
  const hasPlan = status.offers === undefined || status.plan !== 'none'
  const offerCredits = status.offers?.credits !== false
  return (
    <div>
      <div className="config-stats">
        <div className="config-stat">
          <div className="mono-label">Balance</div>
          <div className="mono-body">{formatBalance(status.balance_usd)}</div>
        </div>
        {hasPlan && (
          <>
            <div className="config-stat">
              <div className="mono-label">Plan</div>
              <div className="mono-body">
                {status.plan} · {status.subscription_status}
              </div>
            </div>
            <div className="config-stat">
              <div className="mono-label">Seats</div>
              <div className="mono-body">{status.seats}</div>
            </div>
          </>
        )}
      </div>

      {isAdmin ? (
        <div className="row" style={{ gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
          {offerCredits && (
            <>
              <button
                type="button"
                className="btn"
                disabled={buyPending !== null}
                onClick={() => onBuy('credits_small')}
              >
                {buyPending === 'credits_small' ? 'Redirecting…' : 'Add $20 credits'}
              </button>
              <button
                type="button"
                className="btn"
                disabled={buyPending !== null}
                onClick={() => onBuy('credits_large')}
              >
                {buyPending === 'credits_large' ? 'Redirecting…' : 'Add $100 credits'}
              </button>
            </>
          )}
          {hasPlan && (
            <button type="button" className="btn" disabled={managePending} onClick={onManage}>
              {managePending ? 'Redirecting…' : 'Manage subscription'}
            </button>
          )}
        </div>
      ) : (
        <div className="mono-body" style={{ color: 'var(--text-muted)', marginTop: 10 }}>
          {hasPlan
            ? 'Buying credits and managing the subscription requires the admin role.'
            : 'Buying credits requires the admin role.'}
        </div>
      )}
    </div>
  )
}

// ── Usage history ─────────────────────────────────────────────────────────

function AmountCell({ usd }: { usd: number }) {
  const sign = usd < 0 ? '-' : '+'
  return (
    <span style={{ color: usd < 0 ? 'var(--red)' : 'var(--green)' }}>
      {sign}${Math.abs(usd).toFixed(2)}
    </span>
  )
}

function UsageHistory() {
  const usageQuery = useQuery({
    queryKey: ['billing-usage'],
    queryFn: () => api.billingUsage(),
    retry: false,
  })

  const [items, setItems] = useState<LedgerEntry[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)

  // The first page loads through useQuery like every other list in this app;
  // subsequent pages are appended by the load-more mutation below, since
  // react-query v5 keys a query by its whole argument tuple and a cursor
  // change would refetch a fresh page rather than growing one.
  useEffect(() => {
    if (usageQuery.data) {
      setItems(usageQuery.data.items)
      setNextCursor(usageQuery.data.next_cursor)
    }
  }, [usageQuery.data])

  const loadMoreMutation = useMutation({
    mutationFn: (cursor: string) => api.billingUsage(cursor),
    onSuccess: (page) => {
      setItems((prev) => [...prev, ...page.items])
      setNextCursor(page.next_cursor)
    },
  })

  const columns: Column<LedgerEntry>[] = [
    { key: 'created_at', header: 'When', render: (e) => formatDateTime(e.created_at) },
    { key: 'kind', header: 'Kind', render: (e) => e.kind },
    {
      key: 'amount',
      header: 'Amount',
      align: 'right',
      render: (e) => <AmountCell usd={e.amount_usd} />,
    },
    {
      key: 'balance_after',
      header: 'Balance after',
      align: 'right',
      render: (e) => formatBalance(e.balance_after),
    },
    {
      key: 'run_id',
      header: 'Run',
      render: (e) =>
        e.run_id ? (
          <span title={e.run_id} style={{ fontFamily: 'var(--mono)', fontSize: 'var(--fs-xs)' }}>
            {e.run_id.slice(0, 8)}
          </span>
        ) : (
          '—'
        ),
    },
  ]

  return (
    <div style={{ marginTop: 18 }}>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Usage history
      </div>
      {usageQuery.isLoading ? (
        <div className="empty pulse">Loading usage…</div>
      ) : (
        <MonoTable
          columns={columns}
          rows={items}
          rowKey={(e) => e.id}
          empty="No billing activity yet."
        />
      )}
      {loadMoreMutation.isError && (
        <div className="error-text" style={{ marginTop: 8 }}>
          {(loadMoreMutation.error as Error).message}
        </div>
      )}
      {nextCursor && (
        <div style={{ marginTop: 10 }}>
          <button
            type="button"
            className="btn btn-sm"
            disabled={loadMoreMutation.isPending}
            onClick={() => loadMoreMutation.mutate(nextCursor)}
          >
            {loadMoreMutation.isPending ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  )
}
