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
        parent = self._fixture.commit(self._tree(), [], "foo")

        repository = self._fixture.repository
        root = repository.TreeBuilder(self._tree())
        baz = repository.create_blob(b"baz\n")
        root.insert("baz.py", baz, pygit2.GIT_FILEMODE_BLOB)
        child = self._fixture.commit(root.write(), [parent], "bar")

        walker = VersionControlPathWalker(repository, child, "baz.py", 10)
        result = list(walker.entries())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].type, "added")
        self.assertEqual(result[0].path, "baz.py")


class TestVersionControlParents(unittest.TestCase):
    """Tests parents() across merge, squash and rebase strategies.

    Repository layout:

        I --- T1 --- T2 --- M (merge, parents: T2 + F2) --- S (squash) --- R1 --- R2
               \\           /
                F1 --- F2
    """

    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"foo.txt": "initial"})
        self.sha_i = self._fixture.commit(tree, [], "initial")

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work"})
        self.sha_t1 = self._fixture.commit(tree, [self.sha_i], "trunk 1")

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work\nfeature a"})
        self.sha_f1 = self._fixture.commit(tree, [self.sha_t1], "feature 1", ref=None)

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work\nfeature a\nfeature b"})
        self.sha_f2 = self._fixture.commit(tree, [self.sha_f1], "feature 2", ref=None)

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work\nmore trunk"})
        self.sha_t2 = self._fixture.commit(tree, [self.sha_t1], "trunk 2")

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b"})
        self.sha_m = self._fixture.commit(
            tree, [self.sha_t2, self.sha_f2],
            "merge feature into trunk",
        )

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b\nsquashed"})
        self.sha_s = self._fixture.commit(tree, [self.sha_m], "squash feature2")

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b\nsquashed\nrebased 1"})
        self.sha_r1 = self._fixture.commit(tree, [self.sha_s], "rebased 1")

        tree = self._fixture.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b\nsquashed\nrebased 1\nrebased 2"})
        self.sha_r2 = self._fixture.commit(tree, [self.sha_r1], "rebased 2")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_parents_root(self) -> None:
        self.assertEqual(self._candidate.parents(self.sha_i), [])

    def test_parents_merge(self) -> None:
        result = self._candidate.parents(self.sha_m)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], self.sha_t2)
        self.assertEqual(result[1], self.sha_f2)

    def test_parents_squash(self) -> None:
        result = self._candidate.parents(self.sha_s)
        self.assertEqual(result, [self.sha_m])

    def test_parents_rebase(self) -> None:
        self.assertEqual(self._candidate.parents(self.sha_r2), [self.sha_r1])
        self.assertEqual(self._candidate.parents(self.sha_r1), [self.sha_s])

    def test_parents_feature(self) -> None:
        self.assertEqual(self._candidate.parents(self.sha_f2), [self.sha_f1])
        self.assertEqual(self._candidate.parents(self.sha_f1), [self.sha_t1])


class TestVersionControlBlame(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"foo.txt": "line1\nline2\nline3"})
        self.sha_i = self._fixture.commit(tree, [], "initial")

        tree = self._fixture.tree({"foo.txt": "line1\nline2 changed\nline3"})
        self.sha_f1 = self._fixture.commit(tree, [self.sha_i], "feature 1")

        tree = self._fixture.tree({"foo.txt": "line1\nline2 changed\nline3\nnew feature line"})
        self.sha_f2 = self._fixture.commit(tree, [self.sha_f1], "feature 2")

        tree = self._fixture.tree({"foo.txt": "line1\nline2 fixed\nline3\nnew feature line"})
        self.sha_x1 = self._fixture.commit(tree, [self.sha_f2], "fix 1")

        tree = self._fixture.tree({"foo.txt": "line1\nline2 fixed\nline3\nnew feature line\nfix done"})
        self.sha_x2 = self._fixture.commit(tree, [self.sha_x1], "fix 2")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_blame_order(self) -> None:
        result = self._candidate.blame(
            "foo.txt",
            self.sha_x2,
            [(1, 1), (2, 2), (5, 5)],
        )
        self.assertEqual(
            result,
            [
                (self.sha_i, (1, 1)),
                (self.sha_x1, (2, 2)),
                (self.sha_x2, (5, 5)),
            ],
        )

    def test_blame_line(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_x2, [(2, 2)])
        self.assertEqual(result, [(self.sha_x1, (2, 2))])

    def test_blame_unchanged(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_x2, [(1, 1)])
        self.assertEqual(result, [(self.sha_i, (1, 1))])

    def test_blame_mixed(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_x2, [(1, 5)])
        self.assertEqual(
            result,
            [
                (self.sha_i, (1, 1)),
                (self.sha_x1, (2, 2)),
                (self.sha_i, (3, 3)),
                (self.sha_f2, (4, 4)),
                (self.sha_x2, (5, 5)),
            ],
        )

    def test_blame_range(self) -> None:
        result = self._candidate.blame("foo.txt", self.sha_x2, [(9, 9)])
        self.assertEqual(result, [(None, None)])


