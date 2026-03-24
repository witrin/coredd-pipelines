from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, cast

import networkx as nx
from networkx.readwrite import json_graph
import pygit2

from benchmark.attribution.diff.entity import EntityPath
from benchmark.attribution.domain import FileOperation, OperationType
from benchmark.attribution.history import VersionControl, VersionHistoryPathWalker


@dataclass(frozen=True)
class KnowledgeCommit:
    sha: str
    kind: str
    score: float
    timestamp: datetime
    message: str
    parents: tuple[str, ...]
    files: frozenset[str]
    category: str | None


@dataclass(frozen=True)
class KnowledgeFile:
    sha: str
    parent: str | None
    base: str | None
    after: str | None
    path: str
    language: str | None
    operation: OperationType


@dataclass(frozen=True)
class KnowledgeEntity:
    sha: str
    path: str
    urn: str
    kind: str
    name: str
    operation: OperationType
    source: str


@dataclass(frozen=True)
class KnowledgeState:
    commits: list[KnowledgeCommit]
    files: list[KnowledgeFile]
    entities: list[KnowledgeEntity]
    after: str


@dataclass(frozen=True)
class KnowledgeAuditFailure:
    kind: str
    type: str
    message: str
    sha: str
    parent: str | None
    path: str | None
    language: str | None


@dataclass(frozen=True)
class KnowledgeAuditCase:
    base: str
    after: str
    commits: int
    files: int
    entities: int
    failures: list[KnowledgeAuditFailure]


