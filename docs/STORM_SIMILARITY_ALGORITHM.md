# Storm Similarity Algorithm

## Overview

The storm similarity algorithm finds historical storms in a specific county that are most similar to a current storm event based on weather conditions. 

Unlike the legacy 1-D univariate wind-only model, the new system supports **multi-dimensional Z-score similarity matching** using any combination of the 6 weather features available in the database:
- `peakWSPD10` (Peak wind speed)
- `sumAFWA_TOTPRECIP` (Total precipitation)
- `wgt5` (Hours wind speed > 5 m/s)
- `wgt9` (Hours wind speed > 9 m/s)
- `wgt13` (Hours wind speed > 13 m/s)
- `wgt18` (Hours wind speed > 18 m/s)

It standardises each feature to Z-scores (mean=0, std=1) across the historical dataset for that county, so all features live in the same unit-free space. Distance is measured as the weighted Euclidean (L2) norm in Z-score space. It is fully interpretable, pure Python with no external dependencies, and resolves queries in `<10ms`.

---

## Mathematical Approach

### Step 1: Fetch Historical Data

For a given county, retrieve historical weather observations:

```sql
SELECT
    w.stormCode,
    w.peakWSPD10,
    w.sumAFWA_TOTPRECIP,
    w.wgt5,
    w.wgt9,
    w.wgt13,
    w.wgt18,
    w.outage,
    s.eventDateTime,
    s.eventLength
FROM WeatherData w
JOIN Storm s ON s.stormCode = w.stormCode
WHERE LOWER(w.county) = LOWER(?);
```

### Step 2: Compute Population Statistics

For each selected feature $f$, compute the sample mean ($\mu_f$) and sample standard deviation ($\sigma_f$) across the historical dataset for that county:

**Mean ($\mu_f$):**
$$\mu_f = \frac{1}{n} \sum_{i=1}^n x_{i, f}$$

**Standard Deviation ($\sigma_f$) - with Bessel's correction:**
$$\sigma_f = \sqrt{\frac{1}{n-1} \sum_{i=1}^n (x_{i, f} - \mu_f)^2}$$

### Step 3: Standardize Values to Z-Scores

Convert absolute weather feature values to standardized units (number of standard deviations from the mean) to eliminate unit discrepancies (e.g. comparing wind speed in m/s with precipitation in mm):

$$z_{i, f} = \frac{x_{i, f} - \mu_f}{\sigma_f}$$
$$z_{\text{query}, f} = \frac{x_{\text{query}, f} - \mu_f}{\sigma_f}$$

### Step 4: Calculate Multi-Dimensional Z-Score Distance

Compute the weighted L2 (Euclidean) distance in Z-score space between the historical vector and the query vector over the set of active query features $F$:

$$\text{distance} = \sqrt{\sum_{f \in F} w_f \cdot (z_{i, f} - z_{\text{query}, f})^2}$$

*Note: Default weight $w_f = 1.0$ for all features. If all historical values for a feature are identical ($\sigma_f = 0.0$):*
*   *If the query matches the historical value, its distance contribution is $0.0$.*
*   *If the query differs from the historical value, the distance is treated as $\infty$, resulting in a similarity score of $0.0$ (exact matches are required for constant features).*

### Step 5: Convert Distance to Similarity Score

Convert the non-negative distance metric to a normalized similarity score in $(0, 1]$:

$$\text{similarity} = \frac{1}{1 + \text{distance}}$$

**Interpretation:**
- $\text{similarity} = 1.0$ $\rightarrow$ exact match on all active query parameters
- $\text{similarity} = 0.5$ $\rightarrow$ cumulative Z-score distance of 1.0
- $\text{similarity} \rightarrow 0$ $\rightarrow$ cumulative Z-score distance approaches infinity

### Step 6: Rank and Return Top N

Sort all historical storms by similarity score (descending) and return the top N matches, including historical outage counts as a guide for decision support.

---

## Usage

### Direct CLI Search

Find the top 5 similar storms in Fairfield County for an event with peak wind speed of 9.2 m/s and 45.0 mm of precipitation:

