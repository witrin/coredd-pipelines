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
    corrective change for context. The inducing change renders with line numbers so
    the agent can reference its lines when reporting locations; the correction
    renders without line numbers, as read-only material.

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
        self, commit: str, file: str | None = None, stat: bool = False
    ) -> dict[str, Any]:
        """Show what one commit changed, as a diff with line numbers.

        `commit` is one of the two SHAs from your initial message: the defective
        change or the corrective change. With `file`, returns the unified diff of
        that file with three lines of surrounding context, each line prefixed with
        its absolute line number in the grep format ``L<n>:`` (deleted lines have no
        number on the after revision shown for the defective change). With `stat`
        (or no `file`), returns only the list of changed files with their
        added/deleted line counts, so you can see what the change touches first.
        """
        commit = self._resolve(commit)
        kind = self._kind(commit)
        if kind == "defect":
            base, after = self._defect["base"], self._defect["after"]
            numbering = Numbering.AFTER
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
        return {"file": file, "patch": patch}

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
        line number across that commit's snapshot, restricted to `path` when given.
        Defaults to the defective change.
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
