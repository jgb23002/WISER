# WISER County Maps + Storm Reasoning Test Cases

---

## Slide 1: County Most Affected Overall

**Test case / input data**

Metric: total outage trouble spots across all storms. Map view: `Total outages`
by legacy CT county.

| county | storms | total_outage | avg_peak_wind | outage_per_million_ohline |
|---|---:|---:|---:|---:|
| Fairfield | 294 | 43,475 | 7.41 | 8,894.54 |
| Hartford | 294 | 23,307 | 7.19 | 3,970.83 |
| Litchfield | 294 | 17,601 | 7.87 | 4,427.70 |

**Prompt**

```text
which county was affected the most overall?
```

**AI-generated response**

```text
- Strongest measured signal: Fairfield ranks highest by total_outage with 43,475.
- Next highest is Hartford at 23,307, a gap of 20,168.
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
| Hartford | 4,792 | 9.35 | 25.88 | 4 | 5,869,550 | 816.42 |
| New Haven | 2,958 | 9.95 | 31.08 | 4 | 3,233,197 | 914.88 |

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
  Statewide outages (trouble spots dispatched):  4,792
  Counties with the highest outages:
    Hartford County — 4,792

Relative Impact
  After normalizing by overhead line length, Hartford County experienced the
    greatest relative impact (816.42 per million ohLine).

Analytical reasoning:
- Strongest measured signal: Hartford / storm 2020080406 ranks highest by
  outage with 4,792.
- Wind is one driver, but the rows should be read alongside outage,
  precipitation, threshold-hour, and exposure fields because outages do not
  always rise one-for-one with peak wind.
- Caveat: this explanation uses only returned database fields; causal claims
  need additional evidence such as population, vegetation, asset condition,
  and restoration logs.
```

Dropping the storm identifier reaches `weather_analytics` and restores the
two-county comparison, but across **all** storms rather than this one —
Hartford 23,307 vs New Haven 16,965, not 4,792 vs 2,958.

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
| Fairfield | 43,475 | 4,887,830 | 8,894.54 |
| Middlesex | 13,599 | 2,302,299 | 5,906.70 |
| Windham | 13,179 | 2,400,780 | 5,489.47 |

**Prompt**

```text
normalize outages by population
```

**AI-generated response**

```text
- Strongest measured signal: Fairfield ranks highest by
  outage_per_million_ohline with 8,894.54.
- Next highest is Middlesex at 5,906.70, a gap of 2,987.84.
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
| Windham | 13.11 | 13,179 |
| Litchfield | 12.93 | 17,601 |
| Fairfield | 11.61 | 43,475 |

**Prompt**

```text
which county had the highest maximum wind and did it also have the highest outages?
```

**AI-generated response**

```text
- Strongest measured signal: Windham ranks highest by max_peak_wind with 13.11.
- Next highest is Litchfield at 12.93, a gap of 0.18.
- Wind and outage rankings diverge: Fairfield leads by total_outage with 43,475,
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

Every table and response on this page was re-run against `data/wiser.db` and
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
