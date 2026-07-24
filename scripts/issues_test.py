from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

from issues import Clock, Commits, Index, Resolver, Writer


class TestClock(unittest.TestCase):
    """Timestamp normalization to canonical UTC."""

    def test_zulu_becomes_canonical_utc(self) -> None:
        self.assertEqual(Clock().iso("2025-04-01T00:07:17Z"), "2025-04-01T00:07:17+00:00")

    def test_missing_is_none(self) -> None:
        self.assertIsNone(Clock().iso(None))
        self.assertIsNone(Clock().iso(""))


class TestResolver(unittest.TestCase):
    """Fragment resolution into atomic, dated events with unmodified payloads."""

    def test_pr_head_keeps_native_type_and_created_at(self) -> None:
        foo = {"number": 1, "created_at": "2025-01-01T00:00:00Z", "state": "closed"}
        events = list(Resolver().events({"pr": foo}, None))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "pr")
        self.assertEqual(events[0].time, "2025-01-01T00:00:00+00:00")
        self.assertIs(events[0].payload, foo)

    def test_issue_head_keeps_native_type(self) -> None:
        bar = {"number": 2, "created_at": "2025-02-02T00:00:00Z"}
        events = list(Resolver().events({"issue": bar}, None))
        self.assertEqual(events[0].type, "issue")

    def test_commit_array_dated_by_author_date(self) -> None:
        foo = {"commit": {"author": {"date": "2025-03-03T10:00:00Z"}}}
        bar = {"commit": {"author": {"date": "2025-03-04T10:00:00Z"}}}
        events = list(Resolver().events({"commit": [foo, bar]}, None))
        self.assertEqual([event.type for event in events], ["commit", "commit"])
        self.assertEqual(events[0].time, "2025-03-03T10:00:00+00:00")
        self.assertEqual(events[1].time, "2025-03-04T10:00:00+00:00")

    def test_review_dated_by_submitted_at(self) -> None:
        foo = {"state": "APPROVED", "submitted_at": "2025-05-05T12:00:00Z"}
        events = list(Resolver().events({"review": [foo]}, None))
        self.assertEqual(events[0].type, "review")
        self.assertEqual(events[0].time, "2025-05-05T12:00:00+00:00")

    def test_comment_and_review_comment_dated_by_created_at(self) -> None:
        foo = {"body": "foo", "created_at": "2025-06-06T00:00:00Z"}
        bar = {"body": "bar", "created_at": "2025-06-07T00:00:00Z"}
        events = list(Resolver().events({"comment": [foo], "review_comment": [bar]}, None))
        kinds = sorted(event.type for event in events)
        self.assertEqual(kinds, ["comment", "review_comment"])

    def test_timeline_created_at_and_nested_times(self) -> None:
        committed = {"event": "committed", "author": {"date": "2025-07-01T00:00:00Z"}}
        reviewed = {"event": "reviewed", "submitted_at": "2025-07-02T00:00:00Z"}
        labeled = {"event": "labeled", "created_at": "2025-07-03T00:00:00Z"}
        events = list(Resolver().events({"timeline": [committed, reviewed, labeled]}, None))
        by_type = {event.type: event.time for event in events}
        self.assertEqual(by_type["timeline:committed"], "2025-07-01T00:00:00+00:00")
        self.assertEqual(by_type["timeline:reviewed"], "2025-07-02T00:00:00+00:00")
        self.assertEqual(by_type["timeline:labeled"], "2025-07-03T00:00:00+00:00")

    def test_undated_event_is_dropped_and_counted(self) -> None:
        resolver = Resolver()
        foo = {"commit": {"author": {}}}
        events = list(resolver.events({"commit": [foo]}, None))
        self.assertEqual(events, [])
        self.assertEqual(resolver.dropped, 1)

    def test_review_request_matches_earliest_timeline_event(self) -> None:
        request = {"users": [{"login": "foo"}], "teams": []}
        early = {
            "event": "review_requested",
            "created_at": "2025-08-01T00:00:00Z",
            "requested_reviewer": {"login": "foo"},
        }
        late = {
            "event": "review_requested",
            "created_at": "2025-08-05T00:00:00Z",
            "requested_reviewer": {"login": "foo"},
        }
        events = list(
            Resolver().events({"review_request": request, "timeline": [late, early]}, None)
        )
        requests = [event for event in events if event.type == "review_request"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].time, "2025-08-01T00:00:00+00:00")

    def test_review_request_team_matches_by_slug(self) -> None:
        request = {"users": [], "teams": [{"slug": "bar"}]}
        event = {
            "event": "review_requested",
            "created_at": "2025-09-09T00:00:00Z",
            "requested_team": {"slug": "bar"},
        }
        events = list(
            Resolver().events({"review_request": request, "timeline": [event]}, None)
        )
        requests = [item for item in events if item.type == "review_request"]
        self.assertEqual(requests[0].time, "2025-09-09T00:00:00+00:00")

    def test_review_request_falls_back_to_head_time(self) -> None:
        request = {"users": [{"login": "baz"}], "teams": []}
        events = list(
            Resolver().events({"review_request": request}, "2025-10-10T00:00:00+00:00")
        )
        self.assertEqual(events[0].time, "2025-10-10T00:00:00+00:00")

    def test_review_request_without_time_is_dropped(self) -> None:
        resolver = Resolver()
        request = {"users": [{"login": "baz"}], "teams": []}
        events = list(resolver.events({"review_request": request}, None))
        self.assertEqual(events, [])
        self.assertEqual(resolver.dropped, 1)


