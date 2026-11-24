from dataclasses import dataclass, field
from html import escape
from pathlib import Path

import pygit2


@dataclass
class DiffLine:
    origin: str
    old: int | None
    new: int | None
    text: str
    html: str = ""


@dataclass
class DiffGap:
    """Unchanged new-side lines ``start``..``end`` hidden before a hunk or after the last one; old = new + ``shift``."""

    start: int
    end: int
    shift: int


@dataclass
class DiffHunk:
    header: str
    lines: list[DiffLine]
    gap: DiffGap | None = None


@dataclass
class DiffFile:
    path: str
    old: str | None
    status: str
    additions: int
    deletions: int
    binary: bool
    left: str | None = None
    right: str | None = None
    hunks: list[DiffHunk] = field(default_factory=list)
    deferred: bool = False
    tail: DiffGap | None = None


@dataclass
class TreeEntry:
    name: str
    path: str
    kind: str
    size: int | None


class Highlighter:
    """Turns source text into one HTML fragment per line using Pygments token classes."""

    def __init__(self, limit: int = 1_000_000) -> None:
        self._limit = limit
        self._lexers: dict[str, type | None] = {}

    def lines(self, text: str, path: str) -> list[str]:
        """Return the highlighted HTML of every line of ``text``; plain escaped lines past the size limit."""
        from pygments.lexers import get_lexer_for_filename
        from pygments.token import STANDARD_TYPES
        from pygments.util import ClassNotFound

        raw = text.split("\n")

        if text.endswith("\n"):
            raw.pop()

        if len(text) > self._limit:
            return [escape(line) for line in raw]

        name = path.rsplit("/", 1)[-1]
        key = "." + name.rsplit(".", 1)[-1] if "." in name else name

        # Lexer lookup scans the installed plugin entry points on every call, so the
        # lexer class is resolved once per file extension.
        if key not in self._lexers:
            try:
                self._lexers[key] = type(get_lexer_for_filename(name))
            except ClassNotFound:
                self._lexers[key] = None

        if self._lexers[key] is None:
            return [escape(line) for line in raw]

        lexer = self._lexers[key](stripnl=False, ensurenl=False)

        result: list[str] = []
        current: list[str] = []

        for kind, value in lexer.get_tokens(text):
            name = self._name(kind, STANDARD_TYPES)
            parts = value.split("\n")

            for index, part in enumerate(parts):
                if index:
                    result.append("".join(current))
                    current = []

                if part:
                    current.append(
                        f'<span class="{name}">{escape(part)}</span>' if name else escape(part)
                    )

        if current:
            result.append("".join(current))

        return (result + [""] * len(raw))[: len(raw)]

    def _name(self, kind, types: dict) -> str:
        while kind not in types:
            kind = kind.parent

        return types[kind]


