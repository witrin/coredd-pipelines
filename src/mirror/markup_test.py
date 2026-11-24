import unittest

from benchmark.mirror.markup import Linker, Markup, Sanitizer


class TestSanitizer(unittest.TestCase):
    def test_drops_script_and_handlers(self):
        html = Sanitizer().clean('<p onclick="x()">foo<script>alert(1)</script></p><img src="javascript:bar">')

        self.assertEqual(html, "<p>foo</p><img>")

    def test_keeps_allowed_markup(self):
        html = Sanitizer().clean('<details open><summary>foo</summary><a href="https://bar.baz">qux</a></details>')

        self.assertEqual(
            html,
            '<details open=""><summary>foo</summary><a href="https://bar.baz" rel="noopener noreferrer">qux</a></details>',
        )

    def test_closes_unbalanced_tags(self):
        self.assertEqual(Sanitizer().clean("<div><b>foo</div></div></main>"), "<div><b>foo</b></div>")


class TestLinker(unittest.TestCase):
    def setUp(self):
        self.linker = Linker(["Foo/bar"], self.resolve)

    def resolve(self, repo, sha):
        return None

    def test_rewrites_mirrored_repository(self):
        self.assertEqual(self.linker.href("https://github.com/foo/BAR/pull/12/files#diff-1"), "/Foo/bar/pull/12/files#diff-1")
        self.assertEqual(self.linker.href("https://github.com/Foo/bar/pulls/3"), "/Foo/bar/pull/3")
        self.assertEqual(self.linker.href("https://github.com/Foo/bar"), "/Foo/bar")

    def test_keeps_other_links(self):
        self.assertEqual(self.linker.href("https://github.com/baz/qux/pull/1"), "https://github.com/baz/qux/pull/1")
        self.assertEqual(self.linker.href("https://example.com/Foo/bar"), "https://example.com/Foo/bar")


class TestMarkup(unittest.TestCase):
    def setUp(self):
        self.commits = {("Foo/bar", "abcdef1"): "abcdef1" + "0" * 33}
        self.markup = Markup(Linker(["Foo/bar"], self.resolve))

    def resolve(self, repo, sha):
        return self.commits.get((repo, sha))

    def render(self, text):
        return self.markup.render(text, "Foo/bar")

    def test_links_issue_reference(self):
        self.assertIn('<a href="/Foo/bar/issues/12">#12</a>', self.render("see #12."))

    def test_links_known_commit_only(self):
        html = self.render("fixed in abcdef1 not in fedcba9")

        self.assertIn(f'<a href="/Foo/bar/commit/abcdef1{"0" * 33}">abcdef1</a>', html)
        self.assertNotIn("fedcba9</a>", html)

    def test_rewrites_bare_and_markdown_urls(self):
        html = self.render("https://github.com/Foo/bar/pull/3. and [x](https://github.com/Foo/bar/issues/4)")

        self.assertIn('<a href="/Foo/bar/pull/3">https://github.com/Foo/bar/pull/3</a>.', html)
        self.assertIn('<a href="/Foo/bar/issues/4">x</a>', html)

    def test_cross_repository_reference(self):
        html = self.render("baz/qux#5 and Foo/bar#6")

        self.assertIn('href="https://github.com/baz/qux/issues/5"', html)
        self.assertIn('<a href="/Foo/bar/issues/6">Foo/bar#6</a>', html)

    def test_leaves_code_alone(self):
        html = self.render("`#12` and\n\n```\n#13 abcdef1\n```")

        self.assertNotIn("/issues/12", html)
        self.assertNotIn("/issues/13", html)

    def test_escapes_raw_script(self):
        self.assertNotIn("<script", self.render("foo <script>alert(1)</script>"))

    def test_task_list(self):
        html = self.render("- [ ] foo\n- [x] bar")

        self.assertIn("☐ foo", html)
        self.assertIn("☑ bar", html)


if __name__ == "__main__":
    unittest.main()
