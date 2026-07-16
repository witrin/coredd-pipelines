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

    def _diff(self, base: str, sha: str) -> dict[str, list[list[int]]]:
        """The diff per path as the ranges the annotation component renders: one
        `[base_start, base_end, after_start, after_end]` per git hunk, half-open and
        1-based, so a hunk touches `[base_start, base_end)` on the base side and
        `[after_start, after_end)` on the after side. This is git's own hunk boundary — the
        component feeds each range straight to Monaco as a change region and builds its
        line map from the same ranges, reconstructing nothing.

        It is the diff a localization marking was validated against, so it must be the same
        one the component renders: Monaco's own line diff cuts a moved block differently (it
        pairs the displaced but textually identical lines across the move, where git writes
        a deletion plus an addition), and a marking would then land on a line the rendered
        diff paints as unchanged.

        Computed once per commit and looked up per path — `_changes` walks the whole tree,
        so a diff call per path would be quadratic. Rename detection is off (see `_changes`).
        """
        handle = self._repository.handle
        before = handle.get(base).peel(pygit2.Commit).tree
        after = handle.get(sha).peel(pygit2.Commit).tree
        diff = handle.diff(before, after, context_lines=0, interhunk_lines=0)

        result: dict[str, list[list[int]]] = {}
        for patch in diff:
            path = patch.delta.new_file.path or patch.delta.old_file.path
            if path is None:
                continue
            result[path] = [
                [
                    hunk.old_start,
                    hunk.old_start + hunk.old_lines,
                    hunk.new_start,
                    hunk.new_start + hunk.new_lines,
                ]
                for hunk in patch.hunks
            ]
        return result

    def _changes(self, base: str, sha: str) -> dict[str, object]:
        after = self._tree(sha)
        before = self._tree(base)
        diff = self._diff(base, sha)
        result: dict[str, object] = {}

        for path in sorted(set(after) | set(before)):
            source = before.get(path)
            target = after.get(path)

            if source is not None and target is not None:
                if source == target:
                    result[path] = target
                    self._referenced.add(target)
                else:
                    entry: dict[str, object] = {
                        "action": "change",
                        "source": source,
                        "target": target,
                    }
                    # Only a change has two sides to align, so only its line-level diff is
                    # emitted. An add or a delete has one empty side, which the component
                    # renders as a whole-file addition or deletion.
                    if path in diff:
                        entry["diff"] = diff[path]
                    result[path] = entry
                    self._referenced.add(source)
                    self._referenced.add(target)
            elif target is not None:
                result[path] = {"action": "add", "target": target}
                self._referenced.add(target)
            else:
                result[path] = {"action": "delete", "source": source}
                self._referenced.add(source)

        return result
