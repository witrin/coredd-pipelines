from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from noise import BINS, complete_bins, record


class TestCompleteBins(unittest.TestCase):
    """Filling a sparse rho_bin -> (a_j, b_j) map to all strata."""

    def test_missing_stratum_filled_with_zero_pair(self) -> None:
        # A map with strata 0 and 2 present gains a (0, 0) entry for the missing 1.
        result = complete_bins({0: (1, 2), 2: (3, 4)})
        self.assertEqual(
            result,
            {"0": [1, 2], "1": [0, 0], "2": [3, 4]},
        )

    def test_all_strata_always_present(self) -> None:
        result = complete_bins({})
        self.assertEqual(sorted(result), ["0", "1", "2"])
        for pair in result.values():
            self.assertEqual(pair, [0, 0])

    def test_keys_are_strings(self) -> None:
        result = complete_bins({0: (5, 6)})
        for key in result:
            self.assertIsInstance(key, str)

    def test_values_are_int_lists(self) -> None:
        # Numpy or tuple inputs are normalised to plain int lists for JSON.
        result = complete_bins({1: (7, 8)})
        pair = result["1"]
        self.assertIsInstance(pair, list)
        self.assertIsInstance(pair[0], int)
        self.assertIsInstance(pair[1], int)
        self.assertEqual(pair, [7, 8])

    def test_bins_constant_is_three(self) -> None:
        self.assertEqual(BINS, 3)


class TestRecord(unittest.TestCase):
    """Assembly and serialisation of the noise.json document."""

    def _counters(self) -> tuple[dict, dict]:
        counts_node = {0: (10, 14), 1: (60, 59), 2: (41, 38)}
        gamma_node = {0: (2, 3684), 1: (2, 12658), 2: (3, 7155)}
        return counts_node, gamma_node

    def test_top_level_shape(self) -> None:
        document = record(*self._counters())
        self.assertEqual(sorted(document), ["alpha", "bins", "gamma"])
        self.assertEqual(document["bins"], 3)

    def test_every_counter_has_three_string_keyed_strata(self) -> None:
        document = record(*self._counters())
        for family in ("alpha", "gamma"):
            counter = document[family]
            self.assertEqual(sorted(counter), ["0", "1", "2"])

    def test_alpha_is_counts_gamma_is_gamma(self) -> None:
        # Top-level alpha is counts_node; gamma is gamma_node.
        document = record(*self._counters())
        self.assertEqual(document["alpha"]["1"], [60, 59])
        self.assertEqual(document["alpha"]["0"], [10, 14])
        self.assertEqual(document["gamma"]["2"], [3, 7155])
        self.assertEqual(document["gamma"]["1"], [2, 12658])

    def test_missing_stratum_is_zero_filled(self) -> None:
        document = record({1: (1, 1)}, {})
        self.assertEqual(document["alpha"]["0"], [0, 0])
        self.assertEqual(document["alpha"]["1"], [1, 1])
        self.assertEqual(document["alpha"]["2"], [0, 0])

    def test_document_round_trips_through_json(self) -> None:
        document = record(*self._counters())
        reloaded = json.loads(json.dumps(document))
        self.assertEqual(reloaded, document)


if __name__ == "__main__":
    unittest.main()
