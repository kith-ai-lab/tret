/** Where to land after a Microsoft 365 sign-in that was started outside
 *  Settings > Connections (the composer's "+" menu). The OAuth callback always
 *  redirects to /settings/connections; the Connections view then sends the
 *  browser back to the page that started the flow.
 *
 *  Kept in sessionStorage (this tab only, gone when it closes) and only ever an
 *  in-app path, so it can never redirect off the site. Storage can be missing
 *  or throw (private windows, blocked site data): every access is guarded, and
 *  without it the user simply stays on Connections, as before. */

const KEY = 'tret.connections.returnTo'

function isInAppPath(path: string): boolean {
  return path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/settings/connections')
}

export function rememberReturnPath(path: string): void {
  if (!isInAppPath(path)) return
  try {
    sessionStorage.setItem(KEY, path)
  } catch {
    // No storage: the callback lands on Connections, which still works.
  }
}

/** Drop a remembered path: a sign-in started from Connections itself stays there. */
export function forgetReturnPath(): void {
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    // Nothing stored, nothing to forget.
  }
}

/** The remembered path, removed as it is read (a later, unrelated connect from
 *  Settings must not jump anywhere). Null when there is none or it is unusable. */
export function takeReturnPath(): string | null {
  let path: string | null = null
  try {
    path = sessionStorage.getItem(KEY)
    sessionStorage.removeItem(KEY)
  } catch {
    return null
  }
  return path && isInAppPath(path) ? path : null
}
