# Water methodology

How tret turns the energy a run used into a water figure; where every
constant came from; and what the resulting numbers are *not* good for.

Implementation: `backend/tret/services/water.py` (the arithmetic), resolved
through the same factor ladder as carbon in
`backend/tret/services/emission_factors.py` and attached to every run by
`energy_accounting()` in `backend/tret/services/emissions.py`. Constants:
`backend/tret/data/water_factors.json`. The energy side is
[emissions-methodology.md](emissions-methodology.md); this page assumes it and
does not repeat it.

> **Status: computed on new runs; not yet shown in the interface.** Every run
> recorded from this version on carries a `water` block in its
> `energy_accounting`, and the analytics, what-if and export endpoints return
> water fields. Runs recorded earlier carry no water figure.

**One-line summary: water figures are estimates of freshwater consumed, for
comparing model choices. They are derived from estimated or measured energy
and two configurable factors, and they are not a verified water inventory.**

## Read this first

A run has no water meter. Every litre tret reports is energy multiplied by a
factor, so a water figure can never be more accurate than the energy figure
under it, and it is usually much less accurate, because the factors vary more
than energy does.

Three choices define what the number means, and all three are fixed here:

- **Consumption, not withdrawal.** *Withdrawal* is water taken from a river or
  aquifer, much of which is returned. *Consumption* is the part that does not
  come back, mostly evaporation. Tret counts consumption. Every factor record
  carries `water_basis: "consumption"`, and a source that reports withdrawal is
  not used as a consumption factor.
- **Two parts, always shown separately.** *On-site* water is what the data
  centre evaporates to cool its servers. *Off-site* water is what power plants
  evaporate to generate the electricity the data centre buys. Off-site water is
  usually most of the total, so a figure that shows only cooling water (as
  several published disclosures do) understates the total several times over.
