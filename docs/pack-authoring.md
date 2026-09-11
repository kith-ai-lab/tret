# Authoring a Domain Pack

A pack turns tret into a workbench for *your* domain — contract review,
grant compliance, safety audits, anything where structured judgment over
documents and data needs to be trustworthy. No backend code required.

## Layout

```
my-pack/
├── pack.yaml            # the manifest (below)
├── doctrine/            # ordered markdown rules the agent follows and cites
│   └── 01-principles.md
├── schemas/             # JSON Schema (draft 2020-12) per structured output
│   └── my_verdict.schema.json
├── templates/           # optional deliverable skeletons
└── sample-data/         # optional CSVs seeded as datasets on install
```

Validate any time:

```bash
tret packs validate ./my-pack
```

## pack.yaml

```yaml
pack: my-pack            # slug
version: 0.1.0
display_name: My Domain
description: One paragraph.
doctrine:                # ordered; hashed together into doctrine_sha
  - doctrine/01-principles.md
task_types:
  - slug: my_assessment
    display_name: My assessment
    shape: verdict       # verdict|extraction|drafting|qa_review|freeform
    input_schema:        # FLAT fields only — they become the run form
      subject_id: { type: string, description: "..." }
      category:   { type: string, enum: [a, b, c] }
    output_schema: schemas/my_verdict.schema.json
    terminal_tool: record_verdict
    tools: [lookup_dataset, read_document, file_data_request, record_verdict]
    doctrine:            # OPTIONAL: the doctrine this task needs (see below)
      - doctrine/01-principles.md
    output_contract: One line the model router reads.
    instructions: |
      Step-by-step instructions for the task. Reference your doctrine.
datasets:
  - { name: reference_scores, file: sample-data/reference_scores.csv }
```

## The pieces that make it trustworthy

- **shape** drives the router's deterministic fallback and tells the LLM
  router what the task demands. Pick honestly.
- **doctrine** is where your profession's judgment lives. Write rules with
  *evidence tests* — "you may only use this code if you retrieved X" — so
  compliance is checkable. The agent must cite headings; make them citable.
- **output_schema** is enforced mechanically, with in-loop repair. Prefer
  enums over free text wherever a value is decision-relevant. If your schema
  includes a `cited_values` array (objects with `dataset`, `row_ref`,
  `value`), tret cross-checks every entry against what the run actually
  retrieved via `lookup_dataset` — use it for any output that carries numbers.
- **terminal_tool** (`record_verdict`, `record_finding`, or `draft_section`)
  makes the structured output the *terminal action* of the run. If the model
  stops without recording, the engine nudges it once; if it still records
  nothing, the run ends with status **`completed_without_output`** rather than
  fabricating a finding. Declaring a `terminal_tool` is what makes that honesty
  possible — a task without one has no way to tell "answered" from "gave up".
- **datasets** are the deterministic lane: CSVs whose rows the model can
  retrieve (exact-match filters) but never edit or compute over.

## Task-scoped doctrine

Doctrine is re-sent on every iteration of every run, so doctrine a task never
uses is pure waste — money, latency, and energy. A task type may therefore
declare what it needs:

```yaml
task_types:
  - slug: evidence_extraction
    doctrine:
      - doctrine/01-principles.md                      # a whole file
      - "doctrine/03-reason-codes.md#outdated_inputs"   # one `#`/`##` section
```

- **Omit `doctrine:` and the task gets every doctrine file in the pack** — the
  default, so packs written before this existed behave exactly as before.
- Selectors may only name files listed in the pack's top-level `doctrine:`.
- A section selector matches a heading by text (case-insensitive; a prefix such
  as `#Step 5` is enough). The file's front matter — its title and the framing
  paragraphs before the first `##` — always rides along, because that is
  usually where the rule that makes the section interpretable lives.
- `tret packs validate` fails on a selector naming an unknown file or a
  heading that does not exist, so scoping cannot rot silently. At runtime an
  unresolvable selector fails *open* (whole file loaded) and is noted in the
  run's context composition: a scoping mistake can never starve a task of
  doctrine.
- Scope conservatively. A task must keep every section it might cite or apply —
  if a task grades or produces verdicts, it needs the procedure and the reason
  codes. When unsure, declare nothing.

Whatever a run loads is hashed per file and recorded in the run's
`context_composition` alongside its estimated token count, so the audit trail
states exactly what the model saw — and `GET /api/runs/{id}` shows you which
component of your prompt is spending the tokens.

## Tool results are capped

`lookup_dataset`, `run_method`, and `list_prior_findings` return at most 200
rows / 100KB per call. Over that, the result carries an explicit `[TRUNCATED:
…]` marker telling the model to narrow the query, and the rows it did not see
are **not** citable — the citation cross-check only knows about what was
actually returned. Design datasets and methods so the interesting answer fits:
filterable columns, and a method for anything that wants aggregating.

