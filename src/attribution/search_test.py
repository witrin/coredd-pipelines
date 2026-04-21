from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from benchmark.attribution.domain import (
    Change,
    Commit,
    FileOperation,
    OperationType,
)
from benchmark.attribution.history import VersionControl
from benchmark.attribution.search import (
    DuplicateCallGuard,
    KnowledgeGraphSearchNode,
    KnowledgeGraphSeedNode,
    RepositorySearchNode,
    RepositorySeedNode,
    ResolvedErrorFilter,
    SearchResult,
    TurnBudget,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool


class TestKnowledgeGraphSeedNode(unittest.TestCase):
    @patch("benchmark.attribution.search.EntityPath.resolve")
    @patch.object(VersionControl, "blob", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    @patch.object(VersionControl, "meta", autospec=True)
    def test_seed(
        self,
        meta: Mock,
        manifest: Mock,
        blob: Mock,
        resolve: Mock,
    ) -> None:
        repository = object.__new__(VersionControl)
        node = KnowledgeGraphSeedNode(repository=repository)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        meta.return_value = Commit(
            sha="after-sha",
            message="Fix parser",
            time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            author="a",
        )
        manifest.return_value = [
            FileOperation(
                base="a.py",
                after="a.py",
                operation=OperationType.EDIT,
            )
        ]
        blob.return_value = b"def parse():\n    return 1\n"
        resolve.return_value = {
            ("EDIT", "entity:function=parse", "entity:function=parse")
        }

        result = node.run({"correction": correction})

        meta.assert_called_once_with(repository, "after-sha")
        manifest.assert_called_once_with(repository, "base-sha", "after-sha")
        self.assertEqual(
            result,
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [
                    {"title": "Correction Commit", "body": "after-sha"},
                    {"title": "Commit Message", "body": "Fix parser"},
                    {"title": "Modified Files", "body": "a.py"},
                    {"title": "Modified Entities", "body": "file=a.py:function=parse"},
                ],
            },
        )

    @patch("benchmark.attribution.search.EntityPath.resolve")
    @patch.object(VersionControl, "blob", autospec=True)
    @patch.object(VersionControl, "manifest", autospec=True)
    @patch.object(VersionControl, "meta", autospec=True)
    def test_seed_keeps_nested_entity_scope(
        self,
        meta: Mock,
        manifest: Mock,
        blob: Mock,
        resolve: Mock,
    ) -> None:
        repository = object.__new__(VersionControl)
        node = KnowledgeGraphSeedNode(repository=repository)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        meta.return_value = Commit(
            sha="after-sha",
            message="Fix parser",
            time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            author="a",
        )
        manifest.return_value = [
            FileOperation(
                base="a.py",
                after="a.py",
                operation=OperationType.EDIT,
            )
        ]
        blob.return_value = b"class Outer:\n    def parse(self):\n        return 1\n"
        resolve.return_value = {
            (
                "EDIT",
                "entity:class=Outer:method=parse",
                "entity:class=Outer:method=parse",
            )
        }

        result = node.run({"correction": correction})

        self.assertEqual(
            result["seed"],
            [
                {"title": "Correction Commit", "body": "after-sha"},
                {"title": "Commit Message", "body": "Fix parser"},
                {"title": "Modified Files", "body": "a.py"},
                {
                    "title": "Modified Entities",
                    "body": "file=a.py:class=Outer:method=parse",
                },
            ],
        )


class TestKnowledgeGraphSearchNode(unittest.TestCase):
    def test_search_inputs(self) -> None:
        with self.assertRaises(ValueError):
            KnowledgeGraphSearchNode().run({})

    def test_search_steps(self) -> None:
        tools = Mock(spec_set=["tools"])

        @tool
        def foo() -> str:
            """Return a placeholder value."""
            return "foo"

        @tool
        def bar() -> str:
            """Return another placeholder value."""
            return "bar"

        tools.tools.return_value = [foo, bar]
        agent = Mock(spec_set=["invoke"])
        agent.invoke.return_value = {
            "structured_response": {"introducer": "deadbeef"},
        }

        with patch(
            "benchmark.attribution.search.create_react_agent", return_value=agent
        ):
            KnowledgeGraphSearchNode(tool=tools, llm=object()).run(
                {
                    "base": "base-sha",
                    "after": "after-sha",
                    "seed": [
                        {
                            "title": "Commit Message",
                            "body": "prompt seed",
                        }
                    ],
                },
                steps=7,
            )

        args, kwargs = agent.invoke.call_args
        limit = (7 + 3) * 3 + 4
        self.assertEqual(kwargs, {"config": {"recursion_limit": limit}})
        self.assertEqual(args[0]["remaining_steps"], limit)

    def test_search_steps_invalid(self) -> None:
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = []

        with self.assertRaises(ValueError):
            KnowledgeGraphSearchNode(tool=tool, llm=object()).run(
                {
                    "base": "base-sha",
                    "after": "after-sha",
                    "seed": [
                        {
                            "title": "Commit Message",
                            "body": "prompt seed",
                        }
                    ],
                },
                steps=0,
            )

    def test_search(self) -> None:
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = []
        agent = Mock(spec_set=["invoke"])
        agent.invoke.return_value = {
            "structured_response": {"introducer": "deadbeef"},
        }

        with patch(
            "benchmark.attribution.search.create_react_agent", return_value=agent
        ):
            result = KnowledgeGraphSearchNode(tool=tool, llm=object()).run(
                {
                    "base": "base-sha",
                    "after": "after-sha",
                    "seed": [
                        {
                            "title": "Commit Message",
                            "body": "prompt seed",
                        }
                    ],
                }
            )

        tool.tools.assert_called_once_with()
        self.assertEqual(result, {"introducer": "deadbeef"})

    @patch("benchmark.attribution.search.ToolNode")
    @patch("benchmark.attribution.search.create_react_agent")
    def test_search_agent(self, factory: Mock, node_type: Mock) -> None:
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = ["tool-a", "tool-b"]
        agent = Mock(spec_set=["invoke"])
        agent.invoke.return_value = {
            "structured_response": {"introducer": "deadbeef"},
        }
        factory.return_value = agent
        tool_node = Mock()
        node_type.return_value = tool_node

        llm = object()
        node = KnowledgeGraphSearchNode(tool=tool, llm=llm)

        result = node.run(
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [
                    {
                        "title": "Commit Message",
                        "body": "prompt seed\nwith detail",
                    },
                    {
                        "title": "Modified Files",
                        "body": "a.py\nb.py",
                    },
                ],
            }
        )

        tool.tools.assert_called_once_with()
        node_type.assert_called_once()
        args, kwargs = node_type.call_args
        self.assertEqual(args, (["tool-a", "tool-b"],))
        self.assertIn("handle_tool_errors", kwargs)
        self.assertTrue(callable(kwargs["handle_tool_errors"]))
        self.assertEqual(
            kwargs["handle_tool_errors"](RuntimeError("boom")),
            "boom",
        )
        factory.assert_called_once()
        factory_args, factory_kwargs = factory.call_args
        self.assertEqual(factory_args, (llm,))
        self.assertIs(factory_kwargs["tools"], tool_node)
        self.assertIs(factory_kwargs["response_format"], SearchResult)
        self.assertIsInstance(factory_kwargs["pre_model_hook"], ResolvedErrorFilter)
        self.assertIsInstance(factory_kwargs["post_model_hook"], TurnBudget)
        self.assertIn("blame", factory_kwargs["prompt"])
        self.assertIn("candidates", factory_kwargs["prompt"])
        self.assertIn("full 40-character", factory_kwargs["prompt"])
        args, kwargs = agent.invoke.call_args
        limit = (20 + 3) * 3 + 4
        self.assertEqual(kwargs, {"config": {"recursion_limit": limit}})
        self.assertEqual(args[0]["remaining_steps"], limit)
        self.assertEqual(len(args[0]["messages"]), 1)
        self.assertIsInstance(args[0]["messages"][0], HumanMessage)
        self.assertEqual(
            args[0]["messages"][0].content,
            "\n\n".join(
                [
                    "# Context",
                    "## Commit Message\n````text\nprompt seed\nwith detail\n````",
                    "## Modified Files\n````text\na.py\nb.py\n````",
                ]
            ),
        )
        self.assertEqual(result, {"introducer": "deadbeef"})


