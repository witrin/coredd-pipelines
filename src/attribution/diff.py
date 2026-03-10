from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Tuple, overload

import pygit2
from unidiff import PatchedFile, patch
from unidiff.patch import PatchInfo
from tree_sitter import Parser, Query, QueryCursor
from tree_sitter_language_pack import get_language
from benchmark.attribution.domain import (
    Anchor,
    Context,
    FileOperation,
    OperationType,
    Reference,
    Slice,
)
from benchmark.attribution.history import VersionControl


# class GumTreeBridge:
#     _lock = threading.Lock()
#     _started = False

#     def __init__(self, jar: Path | None = None) -> None:
#         self._jar = jar or Path("/usr/local/share/java/gumtree.jar")
#         self._ensure_jvm()

#     def _ensure_jvm(self) -> None:
#         with GumTreeBridge._lock:
#             if not GumTreeBridge._started:
#                 import jpype

#                 jpype.startJVM(
#                     "-XX:+IgnoreUnrecognizedVMOptions",
#                     "--enable-native-access=ALL-UNNAMED",
#                     classpath=[str(self._jar)],
#                 )
#                 GumTreeBridge._started = True

#     def diff(self, base_path: Path, after_path: Path) -> dict:
#         import json

#         import jpype

#         try:
#             Run = jpype.JClass("com.github.gumtreediff.client.Run")
#             Diff = jpype.JClass("com.github.gumtreediff.actions.Diff")
#             ActionsIoUtils = jpype.JClass("com.github.gumtreediff.io.ActionsIoUtils")
#             GumtreeProperties = jpype.JClass(
#                 "com.github.gumtreediff.matchers.GumtreeProperties"
#             )
#             StringWriter = jpype.JClass("java.io.StringWriter")

#             Run.initGenerators()
#             Run.initMatchers()

#             properties = GumtreeProperties()
#             diff = Diff.compute(str(base_path), str(after_path), None, None, properties)
#             self._raise_for_parser_error_nodes(diff)

#             writer = StringWriter()
#             ActionsIoUtils.toJson(diff.src, diff.editScript, diff.mappings).writeTo(
#                 writer
#             )
#             payload = json.loads(str(writer.toString()))
#             self._raise_for_error_payload(payload)
#             return payload
#         except Exception as exc:
#             raise RuntimeError("Failed to compute GumTree diff payload.") from exc

#     def _raise_for_parser_error_nodes(self, diff: object) -> None:
#         for side, tree_context in (("source", diff.src), ("target", diff.dst)):
#             root = tree_context.getRoot()
#             for node in root.preOrder():
#                 node_type = str(node.getType()).upper()
#                 if node_type not in {"ERROR", "MISSING"}:
#                     continue

#                 start = int(node.getPos())
#                 end = start + int(node.getLength())
#                 raise ValueError(
#                     f"GumTree parse error node {node_type} in {side} file at [{start},{end}]"
#                 )

#     @staticmethod
#     def _raise_for_error_payload(payload: object) -> None:
#         if not isinstance(payload, dict):
#             raise ValueError("GumTree returned non-object JSON payload.")

#         for key in ("error", "exception"):
#             value = payload.get(key)
#             if value:
#                 raise ValueError(f"GumTree reported {key} with {value}")

#         errors = payload.get("errors")
#         if isinstance(errors, list) and errors:
#             raise ValueError(f"GumTree reported errors: {errors[0]}")

#         status = payload.get("status")
#         if isinstance(status, str) and status.lower() in {"error", "failed", "failure"}:
#             message = payload.get("message") or status
#             raise ValueError(
#                 f"GumTree failed with status {status} and message {message}"
#             )

#         actions = payload.get("actions")
#         matches = payload.get("matches")
#         if not isinstance(actions, list) or not isinstance(matches, list):
#             raise ValueError(
#                 "GumTree JSON payload is missing list fields 'actions' and 'matches'."
#             )


# @dataclass(frozen=True)
# class Node:
#     typ: str
#     start: int
#     end: int
#     raw: str


