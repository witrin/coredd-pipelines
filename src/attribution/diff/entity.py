from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import jpype

from benchmark.attribution.domain import FileOperation


@dataclass(frozen=True)
class _Entity:
    kind: str
    name: str
    nested: bool
    node: object
    urn: str


class GumTree:
    _lock = threading.Lock()
    _started = False

    def __init__(self) -> None:
        self._jar = Path("/usr/local/share/java/gumtree.jar")
        self._ensure()

    def diff(self, base: Path, after: Path) -> object:
        Thread = jpype.JClass("java.lang.Thread")
        ClassLoader = jpype.JClass("java.lang.ClassLoader")

        current = Thread.currentThread()
        loader = current.getContextClassLoader()
        if loader is None:
            current.setContextClassLoader(ClassLoader.getSystemClassLoader())

        try:
            Run = jpype.JClass("com.github.gumtreediff.client.Run")
            Diff = jpype.JClass("com.github.gumtreediff.actions.Diff")
            GumtreeProperties = jpype.JClass(
                "com.github.gumtreediff.matchers.GumtreeProperties"
            )
            Run.initGenerators()
            Run.initMatchers()

            diff = Diff.compute(
                str(base),
                str(after),
                None,
                None,
                GumtreeProperties(),
            )
        finally:
            if loader is None:
                current.setContextClassLoader(loader)
        self._validate(diff, base, after)
        return diff

    def _ensure(self) -> None:
        with GumTree._lock:
            if GumTree._started:
                return

            jpype.startJVM(
                "-XX:+IgnoreUnrecognizedVMOptions",
                "--enable-native-access=ALL-UNNAMED",
                classpath=[str(self._jar)],
            )
            GumTree._started = True

    def _validate(self, diff: object, base: Path, after: Path) -> None:
        for side, tree, path in (
            ("src", diff.src, base),
            ("dst", diff.dst, after),
        ):
            for node in tree.getRoot().preOrder():
                typ = str(node.getType()).upper()
                if typ not in {"ERROR", "MISSING"}:
                    continue

                try:
                    label = str(node.getLabel()) or None
                except Exception:
                    label = None

                try:
                    start = int(node.getPos())
                except Exception:
                    start = None

                try:
                    end = int(node.getEndPos())
                except Exception:
                    end = None

                snippet = None
                if start is not None:
                    try:
                        text = path.read_text(errors="replace")
                        left = max(0, start - 80)
                        right = min(
                            len(text),
                            (start if end is None else end) + 80,
                        )
                        snippet = text[left:right]
                    except Exception:
                        snippet = None

                raise ValueError(
                    f"GumTree parse error side={side} type={typ} path={path} "
                    f"label={label!r} start={start} end={end} snippet={snippet!r}"
                )


