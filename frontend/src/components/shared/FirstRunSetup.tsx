import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
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
 *  query, so the whole surface unmounts the moment a provider exists.
 *
 *  Setting a key is admin-only (POST /settings/providers is require_admin), so
 *  non-admins get "ask your administrator" rather than a form that would 403. */
export function FirstRunSetup() {
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providerStatus })
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  // Opens on the first unconfigured render; dismissing leaves the banner, whose
  // button reopens it. The state survives navigation because this component
  // lives in the app shell, not inside any view.
  const [welcomeOpen, setWelcomeOpen] = useState(true)

  if (!providersQuery.isSuccess) return null
  if (providersQuery.data.some((p) => p.configured)) return null

  const isAdmin = ['owner', 'admin'].includes(meQuery.data?.role ?? '')
  const closeWelcome = () => setWelcomeOpen(false)

  return (
    <>
      <div className="first-run-banner" role="status">
        <span>No AI provider is configured — tret cannot run anything yet.</span>
        <span style={{ flex: 1 }} />
        {isAdmin ? (
          <button className="btn btn-sm btn-primary" onClick={() => setWelcomeOpen(true)}>
            Set up
          </button>
        ) : (
          <span style={{ color: 'var(--text-muted)' }}>
            Ask your administrator to add a provider key in{' '}
            <Link to="/settings">Settings</Link>.
          </span>
        )}
      </div>

      <Modal
        open={welcomeOpen}
        onClose={closeWelcome}
        title="Welcome to tret"
        subtitle="One step before anything can run"
        width={620}
      >
        <div className="stack" style={{ gap: 16 }}>
          <div style={{ fontSize: 13.5, lineHeight: 1.6 }}>
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
      </Modal>
    </>
  )
}
