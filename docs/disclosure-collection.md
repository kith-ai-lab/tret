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
  62914560, 60 MiB). A file over the limit is refused, never stored truncated;
- the stored file is named by its SHA-256 and the `Document` records it;
- each run may make at most `TRET_EGRESS_CAPTURE_MAX_PER_RUN` capture attempts
  (default 40); an attempt counts whether or not it succeeded, because a refused
  download still cost the bandwidth;
- `https` only, public addresses only (carrier-grade NAT addresses, 100.64.0.0/10,
  count as non-public), redirects re-checked hop by hop, and the host must pass the
  research allowlist unless `TRET_EGRESS_CAPTURE_ANY_PUBLIC_HOST` is on (below). These
  are the same checks `fetch_url` makes (see [hardening.md](hardening.md), "What the
  research class checks");
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

**Capturing from any public host.** Set `TRET_EGRESS_CAPTURE_ANY_PUBLIC_HOST=true`
(default off) and `capture_source`, only it, may fetch an original from any public
`https` host. Every other check is unchanged: DNS resolution and the private-address
test on every hop, https only, standard ports, the byte ceiling and the `%PDF-` check
for PDFs. `fetch_url` and `web_search` stay behind `TRET_EGRESS_RESEARCH_ALLOW_HOSTS`.
This is acceptable for captures because captured bytes are stored as untrusted
documents and never become facts or reach the model as page text on their own; a
`fetch_url` result goes straight to the model, which is why it stays restricted. The
operational reason is that issuers serve reports from CDNs and document hosts
(scene7, akamai and the like) that nobody can list in advance, and without the flag
each target needs an allowlist entry first. The research mode (`off`, `replay`, `on`)
still governs: the flag widens hosts, not whether research is on. With the flag off,
a capture to a host outside the allowlist is refused as described under "Two per-run
limits" below.

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

The check that retained pages match the source runs `pdftotext -f/-l` over only the
selected page ranges of the source, so a 600-page report is not extracted whole. If
Poppler times out or fails on the source, packaging still succeeds, the manifest records
`verification.oracle: {status: skipped, reason}`, and the package's limitations say the
pages were not compared with the source text. A Poppler failure on the retained subset
still fails packaging.

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

Its routing policy is `{"mode": "auto", "max_cost_tier": "standard", "objective": "eco"}`.
Collection runs are tool-heavy, so the router may not climb past the standard tier, and
within it prefers the cheapest, lowest-energy candidate (the same 12 registers written
12/12 under `balanced` and `eco`, at a quarter of the cost). There is no `min_cost_tier`
floor. One was added after a live run on an economy-tier model spent 22 of 24 iterations
on `web_search` (4 Nov 2026); it was removed because a per-run `web_search` cap
(`TRET_EGRESS_RESEARCH_MAX_SEARCHES_PER_RUN`, 12) now stops that loop, because price tiers
exclude almost every open-weight model regardless of capability, and because routing
evidence and exploration now decide which models earn Collector runs. A harness still
carrying a previous seed policy (`{"mode": "auto", "max_cost_tier": "standard"}`, the same
with `"min_cost_tier": "standard"`, or that plus `"objective": "eco"`, exactly) is upgraded
at boot; a policy an operator has edited is left alone.

The seeded `loop_config` is 40 iterations, 4096 output tokens, temperature 0.3 and a
`max_cost_usd` of 8.0: each run is capped at $8 (the engine default of $5 stopped long
reports mid-map). A harness still carrying either previous seed `loop_config` exactly (24
or 40 iterations, no cap) is upgraded at boot; one an operator has edited keeps its own,
including its own cost cap.

Two per-run limits keep a run that cannot reach a source from spinning:
`TRET_EGRESS_RESEARCH_MAX_SEARCHES_PER_RUN` (default 12) caps `web_search`, and when a
fetch (or, with `TRET_EGRESS_CAPTURE_ANY_PUBLIC_HOST` off, a capture) is refused because
the host is not on `TRET_EGRESS_RESEARCH_ALLOW_HOSTS`,
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

A [collection run](#6-collection-runs-batches) does this for you for the packages an
approver has approved, without a per-file proposal.

## ESEF filing fallback

Some issuers refuse an automated download of their own PDF (HTTP 403), or publish the
sustainability statement only inside the annual report they file under ESEF. When a target
carries a `source_filing_url`, the Collector's task says so, and it may call `capture_filing`
**only** if the issuer's own copy is refused or does not exist for that fiscal year. The
issuer's PDF always stays first, and a filing for a different fiscal year is never used. The
tool takes no URL from the model: it fetches the one ESEF package (a ZIP) for the run's own
target from filings.xbrl.org, stores the original ZIP as a document, and gives the run the
report's text in units: `## Page N` when the filing is a PDF conversion, otherwise
`## Section N: heading`. A page map over it uses `unit` `filing_page` or `filing_section`
(a map without `unit` is a PDF map, as before). The package ceiling is
`TRET_ESEF_FILING_MAX_BYTES` (default 200 MiB); a larger package is refused, not truncated.

An approved filing map packages to `<stem>.extract.html` (text and table cells regenerated
from the filing, no images, scripts or links), `<stem>.layout.md`, `<stem>.page_map.json`
(unit, printed folio or heading per retained unit), `<stem>.extraction_stats.json` and
`<stem>.package.json`. There is no PDF, and verification is a text read-back of the selected
units (see [what verification establishes](#what-verification-establishes-and-what-it-does-not)).
All five files are written back under the same size cap as any package.

Review differs in what the approver checks: ranges are filing pages or sections, not PDF
pages, and printed folios can differ from filing page numbers. The review screen and the
written-file viewer label such a package "From the regulated ESEF filing (not the issuer's
PDF)" with the filing link, and the package's limitations say the same. The run's outcome
summary must state that the package comes from the ESEF filing and why. The evaluator's
evidence names the unit and carries a note that its rubric's "retained pages" means filing
pages or sections for those maps; it judges whether the issuer copy was really unavailable.
Filing units are left out of the page-count metrics (retained and source pages, the
cross-batch main-document check), because they are not PDF pages; the batch manifest
reports them separately as `retained_filing_documents` and `retained_filing_units`.

## 6. Collection runs (batches)

> The Collections page is now **Batches**, and this workflow is its `disclosure` profile.
> See [batch-runs.md](batch-runs.md) for batches in general, including the `prompt` profile.

Sections 1 to 5 describe one disclosure. A **collection** is the same workflow run for
a register of target companies, one engine run per company, with the approvals,
packaging and SharePoint uploads tracked per target and a set of run-level records
written at the end. It adds bookkeeping and parallelism. It adds no new authority:
every page map is still a draft until an approver decides it.

A collection is made of two kinds of row. The **collection** holds the settings
(write target, concurrency, gate, fiscal year) and the register warnings. A **target**
is one register row: its run, its page-map findings, the outcome the run reported, its
status and the files written for it. A target's status is never advanced by hand. It is
re-derived from the run's status, the state of its `package_map` findings and the
packaging results every time any of those changes.

Reading is open to every workspace member. Everything that changes state (create,
start, pause, resume, release, retry, finalize, and deciding maps) needs the approver
role. The API is under `/api/collections`; the Collections page is the front end for it.

### The register

One row per company. As CSV the header row is:

```
company,market,domicile_country,sector,category,expected_form,fiscal_year,known_urls,notes
```

Optional columns after these: `slug`, and the identifier columns `lei`, `cik`, `ticker` and
`source_filing_url` (below). Any other column is refused, and an error names the row it found
the problem on. Blank cells take the default.

| Column | Meaning |
|---|---|
| `company` | required, at most 200 characters |
| `market` | required: `US`, `UK` or `EU` |
| `domicile_country`, `sector` | free text, shown to the run and carried into the manifest |
| `category` | `financial`, `industrial` or `other`; blank is allowed and counts as none of them |
| `expected_form` | `standalone`, `embedded` or `unknown` (the default). Whether the disclosure is expected to be its own report or a section of an annual report |
| `fiscal_year` | the year wanted; blank takes the collection's fiscal year |
| `known_urls` | up to 20 `https://` URLs separated by `\|`. The run opens these first, exactly as given |
| `notes` | at most 2000 characters, passed to the run |
| `slug` | optional. Lowercase letters, digits and hyphens, at most 40. It names the target's folder and the file stems. Left out, it is derived from the company name (corporate suffixes such as plc, SE, Inc. dropped) and made unique |
| `lei` | optional. Exactly 20 letters or digits, upper-cased on input |
| `cik` | optional. 1 to 10 digits, stored without leading zeros |
| `ticker` | optional. 1 to 12 letters, digits, dots or hyphens, upper-cased |
| `source_filing_url` | optional. An `https://` URL of at most 2000 characters |

A register has at most 500 targets. The API also accepts the same rows as JSON
(`register`) instead of `register_csv`; exactly one of the two must be given.

Creation also checks the sample against the briefing and returns **warnings**, never
errors: a market with no targets; fewer than 3 `financial` or fewer than 5
`industrial` targets in a market; fewer than 6 `expected_form=embedded` targets in UK or
EU. The warnings show in a box on the collection and are copied into
`target_register.json`. A register that trips them can still be run.

#### Identifier columns

`lei`, `cik`, `ticker` and `source_filing_url` are stored together in one nullable JSON
column on the target (`identifiers`, keys only for the values given) and returned with the
target by the API, including in a shared batch. When a target has them the run's task adds
one line (`Identifiers: LEI ...; ticker ...`) and, for a `source_filing_url`, a line naming it
as the filing of record, to be used only to confirm the reporting entity and fiscal year and
never captured as the document. A target without identifiers gets exactly the task it got
before.

#### Building a register from the ESEF filings index

`tret batch register-esef` writes a register CSV of EU and UK companies from the yearly index
at filings.xbrl.org. It runs offline of any Tret server (no URL, token or database).

```sh
tret batch register-esef --year 2024 [--countries FR,NL] [--exclude-countries GB] \
    [--top 500] [--rank-by revenue|none] --out register.csv \
    [--index-file PATH] [--cache-dir PATH] [--min-interval 1.5] [--limit-fetch N]
```

It drops filings whose period end is not a date in the requested year, entities whose
identifier is not a LEI, and filings from Ukraine; keeps one filing per LEI (fewest errors,
then latest processed); and writes one row per company: market `UK` for filing country `GB`
and `EU` otherwise, `domicile_country` set to the filing's country (the filing jurisdiction,
which the notes say, and not necessarily the company's home), category `other`, expected form
`embedded`, no known URLs, and the LEI and the filing's report URL as `source_filing_url`.
`--rank-by revenue` fetches each filing's xBRL-JSON, one request at a time with the minimum
interval, back-off on HTTP 429 and 5xx, and a size cap per response, and orders companies by
`ifrs-full:Revenue`, else `RevenueFromContractsWithCustomers`, else `InsuranceRevenue`
(undimensioned, latest period end, preferring a full-year period over a shorter one) converted to EUR with a rough built-in
exchange table; a company with no revenue fact is sized as 5% of its assets and marked
`assets_proxy`, and one with neither sorts last. Only the extracted figure is cached
(`--cache-dir`, default `<out>.cache`), so a rerun resumes without refetching;
`--limit-fetch` caps the fetches in one invocation. Next to the register it writes
`<out>.ranking.csv` with every candidate and its size, which is for ranking only and is not
a financial figure.

An ESEF report is inline XBRL, not a PDF. The importer supplies the target list and the
filing of record; the Collector still looks for the issuer's sustainability disclosure PDF
first and uses the filing only as the [fallback below](#esef-filing-fallback).

### Creating a collection

A collection can also be started from a chat with `start_batch` (`profile: "disclosure"`),
giving the register as an attached CSV document (`register_document_id`) or inline text
(`register_csv`) and the write target as `target_slug`. It creates the same batch as the
form below, with the chat's run as its parent; see [batch-runs.md](batch-runs.md).

On the Collections page choose New collection. The write target must be a Microsoft 365
write target that an admin has already connected with write access (Settings,
Connections); without one the page says so and the API refuses with 422.

| Setting | Meaning |
|---|---|
| Name, run slug | the slug is 3 to 61 lowercase letters, digits and hyphens, unique in the workspace. It becomes a folder name |
| Write target | the SharePoint folder everything is written under, always inside its `tret/` subfolder |
| Write root | one or two folder names, default `runs`. Package files go to `<write root>/<run slug>/packages/<target slug>/<file>`, the records to `<write root>/<run slug>/` |
| Concurrency | targets executing at once for this collection, 1 to 6 (default 3) |
| First batch | `0` means no gate. Otherwise only the first N targets in register order start; the rest are `held` until released |
| Fiscal year | the year wanted for a target that does not set its own (default 2025) |
| Auto-write | on (default): package files are uploaded when packaging succeeds. Off: packages stay in tret and the target ends `completed` |
| Model override | optional model id used for every target run instead of the router's choice |
| Max iterations | optional, 1 to 50: the iteration cap for every target run, in place of the harness value (the Disclosure Collector is seeded at 40) |

The collection starts as `draft`; Start launches the first targets. A workspace without
a "Disclosure Collector" harness cannot start one (the harness is seeded at boot while
the research class is on; see [The Disclosure Collector harness](#the-disclosure-collector-harness)).

### What happens to one target

Each target gets its own run on the Disclosure Collector harness, with a prompt built
from the register row. No person is in the run; it is told not to ask questions. The
prompt carries the target's facts and the briefing's doctrine: collect the wanted fiscal
year where it is published, and if only an earlier period exists collect that and say so
rather than jumping to a later year; prefer issuer sources, then official repositories
(SEC EDGAR, the FCA National Storage Mechanism, ESEF portals), and report a third-party
copy as a substitution with the reason; keep the disclosure, its assurance statement,
substantive data appendices and material incorporated by reference, and leave general
financial statements out; take page ranges only from pages actually read; record the
period, publication date (with its basis) and assurance date separately, with unknown
left unknown. The procedure is: open the known URLs or search a few precise queries
(without looping), `capture_source` the PDFs, `read_document` to confirm company, period
and layout (in a long report it finds the sustainability statement, its assurance report and any appendix with `search_documents` on the contents-page titles and reads forward from the hit's `chunk_ordinal`, never guessing ordinals or paging from the start), `propose_package_map` for each document worth keeping (with stems
`<slug>`, `<slug>-assurance`, `<slug>-appendix` and `<slug>-container-<topic>` for each
further document, every stem unique within the target, and `ocr="auto"` for
scanned pages; the tool refuses a stem another map in the run already uses, and the
collection refuses to write two packages whose file names collide, so one package can
never replace another's files on SharePoint; the role is one of the packaging roles `primary_disclosure`,
`embedded_statement_and_assurance`, `assurance`, `data_appendix`, `incorporated_context`,
`methodology` and `other`, each described in the prompt), then finish. The run is told not to call `propose_connected_write`: the
collection writes approved packages itself.

The last step is `report_collection_outcome`, called once, even when nothing was found.
It is how the target is scored. It takes `status` (`captured`, `substituted`, `gap` or
`blocked`) and a `summary`, and optionally the reporting period label, start and end,
publication date and its basis, disclosure type, framework, language, `fy2025_gap` (true
when the period collected is not the requested fiscal year, whatever year that is), a
substitution note, `blocked_urls` and `candidate_urls`. A second call replaces the first.
Outside a collection run the tool refuses. This metadata is the model's report of what it
read. It is recorded as reported and not independently checked.

The run has the same limits as any other: the search cap and capture budget in section 4,
and a wall-clock limit (below). A run that exceeds it is cancelled and the target fails
with `timeout`.

### Target status

| Status | Meaning | What the operator does |
|---|---|---|
| `pending` | will launch when a slot is free | nothing |
| `held` | beyond the first batch; waiting for the gate | Release, below |
| `running` | its run is executing | nothing |
| `review` | the run finished and at least one of its page maps is still a draft (a run that ended failed or cancelled but proposed maps lands here too, with the run failure shown as a warning) | decide the maps (batch approval, below) |
| `packaging` | every map is decided and at least one approved map is being packaged | nothing; it settles on its own |
| `written` | every approved map is packaged and every file is uploaded | nothing |
| `completed` | every approved map is packaged; auto-write is off, so nothing was uploaded. Also a target packaged after its collection was finalized: nothing is uploaded and the error reads "collection finalized before write" | nothing, or write the files by hand; after finalize, Retry write, then finalize again |
| `gap` | the run ended with no page map. The outcome says `gap` or `blocked`, or the error says "run ended without a package map" | read the outcome summary and the candidate and blocked URLs; Retry, or leave it as a recorded gap |
| `failed` | the run failed, was cancelled, timed out, crashed or was killed by a restart, and proposed no page map; or an approved map's packaging failed | read the error; Retry (a new run) or, for a packaging failure, fix the cause and use package-retry on the finding in Approvals |
| `rejected` | every page map was rejected | Retry for a new attempt, or leave it |
| `write_failed` | packaging succeeded but an upload did not; files already uploaded are kept | fix the cause (connection, scopes, a name already taken), then Retry write |

A run that ended failed or cancelled but had already proposed page maps is judged by
those maps like any other run (the approver still decides them) and the run failure is
shown as a warning on the target. Only a run with no maps becomes `failed`.

The collection's own status follows from its targets, in the order below, except
`draft`, `paused`, `finalized` and `failed`, which stay until the operator moves them
(`failed` is reserved and not used).

| Collection status | Meaning |
|---|---|
| `draft` | created, not started |
| `running` | any target is `running` or `pending` |
| `review` | nothing running or pending, and at least one target is in `review` or `packaging` (this outranks `gate`, so decisions still owed stay visible) |
| `gate` | nothing running, pending, in review or packaging, and some targets are still `held` |
| `completed` | every target is in a final state |
| `paused` | an operator paused it, or (with `TRET_BATCHES_AUTO_RESUME=false`) a restart paused it (below) |
| `finalized` | the run-level records were written |

### The review gate

With a first batch of, say, 12, the collection runs those 12 and then sits at `gate`.
The gate opens as soon as the first batch has nothing left to launch. It does not wait
for their maps to be decided, so the sensible order is to decide and inspect the first
batch's packages (the page ranges, the incorporated sections, the link audits, the
files in SharePoint) before you press **Release remaining**. Release is allowed from `gate`,
`review`, `completed` or `paused` whenever held targets exist (409 otherwise). It turns
every held target into `pending` and launches them, or leaves them pending if the
collection is paused. There is no second gate.

### Batch approval

A collection's draft page maps are listed together on its page ("Package maps awaiting
review"), each with the target, role, scope, page ranges, the source and the reviewer
note. Select some or all, add an optional note, and Approve selected or Reject
selected. This calls `POST /api/findings/decide-batch`, which decides each finding
exactly as the single approval does, one by one: one that cannot be decided (already
decided, not found) reports its own error and does not stop the others. Up to 200
findings per request.

Approving a map is the approval for everything that follows from it, as in section 4: it
starts packaging. With **auto-write on**, it is also the approval to write that package's
files to SharePoint, under `<write target>/tret/<write root>/<run slug>/packages/<target
slug>/`, with each upload attributed in the connection audit log to the approver of that
map. There is no separate upload approval per file inside a collection, which is why
batch approval should be done by reading the maps, not by pressing select-all. With
auto-write off nothing is uploaded. Uploads replace: re-uploading an approved
package overwrites the previous copy under `runs/<run_slug>/packages/<slug>/`, because
the collection owns that folder. An upload that fails sends the target to `write_failed`,
keeping the files already uploaded; a retry skips documents already recorded as written.

Packaging is the same sandboxed, verified packager as section 4, at most two at a time
whatever the number of collections. Maps approved faster than that wait their turn.

### Retry, pause and resume

- **Retry** starts a new run for a `failed`, `gap`, `rejected` or `write_failed` target.
  It is refused on a `draft` or `finalized` collection. The attempt count rises, the
  earlier run stays in the run list, and the previous attempt is archived in the
  target's `history` with its outcome, error, cost, model and run id. Target and
  collection cost include earlier attempts.
- **Retry write** re-attempts the uploads of a `write_failed` target, or of a target
  packaged after its collection was finalized. Files already uploaded are not uploaded
  again.
- **Pause** is allowed while the collection is `running`, `gate` or `review`. It stops
  new launches. Runs already executing finish, and approvals, packaging and uploads
  carry on. A target still waiting for a process-wide slot goes back to `pending`.
  **Resume** launches the pending targets again.
- **A restart.** Runs and packaging tasks live in the server process. At boot, the sweep
  re-settles every target that was `running`, in `review` or `packaging`: one whose run
  was killed by the restart (and proposed no map) becomes `failed` (retry); the others
  settle to `review`, `packaging`, `written` and so on from their maps. A collection that
  was `running` resumes on its own: its launch loop starts again in the background once startup
  (sweeps and extension startup tasks) has finished, `last_resumed_at` and `last_resume_note` record it, and a target whose
  run the restart cut off (the run error starts `process_restart`) goes back to `pending`,
  its attempt archived in `history` like a manual retry. A target is put back at most twice
  this way; after that it stays `failed` with an error saying how many restarts
  interrupted it, and an operator retries it. A collection is auto-resumed at most twice in
  a row: if the server restarts a third time without any row settling for another reason, it
  is `paused` with a note about repeated restarts and its cut-off rows stay `failed`. A
  collection an operator had paused stays paused. With `TRET_BATCHES_AUTO_RESUME=false` the old behaviour applies: a collection
  that still has pending targets is set to `paused` with a note that the server restarted,
  nothing launches at boot, and the operator presses Resume, then Retry on the failed
  targets. Other collections settle to `review`,
  `gate` or `completed`. On a clean shutdown the server waits for in-flight target runs
  the same way it waits for packaging, and launches no new target while it drains.

### Finalize

Finalize writes six files to `<write target>/tret/<write root>/<run slug>/`. It is
available from `gate`, `review`, `completed`, `paused` and `finalized`, not while the
collection is `draft` or `running`. It is refused (409) while any target is `running`,
`pending` or `packaging`, or any package map is still a draft (approve or reject it
first). Pressing it is the approval for writing these records; the page
asks you to confirm the folder first. The same content can be previewed beforehand
(`GET /api/collections/{id}/records`, the Preview records button). The files are
modelled on the pilot's manifest, execution record and `SHA256SUMS`.

| File | Contents |
|---|---|
| `manifest.json` | the run, the sample by market, outcome counts (written, completed, gap, failed, rejected, retained documents and pages), the completeness definition, and per company: status, reported outcome, reporting period, publication date and basis, disclosure type, framework, language, FY gap and substitution note, and per document the stem, role, scope, kept ranges, source URL, SHA-256 and page count, the retained SHA-256, page count and files with their SharePoint links, the approver, decision time and note, packaging timings and limitations |
| `execution.json` | start, finalize time and measured elapsed seconds; one row per target run (run id, status, model, iterations, catalog-priced cost, provider-reported cost, wall seconds, attempts); totals; packaging seconds and OCR pages per target; concurrency, model override, tret and packaging versions |
| `exception_log.json` | every target that did not end `written` or `completed`, and every substituted one: status, error, outcome summary, blocked and candidate URLs, substitution note, attempts |
| `target_register.json` | the register as run (positions, slugs, resolved fiscal years), the first-batch size and the creation warnings |
| `SHA256SUMS.txt` | `sha256  path` for the five other record files plus every package file written, paths relative to the run folder (`packages/<slug>/<file>`). Written last. With auto-write off it lists only the five records |
| `results_summary.md` | a table per market of company, status, retained pages and note, and the status totals |

The completeness line in the manifest reads "captured core disclosure within reviewed
scope; not independently certified complete", and means exactly that. Writes use
`conflict="replace"`, so finalizing again overwrites the six files at the same names.
`SHA256SUMS.txt` is uploaded last. If any file fails, the collection records the error,
stays unfinalized and shows it; fix the cause and finalize again. After a finalize,
later packaging uploads nothing: the target is `completed` with the error "collection
finalized before write". Use Retry write on it, then finalize again so the records and
`SHA256SUMS.txt` match.

### Cost and instrumentation

`execution.json` records, per run, the model used, iteration count, wall seconds and two
costs. `cost_usd` is priced from token counts at the catalog rate, includes cost
delegated to subagents and includes earlier attempts of a retried target;
`current_run_cost_usd` is the current run alone, so the file shows both. `reported_cost_usd` is what the provider said it charged, where
the provider says. The two can differ, and neither is an invoice. The collection page
shows the first. Packaging makes no model call and costs none; its wall seconds are
recorded separately. Run seconds are summed over targets that overlapped, so the sum
exceeds elapsed time. `measured_wall_seconds` is the elapsed figure and the file says
so in `timing_notes`.

### Limits

| Limit | Value |
|---|---|
| Targets executing at once, per collection | 1 to 6 (the setting) |
| Targets executing at once, whole process | `TRET_COLLECTIONS_MAX_CONCURRENT` (default 3, clamped 1 to 6); the lower of the two applies |
| Wall-clock limit per target run | `TRET_COLLECTIONS_RUN_TIMEOUT_SECONDS` (default 1200); on expiry the run gets 30 seconds to stop, then the target is `failed` with `timeout` |
| Packaging jobs at once | 2 (1 on a single-CPU machine), shared with ordinary approvals |
| `web_search` calls per run | `TRET_EGRESS_RESEARCH_MAX_SEARCHES_PER_RUN`, 12 |
| `capture_source` attempts per run | `TRET_EGRESS_CAPTURE_MAX_PER_RUN`, 40 |
| Captured file size, and the largest file a collection will upload | `TRET_EGRESS_CAPTURE_MAX_BYTES`, 60 MiB |
| Targets per register / known URLs per target | 500 / 20 |

Each running target holds a database session, so check the database connection pool
before raising concurrency on a small database.

Text extraction of a captured PDF over 2 MB gets at least 180 seconds, and a budget
that grows with the file beyond about 37 MiB: 30 seconds plus 4 seconds per MiB, at most
600 seconds (a 48 MB registration document gets about 222 seconds; the cap is reached at
about 142 MiB). Poppler itself is allowed the same time, where it used to be stopped at
120 seconds while the caller waited on. Uploads of package files to SharePoint send 5 MiB chunks with a
180-second timeout each, and a target whose upload still fails is `write_failed`
and can be retried from the Collections page without re-running the model.

#### Sizing the machine

The first live batch (12 companies, concurrency 3, two packaging jobs) on a
one-vCPU, 1 GB machine ran at a load average near 3 for 25 minutes: two health
checks blipped, two SharePoint uploads timed out and were retried, and two large
PDFs ran out of extraction time. On such a machine set
`TRET_COLLECTIONS_MAX_CONCURRENT=2`; for a 48-target register give the process two
vCPUs and 2 GB. Packaging and OCR are CPU-bound and share the machine with the
running targets.

### The first run

Do not start with 48. Make a collection of the 12 pilot companies with their known URLs,
concurrency 3, a first batch of 12 and auto-write on to a test folder (a write target
and root you can delete from). Read every map, check what landed in SharePoint against
the page ranges, read the exception log, and look at the cost and wall time per target.
Then create the 48-target register as a second collection with a new run slug, following
the briefing's sample shape (the warnings tell you when it drifts), a first batch of 12 so the first dozen can be
checked before the rest are released. Finalize each
collection when its targets have settled.

## Stage 1 normaliser (experimental, off by default)

`Document.extracted_text` is a poor input for evaluating a report: `pdftotext -layout`
flattens tables, and the text stops at 2,000,000 characters, which is often before the
sustainability statement of a long report. The normaliser turns each captured report into
one Markdown file with real Markdown tables, plus quality metrics and a quarantine status.
The Markdown lives in the blob store (`normalised/<document id>/<version>.md`, see
[storage.md](storage.md)), never in Postgres; the `disclosure_reports` table holds the key,
the SHA-256, the metrics and the status, one row per document and normaliser version.

It is behind `TRET_DISCLOSURE_NORMALISER` (`off` by default). Nothing runs it automatically:
an operator runs it per batch.

```bash
TRET_DISCLOSURE_NORMALISER=on tret disclosure normalise --batch <batch id> \
    [--limit N] [--target <slug>] [--only-status review] [--force]
tret disclosure normalise-file report.pdf --out ./out     # no database, no setting needed
```

`normalise` handles every PDF and ESEF package a target's run fetched
(`Document.meta.fetched_by_run`) and marks the one with the most Markdown as `is_primary`;
rerunning at the same version changes nothing unless `--force` (a forced rerun that fails
keeps the previous good report and notes `metrics.last_error`). The documents of a target are
those it fetched plus those in its run's `document_ids` (a reused capture); a document two
targets share stays linked to the first, the others listed in `metrics.also_linked_targets`. `normalise-file` writes
`<stem>.md` and `<stem>.metrics.json` for a manual check. Read-only routes serve the results:
`GET /api/disclosure-reports?batch_id=`, `/{id}` and `/{id}/markdown` (404 while the setting
is off).

**PDF, two passes.** Pass 1 is `pdftotext -layout` over every page, uncapped. Pass 2 sends
only the table pages to [docling](https://github.com/docling-project/docling) (OCR off, CPU,
2 threads), `TRET_NORMALISER_CHUNK_PAGES` pages at a time, each chunk in its own child process
(`python -I -m tret.services.normalise_worker`) with a wall-clock timeout and a resident-memory
guard. Docling needs 9 to 12 GB for a whole document, which is why it never sees one; measured
with three-page chunks (OCR off) its child peaked at 2.1 GB on a 192-page report and 2.4 GB
on a 408-page one (five-page chunks: 3.0 and 3.3 GB, about 40% faster). A chunk that fails, times out or is
killed leaves its pages as pass-1 text, noted in the metrics (`table_pages_fallback`,
`chunks[].error`). A page docling returns empty (image markers only) or with under 60% of
the page's numbers also keeps its pdftotext text (`table_pages_rejected`). The page count
comes from `pdfinfo`, and the pypdf fallback runs in the same guarded child, never in the
server process. A page is a table page when it has at least 25 numeric tokens and at least
four lines with two or more purely numeric cells separated by runs of two or more spaces (two
lines when the page names emissions); at most `TRET_NORMALISER_MAX_TABLE_PAGES` pages are
converted, emissions pages first, and `capped` records a cut. Output pages are `## Page N`
with physical page numbers, the same as `extracted_text`, so existing page maps line up;
docling's own headings are pushed down to level 4 and below so the page marker stays the only
level-2 heading.

**ESEF filings** (provenance `esef_filing`) are read with the filing parser in a child
process, uncapped; consecutive table rows become a Markdown table and the unit markers are
`## Page N` or `## Section N: heading` as in the filing extract. Rendering a paged ESEF
filing to PDF is not done.

**Quarantine.** A report is `quarantined` (its Markdown is still stored) when it has no pages
or no text (`empty`), a median under 200 characters per page (`no_text_layer`: scanned, needs
OCR, which this stage does not do), or more than 1% U+FFFD characters (`garbled`). The
median rule is not applied to an ESEF filing read as sections. `metrics` holds page counts,
the empty-page count, median characters per page, the replacement-character rate, the table
pages selected, converted and fallen back, `tables_found`, `capped`, converter versions,
seconds in total and in docling, and the peak resident size of the children.

**Settings** (all in `.env.example`): `TRET_DISCLOSURE_NORMALISER`, `TRET_NORMALISER_DOCLING`
(`auto` uses docling when importable, otherwise tables are left as layout text and marked
unstructured; `off`), `TRET_NORMALISER_MAX_RSS_MB` (3200, also the limit for the pypdf and ESEF children),
`TRET_NORMALISER_CHUNK_PAGES` (3),
`TRET_NORMALISER_CHUNK_TIMEOUT_SECONDS` (300), `TRET_NORMALISER_MAX_TABLE_PAGES` (150).

**Installing docling.** It is the optional `normalise` extra (`pip install "tret[normalise]"`):
torch and models make it about 1.4 GB plus roughly 0.5 GB of models downloaded on first use.
It is not in the server extra and not in the shipped images. Plan a machine of at least 4 GB
for the conversion; a 2 GB machine is not safe. A separate 4 GB worker machine for a
hosted deployment is a later change to the hosting extension, not part of this one.

## Peer taxonomy (experimental, API off by default)

Evaluating one company's disclosure against its peers needs a peer pool, and the pools are built
on an open ladder rather than a licensed one: **NACE Rev. 2** (Eurostat) with Tret's own industry
clusters on top. GICS is licensed and is never used to build a pool. The code is `tret/taxonomy/`
(pure functions, no database), the data files ship in the package (`tret/taxonomy/data/`, with a
README naming each source), and the version is `TAXONOMY_VERSION` (`nace2-tret-1`, stored on every
classification).

**The ladder.** group (272, e.g. `21.2`) -> division (88, `21`) -> cluster (29 Tret clusters such as
`pharma_biotech`, `banks`, `utilities`) -> section (21, `C`). Each division maps to exactly one
cluster, with group-level overrides where a division is mixed (`64.1` is `banks`, the rest of 64
`diversified_financials`; `65` is `insurance`). `ladder("26.1")` accepts a division (`26`), a group
(`26.1` or `261`) or a class (`26.11`, cut back to its group) and raises `ValueError` for anything
that is not in NACE Rev. 2; `pool_key(level, ladder)` gives `division:21`, `cluster:banks` and so on.

**Sources of a classification, in order of trust** (one row per source in `company_classifications`,
workspace-scoped like the register; the effective one is the most trusted present):

1. `manual`: a workspace admin's override (`PUT /api/company-classifications/manual`), with the
   reviewer and time recorded.
2. `sec_sic`: the SEC's SIC code for the company, through a curated SIC (1987) to NACE Rev. 2 table
   (`tret/taxonomy/data/sic_nace.csv`). This is not an official concordance: it is division-accurate
   by design, fills a group only where the SIC heading falls inside one, and grades every row
   `exact`, `close` or `broad` (a `broad` row spans several NACE divisions, or is a catch-all such
   as 7389, and wants a human look before a peer pool leans on it).
3. `extractor`: proposed by a collection run. Not built yet (a later change).

**GICS cross-check.** The register's `sector` field (a GICS sector name for the US rows) is mapped
to the set of clusters it can reasonably contain. When the NACE cluster is outside that set the row
gets `gics_disagrees = true`. It is a flag for a reader: nothing is auto-resolved.

**Importing SIC codes.** An operator runs it per batch; it reads and writes the database directly
(no server) and fetches `https://data.sec.gov/submissions/CIK##########.json` for each target with a
CIK:

```bash
TRET_SEC_USER_AGENT="Your Organisation contact-address-or-url" \
  tret disclosure classify-sec --batch <batch id> [--limit N] [--force] [--dry-run]
tret disclosure classifications --batch <batch id> [--csv out.csv]   # effective classification per target
```

The SEC requires a descriptive User-Agent that says who is calling and how to reach them, so the
command refuses to run (exit 2) while `TRET_SEC_USER_AGENT` is empty. The requests go through
`tret/net` like every other outbound call, under the `research` egress class pinned to
`data.sec.gov`: they need `TRET_EGRESS=on` and `TRET_EGRESS_RESEARCH=on` (the command refuses with
exit 2 otherwise, or when `TRET_EGRESS_RESEARCH_ALLOW_HOSTS` is set and excludes the host),
`TRET_EGRESS_PROXY` applies, and each request is in the egress audit log. Requests are paced at no
more than 8 a second, with backoff on 429 and 5xx; a 403 (the SEC refusing the client) or a
connection that cannot be made stops the run at once. Rows are written in short transactions every
25 CIKs and at the end, so an interrupted run keeps what it finished and no transaction is held
open while the SEC is slow. A target is skipped when it has no CIK or already has a `sec_sic` row
(`--force` redoes it; two register spellings of one CIK, `320193` and `0000320193`, are the same
company). The summary counts classified, unmapped SIC (the SEC's code is not in the concordance),
unclassifiable SIC (a code with no industry: blank checks 6770, depositary receipts 8880, foreign
governments 8888, non-operating establishments 9995; nothing is written), no SIC on record (the
SEC returns an empty one for some filers), errors and skips. `classify-sec` and `classifications`
are operator tools and work whether or not `TRET_PEER_TAXONOMY` is on.

**Effective classification and keys.** A company is known by `cik`, `lei`, `ticker` or (for a
register target) its slug. `tret disclosure classifications` and
`GET /api/company-classifications/effective` use one resolver: across the rows under every key
given, manual beats sec_sic beats extractor. A manual override keyed by ticker therefore applies to
every target with that ticker in the workspace, and a classification keyed by CIK is not seen by a
lookup that gives only the ticker. An override keeps the company's GICS sector (from its other rows
or its register target), so the cross-check still runs after it.

**Clusters and the division rung.** Group-level cluster overrides (26.6 medical devices, 64.1
banks, 58.2 software) mean the division rung of the ladder can be broader than the cluster rung
(division 64 holds banks and diversified financials; division 26 holds hardware and medical
devices). Which rung a pool falls back to, and in what order, is decided in the pools change.

**API.** `TRET_PEER_TAXONOMY=on` turns on `GET /api/company-classifications` (filters `source`,
`cluster`, `division`, `disagreements_only`; paginated),
`GET /api/company-classifications/effective?cik=&lei=&ticker=&target=` (at least one; the company's
effective row with every source's row) and `PUT /api/company-classifications/manual` (workspace
admin; body `company_key_type` = `lei`, `cik`, `ticker` or `target`, `company_key`, `company_name`,
`nace_code` at group or division level, `note`; 422 for an unknown NACE code). All routes are a 404
while the setting is off.

## Acceptance dimensions, and where each one lives

| Dimension | What a reviewer asks | Where tret keeps it |
|---|---|---|
| Discovery | How was this source found, and is it the right one? | `web_search` is navigation only and carries no evidence. The run transcript and the egress log show what was searched and fetched; the `capture_source` `note` and the finding's `review_note` say why this source. In a collection, the target's reported outcome (status, summary, substitution note, blocked and candidate URLs) and the exception log record where discovery fell short or substituted |
| Capture | Is this the complete original, and can it be verified later? | The `source_kind='web'` Document: SHA-256, `url`, `redirects`, `http_status`, `retrieved_at`, headers, `body_truncated`, `sha256_mismatch`; the stored file; the egress row |
| Completeness | Do the retained pages cover the disclosure and what it incorporates? | The `package_map` finding (`keep_ranges`, `incorporations`, `exclusions`, `review_note`) and the approval; `page_map.json`; the link audit (`target_page_not_retained`, `unresolved_source_destination`) |
| Extraction quality | Is the text usable? | `extraction_stats.json` (empty pages, replacement characters, parser warnings by phase), `text_mode` and Poppler version, the `limitations` list |
| QA | Who checked it, and against what? | The `Approval` row (approver, time, note), repeated per document in a collection's `manifest.json`; the verification block in `package.json` (what was checked, what it establishes and does not); the source and retained SHA-256 on every package Document; `payload.package` including retries |
| Analytical eligibility | May a number from this be used in a verdict? | Not here. Captured and package documents are third-party text and are never registered for citation. A figure becomes usable only when an operator promotes it into a dataset and it comes back through `lookup_dataset` ([trust-doctrine.md](trust-doctrine.md)) |

## What is deliberately not automated

- **Choosing pages.** Code does not guess a disclosure's range, a statement's end or
  where an incorporated section sits. Every range is the model's proposal and a
  person's decision. Batch approval of a collection's maps changes how many are decided
  in one request, not who decides: each map is still approved or rejected by a person,
  and that approval is what authorises the upload.
- **Discovery.** Which source is the right one is the model's judgement, made without a
  person in the run. Nothing checks it except the approver reading the map and the
  exception log afterwards. A collection never decides that a company has no
  disclosure: `gap` and `blocked` are the model's reports, shown for a person to
  retry or accept.
- **Acceptance review and reference facts.** The period, publication date, framework and
  language in a target's outcome are what the model says it read. They are recorded as
  reported and are not checked against any reference. Whether the sample meets the
  briefing's shape is only warned about at creation. Finalize records completeness as
  "within reviewed scope; not independently certified complete".
- **Releasing the gate and finalizing.** A person does both. Nothing runs the rest of
  a batch or writes its records on a timer.
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
| `TRET_EGRESS_CAPTURE_MAX_BYTES` | largest file `capture_source` stores, default 62914560 (60 MiB); bigger is refused. A collection uploads package files under the same ceiling |
| `TRET_EGRESS_CAPTURE_MAX_PER_RUN` | capture attempts per run, default 40 |
| `TRET_EGRESS_CAPTURE_ANY_PUBLIC_HOST` | default `false`. When `true`, `capture_source` (only) may fetch from any public https host; `fetch_url` and `web_search` stay on the allowlist. Boot logs a note when it is on |
| `TRET_COLLECTIONS_MAX_CONCURRENT` | collection target runs executing at once across the process, default 3 (clamped 1 to 6) |
| `TRET_COLLECTIONS_RUN_TIMEOUT_SECONDS` | wall-clock limit for one target's run, default 1200; the run is cancelled and the target fails with `timeout`. No new target is launched while the server drains for shutdown, and a restart resumes a running collection on its own, or pauses it with `TRET_BATCHES_AUTO_RESUME=false` (section 6); also `TRET_BATCHES_AUTO_RESUME`, default true |

`propose_package_map` itself makes no network request; it works on documents already
in the project, so it is available whether or not research is enabled (a PDF someone
uploaded can be packaged the same way). A pack opts in by listing it in a task type's
tools and a harness's `tool_names`.

The Docker images install `poppler-utils`, so `pdftotext` is on the path and packages
record `text_mode: "pdftotext"`. A deployment without it still packages with the
`pypdf` fallback and says so. Packages are written under `TRET_STORAGE_DIR/packages/`.
