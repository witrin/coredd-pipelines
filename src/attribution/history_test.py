import json
import shutil
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pygit2

from benchmark.attribution.diff.reference import PatchFactory
from benchmark.attribution.domain import Change, Commit, FileOperation, OperationType
from benchmark.attribution.history import (
    PlatformCache,
    VersionControl,
    VersionControlPathWalker,
    VersionControlSnapshot,
)
from benchmark.attribution.history_fixture import HistoryFixture


class TestVersionControlPathWalkerSubmodule(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def _tree(self) -> pygit2.Oid:
        repository = self._fixture.repository
        submodule = repository.TreeBuilder()
        submodule.insert("bar", pygit2.Oid(hex="1" * 40), 0o160000)
        nested = submodule.write()

        root = repository.TreeBuilder()
        root.insert("foo", nested, pygit2.GIT_FILEMODE_TREE)
        return root.write()

    def test_entries_ignore_missing_gitlink_object(self) -> None:
        foo = self._fixture.commit(self._tree(), [], "foo")

        repository = self._fixture.repository
        root = repository.TreeBuilder(self._tree())
        baz = repository.create_blob(b"baz\n")
        root.insert("baz.py", baz, pygit2.GIT_FILEMODE_BLOB)
        bar = self._fixture.commit(root.write(), [foo], "bar")

        walker = VersionControlPathWalker(repository, bar, "baz.py", 10)
        result = list(walker.entries())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].operation, OperationType.ADD)
        self.assertEqual(result[0].base, None)
        self.assertEqual(result[0].after, "baz.py")


class TestVersionControlParents(unittest.TestCase):
    """Tests parents() across merge, squash and rebase strategies.

    Repository layout:

        foo --- bar --- quux --- corge --- grault --- garply --- waldo
                \\              /
                 baz --- qux
    """

    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"foo.txt": "foo"})
        self.sha_foo = self._fixture.commit(tree, [], "foo")

        tree = self._fixture.tree({"foo.txt": "foo\nbar"})
        self.sha_bar = self._fixture.commit(tree, [self.sha_foo], "bar")

        tree = self._fixture.tree({"foo.txt": "foo\nbar\nbaz"})
        self.sha_baz = self._fixture.commit(tree, [self.sha_bar], "baz", ref=None)

        tree = self._fixture.tree({"foo.txt": "foo\nbar\nbaz\nqux"})
        self.sha_qux = self._fixture.commit(tree, [self.sha_baz], "qux", ref=None)

        tree = self._fixture.tree({"foo.txt": "foo\nbar\nquux"})
        self.sha_quux = self._fixture.commit(tree, [self.sha_bar], "quux")

        tree = self._fixture.tree({"foo.txt": "foo\nbar\nquux\nbaz\nqux"})
        self.sha_corge = self._fixture.commit(
            tree, [self.sha_quux, self.sha_qux],
            "corge",
        )

        tree = self._fixture.tree({"foo.txt": "foo\nbar\nquux\nbaz\nqux\ngrault"})
        self.sha_grault = self._fixture.commit(tree, [self.sha_corge], "grault")

        tree = self._fixture.tree({"foo.txt": "foo\nbar\nquux\nbaz\nqux\ngrault\ngarply"})
        self.sha_garply = self._fixture.commit(tree, [self.sha_grault], "garply")

        tree = self._fixture.tree(
            {"foo.txt": "foo\nbar\nquux\nbaz\nqux\ngrault\ngarply\nwaldo"}
        )
        self.sha_waldo = self._fixture.commit(tree, [self.sha_garply], "waldo")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_parents_root(self) -> None:
        self.assertEqual(self._candidate.parents(self.sha_foo), [])

    def test_parents_merge(self) -> None:
        result = self._candidate.parents(self.sha_corge)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], self.sha_quux)
        self.assertEqual(result[1], self.sha_qux)

    def test_parents_squash(self) -> None:
        result = self._candidate.parents(self.sha_grault)
        self.assertEqual(result, [self.sha_corge])

    def test_parents_rebase(self) -> None:
        self.assertEqual(self._candidate.parents(self.sha_waldo), [self.sha_garply])
        self.assertEqual(self._candidate.parents(self.sha_garply), [self.sha_grault])

    def test_parents_feature(self) -> None:
        self.assertEqual(self._candidate.parents(self.sha_qux), [self.sha_baz])
        self.assertEqual(self._candidate.parents(self.sha_baz), [self.sha_bar])


