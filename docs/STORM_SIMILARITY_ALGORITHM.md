# Storm Similarity Algorithm

## Overview

The storm similarity algorithm finds historical storms in a specific county that are most similar to a current storm event based on **peak wind speed (peakWSPD10)**.

It uses **1D statistical distance** with Z-score normalization—no machine learning required. This provides full interpretability while maintaining real-time performance (<10ms).

---

## Mathematical Approach

### Step 1: Fetch Historical Data

```sql
SELECT peakWSPD10 FROM WeatherData WHERE county = ?
```

For a given county, retrieve all historical wind speed observations.

**Example (Fairfield County):**
- 294 historical storms
- Wind speeds: [5.8, 6.4, 5.7, 10.6, ..., 6.2] m/s

### Step 2: Compute Population Statistics

Given: W = {w₁, w₂, ..., wₙ} (historical wind speeds)

**Mean (μ):**
```
μ = (1/n) × Σ wᵢ
```

**Standard Deviation (σ) - with Bessel's correction:**
```
σ = √[(1/(n-1)) × Σ(wᵢ - μ)²]
```

**Example:**
```
μ = 7.41 m/s
σ = 1.69 m/s
Range: 3.51 - 11.61 m/s
```

### Step 3: Standardize Values to Z-Scores

Convert absolute wind speeds to standardized units (# of SDs from mean).

**For any wind speed w:**
```
z = (w - μ) / σ
```

**Example (Fairfield, current storm = 9.2 m/s):**
```
z_current = (9.2 - 7.41) / 1.69 = 1.06 SDs above mean
```

### Step 4: Calculate Similarity (1D Distance)

For each historical storm with wind speed wᵢ:

**Z-score distance:**
```
distance = |z_i - z_current|
         = |(wᵢ - μ)/σ - (w_current - μ)/σ|
```

**Similarity score (inverse distance):**
```
similarity = 1 / (1 + distance)
           ∈ [0, 1]
```

**Interpretation:**
- `similarity = 1.0` → identical wind speed
- `similarity = 0.5` → 1 SD away
- `similarity ≈ 0.33` → 2 SDs away
- `similarity → 0` → many SDs away

### Step 5: Rank and Return Top N

Sort all historical storms by similarity (descending).

Return top N results with:
- Original wind speed
- Z-score
- Similarity score
- Historical outage count (for decision support)

---

## Usage

### Basic Query

Find top 5 similar storms in Fairfield County for a 9.2 m/s wind event:

```bash
python scripts/find_similar_storms.py --county Fairfield --wind-speed 9.2
```

### With Custom Limit

```bash
python scripts/find_similar_storms.py --county Hartford --wind-speed 6.5 --limit 10
```

### With Custom Database Path

```bash
python scripts/find_similar_storms.py \
  --county "New Haven" \
  --wind-speed 5.0 \
  --db path/to/custom.db
```

---

## Example Output

```
==========================================================================================
Storm Similarity Analysis for Fairfield
==========================================================================================

Current Storm:
  Peak Wind Speed (peakWSPD10): 9.20 m/s
  Z-Score: 1.06 (standardized distance from mean)

Historical Data Summary (Fairfield):
  Total Historical Storms: 294
  Mean Wind Speed: 7.41 m/s
  Std Dev: 1.69 m/s
  Range: 3.51 - 11.61 m/s

Rank   Storm Code      Wind (m/s)   Z-Score    Similarity   Outages    Precip (mm)  Event Date        
------------------------------------------------------------------------------------------
1      2020022700      9.20         1.06       99.9%        51         15.41        2020-02-27 00:00:00
2      2016032818      9.21         1.06       99.7%        124        0.02         2016-03-28 18:00:00
3      2023033118      9.22         1.07       99.0%        15         12.82        2023-03-31 18:00:00
4      2016030100      9.16         1.03       97.5%        24         16.98        2016-03-01 00:00:00
5      2020042112      9.16         1.03       97.4%        30         9.62         2020-04-21 12:00:00

==========================================================================================
```

**What the results mean:**
- Rank 1 has identical wind speed (99.9% similar) with 51 historical outages
- Rank 2 is almost identical (99.7% similar) with 124 historical outages
- Even Rank 5 is 97.4% similar (only 0.03 SD away)

---

## Algorithm Properties

### Advantages

✅ **Fully interpretable:** Z-scores explain exactly how far each match is from the mean
✅ **Real-time:** O(n) calculation for n historical storms (~10ms for 300 storms)
✅ **No training required:** Works immediately with historical data
✅ **Robust:** Handles edge cases (zero std dev, empty datasets)
✅ **Domain-appropriate:** Wind speed is directly relevant to outage prediction

### Why Not Machine Learning?

- ❌ Black-box: Can't explain "why" a storm is similar
- ❌ Requires labeled training data we don't have
- ❌ Overfitting risk: Only 294 storms × 8 counties = 2,352 observations
- ❌ Training overhead: Can't retrain in real-time
- ❌ Regulatory issues: Utilities need explainability

---

## Mathematical Properties

### Similarity Score Range

For any pairing, similarity ∈ (0, 1]:

| Distance (SDs) | Similarity | Example |
|---|---|---|
| 0 | 1.00 | Identical wind |
| 0.5 | 0.67 | Half SD away |
| 1.0 | 0.50 | 1 SD away |
| 2.0 | 0.33 | 2 SDs away |
| 3.0 | 0.25 | 3 SDs away |
| ∞ | 0 | Very different |

### Statistical Validity

Uses **Bessel's correction** (n-1 denominator) for unbiased sample standard deviation:

```
σ_sample = √[Σ(wᵢ - μ)² / (n-1)]
```

This accounts for estimation bias when working with samples rather than full populations.

---

## Implementation Details

### File Structure

```
scripts/find_similar_storms.py     # Main CLI script
tests/test_similarity_algorithm.py # Core algorithm unit tests
```

Canonical algorithm write-up: this file (`docs/STORM_SIMILARITY_ALGORITHM.md`).
`scripts/STORM_SIMILARITY_ALG.md` is a short pointer here for backward-compatible links.

### Core Functions

```python
def compute_statistics(wind_speeds) -> (mean, std_dev, min_val, max_val)
    # Compute μ, σ, and range

def calculate_similarity(hist_wind, current_wind, mean, std_dev) 
    -> (similarity, z_historical, z_current)
    # Calculate 1D Z-score distance

def find_similar_storms(county, current_wind_speed, limit=5) 
    -> list[storm_dict]
    # Query DB, compute stats, rank results
```

### Test Coverage

Run the similarity unit tests:

```bash
python -m unittest tests.test_similarity_algorithm -v
```

`tests/test_similarity_algorithm.py` covers statistics computation, similarity scoring,
edge cases (zero standard deviation), mathematical properties (range, symmetry,
monotonic decrease), and Fairfield-style scenarios.

---

## Use Cases

### 1. Outage Prediction
When a current storm has wind speed 9.2 m/s, find historical storms with similar wind to estimate expected outages:

```bash
python scripts/find_similar_storms.py --county Fairfield --wind-speed 9.2
# → Top match had 51 outages → expect ~51 outages for current storm
```

### 2. Emergency Response Planning
Before a forecasted storm, prepare resources based on similar historical events:

```bash
python scripts/find_similar_storms.py --county Hartford --wind-speed 10.5 --limit 3
# → Review top 3 most similar storms to understand typical impact
```

### 3. Risk Assessment
Identify whether current conditions are "unusual" for the county:

```bash
python scripts/find_similar_storms.py --county "New Haven" --wind-speed 3.5
# → If similarity < 0.5, this is unusual (< 1 SD from mean)
```

---

## Performance Notes

- **Query time:** <10ms for 294 historical storms
- **Memory:** Minimal (just wind speed array + statistics)
- **Scalability:** O(n) where n = number of historical storms
- **Database:** Reads only `peakWSPD10` column per query

For millions of historical records, consider adding:
- Database indexes on `county` and `peakWSPD10`
- Cached statistics updated periodically
- Parallel queries across counties

---

## References

- **Z-Score Formula:** (x - μ) / σ
- **Similarity Function:** 1 / (1 + |z_i - z_current|)
- **Bessel's Correction:** n-1 for sample std dev
- **Statistical Basis:** Standard univariate distance in normalized space
