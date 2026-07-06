from __future__ import annotations

import re
from typing import Any

import pygit2
from langchain_core.tools import BaseTool, tool
from pathspec import PathSpec

from benchmark.attribution.agent import DiffRenderer
from benchmark.attribution.diff.reference import Numbering, PatchView
from benchmark.attribution.domain import Change
from benchmark.attribution.history import VersionControl


class LocateTool:
    """Snapshot-local tool surface for locating a defect inside the inducing change.

    The agent searches the inducing change (the defective commit) and may read the
    corrective change for context. The inducing change renders every line with a
    reference — ``L<n>`` for a present line, ``D<n>`` for a deleted one — so the agent
    can report any changed line, deletions included; the correction renders without
    references, as read-only material.

    Unlike the attribution tool surface, these tools impose no ancestry guard.
    Both commits are fixed inputs; there is no history search to constrain.
    """

    def __init__(
        self,
        repository: VersionControl,
        correction: Change,
        defect: Change,
    ) -> None:
        self._repository = repository
        self._correction = correction
        self._defect = defect

    def tools(self) -> list[BaseTool]:
        return [
            tool(self.show),
            tool(self.grep),
        ]

    def _commits(self) -> str:
        # The two commits the agent may operate on. Abbreviated SHAs suffice — the
        # agent passes them back through _resolve, which expands any prefix.
        return (
            f"the defective change {self._defect['after'][:12]} "
            f"or the corrective change {self._correction['after'][:12]}"
        )

    def _resolve(self, commit: str) -> str:
        handle = self._repository.handle
        try:
            node = handle.revparse_single(commit)
            return str(node.peel(pygit2.Commit).id)
        except KeyError:
            raise ValueError(
                f"Commit {commit!r} does not exist. Pass one of these two SHAs "
                f"exactly: {self._commits()}."
            )

    def _kind(self, commit: str) -> str:
        if commit in (self._defect["after"], self._defect["base"]):
            return "defect"
        if commit in (self._correction["after"], self._correction["base"]):
            return "correction"
        raise ValueError(
            f"Commit {commit[:12]} is neither change. Pass one of these two SHAs "
            f"exactly: {self._commits()}."
        )

    def show(
        self,
        commit: str,
        file: str | None = None,
        stat: bool = False,
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        """Show what one commit changed, as a diff whose lines carry references.

        `commit` is one of the two SHAs from your initial message: the defective
        change or the corrective change. With `file`, returns the unified diff of
        that file with three lines of surrounding context. For the defective change
        every line carries a reference: ``L<n>`` for a present line (context or
        addition) and ``D<n>`` for a deleted line — deleted lines are real defect
        locations, so use their ``D`` reference to report them.

        For a large diff, pass `start`/`end` (``L`` line numbers, the after side) to
        focus on that window; the ``D`` deletions inside it are shown too. Passing only
        `start` shows from there onward. An out-of-range bound returns an error naming
        the valid range.

        With `stat` (or no `file`), returns only the list of changed files with their
        added/deleted line counts, so you can see what the change touches first.
        """
        commit = self._resolve(commit)
        kind = self._kind(commit)
        if kind == "defect":
            base, after = self._defect["base"], self._defect["after"]
            numbering = Numbering.SIGNED
        else:
            base, after = self._correction["base"], self._correction["after"]
            numbering = Numbering.NONE

        diff = self._repository.diff(base, after, context=3)

        if stat or file is None:
            return {"commit": commit[:12], "files": DiffRenderer().stat(diff)}

        rendered = DiffRenderer().render(diff, file)
        if not rendered.strip():
            return {
                "file": file,
                "patch": "",
                "note": f"{file!r} was not modified by this commit.",
            }
        patch = PatchView(rendered, numbering=numbering).render()
        if start is not None or end is not None:
            patch = self._focus(patch, file, start, end)
        return {"file": file, "patch": patch}

    def _focus(
        self, patch: str, file: str, start: int | None, end: int | None
    ) -> str:
        """Restrict a SIGNED-rendered patch to an after-line (``L``) window.

        Bounds are ``L`` line numbers. Rows within the window are kept; a ``D``
        deletion is kept while the surrounding ``L`` rows are inside the window, so a
        deletion never drops out of its context. File and hunk headers are always
        kept. An out-of-range bound is rejected with the valid range rather than
        clamped, mirroring the blame tool.
        """
        rows = patch.splitlines()
        after_numbers = [
            value
            for row in rows
            for axis, value in [self._row_reference(row)]
            if value is not None and axis == "L"
        ]
        if not after_numbers:
            return patch
        amin, amax = min(after_numbers), max(after_numbers)

        for bound in (start, end):
            if bound is not None and (bound < amin or bound > amax):
                raise ValueError(
                    f"Invalid line window {start}-{end} for {file!r}: the after-side "
                    f"lines run {amin}-{amax}."
                )
        if start is not None and end is not None and end < start:
            raise ValueError(
                f"Invalid line window {start}-{end} for {file!r}: end precedes start."
            )

        kept: list[str] = []
        active = start is None  # open start -> included until we pass end
        for row in rows:
            axis, value = self._row_reference(row)
            if value is None:  # file/hunk header
                kept.append(row)
                continue
            if axis == "L":
                inside = (start is None or value >= start) and (
                    end is None or value <= end
                )
                active = inside
                if inside:
                    kept.append(row)
            elif active:
                # A D deletion inside the current L window keeps its context.
                kept.append(row)
        return "\n".join(kept)

    def _row_reference(self, row: str) -> tuple[str | None, int | None]:
        # Parse the leading L<n>/D<n> reference of a SIGNED patch row, if any.
        token = row.split("|", 1)[0].strip()
        if len(token) >= 2 and token[0] in "LD" and token[1:].isdigit():
            return token[0], int(token[1:])
        return None, None

    def grep(
        self,
        needle: str,
        commit: str | None = None,
        path: str | None = None,
        max: int = 50,
    ) -> list[dict[str, Any]]:
        """Search the files of a commit snapshot with a regex.

        `commit`, when given, is one of the two SHAs from your initial message: the
        defective change or the corrective change. Returns matching lines with their
        ``L<n>`` reference across that commit's snapshot, restricted to `path` when
        given. Defaults to the defective change. Like ``git grep``, this searches a
        snapshot, so it never matches a deleted line — to inspect a deletion, open the
        surrounding window with ``show`` and report its ``D`` reference.
        """
        if not needle:
            raise ValueError("grep needle must not be empty.")
        if max < 1:
            raise ValueError("grep max must be a positive integer.")
        try:
            pattern = re.compile(needle)
        except re.error as error:
            raise ValueError(f"Invalid grep regex: {error}") from error

        commit = self._resolve(commit or self._defect["after"])
        self._kind(commit)

        paths = self._repository.tree(commit)
        if path is not None:
            patterns = [path]
            if not any(token in path for token in "*?[]!"):
                prefix = path.rstrip("/")
                if prefix:
                    patterns.append(f"{prefix}/**")
            spec = PathSpec.from_lines("gitignore", patterns)
            paths = [current for current in paths if spec.match_file(current)]
            if not paths:
                raise ValueError(
                    f"The path {path!r} does not match any file at {commit[:12]}."
                )

        result: list[dict[str, Any]] = []
        total = 0
        for current in paths:
            try:
                data = self._repository.blob(commit, current)
            except (FileNotFoundError, ValueError, KeyError):
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


class LocateToolFactory:
    """Bind the locate tools to a fixed correction and inducing change."""

    def __init__(self, repository: VersionControl) -> None:
        self._repository = repository

    def create(self, correction: Change, defect: Change) -> LocateTool:
        return LocateTool(
            repository=self._repository,
            correction=correction,
            defect=defect,
        )
