import json
from pathlib import Path


class Projection:
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
