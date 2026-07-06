from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import Mock, patch

from unidiff import PatchSet

from benchmark.attribution.diff.reference import Numbering, PatchView
from benchmark.attribution.domain import Change
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
        out = PatchView(self._patch(), numbering=Numbering.AFTER).after()

        self.assertEqual(out, "File: a.py\nL4: foo\nL5: bar\nL6: baz")

    def test_after_plain_default(self) -> None:
        out = PatchView(self._patch()).after()

        self.assertEqual(out, "File: a.py\nfoo\nbar\nbaz")

    def test_base_numbered(self) -> None:
        out = PatchView(self._patch(), numbering=Numbering.BASE).base()

        self.assertEqual(out, "File: a.py\nL4: foo\nL5: baz")

    def test_base_plain_default(self) -> None:
        out = PatchView(self._patch()).base()

        self.assertEqual(out, "File: a.py\nfoo\nbaz")


class TestLocateNode(unittest.TestCase):
    """The locate node searches the inducing change guided by the fix context."""

    def _files(self) -> list[str]:
        # The suspect files of the correction — the agent's starting point.
        return ["a.py", "b.py"]

    def _inputs(self) -> dict[str, object]:
        return {
            "correction": Change(
                number=1,
                base="correction-base",
                after="correction-after",
                description="Fix bar",
            ),
            "rationale": "The old line is faulty.",
            "files": self._files(),
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
                "locations": [{"file": "a.py", "line": "L5"}],
                "reason": "Missing null check.",
            }
        )
        node, agent = self._node(response)

        with patch(
            "benchmark.localization.reason.create_react_agent", return_value=agent
        ):
            result = node.run(self._inputs())

        self.assertEqual(result["locations"], [{"file": "a.py", "line": "L5"}])
        self.assertEqual(result["reason"], "Missing null check.")

    def test_locate_passes_locations_through_unvalidated(self) -> None:
        # No post-hoc validation: whatever the agent reports is taken as-is,
        # mirroring attribution. A line the snippet never showed still passes.
        response = LocateResponseSchema.model_validate(
            {"locations": [{"file": "other.py", "line": "L99"}]}
        )
        node, agent = self._node(response)

        with patch(
            "benchmark.localization.reason.create_react_agent", return_value=agent
        ):
            result = node.run(self._inputs())

        self.assertEqual(result["locations"], [{"file": "other.py", "line": "L99"}])
        self.assertNotIn("reason", result)

    def test_locate_does_not_reveal_inducing_change(self) -> None:
        # The inducing change diff must NOT be handed to the agent — that would
        # trivialise the search. The message carries only root cause + fix context.
        response = LocateResponseSchema.model_validate(
            {"locations": [{"file": "a.py", "line": "L5"}]}
        )
        node, agent = self._node(response)

        with patch(
            "benchmark.localization.reason.create_react_agent", return_value=agent
        ):
            node.run(self._inputs())

        message = agent.invoke.call_args.args[0]["messages"][0].content
        self.assertIn("# Root cause", message)
        self.assertIn("# Suspect files of the corrective change", message)
        self.assertIn("a.py", message)
        # The defective change's own diff is never revealed; the agent must search.
        self.assertNotIn("# Inducing change", message)
        self.assertNotIn("# Defective change diff", message)

    def test_locate_requires_inputs(self) -> None:
        node = LocateNode()

        with self.assertRaises(ValueError):
            node.run({"rationale": "r", "files": self._files()})

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

    def _diff_text(self) -> str:
        return "\n".join(
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

    def _tool(self, rendered: str | None = None) -> tuple[LocateTool, Mock]:
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
        # show renders DiffRenderer().render(repository.diff(...)). The diff object
        # is opaque to the test; DiffRenderer is patched to yield the unified text.
        repository.diff.return_value = Mock()
        tool = LocateTool(
            repository=repository,
            correction=self._correction(),
            defect=self._defect(),
        )
        self._rendered = (
            rendered if rendered is not None else self._diff_text()
        )
        return tool, repository

    def _show(self, tool: LocateTool, **kwargs) -> dict:
        with patch(
            "benchmark.localization.agent.DiffRenderer.render",
            return_value=self._rendered,
        ):
            return tool.show(**kwargs)

    def test_show_defect_is_numbered(self) -> None:
        tool, _ = self._tool()

        out = self._show(tool, commit="da", file="a.py")

        # The defect side renders a signed diff: one copyable reference per line, an
        # after line as ``L<n>`` with no trailing colon.
        self.assertIn("L5 | +buggy_line", out["patch"])

    def test_show_correction_is_plain(self) -> None:
        tool, _ = self._tool()

        out = self._show(tool, commit="ca", file="a.py")

        # The correction side renders the same diff without line numbers.
        self.assertIn("+buggy_line", out["patch"])
        self.assertNotIn("L5 |", out["patch"])

    def test_show_rejects_unknown_commit(self) -> None:
        tool, _ = self._tool()

        with self.assertRaises(ValueError):
            self._show(tool, commit="zz", file="a.py")

    def test_show_unmodified_file_returns_note(self) -> None:
        tool, _ = self._tool(rendered="")

        out = self._show(tool, commit="da", file="other.py")

        self.assertEqual(out["patch"], "")

    def test_show_stat_lists_changed_files(self) -> None:
        tool, _ = self._tool()

        with patch(
            "benchmark.localization.agent.DiffRenderer.stat",
            return_value=[{"path": "a.py", "added": 2, "deleted": 1}],
        ):
            out = tool.show(commit="da", stat=True)

        self.assertEqual(out["files"], [{"path": "a.py", "added": 2, "deleted": 1}])
        self.assertNotIn("patch", out)

    def test_show_without_file_defaults_to_stat(self) -> None:
        tool, _ = self._tool()

        with patch(
            "benchmark.localization.agent.DiffRenderer.stat",
            return_value=[{"path": "a.py", "added": 2, "deleted": 1}],
        ):
            out = tool.show(commit="da")

        self.assertIn("files", out)
        self.assertNotIn("patch", out)

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
            locate=locate,
        )

        result = graph._run_locate(
            {
                "correction": Change(
                    number=1, base="cb", after="ca", description="c"
                ),
                "rationale": "r",
                "files": ["a.py", "b.py"],
                "defect": Change(
                    number=0, base="defect-base", after="defect-sha", description="d"
                ),
                "nodes": {},
            }
        )

        locate.run.assert_called_once()
        call = locate.run.call_args.args[0]
        self.assertNotIn("refinement", call)
        # The locate node receives the suspect files, not a refined fix context.
        self.assertEqual(call["files"], ["a.py", "b.py"])
        self.assertEqual(
            result["locations"],
            [{"file": "a.py", "line": 5}],
        )
        self.assertEqual(result["nodes"]["locate"], locate.run.return_value)


