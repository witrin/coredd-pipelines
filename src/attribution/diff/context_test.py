import unittest
from textwrap import dedent

from benchmark.attribution.diff.context import PatchScope, ScopePath, Statement
from benchmark.attribution.domain import Anchor, FileOperation, OperationType

class TestScopePath(unittest.TestCase):
    def _bind(
        self,
        base: str | None,
        after: str | None,
        *,
        suffix: str = ".ts",
        language: str | None = None,
    ) -> ScopePath:
        path = f"file{suffix}"
        file = FileOperation(
            path if base is not None else None,
            path if after is not None else None,
            (
                OperationType.ADD
                if base is None
                else OperationType.DELETE if after is None else OperationType.EDIT
            ),
        )
        return PatchScope().bind(
            file,
            dedent(base).lstrip("\n").encode() if base is not None else None,
            dedent(after).lstrip("\n").encode() if after is not None else None,
            language=language,
        )

    def test_bind_language(self) -> None:
        scope = self._bind(
            """
            export class Foo {
              bar() {
                return 1;
              }
            }
            """,
            """
            export class Foo {
              bar() {
                return 2;
              }
            }
            """,
        )

        self.assertIsInstance(scope, ScopePath)
        self.assertEqual(scope.language, "typescript")

    def test_bind_override(self) -> None:
        scope = self._bind(
            """
            export class Foo {
              bar() {
                return 1;
              }
            }
            """,
            """
            export class Foo {
              bar() {
                return 2;
              }
            }
            """,
            suffix=".txt",
            language="typescript",
        )

        self.assertEqual(scope.language, "typescript")

    def test_bind_unsupported(self) -> None:
        with self.assertRaises(ValueError):
            self._bind("ok\n", "ok\n", suffix=".txt")

    def test_bind_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self._bind(
                """
                export class Foo {
                  bar( {
                """,
                """
                export class Foo {
                  bar() {
                    return 1;
                  }
                }
                """,
            )

    def test_resolve_import(self) -> None:
        scope = self._bind(
            """
            import { TimeoutError } from "@opentelemetry/sdk-metrics";
            export class Foo {
              bar() {
                return 1;
              }
            }
            """,
            """
            import { AwsWrapperError } from "../../../utils/errors";
            export class Foo {
              bar() {
                return 1;
              }
            }
            """,
        )

        anchor = scope.resolve(Anchor(base=(1, 2), after=(1, 2)))

        self.assertIsInstance(anchor[0], Statement)
        self.assertIsInstance(anchor[1], Statement)
        self.assertEqual(
            anchor,
            (
                Statement(
                    scope="scope:program",
                    excerpt='import { TimeoutError } from "@opentelemetry/sdk-metrics";',
                ),
                Statement(
                    scope="scope:program",
                    excerpt='import { AwsWrapperError } from "../../../utils/errors";',
                ),
            ),
        )

    def test_resolve_throw(self) -> None:
        scope = self._bind(
            """
            export class SuspendExecuteRouting {
              async apply() {
                if (ready) {
                  throw new TimeoutError("boom");
                }
              }
            }
            """,
            """
            export class SuspendExecuteRouting {
              async apply() {
                if (ready) {
                  throw new AwsWrapperError("boom");
                }
              }
            }
            """,
        )

        anchor = scope.resolve(Anchor(base=(4, 5), after=(4, 5)))

        self.assertEqual(
            anchor,
            (
                Statement(
                    scope="scope:class=SuspendExecuteRouting:method=apply",
                    excerpt='throw new TimeoutError("boom");',
                ),
                Statement(
                    scope="scope:class=SuspendExecuteRouting:method=apply",
                    excerpt='throw new AwsWrapperError("boom");',
                ),
            ),
        )

    def test_resolve_add(self) -> None:
        scope = self._bind(
            None,
            """
            import { AwsWrapperError } from "../../../utils/errors";
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(0, 0), after=(1, 2))),
            (
                None,
                Statement(
                    scope="scope:program",
                    excerpt='import { AwsWrapperError } from "../../../utils/errors";',
                ),
            ),
        )

    def test_resolve_delete(self) -> None:
        scope = self._bind(
            """
            import { TimeoutError } from "@opentelemetry/sdk-metrics";
            """,
            None,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(1, 2), after=(0, 0))),
            (
                Statement(
                    scope="scope:program",
                    excerpt='import { TimeoutError } from "@opentelemetry/sdk-metrics";',
                ),
                None,
            ),
        )

    def test_resolve_function(self) -> None:
        scope = self._bind(
            """
            function top() {
              return 1;
            }
            """,
            """
            function top() {
              return 2;
            }
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(1, 2), after=(1, 2))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt="function top() {",
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt="function top() {",
                ),
            ),
        )
        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt="return 1;",
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt="return 2;",
                ),
            ),
        )

    def test_resolve_nested_function(self) -> None:
        scope = self._bind(
            """
            function outer() {
              function inner() {
                return 1;
              }
              return inner();
            }
            """,
            """
            function outer() {
              function inner() {
                return 2;
              }
              return inner();
            }
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="function inner() {",
                ),
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="function inner() {",
                ),
            ),
        )
        self.assertEqual(
            scope.resolve(Anchor(base=(3, 4), after=(3, 4))),
            (
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="return 1;",
                ),
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="return 2;",
                ),
            ),
        )

    def test_resolve_arrow(self) -> None:
        scope = self._bind(
            """
            const top = () => {
              return 1;
            };
            """,
            """
            const top = () => {
              return 2;
            };
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(1, 2), after=(1, 2))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt="const top = () => {",
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt="const top = () => {",
                ),
            ),
        )
        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt="return 1;",
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt="return 2;",
                ),
            ),
        )

    def test_resolve_nested_arrow(self) -> None:
        scope = self._bind(
            """
            const outer = () => {
              const inner = () => {
                return 1;
              };
              return inner();
            };
            """,
            """
            const outer = () => {
              const inner = () => {
                return 2;
              };
              return inner();
            };
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="const inner = () => {",
                ),
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="const inner = () => {",
                ),
            ),
        )
        self.assertEqual(
            scope.resolve(Anchor(base=(3, 4), after=(3, 4))),
            (
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="return 1;",
                ),
                Statement(
                    scope="scope:function=outer:function=inner",
                    excerpt="return 2;",
                ),
            ),
        )

    def test_resolve_assignment(self) -> None:
        scope = self._bind(
            """
            value =
              foo(
                one,
                two,
              );
            """,
            """
            value =
              bar(
                one,
                two,
              );
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(1, 2), after=(1, 2))),
            (
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value =
                          foo(
                            one,
                            two,
                          );
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value =
                          bar(
                            one,
                            two,
                          );
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_augmented(self) -> None:
        scope = self._bind(
            """
            value +=
              foo(
                one,
                two,
              );
            """,
            """
            value +=
              bar(
                one,
                two,
              );
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(1, 2), after=(1, 2))),
            (
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value +=
                          foo(
                            one,
                            two,
                          );
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value +=
                          bar(
                            one,
                            two,
                          );
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_nested_assignment(self) -> None:
        scope = self._bind(
            """
            function top() {
              value =
                foo(
                  one,
                  two,
                );
            }
            """,
            """
            function top() {
              value =
                bar(
                  one,
                  two,
                );
            }
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value =
                            foo(
                              one,
                              two,
                            );
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value =
                            bar(
                              one,
                              two,
                            );
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_nested_augmented(self) -> None:
        scope = self._bind(
            """
            function top() {
              value +=
                foo(
                  one,
                  two,
                );
            }
            """,
            """
            function top() {
              value +=
                bar(
                  one,
                  two,
                );
            }
            """,
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value +=
                            foo(
                              one,
                              two,
                            );
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value +=
                            bar(
                              one,
                              two,
                            );
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_python_assignment(self) -> None:
        scope = self._bind(
            """
            value = (
                foo(
                    one,
                    two,
                )
            )
            """,
            """
            value = (
                bar(
                    one,
                    two,
                )
            )
            """,
            suffix=".py",
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(1, 2), after=(1, 2))),
            (
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value = (
                            foo(
                                one,
                                two,
                            )
                        )
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value = (
                            bar(
                                one,
                                two,
                            )
                        )
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_python_augmented(self) -> None:
        scope = self._bind(
            """
            value += (
                foo(
                    one,
                    two,
                )
            )
            """,
            """
            value += (
                bar(
                    one,
                    two,
                )
            )
            """,
            suffix=".py",
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(1, 2), after=(1, 2))),
            (
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value += (
                            foo(
                                one,
                                two,
                            )
                        )
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:program",
                    excerpt=dedent(
                        """
                        value += (
                            bar(
                                one,
                                two,
                            )
                        )
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_python_nested_assignment(self) -> None:
        scope = self._bind(
            """
            def top():
                value = (
                    foo(
                        one,
                        two,
                    )
                )
            """,
            """
            def top():
                value = (
                    bar(
                        one,
                        two,
                    )
                )
            """,
            suffix=".py",
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value = (
                                foo(
                                    one,
                                    two,
                                )
                            )
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value = (
                                bar(
                                    one,
                                    two,
                                )
                            )
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_python_nested_augmented(self) -> None:
        scope = self._bind(
            """
            def top():
                value += (
                    foo(
                        one,
                        two,
                    )
                )
            """,
            """
            def top():
                value += (
                    bar(
                        one,
                        two,
                    )
                )
            """,
            suffix=".py",
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(2, 3), after=(2, 3))),
            (
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value += (
                                foo(
                                    one,
                                    two,
                                )
                            )
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:function=top",
                    excerpt=dedent(
                        """
                        value += (
                                bar(
                                    one,
                                    two,
                                )
                            )
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_python_method_assignment(self) -> None:
        scope = self._bind(
            """
            class Foo:
                def bar(self):
                    value = (
                        foo(
                            one,
                            two,
                        )
                    )
            """,
            """
            class Foo:
                def bar(self):
                    value = (
                        bar(
                            one,
                            two,
                        )
                    )
            """,
            suffix=".py",
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(3, 4), after=(3, 4))),
            (
                Statement(
                    scope="scope:class=Foo:method=bar",
                    excerpt=dedent(
                        """
                        value = (
                                    foo(
                                        one,
                                        two,
                                    )
                                )
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:class=Foo:method=bar",
                    excerpt=dedent(
                        """
                        value = (
                                    bar(
                                        one,
                                        two,
                                    )
                                )
                        """
                    ).strip(),
                ),
            ),
        )

    def test_resolve_python_method_augmented(self) -> None:
        scope = self._bind(
            """
            class Foo:
                def bar(self):
                    value += (
                        foo(
                            one,
                            two,
                        )
                    )
            """,
            """
            class Foo:
                def bar(self):
                    value += (
                        bar(
                            one,
                            two,
                        )
                    )
            """,
            suffix=".py",
        )

        self.assertEqual(
            scope.resolve(Anchor(base=(3, 4), after=(3, 4))),
            (
                Statement(
                    scope="scope:class=Foo:method=bar",
                    excerpt=dedent(
                        """
                        value += (
                                    foo(
                                        one,
                                        two,
                                    )
                                )
                        """
                    ).strip(),
                ),
                Statement(
                    scope="scope:class=Foo:method=bar",
                    excerpt=dedent(
                        """
                        value += (
                                    bar(
                                        one,
                                        two,
                                    )
                                )
                        """
                    ).strip(),
                ),
            ),
        )