class TestVersionControlBlame(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"foo.txt": "foo\nbar\nbaz"})
        self.sha_foo = self._fixture.commit(tree, [], "foo")

        tree = self._fixture.tree({"foo.txt": "foo\nqux\nbaz"})
        self.sha_bar = self._fixture.commit(tree, [self.sha_foo], "bar")

        tree = self._fixture.tree({"foo.txt": "foo\nqux\nbaz\nquux"})
        self.sha_baz = self._fixture.commit(tree, [self.sha_bar], "baz")

        tree = self._fixture.tree({"foo.txt": "foo\ncorge\nbaz\nquux"})
        self.sha_qux = self._fixture.commit(tree, [self.sha_baz], "qux")

        tree = self._fixture.tree({"foo.txt": "foo\ncorge\nbaz\nquux\ngrault"})
        self.sha_quux = self._fixture.commit(tree, [self.sha_qux], "quux")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_blame_order(self) -> None:
        result = self._candidate.blame(
            "foo.txt",
            self.sha_quux,
            [(1, 1), (2, 2), (5, 5)],
        )
        self.assertEqual(
            result,
            [
                (self.sha_foo, "foo.txt", (1, 1)),
                (self.sha_qux, "foo.txt", (2, 2)),
                (self.sha_quux, "foo.txt", (5, 5)),
            ],
        )

    def test_blame_line(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_quux, [(2, 2)])
        self.assertEqual(result, [(self.sha_qux, "foo.txt", (2, 2))])

    def test_blame_unchanged(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_quux, [(1, 1)])
        self.assertEqual(result, [(self.sha_foo, "foo.txt", (1, 1))])

    def test_blame_mixed(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_quux, [(1, 5)])
        self.assertEqual(
            result,
            [
                (self.sha_foo, "foo.txt", (1, 1)),
                (self.sha_qux, "foo.txt", (2, 2)),
                (self.sha_foo, "foo.txt", (3, 3)),
                (self.sha_baz, "foo.txt", (4, 4)),
                (self.sha_quux, "foo.txt", (5, 5)),
            ],
        )

    def test_blame_range(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_quux, [(9, 9)])
        self.assertEqual(result, [(None, None, None)])


class TestVersionControlMaterialize(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()
        self._outdir = tempfile.TemporaryDirectory()

        tree = self._fixture.tree({"foo.txt": "foo content"})
        self.sha_foo = self._fixture.commit(tree, [], "foo")

        tree = self._fixture.tree({"foo.txt": "bar content"})
        self.sha_bar = self._fixture.commit(tree, [self.sha_foo], "bar")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)
        self._outdir.cleanup()

    def test_materialize_dirs(self) -> None:
        base_dir, after_dir, _ = self._candidate.materialize(
            self.sha_foo, self.sha_bar, self._outdir.name
        )
        self.assertTrue(base_dir.is_dir())
        self.assertTrue(after_dir.is_dir())

    def test_materialize_content(self) -> None:
        base_dir, after_dir, _ = self._candidate.materialize(
            self.sha_foo, self.sha_bar, self._outdir.name
        )
        self.assertEqual(
            (base_dir / "foo.txt").read_bytes(), b"foo content\n"
        )
        self.assertEqual(
            (after_dir / "foo.txt").read_bytes(), b"bar content\n"
        )

    def test_materialize_manifest(self) -> None:
        _, _, ops = self._candidate.materialize(
            self.sha_foo, self.sha_bar, self._outdir.name
        )
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].operation, OperationType.EDIT)
        self.assertEqual(ops[0].base, "foo.txt")
        self.assertEqual(ops[0].after, "foo.txt")


