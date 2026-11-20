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

## Session folders

A chat conversation can carry up to 10 OneDrive/SharePoint **folders**
(`Conversation.connected_folders`). While it has any, every connected-file
tool in that conversation's runs, including delegated child runs and
subagents, only sees items inside those folders. With none, behaviour is
exactly as described in [Live access in runs](#live-access-in-runs), limited
only by the [read allowlist](#read-allowlist).

- **Verified, not assumed.** `search_connected_files` searches each folder
  with Graph's folder-scoped search and re-checks every hit;
  `read_connected_file` and `list_connected_folder` check the item's
  `parentReference.id` chain upward (at most 25 hops, same drive) before
  fetching anything. A per-run cache keeps repeat checks cheap. The `source`
  argument of search is ignored while folders are attached.
- **`list_connected_folder`** is offered automatically next to
  search/read when the session has folders. With no `item_ref` it lists the
  attached folders; with a folder's `item_ref` it lists that folder's children
  (one page of 200; it says when truncated). It counts against the per-run
  search budget.
- **Names and paths are server-side.** Clients send only `drive_id` and
  `item_id`; the name, path and URL are read from Graph. A folder must be a
  folder, in a drive the workspace's read allowlist permits.
- **Personal Microsoft accounts** have no SharePoint, so `/sites` answers
  400, 403 or 404 for them; browsing and source derivation then still return
  the user's OneDrive. Any other failure (401, 429, 5xx, network) is raised,
  never read as "no SharePoint", and nothing is cached from it. The same
  statuses decide the containment check: 400/403/404 on an item mean "not
  inside"; throttling or auth errors fail the call and cache no verdict.
- **Containment is never decided by a search hit.** A search hit's own claim
  about its parent may be used to filter what search lists, but reading a file
  or listing a folder walks Graph's parent chain from a fresh fetch of the item.
- **Batches are not offered in a folder session.** `start_batch` is withheld
  (a batch's row runs do not carry the folder restriction); delegated runs and
  subagents do inherit it. If a conversation's folder list is present but no
  entry in it is usable, the connected-file tools are withheld for the run
  rather than treated as unrestricted.
- **Earlier connected documents stay out of reach.** In a folder session
  `read_document` and `search_documents` skip connected documents (files
  pulled from OneDrive/SharePoint by earlier runs) unless they are attached to
  this run or were read by `read_connected_file` in it, since that read
  checked the folders. Re-read an earlier file with `read_connected_file`.
  Uploaded and web documents are unaffected.
- **Changes apply from the next message.** Changing a conversation's folders
  does not affect a reply that is already running.

### API

All routes need a workspace member and, when folders are non-empty, an active
m365 connection plus the `connections.use` gate.

- `POST /api/chat` accepts `{"folders": [{"drive_id", "item_id"}, ...]}`
  (optional, max 10, unknown keys rejected).
- `PUT /api/chat/{conversation_id}/folders` with `{"folders": [...]}` replaces
  the list and returns the conversation (`messages` included). An empty list
  clears it and needs no connection or gate.
- Every conversation response carries `"folders": [{drive_id, item_id, name,
  path, web_url}]` (`[]` when none).
- Status codes: 403 gate refused (`{reason, detail}`), 404 unknown
  conversation or another workspace's, 409 connection unusable or needs
  reconnecting, 422 not a folder / drive outside the allowed sources / more
  than 10 / malformed body, 502 Graph failed.

## Reading files, and saving them (Learning round)

**Reads are synchronous.** `read_connected_file` downloads the file and extracts its text in
the same call; there is nothing to wait for and no "not extracted yet" state. Supported
types: pdf, docx, xlsx, pptx, csv, tsv, md, txt, json, yaml, **html/htm** (visible text:
script, style, template and noscript content dropped; headings, list items and table cells
kept), **svg/xml/css** (returned as source), and **zip** (a listing; see below).

- `raw=true` returns the source of an html/htm/svg/xml/css file instead of its visible text,
  for example to read an SVG logo or a stylesheet to inline.
- A `.zip` read returns a listing (path, size, type, readable or not and why). Pass
  `member="<path>"` to read one member through the normal extractor. Guards: the archive's
  total uncompressed size is checked first (200 MB), each member is capped at 50 MB, at most
  2000 members are listed, encrypted members and nested zips are listed but not opened.
- A file that cannot be read says why: `Could not extract text from X.html (type html): ...`,
  `No text could be extracted from ...` (empty, script-only or image-only), `... outside this
  session's folders`. It never returns "chars 0-0 of 0". `read_document` gives the same errors.
- A failed read does not count against `TRET_CONNECTIONS_MAX_READS_PER_RUN`; only a read that
  returns text does. Every model turn still counts as one step of the run's step limit,
  whatever its tool result.

**Saving (cohort edition).** Every folder attached to a session is a write target
(`session-1`, `session-2`, ... in attachment order; `GET /api/chat/{id}/write-targets` lists
them next to the workspace's output folders). Files go to `<folder>/tret/<filename>`,
create-only (a taken name fails, nothing is overwritten). The write scopes and the
`connections.write` plan gate still apply. With no session folder and no workspace output
folder the write tool is withheld and the model is told: *No output folder set: ask the member
to attach a folder to this session (+ -> Add OneDrive folder) or add an output folder in
Settings -> Connections.* The standard edition is unchanged (workspace output folders only).

Every save records a `connected_write` finding on the run (visible on the reply), with the
sha256 of the bytes uploaded for every source kind (`payload.upload.sha256`). A long file is
built across several calls with the `draft_file` tool (`mode` create or append; stored as a
document with `source_kind` "generated") and saved with
`propose_connected_write(document_id=...)`. The "Save to folder" button posts to
`POST /api/chat/{id}/files`, which uses the same finding and upload pipeline; the member who
clicks is the approver (recorded as an approval that is not a review, so saving a file is not
quality evidence about the model). With nowhere to save it answers 409
`{"code": "no_output_folder"}`.

**Audit sidecar.** After every successful write, `<filename>.audit.json` is uploaded next to
the file (create-only; schema `tret.deliverable_audit/v1`): who produced it, run and
conversation ids, models used, files read (with hashes), datasets looked up, method runs and
unvetted analysis runs (each marked vetted or not), the grounding status, the guardrails in
force, and the run tree's tokens, cost, energy and CO2e estimates. It holds no credentials.
If the sidecar upload fails the main file still counts as saved; the error is recorded in
`payload.audit.error`.

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

### What a run can propose, and where it lands

A run proposes a write with `propose_connected_write`, which records a
draft finding and uploads nothing until a person approves it. There are
three sources; exactly one per proposal:

| Source | What is uploaded | Limit |
|---|---|---|
| `content` | the inline text the run supplied | 4MB |
| `deliverable` | a deliverable with drafted sections, rendered to markdown/html/pdf **at approval time** | none fixed |
| `document_id` | a stored Document (a captured issuer PDF, a workbook) **as-is** | 25MB |

The `document_id` source must be a document attached to the run (the same
rule `read_document` uses) whose stored file still exists. `filename`
defaults to the document's own name. The approval card shows the original
file name, content type, size, the SHA-256 the upload is pinned to, and its
provenance (for example "web capture"). **The bytes uploaded are exactly the
bytes approved:** at approval Tret re-reads the stored file and compares its
SHA-256 with the one recorded in the proposal; on a mismatch (or a missing
file) nothing is uploaded and the finding's upload is recorded as `failed`
with the reason. The 25MB ceiling is the same as the web-capture and import
caps.

`filename` may be a relative path with forward-slash segments, for example
`runs/2026-11-stage-i/packages/siemens/siemens.pdf`, so a package of files
lands in a folder per run and company under the target's `tret/` folder.
Each segment follows the usual filename rules; there can be at most 6
segments and 400 characters in all, and no leading slash, empty segment, or
`.` / `..`. Missing folders are created automatically as part of the upload
(Graph path addressing); existing ones are reused. The create-only rule
above applies to the final file.

Size handling: up to 4,000,000 bytes goes up as one request; anything larger
goes through a Graph upload session in 5MiB chunks (a multiple of the 320KiB
Graph requires), retrying a chunk once on a 5xx. Write-back is Microsoft 365
only; Google Drive has no write path, so none of this applies to it.

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