```bash
python scripts/find_similar_storms.py --county Fairfield --wind-speed 9.2 --precip 45.0 --limit 5
```

### Natural-Language routing CLI

```bash
python scripts/wiser_cli.py "find storms similar to wind=9.2 precip=45 in Fairfield" --limit 5
```

### Parity with Legacy 1-D wind-only algorithm
Querying with only a single feature (e.g., wind speed) yields the exact same ranking and similarity scores as the legacy 1-D algorithm:

```bash
python scripts/find_similar_storms.py --county Hartford --wind-speed 6.5
```

---

## Implementation Details

### File Structure

*   `src/wiser/similarity_engine.py`: Contains the multi-dimensional L2 Z-score math, population statistics computation, and ranking logic.
*   `src/wiser/agents/similarity_query_agent.py`: Database agent fetching historical records and invoking the similarity engine.
*   `scripts/find_similar_storms.py`: User-facing CLI tool displaying ranked similar storms and historical outages.
*   `tests/test_similarity_engine.py`: Unit tests verifying Z-score calculations, feature overrides, zero standard deviations, and single-parity checks.
*   `tests/test_similarity_algorithm.py`: Unit tests verifying legacy 1-D mathematical calculations (now defined locally in the tests suite).

### Core Functions

Inside `src/wiser/similarity_engine.py`:
```python
def compute_feature_stats(records, features) -> dict[str, tuple[float, float]]
    # Compute mean and standard deviation per feature

def compute_l2_zscore_distance(hist_values, query_values, stats, weights) -> float
    # Calculate weighted Euclidean distance in Z-score space

def distance_to_similarity(distance) -> float
    # Convert distance to score in (0, 1]

def rank_by_similarity(historical, query_params, feature_weights=None) -> list[tuple[dict, float]]
    # Orchestrates stats computation, distance calculation, and ranks results
```

Inside `src/wiser/agents/similarity_query_agent.py`:
```python
class SimilarityQueryAgent:
    def find_similar(self, conn, county, query_params, limit=5) -> list[dict]
        # Query database, invoke similarity_engine, and return top similar records
```

### Test Coverage

Run the multi-dimensional similarity unit tests:

```bash
python -m unittest tests.test_similarity_engine -v
```

This test suite covers:
1.  **Single-feature parity** – ensuring wind-only query matches the legacy 1-D Z-score math.
2.  **Two-feature ranking** – verifying that adding precipitation alters rankings appropriately.
3.  **Zero standard deviation safety** – ensuring constant features do not crash the engine.
4.  **Feature weight overrides** – testing that boosting specific feature weights modifies similarity and rankings.
5.  **Partial query parameters** – ensuring queries can run on any subset of the 6 weather features.

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

### 3. Narrative Impact Context

For user questions like "impact of the new storm", WISER can also retrieve narrative context from NOAA/NHC/NCEI pages and linked National Hurricane Center Tropical Cyclone Report PDFs:

```bash
python scripts/wiser_cli.py "impact of the new storm in Connecticut" --limit 5
```

This is a separate text-similarity workflow from the county wind-speed comparison above. External paragraphs are stored in the RAG tables, then ranked with optional Meta/Facebook FAISS vector search when `faiss-cpu` and `numpy` are installed. Without FAISS, WISER uses a dependency-free exact text-vector fallback. See [EXTERNAL_STORM_CONTEXT.md](EXTERNAL_STORM_CONTEXT.md).

### 4. Risk Assessment
Identify whether current conditions are "unusual" for the county:

```bash
python scripts/find_similar_storms.py --county "New Haven" --wind-speed 3.5
# → If similarity < 0.5, this is unusual (< 1 SD from mean)
```

---

## Performance Notes

- **Query time:** <10ms for 294 historical storms
- **Memory:** Minimal — one dict per historical row containing all six weather features plus metadata
- **Scalability:** O(n) where n = number of historical storms
- **Database:** Reads all six weather feature columns (`peakWSPD10`, `sumAFWA_TOTPRECIP`, `wgt5`, `wgt9`, `wgt13`, `wgt18`) per county query; scoring only uses the subset specified in `query_params`

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
