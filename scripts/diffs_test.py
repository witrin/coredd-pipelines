from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diffs import Attribution, Export, Unidiff


class TestAttribution(unittest.TestCase):
    """Trunk commit to pull request mapping."""

    def _rows(self) -> list:
        return [
            {
                "name": "foo/bar",
                "identifier": "baz",
                "payload": json.dumps(
                    {
                        "associatedPullRequests": {
                            "nodes": [
                                {
                                    "number": 7,
                                    "merged": True,
                                    "repository": {"nameWithOwner": "foo/bar"},
                                }
                            ]
                        }
                    }
                ),
            },
            {
                "name": "foo/bar",
                "identifier": "qux",
                "payload": json.dumps(
                    {
                        "associatedPullRequests": {
                            "nodes": [
                                {
                                    "number": 9,
                                    "merged": False,
                                    "repository": {"nameWithOwner": "foo/bar"},
                                }
                            ]
                        }
                    }
                ),
            },
        ]

    def test_merged_request_of_same_repository_maps(self) -> None:
        self.assertEqual(Attribution(self._rows()).change("baz"), ("foo/bar", 7))

    def test_unmerged_does_not_map(self) -> None:
        self.assertIsNone(Attribution(self._rows()).change("qux"))


class FakeDiff:
    """A pygit2.Diff stand-in whose ``patch`` and iterated ``text`` are fixed."""

    def __init__(self, patch, entries=()) -> None:
        self.patch = patch
        self._entries = list(entries)

    def __iter__(self):
        return iter(self._entries)


class FakeEntry:
    def __init__(self, text) -> None:
        self.text = text


class FakeControl:
    """A version control handle recording its diff calls and returning fixed diffs."""

    def __init__(self, parents: dict, diffs: dict) -> None:
        self._parents = parents
        self._diffs = diffs
        self.calls: list = []

    def parents(self, commit: str) -> list:
        return [self._parents[commit]]

    def diff(self, base: str, after: str, *, context: int = 0):
        self.calls.append((base, after, context))
        return self._diffs[(base, after)]


class FakeRepos:
    """Repos stand-in sharing one control handle across every repository."""

    def __init__(self, control: FakeControl) -> None:
        self._control = control

    def control(self, name: str) -> FakeControl:
        return self._control

    def base(self, name: str, commit: str) -> str:
        return self._control.parents(commit)[0]


class TestUnidiff(unittest.TestCase):
    """Unified-diff text rendered through the own lib."""

    def test_patch_text_is_returned(self) -> None:
        body = "diff --git a/a.py b/a.py\n@@ -1,3 +1,3 @@\n-old\n+new\n"
        control = FakeControl({}, {("base", "after"): FakeDiff(body)})
        self.assertEqual(Unidiff().text(control, "base", "after"), body)

    def test_context_three_is_requested(self) -> None:
        control = FakeControl({}, {("base", "after"): FakeDiff("x")})
        Unidiff().text(control, "base", "after")
        self.assertEqual(control.calls, [("base", "after", 3)])

    def test_empty_diff_patch_none_falls_back_to_entries(self) -> None:
        # pygit2 yields ``patch is None`` for an empty diff; the concatenated
        # entry texts (also empty here) are the fallback.
        control = FakeControl(
            {}, {("base", "after"): FakeDiff(None, [FakeEntry(""), FakeEntry(None)])}
        )
        self.assertEqual(Unidiff().text(control, "base", "after"), "")


class TestGroup(unittest.TestCase):
    """Grouping the label asset's defects into changes by pull request."""

    def _attribution(self) -> Attribution:
        rows = []
        for identifier, name, number in (
            ("d1", "foo/bar", 7),
            ("d2", "foo/bar", 9),
            ("d3", "baz/qux", 3),
        ):
            rows.append(
                {
                    "name": name,
                    "identifier": identifier,
                    "payload": json.dumps(
                        {
                            "associatedPullRequests": {
                                "nodes": [
                                    {
                                        "number": number,
                                        "merged": True,
                                        "repository": {"nameWithOwner": name},
                                    }
                                ]
                            }
                        }
                    ),
                }
            )
        return Attribution(rows)

    def _export(self, repositories: list[str]) -> Export:
        export = Export.__new__(Export)
        export._filter = set(repositories)
        export._kept = 0
        export._flagged = 0
        return export

    def test_one_group_per_attributed_defect(self) -> None:
        export = self._export([])
        labels = [{"defect": "d1"}, {"defect": "d2"}, {"defect": "d3"}]
        grouped = export._group(labels, self._attribution())
        self.assertEqual(
            grouped,
            {
                ("foo/bar", 7): {"d1"},
                ("foo/bar", 9): {"d2"},
                ("baz/qux", 3): {"d3"},
            },
        )
        self.assertEqual(export._kept, 3)
        self.assertEqual(export._flagged, 0)

    def test_unattributed_defect_is_dropped_and_counted(self) -> None:
        export = self._export([])
        labels = [{"defect": "d1"}, {"defect": "unknown"}]
        grouped = export._group(labels, self._attribution())
        self.assertEqual(grouped, {("foo/bar", 7): {"d1"}})
        self.assertEqual(export._kept, 1)
        self.assertEqual(export._flagged, 1)

    def test_repository_filter_selects_by_name(self) -> None:
        export = self._export(["foo/bar"])
        labels = [{"defect": "d1"}, {"defect": "d3"}]
        grouped = export._group(labels, self._attribution())
        self.assertEqual(grouped, {("foo/bar", 7): {"d1"}})
        self.assertEqual(export._kept, 1)


class TestWrite(unittest.TestCase):
    """The layout and atomic write of the diff files."""

    def _export(self, outdir: Path) -> Export:
        export = Export.__new__(Export)
        export._outdir = outdir
        export._unidiff = Unidiff()
        return export

    def _repos(self, diffs: dict, parents: dict) -> FakeRepos:
        return FakeRepos(FakeControl(parents, diffs))

    def test_diff_lands_at_owner_repo_pr_base_after(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            body = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
            repos = self._repos(
                {("base", "defect"): FakeDiff(body)}, {"defect": "base"}
            )
            self._export(outdir)._write("foo/bar", 7, {"defect"}, repos)

            path = outdir / "foo" / "bar" / "7" / "base..defect.diff"
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), body)

    def test_multiple_defects_of_one_pr_each_get_a_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            repos = self._repos(
                {
                    ("p1", "d1"): FakeDiff("one\n"),
                    ("p2", "d2"): FakeDiff("two\n"),
                },
                {"d1": "p1", "d2": "p2"},
            )
            self._export(outdir)._write("foo/bar", 7, {"d1", "d2"}, repos)

            directory = outdir / "foo" / "bar" / "7"
            self.assertEqual(
                sorted(p.name for p in directory.iterdir()),
                ["p1..d1.diff", "p2..d2.diff"],
            )

    def test_write_leaves_no_temp_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            outdir = Path(raw)
            repos = self._repos(
                {("base", "defect"): FakeDiff("body\n")}, {"defect": "base"}
            )
            self._export(outdir)._write("foo/bar", 7, {"defect"}, repos)

            directory = outdir / "foo" / "bar" / "7"
            self.assertEqual(
                [p.name for p in directory.iterdir()], ["base..defect.diff"]
            )


if __name__ == "__main__":
    unittest.main()