class TestVersionControlMaterialize(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()
        self._outdir = tempfile.TemporaryDirectory()

        tree = self._fixture.tree({"foo.txt": "base content"})
        self.sha_base = self._fixture.commit(tree, [], "base")

        tree = self._fixture.tree({"foo.txt": "after content"})
        self.sha_after = self._fixture.commit(tree, [self.sha_base], "after")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)
        self._outdir.cleanup()

    def test_materialize_dirs(self) -> None:
        base_dir, after_dir, _ = self._candidate.materialize(
            self.sha_base, self.sha_after, self._outdir.name
        )
        self.assertTrue(base_dir.is_dir())
        self.assertTrue(after_dir.is_dir())

    def test_materialize_content(self) -> None:
        base_dir, after_dir, _ = self._candidate.materialize(
            self.sha_base, self.sha_after, self._outdir.name
        )
        self.assertEqual(
            (base_dir / "foo.txt").read_bytes(), b"base content\n"
        )
        self.assertEqual(
            (after_dir / "foo.txt").read_bytes(), b"after content\n"
        )

    def test_materialize_manifest(self) -> None:
        _, _, ops = self._candidate.materialize(
            self.sha_base, self.sha_after, self._outdir.name
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

        tree = self._fixture.tree({"foo.txt": "initial"})
        self.sha_i = self._fixture.commit(tree, [], "initial")

        tree = self._fixture.tree({"foo.txt": "changed", "bar.txt": "new"})
        self.sha_c = self._fixture.commit(tree, [self.sha_i], "change")

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)
        self._outdir.cleanup()

    def test_manifest_add(self) -> None:
        _, _, ops = self._candidate.materialize(
            self.sha_i, self.sha_c, self._outdir.name
        )
        by_path = {(o.base, o.after): o.operation for o in ops}
        self.assertEqual(by_path[(None, "bar.txt")], OperationType.ADD)
        self.assertEqual(by_path[("foo.txt", "foo.txt")], OperationType.EDIT)

    def test_manifest_empty(self) -> None:
        _, _, ops = self._candidate.materialize(
            self.sha_i, self.sha_i, self._outdir.name
        )
        self.assertEqual(ops, [])


class TestVersionControlMeta(unittest.TestCase):
    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"a.txt": "v0"})
        self.sha_i = self._fixture.commit(tree, [], "initial commit")
        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_meta(self) -> None:
        result = self._candidate.meta(self.sha_i)
        self.assertIsInstance(result, Commit)
        self.assertEqual(result.message, "initial commit")
        self.assertEqual(result.author, "Test")
        self.assertIsInstance(result.time, datetime)
        self.assertEqual(result.time.tzinfo, timezone.utc)

    def test_meta_missing(self) -> None:
        with self.assertRaises(ValueError):
            self._candidate.meta("0" * 40)