class _Delta:
    def __init__(self, path: str) -> None:
        self.new_file = type("F", (), {"path": path})()
        self.old_file = type("F", (), {"path": path})()


class _Hunk:
    def __init__(self, old_start, old_lines, new_start, new_lines) -> None:
        self.old_start = old_start
        self.old_lines = old_lines
        self.new_start = new_start
        self.new_lines = new_lines


class _Patch:
    def __init__(self, path, hunks) -> None:
        self.delta = _Delta(path)
        self.hunks = hunks


class _Repo:
    """Minimal VersionControl stand-in exposing only diff()."""

    def __init__(self, patches) -> None:
        self._patches = patches

    def diff(self, base, after, context=0):
        return self._patches


class TestReferenceLine(unittest.TestCase):
    def setUp(self) -> None:
        from benchmark.localization.reason import HunkSpans

        # deletion of base 543-544, addition of after 545-550
        self.spans = HunkSpans(before=[(543, 545)], after=[(545, 551)])

    def test_addition_reference_valid(self) -> None:
        from benchmark.localization.reason import ReferenceLine

        self.assertEqual(ReferenceLine("L548").classify(self.spans), "ok")

    def test_deletion_reference_valid(self) -> None:
        from benchmark.localization.reason import ReferenceLine

        self.assertEqual(ReferenceLine("D543").classify(self.spans), "ok")
        self.assertEqual(ReferenceLine("D544").classify(self.spans), "ok")

    def test_outside_hunks(self) -> None:
        from benchmark.localization.reason import ReferenceLine

        self.assertEqual(ReferenceLine("L999").classify(self.spans), "outside_hunks")
        self.assertEqual(ReferenceLine("D999").classify(self.spans), "outside_hunks")

    def test_bad_reference(self) -> None:
        from benchmark.localization.reason import ReferenceLine

        self.assertEqual(ReferenceLine("junk").classify(self.spans), "bad_ref")

    def test_bare_number_is_after(self) -> None:
        from benchmark.localization.reason import ReferenceLine

        ref = ReferenceLine("548")
        self.assertFalse(ref.deleted)
        self.assertEqual(ref.number, 548)

    def test_wrong_file(self) -> None:
        from benchmark.localization.reason import HunkSpans, ReferenceLine

        self.assertEqual(ReferenceLine("L548").classify(HunkSpans([], [])), "wrong_file")


