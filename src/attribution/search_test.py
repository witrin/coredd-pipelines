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
    KnowledgeGraphSearchNode,
    KnowledgeGraphSeedNode,
    RepositorySearchNode,
    RepositorySeedNode,
    SearchResult,
)
from langchain_core.messages import HumanMessage
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
        self.assertEqual(kwargs, {"config": {"recursion_limit": 7}})
        self.assertEqual(args[0]["remaining_steps"], 7)

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
        factory.assert_called_once_with(
            llm,
            tools=tool_node,
            prompt=inspect.cleandoc(
                """
                Identify which commit first introduced the bug fixed by the given correction.

                ## Commit Types (Priority Order)
                1. blame (score 1.0): Code deleted or modified by the correction was last
                   touched by this commit. Highest priority.
                2. blame-ancestor (score 0.6): Commit before the blame commit in file
                   history. Lower priority.
                3. correction-ancestor (score 0.3): Commit after blame but before the
                   correction. Lowest priority.

                ## Workflow
                1. Call candidates first. Use the fitness ranking as initial guidance, but
                   override it if tool evidence supports a different commit.
                2. Verify the top-ranked candidate using content before deciding:
                   - Does it ADD the code that the correction REMOVES?
                   - Does it introduce the LOGIC that exhibits the bug?
                3. If the top candidate is refactoring, cleanup, or import churn, reject it
                   and inspect related commits via neighbors or ancestors instead.
                4. Before selecting a candidate, confirm a direct causal link to the buggy
                   logic. A commit that merely exposes or triggers a pre-existing bug is
                   not the introducer.

                Each candidate lists modified files and entities (programs, classes,
                methods, functions). Use these to assess structural overlap with the
                correction.

                ## Constraints
                - Limit content to 3 calls per case. Use it to verify, not to explore.
                - If content fails, decide with the evidence you already have.
                - Make at least one investigation step before deciding unless a single
                  candidate is conclusive.
                - Do not inspect all candidates exhaustively. Focus on the most plausible.

                Return ONLY the full 40-character introducer SHA, or null if no justified
                decision can be made.
                """
            ),
            response_format=SearchResult,
        )
        args, kwargs = agent.invoke.call_args
        self.assertEqual(kwargs, {"config": {"recursion_limit": 20}})
        self.assertEqual(args[0]["remaining_steps"], 20)
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
        self.assertEqual(kwargs, {"config": {"recursion_limit": 11}})
        self.assertEqual(args[0]["remaining_steps"], 11)

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
        self.assertIn("blame", factory_kwargs["prompt"])
        self.assertIn("pickaxe", factory_kwargs["prompt"])
        self.assertIn("trace", factory_kwargs["prompt"])
        self.assertIn("full 40-character", factory_kwargs["prompt"])
        invoke_args, invoke_kwargs = agent.invoke.call_args
        self.assertEqual(invoke_kwargs, {"config": {"recursion_limit": 30}})
        self.assertEqual(invoke_args[0]["remaining_steps"], 30)
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
