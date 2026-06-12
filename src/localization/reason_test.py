from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import Mock, patch

from unidiff import PatchSet

from benchmark.attribution.diff.reference import PatchFactory, PatchView
from benchmark.attribution.domain import Change
from benchmark.attribution.history import VersionControl
from benchmark.localization.graph import LocateGraph
from benchmark.localization.reason import (
    LocateNode,
    LocateResponseSchema,
)


class TestPatchViewNumbered(unittest.TestCase):
    def _patch(self) -> PatchSet:
        return PatchSet(
            "\n".join(
                [
                    "diff --git a/a.py b/a.py",
                    "--- a/a.py",
                    "+++ b/a.py",
                    "@@ -4,2 +4,3 @@",
                    " foo",
                    "+bar",
                    " baz",
                ]
            )
        )

    def test_after_numbered(self) -> None:
        out = PatchView(self._patch(), numbered=True).after()

        self.assertEqual(out, "File: a.py\n4 | foo\n5 | bar\n6 | baz")

    def test_after_plain_default(self) -> None:
        out = PatchView(self._patch()).after()

        self.assertEqual(out, "File: a.py\nfoo\nbar\nbaz")

    def test_base_numbered(self) -> None:
        out = PatchView(self._patch(), numbered=True).base()

        self.assertEqual(out, "File: a.py\n4 | foo\n5 | baz")

    def test_base_plain_default(self) -> None:
        out = PatchView(self._patch()).base()

        self.assertEqual(out, "File: a.py\nfoo\nbaz")


class TestLocateNode(unittest.TestCase):
    def _expansion(self) -> str:
        return "\n".join(
            [
                "diff --git a/a.py b/a.py",
                "--- a/a.py",
                "+++ b/a.py",
                "@@ -1,3 +1,3 @@",
                " foo",
                "-bar",
                "+baz",
                " qux",
            ]
        )

    def _causes(self) -> list[dict[str, object]]:
        return [
            {
                "reason": "Old line.",
                "statements": [
                    {
                        "reference": "0-0-1",
                        "scope": "scope:program",
                        "excerpt": "bar",
                    }
                ],
            }
        ]

    def _refinement(self) -> PatchSet:
        return PatchSet(
            "\n".join(
                [
                    "diff --git a/a.py b/a.py",
                    "--- a/a.py",
                    "+++ b/a.py",
                    "@@ -4,2 +4,3 @@",
                    " keep",
                    "+buggy_line",
                    " tail",
                ]
            )
        )

    def test_locate(self) -> None:
        repository = Mock(spec_set=["blame", "blob"])
        repository.blame.return_value = [("defect-sha", "a.py", (5, 5))]
        repository.blob.return_value = b"keep\n"
        factory = PatchFactory.__new__(PatchFactory)
        factory.repository = repository

        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "locations": [{"line": 5, "excerpt": "buggy_line", "reason": "Holds the bug."}]
        }
        node = LocateNode(factory=factory, llm=llm)

        with patch.object(
            PatchFactory,
            "refine",
            autospec=True,
            return_value=self._refinement(),
        ) as materialize:
            result = node.run(
                {
                    "correction": Change(
                        number=1,
                        base="correction-base",
                        after="correction-after",
                        description="Fix bar",
                    ),
                    "rationale": "The old line is faulty.",
                    "causes": self._causes(),
                    "expansion": self._expansion(),
                    "defect": Change(
                        number=0,
                        base="defect-base",
                        after="defect-sha",
                        description="Introduce bar",
                    ),
                }
            )

        repository.blame.assert_called_once_with("a.py", "correction-base", [(2, 2)])
        materialize.assert_called_once()
        base, after, slices, anchor = materialize.call_args.args[1:5]
        self.assertEqual((base, after, anchor), ("defect-base", "defect-sha", "after"))
        self.assertEqual([item.file for item in slices], [("a.py", "a.py")])
        self.assertEqual(
            [tuple(anchor.after) for item in slices for anchor in item.anchors],
            [(5, 6)],
        )
        self.assertEqual(materialize.call_args.kwargs, {"force": False})
        llm.with_structured_output.assert_called_once()
        prompt = structured.invoke.call_args.args[0]
        self.assertIn("5 | buggy_line", prompt)
        self.assertIn("- scope:program bar", prompt)
        self.assertIn("The old line is faulty.", prompt)
        self.assertEqual(
            result,
            {
                "defect": Change(
                    number=0,
                    base="defect-base",
                    after="defect-sha",
                    description="Introduce bar",
                ),
                "locations": [
                    {
                        "file": "a.py",
                        "line": 5,
                        "excerpt": "buggy_line",
                        "reason": "Holds the bug.",
                    }
                ],
            },
        )

    def test_locate_drops_unmapped_lines(self) -> None:
        repository = Mock(spec_set=["blame", "blob"])
        repository.blame.return_value = [("defect-sha", "a.py", (5, 5))]
        repository.blob.return_value = b"keep\n"
        factory = PatchFactory.__new__(PatchFactory)
        factory.repository = repository

        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "locations": [{"line": 99, "excerpt": "ghost", "reason": "Not in snippet."}]
        }
        node = LocateNode(factory=factory, llm=llm)

        with patch.object(
            PatchFactory,
            "refine",
            autospec=True,
            return_value=self._refinement(),
        ):
            result = node.run(
                {
                    "correction": Change(
                        number=1,
                        base="correction-base",
                        after="correction-after",
                        description="Fix bar",
                    ),
                    "rationale": "The old line is faulty.",
                    "causes": self._causes(),
                    "expansion": self._expansion(),
                    "defect": Change(
                        number=0,
                        base="defect-base",
                        after="defect-sha",
                        description="Introduce bar",
                    ),
                }
            )

        self.assertEqual(result["locations"], [])

    def test_locate_skips_other_commits(self) -> None:
        repository = Mock(spec_set=["blame", "blob"])
        repository.blame.return_value = [("other-sha", "a.py", (7, 7))]
        repository.blob.return_value = b"keep\n"
        factory = PatchFactory.__new__(PatchFactory)
        factory.repository = repository

        llm = Mock(spec_set=["with_structured_output"])
        node = LocateNode(factory=factory, llm=llm)

        result = node.run(
            {
                "correction": Change(
                    number=1,
                    base="correction-base",
                    after="correction-after",
                    description="Fix bar",
                ),
                "rationale": "The old line is faulty.",
                "causes": self._causes(),
                "expansion": self._expansion(),
                "defect": Change(
                    number=0,
                    base="defect-base",
                    after="defect-sha",
                    description="Introduce bar",
                ),
            }
        )

        llm.with_structured_output.assert_not_called()
        self.assertEqual(result, {"defect": result["defect"], "locations": []})

    def test_locate_requires_llm(self) -> None:
        node = LocateNode()

        with self.assertRaises(ValueError):
            node.run(
                {
                    "rationale": "r",
                    "causes": self._causes(),
                    "expansion": self._expansion(),
                }
            )


