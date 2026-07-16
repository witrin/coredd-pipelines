from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pygit2

from benchmark.attribution.history import VersionControl
from benchmark.evaluation.annotation import RepositoryProjection


class TestRepositoryProjection(unittest.TestCase):
    def _repository(self, directory: Path):
        repository = pygit2.init_repository(str(directory), bare=False)
        author = pygit2.Signature("foo", "foo@bar.baz")

        def commit(tree, parents):
            return repository.create_commit(
                "HEAD", author, author, "message", tree, parents
            )

        first_blob = repository.create_blob(b"alpha\n")
        builder = repository.TreeBuilder()
        builder.insert("a.py", first_blob, pygit2.GIT_FILEMODE_BLOB)
        base = commit(builder.write(), [])

        second_blob = repository.create_blob(b"beta\n")
        added_blob = repository.create_blob(b"gamma\n")
        builder = repository.TreeBuilder()
        builder.insert("a.py", second_blob, pygit2.GIT_FILEMODE_BLOB)
        builder.insert("b.py", added_blob, pygit2.GIT_FILEMODE_BLOB)
        after = commit(builder.write(), [base])

        return repository, str(base), str(after), {
            "alpha": str(first_blob),
            "beta": str(second_blob),
            "gamma": str(added_blob),
        }

    def test_transform_changes_and_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, base, after, oids = self._repository(Path(directory))
            projection = RepositoryProjection(VersionControl(repository))

            result = projection.transform(after, "defect")

            self.assertIsNotNone(result)
            self.assertEqual(result["sha"], after)
            self.assertEqual(result["labels"], ["defect"])

            changes = result["changes"]
            # a.py changed: alpha -> beta. A change carries git's hunk ranges as
            # [base_start, base_end, after_start, after_end], half-open. The one replaced
            # line is a single hunk spanning base [1,2) and after [1,2).
            self.assertEqual(
                changes["a.py"],
                {
                    "action": "change",
                    "source": oids["alpha"],
                    "target": oids["beta"],
                    "diff": [[1, 2, 1, 2]],
                },
            )
            # b.py added. An add has one empty side and no diff to align, so no ranges.
            self.assertEqual(changes["b.py"], {"action": "add", "target": oids["gamma"]})

            # all three blob ids are referenced and fetchable
            self.assertEqual(
                projection.blobs(),
                {oids["alpha"], oids["beta"], oids["gamma"]},
            )
            self.assertEqual(projection.fetch(oids["beta"]), b"beta\n")
            self.assertEqual(projection.fetch(oids["gamma"]), b"gamma\n")

    def test_root_commit_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, base, after, oids = self._repository(Path(directory))
            projection = RepositoryProjection(VersionControl(repository))
            self.assertIsNone(projection.transform(base, "correction"))

    def test_moved_block_diff_is_gits_not_a_move_pairing(self) -> None:
        """A moved block must be emitted as git cuts it: deletions plus additions.

        This is the whole point of shipping the line-level diff. Monaco's own line diff
        pairs the displaced but textually identical lines ACROSS the move and paints them
        as unchanged; git compacts the hunk boundary and writes the block as removed and
        re-added. A localization marking is validated against git's cut, so if the
        component rendered Monaco's, the marking would sit on a line the diff calls
        untouched. The diff cells in the payload are what keeps the two in step.
        """
        with tempfile.TemporaryDirectory() as directory:
            repository = pygit2.init_repository(str(directory), bare=False)
            author = pygit2.Signature("foo", "foo@bar.baz")

            def commit(tree, parents):
                return repository.create_commit(
                    "HEAD", author, author, "message", tree, parents
                )

            # `block` moves from the top of the file to the bottom, unchanged.
            block = "def check(v):\n    return v\n"
            before = repository.create_blob(
                f"{block}\nHEAD = 1\nTAIL = 2\n".encode()
            )
            builder = repository.TreeBuilder()
            builder.insert("a.py", before, pygit2.GIT_FILEMODE_BLOB)
            base = commit(builder.write(), [])

            after_blob = repository.create_blob(
                f"HEAD = 1\nTAIL = 2\n\n{block}".encode()
            )
            builder = repository.TreeBuilder()
            builder.insert("a.py", after_blob, pygit2.GIT_FILEMODE_BLOB)
            head = commit(builder.write(), [base])

            projection = RepositoryProjection(VersionControl(repository))
            changes = projection.transform(str(head), "defect")["changes"]
            ranges = changes["a.py"]["diff"]

            # git writes a moved block as a deletion hunk plus a separate addition hunk, each
            # one-sided (an empty range on the unchanged side). Monaco would instead pair the
            # displaced lines and leave one side empty across the move. So every range here is
            # one-sided, and both a pure deletion and a pure addition are present.
            for bs, be, as_, ae in ranges:
                self.assertTrue(bs == be or as_ == ae, ranges)
            self.assertTrue(any(bs < be and as_ == ae for bs, be, as_, ae in ranges), ranges)
            self.assertTrue(any(as_ < ae and bs == be for bs, be, as_, ae in ranges), ranges)


if __name__ == "__main__":
    unittest.main()
