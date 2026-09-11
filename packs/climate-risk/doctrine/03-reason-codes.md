# Divergence Reason Codes

Every divergence verdict carries exactly one reason code — the *most
defensible* explanation, not a list of possibilities. Each code has an
evidence test: if you cannot satisfy the test from what you retrieved this
run, you may not use the code.

## site_specific_factor vs. methodology_choice

These two codes are the ones most often confused, because a vendor's method
note frequently *names* a physical-sounding property (elevation, terrain,
exposure) on its way to explaining a technique. The property named is not
what decides the code — whose explanation it is does:

- **`methodology_choice`** when a retrieved method note names *how the
  vendor computed the score* — a technique such as terrain amplification, the
  resolution it modeled at, a projection method, or another modelling
  choice. The note is explaining the vendor's approach, even when that
  approach itself references a site property (elevation feeding a terrain
  model, say) — the divergence is still best explained by the *choice of
  method*, not by the site.
- **`site_specific_factor`** only when a physical property of the site
  *itself* explains the gap, with no vendor method or technique in the
  explanation — a retrieved value or document passage describing the local
  factor directly (engineered protection, micro-siting, a site-level
  measurement), independent of how either source computed anything from it.

**When a method note exists, prefer `methodology_choice`.** A method note
naming a technique is evidence about the vendor's process; treat it as such
even where the technique operates on a site property, rather than reaching
past it for `site_specific_factor` on the strength of that same property
being physical. Reserve `site_specific_factor` for when no method or
technique is in evidence — only the site property is.

*Worked example:* Rowan Ridge (S-011) × wind, where a vendor method note
reads "gust exposure at site elevation with terrain amplification." The
gap is `methodology_choice`, not `site_specific_factor`: "terrain
amplification" names the vendor's modelling technique, and "site elevation"
appears only as an input to that technique, not as a standalone physical
factor with its own retrieved evidence. `site_specific_factor` would apply
only if the evidence instead described, say, an engineered flood berm at the
site with no accompanying method note explaining how either score used it.

## site_specific_factor

The gap is best explained by a real, local property of the site that one
source captures and the other cannot see at its resolution (elevation,
engineered protection, micro-siting).

**Evidence test:** you can point to a retrieved value or document passage that
describes the local factor, with no vendor method note or named technique
in the explanation (see "site_specific_factor vs. methodology_choice" above —
a method note naming a technique makes this `methodology_choice` instead,
even when that technique operates on a site property). Suspicion of a local
factor without evidence is `scale_mismatch` or a data request, not this
code.

## outdated_inputs

The reference score predates a material trend the forward-looking signal now
shows; its vintage is the most defensible explanation for the gap.

**Evidence test:** the retrieved vintage is meaningfully older than the signal
horizon under assessment, AND the signal is robust per the procedure. A fresh
score cannot receive this code.

## methodology_choice

The sources measure genuinely different things by design (for example, one
scores present-day exposure while the other projects forward change, or one
applies a technique such as terrain amplification the other does not), and
that design difference plausibly produces the gap.

**Evidence test:** a retrieved method note or documented scope difference
identifies the differing design choice or technique — including a technique
that operates on a site property (see "site_specific_factor vs.
methodology_choice" above; when a method note is in evidence, this code is
preferred over `site_specific_factor` even then). Absent any method
information, prefer `coverage_gap` via a data request for the methodology
documentation.

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