class TestRepositorySeedNode(unittest.TestCase):
    def test_repository_seed_requires_inputs(self) -> None:
        with self.assertRaises(ValueError):
            RepositorySeedNode().run({})

    @patch("benchmark.attribution.search.DiffRenderer.render")
    @patch.object(VersionControl, "manifest", autospec=True)
    @patch.object(VersionControl, "diff", autospec=True)
    @patch.object(VersionControl, "meta", autospec=True)
    def test_repository_seed_contains_fix_diff(
        self,
        meta: Mock,
        diff: Mock,
        manifest: Mock,
        render: Mock,
    ) -> None:
        repository = object.__new__(VersionControl)
        node = RepositorySeedNode(repository=repository)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        meta.return_value = Commit(
            sha="after-sha",
            message="Fix parser",
            time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            author="a",
        )
        diff.return_value = object()
        render.return_value = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
        manifest.return_value = [
            FileOperation(
                base="foo.py",
                after="foo.py",
                operation=OperationType.EDIT,
            )
        ]

        result = node.run({"correction": correction})

        meta.assert_called_once_with(repository, "after-sha")
        diff.assert_called_once_with(repository, "base-sha", "after-sha", context=3)
        manifest.assert_called_once_with(repository, "base-sha", "after-sha")
        self.assertEqual(
            result,
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [
                    {"title": "Correction Commit", "body": "after-sha"},
                    {"title": "Commit Message", "body": "Fix parser"},
                    {
                        "title": "Fix Diff",
                        "body": "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py",
                    },
                    {"title": "Modified Files", "body": "foo.py"},
                ],
            },
        )

    @patch("benchmark.attribution.search.DiffRenderer.render")
    @patch.object(VersionControl, "manifest", autospec=True)
    @patch.object(VersionControl, "diff", autospec=True)
    @patch.object(VersionControl, "meta", autospec=True)
    def test_repository_seed_omits_empty_sections(
        self,
        meta: Mock,
        diff: Mock,
        manifest: Mock,
        render: Mock,
    ) -> None:
        repository = object.__new__(VersionControl)
        node = RepositorySeedNode(repository=repository)
        correction = Change(
            number=1,
            base="base-sha",
            after="after-sha",
            description="Fix parser",
        )
        meta.return_value = Commit(
            sha="after-sha",
            message="",
            time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            author="a",
        )
        diff.return_value = object()
        render.return_value = ""
        manifest.return_value = []

        result = node.run({"correction": correction})

        self.assertEqual(
            result["seed"],
            [{"title": "Correction Commit", "body": "after-sha"}],
        )


