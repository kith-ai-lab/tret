# Collecting sustainability disclosures

This is the end-to-end workflow for collecting an issuer's public sustainability
disclosure with tret: find it, capture the original, decide which pages are the
disclosure, have a person approve that decision, and keep a verified page subset
with the evidence of how it was made. It was built for Kith Climate, where the
disclosure is often a few dozen pages inside a 300-page annual report, but nothing
in it is specific to one pack.

The short version: **a run proposes, a person approves, a deterministic packager
executes.** Page ranges are never inferred by code. The model reads and proposes;
the human decides; the packager retains exactly the pages that were approved and
checks that it did.

```
web_search            navigation only, never evidence
  -> capture_source   the complete original, hashed, as a Document (source_kind web)
  -> read_document    the model reads it, banner-marked as unverified
  -> propose_package_map   a DRAFT package_map finding: physical page ranges
  -> approval         a person accepts (or rejects) the map in the Approvals queue
  -> packager         retained PDF + page map + link audit + text + stats + record,
                      each registered as a Document (source_kind package)
  -> propose_connected_write (document_id)   an approval-gated copy into SharePoint
```

Nothing after `capture_source` happens without a human decision, and nothing here
puts a number into a verdict. See [What is deliberately not automated](#what-is-deliberately-not-automated).

## 1. Capture

`capture_source` collects a public file (an issuer's report PDF, a workbook, an
ESEF package) as a `Document`. It is the one tool built to keep *originals*:

- the **complete** body is stored, up to `TRET_EGRESS_CAPTURE_MAX_BYTES` (default
  25 MiB). A file over the limit is refused, never stored truncated;
- the stored file is named by its SHA-256 and the `Document` records it;
- each run may make at most `TRET_EGRESS_CAPTURE_MAX_PER_RUN` capture attempts
  (default 40); an attempt counts whether or not it succeeded, because a refused
  download still cost the bandwidth;
- `https` only, public addresses only, redirects re-checked hop by hop, and the host
  must pass the research allowlist. These are the same checks `fetch_url` makes (see
  [hardening.md](hardening.md), "What the research class checks");
- pass `expected_sha256` when re-collecting a source captured before. The new copy
  is stored either way, and the result says plainly if the file changed. A reviewed
  page range belongs to specific bytes, so a changed source needs re-review.

The capture `Document` has `source_kind='web'` and these provenance fields in
`meta`: `provenance='web_capture'`, `url`, `requested_url`, `redirects`,
`http_status`, `retrieved_at`, `content_type`, `bytes`, `sha256`, `headers`,
`fetched_by_run`, `body_truncated`, `expected_sha256`, `sha256_mismatch` and the
optional `operator_note`. Every retrieval is also a row in the egress log.

Report-only subagents are not granted `capture_source`: collecting an original is the
main run's act.

## 2. Read

`read_document` returns the extracted text with an unverified-web-source banner on
every read, not only the first. For a PDF the text carries a `## Page N` marker per
page, which is what lets the model say where a statement is. The banner repeats the
capture's SHA-256 and, if the source changed since the expected hash, says so.

Reading is how the model finds the disclosure. It is not evidence for a number:
nothing in a captured document is registered for citation.

## 3. Propose the page map

`propose_package_map` records a draft `package_map` finding. It packages nothing.

| Field | Meaning |
|---|---|
| `source_document_id` | a PDF Document attached to the run, whose stored file still hashes to its recorded SHA-256 |
| `title`, `role`, `scope` | what the package is. `role` is one of the packaging roles; `scope` is `standalone` or `embedded` |
| `keep_ranges` | inclusive `[first, last]` pairs of **physical** PDF page indices from 1; `null` only for a whole standalone report |
| `incorporations` | sections the retained pages incorporate by reference, and where each was found |
| `exclusions` | ranges considered and deliberately left out, with a reason |
| `review_note` | what the reviewer should look at |
| `stem` | file-name stem for the outputs (default: the source file name without extension) |

Things the model is told, and the tool enforces where code can:

- **Physical pages, not folios.** A range is a position in the PDF file counted
  from 1. The page printed "47" may be physical page 52. The packager records each
  retained page's source label next to its physical number so a reviewer can see
  both.
- **Embedded statements need explicit ranges.** A sustainability statement or
  assurance report inside a larger annual report is `scope=embedded` and must list
  the pages that are it. An embedded package may not keep every page: that is a
  whole report and should say so.
- **A whole standalone report uses `null`.** It is retained byte for byte.
- **Incorporated sections are kept in full.** When the statement says "see the
  Remuneration Report", the cited component is recorded in `incorporations` with the
  physical range where it actually is, and those pages are retained, not the first
  page of the section and not an index entry. A component the model could not find
  is recorded `unresolved`, not guessed. A `retained` record whose pages are not all
  in `keep_ranges` is refused.
- Ranges must be increasing, disjoint and in bounds. A mistake comes back as a tool
  error quoting the rule so the model can fix it.

The finding's `payload` carries the source (id, file name, SHA-256, size, page
count, provenance and URL), the map as proposed, the stem, `selected_page_count`
and `full_original`, and a `package` slot that stays `null` until approval. A
newer draft with the same subject supersedes an older one in the queue.

