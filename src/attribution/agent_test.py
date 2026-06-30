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
        self.assertIn("inspect the search space", tools[0].description)
        self.assertIn("structurally related commit candidates", tools[1].description)
        self.assertIn("same files or entities", tools[1].description)
        self.assertIn("Query one commit", tools[2].description)
        self.assertIn("inspect commit metadata", tools[2].description)
        self.assertIn("causal verification", tools[3].description)

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
            target = fixture.commit(tree, [base], "bump return value")
            tree = fixture.tree({"foo.py": "def foo():\n    return 2\n"})
            after = fixture.commit(tree, [target], "correction")

            repository = VersionControl(fixture.repository)
            tool = RepositoryTool(repository=repository, after=after)

            result = tool.show(target)

            self.assertEqual(result["sha"], target)
            self.assertEqual(result["message"], "bump return value")
            self.assertEqual(result["parents"], [base])
            self.assertIn("return 1", result["patch"])
            self.assertIn("return 2", result["patch"])

    def test_show_stat_lists_changed_files(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "def foo():\n    return 1\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "def foo():\n    return 2\n"})
            target = fixture.commit(tree, [base], "bump return value")
            tree = fixture.tree({"foo.py": "def foo():\n    return 2\n"})
            after = fixture.commit(tree, [target], "correction")

            repository = VersionControl(fixture.repository)
            tool = RepositoryTool(repository=repository, after=after)

            result = tool.show(target, stat=True)

            self.assertEqual([f["path"] for f in result["files"]], ["foo.py"])
            self.assertEqual(result["files"][0]["added"], 1)
            self.assertEqual(result["files"][0]["deleted"], 1)
            self.assertNotIn("patch", result)

    def test_show_correction_rejected_in_diff_strategy(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "def foo():\n    return 1\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "def foo():\n    return 2\n"})
            after = fixture.commit(tree, [base], "correction")

            tool = RepositoryTool(repository=VersionControl(fixture.repository), after=after)

            # diff strategy (default): the correction diff is in the seed already.
            with self.assertRaises(ValueError):
                tool.show(after)

    def test_show_correction_allowed_in_stat_strategy(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "def foo():\n    return 1\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "def foo():\n    return 2\n"})
            after = fixture.commit(tree, [base], "correction")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository), after=after, strategy="stat"
            )

            # stat strategy: the seed has no diff, so the agent may fetch the
            # correction itself.
            result = tool.show(after)
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
            target = fixture.commit(tree, [base], "bump both")
            tree = fixture.tree(
                {
                    "foo.py": "def foo():\n    return 2\n",
                    "bar.py": "def bar():\n    return 2\n",
                }
            )
            after = fixture.commit(tree, [target], "correction")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.show(target, path="foo.py")

            self.assertIn("foo.py", result["patch"])
            self.assertNotIn("bar.py", result["patch"])

    def test_blame_returns_origin_commit(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "alpha\nbeta\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "alpha\ngamma\n"})
            middle = fixture.commit(tree, [base], "replace beta")
            tree = fixture.tree({"foo.py": "alpha\ngamma\n"})
            after = fixture.commit(tree, [middle], "correction")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            first = tool.blame("foo.py", middle, 1, 1)
            self.assertEqual(
                first["blame"],
                f"L1: {base[:12]} | foo.py | alpha",
            )
            self.assertEqual(first["commits"], {base: "base"})

            second = tool.blame("foo.py", middle, 2, 2)
            self.assertEqual(
                second["blame"],
                f"L2: {middle[:12]} | foo.py | gamma",
            )
            self.assertEqual(second["commits"], {middle: "replace beta"})

    def test_blame_rejects_out_of_range_with_valid_interval(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "alpha\nbeta\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "alpha\ngamma\n"})
            after = fixture.commit(tree, [base], "correction")

            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            # foo.py has 2 lines at base; asking up to 400 must error clearly,
            # naming the valid range, not silently blank out every line.
            with self.assertRaises(ValueError) as ctx:
                tool.blame("foo.py", base, 1, 400)
            message = str(ctx.exception)
            self.assertIn("1-2", message)
            self.assertIn("2 lines", message)

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
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=target)
            self.assertEqual(
                result,
                [
                    {
                        "path": "foo.py",
                        "matches": "L1: needle",
                    }
                ],
            )

    def test_grep_groups_matches_by_file(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "needle\nother\nneedle\n",
                    "bar.py": "needle\n",
                }
            )
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=target)
            self.assertEqual(
                result,
                [
                    {
                        "path": "bar.py",
                        "matches": "L1: needle",
                    },
                    {
                        "path": "foo.py",
                        "matches": "L1: needle\nL3: needle",
                    },
                ],
            )

    def test_grep_respects_path_filter(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "needle\n",
                    "bar.py": "needle\n",
                }
            )
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=target, path="bar.py")
            self.assertEqual(
                result,
                [
                    {
                        "path": "bar.py",
                        "matches": "L1: needle",
                    }
                ],
            )

    def test_grep_respects_path_prefix_pattern(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "needle\n",
                    "pkg/bar.py": "needle\n",
                    "pkg/nested/baz.py": "needle\n",
                }
            )
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=target, path="pkg")
            self.assertEqual(
                [entry["path"] for entry in result],
                ["pkg/bar.py", "pkg/nested/baz.py"],
            )

    def test_grep_respects_path_glob_pattern(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "needle\n",
                    "pkg/bar.py": "needle\n",
                    "pkg/nested/baz.py": "needle\n",
                    "pkg/nested/qux.txt": "needle\n",
                }
            )
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=target, path="pkg/**/*.py")
            self.assertEqual(
                [entry["path"] for entry in result],
                ["pkg/bar.py", "pkg/nested/baz.py"],
            )

    def test_grep_treats_needle_as_regex(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "needle\nneedful\n"})
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("need.*", commit=target)
            self.assertEqual(
                result,
                [
                    {
                        "path": "foo.py",
                        "matches": "L1: needle\nL2: needful",
                    }
                ],
            )

    def test_grep_rejects_invalid_regex(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "needle\n"})
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            with self.assertRaises(ValueError):
                tool.grep("(", commit=target)

    def test_grep_applies_global_match_limit(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree(
                {
                    "foo.py": "needle\nneedle\nneedle\n",
                    "bar.py": "needle\nneedle\n",
                }
            )
            target = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [target], "correction")
            tool = RepositoryTool(
                repository=VersionControl(fixture.repository),
                after=after,
            )

            result = tool.grep("needle", commit=target, max=3)
            self.assertEqual(
                result,
                [
                    {
                        "path": "bar.py",
                        "matches": "L1: needle\nL2: needle",
                    },
                    {
                        "path": "foo.py",
                        "matches": "L1: needle",
                    },
                ],
            )

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

    def test_compress_patch_truncates_at_max_lines(self) -> None:
        lines = ["+" + f"line{i}" for i in range(250)]
        patch = "\n".join(
            ["diff --git a/f.py b/f.py", "--- a/f.py", "+++ b/f.py", "@@ -0,0 +1,250 @@"]
            + lines
        )
        compressed = RepositoryTool._compress_patch(patch)
        self.assertIn("diff lines omitted", compressed)
        self.assertLessEqual(len(compressed.split("\n")), 205)

    def test_compress_patch_strips_context_over_char_threshold(self) -> None:
        changed = ["+added_line"] * 40 + ["-removed_line"] * 40
        context = [" context_padding_" + "x" * 40] * 60
        interleaved: list[str] = []
        for i in range(40):
            interleaved.append(context[i])
            interleaved.append(changed[i])
        interleaved.extend(changed[40:])
        interleaved.extend(context[40:])
        patch = "\n".join(
            ["diff --git a/f.py b/f.py", "--- a/f.py", "+++ b/f.py", "@@ -1,100 +1,100 @@"]
            + interleaved
        )
        self.assertGreater(len(patch), 3000)
        self.assertLessEqual(len(patch.split("\n")), 200)
        compressed = RepositoryTool._compress_patch(patch)
        self.assertNotIn("context_padding", compressed)
        self.assertIn("+added_line", compressed)
        self.assertIn("-removed_line", compressed)

    def test_compress_patch_strips_context_and_truncates(self) -> None:
        changed = ["+" + "a" * 50] * 120
        context = [" " + "b" * 50] * 80
        all_lines = context[:40] + changed + context[40:]
        patch = "\n".join(
            ["diff --git a/f.py b/f.py", "--- a/f.py", "+++ b/f.py", "@@ -1,200 +1,200 @@"]
            + all_lines
        )
        self.assertGreater(len(patch), 3000)
        compressed = RepositoryTool._compress_patch(patch)
        self.assertIn("diff lines omitted", compressed)
        self.assertLessEqual(len(compressed.split("\n")), 85)

    def test_compress_patch_preserves_short_patch(self) -> None:
        patch = "\n".join([
            "diff --git a/f.py b/f.py",
            "--- a/f.py",
            "+++ b/f.py",
            "@@ -1,3 +1,3 @@",
            " context",
            "-old",
            "+new",
        ])
        compressed = RepositoryTool._compress_patch(patch)
        self.assertNotIn("omitted", compressed)
        self.assertIn(" context", compressed)
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
            target = fixture.commit(tree, [base], "edit")
            tree = fixture.tree({"foo.py": "x = 1\n"})
            after = fixture.commit(tree, [target], "correction")

            factory = RepositoryToolFactory(
                repository=VersionControl(fixture.repository),
            )
            tool = factory.create(base, after)

            show = tool.show(target)
            self.assertEqual(show["sha"], target)


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

    def test_preserves_trailing_blank_context(self) -> None:
        # A change whose last hunk ends in blank context lines must still render a
        # patch whose body matches its @@ header. A trailing strip() used to eat
        # those blank lines, leaving the final hunk shorter than promised and
        # unparseable downstream (PatchView / unidiff).
        from benchmark.attribution.diff.reference import PatchView

        # Edit followed by two blank lines, then an unchanged block too far away to
        # close the hunk: the last hunk's trailing context is the blank lines.
        base_body = (
            "def f():\n    x = 1\n    return x\n\n\n"
            "def g():\n    pass\n    pass\n    pass\n    pass\n"
        )
        after_body = (
            "def f():\n    x = 1\n    x += 1\n    return x\n\n\n"
            "def g():\n    pass\n    pass\n    pass\n    pass\n"
        )
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": base_body})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": after_body})
            after = fixture.commit(tree, [base], "edit before blank context")

            repository = VersionControl(fixture.repository)
            diff = repository.diff(base, after, context=3)
            rendered = DiffRenderer().render(diff, path="foo.py")

            # The blank context lines render as a lone space and must survive.
            self.assertTrue(rendered.endswith(" \n "))
            # Must not raise UnidiffParseError; the body has to match the header.
            PatchView(rendered)

    @staticmethod
    def _raw_tree(fixture, files: dict[str, bytes]):
        # HistoryFixture.tree() forces a trailing newline, which cannot reproduce
        # the no-newline-at-EOF marker; build the tree from raw blob bytes instead.
        import pygit2

        builder = fixture.repository.TreeBuilder()
        for name, content in sorted(files.items()):
            blob = fixture.repository.create_blob(content)
            builder.insert(name, blob, pygit2.GIT_FILEMODE_BLOB)
        return builder.write()

    def test_renders_no_newline_at_eof_marker(self) -> None:
        # When a revision lacks a trailing newline, pygit2 emits a synthetic line
        # whose origin is an EOFNL marker (< > =), not a real diff origin. Rendering
        # "{origin}{content}" used to put a bare ">" line into the body, which is not
        # valid unified-diff and made the patch unparseable downstream (PatchView).
        from benchmark.attribution.diff.reference import Numbering, PatchView

        with HistoryFixture() as fixture:
            tree = self._raw_tree(fixture, {"foo.py": b"a = 1\nb = 2\n"})
            base = fixture.commit(tree, [], "base")
            # after revision has no trailing newline -> ADD_EOFNL marker line.
            tree = self._raw_tree(fixture, {"foo.py": b"a = 1\nb = 3"})
            after = fixture.commit(tree, [base], "edit without trailing newline")

            repository = VersionControl(fixture.repository)
            diff = repository.diff(base, after, context=3)
            rendered = DiffRenderer().render(diff, path="foo.py")

            self.assertIn("\\ No newline at end of file", rendered)
            # No bare marker line, and the patch must parse.
            self.assertNotIn("\n>", rendered)
            self.assertNotIn("\n<", rendered)
            PatchView(rendered, numbering=Numbering.AFTER)

    def test_renders_full_file_deletion_without_trailing_newline(self) -> None:
        # A deleted file with no trailing newline combines a "+0,0" header with the
        # EOFNL marker on the deleted side (origin "<"). This is exactly the Olive
        # case that crashed the localization graph.
        from benchmark.attribution.diff.reference import Numbering, PatchView

        with HistoryFixture() as fixture:
            tree = self._raw_tree(
                fixture, {"foo.py": b"x = 1\ny = 2", "keep.py": b"z = 3\n"}
            )
            base = fixture.commit(tree, [], "base")
            tree = self._raw_tree(fixture, {"keep.py": b"z = 3\n"})
            after = fixture.commit(tree, [base], "delete foo.py")

            repository = VersionControl(fixture.repository)
            diff = repository.diff(base, after, context=3)
            rendered = DiffRenderer().render(diff, path="foo.py")

            self.assertIn("\\ No newline at end of file", rendered)
            PatchView(rendered, numbering=Numbering.AFTER)