class TestRepositorySearchNode(unittest.TestCase):
    def test_repository_search_requires_inputs(self) -> None:
        with self.assertRaises(ValueError):
            RepositorySearchNode().run({})

    def test_repository_search_invalid_steps(self) -> None:
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = []

        with self.assertRaises(ValueError):
            RepositorySearchNode(tool=tool, llm=object()).run(
                {
                    "base": "base-sha",
                    "after": "after-sha",
                    "seed": [{"title": "Commit Message", "body": "prompt"}],
                },
                steps=0,
            )

    def test_repository_search_forwards_steps(self) -> None:
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = []
        agent = Mock(spec_set=["invoke"])
        agent.invoke.return_value = {
            "structured_response": {"introducer": "deadbeef"},
        }

        with patch(
            "benchmark.attribution.search.create_react_agent",
            return_value=agent,
        ):
            RepositorySearchNode(tool=tool, llm=object()).run(
                {
                    "base": "base-sha",
                    "after": "after-sha",
                    "seed": [{"title": "Commit Message", "body": "prompt"}],
                },
                steps=11,
            )

        args, kwargs = agent.invoke.call_args
        limit = (11 + 3) * 3 + 4
        self.assertEqual(kwargs, {"config": {"recursion_limit": limit}})
        self.assertEqual(args[0]["remaining_steps"], limit)

    @patch("benchmark.attribution.search.ToolNode")
    @patch("benchmark.attribution.search.create_react_agent")
    def test_repository_search_agent(
        self,
        factory: Mock,
        node_type: Mock,
    ) -> None:
        tool = Mock(spec_set=["tools"])
        tool.tools.return_value = ["tool-a", "tool-b"]
        agent = Mock(spec_set=["invoke"])
        agent.invoke.return_value = {
            "structured_response": {"introducer": "deadbeef"},
        }
        factory.return_value = agent
        tool_node = Mock()
        node_type.return_value = tool_node

        llm = object()
        node = RepositorySearchNode(tool=tool, llm=llm)

        result = node.run(
            {
                "base": "base-sha",
                "after": "after-sha",
                "seed": [
                    {"title": "Commit Message", "body": "Fix parser"},
                    {
                        "title": "Fix Diff",
                        "body": "diff --git a/foo.py b/foo.py",
                    },
                    {"title": "Modified Files", "body": "foo.py"},
                ],
            }
        )

        tool.tools.assert_called_once_with()
        node_type.assert_called_once()
        args, kwargs = node_type.call_args
        self.assertEqual(args, (["tool-a", "tool-b"],))
        self.assertIn("handle_tool_errors", kwargs)
        self.assertTrue(callable(kwargs["handle_tool_errors"]))
        factory.assert_called_once()
        factory_args, factory_kwargs = factory.call_args
        self.assertEqual(factory_args, (llm,))
        self.assertIs(factory_kwargs["tools"], tool_node)
        self.assertIs(factory_kwargs["response_format"], SearchResult)
        self.assertIsInstance(factory_kwargs["pre_model_hook"], ResolvedErrorFilter)
        self.assertIsInstance(factory_kwargs["post_model_hook"], TurnBudget)
        self.assertIn("blame", factory_kwargs["prompt"])
        self.assertIn("pickaxe", factory_kwargs["prompt"])
        self.assertIn("trace", factory_kwargs["prompt"])
        self.assertIn("full 40-character", factory_kwargs["prompt"])
        invoke_args, invoke_kwargs = agent.invoke.call_args
        limit = (30 + 3) * 3 + 4
        self.assertEqual(invoke_kwargs, {"config": {"recursion_limit": limit}})
        self.assertEqual(invoke_args[0]["remaining_steps"], limit)
        self.assertEqual(len(invoke_args[0]["messages"]), 1)
        self.assertIsInstance(invoke_args[0]["messages"][0], HumanMessage)
        self.assertEqual(
            invoke_args[0]["messages"][0].content,
            "\n\n".join(
                [
                    "# Context",
                    "## Commit Message\n````text\nFix parser\n````",
                    "## Fix Diff\n````text\ndiff --git a/foo.py b/foo.py\n````",
                    "## Modified Files\n````text\nfoo.py\n````",
                ]
            ),
        )
        self.assertEqual(result, {"introducer": "deadbeef"})


