import random
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from benchmark.attribution.diff import Context, PatchContext, PatchFactory, PatchView
from benchmark.attribution.domain import Anchor, FileOperation, OperationType, Slice
from benchmark.attribution.history import VersionControl
from benchmark.attribution.history_fixture import HistoryFixture
from unidiff import PatchSet


class TestPatchView(unittest.TestCase):
    def test_render_aligns_references_with_separator(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/a.py b/a.py",
                    "--- a/a.py",
                    "+++ b/a.py",
                    "@@ -1,2 +1,2 @@",
                    "-old",
                    "+new",
                    " keep",
                ]
            )
        )

        lines = renderer.render().splitlines()

        self.assertIn(" | diff --git a/a.py b/a.py", lines[0])
        self.assertIn(" | --- a/a.py", lines[1])
        self.assertIn(" | +++ b/a.py", lines[2])
        self.assertIn(" | @@ -1,2 +1,2 @@", lines[3])
        self.assertIn("0-0-0 | -old", lines[4])
        self.assertIn("0-0-1 | +new", lines[5])
        self.assertIn("0-0-2 |  keep", lines[6])
        self.assertTrue(all(" | " in line for line in lines))

    def test_resolve_validates_reference_bounds(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/a.py b/a.py",
                    "--- a/a.py",
                    "+++ b/a.py",
                    "@@ -1 +1 @@",
                    "-old",
                    "+new",
                ]
            )
        )

        zero = renderer.resolve("0-0-0")
        one = renderer.resolve("0-0-1")
        self.assertIsNotNone(zero)
        self.assertIsNotNone(one)
        self.assertEqual((zero.file, zero.hunk, zero.line), (0, 0, 0))
        self.assertEqual((one.file, one.hunk, one.line), (0, 0, 1))
        self.assertIsNone(renderer.resolve("bad"))
        self.assertIsNone(renderer.resolve("1-0-0"))
        self.assertIsNone(renderer.resolve("0-1-0"))
        self.assertIsNone(renderer.resolve("0-0-9"))
        self.assertIsNone(renderer.resolve("-1-0-0"))

    def test_base_projects_plain_code_blocks(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/a.py b/a.py",
                    "--- a/a.py",
                    "+++ b/a.py",
                    "@@ -10,3 +10,3 @@",
                    " keep_before",
                    "-old",
                    " keep_after",
                    "+new",
                ]
            )
        )

        self.assertEqual(
            renderer.base(),
            "\n".join(
                [
                    "File: a.py",
                    "keep_before",
                    "old",
                    "keep_after",
                ]
            ),
        )

    def test_after_projects_plain_code_blocks(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/a.py b/a.py",
                    "--- a/a.py",
                    "+++ b/a.py",
                    "@@ -10,3 +10,3 @@",
                    " keep_before",
                    "-old",
                    " keep_after",
                    "+new",
                ]
            )
        )

        self.assertEqual(
            renderer.after(),
            "\n".join(
                [
                    "File: a.py",
                    "keep_before",
                    "keep_after",
                    "new",
                ]
            ),
        )

    def test_projection_uses_version_specific_paths_for_moves(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/old.py b/new.py",
                    "--- a/old.py",
                    "+++ b/new.py",
                    "@@ -1 +1 @@",
                    "-old",
                    "+new",
                ]
            )
        )

        self.assertIn("File: old.py", renderer.base())
        self.assertIn("File: new.py", renderer.after())

    def test_slices_groups_references_by_file(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/a.py b/a.py",
                    "--- a/a.py",
                    "+++ b/a.py",
                    "@@ -1,2 +1,2 @@",
                    "-old",
                    "+new",
                    " keep",
                ]
            )
        )

        self.assertEqual(
            renderer.resolve(["0-0-0", "0-0-1", "0-0-2"]),
            [
                Slice(
                    file=("a.py", "a.py"),
                    anchors=[
                        Anchor(base=(1, 2), after=(0, 0)),
                        Anchor(base=(0, 0), after=(1, 2)),
                        Anchor(base=(2, 3), after=(2, 3)),
                    ],
                )
            ],
        )

    def test_slices_support_add_only_reference(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/file.py b/file.py",
                    "new file mode 100644",
                    "--- /dev/null",
                    "+++ b/file.py",
                    "@@ -0,0 +1,2 @@",
                    "+new",
                    "+tail",
                ]
            )
        )

        self.assertEqual(
            renderer.resolve(["0-0-0"]),
            [
                Slice(
                    file=(None, "file.py"),
                    anchors=[Anchor(base=(0, 0), after=(1, 2))],
                )
            ],
        )

    def test_slices_support_delete_only_reference(self) -> None:
        renderer = PatchView(
            "\n".join(
                [
                    "diff --git a/file.py b/file.py",
                    "deleted file mode 100644",
                    "--- a/file.py",
                    "+++ /dev/null",
                    "@@ -1,2 +0,0 @@",
                    "-old",
                    "-tail",
                ]
            )
        )

        self.assertEqual(
            renderer.resolve(["0-0-0"]),
            [
                Slice(
                    file=("file.py", None),
                    anchors=[Anchor(base=(1, 2), after=(0, 0))],
                )
            ],
        )


