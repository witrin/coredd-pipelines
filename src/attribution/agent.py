from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, cast

from langchain_core.tools import BaseTool, tool
import pygit2

from benchmark.attribution.diff.entity import EntityPath
from benchmark.attribution.domain import FileOperation
from benchmark.attribution.history import VersionControl, VersionControlPathWalker
from benchmark.attribution.knowledge import KnowledgeGraphBuilder, KnowledgeGraphStore


class DiffRenderer:
    """Render a pygit2.Diff as a unified-diff string, optionally path-scoped."""

    def render(self, diff: pygit2.Diff, path: str | None = None) -> str:
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
                parts.extend(
                    f"{line.origin}{line.content}".rstrip("\n") for line in hunk.lines
                )

        return "\n".join(parts).strip()


class KnowledgeGraphTool:
    """Tool-oriented interface over the in-memory graph."""

    def __init__(
        self,
        repository: VersionControl,
        store: KnowledgeGraphStore,
        budget: int = 3,
    ) -> None:
        self._repository = repository
        self._store = store
        self._reads = 0
        self._budget = budget
        self._target: dict[str, list[str]] = {}
        self._scopes: dict[str, set[tuple[str, ...]]] = {}
        self._renderer = DiffRenderer()

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
        """List the top-ranked commit candidates for the prepared correction graph.

        Use this first to inspect the ranked search space. Each item returns one
        candidate commit with its ordinal score, commit metadata, and the symbols
        (classes, methods, functions) touched by that commit.
        """
        result: list[dict[str, Any]] = []
        for node in self.store.nodes("commit"):
            payload = self.store.payload(node)
            if payload["type"] == "correction":
                continue
            result.append(self._commit(node))

        result.sort(
            key=lambda value: (
                -value["score"],
                -self.store.payload(value["sha"])["timestamp"].timestamp(),
                value["sha"],
            )
        )
        return result[:limit]

    def neighbors(self, sha: str) -> list[dict[str, Any]]:
        """Traverse from one commit to structurally related commit candidates.

        Use this to find structurally related candidate commits. Each item returns a
        related commit together with the shared symbols (classes, methods, functions)
        between the queried commit and that neighbor.
        """
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
                item = result.setdefault(payload["sha"], self._commit(node))

        values = list(result.values())
        scope = self._scope(source_targets)
        for item in values:
            item["symbols"] = self._entities(
                scope,
                self._scope(self._targets(self.store.resolve(cast(str, item["sha"])))),
            )
        values.sort(
            key=lambda value: (
                -value["score"],
                -self._precision(cast(list[str], value["symbols"])),
                -self.store.payload(value["sha"])["timestamp"].timestamp(),
                value["sha"],
            )
        )
        return values

    def meta(self, node: str) -> dict[str, Any]:
        """Inspect one commit or code symbol.

        For commits, this returns commit metadata, the symbols (classes, methods,
        functions) touched by the commit, and the shared symbols between the commit
        and the Correction.
        """
        key = self.store.resolve(node)
        kind = self.store.kind(key)
        return {"key": key, "kind": kind} | self._meta(key, kind)

    def content(self, sha: str, path: str | None = None) -> dict[str, Any]:
        """Read one commit patch, optionally limited to a single path, for deep inspection.

        Use this for causal verification. It returns the commit diff, optionally
        limited to one path.
        """
        if self._reads >= self._budget:
            raise ValueError("Diff read budget exhausted.")
        self._reads += 1
        key = self.store.resolve(sha)
        payload = self.store.payload(key)
        parents = self._repository.parents(payload["sha"])
        patch = ""
        if parents:
            diff = self._repository.diff(parents[0], payload["sha"])
            patch = self._renderer.render(diff, path)
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

    def _meta(
        self,
        key: str,
        kind: str,
    ) -> dict[str, Any]:
        payload = self.store.payload(key)

        if kind == "commit":
            return {
                "sha": payload["sha"],
                "type": self._category(payload),
                "message": payload["message"],
                "timestamp": self._timestamp(payload["timestamp"]),
                "files": self._files(payload),
                "symbols": self._touched(key),
                "overlap": self._overlap(key),
            }

        if kind == "file":
            return {name: payload[name] for name in ("path",) if name in payload}

        return {
            name: payload[name]
            for name in ("kind", "path", "urn", "name", "source")
            if name in payload
        }

    def _commit(self, key: str) -> dict[str, Any]:
        payload = self.store.payload(key)
        category = self._category(payload)
        return {
            "sha": payload["sha"],
            "type": category,
            "score": self._score(category),
            "message": payload["message"],
            "timestamp": self._timestamp(payload["timestamp"]),
            "files": self._files(payload),
            "symbols": self._touched(key),
        }

    def _category(self, payload: dict[str, Any]) -> str:
        current = payload.get("type")
        if isinstance(current, str):
            return current.replace("_", "-")
        raise ValueError("Commit payload requires type.")

    def _files(self, payload: dict[str, Any]) -> list[str]:
        return list(cast(tuple[str, ...] | list[str], payload.get("files", ())))

    def _touched(self, key: str) -> list[str]:
        grouped: dict[str, set[str]] = {}
        for edge in ("ADD", "EDIT", "DELETE"):
            for target in self.store.targets(key, edge):
                if target.startswith("file:"):
                    continue
                value = self.store.payload(target)
                if value.get("urn") == "entity:program":
                    continue
                path = str(value["path"])
                name = self._symbol(value)
                if name is not None:
                    grouped.setdefault(path, set()).add(name)
        return self._grouped(grouped)[:10]

    @staticmethod
    def _symbol(payload: dict[str, Any]) -> str | None:
        urn = str(payload.get("urn", "")).removeprefix("entity:")
        if not urn or urn == "program":
            return None
        parts = urn.split(":")
        return ".".join(part.split("=", 1)[-1] for part in parts)

    @staticmethod
    def _grouped(grouped: dict[str, set[str]]) -> list[str]:
        result: list[str] = []
        for path in sorted(grouped):
            names = sorted(grouped[path])
            result.append(f"{path}: {', '.join(names)}")
        return result

    def _overlap(self, key: str) -> list[str]:
        if self.store.after is None:
            return []
        correction = self.store.resolve(self.store.after)
        if key == correction:
            return []
        return self._entities(
            self._scope(self._targets(correction)),
            self._scope(self._targets(key)),
        )

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
        trimmed = self._trim(source & target)
        grouped: dict[str, set[str]] = {}
        file_only: list[str] = []
        for tup in trimmed:
            if len(tup) == 1:
                file_only.append(tup[0].removeprefix("file="))
            else:
                path = tup[0].removeprefix("file=")
                name = ".".join(
                    part.split("=", 1)[-1] for part in tup[1:]
                )
                grouped.setdefault(path, set()).add(name)
        items = self._grouped(grouped)
        return items or sorted(file_only)

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

    @staticmethod
    def _timestamp(value: object) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

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
        filter: Callable[[str, str], str] | None = None,
    ) -> None:
        self._repository = repository
        self._source = source
        self._filter = filter

    def create(self, base: str, after: str) -> KnowledgeGraphTool:
        if isinstance(self._source, Path):
            with open(self._source / f"{after}.json", "r", encoding="utf-8") as file:
                store = KnowledgeGraphStore.load(json.load(file), filter=self._filter)
        else:
            store = self._source.build(base, after)
            if self._filter is not None:
                store._filter = self._filter
        return KnowledgeGraphTool(
            repository=self._repository,
            store=store,
        )