Interval = Tuple[int, int]


@dataclass(frozen=True)
class ScopeRule:
    priority: int
    language: str | None
    comment: str
    match: Callable[[object, int], bool]
    scope: Callable[[object, int], object]


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


class PatchView:
    def __init__(self, source: patch.PatchSet | str) -> None:
        source = source
        if isinstance(source, patch.PatchSet):
            self._patch = source
            return
        rendered = source if source.endswith("\n") else f"{source}\n"
        try:
            self._patch = patch.PatchSet(rendered)
        except Exception as error:
            raise ValueError(
                f"PatchView requires parseable unified patch content: {rendered!r}"
            ) from error

    def render(self) -> str:
        rows: list[tuple[str | None, str]] = []
        for file_index, patched in enumerate(self._patch):
            for header in self._file_headers(patched):
                rows.append((None, header))
            for hunk_index, hunk in enumerate(patched):
                rows.append((None, self._hunk_header(hunk)))
                for line_index, line in enumerate(hunk):
                    rows.append(
                        (
                            f"{file_index}-{hunk_index}-{line_index}",
                            str(line).rstrip("\n"),
                        )
                    )
        width = max((len(reference) for reference, _ in rows if reference), default=0)
        return "\n".join(
            f"{(reference or '').ljust(width)} | {line}"
            for reference, line in rows
        )

    def base(self) -> str:
        blocks: list[str] = []
        for patched in self._patch:
            for hunk in patched:
                lines = [
                    (line.source_line_no, str(line).rstrip("\n")[1:])
                    for line in hunk
                    if line.line_type in {" ", "-"}
                    and isinstance(line.source_line_no, int)
                ]
                if not lines:
                    continue
                path = self._normalize_path(patched.source_file, source=True)
                if path is None:
                    path = self._normalize_path(patched.target_file, source=False)
                if path is None:
                    continue
                blocks.append(
                    "\n".join(
                        [
                            f"File: {path}",
                            *[line[1] for line in lines],
                        ]
                    )
                )
        return "\n\n".join(blocks)

    def after(self) -> str:
        blocks: list[str] = []
        for patched in self._patch:
            for hunk in patched:
                lines = [
                    (line.target_line_no, str(line).rstrip("\n")[1:])
                    for line in hunk
                    if line.line_type in {" ", "+"}
                    and isinstance(line.target_line_no, int)
                ]
                if not lines:
                    continue
                path = self._normalize_path(patched.target_file, source=False)
                if path is None:
                    path = self._normalize_path(patched.source_file, source=True)
                if path is None:
                    continue
                blocks.append(
                    "\n".join(
                        [
                            f"File: {path}",
                            *[line[1] for line in lines],
                        ]
                    )
                )
        return "\n\n".join(blocks)

    @overload
    def resolve(self, reference: str) -> Reference | None: ...

    @overload
    def resolve(self, reference: list[str]) -> list[Slice]: ...

    def resolve(self, reference: str | list[str]) -> Reference | None | list[Slice]:
        if isinstance(reference, list):
            grouped: dict[tuple[str | None, str | None], list[Anchor]] = {}
            seen: set[tuple[tuple[str | None, str | None], Anchor]] = set()

            for token in reference:
                current = self.resolve(token)
                if current is None:
                    continue
                patched = self._patch[current.file]
                line = patched[current.hunk][current.line]
                pair = (
                    self._normalize_path(patched.source_file, source=True),
                    self._normalize_path(patched.target_file, source=False),
                )
                anchor = self._anchor(line)
                if anchor is None:
                    continue
                key = (pair, anchor)
                if key in seen:
                    continue
                seen.add(key)
                grouped.setdefault(pair, []).append(anchor)

            return [
                Slice(file=pair, anchors=anchors)
                for pair, anchors in grouped.items()
            ]

        values = reference.split("-")
        if len(values) != 3:
            return None
        try:
            file = int(values[0])
            hunk = int(values[1])
            line = int(values[2])
        except ValueError:
            return None
        if file < 0 or hunk < 0 or line < 0:
            return None
        if file >= len(self._patch):
            return None
        patched = self._patch[file]
        if hunk >= len(patched):
            return None
        if line >= len(patched[hunk]):
            return None
        return Reference(file=file, hunk=hunk, line=line)

    def _anchor(self, line: patch.Line) -> Anchor | None:
        base = (
            (line.source_line_no, line.source_line_no + 1)
            if isinstance(line.source_line_no, int)
            else (0, 0)
        )
        after = (
            (line.target_line_no, line.target_line_no + 1)
            if isinstance(line.target_line_no, int)
            else (0, 0)
        )
        if base == (0, 0) and after == (0, 0):
            return None
        return Anchor(base=base, after=after)

    def _file_headers(self, patched: PatchedFile) -> list[str]:
        source = self._normalize_path(patched.source_file, source=True)
        target = self._normalize_path(patched.target_file, source=False)
        source_ref = "/dev/null" if source is None else f"a/{source}"
        target_ref = "/dev/null" if target is None else f"b/{target}"
        lines = [str(info).rstrip("\n") for info in patched.patch_info or []]
        if lines:
            if not any(line.startswith("--- ") for line in lines):
                lines.append(f"--- {source_ref}")
            if not any(line.startswith("+++ ") for line in lines):
                lines.append(f"+++ {target_ref}")
            return lines
        return [
            f"diff --git {source_ref} {target_ref}",
            f"--- {source_ref}",
            f"+++ {target_ref}",
        ]

    def _hunk_header(self, hunk: patch.Hunk) -> str:
        source = self._hunk_range(hunk.source_start, hunk.source_length)
        target = self._hunk_range(hunk.target_start, hunk.target_length)
        section = str(hunk.section_header).strip()
        if section:
            return f"@@ -{source} +{target} @@ {section}"
        return f"@@ -{source} +{target} @@"

    def _hunk_range(self, start: int, length: int) -> str:
        if length == 1:
            return f"{start}"
        return f"{start},{length}"

    def _normalize_path(self, value: object, *, source: bool) -> str | None:
        if not isinstance(value, str):
            return None
        path = value.strip()
        if path == "" or path == "/dev/null":
            return None
        if source and path.startswith("a/"):
            return path[2:]
        if not source and path.startswith("b/"):
            return path[2:]
        return path


