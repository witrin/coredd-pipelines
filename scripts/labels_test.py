from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark.localization.projection import NodeProjection

from labels import Attribution, Clock, Corpus, Export, Label, Nodes


class TestClock(unittest.TestCase):
    """Timestamp normalization to canonical UTC."""

    def test_zulu_becomes_canonical_utc(self) -> None:
        self.assertEqual(Clock().iso("2023-04-17T09:11:29Z"), "2023-04-17T09:11:29+00:00")



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

    def test_unknown_commit_is_none(self) -> None:
        self.assertIsNone(Attribution(self._rows()).change("absent"))


class TestCorpus(unittest.TestCase):
    """Merge timestamps of the corpus changes."""

    def test_merged_timestamp(self) -> None:
        rows = [
            {
                "name": "foo/bar",
                "number": 7,
                "payload": json.dumps({"merged_at": "2023-01-01T00:00:00Z"}),
            }
        ]
        self.assertEqual(Corpus(rows).merged("foo/bar", 7), "2023-01-01T00:00:00Z")


class FakeLine:
    def __init__(self, origin: str, old_lineno: int, new_lineno: int) -> None:
        self.origin = origin
        self.old_lineno = old_lineno
        self.new_lineno = new_lineno


class FakeHunk:
    def __init__(self, lines: list) -> None:
        self.lines = lines


class FakePatch:
    def __init__(self, new_path: str, old_path: str, lines: list) -> None:
        self.delta = type(
            "Delta",
            (),
            {
                "new_file": type("File", (), {"path": new_path})(),
                "old_file": type("File", (), {"path": old_path})(),
            },
        )()
        self.hunks = [FakeHunk([FakeLine(*line) for line in lines])]


class FakeRepository:
    """A repository whose commits, blobs and one diff are fixed in the test.

    ``diff`` returns the same line list for every context, which is enough: the
    projection reads only the context lines, the candidate walk only the edits.
    """

    def __init__(self, commits, diff_lines) -> None:
        self._commits = commits
        self._diff_lines = diff_lines

    def parents(self, commit: str) -> list:
        parent = self._commits[commit]["parent"]
        return [parent] if parent is not None else []

    def blob(self, commit: str, path: str) -> bytes:
        tree = self._commits[commit]["tree"]
        if path not in tree:
            raise FileNotFoundError(f"{path} not found in commit {commit}")
        return tree[path]

    def diff(self, base: str, commit: str, *, context: int = 1):
        return [FakePatch("a.py", "a.py", self._diff_lines)]


class TestNodes(unittest.TestCase):
    """The candidate node space N(c), defective and defect-free."""

    def _module(self) -> bytes:
        return (
            b"import os\n"
            b"\n"
            b"\n"
            b"class Loader:\n"
            b"    def load(self, path):\n"
            b"        data = open(path).read()\n"
            b"        return data\n"
        )

    def _repository(self, diff_lines) -> FakeRepository:
        commits = {
            "defect": {"parent": "base", "tree": {"a.py": self._module()}},
            "base": {"parent": None, "tree": {"a.py": self._module()}},
        }
        return FakeRepository(commits, diff_lines)

    def _attribution(self) -> Attribution:
        rows = []
        for identifier, number in (("mergeA", 9), ("mergeB", 12)):
            rows.append(
                {
                    "name": "foo/bar",
                    "identifier": identifier,
                    "payload": json.dumps(
                        {
                            "associatedPullRequests": {
                                "nodes": [
                                    {
                                        "number": number,
                                        "merged": True,
                                        "repository": {"nameWithOwner": "foo/bar"},
                                    }
                                ]
                            }
                        }
                    ),
                }
            )
        return Attribution(rows)

    def _label(self, correction: str, lines: list) -> dict:
        # A marking on base line 6 (`data = open(path).read()`) lifts to the
        # assignment node; the label struct records that node's own coordinates, so
        # it shares identity with the diff-lifted node and the two merge. The span is
        # read from the projection rather than hard-coded, so the test does not pin
        # the grammar's exact coordinates.
        node = NodeProjection(self._repository([])).project("defect", "a.py", 6, "base")
        return {
            "name": "foo/bar",
            "correction": correction,
            "rho": 0.5,
            "path": node.path,
            "start": list(node.start),
            "end": list(node.end),
            "type": node.type,
            "lines": lines,
        }

    def _nodes(self, labels: list, diff_lines: list) -> list:
        repository = self._repository(diff_lines)
        projection = NodeProjection(repository)
        rows = [{"defect": "defect", "label": labels}]
        return Nodes(rows, self._attribution()).of(
            "foo/bar", "defect", "base", repository, projection
        )

    def test_defect_free_node_has_no_provenance(self) -> None:
        # An edited base line 6 lifts to a node no correction points at.
        nodes = self._nodes([], [("-", 6, -1)])
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["type"], "assignment")
        self.assertEqual(node["base"], "base")
        self.assertEqual(node["after"], "defect")
        self.assertNotIn("provenance", node)
        self.assertNotIn("rho", node)

    def test_corrections_merge_onto_one_node(self) -> None:
        # Two corrections mark the same node from different lines; they collapse to
        # one node carrying both provenance lines.
        labels = [
            self._label("mergeA", ["R6"]),
            self._label("mergeB", ["R7"]),
        ]
        nodes = self._nodes(labels, [("-", 6, -1)])
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(
            node["provenance"],
            [
                {"side": "base", "line": 6, "corrections": [9]},
                {"side": "base", "line": 7, "corrections": [12]},
            ],
        )

    def test_same_line_merges_corrections(self) -> None:
        # One line named by two corrections merges to a single provenance entry.
        labels = [
            self._label("mergeA", ["R6"]),
            self._label("mergeB", ["R6"]),
        ]
        nodes = self._nodes(labels, [("-", 6, -1)])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(
            nodes[0]["provenance"],
            [{"side": "base", "line": 6, "corrections": [9, 12]}],
        )

    def test_base_and_after_live_on_node_not_provenance(self) -> None:
        nodes = self._nodes([self._label("mergeA", ["R6"])], [("-", 6, -1)])
        node = nodes[0]
        self.assertEqual(node["base"], "base")
        self.assertEqual(node["after"], "defect")
        for entry in node["provenance"]:
            self.assertNotIn("base", entry)
            self.assertNotIn("after", entry)
            self.assertNotIn("path", entry)

    def test_defective_node_bins_its_rho(self) -> None:
        # rho 0.5 falls in the middle of three equal-width strata over [0, 1] -> bin 1.
        nodes = self._nodes([self._label("mergeA", ["R6"])], [("-", 6, -1)])
        self.assertEqual(nodes[0]["bin"], 1)

    def test_defect_free_node_takes_median_fallback_bin(self) -> None:
        # One defective node at rho 0.5 (bin 1) and one defect-free node from an edited
        # line elsewhere; the free node inherits the change's median labelled rho (0.5),
        # so it also bins to 1.
        module = self._module()
        commits = {
            "defect": {"parent": "base", "tree": {"a.py": module}},
            "base": {"parent": None, "tree": {"a.py": module}},
        }
        repository = FakeRepository(commits, [("-", 6, -1), ("-", 5, -1)])
        projection = NodeProjection(repository)
        rows = [{"defect": "defect", "label": [self._label("mergeA", ["R6"])]}]
        nodes = Nodes(rows, self._attribution()).of(
            "foo/bar", "defect", "base", repository, projection
        )
        defective = [n for n in nodes if "provenance" in n]
        free = [n for n in nodes if "provenance" not in n]
        self.assertTrue(defective and free)
        self.assertEqual(defective[0]["bin"], 1)
        for node in free:
            self.assertEqual(node["bin"], 1)  # median fallback of the only rho, 0.5

    def test_every_node_carries_a_bin(self) -> None:
        nodes = self._nodes([self._label("mergeA", ["R6"])], [("-", 6, -1), ("-", 5, -1)])
        for node in nodes:
            self.assertIn("bin", node)
            self.assertIn(node["bin"], (0, 1, 2))


