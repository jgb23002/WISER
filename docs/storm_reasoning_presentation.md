# WISER County Maps + Storm Reasoning Test Cases

> Figures regenerated against the deployed database (Neon, PostgreSQL 17).
> The dataset changed after this was first written and every number in it had
> gone stale — Fairfield read 43,475 against an actual 44,402, and the ohLine
> exposure values had moved too, so the normalized columns were wrong as well.
> Regenerate these tables from the database rather than editing them by hand.

---

## Slide 1: County Most Affected Overall

**Test case / input data**

Metric: total outage trouble spots across all storms. Map view: `Total outages`
by legacy CT county.

| county | storms | total_outage | avg_peak_wind | outage_per_million_ohline |
|---|---:|---:|---:|---:|
| Fairfield | 294 | 44,402 | 7.41 | 8,891.23 |
| Hartford | 294 | 22,666 | 7.19 | 3,972.96 |
| Litchfield | 294 | 17,740 | 7.87 | 4,422.05 |

**Prompt**

```text
which county was affected the most overall?
```

**AI-generated response**

```text
- Strongest measured signal: Fairfield ranks highest by total_outage with 44,402.
- Next highest is Hartford at 22,666, a gap of 21,736.
- Caveat: this explanation uses only returned database fields; causal claims need
  additional evidence such as population, vegetation, asset condition, and
  restoration logs.
```

**Evaluation**

Good: identifies the highest-impact county and gives the size of the gap.
Needs refinement: add a second sentence comparing normalized impact when the
question says "affected" but does not specify raw vs normalized impact.

---

## Slide 2: Higher Outage Despite Lower Wind

**Test case / input data**

Storm: `2020080406`.

| county | outage | peakWSPD10 | precip | wgt9 | ohLine | outage_per_million_ohline |
|---|---:|---:|---:|---:|---:|---:|
| Hartford | 4,658 | 9.35 | 25.88 | 4 | 5,705,069 | 816.47 |
| New Haven | 2,833 | 9.95 | 31.08 | 4 | 3,096,793 | 914.82 |

**Prompt**

```text
for storm 2020080406, why did Hartford have more outages than New Haven
despite lower wind?
```

> **Behaviour changed.** This prompt names a storm, so it now routes to
> `storm_summary` rather than `weather_analytics`, and the storm-scoped path
> narrows to the one county it finds in the query text. The two-county
> comparison below is no longer what this prompt produces. Recorded here as it
> stands, not as it was.

**Response today — `Route: storm_summary`, scoped to Hartford alone**

```text
Storm Impact
  Statewide outages (trouble spots dispatched):  4,658
  Counties with the highest outages:
    Hartford County — 4,658

Relative Impact
  After normalizing by overhead line length, Hartford County experienced the
    greatest relative impact (816.47 per million ohLine).

Analytical reasoning:
- Strongest measured signal: Hartford / storm 2020080406 ranks highest by
  outage with 4,658.
- Wind is one driver, but the rows should be read alongside outage,
  precipitation, threshold-hour, and exposure fields because outages do not
  always rise one-for-one with peak wind.
- Caveat: this explanation uses only returned database fields; causal claims
  need additional evidence such as population, vegetation, asset condition,
  and restoration logs.
```

Dropping the storm identifier reaches `weather_analytics` and restores the
two-county comparison, but across **all** storms rather than this one —
Hartford 22,666 vs New Haven 16,260, not 4,658 vs 2,833.

**Evaluation**

The earlier response — which named both counties, called out the non-linear
wind/outage relationship, and showed that ohLine normalization reverses the raw
ranking — is not currently reachable by any phrasing. With the identifier the
comparison county is dropped; without it the storm scope is. A storm-scoped
question naming two counties should keep both in scope; that is an open gap,
not a design decision.

---

## Slide 3: Population Normalization Request

**Test case / input data**

Population is not in the current WISER schema. Available exposure proxy:
`County.ohLine`.

| county | total_outage | ohLine | outage_per_million_ohline |
|---|---:|---:|---:|
| Fairfield | 44,402 | 4,993,911 | 8,891.23 |
| Middlesex | 13,134 | 2,217,370 | 5,923.23 |
| Windham | 12,700 | 2,310,126 | 5,497.54 |

**Prompt**

```text
normalize outages by population
```

**AI-generated response**

```text
- Strongest measured signal: Fairfield ranks highest by
  outage_per_million_ohline with 8,891.23.
- Next highest is Middlesex at 5,923.23, a gap of 2,968.00.
- Population is not present in the current WISER schema, so a per-capita answer
  would need an added population table or input values.
- Caveat: this explanation uses only returned database fields.
```

**Evaluation**

Good: refuses to invent population values and substitutes the available
infrastructure exposure normalization. Next refinement: add Census county
population data as a dedicated table if the professor wants true per-capita
metrics.

---

## Slide 4: Wind Severity vs Outage Severity

**Test case / input data**

Metric: maximum peak wind by county, with total outage included for comparison.

| county | max_peak_wind | total_outage |
|---|---:|---:|
| Windham | 13.11 | 12,700 |
| Litchfield | 12.93 | 17,740 |
| Fairfield | 11.61 | 44,402 |

**Prompt**

```text
which county had the highest maximum wind and did it also have the highest outages?
```

**AI-generated response**

```text
- Strongest measured signal: Windham ranks highest by max_peak_wind with 13.11.
- Next highest is Litchfield at 12.93, a gap of 0.18.
- Wind and outage rankings diverge: Fairfield leads by total_outage with 44,402,
  not Windham.
- Caveat: this explanation uses only returned database fields; causal claims
  need additional evidence.
```

**Evaluation**

Good: demonstrates reasoning across metrics instead of equating wind severity
with outage severity. Next refinement: ask the AI to explain likely drivers of
the divergence only when supporting fields are available.

---

## Reproducibility Notes

County heat maps use `data/geography/ct_counties_legacy.geojson`, the State of
Connecticut legacy 8-county layer. This matches the dataset's older county
names instead of newer planning-region geography.

Every table and response on this page was re-run against the WISER database and
matches, except where slide 2 records a routing change. Slides 1, 3, and 4
reproduce byte-for-byte through the local analytical fallback.

Example command pattern:

```bash
OPENAI_API_KEY= ./.venv/bin/python scripts/wiser_cli.py \
  "which county was affected the most overall?" \
  --no-openai-summary \
  --show-reasoning-prompt \
  --limit 8
```