class PatchContext:
    @staticmethod
    def project(
        file: FileOperation,
        base: bytes | None,
        after: bytes | None,
        hunks: List[Context],
    ) -> PatchedFile:
        source_ref = f"a/{file.base}" if file.base else "/dev/null"
        target_ref = f"b/{file.after}" if file.after else "/dev/null"
        result = PatchedFile(
            patch_info=PatchInfo([f"diff --git {source_ref} {target_ref}\n"]),
            source=source_ref,
            target=target_ref,
        )

        source = (
            base.decode("utf-8", "ignore").splitlines(keepends=True)
            if base is not None
            else []
        )

        target = (
            after.decode("utf-8", "ignore").splitlines(keepends=True)
            if after is not None
            else []
        )

        for h in hunks:
            # In unified diff, a hunk that affects zero lines is rendered
            # as an insertion after line N. Our internal model stores
            # boundaries before line k, so when a range contains no lines
            # we subtract one from the start when writing the header.
            # See also https://www.gnu.org/software/diffutils/manual/html_node/Detailed-Unified.html
            hunk = patch.Hunk(
                src_start=(
                    max(0, h.base[0] - 1) if (h.base[1] - h.base[0]) == 0 else h.base[0]
                ),
                src_len=(h.base[1] - h.base[0]),
                tgt_start=(
                    max(0, h.after[0] - 1)
                    if (h.after[1] - h.after[0]) == 0
                    else h.after[0]
                ),
                tgt_len=(h.after[1] - h.after[0]),
                section_header="",
            )

            cursor = (h.base[0], h.after[0])  # (base_cursor, after_cursor)

            for change in h.changes:
                for line in source[
                    cursor[0]
                    - 1 : cursor[0]
                    - 1
                    + min(change.base[0] - cursor[0], change.after[0] - cursor[1])
                ]:
                    hunk.append(patch.Line(line, " "))

                for line in source[change.base[0] - 1 : change.base[1] - 1]:
                    hunk.append(patch.Line(line, "-"))

                for line in target[change.after[0] - 1 : change.after[1] - 1]:
                    hunk.append(patch.Line(line, "+"))

                cursor = (change.base[1], change.after[1])

            for line in source[
                cursor[0]
                - 1 : cursor[0]
                - 1
                + min(h.base[1] - cursor[0], h.after[1] - cursor[1])
            ]:
                hunk.append(patch.Line(line, " "))

            result.append(hunk)

        return result

    @staticmethod
    def merge(hunks: List[Context]) -> List[Context]:
        if not hunks:
            return []

        hunks = sorted(hunks, key=lambda h: (h.base[0], h.after[0]))

        result: List[Context] = [hunks[0]]

        for candidate in hunks[1:]:
            active = result[-1]

            if (
                active.base[1] > candidate.base[0]
                or active.after[1] > candidate.after[0]
            ):
                result[-1] = Context(
                    base=(
                        min(active.base[0], candidate.base[0]),
                        max(active.base[1], candidate.base[1]),
                    ),
                    after=(
                        min(active.after[0], candidate.after[0]),
                        max(active.after[1], candidate.after[1]),
                    ),
                    changes=active.changes + candidate.changes,
                )
            else:
                result.append(candidate)

        return result

    @staticmethod
    @overload
    def extract(hunk: pygit2.Patch) -> List[Context]: ...

    @staticmethod
    @overload
    def extract(hunk: pygit2.Hunk) -> List[Anchor]: ...

    @staticmethod
    def extract(hunk: pygit2.Patch | pygit2.Hunk) -> List[Context] | List[Anchor]:
        if isinstance(hunk, pygit2.Patch):
            result: List[Context] = []

            for item in hunk.hunks:
                base = item.old_start + (1 if item.old_lines == 0 else 0)
                after = item.new_start + (1 if item.new_lines == 0 else 0)
                result.append(
                    Context(
                        base=(base, base + item.old_lines),
                        after=(after, after + item.new_lines),
                        changes=PatchContext.extract(item),
                    )
                )

            return result

        # In unified diff, a hunk that affects zero lines is rendered
        # as an insertion after line N. Our internal model stores
        # boundaries before line k, so when a range contains no lines
        # we subtract one from the start when writing the header.
        # See also https://www.gnu.org/software/diffutils/manual/html_node/Detailed-Unified.html
        base = hunk.old_start + (1 if hunk.old_lines == 0 else 0)
        after = hunk.new_start + (1 if hunk.new_lines == 0 else 0)

        out: List[Anchor] = []

        open = False
        start = base
        end = base
        lower = after
        upper = after

        for line in hunk.lines:
            origin = line.origin

            if origin == " ":
                if open:
                    out.append(Anchor(base=(start, end), after=(lower, upper)))
                    open = False
                base += 1
                after += 1
                continue

            if not open:
                open = True
                start = end = base
                lower = upper = after

            if origin == "-":
                end = base + 1
                base += 1
            elif origin == "+":
                upper = after + 1
                after += 1

        if open:
            out.append(Anchor(base=(start, end), after=(lower, upper)))

        return out


