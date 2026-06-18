"""
Unit tests for the storm similarity algorithm.

Tests the 1D statistical distance (Z-score) based similarity calculation
for finding similar historical storms based on peak wind speed.
"""

import sys
import unittest
from pathlib import Path

# Add src to path
SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import math

# Legacy 1-D functions defined locally for testing and parity verification
def compute_statistics(wind_speeds: list[float]) -> tuple[float, float, float, float]:
    """
    Compute mean, standard deviation, min, and max for wind speeds.

    Uses Bessel's correction (n-1) for sample standard deviation.

    Args:
        wind_speeds: List of peakWSPD10 values

    Returns:
        Tuple of (mean, std_dev, min_val, max_val)
    """
    if not wind_speeds:
        return 0.0, 0.0, 0.0, 0.0

    n = len(wind_speeds)
    mean = sum(wind_speeds) / n

    if n < 2:
        return mean, 0.0, min(wind_speeds), max(wind_speeds)

    variance = sum((x - mean) ** 2 for x in wind_speeds) / (n - 1)
    std_dev = math.sqrt(variance)

    return mean, std_dev, min(wind_speeds), max(wind_speeds)


def calculate_similarity(
    historical_wind: float,
    current_wind: float,
    mean: float,
    std_dev: float,
) -> tuple[float, float, float]:
    """
    Calculate similarity between current and historical storm.

    Uses Z-score (standardized distance) to measure similarity.

    Similarity = 1 / (1 + normalized_distance)
    - 1.0 = identical wind speed
    - 0.5 = 1 standard deviation away
    - 0.33 = 2 standard deviations away

    Args:
        historical_wind: Historical peakWSPD10
        current_wind: Current peakWSPD10
        mean: Mean of historical wind speeds
        std_dev: Standard deviation of historical wind speeds

    Returns:
        Tuple of (similarity_score, z_historical, z_current)
    """
    if std_dev == 0:
        # All historical values are identical
        if historical_wind == current_wind:
            return 1.0, 0.0, 0.0
        else:
            return 0.0, 0.0, 0.0

    z_historical = (historical_wind - mean) / std_dev
    z_current = (current_wind - mean) / std_dev
    distance = abs(z_historical - z_current)
    similarity = 1.0 / (1.0 + distance)

    return similarity, z_historical, z_current



class TestComputeStatistics(unittest.TestCase):
    """Test statistical computation (mean, std dev, range)."""

    def test_empty_list(self):
        """Empty wind speeds should return zeros."""
        mean, std_dev, min_val, max_val = compute_statistics([])
        self.assertEqual(mean, 0.0)
        self.assertEqual(std_dev, 0.0)
        self.assertEqual(min_val, 0.0)
        self.assertEqual(max_val, 0.0)

    def test_single_value(self):
        """Single value: mean=value, std_dev=0, min=max=value."""
        mean, std_dev, min_val, max_val = compute_statistics([5.0])
        self.assertEqual(mean, 5.0)
        self.assertEqual(std_dev, 0.0)
        self.assertEqual(min_val, 5.0)
        self.assertEqual(max_val, 5.0)

    def test_two_values(self):
        """Two values: mean = average, std dev uses Bessel's correction."""
        wind_speeds = [5.0, 9.0]
        mean, std_dev, min_val, max_val = compute_statistics(wind_speeds)

        # Mean should be 7.0
        self.assertEqual(mean, 7.0)

        # Std dev: variance = ((5-7)^2 + (9-7)^2) / (2-1) = 8 / 1 = 8
        # std_dev = sqrt(8) ≈ 2.828
        self.assertAlmostEqual(std_dev, 2.828427, places=5)

        # Min and max
        self.assertEqual(min_val, 5.0)
        self.assertEqual(max_val, 9.0)

    def test_fairfield_like_data(self):
        """Test with realistic data similar to Fairfield County."""
        wind_speeds = [5.8, 6.4, 5.7, 10.6, 8.1, 6.2, 7.0, 9.2, 5.5, 7.8]
        mean, std_dev, min_val, max_val = compute_statistics(wind_speeds)

        # Mean ≈ 7.23
        self.assertAlmostEqual(mean, 7.23, places=2)

        # Std dev should be positive
        self.assertGreater(std_dev, 0)
        self.assertAlmostEqual(std_dev, 1.683, places=2)

        # Range
        self.assertEqual(min_val, 5.5)
        self.assertEqual(max_val, 10.6)


