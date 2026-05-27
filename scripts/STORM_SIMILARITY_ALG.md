# Storm Similarity Algorithm (Non-ML Statistical Approach)

## Overview

This algorithm finds the **most similar historical storms** to a current storm event using statistical distance on wind speed data.

The system does **not** use machine learning.

Instead, it uses:
- mean
- standard deviation
- normalized distance
- similarity scoring

to identify storms with similar wind speed profiles.

---

# Goal

Given a current storm:

```text
(stormCode, county, peakWSPD10)
```

Find:

```text
Top 5 most similar historical storms in the same county
```

based on:

```text
peakWSPD10 (10-meter peak wind speed)
```

---

# Algorithm Steps

---

# Step 1 — Fetch Historical Storms

Fetch historical storms from the same county.

---

# Step 2 — Compute Population Statistics

Given historical wind speeds:

```math
W = {w₁, w₂, ..., wₙ}
```

where:

```text
wᵢ = historical storm wind speed
n = number of historical storms
```

---

## Mean (Average Wind Speed)

Formula:

```math
μ = (1/n) × Σwᵢ
```

Meaning:
- average historical wind speed

---

## Standard Deviation

Formula:

```math
σ = √[(1/(n−1)) × Σ(wᵢ − μ)²]
```

Meaning:
- measures spread of historical wind speeds

Interpretation:
- low σ → storms are similar
- high σ → storms vary widely

---

## Range

Formula:

```math
R = max(wᵢ) − min(wᵢ)
```


---

# Step 3 — Normalize Current Storm

Current storm:

```text
w_current
```

Compute Z-score:

```math
z = (w_current − μ) / σ
```


Interpretation:

```text
Current storm is x standard deviations above average.
```

Where `x` is the computed Z-score.

---

# Why Z-Score Matters

Raw wind speed differences can be misleading.

Example:
- 1 m/s difference in one county may be huge
- but small in another county

Z-score normalizes based on local historical variability.

---

# Step 4 — Compute Similarity

For every historical storm:

```text
hᵢ = historical storm
wᵢ = historical wind speed
```

---

## Euclidean Distance (1D)

Formula:

```math
distance = |wᵢ − w_current|
```

Meaning:
- absolute wind speed difference

---

## Normalized Distance

Formula:

```math
distance_normalized = |wᵢ − w_current| / σ
```

Meaning:
- distance measured in standard deviations

---

## Similarity Score

Formula:

```math
similarity = 1 / (1 + distance_normalized)
```

Range:

```text
0 → very different
1 → identical
```

Interpretation:
- 1.00 = identical wind speed
- 0.90+ = extremely similar
- 0.50 = moderately similar
- <0.30 = weak similarity

---

# Example Similarity Calculation

Example historical storm:

```text
wᵢ = 6.214515087
```

Current storm:

```text
w_current = 6.243981010
```

Distance:

```math
|6.2145 − 6.2440|
= 0.0295
```

Normalized distance:

```math
0.0295 / 0.321
= 0.09
```

Similarity:

```math
1 / (1 + 0.09)
= 0.917
```

Similarity percentage:

```math
0.917 × 100
= 91.7%
```

Interpretation:

```text
Storm S4 is 91.7% similar to the current storm.
```

---
# Bell Curve Interpretation

The historical wind speeds form a statistical distribution.

The bell curve:
- centers at mean (μ)
- spreads according to standard deviation (σ)

Current storm position on curve:
- indicates rarity
- shows whether storm is typical or unusual

Example:

```text
z = 0.80
```

Meaning:
- current storm is slightly above average
- still within normal historical range

---

# Advantages

## Explainable

Easy to explain why storms were matched.

---

## Fast

Only requires:
- arithmetic
- sorting

No training or GPUs.

---

## Deterministic

Same inputs always produce same outputs.

---

## Interpretable

Similarity scores have direct statistical meaning.

---

# Limitations

Current implementation only uses:

```text
peakWSPD10
```

This ignores:
- precipitation
- storm duration
- temperature
- outages
- pressure
- gusts

Two storms may:
- have similar wind speeds
- but very different impacts

---

# Future Improvements

Possible upgrades:

## Multi-dimensional similarity

Use:
- precipitation

---

## Time-decay Weighting

More recent storms matter more.

---

# Summary

This algorithm:
- compares storms statistically
- normalizes local variability
- computes similarity using standardized distance
- ranks historical storms by closeness

It is:
- fast
- interpretable
- explainable
- extensible

and serves as a strong baseline before introducing machine learning.

---

# TODO / Test Cases


# End