class KnowledgeGraphStore:
    """In-memory temporal knowledge graph."""

    def __init__(self) -> None:
        self._graph = nx.MultiDiGraph()
        self._base = ""
        self._after = ""

    @property
    def graph(self) -> nx.MultiDiGraph:
        return self._graph

    def dump(self) -> dict[str, object]:
        return {
            "base": self._base,
            "after": self._after,
            "graph": cast(
                dict[str, object],
                json_graph.node_link_data(self._graph),
            ),
        }

    @classmethod
    def load(cls, data: dict[str, object]) -> KnowledgeGraphStore:
        store = cls()
        store.base = str(data["base"])
        store.after = str(data["after"])
        store._graph = cast(
            nx.MultiDiGraph,
            json_graph.node_link_graph(
                cast(dict[str, object], cls._value(data["graph"])),
                multigraph=True,
                directed=True,
            ),
        )
        return store

    @classmethod
    def _value(cls, value: object) -> object:
        if isinstance(value, list):
            return [cls._value(item) for item in value]

        if not isinstance(value, dict):
            return value

        result: dict[str, object] = {}

        for key, item in value.items():
            if key == "timestamp" and isinstance(item, str):
                result[key] = datetime.fromisoformat(item)
                continue
            result[str(key)] = cls._value(item)

        return result

    @property
    def base(self) -> str:
        return self._base

    @base.setter
    def base(self, value: str) -> None:
        self._base = value

    @property
    def after(self) -> str:
        return self._after

    @after.setter
    def after(self, value: str) -> None:
        self._after = value

    def add(self, kind: str, payload: dict[str, Any]) -> str:
        key = self._key(kind, payload)
        self._graph.add_node(key, kind=kind, payload=dict(payload))
        return key

    def link(self, edge: str, source: str, target: str) -> None:
        if not self._graph.has_node(source):
            raise KeyError(f"Unknown source node {source}")
        if not self._graph.has_node(target):
            raise KeyError(f"Unknown target node {target}")
        for _, current, data in self._graph.out_edges(source, data=True):
            if data.get("edge") == edge and current == target:
                return
        self._graph.add_edge(source, target, edge=edge)

    def kind(self, node: str) -> str:
        key = self.resolve(node)
        return str(self._graph.nodes[key]["kind"])

    def payload(self, node: str) -> dict[str, Any]:
        key = self.resolve(node)
        return dict(self._graph.nodes[key]["payload"])

    def nodes(self, kind: str) -> list[str]:
        return [
            key
            for key, data in self._graph.nodes(data=True)
            if data.get("kind") == kind
        ]

    def targets(self, node: str, edge: str) -> list[str]:
        key = self.resolve(node)
        result: list[str] = []
        seen: set[str] = set()
        for _, target, data in self._graph.out_edges(key, data=True):
            if data.get("edge") != edge or target in seen:
                continue
            seen.add(target)
            result.append(target)
        return result

    def sources(self, node: str, edge: str) -> list[str]:
        key = self.resolve(node)
        result: list[str] = []
        seen: set[str] = set()
        for source, _, data in self._graph.in_edges(key, data=True):
            if data.get("edge") != edge or source in seen:
                continue
            seen.add(source)
            result.append(source)
        return result

    def chains(self) -> list[list[str]]:
        commits = self.nodes("commit")
        next = {
            self.payload(node)["sha"]: self._commits(
                [
                    self.payload(target)["sha"]
                    for target in self.targets(node, "PRECEDES")
                ]
            )
            for node in commits
        }
        previous = {
            self.payload(node)["sha"]: self._commits(
                [
                    self.payload(source)["sha"]
                    for source in self.sources(node, "PRECEDES")
                ]
            )
            for node in commits
        }
        chains: list[list[str]] = []
        seen: set[str] = set()
        starts = self._commits([sha for sha in next if not previous[sha]])

        for start in starts + self._commits([sha for sha in next if sha not in seen]):
            if start in seen:
                continue
            chain = [start]
            seen.add(start)
            current = start

            while next[current]:
                current = next[current][0]

                if current in seen:
                    break

                chain.append(current)
                seen.add(current)

            chains.append(chain)

        return chains

    def resolve(self, node: str) -> str:
        if self._graph.has_node(node):
            return node
        commit = f"commit:{node}"
        if self._graph.has_node(commit):
            return commit
        file = f"file:{node}"
        if self._graph.has_node(file):
            return file
        raise KeyError(f"Unknown node {node}")

    def _key(self, kind: str, payload: dict[str, Any]) -> str:
        if kind == "commit":
            sha = payload.get("sha")
            if not isinstance(sha, str) or not sha:
                raise ValueError("Commit payload requires non-empty 'sha'.")
            return f"commit:{sha}"
        if kind == "file":
            path = payload.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("File payload requires non-empty 'path'.")
            return f"file:{path}"
        if kind in {"class", "function", "method", "program"}:
            path = payload.get("path")
            urn = payload.get("urn")
            if not isinstance(path, str) or not path:
                raise ValueError(f"{kind} payload requires non-empty 'path'.")
            if not isinstance(urn, str) or not urn.startswith("entity:"):
                raise ValueError(f"{kind} payload requires canonical 'urn'.")
            return f"entity:{path}:{urn.removeprefix('entity:')}"
        raise ValueError(f"Unsupported node kind {kind}")

    def _commits(self, values: list[str]) -> list[str]:
        return sorted(
            values,
            key=lambda value: (
                self.payload(value)["timestamp"],
                value,
            ),
        )


