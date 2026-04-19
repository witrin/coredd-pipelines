from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from benchmark.attribution.diff.reference import PatchFactory
from benchmark.attribution.domain import (
    Anchor,
    Change,
    Commit,
    FileOperation,
    OperationType,
    Slice,
)
from benchmark.attribution.graph import (
    AttributionGraph,
    AttributionGraphBuilder,
    AttributionResult,
)
from benchmark.attribution.graph import AgenticSZZGraphBuilder
from benchmark.attribution.graph import AgenticSZZGraph
from benchmark.attribution.graph import AgentSZZGraphBuilder
from benchmark.attribution.graph import AgentSZZGraph
from benchmark.attribution.graph import LLM4SZZGraphBuilder
from benchmark.attribution.graph import LLM4SZZGraph
from benchmark.attribution.history import VersionControl
from benchmark.attribution.reason import (
    AggregateNode,
    BlameNode,
    CompareNode,
    ExpandNode,
    ExplainNode,
    ExplainResponseSchema,
    FinalizeNode,
    HintResponseSchema,
    HintNode,
    RefineNode,
    SelectNode,
    SummarizeNode,
    VerifyNode,
    VerifyResponseSchema,
)
from unidiff import PatchSet


class TestLLM4SZZGraph(unittest.TestCase):
    def _graph(self) -> LLM4SZZGraph:
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        return LLM4SZZGraph(
            summarize=SummarizeNode(),
            explain=ExplainNode(),
            aggregate=AggregateNode(),
            expand=ExpandNode(),
            hint=HintNode(factory=factory),
            refine=RefineNode(factory=factory),
            verify=VerifyNode(),
            blame=BlameNode(factory=factory),
            compare=CompareNode(factory=factory),
        )

    def _correction(self) -> Change:
        return Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )

    def _hint_factory(
        self,
        values: dict[tuple[str, str | None], bytes | None] | None = None,
    ) -> Mock:
        factory = Mock(spec_set=["read"])
        store = values or {
            ("base-sha", "a.py"): b"import old\nimport context\n",
            ("after-sha", "a.py"): b"import new\nimport context\n",
            ("base-sha", "b.py"): b"import old_b\n",
            ("after-sha", "b.py"): b"import new_b\n",
        }
        factory.read.side_effect = lambda commit, path: store.get((commit, path))
        return factory

    def test_import_smoke(self) -> None:
        self.assertIsNotNone(LLM4SZZGraph)
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

    def test_graph_nodes(self) -> None:
        with self.assertRaises(ValueError):
            LLM4SZZGraph()

    def test_graph_interface(self) -> None:
        self.assertIsInstance(self._graph(), AttributionGraph)

    def test_builder_interface(self) -> None:
        builder = LLM4SZZGraphBuilder(
            summarize=Mock(),
            explain=Mock(),
            aggregate=Mock(),
            expand=Mock(),
            hint=Mock(),
            refine=Mock(),
            verify=Mock(),
        )

        self.assertIsInstance(builder, AttributionGraphBuilder)

    def test_builder_nodes(self) -> None:
        with self.assertRaises(ValueError):
            LLM4SZZGraphBuilder()

    def test_builder_graph(self) -> None:
        repository = object.__new__(VersionControl)
        summarize = object()
        explain = object()
        aggregate = object()
        expand = object()
        hint = object()
        refine = object()
        verify = object()
        blame = object()
        compare = object()
        select = object()
        abort = object()
        finalize = object()
        summarize_builder = Mock(return_value=summarize)
        explain_builder = Mock(return_value=explain)
        aggregate_builder = Mock(return_value=aggregate)
        expand_builder = Mock(return_value=expand)
        hint_builder = Mock(return_value=hint)
        refine_builder = Mock(return_value=refine)
        verify_builder = Mock(return_value=verify)
        blame_builder = Mock(return_value=blame)
        compare_builder = Mock(return_value=compare)
        select_builder = Mock(return_value=select)
        abort_builder = Mock(return_value=abort)
        finalize_builder = Mock(return_value=finalize)
        builder = LLM4SZZGraphBuilder(
            summarize=summarize_builder,
            explain=explain_builder,
            aggregate=aggregate_builder,
            expand=expand_builder,
            hint=hint_builder,
            refine=refine_builder,
            verify=verify_builder,
            blame=blame_builder,
            compare=compare_builder,
            select=select_builder,
            abort=abort_builder,
            finalize=finalize_builder,
        )

        graph = builder.create(repository)

        self.assertIsInstance(graph, LLM4SZZGraph)
        for factory in (
            summarize_builder,
            explain_builder,
            aggregate_builder,
            expand_builder,
            hint_builder,
            refine_builder,
            verify_builder,
            blame_builder,
            compare_builder,
            select_builder,
            abort_builder,
            finalize_builder,
        ):
            factory.assert_called_once_with(repository)

    def test_invoke(self) -> None:
        graph = self._graph()
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"introducer": "abc123", "aborted": True}
        graph.build = Mock(return_value=compiled)

        result = graph.invoke(self._correction())

        compiled.invoke.assert_called_once_with(
            {"correction": self._correction(), "nodes": {}},
            trace=None,
        )
        self.assertEqual(
            result,
            {"introducer": "abc123", "aborted": True},
        )

    def test_route_verify_abort(self) -> None:
        self.assertEqual(
            self._graph().route_verify({"nodes": {"verify": {"verified": False}}}),
            "abort",
        )

    def test_route_verify_pass(self) -> None:
        self.assertEqual(
            self._graph().route_verify({"nodes": {"verify": {"verified": True}}}),
            "blame",
        )

    def test_route_select_finalize(self) -> None:
        self.assertEqual(
            self._graph().route_select({"introducer": "abc"}),
            "finalize",
        )

    def test_route_select_loop(self) -> None:
        self.assertEqual(
            self._graph().route_select(
                {
                    "nodes": {
                        "blame": {"candidates": [{"commit": "a"}, {"commit": "b"}]},
                        "compare": {"index": 0},
                    },
                }
            ),
            "compare",
        )

    def test_route_select_abort(self) -> None:
        self.assertEqual(
            self._graph().route_select(
                {
                    "nodes": {
                        "blame": {"candidates": [{"commit": "a"}]},
                        "compare": {"index": 0},
                    },
                }
            ),
            "abort",
        )

    def test_route_blame_loop(self) -> None:
        self.assertEqual(
            self._graph().route_blame(
                {"nodes": {"blame": {"candidates": [{"commit": "a"}]}}}
            ),
            "compare",
        )

    def test_route_blame_abort(self) -> None:
        self.assertEqual(
            self._graph().route_blame({"nodes": {"blame": {"candidates": []}}}),
            "abort",
        )

    def test_route_explain_loop(self) -> None:
        self.assertEqual(
            self._graph().route_explain(
                {
                    "files": ["a.py", "b.py"],
                    "nodes": {"explain": {"samples": 2, "runs": [{"index": 0}]}},
                }
            ),
            "explain",
        )

    def test_route_explain_aggregate(self) -> None:
        self.assertEqual(
            self._graph().route_explain(
                {
                    "files": ["a.py", "b.py"],
                    "nodes": {
                        "explain": {
                            "samples": 2,
                            "runs": [{"index": 0}, {"index": 1}],
                        }
                    },
                }
            ),
            "aggregate",
        )

    def test_route_explain_shorten(self) -> None:
        graph = LLM4SZZGraph(
            summarize=SummarizeNode(),
            explain=ExplainNode(),
            aggregate=AggregateNode(),
            expand=ExpandNode(),
            hint=HintNode(factory=PatchFactory(object.__new__(VersionControl))),
            refine=RefineNode(factory=PatchFactory(object.__new__(VersionControl))),
            verify=VerifyNode(),
            blame=BlameNode(factory=PatchFactory(object.__new__(VersionControl))),
            compare=CompareNode(factory=PatchFactory(object.__new__(VersionControl))),
        )

        self.assertEqual(
            graph.route_explain(
                {
                    "files": ["a.py", "b.py"],
                    "nodes": {
                        "explain": {
                            "samples": 3,
                            "runs": [{"index": 0}, {"index": 1}],
                        }
                    },
                }
            ),
            "aggregate",
        )

    @patch.object(PatchFactory, "create", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_summarize(
        self,
        manifest: Mock,
        delta_patch: Mock,
    ) -> None:
        operations = [
            FileOperation(base="old.py", after="new.py", operation=OperationType.EDIT)
        ]
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="Fixes the bug.")
        node = SummarizeNode(
            factory=factory,
            llm=llm,
            context=2,
        )
        manifest.return_value = operations
        delta_patch.return_value = "@@ -1 +1 @@\n-a\n+b\n"

        result = node.run(
            {
                "correction": Change(
                    number=1,
                    base="base-sha",
                    after="after-sha",
                    description="Fix bug",
                )
            }
        )

        manifest.assert_called_once_with(
            repository,
            "base-sha",
            "after-sha",
        )
        delta_patch.assert_called_once_with(
            factory,
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

    @patch.object(PatchFactory, "create", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_summarize_empty(
        self,
        manifest: Mock,
        delta_patch: Mock,
    ) -> None:
        operations = [
            FileOperation(base="old.py", after="new.py", operation=OperationType.EDIT),
            FileOperation(
                base="only-old.py", after=None, operation=OperationType.DELETE
            ),
        ]
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        llm = Mock(spec_set=["invoke"])
        node = SummarizeNode(
            factory=factory,
            llm=llm,
        )
        manifest.return_value = operations
        delta_patch.return_value = "   "

        result = node.run(
            {
                "correction": Change(
                    number=1,
                    base="base-sha",
                    after="after-sha",
                    description="Fix bug",
                )
            }
        )

        manifest.assert_called_once_with(
            repository,
            "base-sha",
            "after-sha",
        )
        delta_patch.assert_called_once_with(
            factory,
            "base-sha",
            "after-sha",
            files=operations,
            n=3,
        )
        llm.invoke.assert_not_called()
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["files"], ["new.py", "only-old.py"])

    @patch.object(PatchFactory, "create", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_summarize_zero_context(
        self,
        manifest: Mock,
        delta_patch: Mock,
    ) -> None:
        operations = [
            FileOperation(base="old.py", after="new.py", operation=OperationType.EDIT)
        ]
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="Summary")
        node = SummarizeNode(
            factory=factory,
            llm=llm,
            context=0,
        )
        manifest.return_value = operations
        delta_patch.return_value = "@@ -1 +1 @@\n-a\n+b\n"

        node.run(
            {
                "correction": Change(
                    number=1,
                    base="base-sha",
                    after="after-sha",
                    description="Fix bug",
                )
            }
        )

        self.assertIn(
            "Each hunk contains changed lines only and no unchanged context.",
            llm.invoke.call_args.args[0],
        )

    def test_summarize_negative(self) -> None:
        with self.assertRaises(ValueError):
            SummarizeNode(context=-1)

    @patch.object(PatchFactory, "create", autospec=True)
    @patch.object(VersionControl, "meta", autospec=True)
    @patch.object(VersionControl, "log", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    def test_explain(
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
        factory = PatchFactory(repository)
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "rationale": "The validation logic and normalization changed together.",
            "files": ["c.py", "missing.py", "a.py", "c.py"],
        }
        node = ExplainNode(
            factory=factory,
            llm=llm,
            context=2,
        )
        manifest.return_value = operations
        log.return_value = ["commit-1", "after-sha"]
        meta.side_effect = [
            Commit(
                "after-sha",
                "Merge commit message",
                datetime(2024, 1, 2, tzinfo=timezone.utc),
                "a",
            ),
            Commit(
                "commit-1",
                "Intermediate commit message",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                "a",
            ),
            Commit(
                "after-sha",
                "Merge commit message",
                datetime(2024, 1, 2, tzinfo=timezone.utc),
                "a",
            ),
            Commit(
                "commit-1",
                "Intermediate commit message",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                "a",
            ),
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
        second = node.run(
            {
                **state,
                "runs": first["runs"],
                "samples": first["samples"],
            }
        )

        self.assertEqual(log.call_count, 2)
        self.assertEqual(len(first["runs"]), 1)
        self.assertEqual(first["samples"], 3)
        self.assertNotIn("files", first)
        self.assertNotIn("rationale", first)
        self.assertEqual(
            first["runs"][0]["files"],
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
        self.assertIn(
            "Pull request description:", structured.invoke.call_args_list[0].args[0]
        )
        self.assertIn("Fix the parser", structured.invoke.call_args_list[0].args[0])
        self.assertIn(
            "Commit messages in (base, after]:",
            structured.invoke.call_args_list[0].args[0],
        )
        self.assertIn(
            "Merge commit message", structured.invoke.call_args_list[0].args[0]
        )
        self.assertIn(
            "Intermediate commit message", structured.invoke.call_args_list[0].args[0]
        )
        self.assertIn(
            "Respond only in JSON.", structured.invoke.call_args_list[0].args[0]
        )
        self.assertIn(
            'Use this schema: {"rationale": string, "files": [string, ...]}.',
            structured.invoke.call_args_list[0].args[0],
        )
        self.assertEqual(len(second["runs"]), 2)
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

    def test_explain_json(self) -> None:
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = "not json"
        node = ExplainNode(factory=factory, llm=llm)

        with (
            patch.object(
                VersionControl,
                "manifest",
                autospec=True,
                return_value=[
                    FileOperation(
                        base="a.py", after="a.py", operation=OperationType.EDIT
                    )
                ],
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
            patch.object(PatchFactory, "create", autospec=True, return_value="a.py"),
        ):
            with self.assertRaises(ValueError):
                node.run(
                    {
                        "correction": Change(
                            number=1,
                            base="base-sha",
                            after="after-sha",
                            description="Fix the parser",
                        ),
                        "summary": "Summary",
                    }
                )

    def test_explain_order(self) -> None:
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "rationale": "Rationale",
            "files": ["a.py"],
        }
        node = ExplainNode(factory=factory, llm=llm, samples=3)
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
                    FileOperation(
                        base="a.py", after="a.py", operation=OperationType.EDIT
                    ),
                    FileOperation(
                        base="b.py", after="b.py", operation=OperationType.EDIT
                    ),
                ],
            ),
            patch.object(VersionControl, "log", autospec=True, return_value=[]),
            patch.object(
                VersionControl,
                "meta",
                autospec=True,
                side_effect=[
                    Commit(
                        "after-sha", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"
                    ),
                    Commit(
                        "after-sha", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"
                    ),
                ],
            ),
            patch.object(
                PatchFactory,
                "create",
                autospec=True,
                side_effect=["a.py\nb.py", "b.py\na.py"],
            ) as delta_patch,
        ):
            first = node.run(state)
            second = node.run(
                {
                    **state,
                    "runs": first["runs"],
                    "samples": first["samples"],
                }
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
        self.assertEqual(len(second["runs"]), 2)
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

    def test_explain_context(self) -> None:
        with self.assertRaises(ValueError):
            ExplainNode(context=-1)

    def test_explain_samples(self) -> None:
        with self.assertRaises(ValueError):
            ExplainNode(samples=0)

    def test_expand_level(self) -> None:
        with self.assertRaises(ValueError):
            ExpandNode(level=0)

    def test_expand_context(self) -> None:
        with self.assertRaises(ValueError):
            ExpandNode(context=-1)

    def test_expand(self) -> None:
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        node = ExpandNode(factory=factory, level=2, context=1)

        with (
            patch.object(
                VersionControl,
                "manifest",
                autospec=True,
                return_value=[
                    FileOperation(
                        base="a.py", after="a.py", operation=OperationType.EDIT
                    ),
                    FileOperation(
                        base="b.py", after="b.py", operation=OperationType.EDIT
                    ),
                    FileOperation(
                        base="c.py", after="c.py", operation=OperationType.EDIT
                    ),
                ],
            ) as manifest,
            patch.object(
                PatchFactory, "expand", autospec=True, return_value="expanded"
            ) as expand,
        ):
            result = node.run(
                {
                    "correction": Change(
                        number=1,
                        base="base-sha",
                        after="after-sha",
                        description="Fix the parser",
                    ),
                    "files": ["c.py", "a.py"],
                }
            )

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
        self.assertEqual(result["expansion"], "expanded")
        self.assertNotIn("files", result)

    def test_hint_inputs(self) -> None:
        node = HintNode()

        with self.assertRaises(ValueError):
            node.run({})

    def test_hint_empty(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])

        with self.assertRaises(ValueError):
            HintNode(factory=self._hint_factory(), llm=llm).run(
                {
                    "rationale": "",
                    "expansion": "",
                }
            )

    def test_hint_prompt(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [{"references": ["0-0-0"], "reason": "Old line."}],
            "corrections": [{"references": ["0-0-1"], "reason": "New line."}],
        }
        node = HintNode(factory=self._hint_factory(), llm=llm)

        node.run(
            {
                "correction": self._correction(),
                "rationale": "Validation fails before normalization.",
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
                ),
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
        self.assertIn("Put defect lines only in `causes`.", prompt)
        self.assertIn("Put fix lines only in `corrections`.", prompt)
        self.assertIn("Do not place fix lines in `causes`.", prompt)
        self.assertIn("Do not place defect lines in `corrections`.", prompt)
        self.assertNotIn("1 -old", prompt)

    def test_hint(self) -> None:
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
                    "references": ["0-0-1"],
                    "reason": "The new validation logic fixes it.",
                }
            ],
        }
        node = HintNode(factory=self._hint_factory(), llm=llm)

        result = node.run(
            {
                "correction": self._correction(),
                "rationale": "Validation fails before normalization.",
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
                ),
            }
        )

        self.assertEqual(
            result["causes"],
            [
                {
                    "reason": "The old validation logic fails.",
                    "statements": [
                        {
                            "reference": "0-0-0",
                            "scope": "scope:program",
                            "excerpt": "import old",
                        },
                        {
                            "reference": "0-0-2",
                            "scope": "scope:program",
                            "excerpt": "import context",
                        },
                    ],
                }
            ],
        )
        self.assertEqual(
            result["corrections"],
            [
                {
                    "reason": "The new validation logic fixes it.",
                    "statements": [
                        {
                            "reference": "0-0-1",
                            "scope": "scope:program",
                            "excerpt": "import new",
                        }
                    ],
                },
            ],
        )
        self.assertNotIn("cause", result)
        self.assertNotIn("correction", result)
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

    def test_hint_json(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = "not json"
        node = HintNode(factory=self._hint_factory(), llm=llm)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "correction": self._correction(),
                    "rationale": "Validation fails before normalization.",
                    "expansion": "\n".join(
                        [
                            "diff --git a/a.py b/a.py",
                            "--- a/a.py",
                            "+++ b/a.py",
                            "@@ -1 +1 @@",
                            "-old",
                        ]
                    ),
                }
            )

    def test_hint_reference(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [{"references": ["9-9-9"], "reason": "Bad reference."}],
            "corrections": [],
        }
        node = HintNode(factory=self._hint_factory(), llm=llm)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "correction": self._correction(),
                    "rationale": "Validation fails before normalization.",
                    "expansion": "\n".join(
                        [
                            "diff --git a/a.py b/a.py",
                            "--- a/a.py",
                            "+++ b/a.py",
                            "@@ -1 +1 @@",
                            "-old",
                            "+new",
                        ]
                    ),
                }
            )

    def test_hint_fix_side(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [{"references": ["0-0-1"], "reason": "Bad cause projection."}],
            "corrections": [],
        }
        node = HintNode(factory=self._hint_factory(), llm=llm)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "correction": self._correction(),
                    "rationale": "Validation fails before normalization.",
                    "expansion": "\n".join(
                        [
                            "diff --git a/a.py b/a.py",
                            "--- a/a.py",
                            "+++ b/a.py",
                            "@@ -1 +1 @@",
                            "-old",
                            "+new",
                        ]
                    ),
                }
            )

    def test_hint_scope(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {
            "causes": [{"references": ["0-0-0"], "reason": "Derived from reference."}],
            "corrections": [{"references": ["0-0-1"], "reason": "Derived from fix."}],
        }
        node = HintNode(factory=self._hint_factory(), llm=llm)

        result = node.run(
            {
                "correction": self._correction(),
                "rationale": "Validation fails before normalization.",
                "expansion": "\n".join(
                    [
                        "diff --git a/a.py b/a.py",
                        "--- a/a.py",
                        "+++ b/a.py",
                        "@@ -1 +1 @@",
                        "-old",
                        "+new",
                    ]
                ),
            }
        )

        self.assertEqual(
            result["causes"],
            [
                {
                    "reason": "Derived from reference.",
                    "statements": [
                        {
                            "reference": "0-0-0",
                            "scope": "scope:program",
                            "excerpt": "import old",
                        }
                    ],
                }
            ],
        )
        self.assertEqual(
            result["corrections"],
            [
                {
                    "reason": "Derived from fix.",
                    "statements": [
                        {
                            "reference": "0-0-1",
                            "scope": "scope:program",
                            "excerpt": "import new",
                        }
                    ],
                }
            ],
        )

    def test_hint_files(self) -> None:
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
            "corrections": [
                {
                    "references": ["0-0-1", "1-0-1"],
                    "reason": "Two fixes are involved.",
                }
            ],
        }
        node = HintNode(factory=self._hint_factory(), llm=llm)

        result = node.run(
            {
                "correction": self._correction(),
                "rationale": "Validation fails before normalization.",
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
                        "@@ -1 +1 @@",
                        "-old_b",
                        "+new_b",
                    ]
                ),
            }
        )

        self.assertEqual(
            result["causes"],
            [
                {
                    "reason": "Two files are involved.",
                    "statements": [
                        {
                            "reference": "0-0-0",
                            "scope": "scope:program",
                            "excerpt": "import old",
                        },
                        {
                            "reference": "1-0-0",
                            "scope": "scope:program",
                            "excerpt": "import old_b",
                        },
                    ],
                },
            ],
        )
        self.assertEqual(
            result["corrections"],
            [
                {
                    "reason": "Two fixes are involved.",
                    "statements": [
                        {
                            "reference": "0-0-1",
                            "scope": "scope:program",
                            "excerpt": "import new",
                        },
                        {
                            "reference": "1-0-1",
                            "scope": "scope:program",
                            "excerpt": "import new_b",
                        },
                    ],
                },
            ],
        )

    def test_refine(self) -> None:
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        node = RefineNode(factory=factory)

        with patch.object(
            PatchFactory,
            "refine",
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
                    "expansion": "\n".join(
                        [
                            "diff --git a/a.py b/a.py",
                            "--- a/a.py",
                            "+++ b/a.py",
                            "@@ -1 +1 @@",
                            "-old",
                            "+new",
                        ]
                    ),
                    "causes": [
                        {
                            "reason": "Bug line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": "scope:program",
                                    "excerpt": "import old",
                                }
                            ],
                        },
                        {
                            "reason": "Also selected.",
                            "statements": [
                                {
                                    "reference": "0-0-1",
                                    "scope": None,
                                    "excerpt": None,
                                }
                            ],
                        },
                    ],
                }
            )

        materialize.assert_called_once()
        self.assertEqual(
            materialize.call_args.args[1:5],
            (
                "base-sha",
                "after-sha",
                [
                    Slice(
                        file=("a.py", "a.py"),
                        anchors=[Anchor(base=(1, 2), after=(0, 0))],
                    )
                ],
                "base",
            ),
        )
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
        self.assertIn("--- a/a.py", result["refinement"])

    def test_refine_inputs(self) -> None:
        with self.assertRaises(ValueError):
            RefineNode().run({})

    def test_refine_empty(self) -> None:
        repository = object.__new__(VersionControl)
        factory = PatchFactory(repository)
        node = RefineNode(factory=factory)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "correction": Change(
                        number=1,
                        base="base-sha",
                        after="after-sha",
                        description="Fix parser",
                    ),
                    "expansion": "\n".join(
                        [
                            "diff --git a/a.py b/a.py",
                            "--- a/a.py",
                            "+++ b/a.py",
                            "@@ -1 +1 @@",
                            "-old",
                            "+new",
                        ]
                    ),
                    "causes": [],
                }
            )

    def test_blame(self) -> None:
        repository = object.__new__(VersionControl)
        node = BlameNode(factory=PatchFactory(repository))

        with (
            patch.object(
                VersionControl,
                "blame",
                autospec=True,
                side_effect=[
                    [
                        ("sha-old", "a.py", (2, 2)),
                        ("sha-mid", "a.py", (3, 3)),
                    ],
                    [
                        ("sha-new", "a.py", (10, 10)),
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
                    Commit(
                        "sha-old", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"
                    ),
                    Commit(
                        "sha-mid", "", datetime(2024, 2, 1, tzinfo=timezone.utc), "a"
                    ),
                    Commit(
                        "sha-new", "", datetime(2024, 3, 1, tzinfo=timezone.utc), "a"
                    ),
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
                    ),
                    "causes": [
                        {
                            "reason": "Bug lines.",
                            "statements": [
                                {
                                    "reference": "0-0-1",
                                    "scope": None,
                                    "excerpt": None,
                                },
                                {
                                    "reference": "0-0-2",
                                    "scope": None,
                                    "excerpt": None,
                                },
                                {
                                    "reference": "1-0-1",
                                    "scope": None,
                                    "excerpt": None,
                                },
                            ],
                        }
                    ],
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
            result["candidates"],
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

    def test_blame_segmented_interval(self) -> None:
        repository = object.__new__(VersionControl)
        node = BlameNode(factory=PatchFactory(repository))

        with (
            patch(
                "benchmark.attribution.reason.PatchView.resolve",
                return_value=[
                    Slice(
                        file=("a.py", "a.py"),
                        anchors=[Anchor(base=(2, 4), after=(1, 2))],
                    )
                ],
            ),
            patch.object(
                VersionControl,
                "blame",
                autospec=True,
                return_value=[
                    ("sha-old", "a.py", (2, 2)),
                    ("sha-mid", "a.py", (3, 3)),
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
                    Commit(
                        "sha-old", "", datetime(2024, 1, 1, tzinfo=timezone.utc), "a"
                    ),
                    Commit(
                        "sha-mid", "", datetime(2024, 2, 1, tzinfo=timezone.utc), "a"
                    ),
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
                    "expansion": "\n".join(
                        [
                            "diff --git a/a.py b/a.py",
                            "--- a/a.py",
                            "+++ b/a.py",
                            "@@ -1,3 +1,1 @@",
                            " keep",
                            "-old_a1",
                            "-old_a2",
                        ]
                    ),
                    "causes": [
                        {
                            "reason": "Bug lines.",
                            "statements": [
                                {
                                    "reference": "0-0-1",
                                    "scope": None,
                                    "excerpt": None,
                                }
                            ],
                        }
                    ],
                }
            )

        self.assertEqual(
            [call.args[1:] for call in blame.call_args_list],
            [
                ("a.py", "base-sha", [(2, 3)]),
            ],
        )
        self.assertEqual(
            result["candidates"],
            [
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

    def test_blame_inputs(self) -> None:
        with self.assertRaises(ValueError):
            BlameNode().run({})

    def test_compare(self) -> None:
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
        node = CompareNode(factory=PatchFactory(repository), llm=llm)

        with patch.object(
            PatchFactory,
            "refine",
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
                    "causes": [
                        {
                            "reason": "Old line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": "scope:program",
                                    "excerpt": "import old",
                                }
                            ],
                        }
                    ],
                    "corrections": [
                        {
                            "reason": "Fix line.",
                            "statements": [
                                {
                                    "reference": "0-0-1",
                                    "scope": "scope:program",
                                    "excerpt": "import fix_only",
                                }
                            ],
                        }
                    ],
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
                    ],
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
        self.assertIn("Defect statements:", presence.invoke.call_args_list[0].args[0])
        self.assertIn(
            "- scope:program import old", presence.invoke.call_args_list[0].args[0]
        )
        self.assertNotIn("import fix_only", presence.invoke.call_args_list[0].args[0])
        self.assertNotIn("Fix line.", presence.invoke.call_args_list[0].args[0])
        self.assertIn("File: a.py", presence.invoke.call_args_list[0].args[0])
        self.assertIn("old", presence.invoke.call_args_list[1].args[0])
        self.assertIn("new", presence.invoke.call_args_list[0].args[0])
        self.assertIn("Root cause:", assessment.invoke.call_args_list[0].args[0])
        self.assertIn("Defect statements:", assessment.invoke.call_args_list[0].args[0])
        self.assertIn(
            "- scope:program import old", assessment.invoke.call_args_list[0].args[0]
        )
        self.assertNotIn("import fix_only", assessment.invoke.call_args_list[0].args[0])
        self.assertNotIn("Fix line.", assessment.invoke.call_args_list[0].args[0])
        self.assertEqual(
            result,
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

    def test_compare_deduplicate(self) -> None:
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

        with patch.object(
            PatchFactory,
            "refine",
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
        ):
            CompareNode(factory=PatchFactory(repository), llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "causes": [
                        {
                            "reason": "Old line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": "scope:program",
                                    "excerpt": "import old",
                                }
                            ],
                        },
                        {
                            "reason": "Old line again.",
                            "statements": [
                                {
                                    "reference": "0-0-1",
                                    "scope": "scope:program",
                                    "excerpt": "import old",
                                }
                            ],
                        },
                    ],
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
                    ],
                }
            )

        self.assertEqual(
            presence.invoke.call_args_list[0]
            .args[0]
            .count("- scope:program import old"),
            1,
        )
        self.assertEqual(
            assessment.invoke.call_args_list[0]
            .args[0]
            .count("- scope:program import old"),
            1,
        )

    def test_compare_advance(self) -> None:
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
            PatchFactory,
            "refine",
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
            result = CompareNode(factory=PatchFactory(repository), llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "causes": [
                        {
                            "reason": "Old line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": "scope:program",
                                    "excerpt": "import old",
                                }
                            ],
                        }
                    ],
                    "candidates": [
                        {
                            "commit": "first",
                            "parent": "base-1",
                            "slices": [
                                {
                                    "file": ("a.py", "a.py"),
                                    "anchors": [{"base": (1, 2), "after": (1, 2)}],
                                }
                            ],
                        },
                        {
                            "commit": "second",
                            "parent": "base-2",
                            "slices": [
                                {
                                    "file": ("b.py", "b.py"),
                                    "anchors": [{"base": (2, 3), "after": (2, 3)}],
                                }
                            ],
                        },
                    ],
                    "index": 0,
                }
            )

        self.assertEqual(result["index"], 1)
        self.assertEqual(result["candidate"]["commit"], "second")
        self.assertEqual(result["parent"]["commit"], "base-2")

    def test_compare_inputs(self) -> None:
        with self.assertRaises(ValueError):
            CompareNode().run({})

    def test_compare_empty(self) -> None:
        repository = object.__new__(VersionControl)
        llm = Mock(spec_set=["with_structured_output"])

        with self.assertRaises(ValueError):
            CompareNode(factory=PatchFactory(repository), llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "causes": [
                        {
                            "reason": "Old line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": "scope:program",
                                    "excerpt": "import old",
                                }
                            ],
                        }
                    ],
                    "candidates": [],
                }
            )

    def test_compare_one_sided(self) -> None:
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
            PatchFactory,
            "refine",
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
            result = CompareNode(factory=PatchFactory(repository), llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "causes": [
                        {
                            "reason": "Old line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": "scope:program",
                                    "excerpt": "import old",
                                }
                            ],
                        }
                    ],
                    "candidates": [
                        {
                            "commit": "candidate-sha",
                            "parent": "parent-sha",
                            "slices": [
                                {
                                    "file": (None, "a.py"),
                                    "anchors": [{"base": (0, 0), "after": (1, 2)}],
                                }
                            ],
                        }
                    ],
                }
            )

        self.assertEqual(result["index"], 0)
        self.assertEqual(result["candidate"]["commit"], "candidate-sha")
        self.assertEqual(result["parent"]["commit"], "parent-sha")
        self.assertEqual(
            result["candidate"]["presence"],
            {"passed": True, "reason": "candidate"},
        )
        self.assertEqual(
            result["candidate"]["assessment"],
            {"passed": True, "reason": "candidate"},
        )
        self.assertEqual(result["candidate"]["defect"], True)
        self.assertEqual(
            result["parent"]["presence"],
            {"passed": False, "reason": "Code snippet is empty."},
        )
        self.assertEqual(
            result["parent"]["assessment"],
            {"passed": False, "reason": "Code snippet is empty."},
        )
        self.assertEqual(result["parent"]["defect"], False)
        self.assertEqual(presence.invoke.call_count, 1)
        self.assertEqual(assessment.invoke.call_count, 1)

    def test_select(self) -> None:
        result = SelectNode().run(
            {
                "compare": {
                    "candidate": {"commit": "sha-a", "defect": True},
                    "parent": {"commit": "sha-b", "defect": False},
                }
            }
        )

        self.assertEqual(result, {"introducer": "sha-a"})

    def test_select_commit(self) -> None:
        self.assertEqual(
            SelectNode().run(
                {
                    "compare": {
                        "candidate": {"commit": None, "defect": True},
                        "parent": {"commit": "sha-b", "defect": False},
                    }
                }
            ),
            {},
        )

    def test_select_empty(self) -> None:
        self.assertEqual(
            SelectNode().run(
                {
                    "compare": {
                        "candidate": {"commit": "sha-a", "defect": False},
                        "parent": {"commit": "sha-b", "defect": False},
                    }
                }
            ),
            {},
        )

    def test_finalize(self) -> None:
        self.assertEqual(FinalizeNode().run({}), {"aborted": False})

    def test_verify(self) -> None:
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
                "causes": [
                    {
                        "reason": "Old line.",
                        "statements": [
                            {
                                "reference": "0-0-0",
                                "scope": "scope:program",
                                "excerpt": "import old",
                            }
                        ],
                    }
                ],
                "corrections": [
                    {
                        "reason": "Fix line.",
                        "statements": [
                            {
                                "reference": "0-0-1",
                                "scope": "scope:program",
                                "excerpt": "import fix_only",
                            }
                        ],
                    }
                ],
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
                ),
            }
        )

        llm.with_structured_output.assert_called_once_with(
            VerifyResponseSchema,
            method="json_schema",
            strict=True,
        )
        self.assertEqual(structured.invoke.call_count, 2)
        self.assertIn(
            "Determine whether the provided code snippet contains the defect",
            structured.invoke.call_args_list[0].args[0],
        )
        self.assertIn("Defect statements:", structured.invoke.call_args_list[0].args[0])
        self.assertIn(
            "- scope:program import old", structured.invoke.call_args_list[0].args[0]
        )
        self.assertNotIn("import fix_only", structured.invoke.call_args_list[0].args[0])
        self.assertNotIn("Fix line.", structured.invoke.call_args_list[0].args[0])
        self.assertNotIn("0-0-0", structured.invoke.call_args_list[0].args[0])
        self.assertIn("File: a.py", structured.invoke.call_args_list[0].args[0])
        self.assertIn("old_context", structured.invoke.call_args_list[0].args[0])
        self.assertIn("old", structured.invoke.call_args_list[0].args[0])
        self.assertNotIn("new", structured.invoke.call_args_list[0].args[0])
        self.assertIn("File: a.py", structured.invoke.call_args_list[1].args[0])
        self.assertIn("new", structured.invoke.call_args_list[1].args[0])
        self.assertNotIn("import fix_only", structured.invoke.call_args_list[1].args[0])
        self.assertNotIn("Fix line.", structured.invoke.call_args_list[1].args[0])
        self.assertNotIn("-old", structured.invoke.call_args_list[1].args[0])
        self.assertEqual(
            result,
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

    def test_verify_deduplicate(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.side_effect = [
            {"defect": True, "reason": "The snippet still contains the bug."},
            {"defect": False, "reason": "The snippet is corrected."},
        ]

        VerifyNode(llm=llm).run(
            {
                "rationale": "Validation fails before normalization.",
                "causes": [
                    {
                        "reason": "Old line.",
                        "statements": [
                            {
                                "reference": "0-0-0",
                                "scope": "scope:program",
                                "excerpt": "import old",
                            }
                        ],
                    },
                    {
                        "reason": "Old line again.",
                        "statements": [
                            {
                                "reference": "0-0-1",
                                "scope": "scope:program",
                                "excerpt": "import old",
                            }
                        ],
                    },
                ],
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
                ),
            }
        )

        self.assertEqual(
            structured.invoke.call_args_list[0]
            .args[0]
            .count("- scope:program import old"),
            1,
        )

    def test_verify_malformed(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.return_value = {"defect": True, "reason": "x"}
        with self.assertRaises(ValueError):
            VerifyNode(llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "causes": [
                        {
                            "reason": "Old line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": None,
                                    "excerpt": None,
                                }
                            ],
                        }
                    ],
                    "refinement": 1,
                }
            )

    def test_verify_inputs(self) -> None:
        with self.assertRaises(ValueError):
            VerifyNode().run({})

    def test_verify_empty(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])

        with self.assertRaises(ValueError):
            VerifyNode(llm=llm).run(
                {
                    "rationale": "Validation fails before normalization.",
                    "causes": [],
                    "refinement": "",
                }
            )

    def test_verify_json(self) -> None:
        llm = Mock(spec_set=["with_structured_output"])
        structured = Mock(spec_set=["invoke"])
        llm.with_structured_output.return_value = structured
        structured.invoke.side_effect = ["bad", {"defect": False, "reason": "x"}]
        node = VerifyNode(llm=llm)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "rationale": "Validation fails before normalization.",
                    "causes": [
                        {
                            "reason": "Old line.",
                            "statements": [
                                {
                                    "reference": "0-0-0",
                                    "scope": None,
                                    "excerpt": None,
                                }
                            ],
                        }
                    ],
                    "refinement": "\n".join(
                        [
                            "diff --git a/a.py b/a.py",
                            "--- a/a.py",
                            "+++ b/a.py",
                            "@@ -1 +1 @@",
                            "-old",
                            "+new",
                        ]
                    ),
                }
            )

    def test_aggregate(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="Final rationale.")
        node = AggregateNode(llm=llm)

        result = node.run(
            {
                "summary": "Shared summary",
                "files": ["a.py", "b.py", "c.py"],
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
                ],
            }
        )

        self.assertEqual(result["files"], ["a.py", "ghost.py", "b.py", "c.py"])
        self.assertEqual(result["rationale"], "Final rationale.")
        self.assertEqual(
            result["aggregate"]["files"],
            ["a.py", "ghost.py", "b.py", "c.py"],
        )
        self.assertEqual(
            result["aggregate"]["rationale"],
            "Final rationale.",
        )
        self.assertNotIn("rationales", result["aggregate"])
        llm.invoke.assert_called_once()
        self.assertIn("Summary:", llm.invoke.call_args.args[0])
        self.assertIn("Final related files:", llm.invoke.call_args.args[0])
        self.assertIn("Explain runs:", llm.invoke.call_args.args[0])
        self.assertIn(
            "Output only the final rationale text.", llm.invoke.call_args.args[0]
        )
        self.assertNotIn("choose files", llm.invoke.call_args.args[0])

    def test_aggregate_llm(self) -> None:
        node = AggregateNode()

        with self.assertRaises(ValueError):
            node.run(
                {
                    "summary": "Shared summary",
                    "files": ["a.py"],
                    "runs": [],
                }
            )

    def test_aggregate_empty(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="   ")
        node = AggregateNode(llm=llm)

        with self.assertRaises(ValueError):
            node.run(
                {
                    "summary": "Shared summary",
                    "files": ["a.py"],
                    "runs": [
                        {
                            "index": 0,
                            "rationale": "Rationale",
                            "files": ["a.py"],
                        }
                    ],
                }
            )

