import { ApiError } from '../../api/client'

export function errText(e: unknown): string {
  if (e instanceof ApiError) return e.status === 403 ? 'Requires approver role.' : e.message
  return e instanceof Error ? e.message : String(e)
}

export function safeHref(url: string | null | undefined): string | null {
  return url && /^https?:\/\//i.test(url) ? url : null
}

export function ExtLink({
  url,
  children,
}: {
  url: string | null | undefined
  children: React.ReactNode
}) {
  const href = safeHref(url)
  if (!href) return <>{children}</>
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

export function shorten(s: string | null | undefined, n: number): string {
  if (!s) return ''
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}

export function WarningsBox({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null
  return (
    <div className="callout callout-warn" style={{ marginBottom: 14 }}>
      <div className="callout-title">Register warnings</div>
      <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
        {warnings.map((w, i) => (
          <li key={i}>{w}</li>
        ))}
      </ul>
    </div>
  )
}