## Methods — the deterministic compute lane

When your domain needs *computed* numbers (aggregations, inventories, rates),
ship them as **methods**: vetted Python scripts the agent can invoke with
parameters but never write.

```yaml
methods:
  - slug: my_rollup
    display_name: My rollup
    description: One paragraph the agent reads to know when to use it.
    entrypoint: methods/my_rollup.py
    params_schema:
      status: { type: string, enum: [all, approved], description: "..." }
    inputs: [my_dataset, "findings:my_verdict"]   # materialized and passed in
    timeout_seconds: 60
```

Script contract — a pure function over stdin/stdout, stdlib only:

```python
import json, sys
payload = json.load(sys.stdin)           # {"params": {...}, "inputs": {name: [rows]}}
rows = compute(payload)                   # deterministic!
json.dump({"rows": rows}, sys.stdout)    # flat dicts
```

Rules that keep the trust story intact:
- **Deterministic**: same inputs + params → same output, always. No network,
  no clock, no randomness. The runner executes with an isolated interpreter,
  empty environment, CPU/memory limits, and a timeout.
- **Pin your constants**: emission factors, thresholds, mappings live *in the
  code* with a named version — changing them changes the code sha, which is
  exactly the point.
- **Declare gaps**: if a record can't be processed (unknown unit, missing
  factor), skip it and report it in the output — never silently guess.
- Every execution is recorded in `method_runs` with params, code sha, input
  hashes, and output hash; the agent cites method outputs like dataset rows.

## Harnesses — ready-to-run presets

A pack can ship one or more `harnesses:` entries — each installs as a real,
editable workspace `Harness` row the moment the pack is installed, so a
workspace gets a working starting point instead of an empty harness list.

```yaml
harnesses:
  - name: Risk Reviewer
    description: Reviews disclosures against this pack's own doctrine.
    task_types: [risk_assessment]   # at most ONE of this pack's own task_types slugs; omit for freeform
    tools: [lookup_dataset, read_document, record_verdict]
    suggested_cost_tier: standard    # local|economy|standard|premium — a suggestion, not a pin
```

- **name** (required) becomes the installed harness's name.
- **description** is optional free text, shown wherever the harness is listed.
- **task_types** names **at most one** slug from this same manifest's own
  `task_types:` list — `Harness.task_profile` holds exactly one value, so
  `tret packs validate` fails a preset naming more than one. Omit it (or
  leave it empty) and the harness installs scoped to `task_profile:
  freeform`, the same default an ordinary hand-created harness gets.
- **tools** names builtin tool names — see "Builtin tools you can grant"
  below.
- **suggested_cost_tier** is one of `local`, `economy`, `standard`, or
  `premium`. It only seeds the installed harness's cost ceiling; it is a
  suggestion the installing workspace is free to edit or remove afterward,
  never a pin the pack enforces.

Installing is idempotent **by harness name**, not by pack/version: if a
non-archived harness with that name already exists in the workspace, the
preset is skipped rather than duplicated or overwritten. That is also what
makes upgrading a pack (installing a new version of the same slug) safe — it
leaves a harness a workspace has already installed, and possibly edited,
alone rather than mutating it back to the pack's defaults. An archived
harness of the same name does not block re-creation, since archiving it was
the operator's own choice.

The climate-risk pack's "Climate Analyst" harness
(`packs/climate-risk/pack.yaml`) is a working reference example.

## The safety scan your methods must pass

`tret packs validate` AST-scans every method entrypoint and **fails the pack**
— so it is never installed — if the code reaches for anything that would stop it
being a pure, reproducible function. Violations are reported with `file:line`.

| Refused | Examples |
| --- | --- |
| Network | `socket`, `socketserver`, `ssl`, `http*`, `urllib`, `ftplib`, `smtplib`, `poplib`, `imaplib`, `nntplib`, `telnetlib`, `xmlrpc`, `webbrowser`, `selectors`, `asyncio` |
| Process spawning | `subprocess`, `multiprocessing`, `pty`; `os.system`, `os.popen`, `os.fork`, `os.forkpty`, `os.kill`, `os.killpg`, `os.abort`, `os.exec*`, `os.spawn*`, `os.posix_spawn` |
| Dynamic code | `runpy`, `code`, `codeop`, `eval`, `exec`, `compile` |
| Dynamic import | `importlib`, `imp`, `__import__`, and relative imports (a method is one file) |
| FFI | `ctypes`, `cffi` |
| Code-executing deserialisation | `pickle`, `shelve`, `marshal` |
| Namespace escapes | `__builtins__`, `__subclasses__`, `__globals__`, `__code__`, `__loader__`, `__mro__` |
| Environment mutation | `os.putenv`, `os.unsetenv` |