class RepositoryTool:
    """Repository-native tool surface paralleling AgentSZZ's git-CLI toolset.

    Exposes ``show``, ``blame``, ``pickaxe``, ``trace`` and ``grep`` over a
    :class:`VersionControl` instance so the search agent can operate on the
    repository without a prebuilt knowledge graph.
    """

    def __init__(
        self,
        repository: VersionControl,
        after: str,
        *,
        budget: int = 15,
    ) -> None:
        self._repository = repository
        self._after = after
        self._budget = budget
        self._reads = 0
        self._renderer = DiffRenderer()

    def tools(self) -> list[BaseTool]:
        return [
            tool(self.show),
            tool(self.blame),
            tool(self.pickaxe),
            tool(self.trace),
            tool(self.grep),
        ]

    def _resolve(self, commit: str) -> str:
        node = self._repository.handle.revparse_single(commit)
        return str(node.peel(pygit2.Commit).id)

    def show(self, commit: str, path: str | None = None) -> dict[str, Any]:
        """Return commit metadata and its unified diff against the first parent.

        Accepts any git revision expression (``<sha>``, ``<sha>^``, ``<sha>~1``,
        ``HEAD``, branch names, …). Use this to read the exact change introduced
        by a commit. Optionally restrict the diff to a single ``path``.
        """
        commit = self._resolve(commit)
        meta = self._repository.meta(commit)
        parents = self._repository.parents(commit)
        patch = ""
        if parents:
            diff = self._repository.diff(parents[0], commit)
            patch = self._renderer.render(diff, path)
        return {
            "sha": commit,
            "message": meta.message,
            "timestamp": self._timestamp(meta.time),
            "author": meta.author,
            "parents": parents,
            "path": path,
            "patch": patch,
        }

    def blame(
        self,
        path: str,
        commit: str,
        start: int,
        end: int,
    ) -> dict[str, Any]:
        """Blame one contiguous line range in a file at a given commit.

        Accepts any git revision expression for ``commit``. Returns ``blame``
        as a string with one ``L<n>: <sha> | <origin_path> | <code>`` entry per
        line and ``commits`` as a map from each referenced SHA to the first
        line of its commit message.
        """
        commit = self._resolve(commit)
        raw = self._repository.blame(path, commit, [(start, end)])
        try:
            data = self._repository.blob(commit, path).decode("utf-8", errors="replace")
        except (FileNotFoundError, ValueError):
            data = ""
        source = data.splitlines()

        lines: list[str] = []
        commits: dict[str, str] = {}
        cursor = start
        for sha, origin_path, origin_range in raw:
            if sha is None or origin_path is None or origin_range is None:
                lines.append(f"L{cursor}: ? | ? | ")
                cursor += 1
                continue
            segment_start, segment_end = origin_range
            span = segment_end - segment_start + 1
            if sha not in commits:
                commits[sha] = self._repository.meta(sha).message.splitlines()[0]
            for _ in range(span):
                code = source[cursor - 1] if 0 < cursor <= len(source) else ""
                lines.append(f"L{cursor}: {sha} | {origin_path} | {code}")
                cursor += 1

        return {"blame": "\n".join(lines), "commits": commits}

    def pickaxe(
        self,
        needle: str,
        path: str,
        max: int = 20,
    ) -> list[dict[str, Any]]:
        """Find commits that added or removed occurrences of ``needle`` in ``path``.

        Walks the file history reachable from the correction commit (exclusive)
        with rename tracking and emits at most ``max`` commits, newest first,
        where the occurrence count of ``needle`` changed between the parent and
        the commit.
        """
        self._consume()
        if not needle:
            raise ValueError("pickaxe needle must not be empty.")
        if not path:
            raise ValueError("pickaxe path must not be empty.")

        walker = VersionControlPathWalker(
            self._repository.handle,
            self._after,
            path,
            depth=max * 5 + 5,
        )
        result: list[dict[str, Any]] = []
        for entry in walker.entries():
            if entry.commit == self._after:
                continue
            delta = self._pickaxe_delta(entry, needle)
            if delta is None or delta == 0:
                continue
            result.append(self._pickaxe_entry(entry.commit, delta))
            if len(result) >= max:
                break
        return result

    def trace(
        self,
        pattern: str,
        path: str,
        max: int = 10,
    ) -> list[dict[str, Any]]:
        """Track commits that modified any entity whose URN matches ``pattern``.

        ``pattern`` is a regular expression applied to tree-sitter entity URNs
        (e.g. ``entity:function=parse`` or ``entity:class=Parser:method=read``)
        via :func:`re.search`. Entries whose sources fail to parse are skipped.
        """
        self._consume()
        try:
            matcher = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"Invalid trace pattern: {error}") from error

        language = self._language(path)
        if language is None:
            raise ValueError(f"Unsupported language for path {path!r}.")

        walker = VersionControlPathWalker(
            self._repository.handle,
            self._after,
            path,
            depth=max * 5 + 5,
        )
        result: list[dict[str, Any]] = []
        for entry in walker.entries():
            if entry.commit == self._after:
                continue
            matches = self._trace_match(entry, language, matcher)
            if not matches:
                continue
            result.append(
                {
                    "sha": entry.commit,
                    "path": entry.after or entry.base,
                    "operation": entry.operation.value,
                    "entities": sorted(matches),
                }
            )
            if len(result) >= max:
                break
        return result

    def grep(
        self,
        needle: str,
        commit: str,
        path: str | None = None,
        max: int = 50,
    ) -> list[dict[str, Any]]:
        """Substring search over the repository tree at a given commit.

        Accepts any git revision expression for ``commit``. Returns up to
        ``max`` matches, each carrying ``path``, 1-based ``line``, 0-based
        ``column`` and the full line text. Restrict the search with ``path``
        (exact path, not glob).
        """
        self._consume()
        if not needle:
            raise ValueError("grep needle must not be empty.")

        commit = self._resolve(commit)
        paths = [path] if path is not None else self._repository.tree(commit)
        result: list[dict[str, Any]] = []
        for current in paths:
            try:
                data = self._repository.blob(commit, current)
            except (FileNotFoundError, ValueError):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(text.splitlines(), start=1):
                column = line.find(needle)
                if column < 0:
                    continue
                result.append(
                    {
                        "path": current,
                        "line": index,
                        "column": column,
                        "text": line,
                    }
                )
                if len(result) >= max:
                    return result
        return result

    def _pickaxe_delta(self, entry: Any, needle: str) -> int | None:
        base_data = self._safe_blob(entry.commit, entry.after or entry.base or "")
        parents = self._repository.parents(entry.commit)
        parent_data = b""
        if parents and entry.base is not None:
            parent_data = self._safe_blob(parents[0], entry.base)
        if base_data is None and parent_data is None:
            return None
        after = 0 if base_data is None else base_data.count(needle.encode("utf-8"))
        before = 0 if parent_data is None else parent_data.count(needle.encode("utf-8"))
        return after - before

    def _pickaxe_entry(self, sha: str, delta: int) -> dict[str, Any]:
        meta = self._repository.meta(sha)
        return {
            "sha": sha,
            "message": meta.message,
            "timestamp": self._timestamp(meta.time),
            "author": meta.author,
            "delta": delta,
        }

    def _trace_match(
        self,
        entry: Any,
        language: str,
        matcher: re.Pattern[str],
    ) -> set[str]:
        parents = self._repository.parents(entry.commit)
        base_blob = None
        if parents and entry.base is not None:
            base_blob = self._safe_blob(parents[0], entry.base)
        after_blob = None
        if entry.after is not None:
            after_blob = self._safe_blob(entry.commit, entry.after)

        operation = FileOperation(
            base=entry.base,
            after=entry.after,
            operation=entry.operation,
        )
        try:
            values = EntityPath(
                operation,
                base_blob,
                after_blob,
                language=language,
            ).resolve()
        except Exception:
            return set()

        matches: set[str] = set()
        for _, base_urn, after_urn in values:
            for urn in (base_urn, after_urn):
                if urn is None:
                    continue
                if matcher.search(urn):
                    matches.add(urn)
        return matches

    def _safe_blob(self, commit: str, path: str) -> bytes | None:
        if not path:
            return None
        try:
            return self._repository.blob(commit, path)
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def _timestamp(value: object) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    def _language(self, path: str) -> str | None:
        if path.endswith(".py"):
            return "python"
        if path.endswith(".ts"):
            return "typescript"
        if path.endswith(".tsx"):
            return "tsx"
        if path.endswith(".js") or path.endswith(".jsx"):
            return "javascript"
        if path.endswith(".java"):
            return "java"
        return None

    def _consume(self) -> None:
        if self._reads >= self._budget:
            raise ValueError("Tool read budget exhausted.")
        self._reads += 1


class RepositoryToolFactory:
    def __init__(self, repository: VersionControl, budget: int = 15) -> None:
        self._repository = repository
        self._budget = budget

    def create(self, base: str, after: str) -> RepositoryTool:
        return RepositoryTool(
            repository=self._repository,
            after=after,
            budget=self._budget,
        )
