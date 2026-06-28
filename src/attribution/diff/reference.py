from __future__ import annotations

import enum
from typing import Iterable, List, Tuple, overload

import pygit2
from tree_sitter_language_pack import get_parser
from unidiff import PatchedFile, patch
from unidiff.patch import PatchInfo

from benchmark.attribution.diff.context import PatchScope
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


class Numbering(enum.Enum):
    """How a PatchView prefixes each rendered line.

    NONE prefixes nothing. REF prefixes the file-hunk-line reference label (used by
    HintNode to address lines). AFTER and BASE prefix the absolute line number in the
    grep format ``L<n>:`` on the after / base revision; BOTH prefixes both numbers.
    """

    NONE = "none"
    REF = "ref"
    AFTER = "after"
    BASE = "base"
    BOTH = "both"


class PatchView:
    def __init__(
        self,
        source: patch.PatchSet | str,
        numbering: Numbering = Numbering.NONE,
    ) -> None:
        self._numbering = numbering
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
                            self._prefix(line, file_index, hunk_index, line_index),
                            str(line).rstrip("\n"),
                        )
                    )
        width = max((len(reference) for reference, _ in rows if reference), default=0)
        return "\n".join(
            f"{(reference or '').ljust(width)} | {line}"
            for reference, line in rows
        )

    def _prefix(
        self, line: patch.Line, file_index: int, hunk_index: int, line_index: int
    ) -> str | None:
        # The left-column label for a rendered diff line, per numbering mode. REF is
        # the addressable file-hunk-line reference; AFTER/BASE/BOTH are absolute line
        # numbers in grep format, blank where the line has no number on that side.
        if self._numbering is Numbering.REF:
            return f"{file_index}-{hunk_index}-{line_index}"
        after = (
            f"L{line.target_line_no}:"
            if isinstance(line.target_line_no, int)
            else ""
        )
        base = (
            f"L{line.source_line_no}:"
            if isinstance(line.source_line_no, int)
            else ""
        )
        if self._numbering is Numbering.AFTER:
            return after
        if self._numbering is Numbering.BASE:
            return base
        if self._numbering is Numbering.BOTH:
            return f"{base} {after}".strip()
        return None

    def _line_numbered(self) -> bool:
        # The single-side snippet views show their line number when an absolute
        # numbering mode is selected; REF and NONE render bare text.
        return self._numbering in {Numbering.AFTER, Numbering.BASE, Numbering.BOTH}

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
                            *[
                                f"L{line[0]}: {line[1]}"
                                if self._line_numbered()
                                else line[1]
                                for line in lines
                            ],
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
                            *[
                                f"L{line[0]}: {line[1]}"
                                if self._line_numbered()
                                else line[1]
                                for line in lines
                            ],
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
    def _terminate(line: str) -> str:
        # splitlines(keepends=True) preserves the original terminators, so the
        # final line of a file without a trailing newline comes back without one.
        # Emitting it verbatim glues that body line to the next rendered line
        # (e.g. "-foo" followed by "+foo" collapses into "-foo+foo"), which both
        # corrupts the hunk body count and makes the patch unparseable. We append
        # a newline to every emitted line; the trailing content is unchanged.
        return line if line.endswith(("\n", "\r")) else f"{line}\n"

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
            # Emit the hunk body first, then derive the @@ header lengths from the
            # lines actually emitted. A context line is one unchanged line shared
            # by both revisions, so its source and target advance together; the
            # gaps between consecutive changes are equal on both axes for any
            # well-formed hunk. We render context from the source side and count
            # what we emit, so the declared header can never disagree with the body.
            lines: list[patch.Line] = []
            cursor = (h.base[0], h.after[0])  # (base_cursor, after_cursor)

            for change in h.changes:
                lead = change.base[0] - cursor[0]
                for line in source[cursor[0] - 1 : cursor[0] - 1 + lead]:
                    lines.append(patch.Line(PatchContext._terminate(line), " "))

                for line in source[change.base[0] - 1 : change.base[1] - 1]:
                    lines.append(patch.Line(PatchContext._terminate(line), "-"))

                for line in target[change.after[0] - 1 : change.after[1] - 1]:
                    lines.append(patch.Line(PatchContext._terminate(line), "+"))

                cursor = (change.base[1], change.after[1])

            tail = h.base[1] - cursor[0]
            for line in source[cursor[0] - 1 : cursor[0] - 1 + tail]:
                lines.append(patch.Line(PatchContext._terminate(line), " "))

            base_len = sum(1 for line in lines if line.line_type in (" ", "-"))
            after_len = sum(1 for line in lines if line.line_type in (" ", "+"))

            # In unified diff, a hunk that affects zero lines on one side is
            # rendered as an insertion/deletion after line N. Our internal model
            # stores boundaries before line k, so when a side contributes no lines
            # we subtract one from its start when writing the header.
            # See also https://www.gnu.org/software/diffutils/manual/html_node/Detailed-Unified.html
            hunk = patch.Hunk(
                src_start=max(0, h.base[0] - 1) if base_len == 0 else h.base[0],
                src_len=base_len,
                tgt_start=max(0, h.after[0] - 1) if after_len == 0 else h.after[0],
                tgt_len=after_len,
                section_header="",
            )
            for line in lines:
                hunk.append(line)

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

        for file in files or self.repository.manifest(base, after):
            source = self.read(base, file.base)
            target = self.read(after, file.after)
            hunks: List[Context] = []

            path = file.after or file.base or ""
            try:
                language = PatchScope.language(file)
            except ValueError:
                language = None

            if language:
                parser = get_parser(language)
                roots = [parser.parse(source).root_node, parser.parse(target).root_node]
                self._scope.validate(roots[0], language, path, "base")
                self._scope.validate(roots[1], language, path, "after")

                lines = [
                    source.decode("utf-8", "ignore").splitlines(),
                    target.decode("utf-8", "ignore").splitlines(),
                ]

                # Diff the two file contents directly rather than reusing the
                # commit diff. The commit diff carries rename/copy detection,
                # which can pair this path with an unrelated file that shares
                # content (e.g. a common license header) and thereby drop the
                # file's real change. The endpoints named by the FileOperation
                # are authoritative; diffing their bytes is what the operation
                # actually means, independent of how the commit diff grouped it.
                changes: List[Anchor] = []
                for hunk in pygit2.Patch.create_from(
                    source,
                    target,
                    old_as_path=path,
                    new_as_path=path,
                    context_lines=0,
                ).hunks:
                    changes.extend(PatchContext.extract(hunk))

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
        # A renamed path can carry two deltas in one diff: the rename itself
        # (old=base, new=after) and a same-path delete (old=after, new=after).
        # Both match `after`, so matching either endpoint alone may pick the
        # empty delete and drop every added line. When the operation names both
        # endpoints we require the delta to match both; otherwise the present
        # endpoint alone identifies a pure addition or deletion.
        for item in diff:
            base_ok = item.delta.old_file.path == file.base
            after_ok = item.delta.new_file.path == file.after
            if file.base is not None and file.after is not None:
                if base_ok and after_ok:
                    return item
            elif (file.after is not None and after_ok) or (
                file.base is not None and base_ok
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

        # Expand both sides to the same paired context line. A context line only
        # exists if it is unchanged in both revisions, so the base and after
        # ranges must grow together: taking one side from a context pair without
        # its partner yields an asymmetric hunk whose declared lengths no longer
        # match the lines project() can emit (UnidiffParseError downstream).
        lowest = min(low[0], low[1])
        highest = max(high[0], high[1])
        low = (
            context[lowest][0] if lowest < float("inf") else anchors[0][0],
            context[lowest][1] if lowest < float("inf") else anchors[1][0],
        )
        high = (
            context[highest][0] if highest > -1 else anchors[0][1] - 1,
            context[highest][1] if highest > -1 else anchors[1][1] - 1,
        )

        return Context(
            base=(min(low[0], anchors[0][0]), max(high[0] + 1, anchors[0][1])),
            after=(min(low[1], anchors[1][0]), max(high[1] + 1, anchors[1][1])),
            changes=[change],
        )


class SourceManifest:
    """Narrow a change manifest to source files before the patch is parsed.

    The localization graph operates on the corrective change, whose commit may
    touch non-source files (README, JSON, plain text). Those cannot be parsed
    as part of a unified diff downstream (PatchView) and crash the whole graph.
    This narrows the manifest to source files at the entry, before any node
    expands or parses the patch.
    """

    def __init__(self, repository: VersionControl) -> None:
        self._repository = repository

    def select(self, base: str, after: str) -> List[FileOperation]:
        operations: list[FileOperation] = []
        for operation in self._repository.manifest(base, after):
            try:
                language = PatchScope.language(operation)
            except ValueError:
                # Only source files can host a defect. Drop documentation,
                # configuration, data, and markup before the patch is parsed.
                continue
            # The upstream scope restricts the benchmark to Python repositories,
            # so a defect lives in Python source (.py is the only Python suffix
            # PatchScope recognizes). Drop the other languages PatchScope can
            # parse (.js/.ts/.tsx) before they reach the locate stage.
            if language != "python":
                continue
            operations.append(operation)
        return operations