class TestLocateGraph(unittest.TestCase):
    def _graph(self) -> LocateGraph:
        return LocateGraph(
            summarize=Mock(spec_set=["run"]),
            explain=Mock(spec_set=["run"]),
            aggregate=Mock(spec_set=["run"]),
            expand=Mock(spec_set=["run"]),
            hint=Mock(spec_set=["run"]),
            refine=Mock(spec_set=["run"]),
            locate=Mock(spec_set=["run"]),
        )

    def test_import_smoke(self) -> None:
        self.assertIsNotNone(LocateNode)
        self.assertIsNotNone(LocateGraph)
        self.assertIsNotNone(LocateResponseSchema)

    def test_requires_nodes(self) -> None:
        with self.assertRaises(ValueError):
            LocateGraph()

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_build_smoke(self) -> None:
        self.assertIsNotNone(self._graph().build())

    def test_run_locate_delegates_to_node(self) -> None:
        locate = Mock(spec_set=["run"])
        locate.run.return_value = {
            "defect": Change(
                number=0, base="defect-base", after="defect-sha", description="d"
            ),
            "locations": [{"file": "a.py", "line": 5, "excerpt": "x", "reason": "y"}],
        }
        graph = LocateGraph(
            summarize=Mock(spec_set=["run"]),
            explain=Mock(spec_set=["run"]),
            aggregate=Mock(spec_set=["run"]),
            expand=Mock(spec_set=["run"]),
            hint=Mock(spec_set=["run"]),
            refine=Mock(spec_set=["run"]),
            locate=locate,
        )

        result = graph._run_locate(
            {
                "correction": Change(
                    number=1, base="cb", after="ca", description="c"
                ),
                "rationale": "r",
                "defect": Change(
                    number=0, base="defect-base", after="defect-sha", description="d"
                ),
                "nodes": {
                    "expand": {"expansion": "e"},
                    "hint": {"causes": [{"reason": "x", "statements": []}]},
                },
            }
        )

        locate.run.assert_called_once()
        self.assertEqual(
            result["locations"],
            [{"file": "a.py", "line": 5, "excerpt": "x", "reason": "y"}],
        )
        self.assertEqual(result["nodes"]["locate"], locate.run.return_value)


if __name__ == "__main__":
    unittest.main()