class TestVersionControlManifest(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()
        self._outdir = tempfile.TemporaryDirectory()

        tree = self._fixture.tree({"foo.txt": "foo"})
        self.sha_bar = self._fixture.commit(tree, [], "bar")

        tree = self._fixture.tree({"foo.txt": "bar", "bar.txt": "baz"})
        self.sha_baz = self._fixture.commit(tree, [self.sha_bar], "baz")

        tree = self._fixture.tree({"qux.txt": "bar", "bar.txt": "baz"})
        self.sha_qux = self._fixture.commit(tree, [self.sha_baz], "qux")

        tree = self._fixture.tree({
            "qux.txt": "bar",
            "bar.txt": "baz",
            "copy.txt": "bar",
        })
        self.sha_quux = self._fixture.commit(tree, [self.sha_qux], "quux")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)
        self._outdir.cleanup()

    def test_manifest_add(self) -> None:
        _, _, ops = self._candidate.materialize(
            self.sha_bar, self.sha_baz, self._outdir.name
        )
        by_path = {(o.base, o.after): o.operation for o in ops}
        self.assertEqual(by_path[(None, "bar.txt")], OperationType.ADD)
        self.assertEqual(by_path[("foo.txt", "foo.txt")], OperationType.EDIT)

    def test_manifest_empty(self) -> None:
        _, _, ops = self._candidate.materialize(
            self.sha_bar, self.sha_bar, self._outdir.name
        )
        self.assertEqual(ops, [])

    def test_manifest_rename(self) -> None:
        statuses = {
            delta.status
            for delta in self._candidate.diff(self.sha_baz, self.sha_qux).deltas
        }
        self.assertIn(pygit2.GIT_DELTA_RENAMED, statuses)
        self.assertEqual(
            self._candidate.manifest(self.sha_baz, self.sha_qux),
            [
                FileOperation(
                    base="foo.txt",
                    after="qux.txt",
                    operation=OperationType.MOVE,
                )
            ],
        )

    def test_manifest_copy_uses_similarity(self) -> None:
        statuses = {
            delta.status
            for delta in self._candidate.diff(self.sha_qux, self.sha_quux).deltas
        }
        self.assertTrue(
            statuses <= {
                pygit2.GIT_DELTA_ADDED,
                pygit2.GIT_DELTA_COPIED,
            }
        )
        operation = self._candidate.manifest(self.sha_qux, self.sha_quux)[0]
        self.assertEqual(operation.after, "copy.txt")
        if pygit2.GIT_DELTA_COPIED in statuses:
            self.assertEqual(operation.base, "qux.txt")
            self.assertEqual(operation.operation, OperationType.COPY)
            return
        self.assertEqual(operation.base, None)
        self.assertEqual(operation.operation, OperationType.ADD)


class TestVersionControlMeta(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"a.txt": "v0"})
        self.sha_foo = self._fixture.commit(tree, [], "foo commit")
        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_meta(self) -> None:
        result = self._candidate.meta(self.sha_foo)
        self.assertIsInstance(result, Commit)
        self.assertEqual(result.message, "foo commit")
        self.assertEqual(result.author, "Test")
        self.assertIsInstance(result.time, datetime)
        self.assertEqual(result.time.tzinfo, timezone.utc)

    def test_meta_missing(self) -> None:
        with self.assertRaises(ValueError):
            self._candidate.meta("0" * 40)


class TestVersionControlLog(unittest.TestCase):
    """Repository layout:

        foo --- bar --- quux --- corge
                \\             /
                 baz --- qux
    """

    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"a.txt": "v0"})
        self.sha_foo = self._fixture.commit(tree, [], "foo")

        tree = self._fixture.tree({"a.txt": "v1"})
        self.sha_bar = self._fixture.commit(tree, [self.sha_foo], "bar")

        tree = self._fixture.tree({"a.txt": "vf1"})
        self.sha_baz = self._fixture.commit(
            tree, [self.sha_bar], "baz", ref=None
        )

        tree = self._fixture.tree({"a.txt": "vf2"})
        self.sha_qux = self._fixture.commit(
            tree, [self.sha_baz], "qux", ref=None
        )

        tree = self._fixture.tree({"a.txt": "v2"})
        self.sha_quux = self._fixture.commit(tree, [self.sha_bar], "quux")

        tree = self._fixture.tree({"a.txt": "v_merged"})
        self.sha_corge = self._fixture.commit(
            tree, [self.sha_quux, self.sha_qux], "corge",
        )

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_log_linear(self) -> None:
        result = self._candidate.log(self.sha_foo, self.sha_quux)
        self.assertEqual(result, [self.sha_bar])

    def test_log_merge(self) -> None:
        result = self._candidate.log(self.sha_foo, self.sha_corge, merge=False)
        self.assertIn(self.sha_bar, result)
        self.assertIn(self.sha_quux, result)
        self.assertIn(self.sha_baz, result)
        self.assertIn(self.sha_qux, result)

    def test_log_merge_flag(self) -> None:
        result = self._candidate.log(self.sha_foo, self.sha_corge, merge=True)
        self.assertIn(self.sha_bar, result)
        self.assertIn(self.sha_quux, result)
        self.assertIn(self.sha_baz, result)
        self.assertIn(self.sha_qux, result)
        self.assertNotIn(self.sha_corge, result)

    def test_log_empty(self) -> None:
        result = self._candidate.log(self.sha_bar, self.sha_quux)
        self.assertEqual(result, [])


class TestVersionControlSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()
        self._outdir = tempfile.TemporaryDirectory()

        tree = self._fixture.tree(
            {
                "a.py": """
                def foo():
                    return 1
                """
            }
        )
        self.sha_foo = self._fixture.commit(tree, [], "foo")

        tree = self._fixture.tree(
            {
                "a.py": """
                def foo():
                    return 2
                """
            }
        )
        self.sha_bar = self._fixture.commit(tree, [self.sha_foo], "bar")

        tree = self._fixture.tree(
            {
                "a.py": """
                def foo():
                    return 2
                """,
                "b.py": """
                export function value() {
                    return 1;
                }
                """,
            }
        )
        self.sha_baz = self._fixture.commit(tree, [self.sha_bar], "baz")

        tree = self._fixture.tree(
            {
                "renamed.py": """
                def foo():
                    return 2
                """,
                "b.py": """
                export function value() {
                    return 1;
                }
                """,
            }
        )
        self.sha_qux = self._fixture.commit(
            tree, [self.sha_baz], "qux"
        )

        tree = self._fixture.tree(
            {
                "b.py": """
                export function value() {
                    return 1;
                }
                """,
            }
        )
        self.sha_quux = self._fixture.commit(tree, [self.sha_qux], "quux")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)
        self._outdir.cleanup()

    def _fragment(self, chains: list[list[str]]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}

        for chain in chains:
            for index, sha in enumerate(chain):
                parents = [] if index == 0 else [chain[index - 1]]
                if sha in result and result[sha] != parents:
                    raise ValueError(f"Conflicting test fragment for {sha}")
                result[sha] = parents

        return result

    def _snapshot(self, fragments: list[dict[str, list[str]]]) -> VersionControlSnapshot:
        target = tempfile.mkdtemp(dir=self._outdir.name)
        self._candidate.dump(fragments, target)
        snapshot = VersionControlSnapshot.load(target)
        self.addCleanup(snapshot.close)
        return snapshot

    def _source(self, fragments: list[dict[str, list[str]]]) -> Path:
        target = Path(tempfile.mkdtemp(dir=self._outdir.name))
        self._candidate.dump(fragments, target)
        return target

    def test_dump(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)
        self._candidate.dump([self._fragment([[self.sha_foo, self.sha_bar]])], target)

        with open(f"{target}/meta.json", "r", encoding="utf-8") as file:
            meta = json.load(file)

        commit = meta["commits"][self.sha_bar]
        tree = commit["tree"]
        self.assertEqual(commit["parents"], [self.sha_foo])

        with open(f"{target}/trees/{tree}.json", "r", encoding="utf-8") as file:
            data = json.load(file)

        commit = self._fixture.repository.get(self.sha_bar).peel(pygit2.Commit)
        blob = str(commit.tree["a.py"].id)

        self.assertEqual(data["files"], {"a.py": blob})
        with open(f"{target}/blobs/{blob}", "rb") as file:
            self.assertEqual(file.read(), b"def foo():\n    return 2\n")

    def test_load_parents(self) -> None:
        snapshot = self._snapshot(
            [self._fragment([[self.sha_foo, self.sha_bar, self.sha_baz]])]
        )

        self.assertEqual(snapshot.parents(self.sha_foo), [])
        self.assertEqual(snapshot.parents(self.sha_bar), [self.sha_foo])
        self.assertEqual(snapshot.parents(self.sha_baz), [self.sha_bar])

    def test_diff(self) -> None:
        snapshot = self._snapshot([self._fragment([[self.sha_foo, self.sha_bar]])])

        self.assertEqual(
            snapshot.diff(self.sha_foo, self.sha_bar, context=3).patch,
            self._candidate.diff(self.sha_foo, self.sha_bar, context=3).patch,
        )

    def test_manifest(self) -> None:
        snapshot = self._snapshot(
            [self._fragment([[self.sha_bar, self.sha_baz, self.sha_qux, self.sha_quux]])]
        )

        self.assertEqual(
            snapshot.manifest(self.sha_bar, self.sha_baz),
            self._candidate.manifest(self.sha_bar, self.sha_baz),
        )
        self.assertEqual(
            snapshot.manifest(self.sha_baz, self.sha_qux),
            self._candidate.manifest(self.sha_baz, self.sha_qux),
        )
        self.assertEqual(
            snapshot.manifest(self.sha_qux, self.sha_quux),
            self._candidate.manifest(self.sha_qux, self.sha_quux),
        )

    def test_load_filter(self) -> None:
        source = self._source([self._fragment([[self.sha_foo, self.sha_bar]])])
        snapshot = VersionControlSnapshot.load(
            source,
            filter=lambda kind, value: (
                "filtered" if kind == "message" else value
            ),
        )
        self.addCleanup(snapshot.close)

        self.assertEqual(snapshot.meta(self.sha_bar).message, "filtered")

    def test_blob(self) -> None:
        snapshot = self._snapshot([self._fragment([[self.sha_baz, self.sha_qux]])])

        self.assertEqual(
            snapshot.blob(self.sha_qux, "renamed.py"),
            b"def foo():\n    return 2\n",
        )

    def test_load_reuse(self) -> None:
        snapshot = self._snapshot(
            [
                self._fragment([[self.sha_foo, self.sha_bar, self.sha_baz]]),
                self._fragment([[self.sha_foo, self.sha_bar]]),
            ]
        )

        self.assertEqual(snapshot.parents(self.sha_bar), [self.sha_foo])
        self.assertEqual(snapshot.parents(self.sha_baz), [self.sha_bar])
        self.assertEqual(
            snapshot.diff(self.sha_foo, self.sha_bar).patch,
            self._candidate.diff(self.sha_foo, self.sha_bar).patch,
        )

    def test_load_cache_builds_repo(self) -> None:
        waldo = self._source([self._fragment([[self.sha_foo, self.sha_bar]])])
        cache = Path(self._outdir.name) / "cache-build"
        snapshot = VersionControlSnapshot.load(waldo, cache=cache)
        self.addCleanup(snapshot.close)

        self.assertTrue(cache.is_dir())
        self.assertTrue((cache / "snapshot.json").is_file())
        self.assertEqual(snapshot.parents(self.sha_bar), [self.sha_foo])
        self.assertEqual(
            snapshot.diff(self.sha_foo, self.sha_bar, context=3).patch,
            self._candidate.diff(self.sha_foo, self.sha_bar, context=3).patch,
        )

    def test_load_cache_reuses_repo_without_source(self) -> None:
        waldo = self._source([self._fragment([[self.sha_foo, self.sha_bar]])])
        cache = Path(self._outdir.name) / "cache-reuse"

        first = VersionControlSnapshot.load(waldo, cache=cache)
        self.addCleanup(first.close)
        waldo.joinpath("meta.json").unlink()

        second = VersionControlSnapshot.load(waldo, cache=cache)
        self.addCleanup(second.close)

        self.assertEqual(second.parents(self.sha_bar), [self.sha_foo])
        self.assertEqual(
            second.diff(self.sha_foo, self.sha_bar).patch,
            self._candidate.diff(self.sha_foo, self.sha_bar).patch,
        )

    def test_close_keeps_explicit_cache(self) -> None:
        waldo = self._source([self._fragment([[self.sha_foo, self.sha_bar]])])
        cache = Path(self._outdir.name) / "cache-close"
        snapshot = VersionControlSnapshot.load(waldo, cache=cache)

        snapshot.close()

        self.assertTrue(cache.is_dir())
        self.assertTrue((cache / "HEAD").is_file())

    def test_load_cache_rewrites_invalid_cache(self) -> None:
        waldo = self._source([self._fragment([[self.sha_foo, self.sha_bar]])])
        cache = Path(self._outdir.name) / "cache-invalid"
        cache.mkdir()
        pygit2.init_repository(str(cache), bare=True)

        snapshot = VersionControlSnapshot.load(waldo, cache=cache)
        self.addCleanup(snapshot.close)

        self.assertTrue((cache / "snapshot.json").is_file())
        self.assertEqual(snapshot.parents(self.sha_bar), [self.sha_foo])

    def test_expand(self) -> None:
        snapshot = self._snapshot([self._fragment([[self.sha_foo, self.sha_bar]])])

        patchset = PatchFactory(snapshot).expand(
            self.sha_foo,
            self.sha_bar,
            files=[
                FileOperation(
                    base="a.py",
                    after="a.py",
                    operation=OperationType.EDIT,
                )
            ],
            level=1,
            n=3,
        )

        rendered = str(patchset)
        self.assertIn("diff --git a/a.py b/a.py", rendered)
        self.assertIn("def foo():", rendered)

    def test_dump_deduplicates_blobs(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)
        self._candidate.dump(
            [self._fragment([[self.sha_bar, self.sha_baz, self.sha_qux]])],
            target,
        )

        self.assertEqual(len(list(Path(target, "blobs").iterdir())), 2)

    def test_dump_deduplicates_trees(self) -> None:
        waldo = self._fixture.repository.get(self.sha_bar).peel(pygit2.Commit)
        corge = self._fixture.commit(waldo.tree.id, [self.sha_bar], "corge", ref=None)
        target = tempfile.mkdtemp(dir=self._outdir.name)
        self._candidate.dump([self._fragment([[self.sha_bar, corge]])], target)

        self.assertEqual(len(list(Path(target, "trees").iterdir())), 1)

    def test_load_missing_blob(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)
        self._candidate.dump([self._fragment([[self.sha_foo, self.sha_bar]])], target)

        with open(f"{target}/meta.json", "r", encoding="utf-8") as file:
            meta = json.load(file)

        tree = meta["commits"][self.sha_bar]["tree"]

        with open(f"{target}/trees/{tree}.json", "r", encoding="utf-8") as file:
            data = json.load(file)

        blob = data["files"]["a.py"]
        Path(target, "blobs", blob).unlink()

        with self.assertRaises(ValueError):
            VersionControlSnapshot.load(target)

    def test_dump_merge_parents(self) -> None:
        corge = self._fixture.commit(
            self._fixture.tree({"corge.py": "corge\n"}),
            [self.sha_bar],
            "corge",
            ref=None,
        )
        grault = self._fixture.commit(
            self._fixture.tree({"corge.py": "grault\n"}),
            [self.sha_bar],
            "grault",
            ref=None,
        )
        garply = self._fixture.commit(
            self._fixture.tree({"corge.py": "garply\n"}),
            [corge, grault],
            "garply",
            ref=None,
        )
        snapshot = self._snapshot([{
            self.sha_foo: [],
            self.sha_bar: [self.sha_foo],
            corge: [self.sha_bar],
            grault: [self.sha_bar],
            garply: [corge, grault],
        }])

        self.assertEqual(snapshot.parents(garply), [corge, grault])

    def test_dump_rejects_inconsistent_fragment_parents(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)

        with self.assertRaises(ValueError):
            self._candidate.dump(
                [
                    {self.sha_bar: [self.sha_foo], self.sha_foo: []},
                    {self.sha_bar: [self.sha_baz], self.sha_baz: []},
                ],
                target,
            )

    def test_dump_prefers_concrete_parents_over_stub(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)
        waldo = self._fixture.repository.get(self.sha_baz).peel(pygit2.Commit)
        corge = self._fixture.commit(
            waldo.tree.id, [self.sha_baz], "corge", ref=None
        )
        grault = self._fixture.commit(waldo.tree.id, [corge], "grault", ref=None)

        self._candidate.dump(
            [
                {grault: [corge], corge: []},
                {corge: [self.sha_baz]},
                self._fragment([[self.sha_foo, self.sha_bar, self.sha_baz]]),
            ],
            target,
        )

        snapshot = VersionControlSnapshot.load(target)
        self.addCleanup(snapshot.close)
        self.assertEqual(snapshot.parents(corge), [self.sha_baz])


