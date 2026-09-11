# The Trust Doctrine

tret exists for work where "the model said so" is not good enough: client
deliverables, credit decisions, disclosures that face auditors. These five
principles are product identity, and each one is enforced by architecture —
not by hoping the prompt holds.

## 1. Deterministic vs. interpretive separation

The AI reasons, compares, and drafts. It does not compute, estimate, or recall
numbers. Values enter a run through exactly two doors, both recorded:

- **`lookup_dataset`** — retrieval from structured datasets.
- **`run_method`** — invocation of a *pack-authored, operator-vetted* Python
  method. The agent supplies parameters; it never writes code. Every execution
  is manifest-pinned (params, code sha, input hashes, output hash) in the
  `method_runs` table, and its outputs carry row references
  (`method/<slug>/<run-id>:<row>`) that resolve back to that manifest.

When the model records a verdict, its `cited_values` are cross-checked against
everything actually retrieved or computed this run — a value from neither door
fails validation and is sent back for repair. (`tret/engine/validation.py`,
`tret/services/methods.py`)

Chat and freeform prose have no `cited_values` array — there is no schema for
the cross-check to hold them to — so it used to hold them to nothing at all. A
run could answer three turns running that "the retrieved record shows score
58" while every `lookup_dataset` call that run made came back empty, and the
transcript would look exactly like a run that had never invented anything.
The grounding check (`tret/engine/grounding.py`) closes that gap in the loop
itself, not after the fact: every number in a chat/freeform reply is checked
against everything this run actually retrieved and everything said in the
conversation, and a figure that traces to neither gets the reply sent back for
a rewrite — the same in-loop repair discipline as a rejected `cited_values`
entry, just without a schema to key it off. Two repairs, then the reply ships
as the model wrote it, flagged `unresolved` rather than silently passed off as
clean; the run's status is unaffected, but the flag is on the record and in
the chat UI, because ending a run over one stubborn number would refuse
delivery of everything else it got right.

**What is exempt, honestly.** The check is number-level pattern matching, not
a grader of reasoning, and it is deliberately lenient about the ways a number
can honestly restate evidence rather than invent it: a small bare count under
10 ("we checked 3 datasets") and a markdown list ordinal ("1. ", "2) ") are
never treated as claimed figures at all; a reply may round a retrieved value
("58" for a retrieved 58.37), state the equivalent percent or fraction of one
("71%" for a retrieved 0.71, or the reverse), or do arithmetic — sum,
difference, or mean — over two *other* numbers in the same reply that are
themselves grounded, within half a unit of its own last decimal place, so
"the combined score is 80" clears when the reply also states the two
retrieved figures that sum to it. None of these chain: an arithmetic result
cannot itself support a further arithmetic claim, so a run cannot bootstrap a
tower of "derived" figures from one retrieved seed. An ISO-format date
(`2026-09-11`) is not a claimed figure either — it is a timestamp, not three
cited numbers — though a real figure elsewhere in the same reply is
unaffected. And a run that never called a tool and never retrieved anything
is not checked at all: a pure-knowledge chat answer has nothing retrieved to
contradict, so `run.grounding` records `status: "skipped"` (`checked: False`)
rather than either silently passing or flagging ordinary prose that was never
meant to be grounded — distinct from `"clean"`, which means the check ran and
found nothing wrong.

**What this guarantees, precisely.** Method code is pinned two ways: each
execution records the entrypoint's `code_sha`, and the pack's whole content
hash is re-verified before the method runs, so a pack edited under a running
deployment fails loudly instead of quietly changing numbers. Execution is
confined to a short-lived `python -I` subprocess with an empty environment,
rlimits, a wall clock, output caps, no DB handle, and — on Linux where
`unshare` is usable — no network. Pack validation also AST-scans method code for
network, subprocess, FFI, and dynamic-code use.

**The web is not a third door.** tret can search and read the public web
(`web_search`, `fetch_url` — off by default, see hardening.md §9), and that
capability deliberately changes nothing above. A fetched page is not returned as
prose: it is stored byte-for-byte, hashed, and recorded as a `Document` with
`source_kind='web'`, its URL and its fetch time, which the model then reads with
the same `read_document` it uses for an uploaded PDF — so every read is in the
audit trail, and a reviewer can see the page *as it was when the run read it*
rather than as it is today.

What a web page may never do is put a number into a verdict. Nothing `fetch_url`
retrieves is registered in `ctx.retrieved_values`, so the cited-values
cross-check refuses a figure that came from one — without having been taught
anything about the web. The check did not change, and that is the point. Web
material can be described and attributed; it cannot be cited as a value.
Promoting a snapshot into a dataset is an operator's reviewed act, after which
the number enters through door one like any other. The engine labels the tier on
every read, because by iteration nine the tool result that said where the text
came from is far up the transcript.

**What it does not guarantee.** That scan is a deterrent, not a sandbox, and the
subprocess has no filesystem isolation: pack methods are operator-trusted code,
and installing a pack is deploying code you reviewed. The trust claim here is
*provenance and reproducibility* — every number is attributable to vetted code
and hashed inputs — not *containment of hostile pack authors*. Containment is
the deployment's job; see `docs/hardening.md`.

## 2. The blessing gate

Every structured output is created with status `draft`. It becomes `approved`
only through an approvals endpoint that stamps the approver from the
authenticated session; the API schema has no approver field to spoof. The
platform preamble also forbids the model from claiming its own output is
approved. (`tret/api/findings.py`)

## 3. Provenance everywhere

Every run persists: the full **routing decision** (router model, candidates,
verbatim reasoning, prompt version, fallback state), the executing model, the
**doctrine content hash** current at run time, the complete message
transcript including tool calls, token counts, and cost. Every finding carries
the retrieved values and documents behind it. (`runs.routing`,
`findings.provenance`)

## 4. Doctrine-as-context

A pack's reasoning rules are versioned markdown files, hashed together into a
`doctrine_sha`. They are loaded — tagged with per-file hashes — at the top of
the system prompt, and the model must cite headings it relies on. Change the
doctrine and the sha changes; old runs remain interpretable against the
doctrine they actually ran under. (`tret/engine/context.py`)

## 5. Honest uncertainty

Verdict schemas include `insufficient_data`; confidence tiers are earned by
robustness rules in the doctrine, not fluency. When required data is missing
the model files a **data request** — a first-class record an operator can
fulfill — and completes the assessment with what exists, stating the gap's
effect on confidence.
