# Divergence Reason Codes

Every divergence verdict carries exactly one reason code — the *most
defensible* explanation, not a list of possibilities. Each code has an
evidence test: if you cannot satisfy the test from what you retrieved this
run, you may not use the code.

## site_specific_factor

The gap is best explained by a real, local property of the site that one
source captures and the other cannot see at its resolution (elevation,
engineered protection, micro-siting).

**Evidence test:** you can point to a retrieved value or document passage that
describes the local factor. Suspicion of a local factor without evidence is
`scale_mismatch` or a data request, not this code.

## outdated_inputs

The reference score predates a material trend the forward-looking signal now
shows; its vintage is the most defensible explanation for the gap.

**Evidence test:** the retrieved vintage is meaningfully older than the signal
horizon under assessment, AND the signal is robust per the procedure. A fresh
score cannot receive this code.

## methodology_choice

The sources measure genuinely different things by design (for example, one
scores present-day exposure while the other projects forward change), and that
design difference plausibly produces the gap.

**Evidence test:** a retrieved method note or documented scope difference
identifies the differing design choice. Absent any method information, prefer
`coverage_gap` via a data request for the methodology documentation.

## scale_mismatch

The gap is plausibly an artifact of comparing different spatial or temporal
resolutions (regional signal vs point-scored site), with no evidence of a real
local factor.

**Evidence test:** the sources' resolutions genuinely differ (regional vs
site-level) and neither a local factor nor a vintage problem is evidenced.
This is the honest "resolution artifact" code — use it rather than forcing a
stronger story.

## coverage_gap

A required input exists in principle but is missing, partial, or not assessed
for this site × peril, and the missing piece is what prevents a cleaner
explanation.

**Evidence test:** you filed a data request this run naming the missing
coverage. This code accompanies divergence verdicts made on partial evidence;
where the gap prevents any verdict, use `insufficient_data` instead.
