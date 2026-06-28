from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import Mock, patch

from unidiff import PatchSet

from benchmark.attribution.diff.reference import PatchView
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
    """The locate node searches the inducing change guided by the fix context."""

    def _refinement(self) -> str:
        # The refined context of the correction's fix — the agent's query.
        return "File: a.py\n+if value is None:\n     return"

    def _inputs(self) -> dict[str, object]:
        return {
            "correction": Change(
                number=1,
                base="correction-base",
                after="correction-after",
                description="Fix bar",
            ),
            "rationale": "The old line is faulty.",
            "refinement": self._refinement(),
            "defect": Change(
                number=0,
                base="defect-base",
                after="defect-sha",
                description="Introduce bar",
            ),
        }

    def _node(self, response: object) -> tuple[LocateNode, Mock]:
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = []
        node = LocateNode(tool=tool, llm=Mock())

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

        with patch(
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

        with patch(
            "benchmark.localization.reason.create_react_agent", return_value=agent
        ):
            result = node.run(self._inputs())

        self.assertEqual(result["locations"], [{"file": "other.py", "line": 99}])
        self.assertNotIn("reason", result)

    def test_locate_does_not_reveal_inducing_change(self) -> None:
        # The inducing change diff must NOT be handed to the agent — that would
        # trivialise the search. The message carries only root cause + fix context.
        response = LocateResponseSchema.model_validate(
            {"locations": [{"file": "a.py", "line": 5}]}
        )
        node, agent = self._node(response)

        with patch(
            "benchmark.localization.reason.create_react_agent", return_value=agent
        ):
            node.run(self._inputs())

        message = agent.invoke.call_args.args[0]["messages"][0].content
        self.assertIn("# Root cause", message)
        self.assertIn("# Refined context of the corrective change", message)
        # The defective change's own diff is never revealed; the agent must search.
        self.assertNotIn("# Inducing change", message)
        self.assertNotIn("# Defective change diff", message)

    def test_locate_requires_inputs(self) -> None:
        node = LocateNode()

        with self.assertRaises(ValueError):
            node.run({"rationale": "r", "refinement": self._refinement()})

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
        # revparse_single resolves a known commit to itself; anything else raises
        # KeyError, mirroring pygit2 so _resolve rejects unknown revisions.
        known = {"da", "db", "ca", "cb"}

        def revparse(commit: str):
            if commit not in known:
                raise KeyError(commit)
            node = Mock()
            peeled = Mock()
            peeled.id = commit
            node.peel.return_value = peeled
            return node

        repository.handle.revparse_single.side_effect = revparse
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
                    "refine": {"refinement": "File: a.py\n+if x is None:"},
                },
            }
        )

        locate.run.assert_called_once()
        call = locate.run.call_args.args[0]
        self.assertNotIn("expansion", call)
        # The locate node receives the refined fix context, not the raw causes.
        self.assertEqual(call["refinement"], "File: a.py\n+if x is None:")
        self.assertEqual(
            result["locations"],
            [{"file": "a.py", "line": 5}],
        )
        self.assertEqual(result["nodes"]["locate"], locate.run.return_value)


if __name__ == "__main__":
    unittest.main()