## 4. Approve, then package

A workspace approver sees the map in the Approvals queue and accepts or rejects it.
Rejecting packages nothing.

Accepting runs the packager (`services/packaging.py`) once the decision has
committed. The approval request returns immediately: packaging can take minutes,
so it runs in a background task on its own database session (one or two at a time,
depending on the machine; further approvals wait their turn and show as queued), and the Approvals page polls
the finding every five seconds until it settles. First the source file is hashed again: if its bytes are not the ones the
map was reviewed against, packaging fails with "source bytes changed since the map
was reviewed; recapture and re-review" and writes nothing. Otherwise it writes, under
`storage/packages/<finding id>/`, with the chosen stem:

| File | What it is |
|---|---|
| `<stem>.pdf` | the retained pages (the original, byte for byte, when everything is kept), page labels carried over, internal links remapped to retained pages |
| `<stem>.layout.md`, `<stem>.raw.md` | page-referenced text, one code block per retained page, headed with the original page number, its source label and the retained page number |
| `<stem>.page_map.json` | retained page to source page and label, plus both SHA-256s |
| `<stem>.links.json` | an audit of every link on the retained pages: preserved, remapped, pointing at a page that was not retained, unresolved, or external |
| `<stem>.extraction_stats.json` | characters, empty pages and replacement characters per mode, parser warnings and the phase they occurred in, link counts |
| `<stem>.package.json` | the package record: the approved map, source metadata, hashes, PDF state, tool versions, timings, limitations and the verification statement |

Each file is registered as its own `Document` with `source_kind='package'`,
`uploaded_by` empty, and `meta` linking it back: `package_finding_id`,
`package_role`, `source_document_id`, `source_sha256`, `retained_sha256`,
`text_mode`, `stem`, `title`, `role`, `scope`, `keep_ranges`. The finding
reads `running` from the moment of approval, in the same commit (`queued: true` until
the background task gets a packaging slot, then `queued: false` with
`started_at` restamped), and `package-retry` refuses (409) while it does, so two
attempts never race. A `running` record older than the 600-second deadline plus a
60-second grace is a worker that died (the server restarted mid-package, say): the
Approvals page then offers "Retry packaging" and the retry takes it over. A retry
likewise returns at once and packages in the background. On shutdown the server
waits up to its usual drain window (`TRET_SHUTDOWN_DRAIN_SECONDS`) for packaging
tasks to finish and never cancels one; a task still unfinished when the process
exits leaves its `running` record as it was. A directory left
under `packages/<finding id>` by a dead attempt, with no package Documents registered,
is removed on retry. The outcome
(`packaged` or `failed`, the document ids, hashes, page counts, `text_mode`,
Poppler version, wall time, parser-warning count, limitations) is written to
`payload.package` on the finding.

Packaging never fails the approval. A failure is a recorded fact on the finding,
with its reason, and `POST /api/findings/{id}/package-retry` runs it again (for
example after a source file is restored, or after a timeout). Packaging runs in a
sandboxed child process with a 600-second deadline and, on Linux, a 600 MB
address-space limit; at the deadline the child is killed and nothing is left
behind. On any failure nothing is left in the package directory and no
Documents are kept: a package is evidence, and existing package files are never
overwritten.

A `running` record can also be orphaned by a restart, since the task died with the
process. At boot, once the instance lock is held, every `package_map` finding still
`running` is marked `failed` ("server restarted during packaging; retry") so it can be
retried at once instead of after the 660-second window. If a packaging task itself
crashes (a database error on its final write, say) it records a `failed` result the
same way.

