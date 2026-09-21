// Auth for GET /v1/admin/instances (the private, unfloored fleet view).
//
// The comparison is constant-time on purpose: a naive `===` on strings of
// different lengths, or a short-circuiting byte-by-byte compare, leaks
// information about the secret through response timing. We hash both sides
// with SHA-256 first (so the comparison is always over two fixed-length
// digests -- this also stops the token's *length* from being inferable from
// timing) and then compare every byte of the digests without an early exit.

async function sha256Bytes(text) {
  const data = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return new Uint8Array(digest);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

/**
 * Check the `Authorization: Bearer <token>` header against `env.ADMIN_TOKEN`.
 *
 * Returns `{ ok: true }` or `{ ok: false, status, error }` where `status` is
 * 503 (secret not configured -- distinguishable from "wrong token" so an
 * operator can tell a broken deploy from a bad request) or 401 (missing or
 * wrong token).
 */
export async function checkAdminAuth(request, env) {
  if (!env.ADMIN_TOKEN) {
    return { ok: false, status: 503, error: 'admin API is not configured' };
  }

  // Authorization is the one header this endpoint is explicitly allowed to
  // read (unlike /v1/report, which reads none beyond content-length and
  // content-type). The scheme is matched case-insensitively (`bearer`,
  // `Bearer`, `BEARER` all work -- RFC 7235 auth schemes are
  // case-insensitive) and the token is trimmed, so incidental whitespace
  // from a client doesn't turn into a wrong-token 401.
  const header = request.headers.get('authorization') || '';
  const match = /^bearer\s+(.+)$/i.exec(header);
  const provided = match ? match[1].trim() : '';

  const [providedHash, expectedHash] = await Promise.all([
    sha256Bytes(provided),
    sha256Bytes(env.ADMIN_TOKEN),
  ]);

  if (!timingSafeEqual(providedHash, expectedHash)) {
    return { ok: false, status: 401, error: 'unauthorized' };
  }
  return { ok: true };
}
