from __future__ import annotations

import inspect
import tempfile
import time
from typing import Any

import pygit2


class HistoryFixture:
    """Lightweight wrapper around a bare pygit2 repository for tests.

    Usage as context manager::

        with HistoryFixture() as fix:
            t = fix.tree({"foo.py": '''
                def foo():
                    pass
            '''})
            sha = fix.commit(t, [], "initial")
            fix.repository  # underlying pygit2.Repository

    File content is ``str``, processed via ``inspect.cleandoc`` so that
    indentation from triple-quoted strings is stripped automatically.
    """

    def __init__(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._repo: pygit2.Repository | None = None
        self._offset = 0

    def __enter__(self) -> HistoryFixture:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._repo = pygit2.init_repository(self._tmpdir.name, bare=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._tmpdir:
            self._tmpdir.cleanup()

    @property
    def repository(self) -> pygit2.Repository:
        assert self._repo is not None, "HistoryFixture not entered as context manager"
        return self._repo

    def tree(self, files: dict[str, str]) -> pygit2.Oid:
        """Create a tree from *files*.

        Values are cleaned via ``inspect.cleandoc`` and a trailing newline
        is ensured so that file content behaves like normal source files.
        """
        for name, content in files.items():
            if not name:
                raise ValueError("tree entry name must not be empty")

        tree: dict[str, Any] = {}
        for name, content in files.items():
            node = tree
            parts = name.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = content

        def build(files: dict[str, Any]) -> pygit2.Oid:
            builder = self.repository.TreeBuilder()
            for name, content in sorted(files.items()):
                if isinstance(content, dict):
                    tree_id = build(content)
                    builder.insert(name, tree_id, pygit2.GIT_FILEMODE_TREE)
                    continue
                cleaned = inspect.cleandoc(content)
                if not cleaned.endswith("\n"):
                    cleaned += "\n"
                blob_id = self.repository.create_blob(cleaned.encode("utf-8"))
                builder.insert(name, blob_id, pygit2.GIT_FILEMODE_BLOB)
            return builder.write()

        return build(tree)

    def commit(
        self,
        tree_id: pygit2.Oid,
        parents: list[pygit2.Oid | str],
        message: str,
        *,
        ref: str | None = "refs/heads/main",
    ) -> str:
        """Create a commit and return the hex SHA.

        *parents* accepts both ``pygit2.Oid`` and hex-string SHAs.
        Timestamps auto-increment so commits are always ordered.
        """
        resolved = [
            pygit2.Oid(hex=p) if isinstance(p, str) else p
            for p in parents
        ]
        self._offset += 1
        sig = pygit2.Signature(
            "Test", "test@test.local", int(time.time()) + self._offset,
        )
        return str(
            self.repository.create_commit(ref, sig, sig, message, tree_id, resolved)
        )
