# Divergence Assessment Procedure

Follow these seven steps in order for every site × peril assessment.

## Step 1 — Resolve the site

Identify the site and its region. If the site or its region cannot be
resolved, stop and record `insufficient_data` with a data request.

## Step 2 — Read the forward-looking signal

Retrieve the regional signal for the region and peril across **all available
scenarios**. Note, for each scenario: the direction (increase / decrease /
stable), the magnitude class, and the spread between the median and tail
estimates.

## Step 3 — Check signal robustness

The signal is **robust** when all scenarios agree on direction and are within
one magnitude class of each other. If scenarios disagree on direction, or the
median and tail land in very different magnitude classes, the signal is
**fragile**: your confidence may not exceed `low`, and the fragility must be
stated in the note.

## Step 4 — Read the reference score

Retrieve the vendor reference score for the site and peril, including its
vintage (the year the score was produced) and any method note. A missing
reference score for an assessed peril is a coverage gap: record
`insufficient_data` and file a data request naming the missing coverage.

## Step 5 — Compare on the overlap only

Translate both sources to the honest grain: direction plus magnitude class.
The comparison asks one question: *does the reference score's risk class
reflect what the forward-looking signal indicates for this region?* A
reference score produced years before a strongly trending signal deserves
particular scrutiny of its vintage.

## Step 6 — Issue the verdict

- `agree` — direction and magnitude class are consistent at the honest grain.
- `diverge_signal_higher` — the forward-looking signal indicates materially
  more risk than the reference score reflects.
- `diverge_reference_higher` — the reference score indicates materially more
  risk than the forward-looking signal supports.
- `insufficient_data` — a required input is missing or the signal is too
  fragile to support any comparison.

Any divergence verdict **must** carry a reason code whose evidence test (see
Reason Codes) is satisfied by what you actually retrieved.

## Step 7 — Write the note

Three to six sentences for a credit officer, per the Assessment Principles:
what was compared, what was found, the most defensible reason for any gap, and
what the reader should take from it. State fragility and gaps plainly.