class TestPlatformCache(unittest.TestCase):
    """Tests PlatformCache with a real pygit2 repository.

    Repository layout:

        foo --- bar --- qux
                \\    /
                 baz
    """

    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"a.txt": "v0"})
        self.sha_foo = self._fixture.commit(tree, [], "foo")

        tree = self._fixture.tree({"a.txt": "v1"})
        self.sha_bar = self._fixture.commit(tree, [self.sha_foo], "bar")

        tree = self._fixture.tree({"a.txt": "v_feature"})
        self.sha_baz = self._fixture.commit(tree, [self.sha_bar], "baz", ref=None)

        tree = self._fixture.tree({"a.txt": "v_merged"})
        self.sha_qux = self._fixture.commit(
            tree, [self.sha_bar, self.sha_baz], "qux",
        )

        tree = self._fixture.tree({"a.txt": "v_squash"})
        self.sha_quux = self._fixture.commit(tree, [self.sha_qux], "quux")

        self._repository = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def _make_cache(self, prs: list[dict]) -> PlatformCache:
        names, types, numbers, payloads = [], [], [], []
        for pr in prs:
            pr_payload = {
                "merged": True,
                "merge_commit_sha": pr["merge_sha"],
                "title": pr.get("title", ""),
                "body": pr.get("body", ""),
            }
            names.append("repo/a")
            types.append("pr")
            numbers.append(pr["number"])
            payloads.append(json.dumps(pr_payload))

        table = pa.table({
            "name": names,
            "type": types,
            "number": pa.array(numbers, type=pa.int32()),
            "payload": pa.array(payloads, type=pa.large_string()),
        })
        return PlatformCache(table, self._repository)

    def test_resolve(self) -> None:
        cache = self._make_cache([{
            "number": 10,
            "merge_sha": self.sha_qux,
            "title": "foo",
            "body": "bar",
        }])
        change = cache.resolve(self.sha_qux)
        self.assertEqual(change.number, 10)
        self.assertEqual(change.after, self.sha_qux)
        self.assertEqual(change.base, self.sha_bar)
        self.assertEqual(change.description, "foo\n\nbar")

    def test_resolve_missing(self) -> None:
        cache = self._make_cache([{
            "number": 10,
            "merge_sha": self.sha_qux,
        }])
        with self.assertRaises(KeyError):
            cache.resolve("nonexistent_sha")

    @unittest.skip("Current PlatformCache no longer rejects non-merge commits.")
    def test_build_cache_rejects_non_merge(self) -> None:
        with self.assertRaises(NotImplementedError):
            self._make_cache([{
                "number": 20,
                "merge_sha": self.sha_quux,
            }])

    def test_load(self) -> None:
        cache = self._make_cache([{
            "number": 42,
            "merge_sha": self.sha_qux,
            "title": "baz",
            "body": "",
        }])
        change = cache.load(42)
        self.assertEqual(change.number, 42)
        self.assertEqual(change.after, self.sha_qux)
        self.assertEqual(change.description, "baz")

    def test_load_missing(self) -> None:
        cache = self._make_cache([{
            "number": 42,
            "merge_sha": self.sha_qux,
        }])
        with self.assertRaises(KeyError):
            cache.load(999)