class TestSpanCollision(unittest.TestCase):
    """A block and the single statement filling it share one span, so they
    deduplicate to one node whose type is the more specific reading."""

    def _module(self) -> bytes:
        # The for-loop body is a single if/elif statement, so the loop body
        # ``block`` and the ``if_statement`` filling it span the same lines 2-5.
        return (
            b"for x in items:\n"
            b"    if x:\n"
            b"        a()\n"
            b"    elif y:\n"
            b"        b()\n"
        )

    def _repository(self, diff_lines) -> "FakeRepository":
        commits = {
            "defect": {"parent": "base", "tree": {"a.py": self._module()}},
            "base": {"parent": None, "tree": {"a.py": self._module()}},
        }
        return FakeRepository(commits, diff_lines)

    def _nodes(self, labels: list, diff_lines: list) -> list:
        repository = self._repository(diff_lines)
        projection = NodeProjection(repository)
        rows = [{"defect": "defect", "label": labels}]
        return Nodes(rows, TestNodes()._attribution()).of(
            "foo/bar", "defect", "base", repository, projection
        )

    def test_block_and_statement_collapse_to_specific_type(self) -> None:
        # Line 2 (the if header) lifts to the loop body ``block``; line 4 (the elif)
        # lifts to the ``if_statement`` filling it. Both span lines 2-5, so they are
        # one candidate, and the specific type wins over ``block``.
        projection = NodeProjection(self._repository([]))
        block = projection.project("defect", "a.py", 2, "base")
        statement = projection.project("defect", "a.py", 4, "base")
        # Guard the fixture: the two lines lift to the same span, one of them a block.
        self.assertEqual((block.start, block.end), (statement.start, statement.end))
        self.assertEqual(block.type, "block")
        self.assertNotEqual(statement.type, "block")

        nodes = self._nodes([], [("-", 2, -1), ("-", 4, -1)])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["type"], statement.type)


class TestGroup(unittest.TestCase):
    """Grouping the label asset's defects into records by pull request."""

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

    def test_one_record_per_attributed_defect(self) -> None:
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


class TestLabel(unittest.TestCase):
    """Record assembly."""

    def test_record_shape(self) -> None:
        record = Label().record(
            repository="foo/bar",
            pr=7,
            cutoff="2023-01-01T00:00:00+00:00",
            nodes=[],
        )
        self.assertEqual(
            sorted(record),
            ["cutoff", "nodes", "pr", "repository"],
        )
        self.assertEqual(record["pr"], 7)


if __name__ == "__main__":
    unittest.main()
