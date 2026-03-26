from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from langchain_core.tools import BaseTool

from benchmark.attribution.agent import KnowledgeGraphTool
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

            self.assertEqual(agent.meta(after)["payload"]["sha"], after)
            self.assertEqual(
                agent.content(after, "a.py")["path"],
                "a.py",
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
            self.assertIn("@@", result["patch"])

    def test_tools(self) -> None:
        agent = KnowledgeGraphTool(
            repository=object.__new__(VersionControl),
            store=KnowledgeGraphStore(),
        )

        tools = agent.tools()

        self.assertEqual([tool.name for tool in tools], ["candidates", "neighbors", "meta", "content"])
        self.assertTrue(all(isinstance(tool, BaseTool) for tool in tools))
        self.assertIn("top-ranked commit candidates", tools[0].description)

    def test_budget(self) -> None:
        agent = KnowledgeGraphTool(
            repository=object.__new__(VersionControl),
            store=KnowledgeGraphStore(),
        )
        agent._reads = 30

        with self.assertRaises(ValueError):
            agent.content("sha")


if __name__ == "__main__":
    unittest.main()