class TestTurnBudget(unittest.TestCase):
    def test_counts_only_successful_turns(self) -> None:
        budget = TurnBudget(turns=3)

        # First call — no prior tools, nothing counted.
        state = {"messages": [AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}])]}
        result = budget(state)
        self.assertEqual(result, {"messages": []})

        # Successful turn — budget decrements.
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
                ToolMessage(content="ok", tool_call_id="1"),
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
            ]
        }
        result = budget(state)
        self.assertEqual(result, {"messages": []})
        self.assertEqual(budget._turns, 2)

        # Error turn — turn budget unchanged, error budget decrements.
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
                ToolMessage(content="fail", tool_call_id="2", status="error"),
                AIMessage(content="", tool_calls=[{"name": "c", "args": {}, "id": "3"}]),
            ]
        }
        result = budget(state)
        self.assertEqual(result, {"messages": []})
        self.assertEqual(budget._turns, 2)
        self.assertEqual(budget._errors, 2)

    def test_terminates_on_turn_budget_exhaustion(self) -> None:
        budget = TurnBudget(turns=1)

        # First call — passes through.
        state = {"messages": [AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}])]}
        budget(state)

        # Successful turn consumes last budget.
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
                ToolMessage(content="ok", tool_call_id="1"),
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
            ]
        }
        result = budget(state)
        self.assertEqual(len(result["messages"]), 1)
        msg = result["messages"][0]
        self.assertIsInstance(msg, AIMessage)
        self.assertFalse(msg.tool_calls)
        self.assertIn("budget exhausted", msg.content.lower())

    def test_terminates_on_error_budget_exhaustion(self) -> None:
        budget = TurnBudget(turns=10, max_errors=1)

        # First call.
        state = {"messages": [AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}])]}
        budget(state)

        # Error turn consumes error budget.
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
                ToolMessage(content="fail", tool_call_id="1", status="error"),
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
            ]
        }
        result = budget(state)
        self.assertEqual(len(result["messages"]), 1)
        self.assertIsInstance(result["messages"][0], AIMessage)
        self.assertFalse(result["messages"][0].tool_calls)

    def test_no_termination_when_model_stops(self) -> None:
        budget = TurnBudget(turns=0)

        # Model does not request tools — hook should not interfere.
        state = {"messages": [AIMessage(content="done")]}
        result = budget(state)
        self.assertEqual(result, {"messages": []})

    def test_mixed_turn_counts_as_success(self) -> None:
        budget = TurnBudget(turns=2)

        # First call.
        state = {"messages": [AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}])]}
        budget(state)

        # Mixed: one success + one error → counts as successful turn.
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[
                    {"name": "a", "args": {}, "id": "1"},
                    {"name": "b", "args": {}, "id": "2"},
                ]),
                ToolMessage(content="ok", tool_call_id="1"),
                ToolMessage(content="fail", tool_call_id="2", status="error"),
                AIMessage(content="", tool_calls=[{"name": "c", "args": {}, "id": "3"}]),
            ]
        }
        result = budget(state)
        self.assertEqual(budget._turns, 1)
        self.assertEqual(budget._errors, 3)  # unchanged


