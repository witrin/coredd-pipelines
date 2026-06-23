from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import polars as pl

from benchmark.audit.confidence import ConfidenceComputation


class TestPredictions(unittest.TestCase):
    """_predictions collects one prediction per run partition (dict entry)."""

    def _partition(self, name, number, introducer, aborted) -> pa.Table:
        # a trace table carries several event rows; the run scalars repeat
        return pa.table(
            {
                "name": pa.array([name, name], type=pa.string()),
                "number": pa.array([number, number], type=pa.int64()),
                "introducer": pa.array([introducer, introducer], type=pa.string()),
                "aborted": pa.array([aborted, aborted], type=pa.bool_()),
            }
        )

    def test_one_prediction_per_partition_and_abort_filter(self) -> None:
        traces = {
            "corr|0": self._partition("o/r", 1, "foo", False),
            "corr|1": self._partition("o/r", 1, "foo", False),
            "corr|2": self._partition("o/r", 1, "bar", False),
            "corr|3": self._partition("o/r", 1, None, True),  # aborted, dropped
        }
        predictions = ConfidenceComputation(traces)._predictions()
        self.assertEqual(predictions.height, 3)  # 3 valid runs, aborted dropped
        self.assertEqual(set(predictions["introducer"].to_list()), {"foo", "bar"})

    def test_empty_when_all_aborted(self) -> None:
        traces = {"corr|0": self._partition("o/r", 1, None, True)}
        predictions = ConfidenceComputation(traces)._predictions()
        self.assertEqual(predictions.height, 0)


class TestResolveVoteAndTieBreak(unittest.TestCase):
    """_resolve picks the majority introducer, ties broken by smallest age."""

    def _commit_times(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # correction at day 100; candidates at increasing distance
        return {
            "correction-sha": base + timedelta(days=100),
            "foo": base + timedelta(days=99),   # distance 1
            "bar": base + timedelta(days=80),   # distance 20
            "baz": base + timedelta(days=50),   # distance 50
        }

    def _run(self, predictions: pl.DataFrame):
        times = self._commit_times()

        class FakeRepository:
            def meta(self, sha):
                return SimpleNamespace(time=times[sha])

        class FakePlatform:
            def load(self, number):
                return {"after": "correction-sha"}

        computation = ConfidenceComputation({})
        with patch.object(
            ConfidenceComputation, "_grouped", return_value={"o/r": predictions}
        ), patch(
            "benchmark.audit.confidence.PullRequestEnricher"
        ) as enricher, patch(
            "benchmark.audit.confidence.VersionControl"
        ) as version_control, patch(
            "benchmark.audit.confidence.PlatformCache", return_value=FakePlatform()
        ), patch(
            "benchmark.audit.confidence.storage"
        ), patch(
            "benchmark.audit.confidence.Shard"
        ):
            enricher.return_value = SimpleNamespace(
                enrich=None, attach=lambda *a, **k: None, table=lambda: None
            )
            version_control.create.return_value = FakeRepository()
            return computation._resolve(predictions, pl.DataFrame(), ["o/r"])

    def _predictions(self, shas):
        return pl.DataFrame(
            {
                "name": ["o/r"] * len(shas),
                "number": [1] * len(shas),
                "introducer": list(shas),
                "aborted": [False] * len(shas),
            }
        )

    def test_majority_vote(self) -> None:
        rows = self._run(self._predictions(["foo", "foo", "bar"]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["introducer"], "foo")
        self.assertAlmostEqual(rows[0]["vote"], 2 / 3)
        self.assertAlmostEqual(rows[0]["temporal_distance"], 1.0)  # foo is day 99

    def test_three_way_tie_smallest_age_wins(self) -> None:
        # each candidate one vote -> tie; foo has smallest distance (1 day)
        rows = self._run(self._predictions(["foo", "bar", "baz"]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["introducer"], "foo")
        self.assertAlmostEqual(rows[0]["vote"], 1 / 3)
        self.assertAlmostEqual(rows[0]["temporal_distance"], 1.0)

    def test_two_way_tie_smallest_age_wins(self) -> None:
        # bar and baz tie at one vote each; bar is closer (20 < 50)
        rows = self._run(self._predictions(["bar", "baz"]))
        self.assertEqual(rows[0]["introducer"], "bar")
        self.assertAlmostEqual(rows[0]["temporal_distance"], 20.0)


if __name__ == "__main__":
    unittest.main()
