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
            # a.py changed: alpha -> beta
            self.assertEqual(
                changes["a.py"],
                {"action": "change", "source": oids["alpha"], "target": oids["beta"]},
            )
            # b.py added
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


if __name__ == "__main__":
    unittest.main()