class TestAgenticSZZGraph(unittest.TestCase):
    def test_agentic_nodes(self) -> None:
        with self.assertRaises(ValueError):
            AgenticSZZGraph()

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_agentic_build(self) -> None:
        graph = AgenticSZZGraph(
            seed=Mock(spec_set=["run"]),
            search=Mock(spec_set=["run"]),
        ).build()
        self.assertIsNotNone(graph)

    def test_agentic_interface(self) -> None:
        self.assertIsInstance(
            AgenticSZZGraph(
                seed=Mock(spec_set=["run"]),
                search=Mock(spec_set=["run"]),
            ),
            AttributionGraph,
        )

    def test_agentic_builder_interface(self) -> None:
        builder = AgenticSZZGraphBuilder(
            seed=Mock(),
            search=Mock(),
        )

        self.assertIsInstance(builder, AttributionGraphBuilder)

    def test_agentic_builder_nodes(self) -> None:
        with self.assertRaises(ValueError):
            AgenticSZZGraphBuilder()

    def test_agentic_builder_graph(self) -> None:
        repository = object.__new__(VersionControl)
        seed = object()
        search = object()
        seed_builder = Mock(return_value=seed)
        search_builder = Mock(return_value=search)
        builder = AgenticSZZGraphBuilder(
            seed=seed_builder,
            search=search_builder,
        )

        graph = builder.create(repository)

        self.assertIsInstance(graph, AgenticSZZGraph)
        seed_builder.assert_called_once_with(repository)
        search_builder.assert_called_once_with(repository)

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_agentic_graph_forwards_steps(self) -> None:
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        seed = Mock(spec_set=["run"])
        seed.run.return_value = {
            "base": "base-sha",
            "after": "after-sha",
            "seed": [{"title": "Commit Message", "body": "Fix parser"}],
        }
        search = Mock(spec_set=["run"])
        search.run.return_value = {"introducer": "deadbeef"}
        graph = AgenticSZZGraph(
            seed=seed,
            search=search,
            steps=7,
        )

        result = graph.invoke(correction)

        seed.run.assert_called_once_with({"correction": correction})
        search.run.assert_called_once_with(
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [{"title": "Commit Message", "body": "Fix parser"}],
            },
            steps=7,
        )
        self.assertEqual(result, {"introducer": "deadbeef", "aborted": False})

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_agentic_builder_forwards_steps(self) -> None:
        repository = object.__new__(VersionControl)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        seed = Mock(spec_set=["run"])
        seed.run.return_value = {
            "base": "base-sha",
            "after": "after-sha",
            "seed": [{"title": "Commit Message", "body": "Fix parser"}],
        }
        search = Mock(spec_set=["run"])
        search.run.return_value = {"introducer": "deadbeef"}
        seed_builder = Mock(return_value=seed)
        search_builder = Mock(return_value=search)
        builder = AgenticSZZGraphBuilder(
            seed=seed_builder,
            search=search_builder,
            steps=9,
        )

        result = builder.create(repository).invoke(correction)

        seed_builder.assert_called_once_with(repository)
        search_builder.assert_called_once_with(repository)
        search.run.assert_called_once_with(
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [{"title": "Commit Message", "body": "Fix parser"}],
            },
            steps=9,
        )
        self.assertEqual(result, {"introducer": "deadbeef", "aborted": False})

    def test_agentic_invoke(self) -> None:
        graph = AgenticSZZGraph(
            seed=Mock(spec_set=["run"]),
            search=Mock(spec_set=["run"]),
        )
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"introducer": "abc123", "aborted": False}
        graph.build = Mock(return_value=compiled)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )

        result = graph.invoke(correction)

        compiled.invoke.assert_called_once_with(
            {
                "correction": correction,
                "nodes": {},
            },
            trace=None,
        )
        self.assertEqual(
            result,
            {"introducer": "abc123", "aborted": False},
        )