class KnowledgeCommitGit:
    def __init__(
        self,
        repository: VersionControl,
        cap: int,
        depth: int = 100,
    ) -> None:
        self._repository = repository
        self._cap = cap
        self._depth = depth
        self._meta: dict[str, dict[str, Any]] = {}

    def build(
        self,
        base: str,
        after: str,
    ) -> list[KnowledgeCommit]:
        seeds = self._seed(base, after)
        blame = {sha for sha in seeds if sha != after}

        trace: dict[str, set[str]] = {}
        for sha, paths in seeds.items():
            for name in sorted(f for f in paths if f):
                try:
                    walker = VersionHistoryPathWalker(
                        self._repository._repository, sha, name, self._depth,
                    )
                except (TypeError, FileNotFoundError):
                    continue
                for entry in walker.entries():
                    if entry.type in ("renamed", "copied"):
                        trace.setdefault(entry.commit, set()).update(entry.path)
                    else:
                        trace.setdefault(entry.commit, set()).add(entry.path)
        trace = {sha: files for sha, files in trace.items() if files}

        files: dict[str, set[str]] = {sha: set(values) for sha, values in trace.items()}
        for sha, values in seeds.items():
            files.setdefault(sha, set()).update(values)

        predecessors, order = self._parents(set(files))
        commits = self._commits(after, order, blame, files, predecessors)
        commits = self._limit(after, commits)
        return commits

    def _seed(
        self,
        base: str,
        after: str,
    ) -> dict[str, set[str]]:
        diff = self._repository.diff(base, after)
        seeds: dict[str, set[str]] = {}
        files: set[str] = set()

        for patch in diff:
            path = patch.delta.old_file.path
            if not path:
                continue

            changes = self._changes(patch)
            if changes:
                files.add(path)
                for sha in self._blame(path, base, changes):
                    seeds.setdefault(sha, set()).add(path)
                continue

            contexts = self._contexts(patch)
            if not contexts:
                continue
            try:
                values = self._blame(path, base, contexts)
            except KeyError:
                continue
            files.add(path)
            for sha in values:
                seeds.setdefault(sha, set()).add(path)

        seeds[after] = files
        return seeds

    def _changes(self, patch: pygit2.Patch) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []

        for hunk in patch.hunks:
            start: int | None = None
            previous = 0
            for line in hunk.lines:
                if line.origin == "-":
                    if start is None:
                        start = line.old_lineno
                    previous = line.old_lineno
                elif start is not None:
                    result.append((start, previous))
                    start = None

            if start is not None:
                result.append((start, previous))

        return self._merge(result)

    def _contexts(self, patch: pygit2.Patch) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []

        for hunk in patch.hunks:
            if hunk.old_lines != 0:
                continue

            before = (max(1, hunk.old_start - 2), hunk.old_start - 1)
            after = (hunk.old_start, hunk.old_start + 1)

            if before[0] <= before[1]:
                result.append(before)
            if after[0] <= after[1]:
                result.append(after)

        return self._merge(result)

    def _merge(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not ranges:
            return []

        ordered = sorted(ranges)
        result = [ordered[0]]

        for start, stop in ordered[1:]:
            left, right = result[-1]
            if start > right + 1:
                result.append((start, stop))
                continue
            result[-1] = (left, max(right, stop))

        return result

    def _blame(
        self,
        path: str,
        commit: str,
        ranges: list[tuple[int, int]],
    ) -> set[str]:
        values: set[str] = set()
        for sha, _ in self._repository.blame(path, commit, ranges):
            if sha is None:
                continue
            values.add(sha)
        return values

    def _parents(
        self,
        commits: set[str],
    ) -> tuple[dict[str, tuple[str, ...]], list[str]]:
        repository = self._repository._repository
        identifiers = {sha: repository.revparse_single(sha).id for sha in commits}

        edges: set[tuple[str, str]] = set()
        members = list(commits)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if repository.descendant_of(identifiers[a], identifiers[b]):
                    edges.add((b, a))
                elif repository.descendant_of(identifiers[b], identifiers[a]):
                    edges.add((a, b))

        dag = nx.DiGraph()
        dag.add_nodes_from(commits)
        dag.add_edges_from(edges)
        reduced = nx.transitive_reduction(dag)

        predecessors: dict[str, tuple[str, ...]] = {}
        for sha in commits:
            predecessors[sha] = tuple(sorted(reduced.predecessors(sha)))

        order = list(nx.topological_sort(reduced))

        return predecessors, order

    def _commits(
        self,
        after: str,
        order: list[str],
        blame: set[str],
        trace: dict[str, set[str]],
        predecessors: dict[str, tuple[str, ...]],
    ) -> list[KnowledgeCommit]:
        scores = self._score(after, order, blame)
        result: list[KnowledgeCommit] = []
        for sha in order:
            meta = self._commit(sha)
            category: str | None = (
                "correction" if sha == after else "blame" if sha in blame else None
            )
            result.append(
                KnowledgeCommit(
                    sha=sha,
                    kind=category or "candidate",
                    score=scores[sha],
                    timestamp=cast(datetime, meta["timestamp"]),
                    message=cast(str, meta["message"]),
                    parents=predecessors.get(sha, ()),
                    files=frozenset(trace.get(sha, set())),
                    category=category,
                )
            )
        return result

    def _commit(self, sha: str) -> dict[str, Any]:
        if sha not in self._meta:
            meta = self._repository.meta(sha)
            self._meta[sha] = {
                "message": meta.message,
                "timestamp": meta.time,
            }
        return self._meta[sha]

    def _score(
        self,
        after: str,
        order: list[str],
        blame: set[str],
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        reachable = False
        for sha in reversed(order):
            if sha == after:
                result[sha] = 0.0
            elif sha in blame:
                result[sha] = 1.0
                reachable = True
            elif reachable:
                result[sha] = 0.6
            else:
                result[sha] = 0.3
        return result

    def _limit(
        self,
        after: str,
        commits: list[KnowledgeCommit],
    ) -> list[KnowledgeCommit]:
        if len(commits) <= self._cap + 1:
            return commits

        direct = [c for c in commits if c.sha != after and c.category == "blame"]
        others = [c for c in commits if c.sha != after and c.category != "blame"]
        others.sort(key=lambda c: (-c.score, -c.timestamp.timestamp(), c.sha))
        remaining = max(self._cap - len(direct), 0)
        keep = {after, *(c.sha for c in direct), *(c.sha for c in others[:remaining])}
        return [c for c in commits if c.sha in keep]


class KnowledgeFileGit:
    def __init__(self, repository: VersionControl) -> None:
        self._repository = repository

    def build(
        self,
        commits: list[KnowledgeCommit],
    ) -> list[KnowledgeFile]:
        result: list[KnowledgeFile] = []
        seen: set[tuple[str, str]] = set()

        for commit in commits:
            parent = self._parent(commit.sha)
            paths = commit.files

            if parent is None:
                for path in sorted(paths):
                    if (commit.sha, path) in seen:
                        continue
                    seen.add((commit.sha, path))
                    result.append(
                        KnowledgeFile(
                            sha=commit.sha,
                            parent=None,
                            base=path,
                            after=path,
                            path=path,
                            language=self._language(path),
                            operation=OperationType.ADD,
                        )
                    )
                continue

            diff = self._repository.diff(parent, commit.sha)
            matched: set[str] = set()

            for patch in diff:
                old = patch.delta.old_file.path
                new = patch.delta.new_file.path
                if old not in paths and new not in paths:
                    continue

                path = cast(str, new if new is not None else old)
                matched.add(path)
                if (commit.sha, path) in seen:
                    continue
                seen.add((commit.sha, path))
                result.append(
                    KnowledgeFile(
                        sha=commit.sha,
                        parent=parent,
                        base=old,
                        after=new,
                        path=path,
                        language=self._language(path),
                        operation=self._operation(old, new),
                    )
                )

            for path in sorted(paths - matched):
                if (commit.sha, path) in seen:
                    continue
                seen.add((commit.sha, path))
                result.append(
                    KnowledgeFile(
                        sha=commit.sha,
                        parent=parent,
                        base=path,
                        after=path,
                        path=path,
                        language=self._language(path),
                        operation=OperationType.EDIT,
                    )
                )

        return result

    def _parent(self, sha: str) -> str | None:
        parents = self._repository.parents(sha)
        return parents[0] if parents else None

    def _operation(
        self,
        base: str | None,
        after: str | None,
    ) -> OperationType:
        if base is None:
            return OperationType.ADD
        if after is None:
            return OperationType.DELETE
        if base != after:
            return OperationType.MOVE
        return OperationType.EDIT

    def _language(self, path: str | None) -> str | None:
        if not path:
            return None
        if path.endswith(".py"):
            return "python"
        if path.endswith(".ts"):
            return "typescript"
        if path.endswith(".tsx"):
            return "tsx"
        if path.endswith(".js") or path.endswith(".jsx"):
            return "javascript"
        return None


class KnowledgeEntityGumtree:
    def __init__(self, repository: VersionControl) -> None:
        self._repository = repository

    def build(self, files: list[KnowledgeFile]) -> list[KnowledgeEntity]:
        result: list[KnowledgeEntity] = []
        seen: set[tuple[str, str, str, str]] = set()

        for file in files:
            for value in self._build(file):
                key = (value.sha, value.path, value.urn, value.operation)
                if key in seen:
                    continue
                seen.add(key)
                result.append(value)

        result.sort(key=lambda value: (value.sha, value.kind, value.path, value.urn))
        return result

    def _build(self, file: KnowledgeFile) -> list[KnowledgeEntity]:
        if file.parent is None or file.language is None:
            return []

        result: list[KnowledgeEntity] = []
        resolver = EntityPath(
            FileOperation(
                base=file.base,
                after=file.after,
                operation=file.operation,
            ),
            self._blob(file.parent, file.base),
            self._blob(file.sha, file.after),
            language=file.language,
        )

        for edge, base_urn, after_urn in resolver.resolve():
            value = self._entity(file, edge, base_urn, after_urn)
            if value is None:
                continue
            result.append(value)

        if result or file.operation == OperationType.MOVE:
            return result

        result.append(
            KnowledgeEntity(
                sha=file.sha,
                path=file.path,
                urn="entity:program",
                kind="program",
                name=self._stem(file.path),
                operation=file.operation,
                source="gumtree",
            )
        )
        return result

    def _blob(self, commit: str, path: str | None) -> bytes | None:
        if not path:
            return None
        try:
            return self._repository.blob(commit, path)
        except FileNotFoundError:
            return None

    def _entity(
        self,
        file: KnowledgeFile,
        edge: str,
        base_urn: str | None,
        after_urn: str | None,
    ) -> KnowledgeEntity | None:
        if edge == "ADD":
            urn = cast(str, after_urn)
        elif edge in {"DELETE", "EDIT"}:
            urn = cast(str, base_urn)
        elif edge == "MOVE":
            return None
        else:
            raise ValueError(f"Unsupported entity edge {edge}")

        kind, name = self._urn(urn, file.path)
        return KnowledgeEntity(
            sha=file.sha,
            path=file.path,
            urn=urn,
            kind=kind,
            name=name,
            operation=OperationType[edge],
            source="gumtree",
        )

    def _urn(self, urn: str, path: str) -> tuple[str, str]:
        if urn == "entity:program":
            return ("program", self._stem(path))

        current = urn.removeprefix("entity:").rsplit(":", 1)[-1]
        kind, _, name = current.partition("=")
        if kind not in {"class", "function", "method"} or not name:
            raise ValueError(f"Unsupported entity urn {urn}")
        return (kind, name)

    def _stem(self, path: str) -> str:
        name = path.rsplit("/", 1)[-1]
        if "." not in name:
            return name
        return name.rsplit(".", 1)[0]


class KnowledgeEntityGit:
    def __init__(self, repository: VersionControl) -> None:
        self._repository = repository

    def build(self, files: list[KnowledgeFile]) -> list[KnowledgeEntity]:
        result: list[KnowledgeEntity] = []
        seen: set[tuple[str, str, str, OperationType]] = set()

        for file in files:
            if file.language is None:
                continue

            if file.parent is None or file.operation == OperationType.ADD:
                self._collect(self._program(file, OperationType.ADD), seen, result)
                continue

            if file.operation == OperationType.DELETE:
                self._collect(self._program(file, OperationType.DELETE), seen, result)
                continue

            patch = self._patch(file)
            if patch is None:
                continue

            for hunk in patch.hunks:
                value = self._entity(file, hunk.header)
                if value is None:
                    value = self._program(file, OperationType.EDIT)
                self._collect(value, seen, result)

        result.sort(key=lambda value: (value.sha, value.kind, value.path, value.urn))
        return result

    def _collect(
        self,
        value: KnowledgeEntity,
        seen: set[tuple[str, str, str, OperationType]],
        result: list[KnowledgeEntity],
    ) -> None:
        key = (value.sha, value.path, value.urn, value.operation)
        if key in seen:
            return
        seen.add(key)
        result.append(value)

    def _program(
        self, file: KnowledgeFile, operation: OperationType
    ) -> KnowledgeEntity:
        name = file.path.rsplit("/", 1)[-1]
        if "." in name:
            name = name.rsplit(".", 1)[0]
        return KnowledgeEntity(
            sha=file.sha,
            path=file.path,
            urn="entity:program",
            kind="program",
            name=name,
            operation=operation,
            source="git",
        )

    def _patch(self, file: KnowledgeFile) -> pygit2.Patch | None:
        diff = self._repository.diff(cast(str, file.parent), file.sha)

        for patch in diff:
            old = patch.delta.old_file.path
            new = patch.delta.new_file.path
            if old == file.base and new == file.after:
                return patch
            if file.path == old or file.path == new:
                return patch

        return None

    def _entity(self, file: KnowledgeFile, header: str) -> KnowledgeEntity | None:
        value = self._header(file.language, header)
        if value is None:
            return None

        kind, name = value
        return KnowledgeEntity(
            sha=file.sha,
            path=file.path,
            urn=f"entity:{kind}={name}",
            kind=kind,
            name=name,
            operation=OperationType.EDIT,
            source="git",
        )

    def _header(
        self,
        language: str,
        header: str,
    ) -> tuple[str, str] | None:
        _, _, tail = header.partition("@@")
        _, _, tail = tail.partition("@@")
        tail = tail.strip()

        if not tail:
            return None

        if language == "python":
            return self._python(tail)

        if language in {"javascript", "typescript", "tsx"}:
            return self._script(tail)

        return None

    def _python(self, value: str) -> tuple[str, str] | None:
        if value.startswith("async def "):
            name = value.removeprefix("async def ").split("(", 1)[0].strip()
            return ("function", name) if name else None

        if value.startswith("def "):
            name = value.removeprefix("def ").split("(", 1)[0].strip()
            return ("function", name) if name else None

        if value.startswith("class "):
            name = value.removeprefix("class ").split("(", 1)[0]
            name = name.split(":", 1)[0].strip()
            return ("class", name) if name else None

        return None

    def _script(self, value: str) -> tuple[str, str] | None:
        class_match = re.search(
            r"(?:^|\s)class\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            value,
        )
        if class_match is not None:
            return ("class", class_match.group(1))

        function_match = re.search(
            r"(?:^|\s)(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            value,
        )
        if function_match is not None:
            return ("function", function_match.group(1))

        return None


class KnowledgeGraphMaterializer:
    def build(
        self,
        state: KnowledgeState,
        base: str,
        after: str,
    ) -> KnowledgeGraphStore:
        store = KnowledgeGraphStore()
        store.base = base
        store.after = after

        files: dict[str, list[str]] = {}
        for file in state.files:
            files.setdefault(file.sha, []).append(file.path)

        for commit in state.commits:
            store.add(
                "commit",
                {
                    "sha": commit.sha,
                    "type": commit.kind,
                    "score": commit.score,
                    "timestamp": commit.timestamp,
                    "message": commit.message,
                    "files": tuple(sorted(files.get(commit.sha, []))),
                },
            )

        seen_files: set[str] = set()
        for file in state.files:
            if file.path in seen_files:
                continue
            seen_files.add(file.path)
            store.add("file", {"path": file.path})

        seen_entities: set[tuple[str, str]] = set()
        for entity in state.entities:
            key = (entity.path, entity.urn)
            if key in seen_entities:
                continue
            seen_entities.add(key)
            store.add(
                entity.kind,
                {
                    "kind": entity.kind,
                    "path": entity.path,
                    "urn": entity.urn,
                    "name": entity.name,
                    "source": entity.source,
                },
            )

        for file in state.files:
            store.link("MODIFIES", f"commit:{file.sha}", f"file:{file.path}")
            if file.operation == OperationType.MOVE:
                store.link("MOVE", f"file:{file.base}", f"file:{file.after}")

        for entity in state.entities:
            target = f"entity:{entity.path}:{entity.urn.removeprefix('entity:')}"
            store.link(entity.operation.name, f"commit:{entity.sha}", target)
            store.link("IN", target, f"file:{entity.path}")

        for commit in state.commits:
            if commit.category == "blame":
                store.link("BLAME", f"commit:{state.after}", f"commit:{commit.sha}")

        for commit in state.commits:
            for predecessor in commit.parents:
                store.link("PRECEDES", f"commit:{predecessor}", f"commit:{commit.sha}")

        return store


class KnowledgeGraphBuilder:
    """Build a commit and file graph from one corrective change."""

    def __init__(
        self,
        repository: VersionControl,
        depth: int = 24,
        cap: int = 200,
    ) -> None:
        if depth < 0:
            raise ValueError("depth must be non-negative")
        if cap < 1:
            raise ValueError("cap must be positive")
        self._commits = KnowledgeCommitGit(repository, cap, depth)
        self._files = KnowledgeFileGit(repository)
        self._entities = KnowledgeEntityGumtree(repository)
        self._fallback = KnowledgeEntityGit(repository)
        self._materializer = KnowledgeGraphMaterializer()

    def build(self, base: str, after: str) -> KnowledgeGraphStore:
        commits = self._commits.build(base, after)
        files = self._files.build(commits)
        try:
            entities = self._entities.build(files)
        except Exception:
            entities = self._fallback.build(files)
        state = KnowledgeState(
            commits=commits,
            files=files,
            entities=entities,
            after=after,
        )
        return self._materializer.build(state, base, after)


class KnowledgeAuditBuilder:
    def __init__(
        self,
        repository: VersionControl,
        depth: int = 24,
        cap: int = 200,
    ) -> None:
        if depth < 0:
            raise ValueError("depth must be non-negative")
        if cap < 1:
            raise ValueError("cap must be positive")
        self._commits = KnowledgeCommitGit(repository, cap, depth)
        self._files = KnowledgeFileGit(repository)
        self._entities = KnowledgeEntityGumtree(repository)

    def build(self, base: str, after: str) -> KnowledgeAuditCase:
        try:
            commits = self._commits.build(base, after)
        except Exception as error:
            return KnowledgeAuditCase(
                base=base,
                after=after,
                commits=0,
                files=0,
                entities=0,
                failures=[
                    self._failure(
                        kind="commit",
                        error=error,
                        sha=after,
                    )
                ],
            )

        try:
            files = self._files.build(commits)
        except Exception as error:
            return KnowledgeAuditCase(
                base=base,
                after=after,
                commits=len(commits),
                files=0,
                entities=0,
                failures=[
                    self._failure(
                        kind="file",
                        error=error,
                        sha=after,
                    )
                ],
            )

        failures: list[KnowledgeAuditFailure] = []
        entities = 0
        count = 0

        for file in files:
            if file.parent is None or file.language is None:
                continue

            count += 1
            try:
                entities += len(self._entities.build([file]))
            except Exception as error:
                failures.append(
                    self._failure(
                        kind="entity",
                        error=error,
                        sha=file.sha,
                        parent=file.parent,
                        path=file.path,
                        language=file.language,
                    )
                )

        return KnowledgeAuditCase(
            base=base,
            after=after,
            commits=len(commits),
            files=count,
            entities=entities,
            failures=failures,
        )

    def _failure(
        self,
        *,
        kind: str,
        error: Exception,
        sha: str,
        parent: str | None = None,
        path: str | None = None,
        language: str | None = None,
    ) -> KnowledgeAuditFailure:
        return KnowledgeAuditFailure(
            kind=kind,
            type=type(error).__name__,
            message=str(error),
            sha=sha,
            parent=parent,
            path=path,
            language=language,
        )
