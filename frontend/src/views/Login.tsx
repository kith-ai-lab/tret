import { useMutation, useQuery } from '@tanstack/react-query'
import { Wordmark } from '../components/shared/Wordmark'
import { type FormEvent, useState } from 'react'
import { useLocation } from 'react-router-dom'

import { api } from '../api/client'

/** Sign-in screen. What it shows depends on `GET /api/auth/config`, fetched
 *  before there is any session to check:
 *
 *  - `password` — the form only (unchanged from before OIDC existed).
 *  - `oidc` — a single SSO button in place of the form; password login is
 *    rejected server-side in this mode, so there is nothing useful the form
 *    could do here.
 *  - `both` — the form, a divider, and the SSO button.
 *
 *  A failed config fetch falls back to the password form: a broken /config
 *  endpoint must never be the thing that locks every user out of signing in.
 *
 *  The SSO button carries `next=<current path>` so a deep link — most
 *  notably `/invite/{token}`, which App.tsx renders this same screen in
 *  place of when the visitor isn't signed in yet — survives the round trip
 *  through the identity provider and back. */
export function Login({ onLogin }: { onLogin: () => void }) {
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const configQuery = useQuery({
    queryKey: ['auth-config'],
    queryFn: api.authConfig,
    retry: false,
  })

  const loginMutation = useMutation({
    mutationFn: () => api.login(email, password),
    onSuccess: onLogin,
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    loginMutation.mutate()
  }

  if (configQuery.isLoading) {
    return (
      <div className="login-wrap">
        <div className="mono-label pulse">loading…</div>
      </div>
    )
  }

  const mode = configQuery.data?.auth_mode ?? 'password'
  const ssoUrl = configQuery.data?.oidc_login_url ?? null
  const showPassword = mode !== 'oidc'
  const showSso = mode !== 'password' && !!ssoUrl
  const next = `${location.pathname}${location.search}`

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="sidebar-brand" style={{ padding: '0 0 6px' }}>
          <Wordmark size={26} />
        </div>
        <div className="view-sub" style={{ marginBottom: 18 }}>
          analyst workbench — sign in
        </div>

        {showPassword && (
          <form onSubmit={submit}>
            <div className="field">
              <label className="mono-label">Email</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="username"
                autoFocus
                required
              />
            </div>
            <div className="field">
              <label className="mono-label">Password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </div>
            {loginMutation.isError && (
              <div className="error-text" style={{ marginBottom: 12 }}>
                {(loginMutation.error as Error).message}
              </div>
            )}
            <button
              className="btn btn-primary"
              type="submit"
              disabled={loginMutation.isPending}
              style={{ width: '100%' }}
            >
              {loginMutation.isPending ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        )}

        {showPassword && showSso && (
          <div className="login-divider">
            <span>or</span>
          </div>
        )}

        {showSso && (
          <button
            type="button"
            className="btn btn-primary"
            style={{ width: '100%' }}
            onClick={() => {
              window.location.href = `${ssoUrl}?next=${encodeURIComponent(next)}`
            }}
          >
            Continue to sign in
          </button>
        )}

        {!showPassword && !showSso && (
          <div className="error-text">
            Sign-in is not available on this deployment — contact your administrator.
          </div>
        )}
      </div>
    </div>
  )
}
