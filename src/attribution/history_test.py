import json
import tempfile
import unittest
from datetime import datetime, timezone

import pyarrow as pa
import pygit2

from benchmark.attribution.domain import Change, Commit, FileOperation, OperationType
from benchmark.attribution.history import PlatformCache, VersionControl
from benchmark.attribution.history_fixture import HistoryFixture


class TestVersionControlParents(unittest.TestCase):
    """Tests parents() across merge, squash and rebase strategies.

    Repository layout:

        I --- T1 --- T2 --- M (merge, parents: T2 + F2) --- S (squash) --- R1 --- R2
               \\           /
                F1 --- F2
    """

    def setUp(self) -> None:
        self._fix = HistoryFixture()
        self._fix.__enter__()

        t_i = self._fix.tree({"foo.txt": "initial"})
        t_t1 = self._fix.tree({"foo.txt": "initial\ntrunk work"})
        t_t2 = self._fix.tree({"foo.txt": "initial\ntrunk work\nmore trunk"})

        t_f1 = self._fix.tree({"foo.txt": "initial\ntrunk work\nfeature a"})
        t_f2 = self._fix.tree({"foo.txt": "initial\ntrunk work\nfeature a\nfeature b"})

        t_m = self._fix.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b"})
        t_s = self._fix.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b\nsquashed"})
        t_r1 = self._fix.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b\nsquashed\nrebased 1"})
        t_r2 = self._fix.tree({"foo.txt": "initial\ntrunk work\nmore trunk\nfeature a\nfeature b\nsquashed\nrebased 1\nrebased 2"})

        self.sha_i = self._fix.commit(t_i, [], "initial")
        self.sha_t1 = self._fix.commit(t_t1, [self.sha_i], "trunk 1")

        self.sha_f1 = self._fix.commit(t_f1, [self.sha_t1], "feature 1", ref=None)
        self.sha_f2 = self._fix.commit(t_f2, [self.sha_f1], "feature 2", ref=None)

        self.sha_t2 = self._fix.commit(t_t2, [self.sha_t1], "trunk 2")

        self.sha_m = self._fix.commit(
            t_m, [self.sha_t2, self.sha_f2],
            "merge feature into trunk",
        )
        self.sha_s = self._fix.commit(t_s, [self.sha_m], "squash feature2")
        self.sha_r1 = self._fix.commit(t_r1, [self.sha_s], "rebased 1")
        self.sha_r2 = self._fix.commit(t_r2, [self.sha_r1], "rebased 2")

        self.candidate = VersionControl(self._fix.repository)

    def tearDown(self) -> None:
        self._fix.__exit__(None, None, None)

    def test_parents_root(self) -> None:
        self.assertEqual(self.candidate.parents(self.sha_i), [])

    def test_parents_merge_returns_both(self) -> None:
        result = self.candidate.parents(self.sha_m)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], self.sha_t2)
        self.assertEqual(result[1], self.sha_f2)

    def test_parents_squash(self) -> None:
        result = self.candidate.parents(self.sha_s)
        self.assertEqual(result, [self.sha_m])

    def test_parents_rebase_chain(self) -> None:
        self.assertEqual(self.candidate.parents(self.sha_r2), [self.sha_r1])
        self.assertEqual(self.candidate.parents(self.sha_r1), [self.sha_s])

    def test_parents_feature_branch(self) -> None:
        self.assertEqual(self.candidate.parents(self.sha_f2), [self.sha_f1])
        self.assertEqual(self.candidate.parents(self.sha_f1), [self.sha_t1])


class TestVersionControlBlame(unittest.TestCase):
    def setUp(self) -> None:
        self._fix = HistoryFixture()
        self._fix.__enter__()

        t_i = self._fix.tree({"foo.txt": "line1\nline2\nline3"})
        t_f1 = self._fix.tree({"foo.txt": "line1\nline2 changed\nline3"})
        t_f2 = self._fix.tree({"foo.txt": "line1\nline2 changed\nline3\nnew feature line"})
        t_x1 = self._fix.tree({"foo.txt": "line1\nline2 fixed\nline3\nnew feature line"})
        t_x2 = self._fix.tree({"foo.txt": "line1\nline2 fixed\nline3\nnew feature line\nfix done"})

        self.sha_i = self._fix.commit(t_i, [], "initial")
        self.sha_f1 = self._fix.commit(t_f1, [self.sha_i], "feature 1")
        self.sha_f2 = self._fix.commit(t_f2, [self.sha_f1], "feature 2")
        self.sha_x1 = self._fix.commit(t_x1, [self.sha_f2], "fix 1")
        self.sha_x2 = self._fix.commit(t_x2, [self.sha_x1], "fix 2")

        self.candidate = VersionControl(self._fix.repository)

    def tearDown(self) -> None:
        self._fix.__exit__(None, None, None)

    def test_blame_returns_feature_and_fix_commits(self) -> None:
        shas = self.candidate.blame("foo.txt", self.sha_x2, [(1, 5)])
        self.assertIn(self.sha_i, shas)
        self.assertIn(self.sha_x1, shas)
        self.assertIn(self.sha_x2, shas)

    def test_blame_single_line_fix(self) -> None:
        shas = self.candidate.blame("foo.txt", self.sha_x2, [(2, 2)])
        self.assertEqual(shas, [self.sha_x1])

    def test_blame_unchanged_line(self) -> None:
        shas = self.candidate.blame("foo.txt", self.sha_x2, [(1, 1)])
        self.assertEqual(shas, [self.sha_i])


