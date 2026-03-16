from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from benchmark.attribution.domain import FileOperation, OperationType
from benchmark.attribution.diff.entity import EntityPath
from benchmark.attribution.history import VersionControl
from benchmark.attribution.history_fixture import HistoryFixture


class TestGumTree(unittest.TestCase):
    def _commit(
        self,
        fixture: HistoryFixture,
        before: dict[str, str],
        after: dict[str, str],
    ) -> tuple[str, str]:
        base = fixture.commit(fixture.tree(before), [], "base")
        after_sha = fixture.commit(fixture.tree(after), [base], "after")
        return base, after_sha

    def _pair(
        self,
        history: VersionControl,
        tmpdir: str,
        base: str,
        after: str,
        path: str,
    ) -> tuple[Path, Path]:
        root = Path(tmpdir) / after
        root.mkdir(parents=True, exist_ok=True)
        source = root / "base"
        target = root / "after"
        source.write_bytes(history.blob(base, path))
        target.write_bytes(history.blob(after, path))
        return source, target

    def test_python_nested_edit(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.py": """
                            class Outer:
                                first = 1
                                second = 2
                                third = 3

                                class Inner:
                                    def bar(self):
                                        total = 0
                                        for step in range(3):
                                            total += step
                                        if total > 1:
                                            note = total + 1
                                        else:
                                            note = total - 1
                                        while note < 20:
                                            note += 2
                                        return note

                                def foo(self):
                                    seed = self.first + self.second
                                    def baz(flag):
                                        current = seed
                                        if flag:
                                            current = current + 10
                                        else:
                                            current = current - 5
                                        while current < 100:
                                            current += 4
                                        return current
                                    return baz(True) + self.third
                            """,
                        "b.py": """
                            def qux():
                                return 1
                            """,
                    },
                    {
                        "a.py": """
                            class Outer:
                                first = 1
                                second = 20
                                third = 3

                                class Inner:
                                    def bar(self):
                                        total = 0
                                        for step in range(3):
                                            total += step
                                        if total > 1:
                                            note = total + 5
                                        else:
                                            note = total - 1
                                        while note < 20:
                                            note += 2
                                        return note

                                def foo(self):
                                    seed = self.first + self.second
                                    def baz(flag):
                                        current = seed
                                        if flag:
                                            current = current + 10
                                        else:
                                            current = current - 5
                                        while current < 100:
                                            current += 9
                                        return current
                                    return baz(True) + self.third
                            """,
                        "b.py": """
                            def qux():
                                return 2
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.py")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.py",
                            after="a.py",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="python",
                    ).resolve(),
                    {
                        ("EDIT", "entity:class=Outer", "entity:class=Outer"),
                        (
                            "EDIT",
                            "entity:class=Outer:method=foo:function=baz",
                            "entity:class=Outer:method=foo:function=baz",
                        ),
                        (
                            "EDIT",
                            "entity:class=Outer:class=Inner:method=bar",
                            "entity:class=Outer:class=Inner:method=bar",
                        ),
                    },
                )

    def test_python_nested_simple(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.py": """
                            class Outer:
                                first = 1
                                second = 2
                                third = 3

                                class Inner:
                                    def bar(self):
                                        total = 0
                                        return total + 1

                                def foo(self):
                                    seed = self.first + self.second
                                    def baz(flag):
                                        current = seed
                                        if flag:
                                            current = current + 10
                                        return current
                                    return baz(True) + self.third
                            """,
                    },
                    {
                        "a.py": """
                            class Outer:
                                first = 1
                                second = 20
                                third = 3

                                class Inner:
                                    def bar(self):
                                        total = 0
                                        return total + 5

                                def foo(self):
                                    seed = self.first + self.second
                                    def baz(flag):
                                        current = seed
                                        if flag:
                                            current = current + 10
                                        return current + 1
                                    return baz(True) + self.third
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.py")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.py",
                            after="a.py",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="python",
                    ).resolve(),
                    {
                        ("EDIT", "entity:class=Outer", "entity:class=Outer"),
                        (
                            "EDIT",
                            "entity:class=Outer:method=foo:function=baz",
                            "entity:class=Outer:method=foo:function=baz",
                        ),
                        (
                            "EDIT",
                            "entity:class=Outer:class=Inner:method=bar",
                            "entity:class=Outer:class=Inner:method=bar",
                        ),
                    },
                )

    def test_typescript_nested_edit(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.ts": """
                            export class Outer {
                              first = 1;
                              second = 2;
                              third = 3;

                              foo(flag: boolean) {
                                const seed = this.first + this.second;
                                const baz = () => {
                                  let current = seed;
                                  if (flag) {
                                    current = current + 10;
                                  } else {
                                    current = current - 5;
                                  }
                                  while (current < 100) {
                                    current += 4;
                                  }
                                  return current;
                                };

                                class Inner {
                                  bar() {
                                    let total = 0;
                                    for (const step of [1, 2, 3]) {
                                      total += step;
                                    }
                                    if (total > 1) {
                                      total += 7;
                                    } else {
                                      total -= 1;
                                    }
                                    return total;
                                  }
                                }

                                return baz() + new Inner().bar() + this.third;
                              }
                            }
                            """,
                        "b.ts": """
                            export const qux = () => 1;
                            """,
                    },
                    {
                        "a.ts": """
                            export class Outer {
                              first = 1;
                              second = 20;
                              third = 3;

                              foo(flag: boolean) {
                                const seed = this.first + this.second;
                                const baz = () => {
                                  let current = seed;
                                  if (flag) {
                                    current = current + 10;
                                  } else {
                                    current = current - 5;
                                  }
                                  while (current < 100) {
                                    current += 9;
                                  }
                                  return current;
                                };

                                class Inner {
                                  bar() {
                                    let total = 0;
                                    for (const step of [1, 2, 3]) {
                                      total += step;
                                    }
                                    if (total > 1) {
                                      total += 12;
                                    } else {
                                      total -= 1;
                                    }
                                    return total;
                                  }
                                }

                                return baz() + new Inner().bar() + this.third;
                              }
                            }
                            """,
                        "b.ts": """
                            export const qux = () => 2;
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.ts")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.ts",
                            after="a.ts",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="typescript",
                    ).resolve(),
                    {
                        ("EDIT", "entity:class=Outer", "entity:class=Outer"),
                        (
                            "EDIT",
                            "entity:class=Outer:method=foo:function=baz",
                            "entity:class=Outer:method=foo:function=baz",
                        ),
                        (
                            "EDIT",
                            "entity:class=Outer:method=foo:class=Inner:method=bar",
                            "entity:class=Outer:method=foo:class=Inner:method=bar",
                        ),
                    },
                )

    def test_python_add(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.py": """
                            VALUE = 1
                            """,
                        "b.py": """
                            def qux():
                                return 1
                            """,
                    },
                    {
                        "a.py": """
                            import math

                            VALUE = 1

                            def top():
                                return math.ceil(VALUE)

                            class Left:
                                pass

                            class Right:
                                pass

                            if __name__ == "__main__":
                                print(top())
                            """,
                        "b.py": """
                            def qux():
                                return 2
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.py")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.py",
                            after="a.py",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="python",
                    ).resolve(),
                    {
                        ("ADD", None, "entity:function=top"),
                        ("ADD", None, "entity:class=Left"),
                        ("ADD", None, "entity:class=Right"),
                    },
                )

    def test_python_nested_add(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.py": """
                            class Outer:
                                def foo(self):
                                    return 1
                            """,
                    },
                    {
                        "a.py": """
                            class Outer:
                                def foo(self):
                                    return 1

                                def bar(self):
                                    return 2
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.py")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.py",
                            after="a.py",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="python",
                    ).resolve(),
                    {
                        ("ADD", None, "entity:class=Outer:method=bar"),
                    },
                )

    def test_python_add_class(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.py": """
                            VALUE = 1
                            """,
                    },
                    {
                        "a.py": """
                            VALUE = 1

                            class Outer:
                                def foo(self):
                                    return VALUE + 1
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.py")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.py",
                            after="a.py",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="python",
                    ).resolve(),
                    {
                        ("ADD", None, "entity:class=Outer"),
                        ("ADD", None, "entity:class=Outer:method=foo"),
                    },
                )

    def test_typescript_move(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.ts": """
                            export class Left {
                              bar() {
                                let total = 0;
                                for (const step of [1, 2, 3]) {
                                  total += step;
                                }
                                return total + 10;
                              }
                            }

                            export class Right {
                              foo() {
                                return 1;
                              }
                            }
                            """,
                        "b.ts": """
                            export const qux = () => 1;
                            """,
                    },
                    {
                        "a.ts": """
                            export class Left {
                              foo() {
                                return 1;
                              }
                            }

                            export class Right {
                              bar() {
                                let total = 0;
                                for (const step of [1, 2, 3]) {
                                  total += step;
                                }
                                return total + 10;
                              }
                            }
                            """,
                        "b.ts": """
                            export const qux = () => 2;
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.ts")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.ts",
                            after="a.ts",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="typescript",
                    ).resolve(),
                    {
                        (
                            "MOVE",
                            "entity:class=Left:method=bar",
                            "entity:class=Right:method=bar",
                        ),
                    },
                )

    def test_typescript_delete(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.ts": """
                            export function outer() {
                              function middle(flag: boolean) {
                                const baz = () => {
                                  let total = 0;
                                  if (flag) {
                                    total += 10;
                                  } else {
                                    total -= 2;
                                  }
                                  while (total < 50) {
                                    total += 5;
                                  }
                                  return total;
                                };
                                return baz();
                              }
                              return middle(true);
                            }
                            """,
                        "b.ts": """
                            export const qux = () => 1;
                            """,
                    },
                    {
                        "a.ts": """
                            export function outer() {
                              function middle(flag: boolean) {
                                return flag ? 1 : 0;
                              }
                              return middle(true);
                            }
                            """,
                        "b.ts": """
                            export const qux = () => 2;
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.ts")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.ts",
                            after="a.ts",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="typescript",
                    ).resolve(),
                    {
                        (
                            "DELETE",
                            "entity:function=outer:function=middle:function=baz",
                            None,
                        ),
                    },
                )

    def test_python_mixed_edit(self) -> None:
        with HistoryFixture() as fixture:
            history = VersionControl(fixture.repository)
            with tempfile.TemporaryDirectory() as tmpdir:
                base, after = self._commit(
                    fixture,
                    {
                        "a.py": """
                            VALUE = 1

                            class Foo:
                                def bar(self):
                                    total = VALUE + 2
                                    return total
                            """,
                        "b.py": """
                            def qux():
                                return 1
                            """,
                    },
                    {
                        "a.py": """
                            VALUE = 10

                            class Foo:
                                def bar(self):
                                    total = VALUE + 20
                                    return total
                            """,
                        "b.py": """
                            def qux():
                                return 2
                            """,
                    },
                )

                source, target = self._pair(history, tmpdir, base, after, "a.py")

                self.assertEqual(
                    EntityPath(
                        FileOperation(
                            base="a.py",
                            after="a.py",
                            operation=OperationType.EDIT,
                        ),
                        source.read_bytes(),
                        target.read_bytes(),
                        language="python",
                    ).resolve(),
                    {
                        ("EDIT", "entity:program", "entity:program"),
                        (
                            "EDIT",
                            "entity:class=Foo:method=bar",
                            "entity:class=Foo:method=bar",
                        ),
                    },
                )


if __name__ == "__main__":
    unittest.main()