class TestLocationValidator(unittest.TestCase):
    def test_partition_splits_good_and_bad(self) -> None:
        from benchmark.localization.reason import LocationValidator

        repo = _Repo([_Patch("f.py", [_Hunk(543, 2, 545, 6)])])
        validator = LocationValidator(
            repo, Change(number=0, base="b", after="a", description="d")
        )
        accepted, rejected = validator.partition(
            [
                {"file": "f.py", "line": "D543"},
                {"file": "f.py", "line": "L548"},
                {"file": "f.py", "line": "L999"},
                {"file": "other.py", "line": "L5"},
            ]
        )
        self.assertEqual(
            accepted,
            [{"file": "f.py", "line": "D543"}, {"file": "f.py", "line": "L548"}],
        )
        verdicts = {loc["line"]: verdict for loc, verdict in rejected}
        self.assertEqual(verdicts["L999"], "outside_hunks")
        self.assertEqual(verdicts["L5"], "wrong_file")


class TestLocateNodeRetry(unittest.TestCase):
    def _tool(self):
        tool = Mock()
        tool.tools.return_value = []
        # repo diff: change to f.py adding after lines 5-6
        tool._repository = _Repo([_Patch("f.py", [_Hunk(5, 0, 5, 2)])])
        return tool

    def test_retry_keeps_good_replaces_bad(self) -> None:
        from benchmark.localization import reason as R

        node = R.LocateNode(tool=Mock(), llm=Mock(), retries=1)
        tool = self._tool()
        first = R.LocateResponseSchema(
            locations=[
                R.LocateItemSchema(file="f.py", line="L5"),
                R.LocateItemSchema(file="f.py", line="L999"),
            ],
            reason="first",
        )
        second = R.LocateResponseSchema(
            locations=[R.LocateItemSchema(file="f.py", line="L6")],
            reason="second",
        )
        graph = Mock()
        graph.invoke.side_effect = [
            {"messages": [], "structured_response": first},
            {"messages": [], "structured_response": second},
        ]
        with patch.object(R, "create_react_agent", return_value=graph):
            out = node._search(
                rationale="r",
                files=["f.py"],
                defect=Change(number=0, base="db", after="da", description="d"),
                correction=Change(number=1, base="cb", after="ca", description="c"),
                tool=tool,
            )

        self.assertEqual(graph.invoke.call_count, 2)
        # L5 (good, kept) and L6 (replacement) survive; L999 (bad) is dropped.
        self.assertEqual(sorted(item.line for item in out.locations), ["L5", "L6"])

    def test_no_retry_when_all_valid(self) -> None:
        from benchmark.localization import reason as R

        node = R.LocateNode(tool=Mock(), llm=Mock(), retries=1)
        tool = self._tool()
        good = R.LocateResponseSchema(
            locations=[R.LocateItemSchema(file="f.py", line="L5")], reason=None
        )
        graph = Mock()
        graph.invoke.side_effect = [{"messages": [], "structured_response": good}]
        with patch.object(R, "create_react_agent", return_value=graph):
            out = node._search(
                rationale="r",
                files=["f.py"],
                defect=Change(number=0, base="db", after="da", description="d"),
                correction=Change(number=1, base="cb", after="ca", description="c"),
                tool=tool,
            )
        self.assertEqual(graph.invoke.call_count, 1)
        self.assertEqual([i.line for i in out.locations], ["L5"])


if __name__ == "__main__":
    unittest.main()
