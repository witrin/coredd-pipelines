from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from langchain_core.tools import BaseTool, tool
import pygit2

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
        self._target: dict[str, list[str]] = {}
        self._scopes: dict[str, set[tuple[str, ...]]] = {}

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
            category = self._category(payload)
            result.append(
                {
                    "sha": payload["sha"],
                    "type": category,
                    "score": self._score(category),
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
        source_targets = self._targets(source)
        for target in source_targets:
            for node in self._sources(target):
                if node == source:
                    continue
                payload = self.store.payload(node)
                if payload["type"] == "correction":
                    continue
                category = self._category(payload)
                item = result.setdefault(
                    payload["sha"],
                    {
                        "sha": payload["sha"],
                        "type": category,
                        "score": self._score(category),
                    },
                )

        values = list(result.values())
        scope = self._scope(source_targets)
        for item in values:
            item["entities"] = self._entities(
                scope,
                self._scope(
                    self._targets(self.store.resolve(cast(str, item["sha"])))
                ),
            )
        values.sort(
            key=lambda value: (
                -value["score"],
                -self._precision(cast(list[str], value["entities"])),
                -self.store.payload(value["sha"])["timestamp"].timestamp(),
                value["sha"],
            )
        )
        return values

    def meta(self, node: str) -> dict[str, Any]:
        """Return the graph kind and curated payload for one graph node."""
        key = self.store.resolve(node)
        kind = self.store.kind(key)
        return {
            "key": key,
            "kind": kind,
            "payload": self._meta(key, kind),
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
            patch = self._render(diff, path)
            if path is not None and not patch:
                files = payload["files"]
                allowed = ", ".join(files[:8])
                if len(files) > 8:
                    allowed = f"{allowed}, ..."
                raise ValueError(
                    f"Commit {payload['sha']} does not modify path {path}. "
                    f"Use one of {allowed}."
                )
        return {
            "sha": payload["sha"],
            "type": self._category(payload),
            "score": self._score(self._category(payload)),
            "message": payload["message"],
            "path": path,
            "patch": patch,
        }

    def _render(
        self,
        diff: pygit2.Diff,
        path: str | None,
    ) -> str:
        parts: list[str] = []

        for value in diff:
            old = value.delta.old_file.path
            new = value.delta.new_file.path
            if path is not None and old != path and new != path:
                continue

            source = "/dev/null" if old is None else f"a/{old}"
            target = "/dev/null" if new is None else f"b/{new}"
            parts.append(f"diff --git {source} {target}")
            parts.append(f"--- {source}")
            parts.append(f"+++ {target}")

            for hunk in value.hunks:
                parts.append(hunk.header.rstrip("\n"))
                parts.extend(str(line).rstrip("\n") for line in hunk.lines)

        return "\n".join(parts).strip()

    def _meta(
        self,
        key: str,
        kind: str,
    ) -> dict[str, Any]:
        payload = self.store.payload(key)

        if kind == "commit":
            return {
                name: payload[name]
                for name in ("sha", "message", "timestamp", "files")
                if name in payload
            } | {"type": self._category(payload)}

        if kind == "file":
            return {
                name: payload[name]
                for name in ("path",)
                if name in payload
            }

        return {
            name: payload[name]
            for name in ("kind", "path", "urn", "name", "source")
            if name in payload
        }

    def _category(self, payload: dict[str, Any]) -> str:
        current = payload.get("type")
        if isinstance(current, str):
            return current.replace("_", "-")
        raise ValueError("Commit payload requires type.")

    def _score(self, category: str) -> float:
        if category == "blame":
            return 1.0
        if category == "blame-ancestor":
            return 0.6
        if category == "correction-ancestor":
            return 0.3
        if category == "correction":
            return 0.0
        raise ValueError(f"Unsupported commit category {category}.")

    def _targets(self, node: str) -> list[str]:
        current = self._target.get(node)
        if current is not None:
            return current

        result: list[str] = []
        seen: set[str] = set()
        for edge in ("ADD", "EDIT", "DELETE", "MODIFIES"):
            for target in self.store.targets(node, edge):
                if target in seen:
                    continue
                seen.add(target)
                result.append(target)
        self._target[node] = result
        return result

    def _sources(self, node: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        edges = ("MODIFIES",) if node.startswith("file:") else ("ADD", "EDIT", "DELETE")
        for edge in edges:
            for source in self.store.sources(node, edge):
                if source in seen:
                    continue
                seen.add(source)
                result.append(source)
        return result

    def _entities(
        self,
        source: set[tuple[str, ...]],
        target: set[tuple[str, ...]],
    ) -> list[str]:
        nodes = sorted(":".join(value) for value in self._trim(source & target))
        items = [value for value in nodes if ":" in value]
        return items or nodes

    def _precision(self, values: list[str]) -> int:
        return 1 if any(":" in value for value in values) else 0

    def _scope(
        self,
        nodes: list[str],
    ) -> set[tuple[str, ...]]:
        result: set[tuple[str, ...]] = set()

        for node in nodes:
            current = self._scopes.get(node)
            if current is None:
                current = self._expand(self._path(node))
                self._scopes[node] = current
            result.update(current)

        return result

    def _path(self, node: str) -> tuple[str, ...]:
        if node.startswith("file:"):
            return (f"file={node.removeprefix('file:')}",)

        payload = self.store.payload(node)
        path = str(payload["path"])
        urn = str(payload["urn"]).removeprefix("entity:")
        if urn == "program":
            return (f"file={path}",)
        return (f"file={path}", *tuple(urn.split(":")))

    def _expand(
        self,
        value: tuple[str, ...],
    ) -> set[tuple[str, ...]]:
        return {value[:index] for index in range(1, len(value) + 1)}

    def _trim(
        self,
        values: set[tuple[str, ...]],
    ) -> list[tuple[str, ...]]:
        result: list[tuple[str, ...]] = []

        for current in sorted(values, key=lambda value: (-len(value), value)):
            if any(self._starts(current, item) for item in result):
                continue
            result.append(current)

        return sorted(result)

    def _starts(
        self,
        prefix: tuple[str, ...],
        value: tuple[str, ...],
    ) -> bool:
        if len(prefix) >= len(value):
            return False
        return value[: len(prefix)] == prefix


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
