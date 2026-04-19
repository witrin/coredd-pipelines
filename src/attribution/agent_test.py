from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from langchain_core.tools import BaseTool

from benchmark.attribution.agent import (
    DiffRenderer,
    KnowledgeGraphTool,
    KnowledgeGraphToolFactory,
    RepositoryTool,
    RepositoryToolFactory,
)
from benchmark.attribution.history import VersionControl, VersionControlSnapshot
from benchmark.attribution.history_fixture import HistoryFixture
from benchmark.attribution.knowledge import KnowledgeGraphBuilder, KnowledgeGraphStore


class TestKnowledgeGraphTool(unittest.TestCase):
    def _entity(self, store, key: str) -> dict[str, object]:
        return store.graph.nodes[key]["payload"]

    def _tool(
        self,
        repository: VersionControl,
        base: str,
        after: str,
    ) -> tuple[KnowledgeGraphTool, KnowledgeGraphStore]:
        store = KnowledgeGraphBuilder(repository=repository).build(base, after)
        return KnowledgeGraphTool(repository=repository, store=store), store

    def test_build_entity(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 1
                    """
                }
            )
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 2
                    """
                }
            )
            after = fixture.commit(tree, [base], "after")

            agent, store = self._tool(VersionControl(fixture.repository), base, after)

            key = "entity:a.py:function=foo"
            self.assertIn(key, store.graph.nodes)
            self.assertEqual(
                self._entity(store, key),
                {
                    "kind": "function",
                    "path": "a.py",
                    "urn": "entity:function=foo",
                    "name": "foo",
                    "source": "gumtree",
                },
            )

            edges = [
                (source, target, data["edge"])
                for source, target, data in store.graph.edges(data=True)
                if data["edge"] in {"ADD", "EDIT", "DELETE"}
            ]
            self.assertEqual(
                edges,
                [
                    (f"commit:{after}", "entity:a.py:function=foo", "EDIT"),
                ],
            )

    def test_build_nested(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "a.py": """
                    class Outer:
                        first = 1
                        second = 2
                        third = 3

                        class Inner:
                            def bar(self):
                                total = 0
                                for step in range(3):
                                    total += step
                                if total > 1:
                                    note = total + 1
                                else:
                                    note = total - 1
                                while note < 20:
                                    note += 2
                                return note

                        def foo(self):
                            seed = self.first + self.second
                            def baz(flag):
                                current = seed
                                if flag:
                                    current = current + 10
                                else:
                                    current = current - 5
                                while current < 100:
                                    current += 4
                                return current
                            return baz(True) + self.third
                    """
                }
            )
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree(
                {
                    "a.py": """
                    class Outer:
                        first = 1
                        second = 20
                        third = 3

                        class Inner:
                            def bar(self):
                                total = 0
                                for step in range(3):
                                    total += step
                                if total > 1:
                                    note = total + 5
                                else:
                                    note = total - 1
                                while note < 20:
                                    note += 2
                                return note

                        def foo(self):
                            seed = self.first + self.second
                            def baz(flag):
                                current = seed
                                if flag:
                                    current = current + 10
                                else:
                                    current = current - 5
                                while current < 100:
                                    current += 9
                                return current
                            return baz(True) + self.third
                    """
                }
            )
            after = fixture.commit(tree, [base], "after")

            agent, store = self._tool(VersionControl(fixture.repository), base, after)

            expected = {
                "entity:a.py:class=Outer": {
                    "kind": "class",
                    "path": "a.py",
                    "urn": "entity:class=Outer",
                    "name": "Outer",
                    "source": "gumtree",
                },
                "entity:a.py:class=Outer:class=Inner:method=bar": {
                    "kind": "method",
                    "path": "a.py",
                    "urn": "entity:class=Outer:class=Inner:method=bar",
                    "name": "bar",
                    "source": "gumtree",
                },
                "entity:a.py:class=Outer:method=foo:function=baz": {
                    "kind": "function",
                    "path": "a.py",
                    "urn": "entity:class=Outer:method=foo:function=baz",
                    "name": "baz",
                    "source": "gumtree",
                },
            }

            self.assertEqual(
                {
                    key: self._entity(store, key)
                    for key in expected
                },
                expected,
            )

            edges = [
                (source, target, data["edge"])
                for source, target, data in store.graph.edges(data=True)
                if source == f"commit:{after}" and data["edge"] == "EDIT"
            ]
            self.assertEqual(
                sorted(edges),
                sorted((f"commit:{after}", key, "EDIT") for key in expected),
            )

    def test_build_deduplicate(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 1
                    """
                }
            )
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 2
                    """
                }
            )
            after = fixture.commit(tree, [base], "after")

            with patch(
                "benchmark.attribution.knowledge.EntityPath.resolve",
                return_value=[
                    ("EDIT", "entity:function=foo", "entity:function=foo"),
                    ("EDIT", "entity:function=foo", "entity:function=foo"),
                ],
            ):
                agent, store = self._tool(VersionControl(fixture.repository), base, after)

            edges = [
                (source, target, data["edge"])
                for source, target, data in store.graph.edges(data=True)
                if data["edge"] == "EDIT"
            ]
            self.assertEqual(
                edges,
                [
                    (f"commit:{after}", "entity:a.py:function=foo", "EDIT"),
                ],
            )

    def test_load(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 1
                    """
                }
            )
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 2
                    """
                }
            )
            after = fixture.commit(tree, [base], "after")

            repository = VersionControl(fixture.repository)
            store = KnowledgeGraphBuilder(repository=repository).build(base, after)
            loaded = KnowledgeGraphStore.load(store.dump())

            agent = KnowledgeGraphTool(repository=repository, store=loaded)

            self.assertEqual(agent.meta(after)["sha"], after)
            self.assertEqual(
                agent.content(after, "a.py")["path"],
                "a.py",
            )

    def test_factory_redacts_messages_through_filter(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 1
                    """
                }
            )
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 2
                    """
                }
            )
            after = fixture.commit(tree, [base], "secret fix")

            repository = VersionControl(fixture.repository)
            store = KnowledgeGraphBuilder(repository=repository).build(base, after)

            with tempfile.TemporaryDirectory() as work:
                path = Path(work)
                with open(path / f"{after}.json", "w", encoding="utf-8") as file:
                    json.dump(store.dump(), file, default=str)

                factory = KnowledgeGraphToolFactory(
                    repository=repository,
                    source=path,
                    filter=lambda kind, value, identifier: (
                        value.replace("secret", "<redacted>")
                        if kind == "message"
                        else value
                    ),
                )
                agent = factory.create(base, after)

            self.assertNotIn("secret", agent.meta(after)["message"])
            self.assertIn("<redacted>", agent.meta(after)["message"])
            self.assertTrue(
                all("secret" not in candidate["message"] for candidate in agent.candidates())
            )

    def test_candidates_use_canonical_type(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.add(
            "function",
            {
                "kind": "function",
                "path": "a.py",
                "urn": "entity:function=foo",
                "name": "foo",
                "source": "gumtree",
            },
        )
        store.add(
            "commit",
            {
                "sha": "foo",
                "type": "blame",
                "message": "foo",
                "timestamp": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.add(
            "commit",
            {
                "sha": "bar",
                "type": "blame-ancestor",
                "message": "bar",
                "timestamp": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.add(
            "commit",
            {
                "sha": "baz",
                "type": "correction-ancestor",
                "message": "baz",
                "timestamp": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "parents": (),
                "files": ("b.py",),
            },
        )
        store.link("EDIT", "commit:foo", "entity:a.py:function=foo")
        store.link("EDIT", "commit:bar", "entity:a.py:function=foo")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(
            agent.candidates(),
            [
                {
                    "sha": "foo",
                    "type": "blame",
                    "score": 1.0,
                    "message": "foo",
                    "timestamp": "2024-01-03T00:00:00+00:00",
                    "files": ["a.py"],
                    "entities": ["a.py: foo"],
                },
                {
                    "sha": "bar",
                    "type": "blame-ancestor",
                    "score": 0.6,
                    "message": "bar",
                    "timestamp": "2024-01-02T00:00:00+00:00",
                    "files": ["a.py"],
                    "entities": ["a.py: foo"],
                },
                {
                    "sha": "baz",
                    "type": "correction-ancestor",
                    "score": 0.3,
                    "message": "baz",
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "files": ["b.py"],
                    "entities": [],
                },
            ],
        )

    def test_meta_filters_commit_payload_to_public_contract(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.after = "foo"
        store.add("file", {"path": "a.py"})
        store.add(
            "function",
            {
                "kind": "function",
                "path": "a.py",
                "urn": "entity:function=foo",
                "name": "foo",
                "source": "gumtree",
            },
        )
        store.add(
            "commit",
            {
                "sha": "foo",
                "type": "correction",
                "message": "foo",
                "timestamp": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.add(
            "commit",
            {
                "sha": "bar",
                "type": "blame-ancestor",
                "message": "bar",
                "timestamp": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "parents": ("foo", "baz"),
                "files": ("a.py",),
            },
        )
        store.link("MODIFIES", "commit:foo", "file:a.py")
        store.link("EDIT", "commit:foo", "entity:a.py:function=foo")
        store.link("MODIFIES", "commit:bar", "file:a.py")
        store.link("EDIT", "commit:bar", "entity:a.py:function=foo")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(
            agent.meta("bar"),
            {
                "kind": "commit",
                "sha": "bar",
                "type": "blame-ancestor",
                "message": "bar",
                "timestamp": "2024-01-02T00:00:00+00:00",
                "files": ["a.py"],
                "entities": ["a.py: foo"],
                "overlap": ["a.py: foo"],
            },
        )
        self.assertNotIn("payload", agent.meta("bar"))
        self.assertNotIn("parents", agent.meta("bar"))
        self.assertNotIn("score", agent.meta("bar"))
        self.assertNotIn("predecessors", agent.meta("bar"))

    def test_meta_reports_prefix_overlap_with_correction(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.after = "foo"
        store.add("file", {"path": "a.py"})
        store.add(
            "class",
            {
                "kind": "class",
                "path": "a.py",
                "urn": "entity:class=Outer",
                "name": "Outer",
                "source": "gumtree",
            },
        )
        store.add(
            "method",
            {
                "kind": "method",
                "path": "a.py",
                "urn": "entity:class=Outer:method=bar",
                "name": "bar",
                "source": "gumtree",
            },
        )
        store.add(
            "commit",
            {
                "sha": "foo",
                "type": "correction",
                "message": "foo",
                "timestamp": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.add(
            "commit",
            {
                "sha": "bar",
                "type": "blame-ancestor",
                "message": "bar",
                "timestamp": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.link("MODIFIES", "commit:foo", "file:a.py")
        store.link("EDIT", "commit:foo", "entity:a.py:class=Outer:method=bar")
        store.link("MODIFIES", "commit:bar", "file:a.py")
        store.link("EDIT", "commit:bar", "entity:a.py:class=Outer")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(agent.meta("bar")["overlap"], ["a.py: Outer"])

    def test_meta_reports_self_overlap_for_correction(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.after = "foo"
        store.add("file", {"path": "a.py"})
        store.add(
            "function",
            {
                "kind": "function",
                "path": "a.py",
                "urn": "entity:function=foo",
                "name": "foo",
                "source": "gumtree",
            },
        )
        store.add(
            "commit",
            {
                "sha": "foo",
                "type": "correction",
                "message": "foo",
                "timestamp": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.link("MODIFIES", "commit:foo", "file:a.py")
        store.link("EDIT", "commit:foo", "entity:a.py:function=foo")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(agent.meta("foo")["overlap"], ["a.py: foo"])

    def test_neighbors_prefer_entity_overlap_and_map_type(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.add("file", {"path": "a.py"})
        store.add(
            "function",
            {
                "kind": "function",
                "path": "a.py",
                "urn": "entity:function=foo",
                "name": "foo",
                "source": "gumtree",
            },
        )
        for sha, category, day in (
            ("foo", "blame", 11),
            ("bar", "blame-ancestor", 7),
            ("baz", "correction-ancestor", 4),
        ):
            store.add(
                "commit",
                {
                    "sha": sha,
                    "type": category,
                    "message": sha,
                    "timestamp": datetime(
                        2024,
                        1,
                        day,
                        tzinfo=timezone.utc,
                    ),
                    "parents": (),
                    "files": (),
                },
            )

        store.link("MODIFIES", "commit:foo", "file:a.py")
        store.link("EDIT", "commit:foo", "entity:a.py:function=foo")
        store.link("MODIFIES", "commit:bar", "file:a.py")
        store.link("EDIT", "commit:bar", "entity:a.py:function=foo")
        store.link("MODIFIES", "commit:baz", "file:a.py")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(
            agent.neighbors("foo"),
            [
                {
                    "sha": "bar",
                    "type": "blame-ancestor",
                    "files": [],
                    "overlap": ["a.py: foo"],
                },
                {
                    "sha": "baz",
                    "type": "correction-ancestor",
                    "files": [],
                    "overlap": ["a.py"],
                },
            ],
        )

    def test_neighbors_fall_back_to_file_when_levels_differ(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.add("file", {"path": "a.py"})
        store.add(
            "class",
            {
                "kind": "class",
                "path": "a.py",
                "urn": "entity:class=Outer",
                "name": "Outer",
                "source": "gumtree",
            },
        )
        store.add(
            "method",
            {
                "kind": "method",
                "path": "a.py",
                "urn": "entity:class=Outer:method=bar",
                "name": "bar",
                "source": "gumtree",
            },
        )
        for sha, category, day in (
            ("foo", "blame", 11),
            ("bar", "blame-ancestor", 7),
        ):
            store.add(
                "commit",
                {
                    "sha": sha,
                    "type": category,
                    "message": sha,
                    "timestamp": datetime(
                        2024,
                        1,
                        day,
                        tzinfo=timezone.utc,
                    ),
                    "parents": (),
                    "files": (),
                },
            )

        store.link("MODIFIES", "commit:foo", "file:a.py")
        store.link("EDIT", "commit:foo", "entity:a.py:class=Outer:method=bar")
        store.link("MODIFIES", "commit:bar", "file:a.py")
        store.link("EDIT", "commit:bar", "entity:a.py:class=Outer")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(
            agent.neighbors("foo"),
            [
                {
                    "sha": "bar",
                    "type": "blame-ancestor",
                    "files": [],
                    "overlap": ["a.py: Outer"],
                },
            ],
        )

    def test_neighbors_preserve_program_and_function_levels(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.add("file", {"path": "a.py"})
        store.add(
            "program",
            {
                "kind": "program",
                "path": "a.py",
                "urn": "entity:program",
                "name": "program",
                "source": "gumtree",
            },
        )
        store.add(
            "function",
            {
                "kind": "function",
                "path": "a.py",
                "urn": "entity:function=foo",
                "name": "foo",
                "source": "gumtree",
            },
        )
        for sha, category, day in (
            ("foo", "blame", 11),
            ("bar", "blame-ancestor", 7),
        ):
            store.add(
                "commit",
                {
                    "sha": sha,
                    "type": category,
                    "message": sha,
                    "timestamp": datetime(
                        2024,
                        1,
                        day,
                        tzinfo=timezone.utc,
                    ),
                    "parents": (),
                    "files": (),
                },
            )

        store.link("MODIFIES", "commit:foo", "file:a.py")
        store.link("EDIT", "commit:foo", "entity:a.py:program")
        store.link("EDIT", "commit:foo", "entity:a.py:function=foo")
        store.link("MODIFIES", "commit:bar", "file:a.py")
        store.link("EDIT", "commit:bar", "entity:a.py:program")
        store.link("EDIT", "commit:bar", "entity:a.py:function=foo")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(
            agent.neighbors("foo"),
            [
                {
                    "sha": "bar",
                    "type": "blame-ancestor",
                    "files": [],
                    "overlap": ["a.py: program, foo"],
                },
            ],
        )

    def test_candidates_include_program_entities(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.add(
            "program",
            {
                "kind": "program",
                "path": "a.py",
                "urn": "entity:program",
                "name": "program",
                "source": "gumtree",
            },
        )
        store.add(
            "function",
            {
                "kind": "function",
                "path": "a.py",
                "urn": "entity:function=foo",
                "name": "foo",
                "source": "gumtree",
            },
        )
        store.add(
            "commit",
            {
                "sha": "foo",
                "type": "blame",
                "message": "foo",
                "timestamp": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.link("EDIT", "commit:foo", "entity:a.py:program")
        store.link("EDIT", "commit:foo", "entity:a.py:function=foo")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(
            agent.candidates(),
            [
                {
                    "sha": "foo",
                    "type": "blame",
                    "score": 1.0,
                    "message": "foo",
                    "timestamp": "2024-01-03T00:00:00+00:00",
                    "files": ["a.py"],
                    "entities": ["a.py: program, foo"],
                },
            ],
        )

    def test_candidates_include_non_function_entities(self) -> None:
        repository = Mock(spec_set=["parents"])
        store = KnowledgeGraphStore()
        store.add(
            "class",
            {
                "kind": "class",
                "path": "a.py",
                "urn": "entity:class=Outer",
                "name": "Outer",
                "source": "gumtree",
            },
        )
        store.add(
            "method",
            {
                "kind": "method",
                "path": "a.py",
                "urn": "entity:class=Outer:method=bar",
                "name": "bar",
                "source": "gumtree",
            },
        )
        store.add(
            "commit",
            {
                "sha": "foo",
                "type": "blame",
                "message": "foo",
                "timestamp": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "parents": (),
                "files": ("a.py",),
            },
        )
        store.link("EDIT", "commit:foo", "entity:a.py:class=Outer:method=bar")

        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        self.assertEqual(
            agent.candidates(),
            [
                {
                    "sha": "foo",
                    "type": "blame",
                    "score": 1.0,
                    "message": "foo",
                    "timestamp": "2024-01-03T00:00:00+00:00",
                    "files": ["a.py"],
                    "entities": ["a.py: Outer.bar"],
                },
            ],
        )

    def test_snapshot(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 1
                    """
                }
            )
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree(
                {
                    "a.py": """
                    def foo():
                        return 2
                    """
                }
            )
            after = fixture.commit(tree, [base], "after")

            repository = VersionControl(fixture.repository)
            store = KnowledgeGraphBuilder(repository=repository).build(base, after)
            target = tempfile.TemporaryDirectory()
            self.addCleanup(target.cleanup)
            repository.dump([{base: [], after: [base]}], target.name)
            snapshot = VersionControlSnapshot.load(target.name)
            self.addCleanup(snapshot.close)

            agent = KnowledgeGraphTool(
                repository=snapshot,
                store=KnowledgeGraphStore.load(store.dump()),
            )

            result = agent.content(after, "a.py")

            self.assertEqual(result["sha"], after)
            self.assertEqual(result["path"], "a.py")
            self.assertIn("diff --git a/a.py b/a.py", result["patch"])
            self.assertIn("@@", result["patch"])
            self.assertNotIn("index ", result["patch"])
            self.assertNotIn("type", result)
            self.assertNotIn("score", result)
            self.assertNotIn("message", result)

    def test_tools(self) -> None:
        agent = KnowledgeGraphTool(
            repository=object.__new__(VersionControl),
            store=KnowledgeGraphStore(),
        )

        tools = agent.tools()

        self.assertEqual([tool.name for tool in tools], ["candidates", "neighbors", "meta", "content"])
        self.assertTrue(all(isinstance(tool, BaseTool) for tool in tools))
        self.assertIn("top-ranked commit candidates", tools[0].description)
        self.assertIn("touched by that commit", tools[0].description)
        self.assertIn("entity", tools[1].description)
        self.assertIn("touched files", tools[1].description)
        self.assertIn("Inspect one commit", tools[2].description)
        self.assertIn("entity overlap between", tools[2].description)
        self.assertIn("commit SHA, optional path", tools[3].description)

    def test_budget(self) -> None:
        repository = Mock(spec_set=["parents"])
        repository.parents.return_value = []
        store = KnowledgeGraphStore()
        store.add(
            "commit",
            {
                "sha": "foo",
                "type": "correction-ancestor",
                "message": "foo",
                "timestamp": 0,
                "files": [],
            },
        )
        agent = KnowledgeGraphTool(
            repository=repository,
            store=store,
        )

        for _ in range(3):
            agent.content("foo")

        with self.assertRaises(ValueError):
            agent.content("foo")


class TestRepositoryTool(unittest.TestCase):
    def test_show_returns_diff_against_first_parent(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "def foo():\n    return 1\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "def foo():\n    return 2\n"})
            after = fixture.commit(tree, [base], "bump return value")

            repository = VersionControl(fixture.repository)
            tool = RepositoryTool(repository=repository, after=after)

            result = tool.show(after)

            self.assertEqual(result["sha"], after)
            self.assertEqual(result["message"], "bump return value")
            self.assertEqual(result["parents"], [base])
            self.assertIn("return 1", result["patch"])
            self.assertIn("return 2", result["patch"])

    def test_show_scoped_to_path(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "def foo():\n    return 1\n",
                    "bar.py": "def bar():\n    return 1\n",
                }
            )
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree(
                {
                    "foo.py": "def foo():\n    return 2\n",
                    "bar.py": "def bar():\n    return 2\n",
                }
            )
            after = fixture.commit(tree, [base], "bump both")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.show(after, path="foo.py")

            self.assertIn("foo.py", result["patch"])
            self.assertNotIn("bar.py", result["patch"])

    def test_blame_returns_origin_commit(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "alpha\nbeta\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "alpha\ngamma\n"})
            after = fixture.commit(tree, [base], "replace beta")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            first = tool.blame("foo.py", after, 1, 1)
            self.assertEqual(
                first["blame"],
                f"L1: {base[:12]} | foo.py | alpha",
            )
            self.assertEqual(first["commits"], {base: "base"})

            second = tool.blame("foo.py", after, 2, 2)
            self.assertEqual(
                second["blame"],
                f"L2: {after[:12]} | foo.py | gamma",
            )
            self.assertEqual(second["commits"], {after: "replace beta"})

    def test_pickaxe_emits_commits_with_changed_count(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "x = 0\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "x = 0\ny = token\n"})
            introduce = fixture.commit(tree, [base], "introduce token")
            tree = fixture.tree({"foo.py": "x = 0\ny = token\nz = 1\n"})
            after = fixture.commit(tree, [introduce], "unrelated")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.pickaxe("token", path="foo.py")
            shas = {entry["sha"] for entry in result}
            self.assertIn(introduce, shas)
            self.assertNotIn(after, shas)

    def test_pickaxe_requires_path(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "pass\n"})
            after = fixture.commit(tree, [], "base")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )
            with self.assertRaises(ValueError):
                tool.pickaxe("needle", path="")

    def test_trace_matches_entity_regex(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": """
                    def parse():
                        return 1
                    """,
                }
            )
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree(
                {
                    "foo.py": """
                    def parse():
                        return 2
                    """,
                }
            )
            change = fixture.commit(tree, [base], "tweak parse")
            tree = fixture.tree(
                {
                    "foo.py": """
                    def parse():
                        return 2

                    def other():
                        pass
                    """,
                }
            )
            after = fixture.commit(tree, [change], "add unrelated")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.trace("parse", path="foo.py")
            shas = {entry["sha"] for entry in result}
            self.assertIn(change, shas)

    def test_grep_scans_tree_at_commit(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "needle\nother\n",
                    "bar.py": "unrelated\n",
                }
            )
            after = fixture.commit(tree, [], "base")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=after)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["path"], "foo.py")
            self.assertEqual(result[0]["line"], 1)
            self.assertEqual(result[0]["text"], "needle")

    def test_grep_respects_path_filter(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "needle\n",
                    "bar.py": "needle\n",
                }
            )
            after = fixture.commit(tree, [], "base")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=after, path="bar.py")
            self.assertEqual([entry["path"] for entry in result], ["bar.py"])

    def test_tools_exposes_five_entries(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "pass\n"})
            after = fixture.commit(tree, [], "base")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            entries = tool.tools()
            self.assertEqual(len(entries), 5)
            for entry in entries:
                self.assertIsInstance(entry, BaseTool)

    def test_budget_blocks_over_budget_pickaxe(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "needle\n"})
            after = fixture.commit(tree, [], "base")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
                budget=1,
            )
            tool.pickaxe("needle", path="foo.py")
            with self.assertRaises(ValueError):
                tool.pickaxe("needle", path="foo.py")

    def test_pickaxe_without_path_searches_across_files(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "pass\n", "b.py": "pass\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"a.py": "token = 1\n", "b.py": "pass\n"})
            introduce = fixture.commit(tree, [base], "add token in a")
            tree = fixture.tree({"a.py": "token = 1\n", "b.py": "token = 2\n"})
            after = fixture.commit(tree, [introduce], "add token in b")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.pickaxe("token", path=None)
            shas = {entry["sha"] for entry in result}
            self.assertIn(introduce, shas)
            self.assertNotIn(after, shas)

    def test_pickaxe_without_path_skips_correction(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "pass\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "token\n"})
            after = fixture.commit(tree, [base], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.pickaxe("token", path=None)
            shas = {entry["sha"] for entry in result}
            self.assertNotIn(after, shas)

    def test_show_compresses_large_patch(self) -> None:
        lines = ["+" + f"line{i}" for i in range(120)]
        patch = "\n".join(
            ["diff --git a/f.py b/f.py", "--- a/f.py", "+++ b/f.py", "@@ -0,0 +1,120 @@"]
            + lines
        )
        compressed = RepositoryTool._compress_patch(patch)
        self.assertIn("diff lines omitted", compressed)
        output_lines = compressed.split("\n")
        self.assertLessEqual(len(output_lines), 85)

    def test_show_preserves_short_patch(self) -> None:
        patch = "\n".join([
            "diff --git a/f.py b/f.py",
            "--- a/f.py",
            "+++ b/f.py",
            "@@ -1 +1 @@",
            "-old",
            "+new",
        ])
        compressed = RepositoryTool._compress_patch(patch)
        self.assertNotIn("omitted", compressed)
        self.assertIn("-old", compressed)
        self.assertIn("+new", compressed)

    def test_blame_compresses_long_output(self) -> None:
        lines = [f"L{i}: abc123 | foo.py | code" for i in range(200)]
        compressed = RepositoryTool._compress_blame(lines)
        self.assertLess(len(compressed), 200)
        self.assertTrue(any("omitted" in line for line in compressed))

    def test_blame_preserves_short_output(self) -> None:
        lines = [f"L{i}: abc123 | foo.py | code" for i in range(10)]
        compressed = RepositoryTool._compress_blame(lines)
        self.assertEqual(compressed, lines)

    def test_blame_uses_short_shas(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "alpha\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "beta\n"})
            after = fixture.commit(tree, [base], "edit")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.blame("foo.py", commit=base, start=1, end=1)
            blame_text = result["blame"]
            for line in blame_text.splitlines():
                if line.startswith("L") and "|" in line:
                    sha_part = line.split(":")[1].strip().split(" |")[0].strip()
                    if sha_part != "?":
                        self.assertEqual(len(sha_part), 12)

    def test_factory_creates_tool_bound_to_after(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "pass\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "x = 1\n"})
            after = fixture.commit(tree, [base], "edit")

            factory = RepositoryToolFactory(
                repository=VersionControl(fixture.repository),
            )
            tool = factory.create(base, after)

            show = tool.show(after)
            self.assertEqual(show["sha"], after)


class TestDiffRenderer(unittest.TestCase):
    def test_renders_path_scoped_hunks(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "alpha\n",
                    "bar.py": "alpha\n",
                }
            )
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree(
                {
                    "foo.py": "beta\n",
                    "bar.py": "beta\n",
                }
            )
            after = fixture.commit(tree, [base], "edit")

            repository = VersionControl(fixture.repository)
            diff = repository.diff(base, after)

            full = DiffRenderer().render(diff)
            scoped = DiffRenderer().render(diff, path="foo.py")

            self.assertIn("foo.py", full)
            self.assertIn("bar.py", full)
            self.assertIn("foo.py", scoped)
            self.assertNotIn("bar.py", scoped)


if __name__ == "__main__":
    unittest.main()