class TestDuplicateCallGuardStatus(unittest.TestCase):
    def test_duplicate_returns_error_status(self) -> None:
        guard = DuplicateCallGuard()
        execute = Mock(return_value=ToolMessage(content="ok", tool_call_id="1"))
        request = Mock()
        request.tool_call = {"name": "show", "args": {"commit": "abc"}, "id": "1"}

        # First call — executes.
        result = guard(request, execute)
        execute.assert_called_once()
        self.assertEqual(result.status, "success")

        # Second call — duplicate, error status.
        request.tool_call = {"name": "show", "args": {"commit": "abc"}, "id": "2"}
        result = guard(request, execute)
        self.assertEqual(result.status, "error")
        self.assertIn("already called", result.content)


class TestResolvedErrorFilter(unittest.TestCase):
    def test_filters_resolved_error(self) -> None:
        hook = ResolvedErrorFilter()
        state = {
            "messages": [
                HumanMessage(content="start"),
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
                ToolMessage(content="bad input", tool_call_id="1", status="error"),
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
                ToolMessage(content="ok", tool_call_id="2"),
                AIMessage(content="", tool_calls=[{"name": "c", "args": {}, "id": "3"}]),
            ]
        }
        result = hook(state)
        messages = result["llm_input_messages"]

        self.assertEqual(len(messages), 4)
        self.assertNotIn(state["messages"][1], messages)
        self.assertNotIn(state["messages"][2], messages)

    def test_keeps_unresolved_error(self) -> None:
        hook = ResolvedErrorFilter()
        state = {
            "messages": [
                HumanMessage(content="start"),
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
                ToolMessage(content="bad input", tool_call_id="1", status="error"),
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
                ToolMessage(content="still bad", tool_call_id="2", status="error"),
            ]
        }
        result = hook(state)
        messages = result["llm_input_messages"]

        self.assertEqual(len(messages), 3)
        self.assertNotIn(state["messages"][1], messages)
        self.assertNotIn(state["messages"][2], messages)
        self.assertIn(state["messages"][3], messages)
        self.assertIn(state["messages"][4], messages)

    def test_keeps_success_messages(self) -> None:
        hook = ResolvedErrorFilter()
        state = {
            "messages": [
                HumanMessage(content="start"),
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
                ToolMessage(content="ok", tool_call_id="1"),
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
                ToolMessage(content="ok", tool_call_id="2"),
            ]
        }
        result = hook(state)
        messages = result["llm_input_messages"]

        self.assertEqual(len(messages), 5)

    def test_passthrough_without_errors(self) -> None:
        hook = ResolvedErrorFilter()
        state = {
            "messages": [
                HumanMessage(content="start"),
                AIMessage(content="done"),
            ]
        }
        result = hook(state)
        messages = result["llm_input_messages"]

        self.assertEqual(messages, state["messages"])

    def test_empty_messages(self) -> None:
        hook = ResolvedErrorFilter()
        result = hook({"messages": []})

        self.assertEqual(result["llm_input_messages"], [])

    def test_mixed_batch_removes_partial_errors(self) -> None:
        """AIMessage with mixed results is rewritten to keep only successful tool_calls."""
        hook = ResolvedErrorFilter()
        state = {
            "messages": [
                HumanMessage(content="start"),
                AIMessage(content="", tool_calls=[
                    {"name": "a", "args": {}, "id": "1"},
                    {"name": "b", "args": {}, "id": "2"},
                ]),
                ToolMessage(content="ok", tool_call_id="1"),
                ToolMessage(content="bad", tool_call_id="2", status="error"),
                AIMessage(content="", tool_calls=[{"name": "c", "args": {}, "id": "3"}]),
                ToolMessage(content="also bad", tool_call_id="3", status="error"),
                AIMessage(content="", tool_calls=[{"name": "d", "args": {}, "id": "4"}]),
            ]
        }
        result = hook(state)
        messages = result["llm_input_messages"]

        # AIMessage[1] stays but rewritten — only id=1 kept, id=2 removed
        rewritten = messages[1]
        self.assertIsInstance(rewritten, AIMessage)
        self.assertEqual([c["id"] for c in rewritten.tool_calls], ["1"])
        # Success ToolMessage stays
        self.assertIn(state["messages"][2], messages)
        # Error ToolMessage filtered
        self.assertNotIn(state["messages"][3], messages)
        # AIMessage[4] (all-error) filtered with its ToolMessage
        self.assertNotIn(state["messages"][4], messages)
        self.assertNotIn(state["messages"][5], messages)
        # Current AIMessage stays
        self.assertIn(state["messages"][6], messages)
        self.assertEqual(len(messages), 4)

    def test_preserves_tool_call_invariant(self) -> None:
        """Every AIMessage with tool_calls must have matching ToolMessages."""
        hook = ResolvedErrorFilter()
        state = {
            "messages": [
                HumanMessage(content="start"),
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
                ToolMessage(content="boundary error", tool_call_id="1", status="error"),
                AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "2"}]),
                ToolMessage(content="ok", tool_call_id="2"),
                AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "3"}]),
            ]
        }
        result = hook(state)
        messages = result["llm_input_messages"]

        # The all-error AIMessage and its ToolMessage are both removed.
        self.assertNotIn(state["messages"][1], messages)
        self.assertNotIn(state["messages"][2], messages)

        # Verify invariant: every non-boundary AIMessage with tool_calls
        # has a matching ToolMessage.  The last AIMessage (boundary) may
        # not have responses yet.
        tool_message_ids = {
            message.tool_call_id
            for message in messages
            if isinstance(message, ToolMessage)
        }
        for message in messages[:-1]:
            if isinstance(message, AIMessage) and message.tool_calls:
                for call in message.tool_calls:
                    self.assertIn(
                        call["id"],
                        tool_message_ids,
                        f"Orphaned tool_call id: {call['id']}",
                    )
