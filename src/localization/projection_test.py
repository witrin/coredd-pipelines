from __future__ import annotations

import unittest

from benchmark.localization.projection import NodeProjection


class FakeLine:
    def __init__(self, origin: str, old_lineno: int, new_lineno: int) -> None:
        self.origin = origin
        self.old_lineno = old_lineno
        self.new_lineno = new_lineno


class FakeHunk:
    def __init__(self, lines: list[FakeLine]) -> None:
        self.lines = lines


class FakePatch:
    def __init__(self, path: str, hunks: list[FakeHunk]) -> None:
        self.delta = type("Delta", (), {"new_file": type("File", (), {"path": path})()})()
        self.hunks = hunks


class FakeRepository:
    """A repository whose commits, blobs and diffs are fixed in the test.

    Commits are keyed by name; each maps to a parent and a tree of path to source
    bytes. Diffs are supplied per (base, commit) as the diff lines git reports,
    each carrying an origin and its base and after line numbers.
    """

    def __init__(self, commits, diffs) -> None:
        self._commits = commits
        self._diffs = diffs

    def parents(self, commit: str) -> list[str]:
        parent = self._commits[commit]["parent"]
        return [parent] if parent is not None else []

    def blob(self, commit: str, path: str) -> bytes:
        tree = self._commits[commit]["tree"]
        if path not in tree:
            raise FileNotFoundError(f"{path} not found in commit {commit}")
        return tree[path]

    def diff(self, base: str, commit: str, *, context: int = 1):
        return [
            FakePatch(path, [FakeHunk([FakeLine(*line) for line in lines])])
            for path, lines in self._diffs.get((base, commit), {}).items()
        ]


class TestNodeProjection(unittest.TestCase):
    def _module(self) -> bytes:
        return (
            b"import os\n"
            b"\n"
            b"\n"
            b"class Loader:\n"
            b"    def load(self, path):\n"
            b"        data = open(path).read()\n"
            b"        return data\n"
        )

    def _projection(self, diffs) -> NodeProjection:
        commits = {
            "child": {"parent": "base", "tree": {"a.py": self._module()}},
            "base": {"parent": None, "tree": {"a.py": self._module()}},
        }
        return NodeProjection(FakeRepository(commits, diffs))

    def test_base_line_lifts_to_parent(self) -> None:
        # Base line 6 (`data = open(path).read()`): the innermost named node is the
        # assignment target, one lift up reaches the assignment statement itself.
        projection = self._projection({})
        node = projection.project("child", "a.py", 6, "base")

        self.assertIsNotNone(node)
        self.assertEqual(node.type, "assignment")
        self.assertEqual(node.start[0], 6)

    def test_def_line_lifts_to_body_block(self) -> None:
        # The method signature line lifts one level to the surrounding body block.
        projection = self._projection({})
        node = projection.project("child", "a.py", 5, "base")

        self.assertIsNotNone(node)
        self.assertEqual(node.type, "block")

    def test_after_context_maps_onto_base(self) -> None:
        # A context line at after 6 records base 6, so it reaches the same node the
        # base coordinate does.
        diffs = {("base", "child"): {"a.py": [(" ", 6, 6)]}}
        projection = self._projection(diffs)
        node = projection.project("child", "a.py", 6, "after")

        self.assertIsNotNone(node)
        self.assertEqual(node.type, "assignment")
        self.assertEqual(node.start[0], 6)

    def test_after_insertion_lifts_to_common_parent(self) -> None:
        # An inserted after line (no base counterpart) resolves to the node spanning
        # the base lines it sits between: here inside the method body, the block.
        diffs = {
            ("base", "child"): {
                "a.py": [(" ", 6, 6), ("+", -1, 7), (" ", 7, 8)]
            }
        }
        projection = self._projection(diffs)
        node = projection.project("child", "a.py", 7, "after")

        self.assertIsNotNone(node)
        self.assertEqual(node.type, "block")

    def test_added_file_uses_own_tree(self) -> None:
        # The file is absent from the base tree, so an after marking lifts within
        # the change's own tree instead of the parent's.
        commits = {
            "child": {"parent": "base", "tree": {"a.py": self._module()}},
            "base": {"parent": None, "tree": {}},
        }
        projection = NodeProjection(FakeRepository(commits, {}))
        node = projection.project("child", "a.py", 6, "after")

        self.assertIsNotNone(node)
        self.assertEqual(node.type, "assignment")

    def test_non_python_is_unmappable(self) -> None:
        projection = self._projection({})

        self.assertIsNone(projection.project("child", "notes.md", 1, "after"))

    def test_blank_base_line_is_unmappable(self) -> None:
        # Base line 2 is blank and reached as a single line, so it maps to no node.
        projection = self._projection({})

        self.assertIsNone(projection.project("child", "a.py", 2, "base"))

    def test_same_node_deduplicates(self) -> None:
        # Two lookups of one line lift to the same node identity.
        projection = self._projection({})
        first = projection.project("child", "a.py", 6, "base")
        second = projection.project("child", "a.py", 6, "base")

        self.assertEqual(first, second)


class TestCommonParentAcrossNesting(unittest.TestCase):
    """The insertion span resolves to the innermost node enclosing both neighbours,
    which follows the nesting rather than always the module."""

    def _projection(self, base_bytes, after_bytes, diff_lines) -> NodeProjection:
        commits = {
            "child": {"parent": "base", "tree": {"a.py": after_bytes}},
            "base": {"parent": None, "tree": {"a.py": base_bytes}},
        }
        diffs = {("base", "child"): {"a.py": diff_lines}}
        return NodeProjection(FakeRepository(commits, diffs))

    def test_top_level_insertion_is_module(self) -> None:
        base = b"def a():\n    return 1\ndef b():\n    return 2\n"
        after = b"def a():\n    return 1\ndef c():\n    return 9\ndef b():\n    return 2\n"
        projection = self._projection(
            base, after, [(" ", 2, 2), ("+", -1, 3), ("+", -1, 4), (" ", 3, 5)]
        )
        node = projection.project("child", "a.py", 3, "after")

        self.assertIsNotNone(node)
        self.assertEqual(node.type, "module")

    def test_in_class_insertion_is_class_body(self) -> None:
        # A method inserted between two methods resolves to their common enclosing
        # node, the class body block, rather than the module.
        base = b"class C:\n    def a(self):\n        return 1\n    def b(self):\n        return 2\n"
        after = (
            b"class C:\n    def a(self):\n        return 1\n"
            b"    def c(self):\n        return 9\n    def b(self):\n        return 2\n"
        )
        projection = self._projection(
            base, after, [(" ", 3, 3), ("+", -1, 4), ("+", -1, 5), (" ", 4, 6)]
        )
        node = projection.project("child", "a.py", 4, "after")

        self.assertIsNotNone(node)
        self.assertEqual(node.type, "block")
        self.assertEqual(node.start[0], 2)


if __name__ == "__main__":
    unittest.main()
