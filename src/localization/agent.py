from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool, tool
from pathspec import PathSpec

from benchmark.attribution.diff.reference import PatchFactory, PatchView
from benchmark.attribution.domain import Change, FileOperation
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
        self._factory = PatchFactory(repository)

    def tools(self) -> list[BaseTool]:
        return [
            tool(self.show),
            tool(self.grep),
        ]

    def _kind(self, commit: str) -> str:
        if commit in (self._defect["after"], self._defect["base"]):
            return "defect"
        if commit in (self._correction["after"], self._correction["base"]):
            return "correction"
        raise ValueError(
            "show and grep operate only on the inducing change "
            f"({self._defect['after'][:12]}) or the correction "
            f"({self._correction['after'][:12]})."
        )

    def show(self, commit: str, file: str) -> dict[str, Any]:
        """Show the regions one commit changed in one file, with surrounding code.

        Returns the changed regions of `file` in `commit` expanded to their
        enclosing functions, not the whole file. Lines of the inducing change are
        prefixed with their line number; lines of the correction are not.
        """
        kind = self._kind(commit)
        if kind == "defect":
            base, after = self._defect["base"], self._defect["after"]
            numbered = True
        else:
            base, after = self._correction["base"], self._correction["after"]
            numbered = False

        operation = self._operation(base, after, file)
        if operation is None:
            return {
                "file": file,
                "patch": "",
                "note": f"{file!r} was not modified by this commit.",
            }

        expansion = str(
            self._factory.expand(base, after, files=[operation])
        )
        if not expansion.strip():
            return {"file": file, "patch": "", "note": "No changes to render."}

        # numbered=True renders `File:` + `<n> | <text>` so the agent can cite a
        # line; numbered=False renders `File:` + bare text only.
        patch = PatchView(expansion, numbered=numbered).after()
        return {"file": file, "patch": patch}

    def grep(
        self,
        needle: str,
        commit: str | None = None,
        path: str | None = None,
        max: int = 50,
    ) -> list[dict[str, Any]]:
        """Search the files of a commit snapshot with a regex.

        Returns matching lines with their line number, across the whole snapshot
        unless `path` restricts it. Defaults to the inducing change snapshot.
        """
        if not needle:
            raise ValueError("grep needle must not be empty.")
        try:
            pattern = re.compile(needle)
        except re.error as error:
            raise ValueError(f"Invalid grep regex: {error}") from error

        commit = commit or self._defect["after"]
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

    def _operation(
        self,
        base: str,
        after: str,
        file: str,
    ) -> FileOperation | None:
        for operation in self._repository.manifest(base, after):
            if file in (operation.after, operation.base):
                return operation
        return None


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