class TestAgentSZZGraph(unittest.TestCase):
    def test_agent_szz_interface(self) -> None:
        self.assertIsInstance(
            AgentSZZGraph(
                seed=Mock(spec_set=["run"]),
                search=Mock(spec_set=["run"]),
            ),
            AttributionGraph,
        )

    def test_agent_szz_requires_nodes(self) -> None:
        with self.assertRaises(ValueError):
            AgentSZZGraph()

    def test_agent_szz_rejects_invalid_steps(self) -> None:
        with self.assertRaises(ValueError):
            AgentSZZGraph(
                seed=Mock(spec_set=["run"]),
                search=Mock(spec_set=["run"]),
                steps=0,
            )

    def test_agent_szz_builder_interface(self) -> None:
        builder = AgentSZZGraphBuilder(
            seed=Mock(),
            search=Mock(),
        )

        self.assertIsInstance(builder, AttributionGraphBuilder)

    def test_agent_szz_builder_requires_nodes(self) -> None:
        with self.assertRaises(ValueError):
            AgentSZZGraphBuilder()

    def test_agent_szz_builder_rejects_invalid_steps(self) -> None:
        with self.assertRaises(ValueError):
            AgentSZZGraphBuilder(
                seed=Mock(),
                search=Mock(),
                steps=0,
            )

    def test_agent_szz_builder_graph(self) -> None:
        repository = object.__new__(VersionControl)
        seed = object()
        search = object()
        seed_builder = Mock(return_value=seed)
        search_builder = Mock(return_value=search)
        builder = AgentSZZGraphBuilder(
            seed=seed_builder,
            search=search_builder,
        )

        graph = builder.create(repository)

        self.assertIsInstance(graph, AgentSZZGraph)
        seed_builder.assert_called_once_with(repository)
        search_builder.assert_called_once_with(repository)

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_agent_szz_graph_forwards_steps(self) -> None:
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        seed = Mock(spec_set=["run"])
        seed.run.return_value = {
            "base": "base-sha",
            "after": "after-sha",
            "seed": [{"title": "Commit Message", "body": "Fix parser"}],
        }
        search = Mock(spec_set=["run"])
        search.run.return_value = {"introducer": "deadbeef"}
        graph = AgentSZZGraph(
            seed=seed,
            search=search,
            steps=13,
        )

        result = graph.invoke(correction)

        seed.run.assert_called_once_with({"correction": correction})
        search.run.assert_called_once_with(
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [{"title": "Commit Message", "body": "Fix parser"}],
            },
            steps=13,
        )
        self.assertEqual(
            result,
            {"introducer": "deadbeef", "aborted": False},
        )

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_agent_szz_builder_forwards_steps(self) -> None:
        repository = object.__new__(VersionControl)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        seed = Mock(spec_set=["run"])
        seed.run.return_value = {
            "base": "base-sha",
            "after": "after-sha",
            "seed": [{"title": "Commit Message", "body": "Fix parser"}],
        }
        search = Mock(spec_set=["run"])
        search.run.return_value = {"introducer": "deadbeef"}
        seed_builder = Mock(return_value=seed)
        search_builder = Mock(return_value=search)
        builder = AgentSZZGraphBuilder(
            seed=seed_builder,
            search=search_builder,
            steps=17,
        )

        result = builder.create(repository).invoke(correction)

        seed_builder.assert_called_once_with(repository)
        search_builder.assert_called_once_with(repository)
        search.run.assert_called_once_with(
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [{"title": "Commit Message", "body": "Fix parser"}],
            },
            steps=17,
        )
        self.assertEqual(
            result,
            {"introducer": "deadbeef", "aborted": False},
        )

    def test_agent_szz_invoke(self) -> None:
        graph = AgentSZZGraph(
            seed=Mock(spec_set=["run"]),
            search=Mock(spec_set=["run"]),
        )
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"introducer": "abc123", "aborted": False}
        graph.build = Mock(return_value=compiled)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )

        result = graph.invoke(correction)

        compiled.invoke.assert_called_once_with(
            {
                "correction": correction,
                "nodes": {},
            },
            trace=None,
        )
        self.assertEqual(
            result,
            {"introducer": "abc123", "aborted": False},
        )
