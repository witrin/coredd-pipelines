from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audits import Audit, Sample, Selection


class Node:
    """A stand-in for the projection's syntax node."""

    def __init__(self, start: tuple, end: tuple, type: str) -> None:
        self.start = start
        self.end = end
        self.type = type


class Projector:
    """A stand-in projection that answers from a line-to-node table."""

    def __init__(self, table: dict) -> None:
        self._table = table
        self.missed = 0

    def nodes(self, name: str, defect: str, path: str, lines: list[dict]) -> list[dict]:
        found: dict[tuple, dict] = {}
        for line in lines:
            node = self._table.get((path, line["line"]))
            if node is None:
                self.missed += 1
                continue
            identity = (node.start, node.end, node.type)
            if identity in found:
                continue
            found[identity] = {
                "start": list(node.start),
                "end": list(node.end),
                "type": node.type,
            }
        return [found[key] for key in sorted(found)]


class TestSelection(unittest.TestCase):
    """The exported form of one reviewed marking."""

    def _raw(self) -> dict:
        return {
            "path": "foo/bar.py",
            "origin": "suggestion",
            "status": "accepted",
            "sha": "defectsha",
            "lines": [{"line": 96, "side": "after"}, {"line": 97, "side": "after"}],
        }

    def test_commits_replace_the_sha(self) -> None:
        entry = Selection(Projector({})).of("foo/bar", "defectsha", "basesha", self._raw())
        self.assertEqual(entry["after"], "defectsha")
        self.assertEqual(entry["base"], "basesha")
        self.assertNotIn("sha", entry)

    def test_lines_keep_side_and_line(self) -> None:
        entry = Selection(Projector({})).of("foo/bar", "defectsha", "basesha", self._raw())
        self.assertEqual(
            entry["lines"],
            [{"side": "after", "line": 96}, {"side": "after", "line": 97}],
        )

    def test_lines_sharing_a_node_collapse(self) -> None:
        node = Node((91, 4), (102, 71), "block")
        projector = Projector({("foo/bar.py", 96): node, ("foo/bar.py", 97): node})
        entry = Selection(projector).of("foo/bar", "defectsha", "basesha", self._raw())
        self.assertEqual(entry["nodes"], [{"start": [91, 4], "end": [102, 71], "type": "block"}])

    def test_lines_without_node_are_counted(self) -> None:
        projector = Projector({})
        entry = Selection(projector).of("foo/bar", "defectsha", "basesha", self._raw())
        self.assertEqual(entry["nodes"], [])
        self.assertEqual(projector.missed, 2)

    def test_distinct_nodes_are_ordered_by_start(self) -> None:
        first = Node((10, 0), (12, 5), "block")
        second = Node((91, 4), (102, 71), "assignment")
        projector = Projector({("foo/bar.py", 96): second, ("foo/bar.py", 97): first})
        entry = Selection(projector).of("foo/bar", "defectsha", "basesha", self._raw())
        self.assertEqual([node["start"] for node in entry["nodes"]], [[10, 0], [91, 4]])


class TestSample(unittest.TestCase):
    """The audited pairs and their corrective merge commit."""

    def test_correction_sha_by_repository_and_defect(self) -> None:
        sample = Sample(
            [{"name": "foo/bar", "defect": "defectsha", "correction": "correctionsha"}]
        )
        self.assertEqual(sample.correction("foo/bar", "defectsha"), "correctionsha")


class TestAudit(unittest.TestCase):
    """Record assembly and the renamed fields."""

    def test_record_shape(self) -> None:
        record = Audit().record(
            repository="foo/bar",
            correction="correctionsha",
            defect="defectsha",
            annotator="baz",
            filter=["intrinsic"],
            attribution="correct",
            confidence=3,
            comment="",
            selections=[],
        )
        self.assertEqual(
            sorted(record),
            [
                "annotator",
                "attribution",
                "comment",
                "confidence",
                "correction",
                "defect",
                "filter",
                "repository",
                "selections",
            ],
        )
        self.assertEqual(record["correction"], "correctionsha")
        self.assertEqual(record["filter"], ["intrinsic"])

    def test_audit_bookkeeping_is_absent(self) -> None:
        record = Audit().record(
            repository="foo/bar",
            correction="correctionsha",
            defect="defectsha",
            annotator="baz",
            filter=[],
            attribution="wrong",
            confidence=1,
            comment="qux",
            selections=[],
        )
        for absent in ("rho_bin", "verdict", "status", "number", "introducer", "categories"):
            self.assertNotIn(absent, record)


if __name__ == "__main__":
    unittest.main()
