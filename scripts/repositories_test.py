from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark.definitions.partitions import Shard

from repositories import blob_prefix, select, target_path


class TestBlobPrefix(unittest.TestCase):
    """The GCS blob prefix a repository's tarball lives under."""

    def test_prefix_matches_asset_layout(self) -> None:
        self.assertEqual(
            blob_prefix("scope-source-raw", "4.0.0", "main", "ab12"),
            "assets/scope-source-raw/version=4.0.0/location=main/partition=ab12",
        )

    def test_prefix_carries_the_shard_verbatim(self) -> None:
        # The shard is the full to_space token (space prefix plus element index) and
        # is appended without transformation.
        names = ["a/one", "b/two", "c/three"]
        shard = Shard.to_space(names, "b/two")
        self.assertTrue(
            blob_prefix("scope-source-raw", "4.0.0", "main", shard).endswith(
                f"partition={shard}"
            )
        )


class TestTargetPath(unittest.TestCase):
    """The local path a repository's tarball is written to."""

    def test_owner_and_repo_split(self) -> None:
        self.assertEqual(
            target_path(Path("/out"), "mistralai/mistral-common"),
            Path("/out/mistralai/mistral-common.tar.gz"),
        )

    def test_nested_owner_keeps_all_but_last_segment(self) -> None:
        self.assertEqual(
            target_path(Path("/out"), "group/sub/repo"),
            Path("/out/group/sub/repo.tar.gz"),
        )


class TestSelect(unittest.TestCase):
    """Which repositories are loaded, and in what order."""

    def _stationary(self) -> list[str]:
        return ["c/three", "a/one", "b/two"]

    def test_empty_request_loads_all_sorted(self) -> None:
        self.assertEqual(
            select(self._stationary(), []),
            ["a/one", "b/two", "c/three"],
        )

    def test_request_is_a_subset_in_stationary_order(self) -> None:
        # Request order does not matter; the stationary sort fixes the order.
        self.assertEqual(
            select(self._stationary(), ["c/three", "a/one"]),
            ["a/one", "c/three"],
        )

    def test_single_request(self) -> None:
        self.assertEqual(
            select(self._stationary(), ["b/two"]),
            ["b/two"],
        )

    def test_unknown_request_is_a_hard_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            select(self._stationary(), ["a/one", "x/absent"])
        self.assertIn("x/absent", str(caught.exception))

    def test_error_names_every_missing_repository(self) -> None:
        with self.assertRaises(ValueError) as caught:
            select(self._stationary(), ["x/absent", "y/gone"])
        message = str(caught.exception)
        self.assertIn("x/absent", message)
        self.assertIn("y/gone", message)


if __name__ == "__main__":
    unittest.main()