Matching is on the dotted name *and* its root package, so `http.client` trips
`http`. The authoritative list is `backend/tret/packs/safety.py`.

If a rule blocks something you need, the need is usually the problem: a method
that fetches a URL is not reproducible, and a method that shells out is not
reviewable. Fetch the data outside tret and ship it as a dataset instead.

**This scan is a deterrent, not a sandbox.** Any determined author can defeat an
AST check. What actually contains a method is the subprocess isolation in
`services/methods.py` and the fact that an operator reviewed your pack before
installing it. See [hardening.md](hardening.md) for what is and is not isolated
(notably: the filesystem is not).

## Integrity pinning and how to re-pin

At install, tret hashes **every entry** in the pack directory — methods,
schemas, datasets, templates, `pack.yaml`, doctrine — and stores it as
`packs.content_hash` (visible on `GET /api/packs`). Before any method executes,
the hash is recomputed and compared. A mismatch fails the run, names both
hashes, and records a failed `method_runs` row.

Build artefacts and editor/VCS noise are excluded (`__pycache__`, `.git`,
`.venv`, `node_modules`, `*.pyc`, `.DS_Store`, …) so the pin survives working in
the directory. Everything else a method could read is in.

### Symlinks are pinned by target, and never followed

A symlink contributes its **path and its target string** to the digest, under a
tag that keeps it distinct from a file whose bytes happen to equal that target.
Adding, removing, or re-pointing a link therefore changes the hash. Links are
never traversed — including symlinked directories, whose *identity* is pinned but
whose contents are not walked (which also makes the walk immune to link cycles).

This closed a hole: symlinks used to be skipped by the walk entirely, so a pinned
pack could ship `analysis.py -> ../elsewhere/analysis.py` and re-pointing that
link swapped the code a method executes without changing a single byte the hash
covered.

The limit is worth stating plainly, because a directory hash cannot fix it:
**content outside the pack directory cannot be pinned.** tret refuses to follow
a link out of the pack (a pack could otherwise aim the hasher at `/dev/urandom`,
or at a file it has no business reading), so a pack whose data lives behind an
external symlink is pinned *by reference only* — the link still points where it
did, but nothing detects an edit to the file at the far end. **Keep pack content
inside the pack.**

Packs with no symlinks are hashed exactly as they were before symlink coverage
existed, so they keep their existing pin and need no reinstall for it.

Two consequences for authoring:

1. **Editing pack files under a running deployment breaks method runs**, on
   purpose. A method whose code changed silently would make earlier findings
   unreproducible while still looking correct.
2. `doctrine_sha` and `content_hash` are different pins. Doctrine changes move
   both; changing a dataset CSV or a method moves only `content_hash`.

The re-pin flow after an intentional edit:

```bash
tret packs hash ./my-pack      # the hash an install would store; changes nothing
```

then reinstall the pack, which re-pins it:

- restart tret — boot runs the idempotent pack install, or
- `POST /api/packs/install {"path": "/path/to/my-pack"}` (admin only).

While iterating locally, expect to reinstall after each edit that touches a
method or its inputs. Bump `version` in `pack.yaml` for anything you publish, so
consumers can tell a re-pin from a genuinely new pack. Packs installed before
integrity pinning existed carry a null hash: tret warns, runs them, and the
next install pins them.

Pinning catches tampering and drift by whoever can write to the pack directory.
It is **not a signature** — the same person can reinstall to re-pin. Signed
packs remain future work.

## Builtin tools you can grant

| Tool | Use |
|---|---|
| `read_document` / `search_documents` | attached evidence documents |
| `list_connected_sources` / `search_connected_files` / `read_connected_file` | live files from a workspace's connected SharePoint/OneDrive |
| `propose_connected_write` | propose writing a file back to a connected SharePoint/OneDrive target |
| `lookup_dataset` | retrieve stored numbers |
| `run_method` | compute derived numbers via vetted pack methods |
| `list_prior_findings` | reference earlier verdicts/extractions |
| `list_pack_lessons` / `propose_pack_lesson` | read this pack's approved lessons for this workspace; propose a new one for review |
| `record_verdict` / `record_finding` | schema-validated structured outputs |
| `draft_section` | store a markdown deliverable section |
| `file_data_request` | declare a gap instead of guessing |

