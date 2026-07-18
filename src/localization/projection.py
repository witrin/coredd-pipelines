from __future__ import annotations

from dataclasses import dataclass

from tree_sitter_language_pack import get_parser

from benchmark.attribution.history import VersionControl


@dataclass(frozen=True)
class SyntaxNode:
    """A syntax node of N(c), identified within the base tree of a change.

    The identity is the span and type in the base file, so the same node reached
    from several marked lines deduplicates to one entry. The type is retained
    because it enters the audit statistics over N(c).
    """

    path: str
    start: tuple[int, int]
    end: tuple[int, int]
    type: str


class NodeProjection:
    """Maps marked lines of a change to the syntax nodes of N(c).

    N(c) is defined over the abstract syntax tree of the change's parent
    (\\autoref{section:measurement-framework}): a deletion or update references the
    affected parent node, an insertion the parent node at its position. The
    localization stage emits lines, so this projection supplies the deterministic
    line-to-node mapping that stage leaves implicit.

    A base line is already a parent-side coordinate. An after line is carried back
    through the diff by the line-level correspondence git reports: a context line
    maps to its recorded base line, while an inserted line has no base counterpart
    and instead resolves to the node the parent already had between the two base
    lines the insertion sits between -- their common enclosing node.
    """

    def __init__(self, repository: VersionControl) -> None:
        self._repository = repository
        self._trees: dict[tuple[str, str], object] = {}
        self._sources: dict[tuple[str, str], tuple[str, ...]] = {}
        self._maps: dict[tuple[str, str], dict[str, dict[int, int]]] = {}
        self._parser = get_parser("python")

    def project(self, commit: str, path: str, line: int, side: str) -> SyntaxNode | None:
        """Return the node in N(commit) a single marked line lifts to.

        None when the file is not Python, does not parse, or the line maps to no
        named node (a blank line or comment). The caller counts these separately.
        """
        if not path.endswith(".py"):
            return None

        base = self._base(commit)

        # A base line is already a parent-side coordinate. An after line anchors in
        # the parent tree when the file existed there; when the change adds the file
        # its parent has no such node, so the change's own tree carries it.
        if side == "base" and base is not None:
            return self._lift(base, path, (line, line))

        if base is not None and self._blob(base, path) is not None:
            span = self._rebase(base, commit, path, line)
            return self._lift(base, path, span) if span is not None else None

        return self._lift(commit, path, (line, line))

    def _base(self, commit: str) -> str | None:
        parents = self._repository.parents(commit)
        return parents[0] if parents else None

    def _rebase(
        self, base: str, commit: str, path: str, line: int
    ) -> tuple[int, int] | None:
        """The base-side span an after-side marking resolves to.

        A context line carries its recorded base line, returned as a one-line span.
        An inserted line has no base line, so it returns the span between the two
        base lines it sits between, whose common enclosing node is the parent it
        was inserted into. An insertion before or after all context falls back to
        the file bounds.
        """
        correspondence = self._correspondence(base, commit, path)
        if line in correspondence:
            base_line = correspondence[line]
            return (base_line, base_line)

        before = [after for after in correspondence if after < line]
        after = [other for other in correspondence if other > line]
        lower = correspondence[max(before)] if before else 1
        upper = correspondence[min(after)] if after else len(self._source(base, path))
        return (lower, upper)

    def _correspondence(self, base: str, commit: str, path: str) -> dict[int, int]:
        """The after-line to base-line map git reports for the context lines of a
        file, the lines present unchanged on both sides."""
        key = (base, commit)
        if key not in self._maps:
            files: dict[str, dict[int, int]] = {}
            for patch in self._repository.diff(base, commit, context=1):
                target = patch.delta.new_file.path
                lines = files.setdefault(target, {})
                for hunk in patch.hunks:
                    for entry in hunk.lines:
                        if entry.origin == " ":
                            lines[entry.new_lineno] = entry.old_lineno
            self._maps[key] = files
        return self._maps[key].get(path, {})

    def _lift(
        self, commit: str, path: str, span: tuple[int, int]
    ) -> SyntaxNode | None:
        root = self._tree(commit, path)
        lines = self._source(commit, path)
        lower, upper = span
        if root is None or lower <= 0 or upper > len(lines) or lower > upper:
            return None
        if lower == upper and lines[lower - 1].strip() == "":
            return None

        column = len(lines[lower - 1]) - len(lines[lower - 1].lstrip())
        if lower == upper:
            # A single line: descend to the innermost named node on it, then lift
            # one level to the node that owns it.
            node = root.descendant_for_point_range(
                (lower - 1, column), (lower - 1, column)
            )
            while node is not None and not getattr(node, "is_named", False):
                node = node.parent
            owner = node.parent if node is not None and node.parent is not None else node
        else:
            # A span between two base lines: the smallest node covering it already
            # is the common enclosing node, so it is taken as is.
            owner = root.descendant_for_point_range((lower - 1, column), (upper - 1, 0))
            while owner is not None and not getattr(owner, "is_named", False):
                owner = owner.parent
        if owner is None:
            return None

        return SyntaxNode(
            path=path,
            start=(owner.start_point[0] + 1, owner.start_point[1]),
            end=(owner.end_point[0] + 1, owner.end_point[1]),
            type=str(owner.type),
        )

    def _tree(self, commit: str, path: str) -> object | None:
        key = (commit, path)
        if key not in self._trees:
            source = self._blob(commit, path)
            if source is None:
                self._trees[key] = None
                self._sources[key] = ()
            else:
                self._trees[key] = self._parser.parse(source).root_node
                self._sources[key] = tuple(source.decode("utf-8", "ignore").splitlines())
        return self._trees[key]

    def _source(self, commit: str, path: str) -> tuple[str, ...]:
        self._tree(commit, path)
        return self._sources.get((commit, path), ())

    def _blob(self, commit: str, path: str) -> bytes | None:
        try:
            return self._repository.blob(commit, path)
        except (KeyError, ValueError, FileNotFoundError):
            return None