class TestWriter(unittest.TestCase):
    """Atomic per-object JSONL writing, sorted and three-field."""

    def _events(self) -> list:
        from issues import Event

        return [
            Event("2025-01-03T00:00:00+00:00", "comment", {"body": "baz"}),
            Event("2025-01-01T00:00:00+00:00", "pr", {"body": "foo"}),
            Event("2025-01-02T00:00:00+00:00", "commit", {"body": "bar"}),
        ]

    def test_writes_sorted_three_field_lines_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            count = Writer().write(outdir, "foo/bar", 7, self._events())
            path = outdir / "foo" / "bar" / "7.jsonl"
            self.assertEqual(count, 3)
            self.assertTrue(path.exists())
            self.assertFalse((outdir / "foo" / "bar" / ".7.jsonl.tmp").exists())
            lines = path.read_text(encoding="utf-8").splitlines()
            times = [json.loads(line)["time"] for line in lines]
            self.assertEqual(times, sorted(times))
            for line in lines:
                self.assertEqual(sorted(json.loads(line)), ["payload", "time", "type"])


class TestIndex(unittest.TestCase):
    """The event index with zero-based line numbers."""

    def test_events_columns_and_line_numbers(self) -> None:
        from issues import Event

        events = [
            Event("2025-01-02T00:00:00+00:00", "commit", {}),
            Event("2025-01-01T00:00:00+00:00", "pr", {}),
        ]
        index = Index()
        index.add("foo/bar", 7, events)
        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            index.write(outdir)
            table = pq.read_table(outdir / "events.parquet").to_pylist()
        self.assertEqual(
            sorted(table[0]), ["issue", "line", "repository", "time", "type"]
        )
        self.assertEqual([row["line"] for row in table], [0, 1])
        self.assertEqual(table[0]["type"], "pr")


class TestCommits(unittest.TestCase):
    """The SHA-to-change mapping over branch commits and the merge commit."""

    def _row(self, identifier: str, number: int, merged: bool = True, owner: str = "foo/bar") -> dict:
        return {
            "name": "foo/bar",
            "identifier": identifier,
            "payload": json.dumps(
                {
                    "associatedPullRequests": {
                        "nodes": [
                            {
                                "number": number,
                                "merged": merged,
                                "repository": {"nameWithOwner": owner},
                            }
                        ]
                    }
                }
            ),
        }

    def test_trunk_commits_map_to_their_change(self) -> None:
        commits = Commits()
        commits.add([self._row("foo", 7), self._row("bar", 9)], set())
        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            commits.write(outdir)
            table = pq.read_table(outdir / "commits.parquet").to_pylist()
        self.assertEqual(sorted(table[0]), ["pr", "repository", "sha"])
        self.assertEqual({(row["sha"], row["pr"]) for row in table}, {("foo", 7), ("bar", 9)})
        self.assertTrue(all(row["repository"] == "foo/bar" for row in table))

    def test_unmerged_and_foreign_requests_are_skipped(self) -> None:
        commits = Commits()
        commits.add(
            [self._row("foo", 7, merged=False), self._row("bar", 9, owner="other/repo")],
            set(),
        )
        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            commits.write(outdir)
            table = pq.read_table(outdir / "commits.parquet").to_pylist()
        self.assertEqual(table, [])

    def test_duplicate_partitions_are_deduplicated(self) -> None:
        commits = Commits()
        commits.add([self._row("foo", 7)], set())
        commits.add([self._row("foo", 7)], set())
        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            commits.write(outdir)
            table = pq.read_table(outdir / "commits.parquet").to_pylist()
        self.assertEqual(len(table), 1)

    def test_repository_filter_applies(self) -> None:
        commits = Commits()
        commits.add([self._row("foo", 7)], {"other/repo"})
        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            commits.write(outdir)
            table = pq.read_table(outdir / "commits.parquet").to_pylist()
        self.assertEqual(table, [])


if __name__ == "__main__":
    unittest.main()