class TestVersionControlPathWalker(unittest.TestCase):
    def _walker(
        self,
        fixture: HistoryFixture,
        commit: str,
        path: str,
        depth: int = 100,
    ):
        return VersionControlPathWalker(
            repository=fixture.repository,
            commit=commit,
            path=path,
            depth=depth,
        )

    def _entries(
        self,
        fixture: HistoryFixture,
        commit: str,
        path: str,
        depth: int = 100,
    ) -> list[object]:
        return list(self._walker(fixture, commit, path, depth).entries())

    def _commits(
        self,
        fixture: HistoryFixture,
        commit: str,
        path: str,
        depth: int = 100,
    ) -> list[str]:
        return [
            str(node.id)
            for node in self._walker(fixture, commit, path, depth).commits()
        ]

    def _git(
        self,
        fixture: HistoryFixture,
        commit: str,
        path: str,
    ) -> list[str]:
        if shutil.which("git") is None:
            self.skipTest("git is required for oracle comparisons")

        result = subprocess.run(
            [
                "git",
                f"--git-dir={fixture.repository.path}",
                "log",
                "--follow",
                "--format=%H",
                commit,
                "--",
                path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def _clone_git(
        self,
        fixture: HistoryFixture,
        commit: str,
        path: str,
    ) -> list[str]:
        if shutil.which("git") is None:
            self.skipTest("git is required for oracle comparisons")

        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    fixture.repository.path,
                    directory,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    directory,
                    "log",
                    "--follow",
                    "--format=%H",
                    commit,
                    "--",
                    path,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        return [line for line in result.stdout.splitlines() if line]

    def _oracle(
        self,
        fixture: HistoryFixture,
        commit: str,
        path: str,
    ) -> list[str]:
        try:
            return self._git(fixture, commit, path)
        except subprocess.CalledProcessError:
            return self._clone_git(fixture, commit, path)

    def _entry(
        self,
        item: object,
    ) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            commit=str(getattr(item, "commit")),
            operation=getattr(item, "operation"),
            base=getattr(item, "base"),
            after=getattr(item, "after"),
        )

    def test_linear_modify_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"a.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "baz\n"})
            baz = fixture.commit(tree, [bar], "baz")

            self.assertEqual(
                self._commits(fixture, baz, "a.py"),
                self._oracle(fixture, baz, "a.py"),
            )

    def test_linear_rename_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"bar.py": "foo\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"bar.py": "baz\n"})
            baz = fixture.commit(tree, [bar], "baz")

            self.assertEqual(
                self._commits(fixture, baz, "bar.py"),
                self._oracle(fixture, baz, "bar.py"),
            )

            entries = [self._entry(item) for item in self._entries(fixture, baz, "bar.py")]
            self.assertEqual(
                [(item.operation, item.base, item.after) for item in entries],
                [
                    (OperationType.EDIT, "bar.py", "bar.py"),
                    (OperationType.MOVE, "foo.py", "bar.py"),
                    (OperationType.ADD, None, "foo.py"),
                ],
            )

    def test_copy_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"foo.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({
                "foo.py": "bar\n",
                "baz.py": "bar\n",
            })
            baz = fixture.commit(tree, [bar], "baz")

            tree = fixture.tree({
                "foo.py": "qux\n",
                "baz.py": "qux\n",
            })
            qux = fixture.commit(tree, [baz], "qux")

            self.assertEqual(
                self._commits(fixture, qux, "baz.py"),
                self._oracle(fixture, qux, "baz.py"),
            )

            entries = [self._entry(item) for item in self._entries(fixture, qux, "baz.py")]
            self.assertEqual(
                [(item.operation, item.base, item.after) for item in entries],
                [
                    (OperationType.EDIT, "baz.py", "baz.py"),
                    (OperationType.COPY, "foo.py", "baz.py"),
                    (OperationType.EDIT, "foo.py", "foo.py"),
                    (OperationType.ADD, None, "foo.py"),
                ],
            )

    def test_merge_treesame_to_first_parent_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"a.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "baz\n"})
            baz = fixture.commit(tree, [foo], "baz", ref=None)

            tree = fixture.tree({"a.py": "bar\n"})
            qux = fixture.commit(tree, [bar, baz], "qux")

            tree = fixture.tree({"a.py": "quux\n"})
            quux = fixture.commit(tree, [qux], "quux")

            result = self._commits(fixture, quux, "a.py")
            self.assertEqual(result, self._oracle(fixture, quux, "a.py"))
            self.assertNotIn(qux, result)

    def test_merge_treesame_to_second_parent_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"a.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "baz\n"})
            baz = fixture.commit(tree, [foo], "baz", ref=None)

            tree = fixture.tree({"a.py": "baz\n"})
            qux = fixture.commit(tree, [bar, baz], "qux")

            tree = fixture.tree({"a.py": "quux\n"})
            quux = fixture.commit(tree, [qux], "quux")

            result = self._commits(fixture, quux, "a.py")
            self.assertEqual(result, self._oracle(fixture, quux, "a.py"))
            self.assertNotIn(qux, result)

    def test_merge_differs_from_both_parents_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"a.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "baz\n"})
            baz = fixture.commit(tree, [foo], "baz", ref=None)

            tree = fixture.tree({"a.py": "qux\n"})
            qux = fixture.commit(tree, [bar, baz], "qux")

            tree = fixture.tree({"a.py": "quux\n"})
            quux = fixture.commit(tree, [qux], "quux")

            result = self._commits(fixture, quux, "a.py")
            self.assertEqual(result, self._oracle(fixture, quux, "a.py"))
            self.assertNotIn(qux, result)

    def test_rename_through_merge_does_not_cross_boundary(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"foo.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"foo.py": "baz\n"})
            baz = fixture.commit(tree, [foo], "baz", ref=None)

            tree = fixture.tree({"bar.py": "qux\n"})
            qux = fixture.commit(tree, [bar, baz], "qux")

            tree = fixture.tree({"bar.py": "quux\n"})
            quux = fixture.commit(tree, [qux], "quux")

            result = self._commits(fixture, quux, "bar.py")
            self.assertEqual(result, self._oracle(fixture, quux, "bar.py"))
            self.assertNotIn(foo, result)
            self.assertNotIn(qux, result)

    def test_add_delete_root_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "baz\n"})
            baz = fixture.commit(tree, [bar], "baz")

            self.assertEqual(
                self._commits(fixture, baz, "a.py"),
                self._oracle(fixture, baz, "a.py"),
            )

            entries = [self._entry(item) for item in self._entries(fixture, baz, "a.py")]
            self.assertEqual(
                [(item.operation, item.base, item.after) for item in entries],
                [
                    (OperationType.ADD, None, "a.py"),
                    (OperationType.DELETE, "a.py", None),
                    (OperationType.ADD, None, "a.py"),
                ],
            )

    def test_depth_limits_visible_commits(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"a.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            tree = fixture.tree({"a.py": "baz\n"})
            baz = fixture.commit(tree, [bar], "baz")

            tree = fixture.tree({"a.py": "qux\n"})
            qux = fixture.commit(tree, [baz], "qux")

            self.assertEqual(
                self._commits(fixture, qux, "a.py", depth=2),
                self._oracle(fixture, qux, "a.py")[:2],
            )

    def test_iterator_matches_commit_list(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "foo\n"})
            foo = fixture.commit(tree, [], "foo")

            tree = fixture.tree({"a.py": "bar\n"})
            bar = fixture.commit(tree, [foo], "bar")

            walker = self._walker(fixture, bar, "a.py")
            self.assertEqual(
                [str(node.id) for node in walker],
                self._commits(fixture, bar, "a.py"),
            )


if __name__ == "__main__":
    unittest.main()
