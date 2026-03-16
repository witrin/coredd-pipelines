from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Tuple, overload

import pygit2
from unidiff import PatchedFile, patch
from unidiff.patch import PatchInfo
from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser
from benchmark.attribution.domain import (
    Anchor,
    Context,
    FileOperation,
    OperationType,
    Reference,
    Slice,
)
from benchmark.attribution.history import VersionControl


Interval = Tuple[int, int]


@dataclass(frozen=True)
class ScopeRule:
    priority: int
    language: str | None
    comment: str
    match: Callable[[object, int], bool]
    scope: Callable[[object, int], object]


@dataclass(frozen=True)
class Statement:
    scope: str
    excerpt: str


class ScopePath:
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
        parser = get_parser(language)
        source = base if base is not None else b""
        target = after if after is not None else b""
        self._root = (
            parser.parse(source).root_node,
            parser.parse(target).root_node,
        )
        self._lines = (
            tuple(source.decode("utf-8", "ignore").splitlines()),
            tuple(target.decode("utf-8", "ignore").splitlines()),
        )
        path = file.after or file.base or ""
        scope = PatchScope()
        scope.validate(self._root[0], language, path, "base")
        scope.validate(self._root[1], language, path, "after")

    @property
    def language(self) -> str:
        return self._language

    def resolve(
        self,
        anchor: Anchor,
    ) -> tuple[Statement | None, Statement | None]:
        return (
            self._resolve(
                anchor.base,
                self._root[0],
                self._lines[0],
                self._base if self._base is not None else b"",
            ),
            self._resolve(
                anchor.after,
                self._root[1],
                self._lines[1],
                self._after if self._after is not None else b"",
            ),
        )

    def _resolve(
        self,
        interval: tuple[int, int],
        root: object,
        lines: tuple[str, ...],
        source: bytes,
    ) -> Statement | None:
        if interval == (0, 0):
            return None

        node = self._node(interval, root, lines)
        if node is None:
            return None

        owner = PatchScope().scope(node, 1, self._language)
        return Statement(
            scope=self._scope_value(node, owner, source),
            excerpt=self._excerpt(node, owner, source),
        )

    def _node(
        self,
        interval: tuple[int, int],
        root: object,
        lines: tuple[str, ...],
    ) -> object | None:
        start, end = interval
        for line in range(start, end if end > start else start + 1):
            if line <= 0 or line > len(lines):
                continue
            if lines[line - 1].strip() == "":
                continue
            column = len(lines[line - 1]) - len(lines[line - 1].lstrip())
            node = root.descendant_for_point_range(
                (line - 1, column), (line - 1, column)
            )
            while node is not None and not getattr(node, "is_named", False):
                node = node.parent
            if node is not None:
                return self._focus(node)
        return None

    def _focus(self, node: object) -> object:
        current = node
        while current is not None:
            if current.type in {"export_statement", "expression_statement"}:
                children = list(getattr(current, "named_children", []))
                if children:
                    current = children[0]
                    continue
            if current.type in {"lexical_declaration", "variable_declaration"}:
                for child in getattr(current, "named_children", []):
                    if child.type == "variable_declarator":
                        current = child
                        break
                else:
                    return current
                continue
            return current
        return node

    def _scope_value(self, node: object, owner: object, source: bytes) -> str:
        items: list[tuple[str, str]] = []
        current = owner
        while current is not None:
            segment = self._owner(current, source)
            if segment is not None:
                items.append(segment)
            current = current.parent
        items.reverse()
        deduplicated: list[tuple[str, str]] = []
        for item in items:
            if not deduplicated or deduplicated[-1] != item:
                deduplicated.append(item)
        items = deduplicated
        current = node
        while current is not None and current is not owner:
            segment = self._owner(current, source)
            if segment is not None:
                if not items or items[-1] != segment:
                    items.append(segment)
                break
            current = current.parent
        if items:
            return "scope:" + ":".join(f"{key}={value}" for key, value in items)
        if str(getattr(owner, "type", "")) in {
            "function_definition",
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
            "class_declaration",
        }:
            return f"scope:type={self._clean(str(owner.type))}"
        return "scope:program"

    def _owner(self, node: object, source: bytes) -> tuple[str, str] | None:
        if node.type == "class_definition":
            value = self._name(node, source)
            return ("class", value) if value else None
        if node.type == "function_definition":
            value = self._name(node, source)
            if not value:
                return None
            current = node.parent
            while current is not None:
                if current.type == "class_definition":
                    return ("method", value)
                if current.type in {"module", "program"}:
                    break
                current = current.parent
            return ("function", value)
        mapping = {
            "class_declaration": ("class", self._name),
            "method_definition": ("method", self._name),
            "function_declaration": ("function", self._function),
            "function_expression": ("function", self._function),
            "arrow_function": ("function", self._function),
            "variable_declarator": ("function", self._function),
        }
        current = mapping.get(node.type)
        if current is None:
            return None
        key, method = current
        value = method(node, source)
        if not value:
            return None
        return (key, value)

    def _excerpt(self, node: object, owner: object, source: bytes) -> str:
        current = node
        while current is not None:
            value = self._statement(current, source)
            if value:
                return value
            if current is owner:
                break
            current = current.parent
        return self._line(owner, source)

    def _name(self, node: object, source: bytes) -> str | None:
        current = node.child_by_field_name("name")
        if current is None:
            return None
        return self._clean(self._text(current, source))

    def _function(self, node: object, source: bytes) -> str | None:
        current = node
        if current.type in {"arrow_function", "function_expression"}:
            parent = current.parent
            if parent is not None and parent.type == "variable_declarator":
                current = parent
        if current.type == "variable_declarator":
            name = current.child_by_field_name("name")
            if name is not None:
                return self._clean(self._text(name, source))
        return self._name(current, source)

    def _statement(self, node: object, source: bytes) -> str | None:
        mapping = {
            "import_statement": self._full,
            "import_from_statement": self._full,
            "throw_statement": self._full,
            "return_statement": self._full,
            "if_statement": self._header,
            "while_statement": self._header,
            "class_declaration": self._header,
            "class_definition": self._header,
            "function_definition": self._header,
            "function_declaration": self._header,
            "method_definition": self._header,
            "assignment_expression": self._statement_parent,
            "augmented_assignment_expression": self._statement_parent,
            "assignment": self._statement_parent,
            "augmented_assignment": self._statement_parent,
            "call_expression": self._statement_parent,
            "function_expression": self._declaration_line,
            "arrow_function": self._declaration_line,
            "variable_declarator": self._variable_line,
        }
        current = mapping.get(node.type)
        if current is None:
            return None
        return current(node, source)

    def _full(self, node: object, source: bytes) -> str:
        return self._text(node, source).strip()

    def _header(self, node: object, source: bytes) -> str:
        return self._line(node, source)

    def _statement_parent(self, node: object, source: bytes) -> str:
        parent = node.parent
        if parent is not None and parent.type == "expression_statement":
            return self._text(parent, source).strip()
        return self._text(node, source).strip()

    def _declaration_line(self, node: object, source: bytes) -> str:
        parent = node.parent
        if parent is not None and parent.type == "variable_declarator":
            current = parent.parent if parent.parent is not None else parent
            return self._line(current, source)
        return self._line(node, source)

    def _variable_line(self, node: object, source: bytes) -> str:
        current = node.parent if node.parent is not None else node
        return self._line(current, source)

    def _text(self, node: object | None, source: bytes) -> str:
        if node is None:
            return ""
        return source[node.start_byte : node.end_byte].decode("utf-8", "ignore")

    def _line(self, node: object | None, source: bytes) -> str:
        return self._text(node, source).splitlines()[0].strip()

    def _strip(self, value: str) -> str:
        return value.strip().strip('"').strip("'")

    def _clean(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.strip()).replace(":", "\\:")