### OCR supplement

`propose_package_map` takes an `ocr` parameter: `off` (the default), `auto` (OCR only
the retained pages whose native layout text is empty, such as a scanned assurance
statement) or `all` (every retained page; for native text that is present but
unreliable). When it is not `off`, the package gains an eighth file,
`<stem>.ocr.md`, a separately labelled supplement. It is registered as its own
searchable `Document` (`package_role` `ocr_text`), headed per page with the original
page number, source label and retained page number, and labelled as OCR.

The supplement never replaces anything: the native `layout.md` and `raw.md`, the
retained PDF and every hash are unchanged. Pages are rendered at 200 dpi and read with
Tesseract, English only, page segmentation mode 6, at most 60 pages per package (the
rest are listed as truncated). OCR confidence is not accuracy: a high score says the
engine was sure, not that the text is right, so the text is not exact transcription and
the retained PDF controls. If Poppler's renderer or Tesseract is unavailable, the
package is still produced without the supplement and a limitation records that OCR was
requested and could not run.

### What verification establishes, and what it does not

Before anything is published the packager checks that the retained PDF has exactly
the selected number of pages, that the extracted text of every retained page equals
the extracted text of its source page, and the SHA-256 of both files. For a whole
original it also checks byte equality. All outputs are staged and moved in only
after every check passes.

That establishes **content extraction and page retention**. It does not establish
reading order, table accuracy, chart reconstruction, completeness of the reviewed
scope, or visual layout fidelity. The Markdown says so in its header and the record
says so in `verification.does_not_establish`; the PDF controls. PDF tagging,
bookmarks, attachments, forms and signature validity are not certified in a page
extract, and external linked documents are audited, not crawled.

Text comes from Poppler's `pdftotext` (`-layout` and `-raw`) when it is installed,
which is the case in the shipped images. Without it the packager falls back to
`pypdf` for both files, still produces a complete package, and records
`text_mode: "pypdf"` plus a limitation noting the lower layout fidelity. Encrypted
sources get one attempt at the empty password, which is how issuers usually ship
them; anything needing a real password is refused.

When a reader opens a package document with `read_document`, the banner says it is
a reviewed package extract derived from the source document and gives the retained
SHA-256 and `text_mode`. It still says that no number in it may be cited.

### The Disclosure Collector harness

A workspace gets a "Disclosure Collector" harness seeded for it, but only while the
research class is on at seed time: `TRET_EGRESS_RESEARCH` must not be `off`, and the
master `TRET_EGRESS` switch must be on too (it caps every class). A deployment with
research off never gets the harness. It is selectable in the Chat harness dropdown
(and runnable from the Workbench) and is what carries `web_search`, `fetch_url`,
`capture_source`, `propose_package_map` and `propose_connected_write`, so a collection
run needs no pack authoring. Nothing it can do skips a gate: every page map and every
upload is still a draft waiting for an approver.

It is found by its name, "Disclosure Collector", and its tool list is topped up at
every boot: there is no marker for a tool an operator removed on purpose, so a removed
tool comes back at the next restart. To turn the harness off, archive it. (Renaming
it makes the seeder look for a new one.)

Its routing policy is `{"mode": "auto", "min_cost_tier": "standard", "max_cost_tier":
"standard"}`. Collection runs are tool-heavy, and a weak model loops (a live run on an
economy-tier model spent 22 of 24 iterations on `web_search`), so the router may only
pick a standard-tier model. `min_cost_tier` is a hard floor, honoured wherever
`max_cost_tier` is; it needs a standard-tier model whose provider has a key, or the run
fails with "No candidate models". A harness still carrying the previous seed policy
(`{"mode": "auto", "max_cost_tier": "standard"}`, exactly) is upgraded at boot; a policy
an operator has edited is left alone.

Two per-run limits keep a run that cannot reach a source from spinning:
`TRET_EGRESS_RESEARCH_MAX_SEARCHES_PER_RUN` (default 12) caps `web_search`, and when a
fetch or capture is refused because the host is not on `TRET_EGRESS_RESEARCH_ALLOW_HOSTS`,
the tool error says so, says that searching again will not help, and asks for an operator
to add the host. At the search cap the error names every host refused that way during the
run.

## 5. Write back