class TestVersionControlMaterialize(unittest.TestCase):
    def setUp(self) -> None:
        self._fix = HistoryFixture()
        self._fix.__enter__()
        self._outdir = tempfile.TemporaryDirectory()

        t_base = self._fix.tree({"foo.txt": "base content"})
        t_after = self._fix.tree({"foo.txt": "after content"})

        self.sha_base = self._fix.commit(t_base, [], "base")
        self.sha_after = self._fix.commit(t_after, [self.sha_base], "after")

        self.candidate = VersionControl(self._fix.repository)

    def tearDown(self) -> None:
        self._fix.__exit__(None, None, None)
        self._outdir.cleanup()

    def test_creates_base_and_after_dirs(self) -> None:
        base_dir, after_dir, _ = self.candidate.materialize(
            self.sha_base, self.sha_after, self._outdir.name
        )
        self.assertTrue(base_dir.is_dir())
        self.assertTrue(after_dir.is_dir())

    def test_file_content_matches_commits(self) -> None:
        base_dir, after_dir, _ = self.candidate.materialize(
            self.sha_base, self.sha_after, self._outdir.name
        )
        self.assertEqual(
            (base_dir / "foo.txt").read_bytes(), b"base content\n"
        )
        self.assertEqual(
            (after_dir / "foo.txt").read_bytes(), b"after content\n"
        )

    def test_manifest_contains_edit(self) -> None:
        _, _, ops = self.candidate.materialize(
            self.sha_base, self.sha_after, self._outdir.name
        )
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].operation, OperationType.EDIT)
        self.assertEqual(ops[0].base, "foo.txt")
        self.assertEqual(ops[0].after, "foo.txt")


class TestVersionControlManifest(unittest.TestCase):
    def setUp(self) -> None:
        self._fix = HistoryFixture()
        self._fix.__enter__()
        self._outdir = tempfile.TemporaryDirectory()

        t_i = self._fix.tree({"foo.txt": "initial"})
        t_c = self._fix.tree({"foo.txt": "changed", "bar.txt": "new"})

        self.sha_i = self._fix.commit(t_i, [], "initial")
        self.sha_c = self._fix.commit(t_c, [self.sha_i], "change")

        self.candidate = VersionControl(self._fix.repository)

    def tearDown(self) -> None:
        self._fix.__exit__(None, None, None)
        self._outdir.cleanup()

    def test_manifest_add_and_edit(self) -> None:
        _, _, ops = self.candidate.materialize(
            self.sha_i, self.sha_c, self._outdir.name
        )
        by_path = {(o.base, o.after): o.operation for o in ops}
        self.assertEqual(by_path[(None, "bar.txt")], OperationType.ADD)
        self.assertEqual(by_path[("foo.txt", "foo.txt")], OperationType.EDIT)

    def test_manifest_no_changes(self) -> None:
        _, _, ops = self.candidate.materialize(
            self.sha_i, self.sha_i, self._outdir.name
        )
        self.assertEqual(ops, [])


class TestVersionControlMeta(unittest.TestCase):
    def setUp(self) -> None:
        self._fix = HistoryFixture()
        self._fix.__enter__()

        t_i = self._fix.tree({"a.txt": "v0"})
        self.sha_i = self._fix.commit(t_i, [], "initial commit")
        self.candidate = VersionControl(self._fix.repository)

    def tearDown(self) -> None:
        self._fix.__exit__(None, None, None)

    def test_meta_returns_commit_info(self) -> None:
        result = self.candidate.meta(self.sha_i)
        self.assertIsInstance(result, Commit)
        self.assertEqual(result.message, "initial commit")
        self.assertEqual(result.author, "Test")
        self.assertIsInstance(result.time, datetime)
        self.assertEqual(result.time.tzinfo, timezone.utc)

    def test_meta_not_found(self) -> None:
        with self.assertRaises(ValueError):
            self.candidate.meta("0" * 40)