class PatchScope:
    _increments = {
        "python": {
            "function_definition",
            "class_definition",
            "decorated_definition",
        },
        "javascript": {
            "function_declaration",
            "function_expression",
            "arrow_function",
            "class_declaration",
            "method_definition",
        },
        "typescript": {
            "function_declaration",
            "function_expression",
            "arrow_function",
            "class_declaration",
            "method_definition",
        },
        "tsx": {
            "function_declaration",
            "function_expression",
            "arrow_function",
            "class_declaration",
            "method_definition",
        },
    }

    _roots = {
        "python": {"module"},
        "javascript": {"program"},
        "typescript": {"program"},
        "tsx": {"program"},
    }

    @property
    def rules(self) -> List[ScopeRule]:
        return [
            ScopeRule(
                priority=10,
                language=None,
                comment="Comment stays local.",
                match=lambda node, context: "comment" in str(node.type).lower(),
                scope=lambda node, context: node,
            ),
            ScopeRule(
                priority=20,
                language=None,
                comment="Root import stays local in Python.",
                match=lambda node, context: (
                    (
                        str(node.type) in {"import_statement", "import_from_statement"}
                        and node.parent is not None
                        and str(node.parent.type) in {"module", "program"}
                    )
                    or (
                        node.parent is not None
                        and str(node.parent.type)
                        in {"import_statement", "import_from_statement"}
                        and node.parent.parent is not None
                        and str(node.parent.parent.type) in {"module", "program"}
                    )
                    or (
                        node.parent is not None
                        and node.parent.parent is not None
                        and str(node.parent.parent.type)
                        in {"import_statement", "import_from_statement"}
                        and node.parent.parent.parent is not None
                        and str(node.parent.parent.parent.type) in {"module", "program"}
                    )
                ),
                scope=lambda node, context: node,
            ),
        ]

    @staticmethod
    def language(file: FileOperation, language: str | None = None) -> str:
        current = language
        if current is None:
            path = file.after or file.base or ""
            suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            current = {
                "py": "python",
                "js": "javascript",
                "ts": "typescript",
                "tsx": "tsx",
            }.get(suffix)
        if current is None:
            raise ValueError("PatchScope.language requires supported language.")
        try:
            get_language(current)
        except Exception as error:
            raise ValueError(
                "PatchScope.language requires supported language."
            ) from error
        return current

    def validate(self, root, language: str, path: str, side: str) -> None:
        for node in (
            QueryCursor(
                Query(
                    get_language(language),
                    "[(ERROR) (MISSING)] @issue",
                )
            )
            .captures(root)
            .get("issue", [])
        ):
            raise RuntimeError(
                f"Failed to parse {side} file {path} at line {node.start_point[0] + 1}."
            )

    def scope(self, node, context: int, language: str):
        for rule in sorted(self.rules, key=lambda item: item.priority):
            if rule.language is not None and rule.language != language:
                continue
            if rule.match(node, context):
                return rule.scope(node, context)

        remaining = context
        if str(node.type) in self._increments.get(language, set()):
            remaining -= 1

        current = node
        while current is not None and remaining > 0:
            parent = current.parent
            if parent is None:
                break
            if str(parent.type) in self._roots.get(language, set()):
                current = parent
                break
            if str(parent.type) in self._increments.get(language, set()):
                remaining -= 1
            current = parent
        return current if current is not None else node

    def bind(
        self,
        file: FileOperation,
        base: bytes | None,
        after: bytes | None,
        *,
        language: str | None = None,
    ) -> ScopePath:
        return ScopePath(
            file=file,
            base=base,
            after=after,
            language=PatchScope.language(file, language),
        )


