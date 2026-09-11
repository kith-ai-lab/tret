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
  through the same ingestion path as a manual upload. Each item that
  reaches the provider — imported fresh or not — logs one row to the
  [activity log](#the-activity-log). Re-importing an item already
  present in the project (same file content, same picked item) does
  not create a second document: the response returns the existing one
  with `"deduplicated": true` on that item.
- **Disconnect**: Settings → Connections → Disconnect (admin only,
  confirms first). The stored refresh token is deleted; for Google,
  tret also best-effort revokes it at Google's revoke endpoint. For
  Microsoft, Graph has no revoke endpoint, so disconnecting just
  removes tret's copy of the token — the token isn't usable elsewhere,
  but if you want it invalidated at Microsoft too, do that from the
  account's own security settings.

## Live access in runs

Beyond the one-time import above, a run can be given **live** access to
a workspace's Microsoft 365 connection — reaching SharePoint/OneDrive
mid-run rather than only through files a person picked up front. Three
pieces, all m365-only (see [Read allowlist](#read-allowlist) for why
Google isn't part of this):

- **Listing sources** — the drives a run is allowed to read from at
  all: an admin's [read allowlist](#read-allowlist) when one is set,
  otherwise every site's default document library plus the connected
  account's own OneDrive.
- **Search** — full-text search across those drives (Microsoft's
  tenant-wide Search API when the connected account supports it,
  falling back to a per-drive search for a personal Microsoft
  account, which the Search API does not cover).
- **Materialize** — pulling one search result into the project's
  documents on demand, through the same ingestion path an import
  uses. A file already pulled in at its current version is recognized
  and not re-downloaded; a newer version at the same location
  downloads again.

A file pulled in this way is recorded with its own document
`source_kind`, `"connected"` — distinct from an upload (a person put
it there) and a fetched web page (never a source of numbers, per
`engine/validation.py`'s cited-values check): a connected document
came from a specific drive the workspace explicitly allowed a run to
read, mid-run, without a person having picked that exact file.

Live access is gated exactly like browsing and importing: the
connection must be `active`, the workspace's plan gate
(`connections.use`) must allow it, and the deployment's own egress
switch (`TRET_EGRESS`) must be on. All three checks are bundled into
one place a run consults before it is even offered these tools, so a
workspace that loses any of the three loses live access on its very
next run — nothing to reconnect once the underlying condition clears.

## Read allowlist

By default, once an m365 connection is active, everything in [Live
access in runs](#live-access-in-runs) can see and search **every**
site's document library plus the connected account's own OneDrive —
"everything the connected account can see," same breadth as the
`Sites.Read.All`/`Files.Read.All` scopes themselves grant (see
[Security notes](#security-notes) below for why that matters more for
Microsoft than for Google).

A workspace admin can narrow that from Settings → Connections: picking
specific sites/drives sets an explicit read allowlist, and from then
on live access only ever sees drives on that list — search and
materialize refuse anything outside it, the same as a request for a
file Graph itself would resolve but this workspace was never allowed
to read. Clearing the allowlist (an empty selection) goes back to
"everything the account can see." The settings page shows which case
is in effect — restricted to specific locations, or full account
access — before you ever run something that would touch it.

This is the **read** allowlist — it scopes what a run may read, nothing
more. What a run (or a person, from a future write-back UI) may write
*to* is a separate, independent allowlist — see [Write-back](#write-back)
below.

## Write-back

Beyond reading, an m365 connection can be upgraded to let runs write
files back into specific SharePoint/OneDrive folders — a report a run
produced, dropped into a location the workspace picked in advance.
gdrive has no write-back: `drive.file`'s per-file grant (see
[Security notes](#security-notes)) has no broader "write access" for a
separate scope to add on top of.

### Enabling write-back scopes

Reading and writing are separate OAuth grants. A connection made the
ordinary way (or made before write-back existed at all) only ever has
the read scopes (`Files.Read.All`, `Sites.Read.All`); write-back needs
`Files.ReadWrite.All` and `Sites.ReadWrite.All` granted on top.

1. **Add the write permissions** to the same Entra app registration
   from [Microsoft 365 setup](#microsoft-365-setup) ("API permissions
   → Add a permission → Microsoft Graph → Delegated"):
   - `Files.ReadWrite.All`
   - `Sites.ReadWrite.All`

   Grant admin consent for these the same way as the read permissions.
2. **Reconnect with write access**: Settings → Connections → the m365
   connection's "Enable write-back" action. This re-runs the OAuth
   consent screen requesting the wider scope set (offline_access and
   User.Read unchanged, Files.Read.All/Sites.Read.All widened to their
   ReadWrite counterparts) and updates the existing connection in
   place — same row, new refresh token and granted scopes, nothing to
   reconnect for reads. Under the hood this is `POST /api/connections/
   m365/authorize` with `{"scope_set": "write"}` instead of the
   default `{"scope_set": "read"}`.
3. Whether the currently granted scopes actually cover write-back is
   reported as `write_enabled` on the connection (`GET /api/
   connections`) and on the write-targets list (`GET /api/connections/
   m365/write-targets`) — the UI uses this to offer "enable write-back"
   only when it would actually do something.

### Output folders

Unlike the read allowlist, there is no "everything the account can
write to" default — write-back is opt-in, per-folder, always. A
workspace admin picks specific folders from Settings → Connections,
each saved as a named **write target** (a slug, a label, and the
SharePoint/OneDrive folder it points at) in the same `PUT /api/
connections/m365/resources` call the read allowlist uses, under a new
`write` key alongside `read`. Setting one key never touches the other.
Clearing the write target list (an empty selection) means exactly what
it says: nothing may be written anywhere, the same as before write-back
was ever enabled.

A run addresses one of these folders by its slug — `GET /api/
connections/m365/write-targets` lists what's available.

### The `tret/` subfolder, and create-only semantics

A write never lands directly in the folder an admin picked. The first
write to a target creates a `tret` subfolder directly under it (reused
on every later write to the same target), and every file lands inside
that subfolder — so a run's output is always visually separated from
whatever else lives in the folder, never mixed in at the top level.

Every write is **create-only**: it never overwrites a file already
there. If the name it would use is already taken, Microsoft Graph
renames the new upload instead (its own `conflictBehavior=rename`) —
the existing file is left exactly as it was, and the new one lands
under whatever name Graph assigns it. tret never deletes, moves, or
shares anything through this path; the only writes it ever makes are
the `tret` folder's own create-if-absent and the file upload itself.

### The write gate

Every write-back call is checked against the workspace's
`connections.write` gate — a separate check from the `connections.use`
gate reads and browsing go through, so a plan can allow read access
without allowing write-back. A workspace whose plan doesn't cover
write-back gets a clear refusal on the attempt; nothing about reading
is affected.

### The activity log

Every connections action — a search, a file read, an import, a
write-back upload (and its failure), a connect, a disconnect, or an
admin changing the read/write allowlists — is recorded to a
per-workspace activity log,
newest first: `GET /api/connections/activity` (workspace admins only).
Each row carries the provider, the action, who (or which run) did it
when known, what it touched, and how many bytes moved — enough to
answer "what has this connection actually been used for" without
digging through server logs.

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
  workspace can trigger an import (or, for m365, a run's live search/
  materialize — see [Live access in runs](#live-access-in-runs)) using
  the connected account's access — there's no per-user delegation.
  Anyone who can invite themselves into (or already has a role in) the
  workspace can browse and pull in anything the connected account can
  see, so treat connecting an account the way you'd treat sharing that
  account's drive with the whole team. For m365, an admin can narrow
  what that "anything" actually covers with the [read
  allowlist](#read-allowlist) — worth setting up front for an account
  whose own SharePoint/OneDrive access is broader than what you want
  every workspace member (and every run) reaching.
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
