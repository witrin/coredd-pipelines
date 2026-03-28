from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from benchmark.attribution.history import VersionControl
from benchmark.attribution.knowledge import KnowledgeGraphBuilder, KnowledgeGraphStore


class KnowledgeGraphTool:
    """Tool-oriented interface over the in-memory graph."""

    def __init__(
        self,
        repository: VersionControl,
        store: KnowledgeGraphStore,
    ) -> None:
        self._repository = repository
        self._store = store
        self._reads = 0

    @property
    def store(self) -> KnowledgeGraphStore:
        return self._store

    def tools(self) -> list[BaseTool]:
        return [
            tool(self.candidates),
            tool(self.neighbors),
            tool(self.meta),
            tool(self.content),
        ]

    def candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        """List the top-ranked commit candidates for the prepared correction graph."""
        result: list[dict[str, Any]] = []
        for node in self.store.nodes("commit"):
            payload = self.store.payload(node)
            if payload["type"] == "correction":
                continue
            result.append(
                {
                    "sha": payload["sha"],
                    "type": payload["type"],
                    "files": payload["files"],
                    "score": payload["score"],
                }
            )

        result.sort(
            key=lambda value: (
                -value["score"],
                -self.store.payload(value["sha"])["timestamp"].timestamp(),
                value["sha"],
            )
        )
        return result[:limit]

    def neighbors(self, sha: str) -> list[dict[str, Any]]:
        """Traverse from one commit to structurally related commit candidates."""
        result: dict[str, dict[str, Any]] = {}
        source = self.store.resolve(sha)

        for target in self.store.targets(source, "MODIFIES"):
            for node in self.store.sources(target, "MODIFIES"):
                if node == source:
                    continue
                payload = self.store.payload(node)
                if payload["type"] == "correction":
                    continue
                result[payload["sha"]] = {
                    "sha": payload["sha"],
                    "type": payload["type"],
                    "files": payload["files"],
                    "score": payload["score"],
                }

        values = list(result.values())
        values.sort(
            key=lambda value: (
                -value["score"],
                -self.store.payload(value["sha"])["timestamp"].timestamp(),
                value["sha"],
            )
        )
        return values

    def meta(self, node: str) -> dict[str, Any]:
        """Return the graph kind and payload for one commit or graph node key."""
        key = self.store.resolve(node)
        return {
            "key": key,
            "kind": self.store.kind(key),
            "payload": self.store.payload(key),
        }

    def content(self, sha: str, path: str | None = None) -> dict[str, Any]:
        """Read one commit patch, optionally limited to a single path, for deep inspection."""
        if self._reads >= 30:
            raise ValueError("Diff read budget exhausted.")
        self._reads += 1
        key = self.store.resolve(sha)
        payload = self.store.payload(key)
        parents = self._repository.parents(payload["sha"])
        patch = ""
        if parents:
            diff = self._repository.diff(parents[0], payload["sha"])
            if path is None:
                patch = diff.patch
            else:
                parts: list[str] = []
                for value in diff:
                    if (
                        value.delta.new_file.path == path
                        or value.delta.old_file.path == path
                    ):
                        parts.append(value.text)
                if not parts:
                    files = payload["files"]
                    allowed = ", ".join(files[:8])
                    if len(files) > 8:
                        allowed = f"{allowed}, ..."
                    raise ValueError(
                        f"Commit {payload['sha']} does not modify path {path}. "
                        f"Use one of {allowed}."
                    )
                patch = "".join(parts).strip()
        return {
            "sha": payload["sha"],
            "type": payload["type"],
            "score": payload["score"],
            "message": payload["message"],
            "path": path,
            "patch": patch,
        }


class KnowledgeGraphToolFactory:
    def __init__(
        self,
        repository: VersionControl,
        source: KnowledgeGraphBuilder | Path,
    ) -> None:
        self._repository = repository
        self._source = source

    def create(self, base: str, after: str) -> KnowledgeGraphTool:
        if isinstance(self._source, Path):
            with open(self._source / f"{after}.json", "r", encoding="utf-8") as file:
                store = KnowledgeGraphStore.load(json.load(file))
        else:
            store = self._source.build(base, after)
        return KnowledgeGraphTool(
            repository=self._repository,
            store=store,
        )