**Lessons memory** is on by default for every pack — no manifest field to
set, and no `tools` entry to declare either. Unlike `list_prior_findings`
(which a task or harness must list explicitly to get), `list_pack_lessons`
and `propose_pack_lesson` are added to every run's tool list by the engine
itself, unconditionally, unless `loop_config.lessons: false` withholds them —
a pack author does nothing to receive them and nothing (short of that flag)
to refuse them. A pack's approved lessons for the current workspace are read
into the prompt automatically (the `pack_lessons` context block, after
doctrine) the same unconditional way. Lessons follow the pack's *slug*, not
one installed version's id — a lesson approved under 1.0.0 is still read into
the prompt once the pack is upgraded to 1.1.0; see docs/architecture.md.
`propose_pack_lesson` only ever creates a `proposed` row (at most 3 per run);
a workspace approver has to bless it (Packs > your pack > Lessons) before it
appears anywhere, and an approved lesson still doesn't ship if the workspace
already has 40 (or ~4,000 characters worth) ahead of it in the queue — the
Packs view marks those "over cap". If this pack should never accrue or read a
lessons memory, set `loop_config.lessons: false` on the harness(es) that run
it — this withholds both tools and the context block for runs on that
harness, with no effect on prompt behavior otherwise (an empty lessons list
already renders nothing).

## Connected sources (live SharePoint/OneDrive)

A workspace can link a Microsoft 365 account under Settings > Connections
(`tret/services/connections.py`). A task that wants to read from it declares
the three tools together, the same way `read_document`/`search_documents`
travel as a pair:

```yaml
    tools: [list_connected_sources, search_connected_files, read_connected_file, record_verdict]
```

- `list_connected_sources` — the connected site drives/OneDrives available,
  each with a `slug` for narrowing a search.
- `search_connected_files` — searches those sources for a query, returning
  hits with an `item_ref`.
- `read_connected_file` — materializes a hit by `item_ref` into a `Document`
  (`source_kind='connected'`) attached to the run, then pages through it
  exactly like `read_document`; the document stays readable afterwards via
  `read_document`/`search_documents` by id.

A connected file is a third trust tier, distinct from an uploaded document and
from `fetch_url`'s web pages: nobody vetted it before the run started, but it
also wasn't chosen off the open internet by the model — it's whatever the
workspace's own connected account can see. It carries its own banner
(`[CONNECTED SOURCE: ...]`) rather than the web tools' unverified notice, and
— like the web tools — no value read this way is ever registered for the
cited-values check; numbers still come only from `lookup_dataset`/`run_method`.

**When nothing is connected.** These three tools stay in the registry
whether or not any workspace has ever connected an account — a harness that
lists them is never an `unknown_tool` failure. Whether they're *offered* to
the model on a given run is a per-workspace check (`ensure_connection_usable`):
no connection, or a connection that's gone stale, and the engine withholds
them before the first token and publishes a `tools_withheld` event
(`reason: "connection_unavailable"`) naming why. The run proceeds without
them rather than failing.

**Per-run caps**, enforced by the tools themselves (`TRET_` env vars, see
`.env.example`):

| Cap | Default | Env var |
|---|---|---|
| connected reads | 20 | `TRET_CONNECTIONS_MAX_READS_PER_RUN` |
| connected bytes | 100MB | `TRET_CONNECTIONS_MAX_BYTES_PER_RUN` |
| connected searches | 30 | `TRET_CONNECTIONS_MAX_SEARCHES_PER_RUN` |

Hitting a cap returns a tool error naming the limit rather than failing the
run — the model can keep working with what it already read.

**Writing back** (`propose_connected_write`) is a different shape entirely, and
deliberately not part of the trio above: it goes through the blessing gate
instead of a per-run budget. Calling it never touches Microsoft Graph — it
resolves a `target` slug (from the connection's configured write targets),
validates the `filename`, and records a `connected_write` Finding with status
`draft`, exactly like `record_finding`/`draft_section`. Give it either inline
`content` (up to 4MB) or a `deliverable` slug that already has at least one
drafted section — never both, never neither — and for a deliverable an
optional `format` (`markdown`/`html`/`pdf`, default `markdown`) says how it
will be rendered. The actual upload happens only when a human approves that
finding (`POST /api/findings/{id}/approval`), and a failed upload is recorded
on the finding rather than blocking the approval — retry it with `POST
/api/findings/{id}/upload-retry`. Offering the tool at all additionally
requires the connection to have write scopes and at least one configured
write target, checked the same withheld-with-a-reason way as the read trio.

## Input schema → form

`input_schema` fields must be flat (`string`, `number`, or `string` with
`enum`). The Workbench generates the run form from them — keep them few and
analyst-friendly.

## Sample data

Ship enough fictional data that a stranger can run every task type
end-to-end after `docker compose up`. Seed at least one case where the
*interesting* outcome occurs (a divergence, a violation, a gap) — demos that
always say "everything agrees" teach nothing.