class TestRepositoryToolValidation(unittest.TestCase):
    def _tool(self, fixture, after):
        return RepositoryTool(repository=VersionControl(fixture.repository), after=after)

    def test_max_must_be_positive(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "pass\n"})
            base = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [base], "correction")
            tool = self._tool(fixture, after)
            for call in (
                lambda: tool.pickaxe("x", path="foo.py", max=0),
                lambda: tool.trace("x", path="foo.py", max=0),
                lambda: tool.grep("x", base, max=0),
            ):
                with self.assertRaises(ValueError):
                    call()

    def test_trace_unsupported_language_names_python(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"README.md": "# hi\n"})
            base = fixture.commit(tree, [], "base")
            after = fixture.commit(tree, [base], "correction")
            tool = self._tool(fixture, after)
            with self.assertRaises(ValueError) as ctx:
                tool.trace("x", path="README.md")
            self.assertIn(".py", str(ctx.exception))

    def test_show_notes_unmodified_path(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "a\n", "bar.py": "b\n"})
            base = fixture.commit(tree, [], "base")
            tree = fixture.tree({"foo.py": "a2\n", "bar.py": "b\n"})
            target = fixture.commit(tree, [base], "touch foo")
            after = fixture.commit(tree, [target], "correction")
            tool = self._tool(fixture, after)
            result = tool.show(target, path="bar.py")
            self.assertEqual(result["patch"], "")
            self.assertIn("not modified", result["note"])


if __name__ == "__main__":
    unittest.main()