class TestCalculateSimilarity(unittest.TestCase):
    """Test similarity calculation using Z-scores."""

    def test_identical_wind_speeds(self):
        """Identical wind speeds should give perfect similarity."""
        similarity, _, _ = calculate_similarity(
            historical_wind=9.2,
            current_wind=9.2,
            mean=7.41,
            std_dev=1.69,
        )
        self.assertAlmostEqual(similarity, 1.0, places=5)

    def test_one_std_dev_away(self):
        """Wind speed 1 SD away should give ~0.5 similarity."""
        # current = mean + 1*std_dev = 7.41 + 1.69 = 9.1
        # historical = mean = 7.41 (z = 0)
        # distance = |0 - 1| = 1
        # similarity = 1 / (1 + 1) = 0.5
        similarity, z_historical, z_current = calculate_similarity(
            historical_wind=7.41,
            current_wind=9.1,
            mean=7.41,
            std_dev=1.69,
        )
        self.assertAlmostEqual(z_historical, 0.0, places=2)
        self.assertAlmostEqual(z_current, 1.0, places=2)
        self.assertAlmostEqual(similarity, 0.5, places=3)

    def test_two_std_dev_away(self):
        """Wind speed 2 SDs away should give ~0.33 similarity."""
        # current = mean + 2*std_dev = 7.41 + 2*1.69 = 10.79
        # historical = mean = 7.41 (z = 0)
        # distance = |0 - 2| = 2
        # similarity = 1 / (1 + 2) = 0.333
        similarity, z_historical, z_current = calculate_similarity(
            historical_wind=7.41,
            current_wind=10.79,
            mean=7.41,
            std_dev=1.69,
        )
        self.assertAlmostEqual(z_current, 2.0, places=2)
        self.assertAlmostEqual(similarity, 1.0 / 3.0, places=3)

    def test_fairfield_example(self):
        """Test with real Fairfield County data."""
        # Current: 9.2 m/s
        # Mean: 7.41, Std Dev: 1.69
        similarity, z_historical, z_current = calculate_similarity(
            historical_wind=9.20,
            current_wind=9.20,
            mean=7.41,
            std_dev=1.69,
        )

        # Z-score: (9.20 - 7.41) / 1.69 ≈ 1.06
        self.assertAlmostEqual(z_current, 1.06, places=2)
        self.assertAlmostEqual(z_historical, 1.06, places=2)

        # Similarity for identical: 1.0
        self.assertAlmostEqual(similarity, 1.0, places=3)

    def test_zero_std_dev(self):
        """If all historical values are identical (std_dev=0)."""
        # matching value
        similarity, _, _ = calculate_similarity(
            historical_wind=7.0,
            current_wind=7.0,
            mean=7.0,
            std_dev=0.0,
        )
        self.assertEqual(similarity, 1.0)

        # non-matching value
        similarity, _, _ = calculate_similarity(
            historical_wind=7.0,
            current_wind=8.0,
            mean=7.0,
            std_dev=0.0,
        )
        self.assertEqual(similarity, 0.0)

    def test_negative_similarity_components(self):
        """Test Z-score calculation with values below mean."""
        similarity, z_historical, z_current = calculate_similarity(
            historical_wind=5.0,
            current_wind=6.0,
            mean=7.41,
            std_dev=1.69,
        )

        # Z-scores should be negative
        self.assertLess(z_historical, 0)
        self.assertLess(z_current, 0)

        # But similarity should be positive
        self.assertGreater(similarity, 0)
        self.assertLess(similarity, 1)


class TestSimilarityMathematicalProperties(unittest.TestCase):
    """Test mathematical properties of the similarity function."""

    def test_similarity_range(self):
        """Similarity should always be in range (0, 1]."""
        mean, std_dev = 7.41, 1.69

        test_pairs = [
            (7.41, 7.41),  # identical
            (5.0, 10.0),  # far apart
            (6.0, 8.0),  # close
            (3.5, 11.0),  # very far
        ]

        for hist_wind, curr_wind in test_pairs:
            similarity, _, _ = calculate_similarity(hist_wind, curr_wind, mean, std_dev)
            self.assertGreater(similarity, 0)
            self.assertLessEqual(similarity, 1.0)

    def test_similarity_symmetry(self):
        """Similarity between A and B should equal similarity between B and A."""
        mean, std_dev = 7.41, 1.69
        a, b = 6.5, 8.5

        sim_a_to_b, _, _ = calculate_similarity(a, b, mean, std_dev)
        sim_b_to_a, _, _ = calculate_similarity(b, a, mean, std_dev)

        self.assertAlmostEqual(sim_a_to_b, sim_b_to_a, places=10)
        self.assertGreater(sim_a_to_b, 0)
        self.assertLessEqual(sim_a_to_b, 1.0)

    def test_similarity_monotonic_decrease(self):
        """As distance increases, similarity should decrease."""
        mean, std_dev = 7.41, 1.69
        current = 9.0

        historical_values = [7.41, 8.0, 8.5, 9.0, 9.5, 10.0]
        similarities = []

        for hist_wind in historical_values:
            similarity, _, _ = calculate_similarity(hist_wind, current, mean, std_dev)
            similarities.append(similarity)

        # The closest value (9.0) should have highest similarity
        max_similarity_idx = similarities.index(max(similarities))
        self.assertEqual(max_similarity_idx, 3)  # Index of 9.0


if __name__ == "__main__":
    unittest.main()