- **Litres, not scarcity.** A litre evaporated in Arizona and a litre in
  Ireland count the same. Scarcity weighting needs to know where a call was
  served, which a hosted API does not reveal. See [Not yet included](#not-yet-included).

## The chain, end to end

```
energy_wh        = node-IT energy, as in the emissions chain
energy_wh_total  = energy_wh * PUE
onsite_ml        = energy_wh       * site_wue_l_per_kwh      # cooling
offsite_ml       = energy_wh_total * grid_water_l_per_kwh    # electricity generation
embodied_ml      = embodied_water_ml_per_run                 # 0 / unset for now
water_ml         = onsite_ml + offsite_ml + embodied_ml
water_ml_low     = water_ml * band_low                       # judgment band
water_ml_high    = water_ml * band_high
avoided_water_ml = baseline_water_ml - water_ml              # signed, never clamped
```

The units work out directly: Wh × L/kWh = mL.

Why the two parts use different energy figures:

- **On-site water uses IT energy.** WUE (water usage effectiveness) is defined
  per kWh of *IT equipment* energy by The Green Grid and ISO/IEC 30134-9, and
  that is how Google applies it in its inference study (water per prompt =
  (total energy − overhead energy) × WUE). Applying WUE to facility energy
  would count the PUE overhead twice.
- **Off-site water uses facility energy.** The power plant generates every kWh
  the data centre draws, including the cooling and power-conversion overhead.

This is the same structure as Jegham et al. 2025, whose energy figures already
calibrate tret's class ladder. They divide facility energy by PUE to get IT
energy. Tret stores both energy figures, so no division is needed.

### Measured energy

A run with a meter records an `energy_boundary`. Water follows it:

| boundary | on-site water applied to | note |
|---|---|---|
| `node_it` | `energy_wh` | the case WUE is defined for |
| `facility` | `energy_wh_total / PUE` | the meter already includes overhead; IT energy is derived from the configured PUE and recorded as derived |
| `gpu`, `partial` | `energy_wh` | understates, as it does for carbon; the run's existing coverage caveat applies |
| `unknown` | `energy_wh` | historical rows; no claim about coverage |

Off-site water always uses `energy_wh_total`, which the emissions chain
already defines for every boundary.

### Local runs

A workstation or on-prem box is not evaporatively cooled, so the local
deployment's site WUE defaults to **0**. This mirrors the local PUE of 1.0. The
electricity it uses still comes from a grid, so off-site water still applies.
An operator who runs local models in an evaporatively cooled server room sets
their own site WUE.

## The factors

New factor keys resolve through the same ladder as grid intensity and PUE:
run override → workspace → managed → env → dataset → shipped default. Each
resolves to the same provenance record (`layer`, `source`, `url`, `as_of`,
`confidence`, `setting`) with one added field, `water_basis`.

### Site WUE (cooling water), shipped default: 0.375 L/kWh

Source: Shehabi et al., *2024 United States Data Center Energy Usage Report*,
Lawrence Berkeley National Laboratory, December 2024
([PDF](https://escholarship.org/content/qt32d6m0d1/qt32d6m0d1.pdf)). The
report gives US data centres' direct (on-site) water consumption in 2023 as
**66 billion litres** (p. 46) and their electricity use as **176 TWh** (p. 55).

66 billion L ÷ 176 billion kWh = **0.375 L/kWh**.

Tret ships the ratio of the two primary figures rather than the report's
rounded average ("just over 0.36 L/kWh", p. 48), because the derivation is
reproducible from the report itself. Note what the denominator is: 176 TWh is
*total facility* electricity, while LBNL defines WUE per IT kWh (p. 46).
Applied per IT kWh, as tret does, the default therefore leans **low** by up to
the fleet's PUE ratio. The band covers this.

Confidence: `structural`. It is a national fleet average, not a provider
figure, and it covers US facilities only.

For context, provider-published figures, **none of which is used as a
default**:

| operator | figure | year | basis | denominator | source |
|---|---:|---|---|---|---|
| Google | 1.15 L/kWh | 2023–24 | consumption ("ISO WUE Category 2") | IT energy | [arXiv:2508.15734](https://arxiv.org/abs/2508.15734) §3.3 |
| Microsoft | 0.27 L/kWh | FY25 | not stated | "energy use" (not qualified) | [2026 Environmental Sustainability Report](https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/microsoft/msc/documents/presentations/CSR/2026-Microsoft-Environmental-Sustainability-Report-PDF.pdf), p. 24 |
| Microsoft | 0.30 L/kWh | FY24 | withdrawal | not stated | 2025 Environmental Sustainability Report, p. 35 |
| AWS | 0.12 L/kWh | 2025 | withdrawal | IT load | [2025 Amazon Sustainability Report](https://sustainability.aboutamazon.com/2025-amazon-sustainability-report.pdf), p. 15 |
| Meta | 0.19 L/kWh | 2024 | withdrawal | IT load | [2025 Environmental Data Index](https://sustainability.atmeta.com/wp-content/uploads/2025/10/Meta_2025-Environmental-Data-Index.pdf), pp. 10, 18 |

The AWS, Meta and FY24 Microsoft figures measure withdrawal. They are not
comparable to Google's consumption figure and must not be entered as
consumption factors. Lower withdrawal WUE does not mean lower consumption.

**Why there are no per-provider defaults.** Tret's managed grid layer ships no
provider entries, because no provider discloses where an API call is served.
Water follows the same rule. Mapping a model vendor to a cloud (for example,
"Anthropic runs on AWS") is an inference, and the AWS figure is withdrawal in
any case. The one defensible exception is a vendor serving its own models from
its own disclosed fleet, such as Google's 1.15 L/kWh for Gemini. That is a
candidate for the managed layer, labelled with its basis and year, not a
core default.

### Grid water factor (electricity generation), shipped default: 4.81 L/kWh

Source: Reig, Luo, Christensen and Sinistore, *Guidance for Calculating Water
Use Embedded in Purchased Electricity*, World Resources Institute, 2020
([report](https://www.wri.org/research/guidance-calculating-water-use-embedded-purchased-electricity),
[PDF](https://files.wri.org/d8/s3fs-public/guidance-calculating-water-use-embedded-purchased-electricity_0.pdf)).
Licence: CC BY 4.0, stated in the PDF.

The default is WRI's generation-weighted average across its 47 countries, **1.27
gal/kWh = 4.81 L/kWh** consumption (Table 4; WRI's own 3.785 L/gal). This
parallels the carbon default, which is also a world average.

Selected country factors from the same report (Appendix 2), for operators who
set a regional value:

| country | gal/kWh | L/kWh |
|---|---:|---:|
| Ireland | 0.39 | 1.48 |
| Germany | 0.51 | 1.93 |
| Japan | 0.61 | 2.31 |
| United States | 0.83 | 3.14 |
| India | 0.91 | 3.44 |
| China | 1.59 | 6.02 |

The range across all 47 countries is 0.30 gal/kWh (Malta) to 4.91 gal/kWh
(Brazil), about 1.1 to 18.6 L/kWh. That spread, more than tenfold, is the main
reason the water band is wider than the carbon band.

What WRI's factors include and leave out:

- **Hydropower evaporation is included**, with 100% of a reservoir's
  evaporation allocated to electricity and none to irrigation, flood control
  or recreation. WRI says this allocation is conservative. It is folded into
  each country's average and not reported separately, so it cannot be
  subtracted out. This is why hydro-heavy grids such as Brazil's have the
  highest factors. Tret records it on every run as
  `hydro_evaporation: "included_full_allocation"`.
- **Generation only.** Transmission and distribution losses and fuel-chain
  water (mining, refining) are excluded.
- **Not year-specific.** The factors come from thinkstep's GaBi database
  (2018) and do not track later changes in each grid's fuel mix.
- **Wind and solar PV count as zero.**

Licensing note: the country factors are republished under the report's
CC BY 4.0 licence with attribution. The PDF also carries a notice that the
underlying GaBi datasets may not be redistributed, so tret ships only WRI's
published country factors, never raw GaBi data.

Confidence: `structural`.

**Figures not used, and why.**

- **US 3.14 L/kWh** (Li et al., [arXiv:2304.03271](https://arxiv.org/abs/2304.03271)).
  This is WRI's US factor, cited correctly. It is a US value; the default is
  the world value.
- **4.35 and 5.11 L/kWh** (Jegham et al. 2025, Table 1, for Azure and AWS).
  Both are cited to a "2024" WRI guidance. WRI has only the 2020 edition,
  which contains neither number. 4.35 L/kWh matches LBNL's US grid average
  (2024 report, p. 57), so the Azure figure appears to come from LBNL. No
  source for 5.11 was found. Neither is used.
- **LBNL 4.35 / 4.52 L/kWh** (US grid average / data-centre-weighted, 2024
  report p. 57). These are plausible US alternatives, but the derivation is
  not documented in enough detail to reproduce, and the default should be a
  world value.

### Embodied water: not counted

Manufacturing chips and servers uses large amounts of ultrapure water. For
cloud runs it is treated like embodied carbon: part of the purchased service,
so 0. For self-hosted hardware the key exists
(`embodied_water_ml_per_run`) but ships unset, because no per-device figure
has been sourced. An unset value is reported as *not counted*, not as zero.

### Uncertainty: a band, not an interval

The default band is **central ÷ 3 to central × 3**, set by
`TRET_WATER_BAND_LOW` / `TRET_WATER_BAND_HIGH` (both multipliers). This is wider than the carbon
band (÷ 2.5 to × 2.5) for stated reasons:

- The grid factor varies more than tenfold between countries (1.1–18.6 L/kWh),
  and a hosted API does not reveal which country served the call.
- Site WUE varies from about 0 (air- or dry-cooled sites) to above 1 L/kWh
  (evaporatively cooled sites), with climate and season.
- The default site WUE leans low (see above), and the grid default leans high
  (hydro allocated in full). These partly offset each other, but not in any
  quantifiable way.
- The water figure carries all the uncertainty of the energy figure under it.

As with carbon, this is a judgment band. It is flagged
`is_confidence_interval: false`, and it must not be reported as a confidence
interval.

## Sanity check against published figures

| disclosure | energy | water | boundary | what tret should show |
|---|---:|---:|---|---|
| Google, median Gemini Apps text prompt, May 2025 ([blog](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference), [arXiv:2508.15734](https://arxiv.org/abs/2508.15734)) | 0.24 Wh | 0.26 mL | on-site cooling only, consumption; 1.15 L/kWh × IT energy | tret's *on-site* part should be the same order, lower because the default WUE is about a third of Google's |
| OpenAI, average ChatGPT query, June 2025 ([post](https://blog.samaltman.com/the-gentle-singularity)) | 0.34 Wh | 0.000085 gal ≈ 0.32 mL | not stated | ≈ 0.95 mL/Wh, which looks like on-site only; not usable as an anchor for the total |
| Mistral Large 2, 400-token Le Chat response, July 2025 ([post](https://mistral.ai/news/our-contribution-to-a-global-environmental-standard-for-ai)) | not given | 45 mL | full lifecycle, consumption: training, inference, hardware manufacturing, end-user devices | an upper reference; tret excludes training and hardware, so its total should be well below this |

Worked check with the shipped defaults at Google's energy (0.24 Wh facility,
PUE 1.09 → 0.22 Wh IT): on-site 0.22 × 0.375 = 0.08 mL; off-site
0.24 × 4.81 = 1.15 mL; total ≈ 1.2 mL. On-site is lower than Google's 0.26 mL
because of the WUE difference. Off-site is about 14 times on-site, which is why
a cooling-only figure is a large understatement.

Anthropic has published no water figure.

## Rules for combining figures

- **Water sums only within one `water_basis`.** A rollup across runs that mix
  bases returns `null` water, with a caveat, in the same way carbon returns
  `null` across incompatible GHG bases. Energy and dollars still sum.
- **Old runs carry `null`, not 0.** A run recorded before water accounting has
  no water figure. Zero would claim the run used no water. A window that
  contains such runs reports how many were not counted.
- **Stored runs are never recomputed.** History can be restated through
  `POST /api/analytics/emissions/whatif`, which works for water because
  `energy_wh` is stored on every run since eco accounting began.
- **The baseline uses the same factor path.** The counterfactual model's water
  resolves through `factor_set_for_model`, exactly as its carbon does, so
  `avoided_water_ml` compares like with like. It is signed and never clamped at
  zero.

## What each run records

A `water` block inside `energy_accounting`, alongside the carbon fields:

```json
"water": {
  "schema_version": 1,
  "water_basis": "consumption",
  "water_ml": 60.51,
  "onsite_ml": 3.75,
  "offsite_ml": 56.76,
  "embodied_ml": null,
  "water_ml_low": 20.17,
  "water_ml_high": 181.5,
  "baseline_water_ml": 150.0,
  "avoided_water_ml": 89.49,
  "factors": [ /* provenance records: site_wue, grid_water, band */ ],
  "caveats": [ /* e.g. hydro included, IT energy derived from PUE */ ]
}
```

The values above are a cloud run of 10 Wh IT energy at PUE 1.18 with the shipped defaults; the baseline is illustrative. The block is computed per model segment and rolled up using the same
multi-segment rule as the carbon band; router and compaction calls get their
own blocks under `runs.overhead`, as their carbon does.

## Configuration

Water keys sit in the same workspace emissions document as grid, PUE and the
band, under `water`, so the existing `emissions_factors_edit` gate and settings
history cover them:

```json
{
  "water": {
    "site_wue_l_per_kwh": 1.15,
    "local_site_wue_l_per_kwh": 0.0,
    "grid_water_l_per_kwh": 3.14,
    "country": "USA",
    "band_low": 0.3333,
    "band_high": 3.0
  }
}
```

Every key is optional. `site_wue_l_per_kwh` applies to cloud runs only and
`local_site_wue_l_per_kwh` to local runs only; local stays 0 unless its own key
is set. `band_low` and `band_high` are both multipliers on the central figure
(`band_low` 0.3333 = central ÷ 3), unlike the carbon band, whose low value is a
divisor. `country` (ISO 3166 alpha-3) selects a WRI country factor; without
it, a grid factor pinned to an Ember country (`dataset:ember:country-XXX`)
selects that country's water factor, and anything else falls back to the
world average with a caveat. Only the six countries listed above are in the
shipped table.

Environment rung (instance-wide): `TRET_WATER_SITE_WUE_L_PER_KWH` (cloud),
`TRET_WATER_LOCAL_SITE_WUE_L_PER_KWH` (local), `TRET_WATER_GRID_L_PER_KWH`,
`TRET_WATER_BAND_LOW`, `TRET_WATER_BAND_HIGH`. Blank means unset. An invalid
value at any layer is skipped, so the next rung applies, and the run carries a
caveat naming the setting.

Precedence and provenance follow the carbon ladder exactly: run override →
workspace → managed → env → dataset (`dataset:wri2020:<ISO3>`) → shipped
default. Each factor record names the layer that won.

## Where the numbers live in the API

- `runs.energy_accounting.water` — the per-run block shown above, rolled up
  across model segments (each segment's own block is in `model_timeline`).
  Router and compaction calls carry their water in `runs.overhead`, exactly as
  their carbon is kept there, so it is never counted twice.
- `GET /api/analytics/emissions` — `water_ml`, `water_onsite_ml`,
  `water_offsite_ml` and `runs_without_water` on the totals and each
  breakdown. Null when a bucket mixes water bases or no run in it has water.
- `POST /api/analytics/emissions/whatif` — accepts the same `water` keys in a
  scenario and restates water for stored runs, including runs recorded before
  water accounting, because their tokens and energy are stored.
  `delta.water_ml` is null unless both sides have water for every run.
- Deliverable export — `water_ml`, `water_onsite_ml`, `water_offsite_ml` per
  section.
- `GET /api/docs/water-methodology` — this page, in the product.

Water is not part of the anonymous telemetry report.

## Not yet included

- **Scarcity weighting.** WRI Aqueduct 4.0 baseline water stress
  ([FAQ](https://www.wri.org/aqueduct/faq), CC BY 4.0) could weight litres by
  local scarcity. It would apply only when the serving region is known, which
  means local runs and operator-pinned provider@region, and it would be a
  second figure next to litres, never a replacement.
- **Interface.** The run detail, dashboard, settings and what-if screens do
  not show water yet.
- **Measured water.** An operator with a facility water meter can set their own
  site WUE today. Per-run metered water is not planned.
- **Embodied water** for self-hosted hardware, until a per-device figure can be
  cited.
- **Training.** Not allocated, as for carbon.
- **A water routing objective.** While factors do not differ by provider,
  water is proportional to energy, so the existing `eco` objective already
  chooses the lowest-water model.

## Limitations: read before quoting any number

- **This is not a water footprint under ISO 14046** and not a water-security
  disclosure. It is a comparison estimate.
- **The serving location is unknown** for hosted APIs, and location drives both
  factors. Both defaults are averages over places the call may never have
  touched.
- **Provider figures disagree on definitions.** Withdrawal and consumption are
  mixed across the industry, and denominators (IT vs facility energy) are not
  always stated. Read the basis before entering any provider number.
- **Hydro is charged in full** to electricity in the grid default, which is
  WRI's stated conservative choice and contested in the literature.
- **No independent verification exists** for any provider's per-prompt water
  figure. Google states its figures were not third-party verified.

## Sources

- Shehabi et al., *2024 United States Data Center Energy Usage Report*, LBNL,
  Dec 2024. <https://escholarship.org/content/qt32d6m0d1/qt32d6m0d1.pdf>
- Reig et al., *Guidance for Calculating Water Use Embedded in Purchased
  Electricity*, WRI, 2020, CC BY 4.0.
  <https://www.wri.org/research/guidance-calculating-water-use-embedded-purchased-electricity>
- Jegham et al., *How Hungry is AI? Benchmarking Energy, Water, and Carbon
  Footprint of LLM Inference*, 2025. <https://arxiv.org/abs/2505.09598>
- Li, Yang, Islam and Ren, *Making AI Less "Thirsty"*, 2023.
  <https://arxiv.org/abs/2304.03271>
- Elsworth et al. (Google), *Measuring the environmental impact of delivering
  AI at Google scale*, Aug 2025. <https://arxiv.org/abs/2508.15734>
- Macknick et al., *Operational water consumption and withdrawal factors for
  electricity generating technologies*, Environ. Res. Lett. 7 045802, 2012
  (per-technology reference; not used for defaults).
- Microsoft, AWS and Meta sustainability reports as linked in the table above.
- WRI Aqueduct 4.0, CC BY 4.0. <https://www.wri.org/aqueduct/faq>