class TestPatchFactory(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._root = Path(self._tmpdir.name)
        self._history_fixture = HistoryFixture()
        self._history_fixture.__enter__()
        self._repository = VersionControl(self._history_fixture.repository)
        self._candidate = PatchFactory(self._repository)

    def tearDown(self) -> None:
        self._history_fixture.__exit__(None, None, None)
        self._tmpdir.cleanup()

    def _patch(self, content: str) -> str:
        normalized = dedent(content).lstrip("\n")
        lines = normalized.splitlines(keepends=True)
        in_hunk = False
        result: list[str] = []
        for raw_line in lines:
            has_newline = raw_line.endswith("\n")
            line = raw_line[:-1] if has_newline else raw_line
            if line.startswith("@@ "):
                in_hunk = True
            if in_hunk and line == "":
                line = " "
            result.append(line + ("\n" if has_newline else ""))
        rendered: list[str] = []
        for index, raw_line in enumerate(result):
            line = raw_line.rstrip("\n")
            if (
                line.startswith("--- ")
                and index + 1 < len(result)
                and result[index + 1].rstrip("\n").startswith("+++ ")
            ):
                recent = [item.rstrip("\n") for item in rendered[-3:]]
                if not any(item.startswith("diff --git ") for item in recent):
                    rendered.append(
                        f"diff --git {line[4:]} {result[index + 1].rstrip()[4:]}\n"
                    )
            rendered.append(raw_line)
        return "".join(rendered)

    def _commit(
        self,
        before: str | None,
        after: str | None,
        suffix: str,
    ) -> tuple[str, str]:
        path = f"file{suffix}"
        before_files = {}
        after_files = {}
        if before is not None:
            before_files[path] = dedent(before).lstrip("\n")
        if after is not None:
            after_files[path] = dedent(after).lstrip("\n")

        base_tree = self._history_fixture.tree(before_files)
        base = self._history_fixture.commit(base_tree, [], "base")
        after_tree = self._history_fixture.tree(after_files)
        head = self._history_fixture.commit(after_tree, [base], "after")
        return base, head

    # == A. Baseline scoped EDIT (n=1) ================================

    def test_ts_method_edit_scoped(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Handles foo-related operations. */
            export class Foo {
              readonly baz: string = "qux";

              foo(): { bar: number } {
                return { bar: 1 };
              }

              /**
               * Computes bar value.
               */
              bar(): number {
                const qux = 2;
                return qux;
              }
            }

            export class Baz {
              private bar: number = 0;

              // returns static value
              baz(): string {
                return `foo-${this.bar}`;
              }

              qux<T>(foo: T): T {
                return foo;
              }
            }
        """,
            """
            /** Handles foo-related operations. */
            export class Foo {
              readonly baz: string = "qux";

              foo(): { bar: number } {
                return { bar: 99 };
              }

              /**
               * Computes bar value.
               */
              bar(): number {
                const qux = 2;
                return qux;
              }
            }

            export class Baz {
              private bar: number = 0;

              // returns static value
              baz(): string {
                return `foo-${this.bar}`;
              }

              qux<T>(foo: T): T {
                return foo;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -5,3 +5,3 @@
               foo(): { bar: number } {
            -    return { bar: 1 };
            +    return { bar: 99 };
               }
        """
            ),
        )

    def test_js_function_edit_scoped(self) -> None:
        base_sha, after_sha = self._commit(
            """
            import { baz } from "./baz";

            /**
             * Builds a foo result object.
             */
            function foo() {
              const bar = [1, 2, 3];
              return bar;
            }

            /* bar helper */
            function bar() {
              return { baz: 2 };
            }

            function qux() {
              throw new Error("not implemented");
            }
        """,
            """
            import { baz } from "./baz";

            /**
             * Builds a foo result object.
             */
            function foo() {
              const bar = [1, 2, 99];
              return bar;
            }

            /* bar helper */
            function bar() {
              return { baz: 2 };
            }

            function qux() {
              throw new Error("not implemented");
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -6,4 +6,4 @@
             function foo() {
            -  const bar = [1, 2, 3];
            +  const bar = [1, 2, 99];
               return bar;
             }
        """
            ),
        )

    def test_py_method_edit_scoped(self) -> None:
        base_sha, after_sha = self._commit(
            """
            class Foo:
                \"\"\"Handles foo-related operations.\"\"\"

                bar: int = 0

                def foo(self):
                    return {"baz": self.bar}

                def baz(self):
                    \"\"\"Computes baz value.\"\"\"
                    return [self.bar, self.bar + 1]

            # qux utilities
            class Baz:
                def __init__(self):
                    self.qux = 42

                @property
                def foo(self):
                    return self.qux

                def bar(self):
                    raise NotImplementedError
        """,
            """
            class Foo:
                \"\"\"Handles foo-related operations.\"\"\"

                bar: int = 0

                def foo(self):
                    return {"baz": self.bar, "qux": 99}

                def baz(self):
                    \"\"\"Computes baz value.\"\"\"
                    return [self.bar, self.bar + 1]

            # qux utilities
            class Baz:
                def __init__(self):
                    self.qux = 42

                @property
                def foo(self):
                    return self.qux

                def bar(self):
                    raise NotImplementedError
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -6,2 +6,2 @@
                 def foo(self):
            -        return {"baz": self.bar}
            +        return {"baz": self.bar, "qux": 99}
        """
            ),
        )

    # == A2. Local rules (n=1) ========================================

    def test_py_import_edit_stays_local(self) -> None:
        base_sha, after_sha = self._commit(
            """
            import os
            import sys

            def foo():
                return 1
        """,
            """
            import os
            import json

            def foo():
                return 1
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -2,1 +2,1 @@
            -import sys
            +import json
        """
            ),
        )

    def test_js_import_edit_stays_local(self) -> None:
        base_sha, after_sha = self._commit(
            """
            import { foo } from "./foo";

            function bar() {
              return 1;
            }
        """,
            """
            import { baz } from "./foo";

            function bar() {
              return 1;
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -1,1 +1,1 @@
            -import { foo } from "./foo";
            +import { baz } from "./foo";
        """
            ),
        )

    def test_ts_import_edit_stays_local(self) -> None:
        base_sha, after_sha = self._commit(
            """
            import { foo } from "./foo";

            export function bar(): number {
              return 1;
            }
        """,
            """
            import { baz } from "./foo";

            export function bar(): number {
              return 1;
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -1,1 +1,1 @@
            -import { foo } from "./foo";
            +import { baz } from "./foo";
        """
            ),
        )

    def test_ts_comment_edit_stays_local(self) -> None:
        base_sha, after_sha = self._commit(
            """
            export class Foo {
              foo(): number {
                // old comment
                return 1;
              }
            }
        """,
            """
            export class Foo {
              foo(): number {
                // new comment
                return 1;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -3,1 +3,1 @@
            -    // old comment
            +    // new comment
        """
            ),
        )

    # == B. Scope-level switch (n=2) ==================================

    def test_ts_context2_includes_class(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Handles foo-related operations. */
            export class Foo {
              readonly baz: string = "qux";

              foo(): { bar: number } {
                return { bar: 1 };
              }

              /**
               * Computes bar value.
               */
              bar(): number {
                const qux = 2;
                return qux;
              }
            }

            export class Baz {
              private bar: number = 0;

              // returns static value
              baz(): string {
                return `foo-${this.bar}`;
              }

              qux<T>(foo: T): T {
                return foo;
              }
            }
        """,
            """
            /** Handles foo-related operations. */
            export class Foo {
              readonly baz: string = "qux";

              foo(): { bar: number } {
                return { bar: 99 };
              }

              /**
               * Computes bar value.
               */
              bar(): number {
                const qux = 2;
                return qux;
              }
            }

            export class Baz {
              private bar: number = 0;

              // returns static value
              baz(): string {
                return `foo-${this.bar}`;
              }

              qux<T>(foo: T): T {
                return foo;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=2,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -2,15 +2,15 @@
             export class Foo {
               readonly baz: string = "qux";
             
               foo(): { bar: number } {
            -    return { bar: 1 };
            +    return { bar: 99 };
               }
             
               /**
                * Computes bar value.
                */
               bar(): number {
                 const qux = 2;
                 return qux;
               }
             }
        """
            ),
        )

    def test_py_context2_includes_class(self) -> None:
        base_sha, after_sha = self._commit(
            """
            class Foo:
                \"\"\"Handles foo-related operations.\"\"\"

                bar: int = 0

                def foo(self):
                    return {"baz": self.bar}

                def baz(self):
                    \"\"\"Computes baz value.\"\"\"
                    return [self.bar, self.bar + 1]

            # qux utilities
            class Baz:
                def __init__(self):
                    self.qux = 42

                @property
                def foo(self):
                    return self.qux

                def bar(self):
                    raise NotImplementedError
        """,
            """
            class Foo:
                \"\"\"Handles foo-related operations.\"\"\"

                bar: int = 0

                def foo(self):
                    return {"baz": self.bar, "qux": 99}

                def baz(self):
                    \"\"\"Computes baz value.\"\"\"
                    return [self.bar, self.bar + 1]

            # qux utilities
            class Baz:
                def __init__(self):
                    self.qux = 42

                @property
                def foo(self):
                    return self.qux

                def bar(self):
                    raise NotImplementedError
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=2,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                '''
            --- a/file.py
            +++ b/file.py
            @@ -1,11 +1,11 @@
             class Foo:
                 """Handles foo-related operations."""
             
                 bar: int = 0
             
                 def foo(self):
            -        return {"baz": self.bar}
            +        return {"baz": self.bar, "qux": 99}
             
                 def baz(self):
                     """Computes baz value."""
                     return [self.bar, self.bar + 1]
        '''
            ),
        )

    # == C. Multi-edit chunking (n=1) =================================

    def test_js_two_functions_edited(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /**
             * Builds foo array.
             */
            function foo() {
              const bar = [1, 2];
              return bar;
            }

            /* bar: no-op */
            function bar() {
              return null;
            }

            // baz computation
            function baz() {
              return { qux: 3 };
            }

            function qux() {
              throw new Error("not implemented");
            }
        """,
            """
            /**
             * Builds foo array.
             */
            function foo() {
              const bar = [1, 99];
              return bar;
            }

            /* bar: no-op */
            function bar() {
              return null;
            }

            // baz computation
            function baz() {
              return { qux: 99 };
            }

            function qux() {
              throw new Error("not implemented");
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -4,4 +4,4 @@
             function foo() {
            -  const bar = [1, 2];
            +  const bar = [1, 99];
               return bar;
             }
            @@ -15,3 +15,3 @@
             function baz() {
            -  return { qux: 3 };
            +  return { qux: 99 };
             }
        """
            ),
        )

    def test_ts_two_classes_edited(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Foo handler. */
            export class Foo {
              readonly baz: string = "qux";

              foo(): number {
                return 1;
              }
              bar(): string[] {
                return ["foo", "bar"];
              }
            }

            export class Bar {
              /** Computes baz. */
              baz(): number {
                return 3;
              }
              qux(): { foo: string } {
                return { foo: "bar" };
              }
            }

            // baz class
            export class Baz {
              foo(): number {
                return 5;
              }
              get bar(): number {
                return 6;
              }
            }
        """,
            """
            /** Foo handler. */
            export class Foo {
              readonly baz: string = "qux";

              foo(): number {
                return 99;
              }
              bar(): string[] {
                return ["foo", "bar"];
              }
            }

            export class Bar {
              /** Computes baz. */
              baz(): number {
                return 3;
              }
              qux(): { foo: string } {
                return { foo: "bar" };
              }
            }

            // baz class
            export class Baz {
              foo(): number {
                return 99;
              }
              get bar(): number {
                return 6;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -5,3 +5,3 @@
               foo(): number {
            -    return 1;
            +    return 99;
               }
            @@ -25,3 +25,3 @@
               foo(): number {
            -    return 5;
            +    return 99;
               }
        """
            ),
        )

    # == D. Partial ADD/DELETE within file (n=1, EDIT) =================

    # @unittest.skip("TODO: refine TypeScript insert anchors")
    def test_ts_add_method(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              foo(): number {
                return this.baz + 1;
              }
              /** Bar getter. */
              bar(): string {
                return "qux";
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              foo(): number {
                return this.baz + 1;
              }
              /** Bar getter. */
              bar(): string {
                return "qux";
              }
              qux(): { foo: number } {
                return { foo: 42 };
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -11,0 +12,3 @@
            +  qux(): { foo: number } {
            +    return { foo: 42 };
            +  }
        """
            ),
        )

    def test_ts_insert_hunk_header_uses_correct_base_line(self) -> None:
        base_sha, after_sha = self._commit(
            """
            export class Foo {
              bar(): string {
                return "qux";
              }
            }
        """,
            """
            export class Foo {
              bar(): string {
                return "qux";
              }
              qux(): { foo: number } {
                return { foo: 42 };
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        # Insert hunks use -N,0 where N is the line before insertion in base.
        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -4,0 +5,3 @@
            +  qux(): { foo: number } {
            +    return { foo: 42 };
            +  }
        """
            ),
        )

    # @unittest.skip("TODO: refine TypeScript delete anchors")
    def test_ts_delete_method(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              foo(): number {
                return this.baz + 1;
              }
              /** Bar getter. */
              bar(): string {
                return "qux";
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              foo(): number {
                return this.baz + 1;
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -8,4 +7,0 @@
            -  /** Bar getter. */
            -  bar(): string {
            -    return "qux";
            -  }
        """
            ),
        )

    def test_js_add_function(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /**
             * Builds foo result.
             */
            function foo() {
              return [1, 2, 3];
            }

            // baz: identity
            function baz(qux) {
              return qux;
            }
        """,
            """
            /**
             * Builds foo result.
             */
            function foo() {
              return [1, 2, 3];
            }

            /* bar: formats value */
            function bar() {
              return `foo-${42}`;
            }

            // baz: identity
            function baz(qux) {
              return qux;
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -7,0 +8,5 @@
            +/* bar: formats value */
            +function bar() {
            +  return `foo-${42}`;
            +}
            +
        """
            ),
        )

    def test_js_delete_function(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /**
             * Builds foo result.
             */
            function foo() {
              return [1, 2, 3];
            }

            /* bar: formats value */
            function bar() {
              return `foo-${42}`;
            }

            // baz: identity
            function baz(qux) {
              return qux;
            }
        """,
            """
            /**
             * Builds foo result.
             */
            function foo() {
              return [1, 2, 3];
            }

            // baz: identity
            function baz(qux) {
              return qux;
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -8,5 +7,0 @@
            -/* bar: formats value */
            -function bar() {
            -  return `foo-${42}`;
            -}
            -
        """
            ),
        )

    def test_py_add_function(self) -> None:
        base_sha, after_sha = self._commit(
            """
            def foo():
                \"\"\"Builds foo result.\"\"\"
                return [1, 2, 3]

            # baz: identity transform
            def baz(qux):
                return qux
        """,
            """
            def foo():
                \"\"\"Builds foo result.\"\"\"
                return [1, 2, 3]

            def bar():
                return f"foo-{42}"

            # baz: identity transform
            def baz(qux):
                return qux
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -4,0 +5,3 @@
            +def bar():
            +    return f"foo-{42}"
            +
        """
            ),
        )

    def test_py_delete_function(self) -> None:
        base_sha, after_sha = self._commit(
            """
            def foo():
                \"\"\"Builds foo result.\"\"\"
                return [1, 2, 3]

            def bar():
                return f"foo-{42}"

            # baz: identity transform
            def baz(qux):
                return qux
        """,
            """
            def foo():
                \"\"\"Builds foo result.\"\"\"
                return [1, 2, 3]

            # baz: identity transform
            def baz(qux):
                return qux
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -5,3 +4,0 @@
            -def bar():
            -    return f"foo-{42}"
            -
        """
            ),
        )

    # == D2. Partial ADD/DELETE statements (n=1, EDIT) ================

    def test_ts_add_statement(self) -> None:
        base_sha, after_sha = self._commit(
            """
            export class Foo {
              foo(): number {
                const x = 1;
                return x;
              }
            }
        """,
            """
            export class Foo {
              foo(): number {
                const x = 1;
                const y = 2;
                return x;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -2,4 +2,5 @@
               foo(): number {
                 const x = 1;
            +    const y = 2;
                 return x;
               }
        """
            ),
        )

    def test_ts_delete_statement(self) -> None:
        base_sha, after_sha = self._commit(
            """
            export class Foo {
              foo(): number {
                const x = 1;
                return x;
              }
            }
        """,
            """
            export class Foo {
              foo(): number {
                return x;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -2,4 +2,3 @@
               foo(): number {
            -    const x = 1;
                 return x;
               }
        """
            ),
        )

    def test_js_add_statement(self) -> None:
        base_sha, after_sha = self._commit(
            """
            function foo() {
              const x = 1;
              return x;
            }
        """,
            """
            function foo() {
              const x = 1;
              const y = 2;
              return x;
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -1,4 +1,5 @@
             function foo() {
               const x = 1;
            +  const y = 2;
               return x;
             }
        """
            ),
        )

    def test_js_delete_statement(self) -> None:
        base_sha, after_sha = self._commit(
            """
            function foo() {
              const x = 1;
              const y = 2;
              return x;
            }
        """,
            """
            function foo() {
              const x = 1;
              return x;
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -1,5 +1,4 @@
             function foo() {
               const x = 1;
            -  const y = 2;
               return x;
             }
        """
            ),
        )

    def test_py_add_statement(self) -> None:
        base_sha, after_sha = self._commit(
            """
            def foo():
                x = 1
                return x
        """,
            """
            def foo():
                x = 1
                y = 2
                return x
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -1,3 +1,4 @@
             def foo():
             x = 1
            +y = 2
             return x
        """
            ),
        )

    def test_py_delete_statement(self) -> None:
        base_sha, after_sha = self._commit(
            """
            def foo():
                x = 1
                y = 2
                return x
        """,
            """
            def foo():
                x = 1
                return x
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -1,4 +1,3 @@
             def foo():
             x = 1
            -y = 2
             return x
        """
            ),
        )

    # == E. Whole-file ADD/DELETE ============================================

    def test_whole_file_add(self) -> None:
        base_sha, after_sha = self._commit(
            None,
            """
            import { baz } from "./baz";

            /** Foo handler. */
            export class Foo {
              readonly bar: string = "qux";
              foo(): number {
                return 1;
              }
            }

            function qux(): string[] {
              return ["foo", "bar"];
            }

            const foo = { bar: 42 };
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- /dev/null
            +++ b/file.ts
            @@ -0,0 +1,15 @@
            +import { baz } from "./baz";
            +
            +/** Foo handler. */
            +export class Foo {
            +  readonly bar: string = "qux";
            +  foo(): number {
            +    return 1;
            +  }
            +}
            +
            +function qux(): string[] {
            +  return ["foo", "bar"];
            +}
            +
            +const foo = { bar: 42 };
        """
            ),
        )

    def test_whole_file_delete(self) -> None:
        base_sha, after_sha = self._commit(
            """
            import { baz } from "./baz";

            /** Foo handler. */
            export class Foo {
              readonly bar: string = "qux";
              foo(): number {
                return 1;
              }
            }

            function qux(): string[] {
              return ["foo", "bar"];
            }

            const foo = { bar: 42 };
        """,
            None,
            ".ts",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ /dev/null
            @@ -1,15 +0,0 @@
            -import { baz } from "./baz";
            -
            -/** Foo handler. */
            -export class Foo {
            -  readonly bar: string = "qux";
            -  foo(): number {
            -    return 1;
            -  }
            -}
            -
            -function qux(): string[] {
            -  return ["foo", "bar"];
            -}
            -
            -const foo = { bar: 42 };
        """
            ),
        )

    # == F. Pairing edge cases (n=1) ==================================

    def test_ts_rename_method(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              foo(): { bar: number } {
                return { bar: this.baz };
              }
              /**
               * Computes bar.
               */
              bar(): number {
                return 2;
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              qux(): { bar: number } {
                return { bar: this.baz };
              }
              /**
               * Computes bar.
               */
              bar(): number {
                return 2;
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            ".ts",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -5,3 +5,3 @@
            -  foo(): { bar: number } {
            +  qux(): { bar: number } {
                 return { bar: this.baz };
               }
        """
            ),
        )

    def test_ts_move_method_between_classes(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Foo handler. */
            export class Foo {
              foo(): number[] {
                return [1, 2];
              }
              bar(): string {
                return "baz";
              }
            }

            export class Baz {
              /** Computes baz. */
              baz(): number {
                return 3;
              }
              qux<T>(foo: T): T {
                return foo;
              }
            }
        """,
            """
            /** Foo handler. */
            export class Foo {
              bar(): string {
                return "baz";
              }
            }

            export class Baz {
              foo(): number[] {
                return [1, 2];
              }
              /** Computes baz. */
              baz(): number {
                return 3;
              }
              qux<T>(foo: T): T {
                return foo;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        # Move produces delete from Foo + insert into Baz: two hunks
        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -3,3 +2,0 @@
            -  foo(): number[] {
            -    return [1, 2];
            -  }
            @@ -11,0 +9,3 @@
            +  foo(): number[] {
            +    return [1, 2];
            +  }
        """
            ),
        )

    @unittest.skip(
        "Known limitation JS doc comment attaches to module scope and this rename hunk expands to module level"
    )
    def test_js_rename_function(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /**
             * Builds foo result.
             */
            function foo() {
              return { bar: 1 };
            }

            /* bar: no-op */
            function bar() {
              return null;
            }

            function baz(qux) {
              return qux;
            }
        """,
            """
            /**
             * Builds qux result.
             */
            function qux() {
              return { bar: 1 };
            }

            /* bar: no-op */
            function bar() {
              return null;
            }

            function baz(qux) {
              return qux;
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -1,6 +1,6 @@
            -/**
            - * Builds foo result.
            - */
            -function foo() {
            +/**
            + * Builds qux result.
            + */
            +function qux() {
               return { bar: 1 };
             }
        """
            ),
        )

    def test_py_rename_function(self) -> None:
        base_sha, after_sha = self._commit(
            """
            def foo():
                \"\"\"Builds foo result.\"\"\"
                return {"bar": 1}

            def bar():
                return None

            # baz utilities
            class Foo:
                def baz(self):
                    raise NotImplementedError
        """,
            """
            def qux():
                \"\"\"Builds qux result.\"\"\"
                return {"bar": 1}

            def bar():
                return None

            # baz utilities
            class Foo:
                def baz(self):
                    raise NotImplementedError
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -1,3 +1,3 @@
            -def foo():
            -    \"\"\"Builds foo result.\"\"\"
            +def qux():
            +    \"\"\"Builds qux result.\"\"\"
                 return {"bar": 1}
        """
            ),
        )

    # == G. Non-body changes (n=1) ====================================

    def test_py_decorator_change(self) -> None:
        base_sha, after_sha = self._commit(
            """
            class Foo:
                \"\"\"Foo handler.\"\"\"

                baz: int = 0

                @foo
                def bar(self):
                    return {"qux": self.baz}

                def qux(self):
                    raise NotImplementedError

            class Baz:
                @property
                def foo(self):
                    return 42
        """,
            """
            class Foo:
                \"\"\"Foo handler.\"\"\"

                baz: int = 0

                @bar
                def bar(self):
                    return {"qux": self.baz}

                def qux(self):
                    raise NotImplementedError

            class Baz:
                @property
                def foo(self):
                    return 42
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -6,3 +6,3 @@
            -    @foo
            +    @bar
                 def bar(self):
                     return {"qux": self.baz}
        """
            ),
        )

    def test_py_signature_change(self) -> None:
        base_sha, after_sha = self._commit(
            """
            def foo(x):
                \"\"\"Transforms x.\"\"\"
                return [x, x + 1]

            def bar():
                return {"baz": 42}

            # qux: identity
            def baz(qux):
                return qux
        """,
            """
            def foo(x, y):
                \"\"\"Transforms x.\"\"\"
                return [x, x + 1]

            def bar():
                return {"baz": 42}

            # qux: identity
            def baz(qux):
                return qux
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.py
            +++ b/file.py
            @@ -1,3 +1,3 @@
            -def foo(x):
            +def foo(x, y):
                 \"\"\"Transforms x.\"\"\"
                 return [x, x + 1]
        """
            ),
        )

    def test_ts_signature_change(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              foo(x: number): number {
                return x + this.baz;
              }

              /**
               * Returns bar string.
               */
              bar(): string {
                return `qux-${this.baz}`;
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            """
            /** Foo handler. */
            export class Foo {
              private baz: number = 0;

              foo(x: number, y: number): number {
                return x + this.baz;
              }

              /**
               * Returns bar string.
               */
              bar(): string {
                return `qux-${this.baz}`;
              }
            }

            export class Baz {
              qux(): boolean {
                return true;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -5,3 +5,3 @@
            -  foo(x: number): number {
            +  foo(x: number, y: number): number {
                 return x + this.baz;
               }
        """
            ),
        )

    # == H. Module-level statements (n=1) =============================

    def test_ts_const_edit(self) -> None:
        base_sha, after_sha = self._commit(
            """
            const foo: { bar: number } = { bar: 1 };

            /** Computes baz. */
            function baz(): string[] {
              return ["foo", "qux"];
            }

            export class Qux {
              bar(): number {
                return foo.bar;
              }
            }
        """,
            """
            const foo: { bar: number } = { bar: 99 };

            /** Computes baz. */
            function baz(): string[] {
              return ["foo", "qux"];
            }

            export class Qux {
              bar(): number {
                return foo.bar;
              }
            }
        """,
            ".ts",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.ts
            +++ b/file.ts
            @@ -1,12 +1,12 @@
            -const foo: { bar: number } = { bar: 1 };
            +const foo: { bar: number } = { bar: 99 };
             
             /** Computes baz. */
             function baz(): string[] {
               return ["foo", "qux"];
             }
             
             export class Qux {
               bar(): number {
                 return foo.bar;
               }
             }
        """
            ),
        )

    def test_js_const_edit(self) -> None:
        base_sha, after_sha = self._commit(
            """
            const foo = { bar: 1, baz: "qux" };

            /**
             * Computes bar.
             */
            function bar() {
              return [foo.bar, foo.baz];
            }

            /* baz: identity */
            function baz(qux) {
              return qux;
            }
        """,
            """
            const foo = { bar: 99, baz: "qux" };

            /**
             * Computes bar.
             */
            function bar() {
              return [foo.bar, foo.baz];
            }

            /* baz: identity */
            function baz(qux) {
              return qux;
            }
        """,
            ".js",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.js
            +++ b/file.js
            @@ -1,13 +1,13 @@
            -const foo = { bar: 1, baz: "qux" };
            +const foo = { bar: 99, baz: "qux" };
             
             /**
              * Computes bar.
              */
             function bar() {
               return [foo.bar, foo.baz];
             }
             
             /* baz: identity */
             function baz(qux) {
               return qux;
             }
        """
            ),
        )

    def test_py_assignment_edit(self) -> None:
        base_sha, after_sha = self._commit(
            """
            foo = {"bar": 1, "baz": "qux"}

            def bar():
                \"\"\"Computes bar.\"\"\"
                return [foo["bar"], foo["baz"]]

            # baz: identity
            def baz(qux):
                return qux
        """,
            """
            foo = {"bar": 99, "baz": "qux"}

            def bar():
                \"\"\"Computes bar.\"\"\"
                return [foo["bar"], foo["baz"]]

            # baz: identity
            def baz(qux):
                return qux
        """,
            ".py",
        )

        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                '''
            --- a/file.py
            +++ b/file.py
            @@ -1,9 +1,9 @@
            -foo = {"bar": 1, "baz": "qux"}
            +foo = {"bar": 99, "baz": "qux"}
             
             def bar():
                 """Computes bar."""
                 return [foo["bar"], foo["baz"]]
             
             # baz: identity
             def baz(qux):
                 return qux
        '''
            ),
        )

    # == I. Syntax / parser errors (n=1) ==============================

    def test_ts_syntax_error_raises(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /** Foo handler. */
            function foo(): { bar: number } {
              return { bar: 1 };
            }
        """,
            """
            /** Foo handler. */
            function foo( { bar: number } {
              return { bar: 99 };
            }
        """,
            ".ts",
        )
        with self.assertRaises(RuntimeError):
            self._candidate.expand(
                base=base_sha,
                after=after_sha,
                files=self._repository.manifest(base_sha, after_sha),
                level=1,
                n=0,
            )

    def test_py_syntax_error_raises(self) -> None:
        base_sha, after_sha = self._commit(
            """
            def foo():
                \"\"\"Builds foo result.\"\"\"
                return {"bar": 1}

            def baz(qux):
                return qux
        """,
            """
            def foo():
                \"\"\"Builds foo result.\"\"\"
                return {"bar": 99}
                    unexpected indent

            def baz(qux):
                return qux
        """,
            ".py",
        )
        with self.assertRaises(RuntimeError):
            self._candidate.expand(
                base=base_sha,
                after=after_sha,
                files=self._repository.manifest(base_sha, after_sha),
                level=1,
                n=0,
            )

    # == J. Whitespace-only / formatting (n=1) ========================

    def test_py_whitespace_only(self) -> None:
        base_sha, after_sha = self._commit(
            """
            class Foo:
                \"\"\"Foo handler.\"\"\"

                bar: int = 0

                def foo(self):
                    return {"baz": self.bar}

                def baz(self):
                    raise NotImplementedError

            def qux():
                return [1, 2, 3]
        """,
            """
            class Foo:
                \"\"\"Foo handler.\"\"\"

                bar: int = 0

                def foo(self):

                    return {"baz": self.bar}

                def baz(self):
                    raise NotImplementedError

            def qux():
                return [1, 2, 3]
        """,
            ".py",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        # GumTree AST ignores whitespace-only changes
        self.assertEqual(len(patch), 0)

    def test_js_whitespace_only(self) -> None:
        base_sha, after_sha = self._commit(
            """
            /**
             * Builds foo result.
             */
            function foo() {
              const bar = [1, 2];
              return bar;
            }

            /* baz: identity */
            function baz(qux) {
              return qux;
            }

            function qux() {
              throw new Error("not implemented");
            }
        """,
            """
            /**
             * Builds foo result.
             */
            function foo() {

              const bar = [1, 2];
              return bar;
            }

            /* baz: identity */
            function baz(qux) {
              return qux;
            }

            function qux() {
              throw new Error("not implemented");
            }
        """,
            ".js",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        # GumTree AST ignores whitespace-only changes
        self.assertEqual(len(patch), 0)

    # == K. Encoding edge (n=1) =======================================

    @unittest.skip("TODO: refine invalid UTF-8 semantic scope behavior")
    def test_invalid_utf8_bytes(self) -> None:
        pass

    # == Kept tests (unchanged) =============================================

    # -- Unsupported generators ---------------------------------------------

    def test_unsupported_file_type_produces_regular_diff(self) -> None:
        base_sha, after_sha = self._commit(
            """
            [section]
            key = 1
        """,
            """
            [section]
            key = 2
        """,
            ".toml",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )
        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.toml
            +++ b/file.toml
            @@ -2,1 +2,1 @@
            -key = 1
            +key = 2
        """
            ),
        )

    def test_unsupported_file_type_uses_requested_context_lines(self) -> None:
        base_sha, after_sha = self._commit(
            "foo = 1\nbar = 1\nbaz = 1\n",
            "foo = 1\nbar = 2\nbaz = 1\n",
            ".toml",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=1,
        )
        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.toml
            +++ b/file.toml
            @@ -1,3 +1,3 @@
             foo = 1
            -bar = 1
            +bar = 2
             baz = 1
        """
            ),
        )

    def test_unsupported_identical_files_produce_empty_patch(self) -> None:
        base_sha, after_sha = self._commit(
            """
            [section]
            key = 1
        """,
            """
            [section]
            key = 1
        """,
            ".toml",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )
        self.assertEqual(len(patch), 0)

    # -- Identical files ----------------------------------------------------

    def test_identical_ts_files_produce_empty_patch(self) -> None:
        base_sha, after_sha = self._commit(
            """
            export class Foo {
              foo(): number {
                return 1;
              }
            }
        """,
            """
            export class Foo {
              foo(): number {
                return 1;
              }
            }
        """,
            ".ts",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(len(patch), 0)

    # -- JSON ---------------------------------------------------------------

    @unittest.skip("JSON behavior decision pending Tree Sitter policy alignment")
    def test_json_edit_raises_without_generator(self) -> None:
        base_sha, after_sha = self._commit(
            '{\n  "a": 1,\n  "b": 2\n}\n',
            '{\n  "a": 99,\n  "b": 2\n}\n',
            ".json",
        )
        with self.assertRaises(RuntimeError):
            self._candidate.expand(
                base=base_sha,
                after=after_sha,
                files=self._repository.manifest(base_sha, after_sha),
                n=1,
            )

    # -- YAML ---------------------------------------------------------------

    def test_yaml_edit_produces_scoped_patch(self) -> None:
        base_sha, after_sha = self._commit(
            "key: value1\nother: same\n",
            "key: value2\nother: same\n",
            ".yaml",
        )
        patch = self._candidate.expand(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            level=1,
            n=0,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            --- a/file.yaml
            +++ b/file.yaml
            @@ -1,1 +1,1 @@
            -key: value1
            +key: value2
        """
            ),
        )

    def test_patch_uses_regular_context_lines(self) -> None:
        base_sha, after_sha = self._commit(
            "foo = 1\nbar = 1\nbaz = 1\n",
            "foo = 1\nbar = 2\nbaz = 1\n",
            ".toml",
        )

        patch = self._candidate.create(
            base=base_sha,
            after=after_sha,
            files=self._repository.manifest(base_sha, after_sha),
            n=1,
        )

        self.assertEqual(
            str(patch),
            self._patch(
                """
            diff --git a/file.toml b/file.toml
            index 8bf10fd..5ed1a49 100644
            --- a/file.toml
            +++ b/file.toml
            @@ -1,3 +1,3 @@
             foo = 1
            -bar = 1
            +bar = 2
             baz = 1
        """
            ),
        )

    def test_patch_returns_all_files_without_filter(self) -> None:
        base_tree = self._history_fixture.tree(
            {
                "edit.txt": "old\n",
                "old.txt": "same\n",
                "gone.txt": "gone\n",
            }
        )
        base_sha = self._history_fixture.commit(base_tree, [], "base")
        after_tree = self._history_fixture.tree(
            {
                "edit.txt": "new\n",
                "new.txt": "same\n",
                "add.txt": "add\n",
            }
        )
        after_sha = self._history_fixture.commit(after_tree, [base_sha], "after")

        patch = self._candidate.create(base_sha, after_sha, n=0)

        self.assertEqual(len(patch), 4)
        self.assertEqual(
            sorted(patched.path for patched in patch),
            ["add.txt", "edit.txt", "gone.txt", "new.txt"],
        )

    def test_patch_filters_manifest_entries_for_all_operation_types(self) -> None:
        base_tree = self._history_fixture.tree(
            {
                "edit.txt": "old\n",
                "old.txt": "same\n",
                "gone.txt": "gone\n",
            }
        )
        base_sha = self._history_fixture.commit(base_tree, [], "base")
        after_tree = self._history_fixture.tree(
            {
                "edit.txt": "new\n",
                "new.txt": "same\n",
                "add.txt": "add\n",
            }
        )
        after_sha = self._history_fixture.commit(after_tree, [base_sha], "after")
        selected = [
            FileOperation(base=None, after="add.txt", operation=OperationType.ADD),
            FileOperation(base="gone.txt", after=None, operation=OperationType.DELETE),
            FileOperation(
                base="old.txt",
                after="new.txt",
                operation=OperationType.MOVE,
            ),
        ]
        patch = self._candidate.create(base_sha, after_sha, files=selected, n=0)

        self.assertEqual(len(patch), 3)
        self.assertEqual(
            sorted(patched.path for patched in patch),
            ["add.txt", "gone.txt", "new.txt"],
        )

    def test_materialize_rejects_empty_slices(self) -> None:
        base_sha, after_sha = self._commit(
            "foo = 1\n",
            "foo = 2\n",
            ".toml",
        )

        with self.assertRaises(ValueError):
            self._candidate.refine(base_sha, after_sha, [], "base")

    def test_materialize_rejects_after_only_anchor_for_base_mode(self) -> None:
        base_sha, after_sha = self._commit(
            "foo = 1\n",
            "foo = 2\n",
            ".toml",
        )

        with self.assertRaises(ValueError):
            self._candidate.refine(
                base_sha,
                after_sha,
                [
                    Slice(
                        file=("file.toml", "file.toml"),
                        anchors=[Anchor(base=(0, 0), after=(1, 2))],
                    )
                ],
                "base",
            )

    def test_materialize_rejects_base_only_anchor_for_after_mode(self) -> None:
        base_sha, after_sha = self._commit(
            "foo = 1\n",
            "foo = 2\n",
            ".toml",
        )

        with self.assertRaises(ValueError):
            self._candidate.refine(
                base_sha,
                after_sha,
                [
                    Slice(
                        file=("file.toml", "file.toml"),
                        anchors=[Anchor(base=(1, 2), after=(0, 0))],
                    )
                ],
                "after",
            )

    def test_materialize_supports_context_anchor_on_both_sides(self) -> None:
        base_sha, after_sha = self._commit(
            """
            one = 1
            two = 1
            three = 1
            four = 1
            """,
            """
            one = 1
            two = 2
            three = 1
            four = 1
            """,
            ".toml",
        )
        slices = [
            Slice(
                file=("file.toml", "file.toml"),
                anchors=[Anchor(base=(3, 4), after=(3, 4))],
            )
        ]

        self.assertEqual(
            str(self._candidate.refine(base_sha, after_sha, slices, "base")),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,4 +1,4 @@
                 one = 1
                -two = 1
                +two = 2
                 three = 1
                 four = 1
                """
            ),
        )
        self.assertEqual(
            str(self._candidate.refine(base_sha, after_sha, slices, "after")),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,4 +1,4 @@
                 one = 1
                -two = 1
                +two = 2
                 three = 1
                 four = 1
                """
            ),
        )

    def test_materialize_clamps_base_window_to_file_start(self) -> None:
        base_sha, after_sha = self._commit(
            """
            keep_1 = 1
            keep_2 = 2
            old = 1
            tail = 1
            """
            ,
            """
            keep_1 = 1
            keep_2 = 2
            new = 1
            tail = 1
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[Anchor(base=(1, 2), after=(1, 2))],
                )
            ],
            "base",
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,3 +1,3 @@
                 keep_1 = 1
                 keep_2 = 2
                -old = 1
                +new = 1
                """
            ),
        )

    def test_materialize_clamps_after_window_to_file_end(self) -> None:
        base_sha, after_sha = self._commit(
            """
            top = 1
            old = 1
            keep = 1
            tail = 1
            """,
            """
            top = 1
            new = 1
            keep = 1
            tail = 1
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[Anchor(base=(4, 5), after=(4, 5))],
                )
            ],
            "after",
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -2,3 +2,3 @@
                -old = 1
                +new = 1
                 keep = 1
                 tail = 1
                """
            ),
        )

    def test_materialize_projects_paired_hunk_when_mapping_exists(self) -> None:
        base_sha, after_sha = self._commit(
            """
            one = 1
            old = 1
            three = 1
            four = 1
            """,
            """
            one = 1
            new = 1
            three = 1
            four = 1
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[Anchor(base=(3, 4), after=(3, 4))],
                )
            ],
            "after",
            force=False,
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,4 +1,4 @@
                 one = 1
                -old = 1
                +new = 1
                 three = 1
                 four = 1
                """
            ),
        )

    def test_materialize_projects_partial_mapping_in_after_mode(self) -> None:
        base_sha, after_sha = self._commit(
            """
            one = 1
            two = 2
            three = 3
            """,
            """
            one = 1
            two = 2
            insert = 9
            three = 3
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[Anchor(base=(0, 0), after=(3, 4))],
                )
            ],
            "after",
            force=False,
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,3 +1,4 @@
                 one = 1
                 two = 2
                +insert = 9
                 three = 3
                """
            ),
        )

    def test_materialize_leaves_opposite_side_empty_when_nothing_maps(self) -> None:
        base_sha, after_sha = self._commit(
            None,
            """
            one = 1
            two = 2
            three = 3
            four = 4
            five = 5
            six = 6
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=(None, "file.toml"),
                    anchors=[Anchor(base=(0, 0), after=(4, 5))],
                )
            ],
            "after",
            force=False,
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- /dev/null
                +++ b/file.toml
                @@ -0,0 +2,5 @@
                +two = 2
                +three = 3
                +four = 4
                +five = 5
                +six = 6
                """
            ),
        )

    def test_materialize_repairs_boundaries_when_forced(self) -> None:
        base_sha, after_sha = self._commit(
            """
            keep = 1
            old = 1
            tail = 1
            """,
            """
            keep = 1
            new = 1
            tail = 1
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[Anchor(base=(2, 3), after=(0, 0))],
                )
            ],
            "base",
            n=0,
            force=True,
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,3 +1,3 @@
                 keep = 1
                -old = 1
                +new = 1
                 tail = 1
                """
            ),
        )

    def test_materialize_omits_file_when_forced_boundaries_cannot_map(self) -> None:
        base_sha, after_sha = self._commit(
            """
            old_1 = 1
            old_2 = 2
            """,
            """
            new_1 = 1
            new_2 = 2
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[Anchor(base=(1, 2), after=(0, 0))],
                )
            ],
            "base",
            n=0,
            force=True,
        )

        self.assertEqual(str(patchset), self._patch(""))

    def test_materialize_keeps_large_paired_window_for_far_context_anchors(self) -> None:
        base_sha, after_sha = self._commit(
            """
            keep_1 = 1
            old_alpha = 1
            keep_3 = 3
            keep_4 = 4
            keep_5 = 5
            keep_6 = 6
            old_beta = 7
            keep_8 = 8
            """,
            """
            keep_1 = 1
            new_alpha = 1
            keep_3 = 3
            keep_4 = 4
            keep_5 = 5
            keep_6 = 6
            new_beta = 7
            keep_8 = 8
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[
                        Anchor(base=(1, 2), after=(1, 2)),
                        Anchor(base=(8, 9), after=(8, 9)),
                    ],
                )
            ],
            "base",
            force=True,
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,8 +1,8 @@
                 keep_1 = 1
                -old_alpha = 1
                +new_alpha = 1
                 keep_3 = 3
                 keep_4 = 4
                 keep_5 = 5
                 keep_6 = 6
                -old_beta = 7
                +new_beta = 7
                 keep_8 = 8
                """
            ),
        )

    def test_materialize_merges_multiple_anchors_in_one_file(self) -> None:
        base_sha, after_sha = self._commit(
            """
            one = 1
            two = 2
            three = 3
            four = 4
            five = 5
            six = 6
            seven = 7
            """,
            """
            one = 1
            two = 9
            three = 3
            four = 4
            five = 8
            six = 6
            seven = 7
            """,
            ".toml",
        )

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("file.toml", "file.toml"),
                    anchors=[
                        Anchor(base=(2, 3), after=(2, 3)),
                        Anchor(base=(6, 7), after=(6, 7)),
                    ],
                )
            ],
            "base",
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/file.toml
                +++ b/file.toml
                @@ -1,7 +1,7 @@
                 one = 1
                -two = 2
                +two = 9
                 three = 3
                 four = 4
                -five = 5
                +five = 8
                 six = 6
                 seven = 7
                """
            ),
        )

    def test_materialize_materializes_multiple_files_independently(self) -> None:
        base_tree = self._history_fixture.tree(
            {
                "a.toml": "one = 1\ntwo = 2\nthree = 3\n",
                "b.toml": "left = 1\nright = 2\nend = 3\n",
            }
        )
        base_sha = self._history_fixture.commit(base_tree, [], "base")
        after_tree = self._history_fixture.tree(
            {
                "a.toml": "one = 1\ntwo = 9\nthree = 3\n",
                "b.toml": "left = 1\nright = 8\nend = 3\n",
            }
        )
        after_sha = self._history_fixture.commit(after_tree, [base_sha], "after")

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=("a.toml", "a.toml"),
                    anchors=[Anchor(base=(1, 2), after=(1, 2))],
                ),
                Slice(
                    file=("b.toml", "b.toml"),
                    anchors=[Anchor(base=(3, 4), after=(3, 4))],
                ),
            ],
            "base",
        )

        self.assertEqual(
            str(patchset),
            self._patch(
                """
                --- a/a.toml
                +++ b/a.toml
                @@ -1,3 +1,3 @@
                 one = 1
                -two = 2
                +two = 9
                 three = 3
                --- a/b.toml
                +++ b/b.toml
                @@ -1,3 +1,3 @@
                 left = 1
                -right = 2
                +right = 8
                 end = 3
                """
            ),
        )

    def test_materialize_roundtrips_multiple_add_only_files(self) -> None:
        base_tree = self._history_fixture.tree({})
        base_sha = self._history_fixture.commit(base_tree, [], "base")
        after_tree = self._history_fixture.tree(
            {
                "a.ts": "alpha\n",
                "b.ts": "beta\n",
            }
        )
        after_sha = self._history_fixture.commit(after_tree, [base_sha], "after")

        patchset = self._candidate.refine(
            base_sha,
            after_sha,
            [
                Slice(
                    file=(None, "a.ts"),
                    anchors=[Anchor(base=(0, 0), after=(1, 2))],
                ),
                Slice(
                    file=(None, "b.ts"),
                    anchors=[Anchor(base=(0, 0), after=(1, 2))],
                ),
            ],
            "after",
        )

        rendered = str(patchset)

        self.assertIn("diff --git /dev/null b/a.ts", rendered)
        self.assertIn("diff --git /dev/null b/b.ts", rendered)
        self.assertEqual(
            PatchView(rendered).resolve(["0-0-0", "1-0-0"]),
            [
                Slice(file=(None, "a.ts"), anchors=[Anchor(base=(0, 0), after=(1, 2))]),
                Slice(file=(None, "b.ts"), anchors=[Anchor(base=(0, 0), after=(1, 2))]),
            ],
        )

    def test_random_composition_matches_pygit_unidiff(self) -> None:
        rng = random.Random()
        path = "one.txt"

        def render(lines: list[str]) -> str:
            return "".join(f"{line}\n" for line in lines)

        def normalize(parsed: PatchSet) -> str:
            parsed[0].patch_info = None
            for hunk in parsed[0]:
                hunk.section_header = ""
            return str(parsed)

        for iteration in range(1000):
            lines = [
                f"{path}-it{iteration:03d}-l{idx:03d}-b{rng.randrange(1000000)}"
                for idx in range(rng.randint(8, 14))
            ]
            mutated = list(lines)

            for operation_index in range(rng.randint(2, 8)):
                operation = rng.choice(("replace", "insert", "delete"))
                if operation == "delete" and len(mutated) == 1:
                    continue

                position = rng.randrange(
                    len(mutated) + (1 if operation == "insert" else 0)
                )
                token = (
                    f"{path}-it{iteration:03d}-op{operation_index:02d}-"
                    f"{rng.randrange(1000000)}"
                )

                if operation == "replace":
                    mutated[position] = f"{token}-r"
                elif operation == "insert":
                    mutated.insert(position, f"{token}-a")
                else:
                    mutated.pop(position)

            if mutated == lines:
                mutated[rng.randrange(len(mutated))] = (
                    f"{path}-it{iteration:03d}-fallback-r{rng.randrange(1000000)}"
                )

            before = render(lines)
            after = render(mutated)

            base = self._history_fixture.commit(
                self._history_fixture.tree({path: before}),
                [],
                f"base-{iteration}",
                ref=None,
            )
            head = self._history_fixture.commit(
                self._history_fixture.tree({path: after}),
                [base],
                f"after-{iteration}",
                ref=None,
            )

            operation = self._repository.manifest(base, head)[0]
            patch = list(self._repository.diff(base, head))[0]

            actual = str(
                PatchContext.project(
                    operation,
                    self._candidate.read(base, operation.base),
                    self._candidate.read(head, operation.after),
                    PatchContext.merge(
                        [
                            Context(
                                base=(
                                    (
                                        hunk.old_start + 1
                                        if hunk.old_lines == 0
                                        else hunk.old_start
                                    ),
                                    (
                                        hunk.old_start + 1
                                        if hunk.old_lines == 0
                                        else hunk.old_start
                                    )
                                    + hunk.old_lines,
                                ),
                                after=(
                                    (
                                        hunk.new_start + 1
                                        if hunk.new_lines == 0
                                        else hunk.new_start
                                    ),
                                    (
                                        hunk.new_start + 1
                                        if hunk.new_lines == 0
                                        else hunk.new_start
                                    )
                                    + hunk.new_lines,
                                ),
                                changes=[change],
                            )
                            for hunk in patch.hunks
                            for change in PatchContext.extract(hunk)
                        ]
                    ),
                )
            )

            self.assertEqual(
                normalize(PatchSet(actual)),
                normalize(PatchSet(patch.text)),
            )


if __name__ == "__main__":
    unittest.main()
