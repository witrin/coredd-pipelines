from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import Mock, patch

from unidiff import PatchSet

from benchmark.attribution.diff.reference import PatchFactory, PatchView
from benchmark.attribution.domain import Change, FileOperation
from benchmark.localization.agent import LocateTool, LocateToolFactory
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
    """The locate node searches N(c) with an agent and resolves marks against it."""

    def _expansion(self) -> PatchSet:
        # The inducing change N(c): numbered after-lines are 4,5,6.
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

    def _inputs(self) -> dict[str, object]:
        return {
            "correction": Change(
                number=1,
                base="correction-base",
                after="correction-after",
                description="Fix bar",
            ),
            "rationale": "The old line is faulty.",
            "causes": self._causes(),
            "defect": Change(
                number=0,
                base="defect-base",
                after="defect-sha",
                description="Introduce bar",
            ),
        }

    def _node(self, response: object) -> tuple[LocateNode, Mock]:
        factory = PatchFactory.__new__(PatchFactory)
        factory.repository = Mock()
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = []
        node = LocateNode(factory=factory, tool=tool, llm=Mock())

        agent = Mock(spec_set=["invoke"])
        agent.invoke.return_value = {"structured_response": response}
        return node, agent

    def test_locate_returns_agent_locations(self) -> None:
        response = LocateResponseSchema.model_validate(
            {
                "locations": [{"file": "a.py", "line": 5}],
                "reason": "Missing null check.",
            }
        )
        node, agent = self._node(response)

        with patch.object(
            PatchFactory, "expand", autospec=True, return_value=self._expansion()
        ), patch(
            "benchmark.localization.reason.create_react_agent", return_value=agent
        ):
            result = node.run(self._inputs())

        self.assertEqual(result["locations"], [{"file": "a.py", "line": 5}])
        self.assertEqual(result["reason"], "Missing null check.")

    def test_locate_passes_locations_through_unvalidated(self) -> None:
        # No post-hoc validation: whatever the agent reports is taken as-is,
        # mirroring attribution. A line the snippet never showed still passes.
        response = LocateResponseSchema.model_validate(
            {"locations": [{"file": "other.py", "line": 99}]}
        )
        node, agent = self._node(response)

        with patch.object(
            PatchFactory, "expand", autospec=True, return_value=self._expansion()
        ), patch(
            "benchmark.localization.reason.create_react_agent", return_value=agent
        ):
            result = node.run(self._inputs())

        self.assertEqual(result["locations"], [{"file": "other.py", "line": 99}])
        self.assertNotIn("reason", result)

    def test_locate_requires_inputs(self) -> None:
        node = LocateNode()

        with self.assertRaises(ValueError):
            node.run({"rationale": "r", "causes": self._causes()})

    def test_locate_rejects_nonpositive_steps(self) -> None:
        with self.assertRaises(ValueError):
            LocateNode(steps=0)


class TestLocateTool(unittest.TestCase):
    """The tool subset: show is numbered for the BIC, plain for the correction."""

    def _correction(self) -> Change:
        return Change(
            number=1, base="cb", after="ca", description="Fix"
        )

    def _defect(self) -> Change:
        return Change(
            number=0, base="db", after="da", description="Introduce"
        )

    def _expansion(self) -> PatchSet:
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

    def _tool(self) -> tuple[LocateTool, Mock]:
        repository = Mock()
        tool = LocateTool(
            repository=repository,
            correction=self._correction(),
            defect=self._defect(),
        )
        tool._factory = Mock(spec_set=["expand"])
        tool._factory.expand.return_value = self._expansion()
        operation = FileOperation(base="a.py", after="a.py", operation=Mock())
        repository.manifest.return_value = [operation]
        return tool, repository

    def test_show_defect_is_numbered(self) -> None:
        tool, _ = self._tool()

        out = tool.show(commit="da", file="a.py")

        self.assertIn("5 | buggy_line", out["patch"])
        self.assertIn("File: a.py", out["patch"])

    def test_show_correction_is_plain(self) -> None:
        tool, _ = self._tool()

        out = tool.show(commit="ca", file="a.py")

        self.assertIn("buggy_line", out["patch"])
        self.assertNotIn("5 | buggy_line", out["patch"])

    def test_show_rejects_unknown_commit(self) -> None:
        tool, _ = self._tool()

        with self.assertRaises(ValueError):
            tool.show(commit="zz", file="a.py")

    def test_show_unmodified_file_returns_note(self) -> None:
        tool, repository = self._tool()
        repository.manifest.return_value = []

        out = tool.show(commit="da", file="other.py")

        self.assertEqual(out["patch"], "")

    def test_grep_defaults_to_defect_snapshot(self) -> None:
        tool, repository = self._tool()
        repository.tree.return_value = ["a.py"]
        repository.blob.return_value = b"alpha\nbuggy_line\n"

        out = tool.grep(needle="buggy")

        repository.tree.assert_called_once_with("da")
        self.assertEqual(out, [{"path": "a.py", "matches": "L2: buggy_line"}])

    def test_grep_rejects_unknown_commit(self) -> None:
        tool, _ = self._tool()

        with self.assertRaises(ValueError):
            tool.grep(needle="x", commit="zz")

    def test_factory_binds_commits(self) -> None:
        repository = Mock()
        created = LocateToolFactory(repository).create(
            self._correction(), self._defect()
        )

        self.assertIsInstance(created, LocateTool)


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
            "locations": [{"file": "a.py", "line": 5}],
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
                    "hint": {"causes": [{"reason": "x", "statements": []}]},
                },
            }
        )

        locate.run.assert_called_once()
        call = locate.run.call_args.args[0]
        self.assertNotIn("expansion", call)
        self.assertEqual(
            result["locations"],
            [{"file": "a.py", "line": 5}],
        )
        self.assertEqual(result["nodes"]["locate"], locate.run.return_value)


if __name__ == "__main__":
    unittest.main()