class PatchFactory:
    # TODO: Add the paper's explicit line-based expansion for supported-language
    # changes outside enclosing scopes. The current implementation always expands
    # to the next structural scope and does not implement the exact "three
    # surrounding lines" behavior described in the paper.
    _extensions = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "tsx",
    }

    def __init__(self, repository: VersionControl) -> None:
        self.repository = repository
        self._scope = PatchScope()

    def create(
        self,
        base: str,
        after: str,
        files: Iterable[FileOperation] | None = None,
        *,
        n: int = 3,
    ) -> patch.PatchSet:
        parsed = patch.PatchSet(
            self.repository.diff(base, after, context=n).patch
        )
        if files is None:
            return parsed

        selected = {(file.base, file.after) for file in files}

        for index in range(len(parsed) - 1, -1, -1):
            source = parsed[index].source_file
            target = parsed[index].target_file
            if source == "/dev/null":
                source = None
            elif isinstance(source, str) and source.startswith("a/"):
                source = source[2:]
            if target == "/dev/null":
                target = None
            elif isinstance(target, str) and target.startswith("b/"):
                target = target[2:]
            if (source, target) not in selected:
                del parsed[index]

        return parsed

    def expand(
        self,
        base: str,
        after: str,
        files: Iterable[FileOperation] | None = None,
        *,
        level: int = 1,
        n: int = 3,
    ) -> patch.PatchSet:
        result = patch.PatchSet("")
        diff = self.repository.diff(base, after)

        for file in files or self.repository.manifest(base, after):
            delta = self._locate(diff, file)
            if delta is None:
                continue

            source = self.read(base, file.base)
            target = self.read(after, file.after)
            hunks: List[Context] = []

            path = file.after or file.base or ""
            language = self._extensions.get(path.split(".")[-1], None)

            if language:
                parser = Parser()
                parser.language = get_language(language)
                roots = [parser.parse(source).root_node, parser.parse(target).root_node]
                self._scope.validate(roots[0], language, path, "base")
                self._scope.validate(roots[1], language, path, "after")

                lines = [
                    source.decode("utf-8", "ignore").splitlines(),
                    target.decode("utf-8", "ignore").splitlines(),
                ]

                changes: List[Anchor] = []
                for hunk in delta.hunks:
                    changes.extend(PatchContext.extract(hunk))
                changes = [
                    change
                    for change in changes
                    if any(
                        line.strip()
                        for line in (
                            lines[0][change.base[0] - 1 : change.base[1] - 1]
                            + lines[1][change.after[0] - 1 : change.after[1] - 1]
                        )
                    )
                ]

                context = self._context(changes, [len(lines[0]), len(lines[1])])

                for change in changes:
                    hunks.append(
                        self._normalize(
                            change,
                            [
                                self._expand(
                                    roots[0],
                                    lines[0],
                                    change.base,
                                    level,
                                    language,
                                ),
                                self._expand(
                                    roots[1],
                                    lines[1],
                                    change.after,
                                    level,
                                    language,
                                ),
                            ],
                            context,
                        )
                    )

                hunks = [hunk for hunk in PatchContext.merge(hunks)]
            else:
                delta = self._locate(
                    self.repository.diff(base, after, context=n), file
                )
                if delta is None:
                    continue
                hunks.extend(PatchContext.extract(delta))

            if hunks:
                result.append(PatchContext.project(file, source, target, hunks))

        return result

    def refine(
        self,
        base: str,
        after: str,
        slices: list[Slice],
        anchor: str,
        n: int = 2,
        force: bool = False,
    ) -> patch.PatchSet:
        if not slices:
            raise ValueError("PatchFactory.refine requires non-empty slices.")
        if anchor not in {"base", "after"}:
            raise ValueError("PatchFactory.refine requires anchor to be 'base' or 'after'.")

        result = patch.PatchSet("")
        operations = {
            (operation.base, operation.after): operation
            for operation in self.repository.manifest(base, after)
        }
        groups: dict[tuple[str | None, str | None], list[Anchor]] = {}

        for item in slices:
            if not item.anchors:
                continue
            groups.setdefault(item.file, []).extend(item.anchors)

        diff = self.repository.diff(base, after, context=3)

        for file, anchors in groups.items():
            operation = operations.get(file)
            if operation is None:
                operation = FileOperation(
                    base=file[0],
                    after=file[1],
                    operation=(
                        OperationType.ADD if file[0] is None
                        else OperationType.DELETE if file[1] is None
                        else OperationType.EDIT
                    ),
                )
            source = self.read(base, operation.base)
            target = self.read(after, operation.after)
            size = {
                "base": len(source.decode("utf-8", "ignore").splitlines())
                if source is not None
                else 0,
                "after": len(target.decode("utf-8", "ignore").splitlines())
                if target is not None
                else 0,
            }
            primary = self._primary_window(size[anchor], anchors, anchor, n)
            delta = self._locate(diff, operation)
            lower = primary[0]
            upper = primary[1] - 1

            if force:
                mapped_lower = self._map_line(delta, lower, anchor)
                while mapped_lower is None and lower > 1:
                    lower -= 1
                    mapped_lower = self._map_line(delta, lower, anchor)
                mapped_upper = self._map_line(delta, upper, anchor)
                while mapped_upper is None and upper < size[anchor]:
                    upper += 1
                    mapped_upper = self._map_line(delta, upper, anchor)
                if mapped_lower is None or mapped_upper is None:
                    continue
                primary = (lower, upper + 1)
                secondary = (mapped_lower, mapped_upper + 1)
            else:
                mapped = [
                    target
                    for line in range(primary[0], primary[1])
                    for target in [self._map_line(delta, line, anchor)]
                    if target is not None
                ]
                secondary = (
                    (min(mapped), max(mapped) + 1)
                    if mapped
                    else (0, 0)
                )

            base_range, after_range = (
                (primary, secondary)
                if anchor == "base"
                else (secondary, primary)
            )

            if base_range == (0, 0) or after_range == (0, 0):
                context = Context(
                    base=base_range,
                    after=after_range,
                    changes=[Anchor(base=base_range, after=after_range)],
                )
            else:
                if delta is None:
                    continue
                primary_range = base_range if anchor == "base" else after_range
                for current in PatchContext.extract(delta):
                    for change in current.changes:
                        if not self._overlaps(
                            change.base if anchor == "base" else change.after,
                            primary_range,
                        ):
                            continue
                        base_range = (
                            min(base_range[0], change.base[0]),
                            max(base_range[1], change.base[1]),
                        )
                        after_range = (
                            min(after_range[0], change.after[0]),
                            max(after_range[1], change.after[1]),
                        )

                scopes = [
                    current
                    for current in PatchContext.extract(delta)
                    if self._overlaps(current.base, base_range)
                    or self._overlaps(current.after, after_range)
                ]
                changes = [
                    change
                    for current in scopes
                    for change in current.changes
                    if self._overlaps(change.base, base_range)
                    or self._overlaps(change.after, after_range)
                ]
                if not changes and scopes:
                    changes = [
                        change
                        for current in scopes
                        for change in current.changes
                    ]
                if not changes:
                    continue
                context = Context(
                    base=base_range,
                    after=after_range,
                    changes=changes,
                )

            result.append(
                PatchContext.project(
                    operation,
                    source,
                    target,
                    [context],
                )
            )

        return result

    def _primary_window(
        self,
        size: int,
        anchors: list[Anchor],
        anchor: str,
        n: int,
    ) -> tuple[int, int]:
        intervals: list[tuple[int, int]] = []
        for item in anchors:
            current = item.base if anchor == "base" else item.after
            opposite = item.after if anchor == "base" else item.base
            if current == (0, 0):
                if opposite != (0, 0):
                    raise ValueError(
                        "PatchFactory.refine requires anchors on the selected side."
                    )
                continue
            intervals.append(current)

        if not intervals:
            raise ValueError(
                "PatchFactory.refine requires anchors on the selected side."
            )

        start = min(item[0] for item in intervals)
        end = max(item[1] for item in intervals)
        return (max(1, start - n), min(size + 1, end + n))

    def _map_line(
        self,
        delta: pygit2.Patch | None,
        line: int,
        anchor: str,
    ) -> int | None:
        if delta is None:
            return line

        for hunk in delta.hunks:
            base = hunk.old_start + (1 if hunk.old_lines == 0 else 0)
            after = hunk.new_start + (1 if hunk.new_lines == 0 else 0)

            for item in hunk.lines:
                if item.origin == " ":
                    if anchor == "base" and base == line:
                        return after
                    if anchor == "after" and after == line:
                        return base
                    base += 1
                    after += 1
                    continue
                if item.origin == "-":
                    if anchor == "base" and base == line:
                        return None
                    base += 1
                    continue
                if item.origin == "+":
                    if anchor == "after" and after == line:
                        return None
                    after += 1

        return line
    
    def _overlaps(
        self,
        candidate: tuple[int, int],
        interval: tuple[int, int],
    ) -> bool:
        if candidate == (0, 0) or interval == (0, 0):
            return False
        return not (
            candidate[1] <= interval[0]
            or candidate[0] >= interval[1]
        )

    def _locate(self, diff: pygit2.Diff, file: FileOperation) -> pygit2.Patch | None:
        for item in diff:
            if (file.after and item.delta.new_file.path == file.after) or (
                file.base and item.delta.old_file.path == file.base
            ):
                return item
        return None

    def read(self, commit: str, path: str | None) -> bytes | None:
        if path is None:
            return b""
        try:
            return self.repository.blob(commit, path)
        except FileNotFoundError:
            return b""

    def _expand(
        self, root, lines: List[str], interval: Interval, context: int, language: str
    ) -> Interval:
        start, end = interval
        lower = start
        upper = end - 1

        for line in range(start, end if end > start else start + 1):
            if end == start:
                continue
            if line <= 0 or line > len(lines):
                continue
            if lines[line - 1].strip() == "":
                continue

            column = len(lines[line - 1]) - len(lines[line - 1].lstrip())
            node = root.descendant_for_point_range(
                (line - 1, column), (line - 1, column)
            )

            current = self._scope.scope(node, context, language)

            lower = min(lower, current.start_point[0] + 1)
            upper = max(upper, current.end_point[0] + 1)

        return (lower, upper + 1)

    @staticmethod
    def _context(changes: List[Anchor], size: Tuple[int, int]) -> List[Tuple[int, int]]:
        base = set()
        after = set()

        for c in changes:
            for n in range(c.base[0], c.base[1]):
                base.add(n)
            for n in range(c.after[0], c.after[1]):
                after.add(n)

        pairs: List[Tuple[int, int]] = []

        left = 1
        right = 1

        while left <= size[0] and right <= size[1]:
            while left <= size[0] and left in base:
                left += 1
            while right <= size[1] and right in after:
                right += 1
            if left <= size[0] and right <= size[1]:
                pairs.append((left, right))
                left += 1
                right += 1

        return pairs

    @staticmethod
    def _normalize(
        change: Anchor,
        scopes: Tuple[Interval, Interval],
        context: List[Tuple[int, int]],
    ) -> Context:
        index = (
            {pair[0]: i for i, pair in enumerate(context)},
            {pair[1]: i for i, pair in enumerate(context)},
        )

        low = [float("inf"), float("inf")]
        high = [-1, -1]
        anchors = [change.base, change.after]

        for side in (0, 1):
            start, end = anchors[side]

            while start > scopes[side][0] and (start - 1) in index[side]:
                start -= 1
            while end < scopes[side][1] and end in index[side]:
                end += 1

            if start in index[side]:
                low[side] = index[side][start]
            if (end - 1) in index[side]:
                high[side] = index[side][end - 1]

        low = (
            (
                context[min(low[0], low[1])][0]
                if low[0] < float("inf") and min(low[0], low[1]) < float("inf")
                else anchors[0][0]
            ),
            (
                context[min(low[0], low[1])][1]
                if low[1] < float("inf") and min(low[0], low[1]) < float("inf")
                else anchors[1][0]
            ),
        )
        high = (
            (
                context[max(high[0], high[1])][0]
                if high[0] > -1 and max(high[0], high[1]) > -1
                else anchors[0][1] - 1
            ),
            (
                context[max(high[0], high[1])][1]
                if high[0] > -1 and max(high[0], high[1]) > -1
                else anchors[1][1] - 1
            ),
        )

        return Context(
            base=(min(low[0], anchors[0][0]), max(high[0] + 1, anchors[0][1])),
            after=(min(low[1], anchors[1][0]), max(high[1] + 1, anchors[1][1])),
            changes=[change],
        )
