from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cohort import (
    BREAKS,
    STRATA,
    bypass_metrics,
    contribution_metrics,
    participation_metrics,
    peer_reviews,
    record,
    review_metrics,
    stationarity_metrics,
)


def catalog(rows: list[dict]) -> pl.DataFrame:
    """A minimal issue catalog: merged changes with their review events."""
    return pl.DataFrame(
        rows,
        schema={
            "name": pl.Utf8,
            "author": pl.Utf8,
            "number": pl.Int64,
            "closed": pl.Utf8,
            "reviews": pl.List(pl.Struct({"type": pl.Utf8, "author": pl.Utf8})),
        },
    )


class TestPeerReviews(unittest.TestCase):
    """Which review events constitute peer review."""

    def test_bot_and_self_reviews_are_dropped(self) -> None:
        frame = catalog(
            [
                {
                    "name": "a/one",
                    "author": "alice",
                    "number": 1,
                    "closed": "2024-01-10T00:00:00Z",
                    "reviews": [
                        {"type": "user", "author": "bob"},
                        {"type": "user", "author": "alice"},
                        {"type": "bot", "author": "linter"},
                    ],
                }
            ]
        )
        reviews = peer_reviews(frame)
        self.assertEqual(reviews.height, 1)
        self.assertEqual(reviews["reviewer"].to_list(), ["bob"])


class TestReviewMetrics(unittest.TestCase):
    """Coverage, distinct reviewers, and the per-month reviewer rate."""

    def _catalog(self) -> pl.DataFrame:
        return catalog(
            [
                {
                    "name": "a/one",
                    "author": "alice",
                    "number": 1,
                    "closed": "2024-01-10T00:00:00Z",
                    "reviews": [
                        {"type": "user", "author": "bob"},
                        {"type": "user", "author": "carol"},
                    ],
                },
                {
                    "name": "a/one",
                    "author": "alice",
                    "number": 2,
                    "closed": "2024-02-10T00:00:00Z",
                    "reviews": [{"type": "user", "author": "alice"}],
                },
                {
                    "name": "a/one",
                    "author": "alice",
                    "number": 3,
                    "closed": "2024-02-20T00:00:00Z",
                    "reviews": [{"type": "user", "author": "bob"}],
                },
            ]
        )

    def test_coverage_counts_peer_reviewed_changes(self) -> None:
        row = review_metrics(self._catalog()).row(0, named=True)
        self.assertEqual(row["merges"], 3)
        # Change 2 carries only a self-review, so 2 of 3 changes are covered.
        self.assertAlmostEqual(row["coverage"], 2 / 3)

    def test_reviewers_are_distinct_over_the_history(self) -> None:
        row = review_metrics(self._catalog()).row(0, named=True)
        self.assertEqual(row["reviewers"], 2)

    def test_reviewer_rate_averages_active_months(self) -> None:
        # January has two distinct reviewers, February one.
        row = review_metrics(self._catalog()).row(0, named=True)
        self.assertAlmostEqual(row["reviewer_rate"], 1.5)

    def test_repository_without_reviews_scores_zero(self) -> None:
        frame = catalog(
            [
                {
                    "name": "b/two",
                    "author": "dave",
                    "number": 1,
                    "closed": "2024-01-10T00:00:00Z",
                    "reviews": [],
                }
            ]
        )
        row = review_metrics(frame).row(0, named=True)
        self.assertEqual(row["coverage"], 0.0)
        self.assertEqual(row["reviewers"], 0)
        self.assertEqual(row["reviewer_rate"], 0.0)


