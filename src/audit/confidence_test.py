from __future__ import annotations

import unittest

import pyarrow as pa
import polars as pl

from benchmark.audit.confidence import ConfidenceComputation


class TestComputeReadsReleasedWinners(unittest.TestCase):
    """compute reads the released winner per correction and carries its signals."""

    def _released(self, rows) -> pa.Table:
        return pa.table(
            {
                "correction": pa.array([r[0] for r in rows], type=pa.string()),
                "bic": pa.array([r[1] for r in rows], type=pa.string()),
                "name": pa.array([r[2] for r in rows], type=pa.string()),
                "number": pa.array([r[3] for r in rows], type=pa.int64()),
                "vote": pa.array([r[4] for r in rows], type=pa.float64()),
                "temporal_distance": pa.array([r[5] for r in rows], type=pa.float64()),
            }
        )

    def test_released_bic_is_reported_as_introducer(self) -> None:
        released = self._released([("t0", "foo", "o/r", 1, 1.0, 3.0)])
        result = pl.from_arrow(ConfidenceComputation(released).compute())
        self.assertEqual(result["introducer"].to_list(), ["foo"])
        self.assertEqual(result["number"].to_list(), [1])

    def test_single_correction_gets_full_confidence(self) -> None:
        released = self._released([("t0", "foo", "o/r", 1, 0.5, 9.0)])
        result = pl.from_arrow(ConfidenceComputation(released).compute())
        self.assertEqual(result.height, 1)
        self.assertAlmostEqual(result["rho"].to_list()[0], 1.0)

    def test_rho_ranks_vote_up_and_distance_down(self) -> None:
        # foo: high vote, far distance; bar: low vote, near distance.
        released = self._released(
            [
                ("t0", "foo", "o/r", 1, 1.0, 10.0),
                ("t1", "bar", "o/r", 2, 0.5, 1.0),
            ]
        )
        result = pl.from_arrow(ConfidenceComputation(released).compute()).sort("number")
        rho = dict(zip(result["number"].to_list(), result["rho"].to_list()))
        # each is best on exactly one signal, so both land at the midpoint.
        self.assertAlmostEqual(rho[1], 0.5)
        self.assertAlmostEqual(rho[2], 0.5)


class TestComputeExcludesAbortedLocalization(unittest.TestCase):
    """A correction whose released introducer aborted localization gets no rho."""

    def _released(self, rows) -> pa.Table:
        return pa.table(
            {
                "correction": pa.array([r[0] for r in rows], type=pa.string()),
                "bic": pa.array([r[1] for r in rows], type=pa.string()),
                "name": pa.array([r[2] for r in rows], type=pa.string()),
                "number": pa.array([r[3] for r in rows], type=pa.int64()),
                "vote": pa.array([r[4] for r in rows], type=pa.float64()),
                "temporal_distance": pa.array([r[5] for r in rows], type=pa.float64()),
            }
        )

    def _localization(self, name, number, defect, aborted) -> pa.Table:
        return pa.table(
            {
                "name": pa.array([name], type=pa.string()),
                "number": pa.array([number], type=pa.int64()),
                "defect": pa.array([defect], type=pa.string()),
                "aborted": pa.array([aborted], type=pa.bool_()),
            }
        )

    def test_aborted_correction_dropped(self) -> None:
        released = self._released(
            [
                ("t0", "foo", "o/r", 1, 1.0, 1.0),
                ("t1", "bar", "o/r", 2, 1.0, 2.0),
            ]
        )
        localization = {"loc|0": self._localization("o/r", 1, "foo", True)}
        result = pl.from_arrow(
            ConfidenceComputation(released, localization).compute()
        )
        # #1 (foo) aborted localization -> dropped; only #2 (bar) survives.
        self.assertEqual(result.height, 1)
        self.assertEqual(result["number"].to_list(), [2])
        self.assertAlmostEqual(result["rho"].to_list()[0], 1.0)

    def test_no_localization_keeps_all(self) -> None:
        released = self._released(
            [
                ("t0", "foo", "o/r", 1, 1.0, 1.0),
                ("t1", "bar", "o/r", 2, 0.5, 2.0),
            ]
        )
        result = pl.from_arrow(ConfidenceComputation(released).compute())
        self.assertEqual(result.height, 2)

    def test_all_aborted_yields_empty(self) -> None:
        released = self._released([("t0", "foo", "o/r", 1, 1.0, 1.0)])
        localization = {"loc|0": self._localization("o/r", 1, "foo", True)}
        result = pl.from_arrow(
            ConfidenceComputation(released, localization).compute()
        )
        self.assertEqual(result.height, 0)


if __name__ == "__main__":
    unittest.main()
