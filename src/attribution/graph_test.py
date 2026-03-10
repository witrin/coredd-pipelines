from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from benchmark.attribution.diff import SourceDelta
from benchmark.attribution.domain import (
    Anchor,
    Change,
    Commit,
    FileOperation,
    OperationType,
    Slice,
)
from benchmark.attribution.history import VersionControl
from benchmark.attribution.graph import (
    AggregateState,
    AggregateNode,
    AttributionGraph,
    AttributionGraphState,
    BlameNode,
    CompareNode,
    ExpandNode,
    ExplainNode,
    ExplainResponseSchema,
    ExplainState,
    FinalizeNode,
    HintResponseSchema,
    HintNode,
    HintState,
    RefineNode,
    SelectNode,
    SummarizeNode,
    VerifyNode,
    VerifyResponseSchema,
)
from benchmark.attribution.tracing import (
    AttributionRunnable,
    ResultTable,
    Trace,
    TracingCallbackHandler,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from unidiff import PatchSet

class TestAttributionGraph(unittest.TestCase):
    def _graph(self) -> AttributionGraph:
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        return AttributionGraph(
            summarize=SummarizeNode(),
            explain=ExplainNode(),
            aggregate=AggregateNode(),
            expand=ExpandNode(),
            hint=HintNode(),
            refine=RefineNode(diff=delta),
            verify=VerifyNode(),
            blame=BlameNode(diff=delta),
            compare=CompareNode(diff=delta),
        )

    def test_import_smoke(self) -> None:
        self.assertIsNotNone(AttributionGraph)
        self.assertIsNotNone(SummarizeNode)
        self.assertIsNotNone(ExplainNode)
        self.assertIsNotNone(AggregateNode)

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_build_smoke(self) -> None:
        graph = self._graph().build()
        self.assertIsNotNone(graph)

    def test_graph_requires_explicit_semantic_nodes(self) -> None:
        with self.assertRaises(ValueError):
            AttributionGraph()

    def test_route_verify_defaults_to_abort(self) -> None:
        self.assertEqual(self._graph().route_verify({}), "abort")

    def test_route_verify_passes_on_success(self) -> None:
        self.assertEqual(
            self._graph().route_verify({"nodes": {"verify": {"verified": True}}}),
            "blame",
        )

    def test_route_select_finalizes_when_commit_is_set(self) -> None:
        self.assertEqual(
            self._graph().route_select({"introducer": "abc"}),
            "finalize",
        )

    def test_route_select_loops_when_candidates_remain(self) -> None:
        self.assertEqual(
            self._graph().route_select({
                "nodes": {
                    "blame": {"candidates": [{"commit": "a"}, {"commit": "b"}]},
                    "compare": {"index": 0},
                },
            }),
            "compare",
        )

    def test_route_select_aborts_when_exhausted(self) -> None:
        self.assertEqual(
            self._graph().route_select({
                "nodes": {
                    "blame": {"candidates": [{"commit": "a"}]},
                    "compare": {"index": 0},
                },
            }),
            "abort",
        )

    def test_route_select_aborts_when_compare_state_is_empty(self) -> None:
        self.assertEqual(
            self._graph().route_select({
                "nodes": {
                    "blame": {"candidates": [{"commit": "a"}]},
                    "compare": {},
                },
            }),
            "abort",
        )

    def test_route_explain_defaults_to_explain(self) -> None:
        self.assertEqual(self._graph().route_explain({}), "explain")

    def test_route_explain_loops_until_target(self) -> None:
        self.assertEqual(
            self._graph().route_explain({
                "files": ["a.py", "b.py"],
                "nodes": {"explain": {"samples": 2, "runs": [{"index": 0}]}},
            }),
            "explain",
        )

    def test_route_explain_aggregates_when_complete(self) -> None:
        self.assertEqual(
            self._graph().route_explain({
                "nodes": {
                    "explain": {
                        "samples": 2,
                        "runs": [{"index": 0}, {"index": 1}],
                    }
                },
            }),
            "aggregate",
        )

    def test_route_explain_shortens_to_file_count(self) -> None:
        graph = AttributionGraph(
            summarize=SummarizeNode(),
            explain=ExplainNode(),
            aggregate=AggregateNode(),
            expand=ExpandNode(),
            hint=HintNode(),
            refine=RefineNode(diff=SourceDelta(object.__new__(VersionControl))),
            verify=VerifyNode(),
            blame=BlameNode(diff=SourceDelta(object.__new__(VersionControl))),
            compare=CompareNode(diff=SourceDelta(object.__new__(VersionControl))),
        )

        self.assertEqual(
            graph.route_explain({
                "nodes": {
                    "explain": {
                        "samples": 3,
                        "runs": [{"index": 0}, {"index": 1}],
                    }
                },
            }),
            "aggregate",
        )

    @patch.object(SourceDelta, "patch", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_summarize_node_uses_patch_and_llm(
        self,
        manifest: Mock,
        delta_patch: Mock,
    ) -> None:
        operations = [FileOperation(base="old.py", after="new.py", operation=OperationType.EDIT)]
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="Fixes the bug.")
        node = SummarizeNode(
            diff=delta,
            llm=llm,
            context=2,
        )
        manifest.return_value = operations
        delta_patch.return_value = "@@ -1 +1 @@\n-a\n+b\n"

        result = node.run({
            "correction": Change(
                number=1,
                base="base-sha",
                after="after-sha",
                description="Fix bug",
            )
        })

        manifest.assert_called_once_with(
            repository,
            "base-sha",
            "after-sha",
        )
        delta_patch.assert_called_once_with(
            delta,
            "base-sha",
            "after-sha",
            files=operations,
            n=2,
        )
        llm.invoke.assert_called_once()
        self.assertIn("Patch:", llm.invoke.call_args.args[0])
        self.assertIn(
            "Each hunk may include up to 2 unchanged context lines around the edits.",
            llm.invoke.call_args.args[0],
        )
        self.assertNotIn("Fix bug", llm.invoke.call_args.args[0])
        self.assertNotIn("Correction description:", llm.invoke.call_args.args[0])
        self.assertNotIn("Semantic patch:", llm.invoke.call_args.args[0])
        self.assertEqual(result["summary"], "Fixes the bug.")
        self.assertEqual(result["files"], ["new.py"])
        self.assertNotIn("expanded", result)

    @patch.object(SourceDelta, "patch", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_summarize_node_skips_empty_patch(
        self,
        manifest: Mock,
        delta_patch: Mock,
    ) -> None:
        operations = [
            FileOperation(base="old.py", after="new.py", operation=OperationType.EDIT),
            FileOperation(base="only-old.py", after=None, operation=OperationType.DELETE),
        ]
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        llm = Mock(spec_set=["invoke"])
        node = SummarizeNode(
            diff=delta,
            llm=llm,
        )
        manifest.return_value = operations
        delta_patch.return_value = "   "

        result = node.run({
            "correction": Change(
                number=1,
                base="base-sha",
                after="after-sha",
                description="Fix bug",
            )
        })

        manifest.assert_called_once_with(
            repository,
            "base-sha",
            "after-sha",
        )
        delta_patch.assert_called_once_with(
            delta,
            "base-sha",
            "after-sha",
            files=operations,
            n=3,
        )
        llm.invoke.assert_not_called()
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["files"], ["new.py", "only-old.py"])

    @patch.object(SourceDelta, "patch", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_summarize_node_prompt_explains_zero_context(
        self,
        manifest: Mock,
        delta_patch: Mock,
    ) -> None:
        operations = [FileOperation(base="old.py", after="new.py", operation=OperationType.EDIT)]
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="Summary")
        node = SummarizeNode(
            diff=delta,
            llm=llm,
            context=0,
        )
        manifest.return_value = operations
        delta_patch.return_value = "@@ -1 +1 @@\n-a\n+b\n"

        node.run({
            "correction": Change(
                number=1,
                base="base-sha",
                after="after-sha",
                description="Fix bug",
            )
        })

        self.assertIn(
            "Each hunk contains changed lines only and no unchanged context.",
            llm.invoke.call_args.args[0],
        )

    def test_summarize_node_rejects_negative_n(self) -> None:
        with self.assertRaises(ValueError):
            SummarizeNode(context=-1)

    @patch.object(SourceDelta, "patch", autospec=True)
    @patch.object(VersionControl, "meta", autospec=True)
    @patch.object(VersionControl, "log", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_explain_node_appends_runs_and_filters_files(
        self,
        manifest: Mock,
        log: Mock,
        meta: Mock,
        delta_patch: Mock,
    ) -> None:
        operations = [
            FileOperation(base="a.py", after="a.py", operation=OperationType.EDIT),
            FileOperation(base="b.py", after="b.py", operation=OperationType.EDIT),
            FileOperation(base="c.py", after="c.py", operation=OperationType.EDIT),
        ]
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "rationale": "The validation logic and normalization changed together.",
            "files": ["c.py", "missing.py", "a.py", "c.py"],
        }
        node = ExplainNode(
            diff=delta,
            llm=llm,
            context=2,
        )
        manifest.return_value = operations
        log.return_value = ["commit-1", "after-sha"]
        meta.side_effect = [
            Commit("after-sha", "Merge commit message", datetime(2024, 1, 2, tzinfo=timezone.utc), "a"),
            Commit("commit-1", "Intermediate commit message", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"),
            Commit("after-sha", "Merge commit message", datetime(2024, 1, 2, tzinfo=timezone.utc), "a"),
            Commit("commit-1", "Intermediate commit message", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"),
        ]
        delta_patch.side_effect = ["a.py\nb.py\nc.py", "c.py\nb.py\na.py"]
        state = {
            "correction": Change(
                number=1,
                base="base-sha",
                after="after-sha",
                description="Fix the parser",
            ),
            "summary": "The parser logic changed across three files.",
        }

        first = node.run(state)
        second = node.run({
            **state,
            "nodes": first["nodes"],
        })

        self.assertEqual(log.call_count, 2)
        self.assertEqual(len(first["nodes"]["explain"]["runs"]), 1)
        self.assertEqual(first["nodes"]["explain"]["samples"], 3)
        self.assertNotIn("files", first)
        self.assertNotIn("rationale", first)
        self.assertEqual(
            first["nodes"]["explain"]["runs"][0]["files"],
            ["c.py", "missing.py", "a.py", "c.py"],
        )
        self.assertEqual(
            [call.args[0] for call in llm.with_structured_output.call_args_list],
            [
                ExplainResponseSchema,
                ExplainResponseSchema,
            ],
        )
        self.assertEqual(
            [call.kwargs for call in llm.with_structured_output.call_args_list],
            [
                {
                    "method": "json_schema",
                    "strict": True,
                },
                {
                    "method": "json_schema",
                    "strict": True,
                },
            ],
        )
        self.assertIn("Pull request description:", structured.invoke.call_args_list[0].args[0])
        self.assertIn("Fix the parser", structured.invoke.call_args_list[0].args[0])
        self.assertIn("Commit messages in (base, after]:", structured.invoke.call_args_list[0].args[0])
        self.assertIn("Merge commit message", structured.invoke.call_args_list[0].args[0])
        self.assertIn("Intermediate commit message", structured.invoke.call_args_list[0].args[0])
        self.assertIn("Respond only in JSON.", structured.invoke.call_args_list[0].args[0])
        self.assertIn(
            'Use this schema: {"rationale": string, "files": [string, ...]}.',
            structured.invoke.call_args_list[0].args[0],
        )
        self.assertEqual(len(second["nodes"]["explain"]["runs"]), 2)
        self.assertNotEqual(
            structured.invoke.call_args_list[0].args[0],
            structured.invoke.call_args_list[1].args[0],
        )
        self.assertEqual(
            [
                operation.after or operation.base
                for operation in delta_patch.call_args_list[0].kwargs["files"]
            ],
            ["a.py", "b.py", "c.py"],
        )
        self.assertEqual(delta_patch.call_args_list[0].kwargs["n"], 2)
        self.assertEqual(
            [
                operation.after or operation.base
                for operation in delta_patch.call_args_list[1].kwargs["files"]
            ],
            ["c.py", "b.py", "a.py"],
        )
        self.assertEqual(delta_patch.call_args_list[1].kwargs["n"], 2)

    def test_explain_node_rejects_invalid_json(self) -> None:
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = "not json"
        node = ExplainNode(diff=delta, llm=llm)

        with (
            patch.object(
                VersionControl,
                "manifest",
                autospec=True,
                return_value=[FileOperation(base="a.py", after="a.py", operation=OperationType.EDIT)],
            ),
            patch.object(VersionControl, "log", autospec=True, return_value=[]),
            patch.object(
                VersionControl,
                "meta",
                autospec=True,
                return_value=Commit(
                    "after-sha",
                    "",
                    datetime(2024, 1, 1, tzinfo=timezone.utc),
                    "a",
                ),
            ),
            patch.object(SourceDelta, "patch", autospec=True, return_value="a.py"),
        ):
            with self.assertRaises(ValueError):
                node.run({
                    "correction": Change(
                        number=1,
                        base="base-sha",
                        after="after-sha",
                        description="Fix the parser",
                    ),
                    "summary": "Summary",
                })

    def test_explain_node_uses_distinct_orders_per_run(self) -> None:
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "rationale": "Rationale",
            "files": ["a.py"],
        }
        node = ExplainNode(diff=delta, llm=llm, samples=3)
        state = {
            "correction": Change(
                number=1,
                base="base-sha",
                after="after-sha",
                description="Fix the parser",
            ),
            "summary": "Summary",
        }
        with (
            patch.object(
                VersionControl,
                "manifest",
                autospec=True,
                return_value=[
                    FileOperation(base="a.py", after="a.py", operation=OperationType.EDIT),
                    FileOperation(base="b.py", after="b.py", operation=OperationType.EDIT),
                ],
            ),
            patch.object(VersionControl, "log", autospec=True, return_value=[]),
            patch.object(
                VersionControl,
                "meta",
                autospec=True,
                side_effect=[
                    Commit("after-sha", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"),
                    Commit("after-sha", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"),
                ],
            ),
            patch.object(SourceDelta, "patch", autospec=True, side_effect=["a.py\nb.py", "b.py\na.py"]) as delta_patch,
        ):
            first = node.run(state)
            second = node.run({
                **state,
                "nodes": first["nodes"],
            })
        self.assertEqual(
            [call.args[0] for call in llm.with_structured_output.call_args_list],
            [
                ExplainResponseSchema,
                ExplainResponseSchema,
            ],
        )
        self.assertEqual(
            [call.kwargs for call in llm.with_structured_output.call_args_list],
            [
                {
                    "method": "json_schema",
                    "strict": True,
                },
                {
                    "method": "json_schema",
                    "strict": True,
                },
            ],
        )
        self.assertEqual(len(second["nodes"]["explain"]["runs"]), 2)
        self.assertEqual(
            [
                operation.after or operation.base
                for operation in delta_patch.call_args_list[0].kwargs["files"]
            ],
            ["a.py", "b.py"],
        )
        self.assertEqual(
            [
                operation.after or operation.base
                for operation in delta_patch.call_args_list[1].kwargs["files"]
            ],
            ["b.py", "a.py"],
        )

    def test_explain_node_rejects_invalid_context(self) -> None:
        with self.assertRaises(ValueError):
            ExplainNode(context=-1)

    def test_explain_node_rejects_invalid_samples(self) -> None:
        with self.assertRaises(ValueError):
            ExplainNode(samples=0)

    def test_expand_node_rejects_invalid_level(self) -> None:
        with self.assertRaises(ValueError):
            ExpandNode(level=0)

    def test_expand_node_rejects_invalid_context(self) -> None:
        with self.assertRaises(ValueError):
            ExpandNode(context=-1)

    def test_expand_node_expands_selected_operations(self) -> None:
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        node = ExpandNode(diff=delta, level=2, context=1)

        with (
            patch.object(
                VersionControl,
                "manifest",
                autospec=True,
                return_value=[
                    FileOperation(base="a.py", after="a.py", operation=OperationType.EDIT),
                    FileOperation(base="b.py", after="b.py", operation=OperationType.EDIT),
                    FileOperation(base="c.py", after="c.py", operation=OperationType.EDIT),
                ],
            ) as manifest,
            patch.object(SourceDelta, "expand", autospec=True, return_value="expanded") as expand,
        ):
            result = node.run({
                "correction": Change(
                    number=1,
                    base="base-sha",
                    after="after-sha",
                    description="Fix the parser",
                ),
                "files": ["c.py", "a.py"],
            })

        manifest.assert_called_once_with(repository, "base-sha", "after-sha")
        expand.assert_called_once()
        self.assertEqual(expand.call_args.args[1:3], ("base-sha", "after-sha"))
        self.assertEqual(
            [
                operation.after or operation.base
                for operation in expand.call_args.kwargs["files"]
            ],
            ["a.py", "c.py"],
        )
        self.assertEqual(
            (expand.call_args.kwargs["level"], expand.call_args.kwargs["n"]),
            (2, 1),
        )
        self.assertEqual(result["nodes"]["expand"]["expansion"], "expanded")
        self.assertNotIn("expansion", result)
        self.assertNotIn("files", result)

    def test_expand_node_returns_empty_state_for_unmatched_files(self) -> None:
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        node = ExpandNode(diff=delta)

        with patch.object(
            VersionControl,
            "manifest",
            autospec=True,
            return_value=[FileOperation(base="a.py", after="a.py", operation=OperationType.EDIT)],
        ):
            result = node.run({
                "correction": Change(
                    number=1,
                    base="base-sha",
                    after="after-sha",
                    description="Fix the parser",
                ),
                "files": ["missing.py"],
            })

        self.assertEqual(result["nodes"]["expand"], {})

    def test_hint_node_requires_inputs(self) -> None:
        node = HintNode()

        with self.assertRaises(ValueError):
            node.run({})

    def test_hint_node_returns_empty_state_for_empty_upstream(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])

        result = HintNode(llm=llm).run(
            {
                "rationale": "",
                "nodes": {
                    "expand": {"expansion": ""},
                },
            }
        )

        self.assertEqual(
            result["nodes"]["hint"],
            {"causes": [], "corrections": []},
        )

    def test_hint_node_renders_references_in_prompt(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [{"references": ["0-0-0"], "reason": "Old line."}],
            "corrections": [{"references": ["0-0-1"], "reason": "New line."}],
        }
        node = HintNode(llm=llm)

        node.run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1,2 +1,2 @@",
                                "-old",
                                "+new",
                                " context",
                            ]
                        )
                    }
                },
            }
        )

        prompt = structured.invoke.call_args.args[0]
        llm.with_structured_output.assert_called_once_with(
            HintResponseSchema,
            method="json_schema",
            strict=True,
        )
        self.assertIn("diff --git a/a.py b/a.py", prompt)
        self.assertIn("--- a/a.py", prompt)
        self.assertIn("+++ b/a.py", prompt)
        self.assertIn("@@ -1,2 +1,2 @@", prompt)
        self.assertIn("0-0-0 | -old", prompt)
        self.assertIn("0-0-1 | +new", prompt)
        self.assertIn("0-0-2 |  context", prompt)
        self.assertNotIn("1 -old", prompt)

    def test_hint_node_writes_structured_hint(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [
                {
                    "references": ["0-0-0", "0-0-2"],
                    "reason": "The old validation logic fails.",
                }
            ],
            "corrections": [
                {
                    "references": ["0-0-1", "0-0-2"],
                    "reason": "The new branch fixes the validation.",
                }
            ],
        }
        node = HintNode(llm=llm)

        result = node.run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1,2 +1,2 @@",
                                "-old",
                                "+new",
                                " context",
                            ]
                        )
                    }
                },
            }
        )

        self.assertEqual(
            result["nodes"]["hint"]["causes"],
            [
                {
                    "references": ["0-0-0", "0-0-2"],
                    "reason": "The old validation logic fails.",
                }
            ],
        )
        self.assertEqual(
            result["nodes"]["hint"]["corrections"],
            [
                {
                    "references": ["0-0-1", "0-0-2"],
                    "reason": "The new branch fixes the validation.",
                }
            ],
        )
        self.assertNotIn("references", result["nodes"]["hint"])
        self.assertNotIn("cause", result["nodes"]["hint"])
        self.assertNotIn("correction", result["nodes"]["hint"])
        self.assertNotIn("files", result)
        self.assertNotIn("rationale", result)
        llm.with_structured_output.assert_called_once_with(
            HintResponseSchema,
            method="json_schema",
            strict=True,
        )
        self.assertEqual(structured.invoke.call_count, 1)
        self.assertIn("Respond only in JSON.", structured.invoke.call_args.args[0])
        self.assertIn(
            'Use this schema: {"causes": [{"references": [string, ...], "reason": string}], "corrections": [{"references": [string, ...], "reason": string}]}.',
            structured.invoke.call_args.args[0],
        )
        self.assertIn("Root cause rationale:", structured.invoke.call_args.args[0])
        self.assertIn("Expanded context:", structured.invoke.call_args.args[0])

    def test_hint_node_rejects_invalid_json(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = "not json"
        node = HintNode(llm=llm)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "rationale": "Validation fails before normalization.",
                    "nodes": {
                        "expand": {
                            "expansion": "\n".join(
                                [
                                    "diff --git a/a.py b/a.py",
                                    "--- a/a.py",
                                    "+++ b/a.py",
                                    "@@ -1 +1 @@",
                                    "-old",
                                ]
                            )
                        }
                    },
                }
            )

    def test_hint_node_keeps_invalid_reference_strings(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [{"references": ["9-9-9"], "reason": "Bad reference."}],
            "corrections": [],
        }
        node = HintNode(llm=llm)

        result = node.run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1 +1 @@",
                                "-old",
                                "+new",
                            ]
                        )
                    }
                },
            }
        )

        self.assertEqual(
            result["nodes"]["hint"]["causes"],
            [
                {
                    "references": ["9-9-9"],
                    "reason": "Bad reference.",
                }
            ],
        )
        self.assertEqual(result["nodes"]["hint"]["corrections"], [])
        self.assertNotIn("references", result["nodes"]["hint"])

    def test_hint_node_keeps_cause_references_as_is(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [
                {"references": ["0-0-1"], "reason": "Bad cause projection."}
            ],
            "corrections": [],
        }
        node = HintNode(llm=llm)

        result = node.run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1 +1 @@",
                                "-old",
                                "+new",
                            ]
                        )
                    }
                },
            }
        )

        self.assertEqual(
            result["nodes"]["hint"]["causes"],
            [
                {
                    "references": ["0-0-1"],
                    "reason": "Bad cause projection.",
                }
            ],
        )
        self.assertNotIn("references", result["nodes"]["hint"])

    def test_hint_node_keeps_correction_references_as_is(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [],
            "corrections": [
                {"references": ["0-0-0"], "reason": "Bad correction projection."}
            ],
        }
        node = HintNode(llm=llm)

        result = node.run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1 +1 @@",
                                "-old",
                                "+new",
                            ]
                        )
                    }
                },
            }
        )

        self.assertEqual(
            result["nodes"]["hint"]["corrections"],
            [
                {
                    "references": ["0-0-0"],
                    "reason": "Bad correction projection.",
                }
            ],
        )
        self.assertNotIn("references", result["nodes"]["hint"])

    def test_hint_node_keeps_references_across_files(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [
                {
                    "references": ["0-0-0", "1-0-0"],
                    "reason": "Two files are involved.",
                }
            ],
            "corrections": [],
        }
        node = HintNode(llm=llm)

        result = node.run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1 +1 @@",
                                "-old_a",
                                "+new_a",
                                "diff --git a/b.py b/b.py",
                                "--- a/b.py",
                                "+++ b/b.py",
                                "@@ -2 +2 @@",
                                "-old_b",
                                "+new_b",
                            ]
                        )
                    }
                },
            }
        )

        self.assertEqual(
            result["nodes"]["hint"]["causes"],
            [
                {
                    "references": ["0-0-0", "1-0-0"],
                    "reason": "Two files are involved.",
                },
            ],
        )

    def test_refine_node_uses_cause_references_and_sets_refinement(self) -> None:
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        node = RefineNode(diff=delta)

        with patch.object(
            SourceDelta,
            "materialize",
            autospec=True,
            return_value=PatchSet(
                "\n".join(
                    [
                        "diff --git a/a.py b/a.py",
                        "--- a/a.py",
                        "+++ b/a.py",
                        "@@ -1 +1 @@",
                        "-old",
                        "+new",
                    ]
                )
            ),
        ) as materialize:
            result = node.run(
                {
                    "correction": Change(
                        number=1,
                        base="base-sha",
                        after="after-sha",
                        description="Fix parser",
                    ),
                    "nodes": {
                        "expand": {
                            "expansion": "\n".join(
                                [
                                    "diff --git a/a.py b/a.py",
                                    "--- a/a.py",
                                    "+++ b/a.py",
                                    "@@ -1 +1 @@",
                                    "-old",
                                    "+new",
                                ]
                            )
                        },
                        "hint": {
                            "causes": [
                                {"references": ["0-0-0"], "reason": "Bug line."},
                                {"references": ["0-0-1"], "reason": "Also selected."},
                            ],
                            "corrections": [
                                {"references": ["9-9-9"], "reason": "Ignore here."}
                            ],
                        },
                    },
                }
            )

        materialize.assert_called_once()
        self.assertEqual(materialize.call_args.args[1:5], ("base-sha", "after-sha", [
            Slice(
                file=("a.py", "a.py"),
                anchors=[Anchor(base=(1, 2), after=(0, 0))],
            )
        ], "base"))
        self.assertEqual(materialize.call_args.kwargs, {"force": True})
        self.assertEqual(
            materialize.call_args.args[3],
            [
                Slice(
                    file=("a.py", "a.py"),
                    anchors=[Anchor(base=(1, 2), after=(0, 0))],
                )
            ],
        )
        self.assertIn("--- a/a.py", result["nodes"]["refine"]["refinement"])

    def test_refine_node_requires_inputs(self) -> None:
        with self.assertRaises(ValueError):
            RefineNode().run({})

    def test_refine_node_returns_empty_state_for_empty_causes(self) -> None:
        repository = object.__new__(VersionControl)
        delta = SourceDelta(repository)
        node = RefineNode(diff=delta)

        result = node.run(
            {
                "correction": Change(
                    number=1,
                    base="base-sha",
                    after="after-sha",
                    description="Fix parser",
                ),
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1 +1 @@",
                                "-old",
                                "+new",
                            ]
                        )
                    },
                    "hint": {"causes": []},
                },
            }
        )

        self.assertEqual(result["nodes"]["refine"], {})

    def test_blame_node_collects_candidates_across_files(self) -> None:
        repository = object.__new__(VersionControl)
        node = BlameNode(diff=SourceDelta(repository))

        with (
            patch.object(
                VersionControl,
                "blame",
                autospec=True,
                side_effect=[
                    [
                        ("sha-old", (2, 2)),
                        ("sha-mid", (3, 3)),
                    ],
                    [
                        ("sha-new", (10, 10)),
                    ],
                ],
            ) as blame,
            patch.object(
                VersionControl,
                "parents",
                autospec=True,
                return_value=["parent-sha"],
            ),
            patch.object(
                VersionControl,
                "blob",
                autospec=True,
                return_value=b"present\n",
            ),
            patch.object(
                VersionControl,
                "meta",
                autospec=True,
                side_effect=[
                    Commit("sha-old", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"),
                    Commit("sha-mid", "", datetime(2024, 2, 1, tzinfo=timezone.utc), "a"),
                    Commit("sha-new", "", datetime(2024, 3, 1, tzinfo=timezone.utc), "a"),
                ],
            ),
        ):
            result = node.run(
                {
                    "correction": Change(
                        number=1,
                        base="base-sha",
                        after="after-sha",
                        description="Fix parser",
                    ),
                    "nodes": {
                        "expand": {
                            "expansion": "\n".join(
                                [
                                    "diff --git a/a.py b/a.py",
                                    "--- a/a.py",
                                    "+++ b/a.py",
                                    "@@ -1,3 +1,2 @@",
                                    " keep_a0",
                                    "-old_a1",
                                    "-old_a2",
                                    "+new_a",
                                    "diff --git a/b.py b/b.py",
                                    "--- a/b.py",
                                    "+++ b/b.py",
                                    "@@ -9,2 +9,1 @@",
                                    " keep_b0",
                                    "-old_b1",
                                ]
                            )
                        },
                        "hint": {
                            "causes": [
                                {
                                    "references": [
                                        "0-0-1",
                                        "0-0-2",
                                        "1-0-1",
                                    ],
                                    "reason": "Bug lines.",
                                }
                            ]
                        },
                    },
                }
            )

        self.assertEqual(
            [call.args[1:] for call in blame.call_args_list],
            [
                ("a.py", "base-sha", [(2, 2), (3, 3)]),
                ("b.py", "base-sha", [(10, 10)]),
            ],
        )
        self.assertEqual(
            result["nodes"]["blame"]["candidates"],
            [
                {
                    "commit": "sha-new",
                    "parent": "parent-sha",
                    "slices": [
                        {
                            "file": ("b.py", "b.py"),
                            "anchors": [{"base": (0, 0), "after": (10, 11)}],
                        }
                    ],
                },
                {
                    "commit": "sha-mid",
                    "parent": "parent-sha",
                    "slices": [
                        {
                            "file": ("a.py", "a.py"),
                            "anchors": [{"base": (0, 0), "after": (3, 4)}],
                        }
                    ],
                },
                {
                    "commit": "sha-old",
                    "parent": "parent-sha",
                    "slices": [
                        {
                            "file": ("a.py", "a.py"),
                            "anchors": [{"base": (0, 0), "after": (2, 3)}],
                        }
                    ],
                },
            ],
        )

    def test_blame_node_ignores_invalid_and_target_only_references(self) -> None:
        repository = object.__new__(VersionControl)
        node = BlameNode(diff=SourceDelta(repository))

        with (
            patch.object(
                VersionControl,
                "blame",
                autospec=True,
                return_value=[("sha-a", (2, 2))],
            ) as blame,
            patch.object(
                VersionControl,
                "parents",
                autospec=True,
                return_value=["parent-sha"],
            ),
            patch.object(
                VersionControl,
                "blob",
                autospec=True,
                return_value=b"present\n",
            ),
            patch.object(
                VersionControl,
                "meta",
                autospec=True,
                return_value=Commit("sha-a", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"),
            ),
        ):
            result = node.run(
                {
                    "correction": Change(
                        number=1,
                        base="base-sha",
                        after="after-sha",
                        description="Fix parser",
                    ),
                    "nodes": {
                        "expand": {
                            "expansion": "\n".join(
                                [
                                    "diff --git a/a.py b/a.py",
                                    "--- a/a.py",
                                    "+++ b/a.py",
                                    "@@ -1,2 +1,2 @@",
                                    " keep",
                                    "-old",
                                    "+new",
                                ]
                            )
                        },
                        "hint": {
                            "causes": [
                                {
                                    "references": ["x", "0-0-1", "0-0-2"],
                                    "reason": "Bug line.",
                                }
                            ]
                        },
                    },
                }
            )

        self.assertEqual([call.args[1:] for call in blame.call_args_list], [("a.py", "base-sha", [(2, 2)])])
        self.assertEqual(
            result["nodes"]["blame"]["candidates"],
            [
                {
                    "commit": "sha-a",
                    "parent": "parent-sha",
                    "slices": [
                        {
                            "file": ("a.py", "a.py"),
                            "anchors": [{"base": (0, 0), "after": (2, 3)}],
                        }
                    ],
                }
            ],
        )

    def test_blame_node_returns_empty_candidates_without_source_lines(self) -> None:
        repository = object.__new__(VersionControl)
        node = BlameNode(diff=SourceDelta(repository))

        result = node.run(
            {
                "correction": Change(
                    number=1,
                    base="base-sha",
                    after="after-sha",
                    description="Fix parser",
                ),
                "nodes": {
                    "expand": {
                        "expansion": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -0,0 +1 @@",
                                "+new",
                            ]
                        )
                    },
                    "hint": {
                        "causes": [
                            {"references": ["0-0-0", "bad"], "reason": "Bug line."}
                        ]
                    },
                },
            }
        )

        self.assertEqual(result["nodes"]["blame"]["candidates"], [])

    def test_blame_node_returns_empty_candidates_when_blame_is_empty(self) -> None:
        repository = object.__new__(VersionControl)
        node = BlameNode(diff=SourceDelta(repository))

        with patch.object(
            VersionControl,
            "blame",
            autospec=True,
            return_value=[(None, None)],
        ) as blame:
            result = node.run(
                {
                    "correction": Change(
                        number=1,
                        base="base-sha",
                        after="after-sha",
                        description="Fix parser",
                    ),
                    "nodes": {
                        "expand": {
                            "expansion": "\n".join(
                                [
                                    "diff --git a/a.py b/a.py",
                                    "--- a/a.py",
                                    "+++ b/a.py",
                                    "@@ -1,2 +1,1 @@",
                                    " keep",
                                    "-old",
                                ]
                            )
                        },
                        "hint": {
                            "causes": [
                                {"references": ["0-0-1"], "reason": "Bug line."}
                            ]
                        },
                    },
                }
            )

        self.assertEqual([call.args[1:] for call in blame.call_args_list], [("a.py", "base-sha", [(2, 2)])])
        self.assertEqual(result["nodes"]["blame"]["candidates"], [])

    def test_blame_node_requires_inputs(self) -> None:
        with self.assertRaises(ValueError):
            BlameNode().run({})

    def test_compare_node_uses_stored_refinement_for_candidate_and_parent(self) -> None:
        repository = object.__new__(VersionControl)
        llm = Mock(spec_set=["with_structured_output"])
        presence = Mock(spec_set=["invoke"])
        assessment = Mock(spec_set=["invoke"])
        llm.with_structured_output.side_effect = [presence, assessment]
        presence.invoke.side_effect = [
            {"present": True, "reason": "Candidate still matches."},
            {"present": False, "reason": "Parent does not match."},
        ]
        assessment.invoke.side_effect = [
            {"defect": True, "reason": "Candidate contains the bug."},
            {"defect": False, "reason": "Parent does not contain the bug."},
        ]
        node = CompareNode(diff=SourceDelta(repository), llm=llm)

        with patch.object(
            SourceDelta,
            "materialize",
            autospec=True,
            return_value=PatchSet(
                "\n".join(
                    [
                        "diff --git a/a.py b/a.py",
                        "--- a/a.py",
                        "+++ b/a.py",
                        "@@ -1,2 +1,2 @@",
                        " old_context",
                        "-old",
                        "+new",
                    ]
                )
            ),
        ) as materialize:
            result = node.run(
                {
                    "rationale": "Validation fails before normalization.",
                    "nodes": {
                        "hint": {
                            "causes": [{"references": ["0-0-0"], "reason": "Old line."}],
                        },
                        "blame": {
                            "candidates": [
                                {
                                    "commit": "candidate-sha",
                                    "parent": "parent-sha",
                                    "slices": [
                                        {
                                            "file": ("a.py", "a.py"),
                                            "anchors": [{"base": (2, 3), "after": (2, 3)}],
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                }
            )

        self.assertEqual(llm.with_structured_output.call_count, 2)
        materialize.assert_called_once()
        self.assertEqual(
            materialize.call_args.args[1:5],
            (
                "parent-sha",
                "candidate-sha",
                [
                    Slice(
                        file=("a.py", "a.py"),
                        anchors=[Anchor(base=(2, 3), after=(2, 3))],
                    )
                ],
                "after",
            ),
        )
        self.assertEqual(materialize.call_args.kwargs, {"force": False})
        self.assertEqual(presence.invoke.call_count, 2)
        self.assertEqual(assessment.invoke.call_count, 2)
        self.assertIn("Explanations:", presence.invoke.call_args_list[0].args[0])
        self.assertIn("File: a.py", presence.invoke.call_args_list[0].args[0])
        self.assertIn("old", presence.invoke.call_args_list[1].args[0])
        self.assertIn("new", presence.invoke.call_args_list[0].args[0])
        self.assertIn("Root cause:", assessment.invoke.call_args_list[0].args[0])
        self.assertIn("Explanations:", assessment.invoke.call_args_list[0].args[0])
        self.assertEqual(
            result["nodes"]["compare"],
            {
                "index": 0,
                "candidate": {
                    "commit": "candidate-sha",
                    "presence": {
                        "passed": True,
                        "reason": "Candidate still matches.",
                    },
                    "assessment": {
                        "passed": True,
                        "reason": "Candidate contains the bug.",
                    },
                    "defect": True,
                },
                "parent": {
                    "commit": "parent-sha",
                    "presence": {
                        "passed": False,
                        "reason": "Parent does not match.",
                    },
                    "assessment": {
                        "passed": False,
                        "reason": "Parent does not contain the bug.",
                    },
                    "defect": False,
                },
            },
        )

    def test_compare_node_advances_to_next_candidate(self) -> None:
        repository = object.__new__(VersionControl)
        llm = Mock(spec_set=["with_structured_output"])
        presence = Mock(spec_set=["invoke"])
        assessment = Mock(spec_set=["invoke"])
        llm.with_structured_output.side_effect = [presence, assessment]
        presence.invoke.side_effect = [
            {"present": True, "reason": "x"},
            {"present": True, "reason": "y"},
        ]
        assessment.invoke.side_effect = [
            {"defect": False, "reason": "x"},
            {"defect": False, "reason": "y"},
        ]

        with patch.object(
            SourceDelta,
            "materialize",
            autospec=True,
            return_value=PatchSet(
                "\n".join(
                    [
                        "diff --git a/b.py b/b.py",
                        "--- a/b.py",
                        "+++ b/b.py",
                        "@@ -2 +2 @@",
                        "-old",
                        "+new",
                    ]
                )
            ),
        ):
            result = CompareNode(diff=SourceDelta(repository), llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "nodes": {
                        "hint": {"causes": [{"references": ["0-0-0"], "reason": "Old line."}]},
                        "blame": {
                            "candidates": [
                                {
                                    "commit": "first",
                                    "parent": "base-1",
                                    "slices": [{"file": ("a.py", "a.py"), "anchors": [{"base": (1, 2), "after": (1, 2)}]}],
                                },
                                {
                                    "commit": "second",
                                    "parent": "base-2",
                                    "slices": [{"file": ("b.py", "b.py"), "anchors": [{"base": (2, 3), "after": (2, 3)}]}],
                                },
                            ]
                        },
                        "compare": {"index": 0},
                    },
                }
            )

        self.assertEqual(result["nodes"]["compare"]["index"], 1)
        self.assertEqual(result["nodes"]["compare"]["candidate"]["commit"], "second")
        self.assertEqual(result["nodes"]["compare"]["parent"]["commit"], "base-2")

    def test_compare_node_requires_inputs(self) -> None:
        with self.assertRaises(ValueError):
            CompareNode().run({})

    def test_compare_node_returns_empty_state_for_empty_candidates(self) -> None:
        repository = object.__new__(VersionControl)
        llm = Mock(spec_set=["with_structured_output"])

        result = CompareNode(diff=SourceDelta(repository), llm=llm).run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "hint": {"causes": [{"references": ["0-0-0"], "reason": "Old line."}]},
                    "blame": {"candidates": []},
                },
            }
        )

        self.assertEqual(
            result["nodes"]["compare"],
            {"index": -1, "candidate": {}, "parent": {}},
        )

    def test_compare_node_returns_empty_state_when_candidate_is_missing(self) -> None:
        repository = object.__new__(VersionControl)
        llm = Mock(spec_set=["with_structured_output"])

        result = CompareNode(diff=SourceDelta(repository), llm=llm).run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "hint": {"causes": [{"references": ["0-0-0"], "reason": "Old line."}]},
                    "blame": {"candidates": [{"commit": "a", "parent": "b", "slices": []}]},
                    "compare": {"index": 0},
                },
            }
        )

        self.assertEqual(
            result["nodes"]["compare"],
            {"index": 0, "candidate": {}, "parent": {}},
        )

    def test_compare_node_accepts_one_sided_projected_snippets(self) -> None:
        repository = object.__new__(VersionControl)
        llm = Mock(spec_set=["with_structured_output"])
        presence = Mock(spec_set=["invoke"])
        assessment = Mock(spec_set=["invoke"])
        llm.with_structured_output.side_effect = [presence, assessment]
        presence.invoke.side_effect = [
            {"present": True, "reason": "candidate"},
            {"present": False, "reason": "parent"},
        ]
        assessment.invoke.side_effect = [
            {"defect": True, "reason": "candidate"},
            {"defect": False, "reason": "parent"},
        ]

        with patch.object(
            SourceDelta,
            "materialize",
            autospec=True,
            return_value=PatchSet(
                "\n".join(
                    [
                        "diff --git a/a.py b/a.py",
                        "--- /dev/null",
                        "+++ b/a.py",
                        "@@ -0,0 +1 @@",
                        "+new",
                    ]
                )
            ),
        ):
            result = CompareNode(diff=SourceDelta(repository), llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "nodes": {
                        "hint": {"causes": [{"references": ["0-0-0"], "reason": "Old line."}]},
                        "blame": {
                            "candidates": [
                                {
                                    "commit": "candidate-sha",
                                    "parent": "parent-sha",
                                    "slices": [{"file": (None, "a.py"), "anchors": [{"base": (0, 0), "after": (1, 2)}]}],
                                }
                            ]
                        },
                    },
                }
            )

        self.assertEqual(result["nodes"]["compare"]["index"], 0)
        self.assertEqual(result["nodes"]["compare"]["candidate"]["commit"], "candidate-sha")
        self.assertEqual(result["nodes"]["compare"]["parent"]["commit"], "parent-sha")
        self.assertEqual(
            result["nodes"]["compare"]["candidate"]["presence"],
            {"passed": True, "reason": "candidate"},
        )
        self.assertEqual(
            result["nodes"]["compare"]["candidate"]["assessment"],
            {"passed": True, "reason": "candidate"},
        )
        self.assertEqual(result["nodes"]["compare"]["candidate"]["defect"], True)
        self.assertEqual(
            result["nodes"]["compare"]["parent"]["presence"],
            {"passed": False, "reason": "Code snippet is empty."},
        )
        self.assertEqual(
            result["nodes"]["compare"]["parent"]["assessment"],
            {"passed": False, "reason": "Code snippet is empty."},
        )
        self.assertEqual(result["nodes"]["compare"]["parent"]["defect"], False)
        self.assertEqual(presence.invoke.call_count, 1)
        self.assertEqual(assessment.invoke.call_count, 1)

    def test_select_node_sets_introducer_on_target_dichotomy(self) -> None:
        result = SelectNode().run(
            {
                "nodes": {
                    "compare": {
                        "candidate": {"commit": "sha-a", "defect": True},
                        "parent": {"commit": "sha-b", "defect": False},
                    }
                }
            }
        )

        self.assertEqual(result, {"introducer": "sha-a"})

    def test_select_node_requires_candidate_commit(self) -> None:
        self.assertEqual(
            SelectNode().run(
                {
                    "nodes": {
                        "compare": {
                            "candidate": {"commit": None, "defect": True},
                            "parent": {"commit": "sha-b", "defect": False},
                        }
                    }
                }
            ),
            {},
        )

    def test_select_node_leaves_state_when_no_match_exists(self) -> None:
        self.assertEqual(
            SelectNode().run(
                {
                    "nodes": {
                        "compare": {
                            "candidate": {"commit": "sha-a", "defect": False},
                            "parent": {"commit": "sha-b", "defect": False},
                        }
                    }
                }
            ),
            {},
        )

    def test_finalize_node_sets_success_state(self) -> None:
        self.assertEqual(FinalizeNode().run({}), {"aborted": False})

    def test_verify_node_checks_base_and_after(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.side_effect = [
            {"defect": True, "reason": "The snippet still contains the bug."},
            {"defect": False, "reason": "The snippet is corrected."},
        ]
        node = VerifyNode(llm=llm)

        result = node.run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "hint": {
                        "causes": [{"references": ["0-0-0"], "reason": "Old line."}],
                        "corrections": [
                            {"references": ["0-0-1"], "reason": "New line."}
                        ],
                    },
                    "refine": {
                        "refinement": "\n".join(
                            [
                                "diff --git a/a.py b/a.py",
                                "--- a/a.py",
                                "+++ b/a.py",
                                "@@ -1,2 +1,2 @@",
                                " old_context",
                                "-old",
                                "+new",
                            ]
                        )
                    },
                },
            }
        )

        llm.with_structured_output.assert_called_once_with(
            VerifyResponseSchema,
            method="json_schema",
            strict=True,
        )
        self.assertEqual(structured.invoke.call_count, 2)
        self.assertIn("the root cause of a bug", structured.invoke.call_args_list[0].args[0])
        self.assertIn("Bug explanations:", structured.invoke.call_args_list[0].args[0])
        self.assertIn("- Old line.", structured.invoke.call_args_list[0].args[0])
        self.assertNotIn("0-0-0", structured.invoke.call_args_list[0].args[0])
        self.assertNotIn("New line.", structured.invoke.call_args_list[0].args[0])
        self.assertIn("File: a.py", structured.invoke.call_args_list[0].args[0])
        self.assertIn("old_context", structured.invoke.call_args_list[0].args[0])
        self.assertIn("old", structured.invoke.call_args_list[0].args[0])
        self.assertNotIn("new", structured.invoke.call_args_list[0].args[0])
        self.assertIn("File: a.py", structured.invoke.call_args_list[1].args[0])
        self.assertIn("new", structured.invoke.call_args_list[1].args[0])
        self.assertNotIn("-old", structured.invoke.call_args_list[1].args[0])
        self.assertEqual(
            result["nodes"]["verify"],
            {
                "base": {
                    "defect": True,
                    "reason": "The snippet still contains the bug.",
                },
                "after": {
                    "defect": False,
                    "reason": "The snippet is corrected.",
                },
                "verified": True,
            },
        )

    def test_verify_node_rejects_malformed_refinement(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {"defect": True, "reason": "x"}
        with self.assertRaises(ValueError):
            VerifyNode(llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "nodes": {
                        "hint": {
                            "causes": [{"references": ["0-0-0"], "reason": "Old line."}],
                            "corrections": [{"references": ["0-0-1"], "reason": "New line."}],
                        },
                        "refine": {"refinement": 1},
                    },
                }
            )

    def test_verify_node_requires_inputs(self) -> None:
        with self.assertRaises(ValueError):
            VerifyNode().run({})

    def test_verify_node_returns_empty_state_for_empty_hint_or_refinement(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])

        result = VerifyNode(llm=llm).run(
            {
                "rationale": "Validation fails before normalization.",
                "nodes": {
                    "hint": {"causes": [], "corrections": []},
                    "refine": {"refinement": ""},
                },
            }
        )

        self.assertEqual(result["nodes"]["verify"], {})

    def test_verify_node_rejects_invalid_json(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.side_effect = ["bad", {"defect": False, "reason": "x"}]
        node = VerifyNode(llm=llm)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "rationale": "Validation fails before normalization.",
                    "nodes": {
                        "hint": {
                            "causes": [{"references": ["0-0-0"], "reason": "Old line."}],
                            "corrections": [{"references": ["0-0-1"], "reason": "New line."}],
                        },
                        "refine": {
                            "refinement": "\n".join(
                                [
                                    "diff --git a/a.py b/a.py",
                                    "--- a/a.py",
                                    "+++ b/a.py",
                                    "@@ -1 +1 @@",
                                    "-old",
                                    "+new",
                                ]
                            )
                        },
                    },
                }
            )

    def test_aggregate_node_sets_final_files_and_rationale(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="Final rationale.")
        node = AggregateNode(llm=llm)

        result = node.run({
            "summary": "Shared summary",
            "files": ["a.py", "b.py", "c.py"],
            "nodes": {
                "explain": {
                    "runs": [
                        {
                            "index": 0,
                            "rationale": "First rationale",
                            "files": ["a.py", "ghost.py", "b.py"],
                        },
                        {
                            "index": 1,
                            "rationale": "Second rationale",
                            "files": ["b.py", "c.py", "ghost.py"],
                        },
                    ]
                }
            },
        })

        self.assertEqual(result["files"], ["a.py", "b.py", "c.py"])
        self.assertEqual(result["rationale"], "Final rationale.")
        self.assertEqual(
            result["nodes"]["explain"]["aggregate"]["files"],
            ["a.py", "b.py", "c.py"],
        )
        self.assertEqual(
            result["nodes"]["explain"]["aggregate"]["rationale"],
            "Final rationale.",
        )
        self.assertNotIn("rationales", result["nodes"]["explain"]["aggregate"])
        llm.invoke.assert_called_once()
        self.assertIn("Summary:", llm.invoke.call_args.args[0])
        self.assertIn("Final related files:", llm.invoke.call_args.args[0])
        self.assertIn("Explain runs:", llm.invoke.call_args.args[0])
        self.assertIn("Output only the final rationale text.", llm.invoke.call_args.args[0])
        self.assertNotIn("choose files", llm.invoke.call_args.args[0])

    def test_aggregate_node_requires_llm(self) -> None:
        node = AggregateNode()

        with self.assertRaises(ValueError):
            node.run({
                "summary": "Shared summary",
                "files": ["a.py"],
                "nodes": {
                    "explain": {
                        "runs": [
                            {
                                "index": 0,
                                "rationale": "Rationale",
                                "files": ["a.py"],
                            }
                        ]
                    }
                },
            })

    def test_aggregate_node_rejects_empty_rationale_output(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="   ")
        node = AggregateNode(llm=llm)

        with self.assertRaises(ValueError):
            node.run({
                "summary": "Shared summary",
                "files": ["a.py"],
                "nodes": {
                    "explain": {
                        "runs": [
                            {
                                "index": 0,
                                "rationale": "Rationale",
                                "files": ["a.py"],
                            }
                        ]
                    }
                },
            })

    def test_state_docstrings_describe_nodes_structure(self) -> None:
        self.assertIsNotNone(AttributionGraphState.__doc__)
        self.assertIn("nodes", AttributionGraphState.__doc__)
        self.assertIsNotNone(ExplainState.__doc__)
        self.assertIsNotNone(AggregateState.__doc__)
        self.assertIsNotNone(HintState.__doc__)

    def test_trace_seed_and_sequence_are_injected(self) -> None:
        trace = Trace(seed="seed-123")
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"phase": "start"},
        )
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_end",
            payload={"phase": "end"},
        )

        events = list(trace.events())
        self.assertEqual(events[0]["trace"], "seed-123")
        self.assertEqual(events[1]["trace"], "seed-123")
        self.assertEqual(events[0]["sequence"], 1)
        self.assertEqual(events[1]["sequence"], 2)
        self.assertIsInstance(events[0]["payload"], str)
        self.assertEqual(json.loads(events[0]["payload"])["phase"], "start")

    def test_trace_json_default_stringifies_non_serializable_objects(self) -> None:
        trace = Trace(seed="json-default")
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"value": object()},
        )

        payload = json.loads(list(trace.events())[0]["payload"])
        self.assertIsInstance(payload["value"], str)

    def test_callback_handler_emits_node_events(self) -> None:
        trace = Trace(seed="node-trace")
        handler = TracingCallbackHandler(trace)

        handler.on_chain_start(
            {"name": "summarize"},
            {"correction": "value"},
            run_id="node-1",
        )
        handler.on_chain_end(
            {"summary": "ok"},
            run_id="node-1",
        )

        events = list(trace.events())
        self.assertEqual([event["event"] for event in events], ["node_start", "node_end"])
        self.assertEqual(events[0]["span"], events[1]["span"])
        self.assertIsNone(events[0]["parent"])
        self.assertIsNone(events[1]["parent"])
        self.assertEqual(events[0]["node"], "summarize")
        self.assertEqual(events[1]["node"], "summarize")

    def test_callback_handler_rejects_missing_name(self) -> None:
        trace = Trace(seed="node-trace-missing-name")
        handler = TracingCallbackHandler(trace)

        with self.assertRaises(ValueError):
            handler.on_chain_start(
                None,
                {"correction": "value"},
                run_id="node-1",
            )

    def test_callback_handler_uses_kwargs_name_fallback(self) -> None:
        trace = Trace(seed="node-trace-kwargs-name")
        handler = TracingCallbackHandler(trace)

        handler.on_chain_start(
            None,
            {"correction": "value"},
            run_id="node-1",
            name=" summarize ",
        )
        handler.on_chain_end(
            {"summary": "ok"},
            run_id="node-1",
        )

        events = list(trace.events())
        self.assertEqual([event["event"] for event in events], ["node_start", "node_end"])
        self.assertEqual(events[0]["node"], "summarize")
        self.assertEqual(events[1]["node"], "summarize")

    def test_callback_handler_emits_llm_events(self) -> None:
        trace = Trace(seed="llm-trace")
        handler = TracingCallbackHandler(trace)
        handler.on_chain_start(
            {"name": "summarize"},
            {"correction": "value"},
            run_id="node-1",
        )
        handler.on_chat_model_start(
            {"name": "ChatOpenAI"},
            [[HumanMessage(content="prompt")]],
            run_id="llm-1",
            parent_run_id="node-1",
            invocation_params={"model": "fake"},
        )
        handler.on_llm_end(
            LLMResult(
                generations=[[
                    ChatGeneration(
                        text="hello",
                        message=AIMessage(
                            content="hello",
                            response_metadata={"logprobs": {"token_logprobs": [-0.1]}},
                        ),
                        generation_info={"finish_reason": "stop"},
                    )
                ]],
                llm_output={"token_usage": {"total_tokens": 2}},
            ),
            run_id="llm-1",
            parent_run_id="node-1",
        )

        events = list(trace.events())
        self.assertEqual(
            [event["event"] for event in events],
            ["node_start", "llm_start", "llm_end"],
        )
        self.assertEqual(events[1]["parent"], "node-1")
        self.assertEqual(events[2]["parent"], "node-1")
        self.assertEqual(events[1]["span"], events[2]["span"])
        self.assertEqual(events[1]["node"], "summarize")
        self.assertEqual(events[2]["node"], "summarize")

        start_payload = json.loads(events[1]["payload"])
        self.assertIn("messages", start_payload)
        self.assertEqual(
            start_payload["messages"],
            [[
                {
                    "type": "human",
                    "content": "prompt",
                    "name": None,
                    "additional_kwargs": {},
                    "response_metadata": {},
                }
            ]],
        )
        end_payload = json.loads(events[2]["payload"])
        self.assertEqual(end_payload["llm_output"]["token_usage"]["total_tokens"], 2)
        self.assertIn("llm_output_keys", end_payload)
        self.assertIn("generation_count", end_payload)
        self.assertEqual(end_payload["generations"][0][0]["text"], "hello")
        self.assertEqual(
            end_payload["generations"][0][0]["message"]["content"],
            "hello",
        )
        self.assertEqual(
            end_payload["generations"][0][0]["generation_info"]["finish_reason"],
            "stop",
        )
        self.assertEqual(end_payload["logprobs"]["token_logprobs"], [-0.1])

    def test_invoke_with_trace_returns_trace(self) -> None:
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"summary": "ok", "aborted": False}
        runnable = AttributionRunnable(compiled)

        result, trace = runnable.invoke_with_trace(
            {"correction": "value"},
            seed="seed-xyz",
        )

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(trace.id, "seed-xyz")
        self.assertEqual(compiled.invoke.call_args.args[0], {"correction": "value"})
        self.assertIn("config", compiled.invoke.call_args.kwargs)
        callbacks = compiled.invoke.call_args.kwargs["config"]["callbacks"]
        self.assertEqual(len(callbacks), 1)
        self.assertIsInstance(callbacks[0], TracingCallbackHandler)

    def test_invoke_without_trace_preserves_kwargs(self) -> None:
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"summary": "ok", "aborted": False}
        runnable = AttributionRunnable(compiled)

        result = runnable.invoke(
            {"correction": "value"},
            config={"tags": ["demo"]},
        )

        self.assertEqual(result["summary"], "ok")
        compiled.invoke.assert_called_once_with(
            {"correction": "value"},
            config={"tags": ["demo"]},
        )

    def test_trace_to_table_and_result_table(self) -> None:
        trace = Trace(seed="tabular")
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"phase": "start"},
        )

        trace_table = trace.to_table()
        self.assertEqual(
            trace_table.column_names,
            ["trace", "sequence", "timestamp", "node", "span", "parent", "event", "payload"],
        )

        result_table = ResultTable.from_result({
            "summary": "ok",
            "introducer": None,
            "aborted": False,
        })
        self.assertEqual(
            result_table.column_names,
            ["summary", "introducer", "aborted", "state_json"],
        )