class Source:
    """Read access to one bare repository of the mirror."""

    def __init__(self, path: Path) -> None:
        self._repository = pygit2.Repository(str(path))

    @property
    def handle(self) -> pygit2.Repository:
        return self._repository

    def commit(self, revision: str) -> pygit2.Commit | None:
        """Resolve a SHA (full or abbreviated), branch, tag or ``pull/<n>/head`` to a commit."""
        candidates = [revision, f"refs/heads/{revision}", f"refs/tags/{revision}", f"refs/{revision}"]

        for candidate in candidates:
            try:
                target = self._repository.revparse_single(candidate)
            except (KeyError, ValueError, pygit2.GitError):
                continue

            try:
                return target.peel(pygit2.Commit)
            except (pygit2.InvalidSpecError, ValueError):
                continue

        return None

    def split(self, spec: str) -> tuple[pygit2.Commit, str] | None:
        """Split ``<ref>/<path>`` where the ref itself may contain slashes, preferring the longest ref."""
        parts = spec.strip("/").split("/")

        for index in range(len(parts), 0, -1):
            commit = self.commit("/".join(parts[:index]))

            if commit is not None:
                return commit, "/".join(parts[index:])

        return None

    def kind(self, commit: pygit2.Commit, path: str) -> str | None:
        """Return ``tree`` or ``blob`` for ``path`` at ``commit``, or None when absent."""
        if not path:
            return "tree"

        try:
            entry = commit.tree[path]
        except KeyError:
            return None

        return "tree" if entry.type_str == "tree" else "blob"

    def tree(self, commit: pygit2.Commit, path: str) -> list[TreeEntry]:
        """List the entries of the directory ``path`` at ``commit``, directories first."""
        node = commit.tree if not path else self._repository[commit.tree[path].id]
        entries = [
            TreeEntry(
                name=item.name,
                path=f"{path}/{item.name}" if path else item.name,
                kind=item.type_str,
                size=self._repository[item.id].size if item.type_str == "blob" else None,
            )
            for item in node
        ]
        return sorted(entries, key=self._order)

    def _order(self, entry: TreeEntry) -> tuple[bool, str]:
        return entry.kind != "tree", entry.name.lower()

    def blob(self, commit: pygit2.Commit, path: str) -> bytes:
        return self._repository[commit.tree[path].id].data

    def readme(self, commit: pygit2.Commit, path: str) -> tuple[str, str] | None:
        """Return the path and text of the README in directory ``path``, if any."""
        for entry in self.tree(commit, path):
            if entry.kind == "blob" and entry.name.lower().split(".")[0] == "readme":
                return entry.path, self.blob(commit, entry.path).decode("utf-8", "replace")

        return None

    def log(
        self, commit: pygit2.Commit, path: str = "", skip: int = 0, limit: int = 50
    ) -> list[pygit2.Commit]:
        """Return first-parent-agnostic history of ``commit`` in time order, optionally filtered to ``path``."""
        walker = self._repository.walk(commit.id, pygit2.enums.SortMode.TIME)
        result: list[pygit2.Commit] = []
        seen = 0

        for item in walker:
            if path and not self._touches(item, path):
                continue

            if seen >= skip:
                result.append(item)

            seen += 1

            if len(result) >= limit:
                break

        return result

    def _touches(self, commit: pygit2.Commit, path: str) -> bool:
        current = self._entry(commit.tree, path)

        if not commit.parents:
            return current is not None

        return any(self._entry(parent.tree, path) != current for parent in commit.parents)

    def _entry(self, tree: pygit2.Tree, path: str):
        try:
            return tree[path].id
        except KeyError:
            return None

    def base(self, base: str, head: str) -> pygit2.Commit | None:
        """Return the merge base of two commits as GitHub uses it for a pull request diff."""
        first, second = self.commit(base), self.commit(head)

        if first is None or second is None:
            return None

        oid = self._repository.merge_base(first.id, second.id)
        return self._repository[oid] if oid else first

    def diff(self, old: pygit2.Commit | None, new: pygit2.Commit) -> list[DiffFile]:
        """Return the file diffs between ``old`` (or the empty tree) and ``new`` with renames detected."""
        if old is None:
            patch = new.tree.diff_to_tree(swap=True, context_lines=3)
        else:
            patch = self._repository.diff(old, new, context_lines=3)

        patch.find_similar()
        files: list[DiffFile] = []
        empty = "0" * 40

        for item in patch:
            delta = item.delta
            path = delta.new_file.path
            source = DiffFile(
                path=path,
                old=delta.old_file.path if delta.old_file.path != path else None,
                status={"A": "added", "D": "removed", "R": "renamed", "M": "modified"}.get(
                    delta.status_char(), "modified"
                ),
                additions=item.line_stats[1],
                deletions=item.line_stats[2],
                binary=delta.is_binary,
                left=str(delta.old_file.id) if str(delta.old_file.id) != empty else None,
                right=str(delta.new_file.id) if str(delta.new_file.id) != empty else None,
            )

            if not delta.is_binary:
                after = (1, 1)

                for hunk in item.hunks:
                    lines = []
                    first = (
                        hunk.old_start if hunk.old_lines else hunk.old_start + 1,
                        hunk.new_start if hunk.new_lines else hunk.new_start + 1,
                    )
                    gap = DiffGap(after[1], first[1] - 1, first[0] - first[1]) if first[1] > after[1] else None
                    after = (first[0] + hunk.old_lines, first[1] + hunk.new_lines)

                    for line in hunk.lines:
                        if line.origin not in (" ", "+", "-"):
                            continue

                        text = line.content.rstrip("\n").rstrip("\r")
                        lines.append(
                            DiffLine(
                                line.origin,
                                line.old_lineno if line.old_lineno > 0 else None,
                                line.new_lineno if line.new_lineno > 0 else None,
                                text,
                                escape(text),
                            )
                        )

                    source.hunks.append(DiffHunk(hunk.header.strip(), lines, gap))

                source.tail = DiffGap(after[1], 0, after[0] - after[1]) if source.hunks and source.right else None

            files.append(source)

        return files

    def context(self, commit: pygit2.Commit, path: str, gap: DiffGap, highlight: Highlighter) -> list[DiffLine]:
        """Return the unchanged lines of ``gap`` from ``path`` at ``commit`` as highlighted context lines."""
        lines = highlight.lines(self.blob(commit, path).decode("utf-8", "replace"), path)
        end = min(gap.end, len(lines))
        return [DiffLine(" ", number + gap.shift, number, "", lines[number - 1]) for number in range(max(1, gap.start), end + 1)]

    def paint(self, file: DiffFile, highlight: Highlighter) -> None:
        """Replace the escaped line text of ``file`` with syntax-highlighted HTML taken from the full blobs."""
        left = self._lines(file.left, file.old or file.path, highlight)
        right = self._lines(file.right, file.path, highlight)

        if file.tail is not None:
            file.tail = DiffGap(file.tail.start, len(right), file.tail.shift) if file.tail.start <= len(right) else None

        for hunk in file.hunks:
            for line in hunk.lines:
                if line.origin == "+" and line.new and line.new <= len(right):
                    line.html = right[line.new - 1]
                elif line.origin != "+" and line.old and line.old <= len(left):
                    line.html = left[line.old - 1]

    def _lines(self, oid: str | None, path: str, highlight: Highlighter) -> list[str]:
        if oid is None:
            return []

        return highlight.lines(self._repository[oid].data.decode("utf-8", "replace"), path)
