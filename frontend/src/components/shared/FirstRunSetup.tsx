import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError, type CheckoutKind } from '../../api/client'
import { Modal } from './Modal'
import { ProviderKeyForm } from './ProviderKeyForm'

/** First-run gate: until at least one provider is configured (a cloud key, or
 *  the local server via TRET_LOCAL_BASE_URL), every run tret could start
 *  would fail — so say so, prominently, instead of letting a fresh install look
 *  quietly broken.
 *
 *  Renders nothing until GET /settings/providers resolves — a configured
 *  install must never see this flash — then a slim banner above every view plus
 *  a welcome dialog on the first unconfigured render. Saving a key invalidates
 *  ['providers'] (ProviderKeyForm does it); this component watches the same
 *  query, so the whole surface unmounts the moment a provider exists — on Tret
 *  Cloud that happens server-side, the instant a credit purchase provisions the
 *  workspace's managed OpenRouter key, with no client action needed beyond the
 *  ['providers'] refetch that already drives this gate.
 *
 *  Setting a key is admin-only (POST /settings/providers is require_admin), so
 *  non-admins get "ask your administrator" rather than a form that would 403.
 *
 *  Billing is a capability the backend may or may not carry — the proprietary
 *  tret-cloud extension — detected exactly like BillingSection.tsx does:
 *  GET /billing/status with retry: false, where a 404 (or any other failure)
 *  means "no billing extension, this is a self-host" and success means Tret
 *  Cloud. Self-host keeps today's paste-a-key dialog byte-for-byte; Tret Cloud
 *  replaces the dialog's primary content with "buy credits" (the purchase
 *  auto-provisions the key — nobody pastes anything), with BYOK still reachable
 *  behind a secondary toggle for anyone who wants to bring their own key anyway. */
export function FirstRunSetup() {
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providerStatus })
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const billingQuery = useQuery({
    queryKey: ['billing-status'],
    queryFn: api.billingStatus,
    retry: false,
  })
  // Opens on the first unconfigured render; dismissing leaves the banner, whose
  // button reopens it. The state survives navigation because this component
  // lives in the app shell, not inside any view.
  const [welcomeOpen, setWelcomeOpen] = useState(true)
  // BYOK stays reachable on Tret Cloud, just tucked behind this toggle so the
  // credit-purchase path is what a new workspace sees first.
  const [showByok, setShowByok] = useState(false)

  if (!providersQuery.isSuccess) return null
  if (providersQuery.data.some((p) => p.configured)) return null

  const isAdmin = ['owner', 'admin'].includes(meQuery.data?.role ?? '')
  const closeWelcome = () => setWelcomeOpen(false)
  const cloudBilling = billingQuery.isSuccess ? billingQuery.data : null

  return (
    <>
      <div className="first-run-banner" role="status">
        <span>
          {cloudBilling
            ? 'No credits yet — tret cannot run anything until this workspace adds some.'
            : 'No AI provider is configured — tret cannot run anything yet.'}
        </span>
        <span style={{ flex: 1 }} />
        {isAdmin ? (
          <button className="btn btn-sm btn-primary" onClick={() => setWelcomeOpen(true)}>
            Set up
          </button>
        ) : (
          <span style={{ color: 'var(--text-muted)' }}>
            {cloudBilling
              ? 'Ask a workspace admin to add credits.'
              : (
                <>
                  Ask your administrator to add a provider key in{' '}
                  <Link to="/settings">Settings</Link>.
                </>
              )}
          </span>
        )}
      </div>

      <Modal
        open={welcomeOpen}
        onClose={closeWelcome}
        title={cloudBilling ? 'Welcome to tret — add credits to start running' : 'Welcome to tret'}
        subtitle="One step before anything can run"
        width={620}
      >
        {cloudBilling ? (
          <CloudWelcome
            status={cloudBilling}
            isAdmin={isAdmin}
            showByok={showByok}
            onToggleByok={() => setShowByok((v) => !v)}
          />
        ) : (
          <SelfHostWelcome isAdmin={isAdmin} closeWelcome={closeWelcome} />
        )}
      </Modal>
    </>
  )
}

/** Unchanged from before billing existed — self-host must see zero difference. */
function SelfHostWelcome({ isAdmin, closeWelcome }: { isAdmin: boolean; closeWelcome: () => void }) {
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div style={{ fontSize: 'var(--fs-md)', lineHeight: 1.6 }}>
        tret sends every task to an AI model, and no provider is set up yet. The fastest
        path is to paste one API key — an OpenRouter key alone is enough (it reaches many
        models), and Anthropic or Moonshot (kimi) keys work too.
      </div>

      {isAdmin ? (
        <ProviderKeyForm heading="Paste an API key" />
      ) : (
        <div className="panel mono-body">
          Adding a key requires the admin role. Ask your administrator to add a provider key
          in{' '}
          <Link to="/settings" onClick={closeWelcome}>
            Settings
          </Link>{' '}
          — this notice disappears for everyone as soon as one is configured.
        </div>
      )}

      <div className="fine-print">
        Prefer no cloud at all? tret can use a local model server (e.g. Ollama) instead —
        no key, nothing leaves your machine. The step-by-step guide is under{' '}
        <Link to="/settings" onClick={closeWelcome}>
          Settings → Set up local models
        </Link>
        .
      </div>
    </div>
  )
}

function CloudWelcome({
  status,
  isAdmin,
  showByok,
  onToggleByok,
}: {
  status: { enabled: boolean }
  isAdmin: boolean
  showByok: boolean
  onToggleByok: () => void
}) {
  // Same checkout mutation pattern as BillingSection: buying credits (or, for
  // a workspace that hasn't activated billing yet, `createCheckout` is also
  // the activation path — status.enabled false doesn't change which call to make.
  const checkoutMutation = useMutation({
    mutationFn: (kind: CheckoutKind) => api.createCheckout(kind),
    onSuccess: (res) => {
      window.location.href = res.url
    },
  })
  const checkoutError = checkoutMutation.error as ApiError | null
  const pendingKind = checkoutMutation.isPending ? checkoutMutation.variables : null

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div style={{ fontSize: 'var(--fs-md)', lineHeight: 1.6 }}>
        tret runs AI models for you — no provider account or API key required. Usage is billed
        from a prepaid credit balance{status.enabled ? '' : ' once this workspace adds some'}.
      </div>

      {isAdmin ? (
        <div className="panel stack" style={{ gap: 12 }}>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn-primary"
              disabled={pendingKind !== null}
              onClick={() => checkoutMutation.mutate('credits_small')}
            >
              {pendingKind === 'credits_small' ? 'Redirecting…' : 'Add $20 credits'}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={pendingKind !== null}
              onClick={() => checkoutMutation.mutate('credits_large')}
            >
              {pendingKind === 'credits_large' ? 'Redirecting…' : 'Add $100 credits'}
            </button>
          </div>
          {checkoutError && (
            <div className="error-text">
              {checkoutError.status === 403 ? 'Requires admin role.' : checkoutError.message}
            </div>
          )}
        </div>
      ) : (
        <div className="panel mono-body">
          Ask a workspace admin to add credits — this notice disappears for everyone as soon as
          the balance is funded.
        </div>
      )}

      <div>
        <button type="button" className="btn btn-sm" onClick={onToggleByok}>
          {showByok ? 'Hide own API key form' : 'Have your own API key?'}
        </button>
        {showByok && (
          <div style={{ marginTop: 12 }}>
            {isAdmin ? (
              <ProviderKeyForm heading="Paste an API key" />
            ) : (
              <div className="panel mono-body">Adding a key requires the admin role.</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
