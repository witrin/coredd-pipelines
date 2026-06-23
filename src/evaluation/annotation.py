import json
from pathlib import Path

import pygit2


class RepositorySnapshotProjection:
    """Projects snapshot data into the annotation component format."""

    def __init__(self, commits: dict[str, dict[str, object]], snapshot: Path) -> None:
        self._commits = commits
        self._snapshot = snapshot
        self._trees: dict[str, dict[str, str]] = {}
        self._referenced: set[str] = set()

    def transform(self, sha: str, label: str) -> dict[str, object] | None:
        """Build a commit dict with changes for the annotation format.

        Returns None if the commit is missing from the snapshot or has no
        parent (changes cannot be computed without a base tree).
        """
        entry = self._commits.get(sha)

        if entry is None:
            return None

        changes = self._changes(sha)

        if changes is None:
            return None

        return {
            "sha": sha,
            "author": entry["author"],
            "message": entry["message"],
            "time": entry["time"],
            "labels": [label],
            "changes": changes,
        }

    def blobs(self) -> set[str]:
        """Return all blob SHAs referenced by prior transform() calls."""
        return set(self._referenced)

    def _load(self, sha: str) -> dict[str, str]:
        if sha in self._trees:
            return self._trees[sha]

        path = self._snapshot / "trees" / f"{sha}.json"

        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)

        self._trees[sha] = data["files"]
        return self._trees[sha]

    def _changes(self, sha: str) -> dict[str, object] | None:
        entry = self._commits.get(sha)

        if entry is None or not entry.get("parents"):
            return None

        parent = entry["parents"][0]
        predecessor = self._commits.get(parent)

        if predecessor is None:
            return None

        after = self._load(entry["tree"])
        base = self._load(predecessor["tree"])
        result: dict[str, object] = {}

        for path in sorted(set(after) | set(base)):
            before = base.get(path)
            current = after.get(path)

            if before is not None and current is not None:
                if before == current:
                    result[path] = current
                    self._referenced.add(current)
                else:
                    result[path] = {
                        "action": "change",
                        "source": before,
                        "target": current,
                    }
                    self._referenced.add(before)
                    self._referenced.add(current)
            elif current is not None:
                result[path] = {"action": "add", "target": current}
                self._referenced.add(current)
            else:
                result[path] = {"action": "delete", "source": before}
                self._referenced.add(before)

        return result


class RepositoryProjection:
    """Projects a live repository into the annotation component format.

    Mirrors the output of RepositorySnapshotProjection but reads commit trees
    and blob contents directly from version control rather than a snapshot
    directory. Blobs are keyed by their git object id, so identical content is
    referenced once across paths, commits, and cases.
    """

    def __init__(self, repository) -> None:
        self._repository = repository
        self._referenced: set[str] = set()
        self._bytes: dict[str, bytes] = {}

    def transform(self, sha: str, label: str) -> dict[str, object] | None:
        """Build a commit dict with changes for the annotation format.

        Returns None if the commit has no parent, since changes cannot be
        computed without a base tree.
        """
        parents = self._repository.parents(sha)

        if not parents:
            return None

        meta = self._repository.meta(sha)
        changes = self._changes(parents[0], sha)

        return {
            "sha": sha,
            "author": meta.author,
            "message": meta.message,
            "time": meta.time.isoformat(),
            "labels": [label],
            "changes": changes,
        }

    def blobs(self) -> set[str]:
        """Return all blob ids referenced by prior transform() calls."""
        return set(self._referenced)

    def fetch(self, blob: str) -> bytes:
        """Return the raw bytes of a referenced blob id."""
        return self._bytes[blob]

    def _tree(self, sha: str) -> dict[str, str]:
        handle = self._repository.handle
        commit = handle.get(sha).peel(pygit2.Commit)
        files: dict[str, str] = {}
        self._collect(handle, commit.tree, "", files)
        return files

    def _collect(self, handle, tree, prefix: str, files: dict[str, str]) -> None:
        for entry in tree:
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            obj = handle.get(entry.id)
            if isinstance(obj, pygit2.Tree):
                self._collect(handle, obj, path, files)
            elif isinstance(obj, pygit2.Blob):
                identifier = str(entry.id)
                files[path] = identifier
                if identifier not in self._bytes:
                    self._bytes[identifier] = obj.data

    def _changes(self, base: str, sha: str) -> dict[str, object]:
        after = self._tree(sha)
        before = self._tree(base)
        result: dict[str, object] = {}

        for path in sorted(set(after) | set(before)):
            source = before.get(path)
            target = after.get(path)

            if source is not None and target is not None:
                if source == target:
                    result[path] = target
                    self._referenced.add(target)
                else:
                    result[path] = {
                        "action": "change",
                        "source": source,
                        "target": target,
                    }
                    self._referenced.add(source)
                    self._referenced.add(target)
            elif target is not None:
                result[path] = {"action": "add", "target": target}
                self._referenced.add(target)
            else:
                result[path] = {"action": "delete", "source": source}
                self._referenced.add(source)

        return result