class TestVersionControlLog(unittest.TestCase):
    """Repository layout:

        I --- A --- B --- C
               \\         /
                F1 --- F2
    """

    def setUp(self) -> None:
        self._fix = HistoryFixture()
        self._fix.__enter__()

        t_i = self._fix.tree({"a.txt": "v0"})
        t_a = self._fix.tree({"a.txt": "v1"})
        t_b = self._fix.tree({"a.txt": "v2"})
        t_f1 = self._fix.tree({"a.txt": "vf1"})
        t_f2 = self._fix.tree({"a.txt": "vf2"})
        t_c = self._fix.tree({"a.txt": "v_merged"})

        self.sha_i = self._fix.commit(t_i, [], "initial")
        self.sha_a = self._fix.commit(t_a, [self.sha_i], "A")
        self.sha_f1 = self._fix.commit(t_f1, [self.sha_a], "F1", ref=None)
        self.sha_f2 = self._fix.commit(t_f2, [self.sha_f1], "F2", ref=None)
        self.sha_b = self._fix.commit(t_b, [self.sha_a], "B")
        self.sha_c = self._fix.commit(
            t_c, [self.sha_b, self.sha_f2], "C (merge)",
        )

        self.candidate = VersionControl(self._fix.repository)

    def tearDown(self) -> None:
        self._fix.__exit__(None, None, None)

    def test_log_linear(self) -> None:
        result = self.candidate.log(self.sha_i, self.sha_b)
        self.assertEqual(result, [self.sha_a])

    def test_log_excludes_merge(self) -> None:
        result = self.candidate.log(self.sha_i, self.sha_c, merge=False)
        self.assertIn(self.sha_a, result)
        self.assertIn(self.sha_b, result)
        self.assertIn(self.sha_f1, result)
        self.assertIn(self.sha_f2, result)

    def test_log_includes_merge(self) -> None:
        result = self.candidate.log(self.sha_i, self.sha_c, merge=True)
        self.assertIn(self.sha_a, result)
        self.assertIn(self.sha_b, result)
        self.assertIn(self.sha_f1, result)
        self.assertIn(self.sha_f2, result)
        self.assertNotIn(self.sha_c, result)

    def test_log_empty(self) -> None:
        result = self.candidate.log(self.sha_a, self.sha_b)
        self.assertEqual(result, [])


class TestPlatformCache(unittest.TestCase):
    """Tests PlatformCache with a real pygit2 repository.

    Repository layout:

        I --- T1 --- M (merge, parents: T1 + F1)
               \\    /
                F1
    """

    def setUp(self) -> None:
        self._fix = HistoryFixture()
        self._fix.__enter__()

        t_i = self._fix.tree({"a.txt": "v0"})
        t_t1 = self._fix.tree({"a.txt": "v1"})
        t_f1 = self._fix.tree({"a.txt": "v_feature"})
        t_m = self._fix.tree({"a.txt": "v_merged"})
        t_s = self._fix.tree({"a.txt": "v_squash"})

        self.sha_i = self._fix.commit(t_i, [], "initial")
        self.sha_t1 = self._fix.commit(t_t1, [self.sha_i], "trunk")
        self.sha_f1 = self._fix.commit(t_f1, [self.sha_t1], "feature", ref=None)
        self.sha_m = self._fix.commit(
            t_m, [self.sha_t1, self.sha_f1], "merge",
        )
        self.sha_s = self._fix.commit(t_s, [self.sha_m], "squash")

        self._vc = VersionControl(self._fix.repository)

    def tearDown(self) -> None:
        self._fix.__exit__(None, None, None)

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
        return PlatformCache(table, self._vc)

    def test_resolve_merge_commit(self) -> None:
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

    def test_resolve_not_found(self) -> None:
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

    def test_change_from_payload_missing_merge_sha(self) -> None:
        with self.assertRaises(ValueError):
            PlatformCache._change_from_payload(1, {}, "some_base")

    def test_load_by_number(self) -> None:
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

    def test_load_not_found(self) -> None:
        cache = self._make_cache([{
            "number": 42,
            "merge_sha": self.sha_m,
        }])
        with self.assertRaises(KeyError):
            cache.load(999)


if __name__ == "__main__":
    unittest.main()