class TestParticipationMetrics(unittest.TestCase):
    """The discussion rate over the merged pull requests."""

    def test_only_foreign_user_comments_before_close_count(self) -> None:
        changes = catalog(
            [
                {
                    "name": "a/one",
                    "author": "alice",
                    "number": 1,
                    "closed": "2024-01-10T00:00:00Z",
                    "reviews": [],
                },
                {
                    "name": "a/one",
                    "author": "alice",
                    "number": 2,
                    "closed": "2024-01-20T00:00:00Z",
                    "reviews": [],
                },
            ]
        )
        discussion = pl.DataFrame(
            {
                "name": ["a/one", "a/one"],
                "number": [1, 2],
                "comments": [
                    [
                        {
                            "type": "user",
                            "author": "bob",
                            "created": "2024-01-05T00:00:00Z",
                        }
                    ],
                    [
                        # Self-comment, bot comment, and a comment after close.
                        {
                            "type": "user",
                            "author": "alice",
                            "created": "2024-01-05T00:00:00Z",
                        },
                        {
                            "type": "bot",
                            "author": "linter",
                            "created": "2024-01-05T00:00:00Z",
                        },
                        {
                            "type": "user",
                            "author": "bob",
                            "created": "2024-02-01T00:00:00Z",
                        },
                    ],
                ],
            }
        )
        row = participation_metrics(changes, discussion).row(0, named=True)
        self.assertAlmostEqual(row["share"], 0.5)

    def test_changes_without_comment_thread_stay_in_the_denominator(self) -> None:
        changes = catalog(
            [
                {
                    "name": "a/one",
                    "author": "alice",
                    "number": n,
                    "closed": "2024-01-10T00:00:00Z",
                    "reviews": [],
                }
                for n in (1, 2, 3, 4)
            ]
        )
        discussion = pl.DataFrame(
            {
                "name": ["a/one"],
                "number": [1],
                "comments": [
                    [
                        {
                            "type": "user",
                            "author": "bob",
                            "created": "2024-01-05T00:00:00Z",
                        }
                    ]
                ],
            }
        )
        row = participation_metrics(changes, discussion).row(0, named=True)
        self.assertAlmostEqual(row["share"], 0.25)


class TestContributionMetrics(unittest.TestCase):
    """History age and the Munaiah core-contributor covering."""

    def _payload(self, login: str | None, committed: str) -> str:
        author = {"user": {"login": login}} if login else {"user": None}
        return json.dumps({"author": author, "committedDate": committed})

    def test_core_is_the_smallest_covering_set(self) -> None:
        # alice carries 4 of 5 commits, so she alone covers the 80% threshold.
        rows = [
            {"name": "a/one", "payload": self._payload("alice", "2020-01-01T00:00:00Z")},
            {"name": "a/one", "payload": self._payload("alice", "2020-06-01T00:00:00Z")},
            {"name": "a/one", "payload": self._payload("alice", "2021-01-01T00:00:00Z")},
            {"name": "a/one", "payload": self._payload("alice", "2021-06-01T00:00:00Z")},
            {"name": "a/one", "payload": self._payload("bob", "2022-01-01T00:00:00Z")},
        ]
        row = contribution_metrics(pl.DataFrame(rows)).row(0, named=True)
        self.assertEqual(row["core"], 1)

    def test_balanced_authors_need_more_of_the_set(self) -> None:
        rows = [
            {"name": "a/one", "payload": self._payload(login, "2020-01-01T00:00:00Z")}
            for login in ("alice", "alice", "bob", "bob", "carol")
        ]
        row = contribution_metrics(pl.DataFrame(rows)).row(0, named=True)
        # alice and bob cover 4 of 5 commits; alice alone covers only 40%.
        self.assertEqual(row["core"], 2)

    def test_age_spans_the_commit_history_in_months(self) -> None:
        rows = [
            {"name": "a/one", "payload": self._payload("alice", "2020-01-01T00:00:00Z")},
            {"name": "a/one", "payload": self._payload("alice", "2021-01-01T00:00:00Z")},
        ]
        row = contribution_metrics(pl.DataFrame(rows)).row(0, named=True)
        self.assertEqual(row["age"], 12)


class TestBypassMetrics(unittest.TestCase):
    """The commit classification and the bypass share."""

    def _row(self, payload: dict) -> dict:
        return {"name": "a/one", "branch": "main", "payload": json.dumps(payload)}

    def _attributed(self) -> dict:
        return self._row(
            {
                "oid": "a" * 40,
                "associatedPullRequests": {
                    "nodes": [
                        {
                            "merged": True,
                            "baseRefName": "main",
                            "repository": {"nameWithOwner": "a/one"},
                        }
                    ]
                },
            }
        )

    def test_classes_are_counted_and_share_excludes_picks(self) -> None:
        frame = pl.DataFrame(
            [
                self._attributed(),
                self._attributed(),
                self._attributed(),
                # No associated pull request: a direct push to the trunk.
                self._row({"oid": "b" * 40}),
                # A cherry-pick trailer wins over the missing attribution.
                self._row(
                    {
                        "oid": "c" * 40,
                        "messageBody": f"(cherry picked from commit {'d' * 40})",
                    }
                ),
            ]
        )
        row = bypass_metrics(frame).row(0, named=True)
        self.assertEqual(row["attributed"], 3)
        self.assertEqual(row["bypassed"], 1)
        self.assertEqual(row["picked"], 1)
        self.assertEqual(row["ambiguous"], 0)
        self.assertAlmostEqual(row["bypass_share"], 0.25)

    def test_multiple_claiming_changes_are_ambiguous(self) -> None:
        claim = {
            "merged": True,
            "baseRefName": "main",
            "repository": {"nameWithOwner": "a/one"},
            "mergeCommit": {"oid": "e" * 40},
        }
        frame = pl.DataFrame(
            [
                self._attributed(),
                self._row(
                    {
                        "oid": "f" * 40,
                        "associatedPullRequests": {"nodes": [claim, claim]},
                    }
                ),
            ]
        )
        row = bypass_metrics(frame).row(0, named=True)
        self.assertEqual(row["ambiguous"], 1)