A package can be copied into the workspace's SharePoint/OneDrive. The run proposes
that with `propose_connected_write` and a `document_id`, once per file, with a
`filename` such as `runs/<run-id>/packages/<company>/<file>` (a filename or path
segment may not contain `%` or `#`); a person approves each upload and the bytes
uploaded are the bytes approved (see
[connections.md](connections.md#what-a-run-can-propose-and-where-it-lands)). Nothing
is overwritten, and a document upload fails if its name is already taken rather
than landing as a renamed copy beside a manifest that names the original. Google
Drive has no write path.

## Acceptance dimensions, and where each one lives

| Dimension | What a reviewer asks | Where tret keeps it |
|---|---|---|
| Discovery | How was this source found, and is it the right one? | `web_search` is navigation only and carries no evidence. The run transcript and the egress log show what was searched and fetched; the `capture_source` `note` and the finding's `review_note` say why this source |
| Capture | Is this the complete original, and can it be verified later? | The `source_kind='web'` Document: SHA-256, `url`, `redirects`, `http_status`, `retrieved_at`, headers, `body_truncated`, `sha256_mismatch`; the stored file; the egress row |
| Completeness | Do the retained pages cover the disclosure and what it incorporates? | The `package_map` finding (`keep_ranges`, `incorporations`, `exclusions`, `review_note`) and the approval; `page_map.json`; the link audit (`target_page_not_retained`, `unresolved_source_destination`) |
| Extraction quality | Is the text usable? | `extraction_stats.json` (empty pages, replacement characters, parser warnings by phase), `text_mode` and Poppler version, the `limitations` list |
| QA | Who checked it, and against what? | The `Approval` row (approver, time, note); the verification block in `package.json` (what was checked, what it establishes and does not); the source and retained SHA-256 on every package Document; `payload.package` including retries |
| Analytical eligibility | May a number from this be used in a verdict? | Not here. Captured and package documents are third-party text and are never registered for citation. A figure becomes usable only when an operator promotes it into a dataset and it comes back through `lookup_dataset` ([trust-doctrine.md](trust-doctrine.md)) |

## What is deliberately not automated

- **Choosing pages.** Code does not guess a disclosure's range, a statement's end or
  where an incorporated section sits. Every range is the model's proposal and a
  person's decision.
- **OCR by default.** A scanned PDF with no text layer packages (the pages are retained)
  but its native text files are empty for those pages, and the stats list them. Images
  are read only when the map asks for the OCR supplement above, and then only into a
  separate, labelled file.
- **Downloads a browser would get.** `capture_source` makes a plain HTTPS request. A
  page that needs JavaScript, a login, a click-through or a bot check is not
  collected; a person downloads the file and uploads it, which gives it
  `source_kind='upload'` and the same packaging path.
- **Promoting numbers.** Reading a disclosure never produces a citable figure. See
  above.
- **Crawling links.** External and unretained link targets are listed for follow-up,
  not fetched.
- **Repairing sources.** A parser repair of a damaged PDF is recorded as a warning
  in the stats and the record, not hidden.

## Deployment

The collection tools are off until an operator turns them on.

| Setting | Effect |
|---|---|
| `TRET_EGRESS_RESEARCH` | `off` (default) withholds `web_search`, `fetch_url` and `capture_source` from every run; `replay` serves earlier snapshots without new requests; `on` enables them |
| `TRET_EGRESS_RESEARCH_ALLOW_HOSTS` | comma-separated hosts the research class may reach, subdomains included. Empty means the public web, and production boot warns about it. For this workflow, list the issuers' domains |
| `TRET_SEARCH_PROVIDER` | the search backend for `web_search` (its host rides the separate `search` egress class and need not be in the allowlist) |
| `TRET_EGRESS_CAPTURE_MAX_BYTES` | largest file `capture_source` stores, default 26214400 (25 MiB); bigger is refused |
| `TRET_EGRESS_CAPTURE_MAX_PER_RUN` | capture attempts per run, default 40 |

`propose_package_map` itself makes no network request; it works on documents already
in the project, so it is available whether or not research is enabled (a PDF someone
uploaded can be packaged the same way). A pack opts in by listing it in a task type's
tools and a harness's `tool_names`.

The Docker images install `poppler-utils`, so `pdftotext` is on the path and packages
record `text_mode: "pdftotext"`. A deployment without it still packages with the
`pypdf` fallback and says so. Packages are written under `TRET_STORAGE_DIR/packages/`.
