from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from benchmark.attribution.domain import OperationType
from benchmark.attribution.history import VersionControl
from benchmark.attribution.history_fixture import HistoryFixture
from benchmark.attribution.knowledge import (
    KnowledgeAuditBuilder,
    KnowledgeCommit,
    KnowledgeCommitGit,
    KnowledgeEntity,
    KnowledgeEntityGumtree,
    KnowledgeEntityGit,
    KnowledgeFile,
    KnowledgeGraphBuilder,
    KnowledgeGraphStore,
)


class TestKnowledgeGraphBuilder(unittest.TestCase):
    def _entity(self, store, key: str) -> dict[str, object]:
        return store.graph.nodes[key]["payload"]

    def test_build_entity(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            after = fixture.commit(tree, [base], "after")

            store = KnowledgeGraphBuilder(
                repository=VersionControl(fixture.repository)
            ).build(base, after)

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

    def test_build_entity_resolve(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            after = fixture.commit(tree, [base], "after")

            with patch(
                "benchmark.attribution.knowledge.EntityPath.resolve",
                return_value=[
                    ("EDIT", "entity:function=foo", "entity:function=foo"),
                ],
            ):
                store = KnowledgeGraphBuilder(
                    repository=VersionControl(fixture.repository)
                ).build(base, after)

            self.assertEqual(
                [
                    (source, target, data["edge"])
                    for source, target, data in store.graph.edges(data=True)
                    if data["edge"] == "EDIT"
                ],
                [
                    (f"commit:{after}", "entity:a.py:function=foo", "EDIT"),
                ],
            )

    def test_build_fallback(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            after = fixture.commit(tree, [base], "after")

            with patch(
                "benchmark.attribution.knowledge.EntityPath.resolve",
                side_effect=ValueError("boom"),
            ), patch(
                "benchmark.attribution.knowledge.KnowledgeEntityGit.build",
                return_value=[
                    KnowledgeEntity(
                        sha=after,
                        path="a.py",
                        urn="entity:function=foo",
                        kind="function",
                        name="foo",
                        operation=OperationType.EDIT,
                        source="git",
                    )
                ],
            ) as fallback:
                store = KnowledgeGraphBuilder(
                    repository=VersionControl(fixture.repository)
                ).build(base, after)

            self.assertTrue(fallback.called)
            self.assertIn("entity:a.py:function=foo", store.graph.nodes)
            self.assertIn("file:a.py", store.graph.nodes)
            self.assertIn(f"commit:{after}", store.graph.nodes)

    def test_build_fallback_global(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({
                "a.py": """
                    def foo():
                        return 1
                """,
                "b.py": """
                    def bar():
                        return 1
                """,
            })
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({
                "a.py": """
                    def foo():
                        return 2
                """,
                "b.py": """
                    def bar():
                        return 2
                """,
            })
            after = fixture.commit(tree, [base], "after")

            with patch(
                "benchmark.attribution.knowledge.EntityPath.resolve",
                side_effect=ValueError("boom"),
            ), patch(
                "benchmark.attribution.knowledge.KnowledgeEntityGit.build",
                return_value=[
                    KnowledgeEntity(
                        sha=after,
                        path="a.py",
                        urn="entity:function=foo",
                        kind="function",
                        name="foo",
                        operation=OperationType.EDIT,
                        source="git",
                    ),
                    KnowledgeEntity(
                        sha=after,
                        path="b.py",
                        urn="entity:function=bar",
                        kind="function",
                        name="bar",
                        operation=OperationType.EDIT,
                        source="git",
                    ),
                ],
            ) as fallback:
                store = KnowledgeGraphBuilder(
                    repository=VersionControl(fixture.repository)
                ).build(base, after)

            self.assertEqual(fallback.call_count, 1)
            files = fallback.call_args.args[0]
            self.assertEqual(sorted({file.path for file in files}), ["a.py", "b.py"])
            self.assertIn("entity:a.py:function=foo", store.graph.nodes)
            self.assertIn("entity:b.py:function=bar", store.graph.nodes)

    def test_build_deduplicate(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            after = fixture.commit(tree, [base], "after")

            with patch(
                "benchmark.attribution.knowledge.EntityPath.resolve",
                return_value=[
                    ("EDIT", "entity:function=foo", "entity:function=foo"),
                    ("EDIT", "entity:function=foo", "entity:function=foo"),
                ],
            ):
                store = KnowledgeGraphBuilder(
                    repository=VersionControl(fixture.repository)
                ).build(base, after)

            self.assertEqual(
                [
                    (source, target, data["edge"])
                    for source, target, data in store.graph.edges(data=True)
                    if data["edge"] == "EDIT"
                ],
                [
                    (f"commit:{after}", "entity:a.py:function=foo", "EDIT"),
                ],
            )

    def test_build_graph(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.txt": "x = 1\ny = 2\n"})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.txt": "x = 1\ny = 3\n"})
            after = fixture.commit(tree, [base], "after")

            store = KnowledgeGraphBuilder(
                repository=VersionControl(fixture.repository)
            ).build(base, after)

            self.assertIn(f"commit:{after}", store.graph.nodes)
            self.assertIn(f"commit:{base}", store.graph.nodes)
            self.assertIn("file:a.txt", store.graph.nodes)
            self.assertIn(
                (f"commit:{after}", "file:a.txt", "MODIFIES"),
                [
                    (source, target, data["edge"])
                    for source, target, data in store.graph.edges(data=True)
                ],
            )

    def test_build_graph_add_only_seed(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            after = fixture.commit(tree, [base], "after")

            store = KnowledgeGraphBuilder(
                repository=VersionControl(fixture.repository)
            ).build(base, after)

            self.assertIn(f"commit:{after}", store.graph.nodes)
            self.assertIn("file:a.py", store.graph.nodes)
            self.assertIn(
                (f"commit:{after}", "file:a.py", "MODIFIES"),
                [
                    (source, target, data["edge"])
                    for source, target, data in store.graph.edges(data=True)
                ],
            )
            self.assertTrue(
                any(
                    source == f"commit:{after}"
                    and target.startswith("entity:a.py:")
                    and data["edge"] == "ADD"
                    for source, target, data in store.graph.edges(data=True)
                )
            )

    def test_build_skip_java(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"A.java": """
                class Foo {
                    int foo() { return 1; }
                }
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"A.java": """
                class Foo {
                    int foo() { return 2; }
                }
            """})
            after = fixture.commit(tree, [base], "after")

            with patch(
                "benchmark.attribution.knowledge.EntityPath.resolve",
                side_effect=AssertionError("java should be skipped"),
            ), patch(
                "benchmark.attribution.knowledge.KnowledgeEntityGit.build",
                side_effect=AssertionError("java should not trigger fallback"),
            ):
                store = KnowledgeGraphBuilder(
                    repository=VersionControl(fixture.repository)
                ).build(base, after)

            self.assertIn("file:A.java", store.graph.nodes)
            self.assertEqual(
                sorted(node for node in store.graph.nodes if node.startswith("entity:")),
                [],
            )

    def test_store_roundtrip(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            after = fixture.commit(tree, [base], "after")

            store = KnowledgeGraphBuilder(
                repository=VersionControl(fixture.repository)
            ).build(base, after)

            loaded = KnowledgeGraphStore.load(store.dump())

            self.assertEqual(loaded.base, store.base)
            self.assertEqual(loaded.after, store.after)
            self.assertEqual(
                sorted(loaded.graph.nodes),
                sorted(store.graph.nodes),
            )
            for node in store.graph.nodes:
                self.assertEqual(loaded.graph.nodes[node], store.graph.nodes[node])
            self.assertEqual(
                sorted(loaded.graph.edges(keys=True)),
                sorted(store.graph.edges(keys=True)),
            )
            for source, target, key, data in store.graph.edges(keys=True, data=True):
                self.assertEqual(
                    loaded.graph.edges[source, target, key],
                    data,
                )

    def test_audit_entity(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            after = fixture.commit(tree, [base], "after")

            audit = KnowledgeAuditBuilder(
                repository=VersionControl(fixture.repository)
            ).build(base, after)

            self.assertEqual(audit.base, base)
            self.assertEqual(audit.after, after)
            self.assertEqual(audit.commits, 2)
            self.assertEqual(audit.files, 1)
            self.assertEqual(audit.entities, 1)
            self.assertEqual(audit.failures, [])

    def test_audit_failure(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            after = fixture.commit(tree, [base], "after")

            with patch(
                "benchmark.attribution.knowledge.EntityPath.resolve",
                side_effect=ValueError("boom"),
            ):
                audit = KnowledgeAuditBuilder(
                    repository=VersionControl(fixture.repository)
                ).build(base, after)

            self.assertEqual(audit.commits, 2)
            self.assertEqual(audit.files, 1)
            self.assertEqual(audit.entities, 0)
            self.assertEqual(len(audit.failures), 1)
            self.assertEqual(audit.failures[0].kind, "entity")
            self.assertEqual(audit.failures[0].type, "ValueError")
            self.assertEqual(audit.failures[0].message, "boom")
            self.assertEqual(audit.failures[0].sha, after)
            self.assertEqual(audit.failures[0].parent, base)
            self.assertEqual(audit.failures[0].path, "a.py")
            self.assertEqual(audit.failures[0].language, "python")


class TestKnowledgeEntityGit(unittest.TestCase):
    def _patch(self, old, new, headers):
        return SimpleNamespace(
            delta=SimpleNamespace(
                old_file=SimpleNamespace(path=old),
                new_file=SimpleNamespace(path=new),
            ),
            hunks=[SimpleNamespace(header=header) for header in headers],
        )

    def _repository(self, patches):
        return SimpleNamespace(
            diff=lambda base, after: patches,
            parents=lambda sha: ["base"],
        )

    def _file(
        self,
        *,
        path: str,
        language: str | None,
        base: str | None = None,
        after: str | None = None,
    ) -> KnowledgeFile:
        return KnowledgeFile(
            sha="after",
            base=path if base is None else base,
            after=path if after is None else after,
            path=path,
            language=language,
            operation=OperationType.EDIT,
        )

    def test_build_python_function(self) -> None:
        entity = KnowledgeEntityGit(
            self._repository(
                [self._patch("a.py", "a.py", ["@@ -1,2 +1,2 @@ def foo():\n"])]
            )
        ).build([self._file(path="a.py", language="python")])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.py",
                    urn="entity:function=foo",
                    kind="function",
                    name="foo",
                    operation=OperationType.EDIT,
                    source="git",
                )
            ],
        )

    def test_build_python_class(self) -> None:
        entity = KnowledgeEntityGit(
            self._repository(
                [self._patch("a.py", "a.py", ["@@ -1,2 +1,2 @@ class Foo(Base):\n"])]
            )
        ).build([self._file(path="a.py", language="python")])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.py",
                    urn="entity:class=Foo",
                    kind="class",
                    name="Foo",
                    operation=OperationType.EDIT,
                    source="git",
                )
            ],
        )

    def test_build_javascript_function(self) -> None:
        entity = KnowledgeEntityGit(
            self._repository(
                [
                    self._patch(
                        "a.js",
                        "a.js",
                        ["@@ -1,2 +1,2 @@ export async function foo(value) {\n"],
                    )
                ]
            )
        ).build([self._file(path="a.js", language="javascript")])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.js",
                    urn="entity:function=foo",
                    kind="function",
                    name="foo",
                    operation=OperationType.EDIT,
                    source="git",
                )
            ],
        )

    def test_build_empty_header(self) -> None:
        entity = KnowledgeEntityGit(
            self._repository([self._patch("a.py", "a.py", ["@@ -1,2 +1,2 @@\n"])])
        ).build([self._file(path="a.py", language="python")])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.py",
                    urn="entity:program",
                    kind="program",
                    name="a",
                    operation=OperationType.EDIT,
                    source="git",
                )
            ],
        )

    def test_build_add_file(self) -> None:
        file = KnowledgeFile(
            sha="after",
            base="a.py",
            after="a.py",
            path="a.py",
            language="python",
            operation=OperationType.ADD,
        )
        entity = KnowledgeEntityGit(
            self._repository([])
        ).build([file])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.py",
                    urn="entity:program",
                    kind="program",
                    name="a",
                    operation=OperationType.ADD,
                    source="git",
                )
            ],
        )

    def test_build_add_file_diff(self) -> None:
        file = KnowledgeFile(
            sha="after",
            base=None,
            after="a.py",
            path="a.py",
            language="python",
            operation=OperationType.ADD,
        )
        entity = KnowledgeEntityGit(
            self._repository([])
        ).build([file])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.py",
                    urn="entity:program",
                    kind="program",
                    name="a",
                    operation=OperationType.ADD,
                    source="git",
                )
            ],
        )

    def test_build_delete_file(self) -> None:
        file = KnowledgeFile(
            sha="after",
            base="a.py",
            after=None,
            path="a.py",
            language="python",
            operation=OperationType.DELETE,
        )
        entity = KnowledgeEntityGit(
            self._repository([])
        ).build([file])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.py",
                    urn="entity:program",
                    kind="program",
                    name="a",
                    operation=OperationType.DELETE,
                    source="git",
                )
            ],
        )

    def test_build_empty_header_deduplicate(self) -> None:
        entity = KnowledgeEntityGit(
            self._repository(
                [
                    self._patch(
                        "a.py",
                        "a.py",
                        [
                            "@@ -1,2 +1,2 @@\n",
                            "@@ -3,4 +3,4 @@\n",
                        ],
                    )
                ]
            )
        ).build([self._file(path="a.py", language="python")])

        self.assertEqual(len(entity), 1)
        self.assertEqual(entity[0].urn, "entity:program")
        self.assertEqual(entity[0].operation, OperationType.EDIT)

    def test_build_deduplicate(self) -> None:
        entity = KnowledgeEntityGit(
            self._repository(
                [
                    self._patch(
                        "a.py",
                        "a.py",
                        [
                            "@@ -1,2 +1,2 @@ def foo():\n",
                            "@@ -3,4 +3,4 @@ def foo():\n",
                        ],
                    )
                ]
            )
        ).build([self._file(path="a.py", language="python")])

        self.assertEqual(len(entity), 1)
        self.assertEqual(entity[0].urn, "entity:function=foo")


class TestKnowledgeEntityGumtree(unittest.TestCase):
    def test_build_falls_back_to_program_when_resolve_is_empty(self) -> None:
        file = KnowledgeFile(
            sha="after",
            base="a.js",
            after="a.js",
            path="a.js",
            language="javascript",
            operation=OperationType.EDIT,
        )
        repository = SimpleNamespace(
            blob=MagicMock(side_effect=FileNotFoundError),
            parents=lambda sha: ["base"],
        )

        with patch(
            "benchmark.attribution.knowledge.EntityPath.resolve",
            return_value=set(),
        ):
            entity = KnowledgeEntityGumtree(repository).build([file])

        self.assertEqual(
            entity,
            [
                KnowledgeEntity(
                    sha="after",
                    path="a.js",
                    urn="entity:program",
                    kind="program",
                    name="a",
                    operation=OperationType.EDIT,
                    source="gumtree",
                )
            ],
        )


class TestKnowledgeCommitGit(unittest.TestCase):
    def _commit(self, commits, sha):
        for commit in commits:
            if commit.sha == sha:
                return commit
        raise KeyError(sha)

    def test_build_keeps_seed_without_trace(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "x = 1\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "x = 2\n"})
            foo = fixture.commit(tree, [init], "foo")

            tree = fixture.tree({"a.py": "x = 2\ny = 1\n"})
            base = fixture.commit(tree, [foo], "base")

            tree = fixture.tree({"a.py": "x = 3\ny = 1\n"})
            after = fixture.commit(tree, [base], "after")

            repository = VersionControl(fixture.repository)
            with patch(
                "benchmark.attribution.knowledge.VersionControlPathWalker",
                side_effect=FileNotFoundError("mocked"),
            ):
                commits = KnowledgeCommitGit(repository, cap=200).build(base, after)

            shas = {c.sha for c in commits}
            self.assertIn(foo, shas)
            self.assertIn(after, shas)

            blame = self._commit(commits, foo)
            self.assertEqual(blame.category, "blame")
            self.assertEqual(blame.kind, "blame")
            self.assertEqual(blame.score, 1.0)
            self.assertIn("a.py", blame.files)

    def test_build_blame(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "x = 1\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "x = 2\n"})
            foo = fixture.commit(tree, [init], "foo")

            tree = fixture.tree({"a.py": "x = 2\ny = 1\n"})
            base = fixture.commit(tree, [foo], "base")

            tree = fixture.tree({"a.py": "x = 3\ny = 1\n"})
            after = fixture.commit(tree, [base], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(base, after)
            shas = {c.sha for c in commits}

            self.assertIn(foo, shas)
            self.assertIn(after, shas)

            blame = self._commit(commits, foo)
            self.assertEqual(blame.category, "blame")
            self.assertEqual(blame.kind, "blame")
            self.assertIn("a.py", blame.files)
            self.assertEqual(blame.score, 1.0)

            correction = self._commit(commits, after)
            self.assertEqual(correction.category, "correction")
            self.assertEqual(correction.kind, "correction")
            self.assertEqual(correction.score, 0.0)

    def test_build_blame_mixed_hunk(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "head\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "head\none\n"})
            foo = fixture.commit(tree, [init], "foo")

            tree = fixture.tree({"a.py": "head\none\ntwo\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "head\none\ntwo\nkeep\n"})
            base = fixture.commit(tree, [bar], "base")

            tree = fixture.tree({"a.py": "head\nkeep\n"})
            after = fixture.commit(tree, [base], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(base, after)

            blame_foo = self._commit(commits, foo)
            blame_bar = self._commit(commits, bar)

            self.assertEqual(blame_foo.category, "blame")
            self.assertEqual(blame_bar.category, "blame")
            self.assertEqual(blame_foo.score, 1.0)
            self.assertEqual(blame_bar.score, 1.0)
            self.assertIn("a.py", blame_foo.files)
            self.assertIn("a.py", blame_bar.files)

    def test_build_add_only_seed(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            after = fixture.commit(tree, [base], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(base, after)

            self.assertEqual([commit.sha for commit in commits], [after])
            correction = self._commit(commits, after)
            self.assertEqual(correction.category, "correction")
            self.assertEqual(correction.kind, "correction")
            self.assertEqual(correction.files, frozenset({"a.py"}))

    def test_build_merge_hides_merge_commit(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "left\n"})
            left = fixture.commit(tree, [init], "left")

            tree = fixture.tree({
                "a.py": "base\n",
                "b.py": "right\n",
            })
            right = fixture.commit(tree, [init], "right", ref=None)

            tree = fixture.tree({
                "a.py": "left\n",
                "b.py": "right\n",
            })
            merge = fixture.commit(tree, [left, right], "merge")

            tree = fixture.tree({
                "a.py": "after\n",
                "b.py": "right\n",
            })
            after = fixture.commit(tree, [merge], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(merge, after)

            self.assertNotIn(merge, {commit.sha for commit in commits})
            self.assertIn(left, {commit.sha for commit in commits})
            self.assertIn("a.py", self._commit(commits, left).files)

    def test_build_merge_follows_second_parent(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({
                "a.py": "base\n",
                "b.py": "left\n",
            })
            left = fixture.commit(tree, [init], "left")

            tree = fixture.tree({"a.py": "right\n"})
            right = fixture.commit(tree, [init], "right", ref=None)

            tree = fixture.tree({
                "a.py": "right\n",
                "b.py": "left\n",
            })
            merge = fixture.commit(tree, [left, right], "merge")

            tree = fixture.tree({
                "a.py": "after\n",
                "b.py": "left\n",
            })
            after = fixture.commit(tree, [merge], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(merge, after)

            self.assertNotIn(merge, {commit.sha for commit in commits})
            self.assertIn(right, {commit.sha for commit in commits})
            self.assertIn("a.py", self._commit(commits, right).files)

    def test_build_score(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "x = 1\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "x = 2\n"})
            foo = fixture.commit(tree, [init], "foo")

            tree = fixture.tree({"a.py": "x = 3\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "x = 3\ny = 1\n"})
            baz = fixture.commit(tree, [bar], "baz")

            tree = fixture.tree({"a.py": "x = 3\ny = 1\nz = 1\n"})
            base = fixture.commit(tree, [baz], "base")

            tree = fixture.tree({"a.py": "x = 4\ny = 1\nz = 1\n"})
            after = fixture.commit(tree, [base], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(base, after)

            self.assertEqual(self._commit(commits, after).score, 0.0)
            self.assertEqual(self._commit(commits, bar).score, 1.0)
            self.assertEqual(self._commit(commits, foo).score, 0.6)
            self.assertEqual(self._commit(commits, baz).score, 0.3)

    def test_build_parents(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.js": """
                function foo() {
                    return 1;
                }
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.js": """
                function foo() {
                    return 2;
                }
            """})
            foo = fixture.commit(tree, [base], "foo")

            tree = fixture.tree({"a.js": """
                function foo() {
                    return 3;
                }
            """})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.js": """
                function foo() {
                    return 4;
                }
            """})
            after = fixture.commit(tree, [bar], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(base, after)

            self.assertEqual(self._commit(commits, foo).parents, (base,))
            self.assertEqual(self._commit(commits, bar).parents, (foo,))
            self.assertEqual(self._commit(commits, after).parents, (bar,))

    def test_build_edges(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "x = 1\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "x = 2\n"})
            foo = fixture.commit(tree, [init], "foo")

            tree = fixture.tree({"a.py": "x = 2\ny = 1\n"})
            base = fixture.commit(tree, [foo], "base")

            tree = fixture.tree({"a.py": "x = 3\ny = 1\n"})
            after = fixture.commit(tree, [base], "after")

            store = KnowledgeGraphBuilder(
                repository=VersionControl(fixture.repository)
            ).build(base, after)

            edges = [
                (source, target, data["edge"])
                for source, target, data in store.graph.edges(data=True)
            ]
            precedes = [
                (s, t) for s, t, e in edges if e == "PRECEDES"
            ]
            blame = [
                (s, t) for s, t, e in edges if e == "BLAME"
            ]
            self.assertTrue(
                any(s == f"commit:{foo}" for s, _ in precedes),
                "foo should have a PRECEDES edge",
            )
            self.assertIn((f"commit:{after}", f"commit:{foo}"), blame)

    def test_build_payload_parents_follow_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "left\n"})
            left = fixture.commit(tree, [init], "left")

            tree = fixture.tree({"a.py": "right\n"})
            right = fixture.commit(tree, [init], "right", ref=None)

            tree = fixture.tree({"a.py": "merge\n"})
            merge = fixture.commit(tree, [left, right], "merge")

            store = KnowledgeGraphBuilder(
                repository=VersionControl(fixture.repository)
            ).build(left, merge)

            self.assertEqual(
                store.payload(f"commit:{merge}")["parents"],
                (left, right),
            )

    def test_fragment(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "left\n"})
            left = fixture.commit(tree, [init], "left")

            tree = fixture.tree({"a.py": "right\n"})
            right = fixture.commit(tree, [init], "right", ref=None)

            tree = fixture.tree({"a.py": "merge\n"})
            merge = fixture.commit(tree, [left, right], "merge")

            store = KnowledgeGraphStore()
            store.add(
                "commit",
                {
                    "sha": init,
                    "type": "candidate",
                    "score": 0.0,
                    "timestamp": datetime.fromtimestamp(0),
                    "message": "init",
                    "parents": (),
                    "files": (),
                },
            )
            store.add(
                "commit",
                {
                    "sha": left,
                    "type": "candidate",
                    "score": 0.0,
                    "timestamp": datetime.fromtimestamp(1),
                    "message": "left",
                    "parents": (init,),
                    "files": (),
                },
            )
            store.add(
                "commit",
                {
                    "sha": right,
                    "type": "candidate",
                    "score": 0.0,
                    "timestamp": datetime.fromtimestamp(2),
                    "message": "right",
                    "parents": (init,),
                    "files": (),
                },
            )
            store.add(
                "commit",
                {
                    "sha": merge,
                    "type": "candidate",
                    "score": 0.0,
                    "timestamp": datetime.fromtimestamp(3),
                    "message": "merge",
                    "parents": (left, right),
                    "files": (),
                },
            )

            self.assertEqual(
                store.fragment(),
                {
                    init: [],
                    left: [init],
                    right: [init],
                    merge: [left, right],
                },
            )

    def test_fragment_adds_missing_parent_stub(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "child\n"})
            child = fixture.commit(tree, [init], "child")

            store = KnowledgeGraphStore()
            store.add(
                "commit",
                {
                    "sha": child,
                    "type": "candidate",
                    "score": 0.0,
                    "timestamp": datetime.fromtimestamp(1),
                    "message": "child",
                    "parents": (init,),
                    "files": (),
                },
            )

            self.assertEqual(store.payload(f"commit:{child}")["parents"], (init,))
            self.assertEqual(store.fragment(), {child: [init], init: []})

    def test_build_limit(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": """
                def foo():
                    return 1
            """})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 2
            """})
            foo = fixture.commit(tree, [base], "foo")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 3
            """})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 4
            """})
            baz = fixture.commit(tree, [bar], "baz")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 5
            """})
            qux = fixture.commit(tree, [baz], "qux")

            tree = fixture.tree({"a.py": """
                def foo():
                    return 6
            """})
            after = fixture.commit(tree, [qux], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=2).build(base, after)
            shas = {c.sha for c in commits}

            self.assertIn(after, shas)
            self.assertIn(qux, shas)
            self.assertLessEqual(len(commits), 3)

    def test_build_limit_prefers_newer_timestamp(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": "old\n"})
            old = fixture.commit(tree, [base], "old")

            tree = fixture.tree({"a.py": "mid\n"})
            mid = fixture.commit(tree, [old], "mid")

            tree = fixture.tree({"a.py": "new\n"})
            new = fixture.commit(tree, [mid], "new")

            tree = fixture.tree({"a.py": "after\n"})
            after = fixture.commit(tree, [new], "after")

            repository = VersionControl(fixture.repository)
            builder = KnowledgeCommitGit(repository, cap=1)
            commits = [
                KnowledgeCommit(
                    sha=after,
                    kind="correction",
                    score=0.0,
                    timestamp=builder._commit(after)["timestamp"],
                    message="after",
                    parents=(),
                    predecessors=(),
                    files=frozenset({"a.py"}),
                    category="correction",
                ),
                KnowledgeCommit(
                    sha=old,
                    kind="candidate",
                    score=0.6,
                    timestamp=builder._commit(old)["timestamp"],
                    message="old",
                    parents=(),
                    predecessors=(),
                    files=frozenset({"a.py"}),
                    category=None,
                ),
                KnowledgeCommit(
                    sha=new,
                    kind="candidate",
                    score=0.6,
                    timestamp=builder._commit(new)["timestamp"],
                    message="new",
                    parents=(),
                    predecessors=(),
                    files=frozenset({"a.py"}),
                    category=None,
                ),
            ]

            limited = builder._limit(after, commits)

            self.assertIn(after, {commit.sha for commit in limited})
            self.assertIn(new, {commit.sha for commit in limited})
            self.assertNotIn(old, {commit.sha for commit in limited})

    def test_build_limit_timestamp_tie_is_deterministic(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({"a.py": "left\n"})
            left = fixture.commit(tree, [base], "left")

            tree = fixture.tree({"a.py": "right\n"})
            right = fixture.commit(tree, [left], "right")

            tree = fixture.tree({"a.py": "after\n"})
            after = fixture.commit(tree, [right], "after")

            repository = VersionControl(fixture.repository)
            builder = KnowledgeCommitGit(repository, cap=1)
            timestamp = builder._commit(left)["timestamp"]
            commits = [
                KnowledgeCommit(
                    sha=after,
                    kind="correction",
                    score=0.0,
                    timestamp=builder._commit(after)["timestamp"],
                    message="after",
                    parents=(),
                    predecessors=(),
                    files=frozenset({"a.py"}),
                    category="correction",
                ),
                KnowledgeCommit(
                    sha="bbb",
                    kind="candidate",
                    score=0.6,
                    timestamp=timestamp,
                    message="bbb",
                    parents=(),
                    predecessors=(),
                    files=frozenset({"a.py"}),
                    category=None,
                ),
                KnowledgeCommit(
                    sha="aaa",
                    kind="candidate",
                    score=0.6,
                    timestamp=timestamp,
                    message="aaa",
                    parents=(),
                    predecessors=(),
                    files=frozenset({"a.py"}),
                    category=None,
                ),
            ]

            limited = builder._limit(after, commits)

            self.assertEqual([commit.sha for commit in limited], [after, "aaa"])

    def test_build_exclude(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({
                "a.py": "x = 1\n",
                "b.py": "y = 1\n",
            })
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({
                "a.py": "x = 1\n",
                "b.py": "y = 2\n",
            })
            foo = fixture.commit(tree, [base], "foo")

            tree = fixture.tree({
                "a.py": "x = 2\n",
                "b.py": "y = 1\n",
            })
            after = fixture.commit(tree, [foo], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(base, after)
            shas = {c.sha for c in commits}

            self.assertNotIn(foo, shas)

    def test_build_files(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({
                "a.py": """
                    def foo():
                        return 1
                """,
                "b.js": """
                    function bar() {
                        return 1;
                    }
                """,
            })
            base = fixture.commit(tree, [], "base")

            tree = fixture.tree({
                "a.py": """
                    def foo():
                        return 2
                """,
                "b.js": """
                    function bar() {
                        return 2;
                    }
                """,
            })
            foo = fixture.commit(tree, [base], "foo")

            tree = fixture.tree({
                "a.py": """
                    def foo():
                        return 3
                """,
                "b.js": """
                    function bar() {
                        return 3;
                    }
                """,
            })
            after = fixture.commit(tree, [foo], "after")

            repository = VersionControl(fixture.repository)
            commits = KnowledgeCommitGit(repository, cap=200).build(base, after)

            blamed = self._commit(commits, foo)
            self.assertIn("a.py", blamed.files)
            self.assertIn("b.js", blamed.files)


if __name__ == "__main__":
    unittest.main()
