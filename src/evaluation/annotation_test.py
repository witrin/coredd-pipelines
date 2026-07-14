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
            # a.py changed: alpha -> beta. A change carries the hunks the annotation
            # component renders the diff from; the one line is replaced, so one hunk.
            self.assertEqual(
                changes["a.py"],
                {
                    "action": "change",
                    "source": oids["alpha"],
                    "target": oids["beta"],
                    "hunks": [[1, 1, 1, 1]],
                },
            )
            # b.py added. An add has one empty side and no diff to align, so no hunks.
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

    def test_moved_block_hunks_are_gits_not_a_move_pairing(self) -> None:
        """A moved block must be emitted as git cuts it: a deletion plus an addition.

        This is the whole point of shipping the hunks. Monaco's own line diff pairs the
        displaced but textually identical lines ACROSS the move and paints them as
        unchanged; git compacts the hunk boundary and writes the block as removed and
        re-added. A localization marking is validated against git's cut, so if the
        component rendered Monaco's, the marking would sit on a line the diff calls
        untouched. The hunks in the payload are what keeps the two in step.
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
            hunks = changes["a.py"]["hunks"]

            # Every hunk is one-sided: lines only leave, or only arrive. A move pairing
            # would instead produce a hunk with a non-zero count on BOTH sides.
            self.assertTrue(
                all(old == 0 or new == 0 for _, old, _, new in hunks),
                f"a moved block must not be paired across the move: {hunks}",
            )
            self.assertTrue(any(old > 0 for _, old, _, _ in hunks), hunks)
            self.assertTrue(any(new > 0 for _, _, _, new in hunks), hunks)


if __name__ == "__main__":
    unittest.main()