class TestVersionControlLog(unittest.TestCase):
    """Repository layout:

        I --- A --- B --- C
               \\         /
                F1 --- F2
    """

    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"a.txt": "v0"})
        self.sha_i = self._fixture.commit(tree, [], "initial")

        tree = self._fixture.tree({"a.txt": "v1"})
        self.sha_a = self._fixture.commit(tree, [self.sha_i], "A")

        tree = self._fixture.tree({"a.txt": "vf1"})
        self.sha_f1 = self._fixture.commit(tree, [self.sha_a], "F1", ref=None)

        tree = self._fixture.tree({"a.txt": "vf2"})
        self.sha_f2 = self._fixture.commit(tree, [self.sha_f1], "F2", ref=None)

        tree = self._fixture.tree({"a.txt": "v2"})
        self.sha_b = self._fixture.commit(tree, [self.sha_a], "B")

        tree = self._fixture.tree({"a.txt": "v_merged"})
        self.sha_c = self._fixture.commit(
            tree, [self.sha_b, self.sha_f2], "C (merge)",
        )

        self._candidate = VersionControl(self._fixture.repository)

    def tearDown(self) -> None:
        self._fixture.__exit__(None, None, None)

    def test_log_linear(self) -> None:
        result = self._candidate.log(self.sha_i, self.sha_b)
        self.assertEqual(result, [self.sha_a])

    def test_log_merge(self) -> None:
        result = self._candidate.log(self.sha_i, self.sha_c, merge=False)
        self.assertIn(self.sha_a, result)
        self.assertIn(self.sha_b, result)
        self.assertIn(self.sha_f1, result)
        self.assertIn(self.sha_f2, result)

    def test_log_merge_flag(self) -> None:
        result = self._candidate.log(self.sha_i, self.sha_c, merge=True)
        self.assertIn(self.sha_a, result)
        self.assertIn(self.sha_b, result)
        self.assertIn(self.sha_f1, result)
        self.assertIn(self.sha_f2, result)
        self.assertNotIn(self.sha_c, result)

    def test_log_empty(self) -> None:
        result = self._candidate.log(self.sha_a, self.sha_b)
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
        self.sha_a = self._fixture.commit(tree, [], "A")

        tree = self._fixture.tree(
            {
                "a.py": """
                def foo():
                    return 2
                """
            }
        )
        self.sha_b = self._fixture.commit(tree, [self.sha_a], "B")

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
        self.sha_c = self._fixture.commit(tree, [self.sha_b], "C")

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
        self.sha_d = self._fixture.commit(tree, [self.sha_c], "D")

        tree = self._fixture.tree(
            {
                "b.py": """
                export function value() {
                    return 1;
                }
                """,
            }
        )
        self.sha_e = self._fixture.commit(tree, [self.sha_d], "E")

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
        self._candidate.dump([self._fragment([[self.sha_a, self.sha_b]])], target)

        with open(f"{target}/meta.json", "r", encoding="utf-8") as file:
            meta = json.load(file)

        commit = meta["commits"][self.sha_b]
        tree = commit["tree"]
        self.assertEqual(commit["parents"], [self.sha_a])

        with open(f"{target}/trees/{tree}.json", "r", encoding="utf-8") as file:
            data = json.load(file)

        commit = self._fixture.repository.get(self.sha_b).peel(pygit2.Commit)
        blob = str(commit.tree["a.py"].id)

        self.assertEqual(data["files"], {"a.py": blob})
        with open(f"{target}/blobs/{blob}", "rb") as file:
            self.assertEqual(file.read(), b"def foo():\n    return 2\n")

    def test_load_parents(self) -> None:
        snapshot = self._snapshot([self._fragment([[self.sha_a, self.sha_b, self.sha_c]])])

        self.assertEqual(snapshot.parents(self.sha_a), [])
        self.assertEqual(snapshot.parents(self.sha_b), [self.sha_a])
        self.assertEqual(snapshot.parents(self.sha_c), [self.sha_b])

    def test_diff(self) -> None:
        snapshot = self._snapshot([self._fragment([[self.sha_a, self.sha_b]])])

        self.assertEqual(
            snapshot.diff(self.sha_a, self.sha_b, context=3).patch,
            self._candidate.diff(self.sha_a, self.sha_b, context=3).patch,
        )

    def test_manifest(self) -> None:
        snapshot = self._snapshot(
            [self._fragment([[self.sha_b, self.sha_c, self.sha_d, self.sha_e]])]
        )

        self.assertEqual(
            snapshot.manifest(self.sha_b, self.sha_c),
            self._candidate.manifest(self.sha_b, self.sha_c),
        )
        self.assertEqual(
            snapshot.manifest(self.sha_c, self.sha_d),
            self._candidate.manifest(self.sha_c, self.sha_d),
        )
        self.assertEqual(
            snapshot.manifest(self.sha_d, self.sha_e),
            self._candidate.manifest(self.sha_d, self.sha_e),
        )

    def test_blob(self) -> None:
        snapshot = self._snapshot([self._fragment([[self.sha_c, self.sha_d]])])

        self.assertEqual(
            snapshot.blob(self.sha_d, "renamed.py"),
            b"def foo():\n    return 2\n",
        )

    def test_load_reuse(self) -> None:
        snapshot = self._snapshot(
            [
                self._fragment([[self.sha_a, self.sha_b, self.sha_c]]),
                self._fragment([[self.sha_a, self.sha_b]]),
            ]
        )

        self.assertEqual(snapshot.parents(self.sha_b), [self.sha_a])
        self.assertEqual(snapshot.parents(self.sha_c), [self.sha_b])
        self.assertEqual(
            snapshot.diff(self.sha_a, self.sha_b).patch,
            self._candidate.diff(self.sha_a, self.sha_b).patch,
        )

    def test_load_cache_builds_repo(self) -> None:
        source = self._source([self._fragment([[self.sha_a, self.sha_b]])])
        cache = Path(self._outdir.name) / "cache-build"
        snapshot = VersionControlSnapshot.load(source, cache=cache)
        self.addCleanup(snapshot.close)

        self.assertTrue(cache.is_dir())
        self.assertTrue((cache / "snapshot.json").is_file())
        self.assertEqual(snapshot.parents(self.sha_b), [self.sha_a])
        self.assertEqual(
            snapshot.diff(self.sha_a, self.sha_b, context=3).patch,
            self._candidate.diff(self.sha_a, self.sha_b, context=3).patch,
        )

    def test_load_cache_reuses_repo_without_source(self) -> None:
        source = self._source([self._fragment([[self.sha_a, self.sha_b]])])
        cache = Path(self._outdir.name) / "cache-reuse"

        first = VersionControlSnapshot.load(source, cache=cache)
        self.addCleanup(first.close)
        source.joinpath("meta.json").unlink()

        second = VersionControlSnapshot.load(source, cache=cache)
        self.addCleanup(second.close)

        self.assertEqual(second.parents(self.sha_b), [self.sha_a])
        self.assertEqual(
            second.diff(self.sha_a, self.sha_b).patch,
            self._candidate.diff(self.sha_a, self.sha_b).patch,
        )

    def test_close_keeps_explicit_cache(self) -> None:
        source = self._source([self._fragment([[self.sha_a, self.sha_b]])])
        cache = Path(self._outdir.name) / "cache-close"
        snapshot = VersionControlSnapshot.load(source, cache=cache)

        snapshot.close()

        self.assertTrue(cache.is_dir())
        self.assertTrue((cache / "HEAD").is_file())

    def test_load_cache_rewrites_invalid_cache(self) -> None:
        source = self._source([self._fragment([[self.sha_a, self.sha_b]])])
        cache = Path(self._outdir.name) / "cache-invalid"
        cache.mkdir()
        pygit2.init_repository(str(cache), bare=True)

        snapshot = VersionControlSnapshot.load(source, cache=cache)
        self.addCleanup(snapshot.close)

        self.assertTrue((cache / "snapshot.json").is_file())
        self.assertEqual(snapshot.parents(self.sha_b), [self.sha_a])

    def test_expand(self) -> None:
        snapshot = self._snapshot([self._fragment([[self.sha_a, self.sha_b]])])

        patchset = PatchFactory(snapshot).expand(
            self.sha_a,
            self.sha_b,
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
            [self._fragment([[self.sha_b, self.sha_c, self.sha_d]])],
            target,
        )

        self.assertEqual(len(list(Path(target, "blobs").iterdir())), 2)

    def test_dump_deduplicates_trees(self) -> None:
        source = self._fixture.repository.get(self.sha_b).peel(pygit2.Commit)
        clone = self._fixture.commit(source.tree.id, [self.sha_b], "clone", ref=None)
        target = tempfile.mkdtemp(dir=self._outdir.name)
        self._candidate.dump([self._fragment([[self.sha_b, clone]])], target)

        self.assertEqual(len(list(Path(target, "trees").iterdir())), 1)

    def test_load_missing_blob(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)
        self._candidate.dump([self._fragment([[self.sha_a, self.sha_b]])], target)

        with open(f"{target}/meta.json", "r", encoding="utf-8") as file:
            meta = json.load(file)

        tree = meta["commits"][self.sha_b]["tree"]

        with open(f"{target}/trees/{tree}.json", "r", encoding="utf-8") as file:
            data = json.load(file)

        blob = data["files"]["a.py"]
        Path(target, "blobs", blob).unlink()

        with self.assertRaises(ValueError):
            VersionControlSnapshot.load(target)

    def test_dump_merge_parents(self) -> None:
        left = self._fixture.commit(
            self._fixture.tree({"merge.py": "left\n"}),
            [self.sha_b],
            "left",
            ref=None,
        )
        right = self._fixture.commit(
            self._fixture.tree({"merge.py": "right\n"}),
            [self.sha_b],
            "right",
            ref=None,
        )
        merge = self._fixture.commit(
            self._fixture.tree({"merge.py": "merge\n"}),
            [left, right],
            "merge",
            ref=None,
        )
        snapshot = self._snapshot([{
            self.sha_a: [],
            self.sha_b: [self.sha_a],
            left: [self.sha_b],
            right: [self.sha_b],
            merge: [left, right],
        }])

        self.assertEqual(snapshot.parents(merge), [left, right])

    def test_dump_rejects_inconsistent_fragment_parents(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)

        with self.assertRaises(ValueError):
            self._candidate.dump(
                [
                    {self.sha_b: [self.sha_a], self.sha_a: []},
                    {self.sha_b: [self.sha_c], self.sha_c: []},
                ],
                target,
            )

    def test_dump_prefers_concrete_parents_over_stub(self) -> None:
        target = tempfile.mkdtemp(dir=self._outdir.name)
        source = self._fixture.repository.get(self.sha_c).peel(pygit2.Commit)
        parent = self._fixture.commit(source.tree.id, [self.sha_c], "parent", ref=None)
        child = self._fixture.commit(source.tree.id, [parent], "child", ref=None)

        self._candidate.dump(
            [
                {child: [parent], parent: []},
                {parent: [self.sha_c], self.sha_c: [self.sha_b]},
            ],
            target,
        )

        snapshot = VersionControlSnapshot.load(target)
        self.addCleanup(snapshot.close)
        self.assertEqual(snapshot.parents(parent), [self.sha_c])