class TestStationarityMetrics(unittest.TestCase):
    """The onset and kind of the fitted review profiles."""

    def _reviews(self, probabilities: dict[str, list[float]]) -> pl.DataFrame:
        grid = [i / 10 for i in range(11)]
        return pl.DataFrame(
            {
                "name": list(probabilities),
                "profile": [[grid, p] for p in probabilities.values()],
            }
        )

    def test_flat_profile_is_stationary_at_onset_zero(self) -> None:
        row = stationarity_metrics(self._reviews({"a/one": [0.9] * 11})).row(
            0, named=True
        )
        self.assertEqual(row["onset"], 0.0)
        self.assertEqual(row["kind"], "stationary")

    def test_early_settling_profile_is_a_stationary_tail(self) -> None:
        row = stationarity_metrics(
            self._reviews({"a/one": [0.2] + [0.9] * 10})
        ).row(0, named=True)
        self.assertAlmostEqual(row["onset"], 0.1)
        self.assertEqual(row["kind"], "stationary tail")

    def test_unfitted_profiles_are_dropped(self) -> None:
        frame = pl.DataFrame(
            {
                "name": ["a/one", "b/two"],
                "profile": [[[0.0, 1.0], [0.9, 0.9]], None],
            }
        )
        self.assertEqual(stationarity_metrics(frame)["name"].to_list(), ["a/one"])


class TestStratum(unittest.TestCase):
    """The discussion-rate cut restates the sample asset's stratification."""

    def _stratum(self, share: float) -> str:
        return (
            pl.DataFrame({"share": [share]})
            .with_columns(
                pl.col("share").cut(breaks=BREAKS, labels=STRATA).cast(pl.Utf8)
            )["share"]
            .item()
        )

    def test_cuts_are_right_closed(self) -> None:
        self.assertEqual(self._stratum(0.32), "low")
        self.assertEqual(self._stratum(0.33), "mid")
        self.assertEqual(self._stratum(0.48), "mid")
        self.assertEqual(self._stratum(0.49), "high")


class TestRecord(unittest.TestCase):
    """The nested shape and rounding of one output record."""

    def _row(self) -> dict:
        return {
            "name": "canonical/operator",
            "review_score": 0.87654321,
            "review_low": 0.81,
            "review_high": 0.93,
            "coverage": 2 / 3,
            "onset": 0.0,
            "kind": "stationary",
            "bypass_share": 0.0123456,
            "attributed": 1234,
            "bypassed": 12,
            "picked": 3,
            "ambiguous": 0,
            "merges": 585,
            "reviewers": 24,
            "reviewer_rate": 3.14159,
            "sloc": 48210,
            "core": 4,
            "age": 71,
            "share": 0.4444444,
            "stratum": "mid",
        }

    def test_record_nests_the_field_groups(self) -> None:
        entry = record(self._row(), sampled=True)
        self.assertEqual(entry["repository"], "canonical/operator")
        self.assertEqual(entry["organization"], "canonical")
        self.assertEqual(entry["review"]["score"], 0.8765)
        self.assertEqual(entry["review"]["coverage"], 0.6667)
        self.assertEqual(entry["stationarity"], {"onset": 0.0, "kind": "stationary"})
        self.assertEqual(entry["bypass"]["share"], 0.0123)
        self.assertEqual(entry["merges"], {"total": 585, "rate": round(585 / 71, 4)})
        self.assertEqual(entry["reviewers"], {"total": 24, "rate": 3.1416})
        self.assertEqual(entry["scale"], {"sloc": 48210, "core": 4, "age": 71})
        self.assertEqual(
            entry["participation"],
            {"share": 0.4444, "stratum": "mid", "sampled": True},
        )

    def test_record_is_json_serialisable(self) -> None:
        json.dumps(record(self._row(), sampled=False))


if __name__ == "__main__":
    unittest.main()