class EntityPath:
    def __init__(
        self,
        file: FileOperation,
        base: bytes | None,
        after: bytes | None,
        *,
        language: str,
    ) -> None:
        self._file = file
        self._base = base
        self._after = after
        self._language = language

    def resolve(self) -> set[tuple[str | None, ...]]:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            base, after = self._materialize(root)
            diff = GumTree().diff(base, after)

        source = self._collect(diff.src.getRoot())
        target = self._collect(diff.dst.getRoot())
        mapping = diff.mappings
        classifier = diff.createAllNodeClassifier()
        source_match, target_match = self._match(source, target, mapping)
        source_move = set(classifier.getMovedSrcs())
        target_move = set(classifier.getMovedDsts())
        blocked = self._blocked(source, set(source_match)) | self._blocked(
            target, target_match
        )
        source_changed = self._content_changed(
            source,
            set(classifier.getUpdatedSrcs()),
            source_match,
        ) | self._content_changed(
            source,
            set(classifier.getDeletedSrcs()),
            source_match,
            blocked,
        )
        target_changed = self._content_changed(
            target,
            set(classifier.getUpdatedDsts()),
            target_match,
        ) | self._content_changed(
            target,
            set(classifier.getInsertedDsts()),
            target_match,
            blocked,
        )

        values: set[tuple[str | None, ...]] = set()
        moved: set[object] = set()

        for current in source.values():
            node = source_match.get(current.node)
            if node is None:
                continue
            other = target[node]

            if current.node not in source_move and other.node not in target_move:
                continue
            if current.kind not in {"function", "method"}:
                continue
            if other.kind not in {"function", "method"}:
                continue
            if current.urn == other.urn:
                continue

            values.add(("MOVE", current.urn, other.urn))
            moved.add(current.node)
            moved.add(other.node)

        for current in source.values():
            node = source_match.get(current.node)
            if current.node in moved:
                continue
            if node is None:
                if current.kind == "program":
                    continue
                values.add(("DELETE", current.urn, None))
                continue

            other = target[node]
            if other.node in moved:
                continue
            if current.urn != other.urn:
                continue
            if current.node not in source_changed and other.node not in target_changed:
                continue
            values.add(("EDIT", current.urn, other.urn))

        for current in target.values():
            if current.node in moved:
                continue
            if current.node in target_match:
                continue
            if current.kind == "program":
                continue
            values.add(("ADD", None, current.urn))

        return values

    def _match(
        self,
        source: dict[object, _Entity],
        target: dict[object, _Entity],
        mapping: object,
    ) -> tuple[dict[object, object], set[object]]:
        source_match: dict[object, object] = {}
        target_match: set[object] = set()

        for current in source.values():
            node = mapping.getDstForSrc(current.node)
            if node is None:
                continue

            other = target.get(node)
            if other is None:
                continue

            source_match[current.node] = other.node
            target_match.add(other.node)

        return source_match, target_match

    def _blocked(
        self,
        nodes: dict[object, _Entity],
        matched: set[object] | dict[object, object],
    ) -> set[str]:
        matched_nodes = set(matched)
        unmatched = set(nodes) - matched_nodes
        blocked: set[str] = set()

        for node in unmatched:
            parent = self._parent(nodes, node)
            while parent is not None:
                if parent in matched_nodes:
                    blocked.add(nodes[parent].urn)
                parent = self._parent(nodes, parent)

        return blocked

    def _content_changed(
        self,
        nodes: dict[object, _Entity],
        changed: set[object],
        matched: set[object] | dict[object, object],
        blocked: set[str] | None = None,
    ) -> set[object]:
        values: set[object] = set()
        matched_nodes = set(matched)
        blocked_urns = blocked or set()

        for node in changed:
            current = self._nearest(nodes, node)
            if current is None:
                continue
            if current.node not in matched_nodes:
                continue
            if current.urn in blocked_urns:
                continue

            values.add(current.node)

        return values

    def _nearest(
        self,
        nodes: dict[object, _Entity],
        node: object,
    ) -> _Entity | None:
        current = node

        while current is not None:
            entity = nodes.get(current)
            if entity is not None:
                return entity
            current = current.getParent()

        return None

    def _collect(self, root: object) -> dict[object, _Entity]:
        raw: dict[object, tuple[str, str]] = {}

        for node in root.preOrder():
            kind = self._kind(node)
            if kind is None:
                continue

            name = self._name(node, kind)
            if name is None:
                continue

            raw[node] = (kind, name)

        values: dict[object, _Entity] = {}
        for node in raw:
            self._entity(raw, values, node)
        return values

    def _kind(self, node: object) -> str | None:
        typ = str(node.getType())

        if self._language == "python":
            if typ == "module":
                return "program"
            if typ == "class_definition":
                return "class"
            if typ == "function_definition":
                return "function"
            return None

        if self._language in {"typescript", "javascript"}:
            if typ == "program":
                return "program"
            if typ == "class_declaration":
                return "class"
            if typ == "method_definition":
                return "method"
            if typ == "function_declaration":
                return "function"
            if typ == "variable_declarator" and self._function_like(node):
                return "function"
            return None

        return None

    def _name(self, node: object, kind: str) -> str | None:
        if kind == "program":
            return "program"

        if self._language == "python":
            if kind in {"class", "function", "method"}:
                child = self._child(node, {"identifier"})
                return self._label(child)
            return None

        if self._language in {"typescript", "javascript"}:
            if kind == "class":
                child = self._child(node, {"type_identifier", "identifier"})
                return self._label(child)
            if kind == "method":
                child = self._child(
                    node,
                    {
                        "property_identifier",
                        "private_property_identifier",
                        "identifier",
                    },
                )
                return self._label(child)
            if kind == "function":
                child = self._child(node, {"identifier"})
                return self._label(child)
            return None

        return None

    def _entity(
        self,
        nodes: dict[object, tuple[str, str]],
        values: dict[object, _Entity],
        node: object,
    ) -> _Entity:
        current = values.get(node)
        if current is not None:
            return current

        kind, name = nodes[node]
        parent = self._parent(nodes, node)
        owner = self._entity(nodes, values, parent) if parent is not None else None
        current_kind = kind
        if kind == "function" and owner is not None and owner.kind == "class":
            current_kind = "method"

        if current_kind == "program":
            urn = "entity:program"
        elif owner is None or owner.kind == "program":
            urn = f"entity:{current_kind}={name}"
        else:
            urn = f"{owner.urn}:{current_kind}={name}"

        current = _Entity(
            kind=current_kind,
            name=name,
            nested=owner is not None and owner.kind != "program",
            node=node,
            urn=urn,
        )
        values[node] = current
        return current

    def _parent(
        self,
        nodes: dict[object, object],
        node: object,
    ) -> object | None:
        current = node.getParent()

        while current is not None:
            if current in nodes:
                return current
            current = current.getParent()

        return None

    def _function_like(self, node: object) -> bool:
        for child in node.getChildren():
            typ = str(child.getType())
            if typ in {"arrow_function", "function_expression"}:
                return True
        return False

    def _child(self, node: object, values: set[str]) -> object | None:
        for child in node.getChildren():
            if str(child.getType()) in values:
                return child
        return None

    def _label(self, node: object | None) -> str | None:
        if node is None:
            return None
        value = str(node.getLabel())
        return value or None

    def _materialize(self, root: Path) -> tuple[Path, Path]:
        path = self._file.after or self._file.base or "file"
        suffix = "".join(Path(path).suffixes)
        base = root / f"base{suffix}"
        after = root / f"after{suffix}"
        base.write_bytes(self._base or b"")
        after.write_bytes(self._after or b"")
        return base, after