class TestPlatformCache(unittest.TestCase):
    """Tests PlatformCache with a real pygit2 repository.

    Repository layout:

        I --- T1 --- M (merge, parents: T1 + F1)
               \\    /
                F1
    """

    def setUp(self) -> None:
        self._fixture = HistoryFixture()
        self._fixture.__enter__()

        tree = self._fixture.tree({"a.txt": "v0"})
        self.sha_i = self._fixture.commit(tree, [], "initial")

        tree = self._fixture.tree({"a.txt": "v1"})
        self.sha_t1 = self._fixture.commit(tree, [self.sha_i], "trunk")

        tree = self._fixture.tree({"a.txt": "v_feature"})
        self.sha_f1 = self._fixture.commit(tree, [self.sha_t1], "feature", ref=None)

        tree = self._fixture.tree({"a.txt": "v_merged"})
        self.sha_m = self._fixture.commit(
            tree, [self.sha_t1, self.sha_f1], "merge",
        )

        tree = self._fixture.tree({"a.txt": "v_squash"})
        self.sha_s = self._fixture.commit(tree, [self.sha_m], "squash")

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
            "merge_sha": self.sha_m,
            "title": "Add feature",
            "body": "Details here",
        }])
        change = cache.resolve(self.sha_m)
        self.assertEqual(change.number, 10)
        self.assertEqual(change.after, self.sha_m)
        self.assertEqual(change.base, self.sha_t1)
        self.assertEqual(change.description, "Add feature\n\nDetails here")

    def test_resolve_missing(self) -> None:
        cache = self._make_cache([{
            "number": 10,
            "merge_sha": self.sha_m,
        }])
        with self.assertRaises(KeyError):
            cache.resolve("nonexistent_sha")

    @unittest.skip("Current PlatformCache no longer rejects non-merge commits.")
    def test_build_cache_rejects_non_merge(self) -> None:
        with self.assertRaises(NotImplementedError):
            self._make_cache([{
                "number": 20,
                "merge_sha": self.sha_s,
            }])

    def test_load(self) -> None:
        cache = self._make_cache([{
            "number": 42,
            "merge_sha": self.sha_m,
            "title": "Fix bug",
            "body": "",
        }])
        change = cache.load(42)
        self.assertEqual(change.number, 42)
        self.assertEqual(change.after, self.sha_m)
        self.assertEqual(change.description, "Fix bug")

    def test_load_missing(self) -> None:
        cache = self._make_cache([{
            "number": 42,
            "merge_sha": self.sha_m,
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
            path=getattr(item, "path"),
            type=str(getattr(item, "type")),
        )

    def test_linear_modify_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "one\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "two\n"})
            mid = fixture.commit(tree, [init], "mid")

            tree = fixture.tree({"a.py": "three\n"})
            after = fixture.commit(tree, [mid], "after")

            self.assertEqual(
                self._commits(fixture, after, "a.py"),
                self._oracle(fixture, after, "a.py"),
            )

    def test_linear_rename_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"old.py": "one\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"new.py": "one\n"})
            rename = fixture.commit(tree, [init], "rename")

            tree = fixture.tree({"new.py": "two\n"})
            after = fixture.commit(tree, [rename], "after")

            self.assertEqual(
                self._commits(fixture, after, "new.py"),
                self._oracle(fixture, after, "new.py"),
            )

            entries = [self._entry(item) for item in self._entries(fixture, after, "new.py")]
            self.assertEqual(
                [(item.type, item.path) for item in entries],
                [
                    ("modified", "new.py"),
                    ("renamed", ("old.py", "new.py")),
                    ("root", "old.py"),
                ],
            )

    def test_copy_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"src.py": "one\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"src.py": "two\n"})
            source = fixture.commit(tree, [init], "source")

            tree = fixture.tree({
                "src.py": "two\n",
                "copy.py": "two\n",
            })
            copy = fixture.commit(tree, [source], "copy")

            tree = fixture.tree({
                "src.py": "three\n",
                "copy.py": "three\n",
            })
            after = fixture.commit(tree, [copy], "after")

            self.assertEqual(
                self._commits(fixture, after, "copy.py"),
                self._oracle(fixture, after, "copy.py"),
            )

            entries = [self._entry(item) for item in self._entries(fixture, after, "copy.py")]
            self.assertEqual(
                [(item.type, item.path) for item in entries],
                [
                    ("modified", "copy.py"),
                    ("copied", ("src.py", "copy.py")),
                    ("modified", "src.py"),
                    ("root", "src.py"),
                ],
            )

    def test_merge_treesame_to_first_parent_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "left\n"})
            left = fixture.commit(tree, [init], "left")

            tree = fixture.tree({"a.py": "right\n"})
            right = fixture.commit(tree, [init], "right", ref=None)

            tree = fixture.tree({"a.py": "left\n"})
            merge = fixture.commit(tree, [left, right], "merge")

            tree = fixture.tree({"a.py": "after\n"})
            after = fixture.commit(tree, [merge], "after")

            result = self._commits(fixture, after, "a.py")
            self.assertEqual(result, self._oracle(fixture, after, "a.py"))
            self.assertNotIn(merge, result)

    def test_merge_treesame_to_second_parent_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "left\n"})
            left = fixture.commit(tree, [init], "left")

            tree = fixture.tree({"a.py": "right\n"})
            right = fixture.commit(tree, [init], "right", ref=None)

            tree = fixture.tree({"a.py": "right\n"})
            merge = fixture.commit(tree, [left, right], "merge")

            tree = fixture.tree({"a.py": "after\n"})
            after = fixture.commit(tree, [merge], "after")

            result = self._commits(fixture, after, "a.py")
            self.assertEqual(result, self._oracle(fixture, after, "a.py"))
            self.assertNotIn(merge, result)

    def test_merge_differs_from_both_parents_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "left\n"})
            left = fixture.commit(tree, [init], "left")

            tree = fixture.tree({"a.py": "right\n"})
            right = fixture.commit(tree, [init], "right", ref=None)

            tree = fixture.tree({"a.py": "merge\n"})
            merge = fixture.commit(tree, [left, right], "merge")

            tree = fixture.tree({"a.py": "after\n"})
            after = fixture.commit(tree, [merge], "after")

            result = self._commits(fixture, after, "a.py")
            self.assertEqual(result, self._oracle(fixture, after, "a.py"))
            self.assertNotIn(merge, result)

    def test_rename_through_merge_does_not_cross_boundary(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"old.py": "base\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"old.py": "main\n"})
            main = fixture.commit(tree, [init], "main")

            tree = fixture.tree({"old.py": "branch\n"})
            branch = fixture.commit(tree, [init], "branch", ref=None)

            tree = fixture.tree({"new.py": "merge\n"})
            merge = fixture.commit(tree, [main, branch], "merge")

            tree = fixture.tree({"new.py": "after\n"})
            after = fixture.commit(tree, [merge], "after")

            result = self._commits(fixture, after, "new.py")
            self.assertEqual(result, self._oracle(fixture, after, "new.py"))
            self.assertNotIn(init, result)
            self.assertNotIn(merge, result)

    def test_add_delete_root_matches_git(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "root\n"})
            root = fixture.commit(tree, [], "root")

            tree = fixture.tree({})
            delete = fixture.commit(tree, [root], "delete")

            tree = fixture.tree({"a.py": "again\n"})
            after = fixture.commit(tree, [delete], "after")

            self.assertEqual(
                self._commits(fixture, after, "a.py"),
                self._oracle(fixture, after, "a.py"),
            )

            entries = [self._entry(item) for item in self._entries(fixture, after, "a.py")]
            self.assertEqual(
                [(item.type, item.path) for item in entries],
                [
                    ("added", "a.py"),
                    ("deleted", "a.py"),
                    ("root", "a.py"),
                ],
            )

    def test_depth_limits_visible_commits(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "one\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "two\n"})
            two = fixture.commit(tree, [init], "two")

            tree = fixture.tree({"a.py": "three\n"})
            three = fixture.commit(tree, [two], "three")

            tree = fixture.tree({"a.py": "four\n"})
            after = fixture.commit(tree, [three], "after")

            self.assertEqual(
                self._commits(fixture, after, "a.py", depth=2),
                self._oracle(fixture, after, "a.py")[:2],
            )

    def test_iterator_matches_commit_list(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"a.py": "one\n"})
            init = fixture.commit(tree, [], "init")

            tree = fixture.tree({"a.py": "two\n"})
            after = fixture.commit(tree, [init], "after")

            walker = self._walker(fixture, after, "a.py")
            self.assertEqual(
                [str(node.id) for node in walker],
                self._commits(fixture, after, "a.py"),
            )


if __name__ == "__main__":
    unittest.main()
