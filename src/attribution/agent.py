from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, cast

from langchain_core.tools import BaseTool, tool
from pathspec import PathSpec
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
    """Tool-oriented interface for temporal knowledge graph search."""

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
        self._scopes: dict[str, tuple[str, ...]] = {}
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
        """List the top-ranked commit candidates.

        Use this first to inspect the search space.
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
        """Traverse to structurally related commit candidates.

        Use this to find commits that changed the same files or entities.
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
                item = result.setdefault(payload["sha"], self._neighbor(node))

        values = list(result.values())
        source = self._paths(source_targets)
        for item in values:
            item["overlap"] = self._shared(
                source,
                self._paths(self._targets(self.store.resolve(cast(str, item["sha"])))),
            )
        values.sort(
            key=lambda value: (
                -self._score(
                    self._category(self.store.payload(cast(str, value["sha"])))
                ),
                -self._precision(cast(list[str], value["overlap"])),
                -self.store.payload(value["sha"])["timestamp"].timestamp(),
                value["sha"],
            )
        )
        return values

    def meta(self, sha: str) -> dict[str, Any]:
        """Query one commit.

        Use this to inspect commit metadata.
        """
        key = self.store.resolve(sha)
        kind = self.store.kind(key)
        return {"kind": kind} | self._meta(key, kind)

    def content(self, sha: str, path: str | None = None) -> dict[str, Any]:
        """Read one commit patch for causal verification.

        Use this to check whether a candidate commit introduced the code that
        the fix later changes.
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
                "entities": self._change(key),
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
            "entities": self._change(key),
        }

    def _neighbor(self, key: str) -> dict[str, Any]:
        payload = self.store.payload(key)
        return {
            "sha": payload["sha"],
            "type": self._category(payload),
            "files": self._files(payload),
        }

    def _category(self, payload: dict[str, Any]) -> str:
        current = payload.get("type")
        if isinstance(current, str):
            return current.replace("_", "-")
        raise ValueError("Commit payload requires type.")

    def _files(self, payload: dict[str, Any]) -> list[str]:
        return list(cast(tuple[str, ...] | list[str], payload.get("files", ())))

    def _change(self, key: str) -> list[str]:
        grouped: dict[str, set[str]] = {}
        for edge in ("ADD", "EDIT", "DELETE"):
            for target in self.store.targets(key, edge):
                if target.startswith("file:"):
                    continue
                value = self.store.payload(target)
                path = str(value["path"])
                name = self._entity(value)
                if name is not None:
                    grouped.setdefault(path, set()).add(name)
        return self._format(grouped)[:10]

    @staticmethod
    def _entity(payload: dict[str, Any]) -> str | None:
        urn = str(payload.get("urn", "")).removeprefix("entity:")
        if not urn:
            return None
        if urn == "program":
            return "program"
        parts = urn.split(":")
        return ".".join(part.split("=", 1)[-1] for part in parts)

    def _format(self, grouped: dict[str, set[str]]) -> list[str]:
        result: list[str] = []
        for path in sorted(grouped):
            names = self._names(grouped[path])
            result.append(f"{path}: {', '.join(names)}")
        return result

    def _names(self, values: set[str]) -> list[str]:
        return sorted(
            values,
            key=lambda value: (
                value != "program",
                value.count("."),
                value,
            ),
        )

    def _overlap(self, key: str) -> list[str]:
        if self.store.after is None:
            return []
        correction = self.store.resolve(self.store.after)
        return self._shared(
            self._paths(self._targets(correction)),
            self._paths(self._targets(key)),
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

    def _shared(
        self,
        source: set[tuple[str, ...]],
        target: set[tuple[str, ...]],
    ) -> list[str]:
        grouped: dict[str, set[str]] = {}
        file_only: list[str] = []
        for left in source:
            for right in target:
                current = self._common(left, right)
                if current is None:
                    continue
                if len(current) == 1:
                    file_only.append(current[0].removeprefix("file="))
                    continue
                path = current[0].removeprefix("file=")
                name = ".".join(part.split("=", 1)[-1] for part in current[1:])
                grouped.setdefault(path, set()).add(name)
        items = self._format(grouped)
        files = sorted(path for path in set(file_only) if path not in grouped)
        return items + files

    def _precision(self, values: list[str]) -> int:
        return 1 if any(":" in value for value in values) else 0

    def _paths(
        self,
        nodes: list[str],
    ) -> set[tuple[str, ...]]:
        result: set[tuple[str, ...]] = set()

        for node in nodes:
            current = self._scopes.get(node)
            if current is None:
                current = self._path(node)
                self._scopes[node] = current
            result.add(current)

        return result

    def _path(self, node: str) -> tuple[str, ...]:
        if node.startswith("file:"):
            return (f"file={node.removeprefix('file:')}",)

        payload = self.store.payload(node)
        path = str(payload["path"])
        urn = str(payload["urn"]).removeprefix("entity:")
        if urn == "program":
            return (f"file={path}", "program")
        return (f"file={path}", *tuple(urn.split(":")))

    def _common(
        self,
        left: tuple[str, ...],
        right: tuple[str, ...],
    ) -> tuple[str, ...] | None:
        if not left or not right or left[0] != right[0]:
            return None
        result: list[str] = [left[0]]
        for a, b in zip(left[1:], right[1:]):
            if a != b:
                break
            result.append(a)
        return tuple(result)

    @staticmethod
    def _timestamp(value: object) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)


class KnowledgeGraphToolFactory:
    def __init__(
        self,
        repository: VersionControl,
        source: KnowledgeGraphBuilder | Path,
        filter: Callable[[str, str, str | None], str] | None = None,
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
    """Repository-native tool surface for commit and code-history search."""

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
        """Show one commit and its patch.

        Use this to inspect what a commit changed. Optionally restrict the diff
        to one path.
        """
        commit = self._resolve(commit)
        meta = self._repository.meta(commit)
        parents = self._repository.parents(commit)
        patch = ""
        if parents:
            diff = self._repository.diff(parents[0], commit)
            patch = self._compress_patch(self._renderer.render(diff, path))
        return {
            "sha": commit,
            "message": meta.message,
            "timestamp": self._timestamp(meta.time),
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
        """Blame a line range in one file at a commit.

        Use this to see which commit last changed specific lines.
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
            short = sha[:12]
            for _ in range(span):
                code = source[cursor - 1] if 0 < cursor <= len(source) else ""
                lines.append(f"L{cursor}: {short} | {origin_path} | {code}")
                cursor += 1

        return {"blame": "\n".join(self._compress_blame(lines)), "commits": commits}

    def pickaxe(
        self,
        needle: str,
        path: str | None = None,
        max: int = 20,
    ) -> list[dict[str, Any]]:
        """Find commits that added or removed a string.

        Use this to trace when text first appeared, disappeared, or changed in
        one file or across the repository.
        """
        self._consume()
        if not needle:
            raise ValueError("pickaxe needle must not be empty.")

        if path is None:
            return self._pickaxe_repository(needle, max)

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
        """Trace commits that changed matching entities in one file.

        Use this to follow the history of functions, methods, or classes when
        line-based blame is not enough.
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
        """Search repository files at a commit with a regex.

        Use this to find definitions, call sites, and references in one
        snapshot. Optionally restrict the search with a path pattern.
        """
        self._consume()
        if not needle:
            raise ValueError("grep needle must not be empty.")

        try:
            pattern = re.compile(needle)
        except re.error as error:
            raise ValueError(f"Invalid grep regex: {error}") from error

        commit = self._resolve(commit)
        paths = self._repository.tree(commit)
        if path is not None:
            patterns = [path]
            if not any(token in path for token in "*?[]!"):
                prefix = path.rstrip("/")
                if prefix:
                    patterns.append(f"{prefix}/**")
            spec = PathSpec.from_lines("gitignore", patterns)
            paths = [current for current in paths if spec.match_file(current)]
        result: list[dict[str, Any]] = []
        total = 0
        for current in paths:
            try:
                data = self._repository.blob(commit, current)
            except (FileNotFoundError, ValueError):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            matches: list[str] = []
            for index, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line) is None:
                    continue
                matches.append(f"L{index}: {line}")
                if len(matches) >= max - total:
                    break
            if not matches:
                continue
            result.append({"path": current, "matches": "\n".join(matches)})
            total += len(matches)
            if total >= max:
                break
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
            "delta": delta,
        }

    def _pickaxe_repository(self, needle: str, max: int) -> list[dict[str, Any]]:
        repo = self._repository.handle
        start = repo.revparse_single(self._after)
        encoded = needle.encode("utf-8")
        result: list[dict[str, Any]] = []
        depth = max * 5 + 5

        for i, commit in enumerate(repo.walk(start.id, pygit2.GIT_SORT_TIME)):
            if i >= depth:
                break
            sha = str(commit.id)
            if sha == self._after:
                continue
            if len(commit.parents) != 1:
                continue

            parent = commit.parents[0]
            diff = repo.diff(parent.tree, commit.tree, context_lines=0)
            delta = 0
            for patch in diff:
                after = 0
                before = 0
                if patch.delta.new_file.path is not None:
                    blob = self._safe_blob(sha, patch.delta.new_file.path)
                    if blob is not None:
                        after = blob.count(encoded)
                if patch.delta.old_file.path is not None:
                    blob = self._safe_blob(str(parent.id), patch.delta.old_file.path)
                    if blob is not None:
                        before = blob.count(encoded)
                delta += after - before

            if delta == 0:
                continue
            result.append(self._pickaxe_entry(sha, delta))
            if len(result) >= max:
                break
        return result

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

    @staticmethod
    def _compress_patch(patch: str, max_lines: int = 80) -> str:
        lines = patch.split("\n")
        kept: list[str] = []
        for line in lines:
            if (
                line.startswith("diff --git")
                or line.startswith("---")
                or line.startswith("+++")
                or line.startswith("@@")
                or line.startswith("+")
                or line.startswith("-")
            ):
                kept.append(line)
        if len(kept) > max_lines:
            half = max_lines // 2
            omitted = len(kept) - max_lines
            kept = (
                kept[:half] + [f"... [{omitted} diff lines omitted] ..."] + kept[-half:]
            )
        return "\n".join(kept)

    @staticmethod
    def _compress_blame(lines: list[str], max_lines: int = 150) -> list[str]:
        if len(lines) <= max_lines:
            return lines
        half = max_lines // 2
        omitted = len(lines) - max_lines
        return lines[:half] + [f"... [{omitted} lines omitted] ..."] + lines[-half:]

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
