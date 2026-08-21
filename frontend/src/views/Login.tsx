import { useMutation } from '@tanstack/react-query'
import { Wordmark } from '../components/shared/Wordmark'
import { type FormEvent, useState } from 'react'

import { api } from '../api/client'

export function Login({ onLogin }: { onLogin: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const loginMutation = useMutation({
    mutationFn: () => api.login(email, password),
    onSuccess: onLogin,
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    loginMutation.mutate()
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="sidebar-brand" style={{ padding: '0 0 6px' }}>
          <Wordmark size={26} />
        </div>
        <div className="view-sub" style={{ marginBottom: 18 }}>
          analyst workbench — sign in
        </div>
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
        <button className="btn btn-primary" type="submit" disabled={loginMutation.isPending} style={{ width: '100%' }}>
          {loginMutation.isPending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
