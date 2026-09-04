# Workspace Connections

Workspace connections link a workspace to a Google Drive or Microsoft
365 (SharePoint/OneDrive) account over OAuth, so members can import
documents from that account into projects. A later phase adds live
agent tools over the same connection; this doc covers what ships today.

**The connected account's access is shared by the whole workspace.**
One admin connects one Google or Microsoft account, and from then on
every member of that workspace can import files as that account — the
connection is not per-user. Pick an account you are comfortable with
the whole workspace using, and see [Security notes](#security-notes)
below for what that does and does not expose.

Both providers are optional and unconfigured by default: with no
client id/secret set, the feature is simply absent from the UI.

## Google Drive setup

1. **Create a Google Cloud project** (or reuse one) at
   [console.cloud.google.com](https://console.cloud.google.com).
2. **Enable APIs** — under "APIs & Services → Library", enable:
   - **Google Drive API**
   - **Google Picker API**
3. **Configure the OAuth consent screen** ("APIs & Services → OAuth
   consent screen"):
   - If your organization is on Google Workspace, set audience
     **Internal**. Internal apps skip Google's verification process
     entirely — nothing to submit, no review wait.
   - Otherwise, **External** in **Testing** mode works fine: add the
     Google accounts that will connect as test users. tret only
     requests `drive.file` (see [Security notes](#security-notes)), a
     non-sensitive scope, so this consent screen never triggers
     Google's restricted-scope verification review even when you
     eventually publish it.
4. **Create an OAuth client** ("APIs & Services → Credentials → Create
   Credentials → OAuth client ID"):
   - Application type: **Web application**
   - Authorized redirect URI: `{TRET_APP_URL}/api/connections/callback`
     — the same base URL tret's OIDC login flow uses, e.g.
     `https://tret.example.com/api/connections/callback`
5. **Create an API key** for the Picker ("Create Credentials → API
   key"). Restrict it to the Picker API if you want to be strict about
   it.
6. **Set the environment variables** (see `.env.example`):

   ```
   TRET_GDRIVE_CLIENT_ID=<OAuth client id>
   TRET_GDRIVE_CLIENT_SECRET=<OAuth client secret>
   TRET_GDRIVE_PICKER_API_KEY=<API key from step 5>
   TRET_GDRIVE_APP_ID=<Cloud project number, not the project id>
   ```

   `TRET_GDRIVE_APP_ID` is the numeric **project number** shown on the
   Cloud console's project dashboard (not the text project id) — the
   Picker requires it.

## Microsoft 365 setup

1. **Register an app** in the
   [Entra admin center](https://entra.microsoft.com) → "App
   registrations → New registration".
   - **Single tenant** if every account that will connect belongs to
     your own organization's tenant.
   - **Multi-tenant** if you expect to connect accounts from other
     organizations' tenants too.
2. **Add a redirect URI**: platform **Web**,
   `{TRET_APP_URL}/api/connections/callback` — the same URL as above.
3. **Add delegated Microsoft Graph API permissions**:
   - `offline_access`
   - `User.Read`
   - `Files.Read.All`
   - `Sites.Read.All`

   Depending on your tenant's admin consent policy, an admin may need
   to grant consent for these before any user in that tenant can
   connect ("API permissions → Grant admin consent").
4. **Create a client secret** ("Certificates & secrets → New client
   secret"). Copy the secret value immediately — it is not shown
   again.
5. **Set the environment variables**:

   ```
   TRET_M365_CLIENT_ID=<Application (client) ID>
   TRET_M365_CLIENT_SECRET=<client secret value>
   ```

## Using it

- **Connect**: Settings → Connections, workspace admins only. Each
  configured provider shows a Connect button; connecting opens the
  provider's consent screen and redirects back into tret.
  Unconfigured providers show a hint to set the env vars above instead
  of a Connect button.
- **Import**: on a project's Documents view, an "Import from…" menu
  appears once at least one connection is active — Google Drive opens
  the Google Picker to select files, SharePoint/OneDrive opens a
  browser over the connected account's sites and drives. Selected
  files are downloaded server-side with the connection's token and fed
  through the same ingestion path as a manual upload.
- **Disconnect**: Settings → Connections → Disconnect (admin only,
  confirms first). The stored refresh token is deleted; for Google,
  tret also best-effort revokes it at Google's revoke endpoint. For
  Microsoft, Graph has no revoke endpoint, so disconnecting just
  removes tret's copy of the token — the token isn't usable elsewhere,
  but if you want it invalidated at Microsoft too, do that from the
  account's own security settings.

## Security notes

- **Encryption at rest.** Refresh tokens are encrypted with the same
  Fernet envelope tret uses for provider API keys entered in the
  settings UI, keyed off `TRET_SECRET_KEY`. Rotating
  `TRET_SECRET_KEY` makes stored connection tokens undecryptable too
  (`docs/hardening.md` §1) — reconnect after a rotation.
- **Scope differs sharply by provider — read this before connecting
  Microsoft.**
  - Google: `drive.file`, which only grants access to files the user
    explicitly picks through the Picker UI at connect time and later
    at import time — not blanket read access to their Drive. Any
    workspace member can obtain a short-lived Google access token for
    that Picker via `GET /api/connections/gdrive/token` (see
    [Using it](#using-it)), but the token is scoped by `drive.file`
    the same way — it cannot read a file nobody has picked through the
    Picker UI.
  - Microsoft: `Files.Read.All` and `Sites.Read.All` are **tenant-wide
    read scopes** — read-only (tret never writes to Drive, SharePoint,
    or OneDrive), but not narrowed to picked files the way Google's
    scope is. They grant read access to everything the connected
    account itself can reach: every SharePoint site and OneDrive the
    account has permission to read, not just what gets imported
    through tret's browse/import UI. Connecting an account whose
    Microsoft permissions are broader than what you want the workspace
    to import is not something the scope itself prevents — pick (or
    provision) an account whose own SharePoint/OneDrive access already
    matches what you're comfortable the whole workspace reaching.
- **Whole-workspace access, one account.** Every member of the
  workspace can trigger an import using the connected account's
  access — there's no per-user delegation. Anyone who can invite
  themselves into (or already has a role in) the workspace can browse
  and pull in anything the connected account can see, so treat
  connecting an account the way you'd treat sharing that account's
  drive with the whole team.
- **Revocation surfaces as an error, not a silent failure.** If the
  provider invalidates the connection outside of tret — the connected
  user changes their password, an org admin revokes the app, the
  refresh token expires from disuse — the next token refresh fails and
  the connection flips to `error` status with a reason. Once a
  connection is in `error` status, every browse and import request
  returns 409 immediately, without contacting the provider again, until
  an admin reconnects it; tret never falls back to a stale token.
- **Access tokens are cached briefly, in-process.** A browse or import
  reuses the last access token it minted for a connection until shortly
  before that token's own expiry, rather than trading the refresh token
  for a new one on every single request — clicking through several
  SharePoint folders in a row costs one refresh, not one per click. The
  cache holds nothing longer than the token's own lifetime and lives
  only in the single tret process handling requests (see
  `docs/hardening.md`'s strict instance lock), so there is nothing to
  invalidate across a fleet that doesn't exist.
