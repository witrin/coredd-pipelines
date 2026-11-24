import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

import pygit2
from starlette.testclient import TestClient

from benchmark.mirror.archive import MirrorSchema
from benchmark.mirror.code import DiffGap, Highlighter, Source
from benchmark.mirror.server import MirrorServer


class MirrorFixture:
    """Builds a minimal mirror root with one repository, one pull request and one issue."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def build(self) -> dict[str, str]:
        repository = pygit2.init_repository(str(self.root / "foo" / "bar.git"), bare=True)
        author = pygit2.Signature("foo", "foo@bar.baz")
        builder = repository.TreeBuilder()
        builder.insert("a.py", repository.create_blob(b"def foo():\n    return 1\n"), pygit2.GIT_FILEMODE_BLOB)
        builder.insert("README.md", repository.create_blob(b"# Bar\n\nSee #1.\n"), pygit2.GIT_FILEMODE_BLOB)
        base = repository.create_commit("refs/heads/main", author, author, "initial", builder.write(), [])
        builder = repository.TreeBuilder(repository[base].tree)
        builder.insert("a.py", repository.create_blob(b"def foo():\n    return 2\n"), pygit2.GIT_FILEMODE_BLOB)
        head = repository.create_commit("refs/heads/main", author, author, "change foo\n\nfixes #2", builder.write(), [base])
        repository.set_head("refs/heads/main")
        repository.references.create("refs/pull/1/head", head)

        connection = sqlite3.connect(self.root / "mirror.sqlite")
        MirrorSchema().create(connection)
        pull = {
            "number": 1, "title": "Change foo", "body": "Fixes #2 in https://github.com/foo/bar/blob/main/a.py#L2",
            "state": "closed", "merged_at": "2024-01-02T00:00:00Z", "closed_at": "2024-01-02T00:00:00Z",
            "created_at": "2024-01-01T00:00:00Z", "user": {"login": "baz"}, "merge_commit_sha": str(head),
            "base": {"sha": str(base), "ref": "main"}, "head": {"sha": str(head), "label": "baz:foo"},
            "commits": 1, "changed_files": 1, "comments": 1, "review_comments": 1, "labels": [],
            "assignees": [], "requested_reviewers": [], "html_url": "https://github.com/foo/bar/pull/1",
        }
        issue = {
            "number": 2, "title": "Foo returns 1", "body": "<script>x()</script>broken", "state": "closed",
            "closed_at": "2024-01-03T00:00:00Z", "created_at": "2023-12-31T00:00:00Z", "user": {"login": "qux"},
            "comments": 0, "labels": [], "assignees": [], "html_url": "https://github.com/foo/bar/issues/2",
        }
        records = [
            ("pr", 1, pull),
            ("issue", 2, issue),
            ("comment", 1, [{"id": 7, "user": {"login": "qux"}, "body": "LGTM", "created_at": "2024-01-01T01:00:00Z"}]),
            ("review", 1, [{"id": 8, "user": {"login": "qux"}, "body": "", "state": "COMMENTED", "submitted_at": "2024-01-01T02:00:00Z", "commit_id": str(head)}]),
            ("review_comment", 1, [{"id": 9, "pull_request_review_id": 8, "user": {"login": "qux"}, "body": "why 2?", "created_at": "2024-01-01T02:00:00Z", "path": "a.py", "line": 2, "position": 3, "commit_id": str(head), "diff_hunk": "@@ -1,2 +1,2 @@\n def foo():\n-    return 1\n+    return 2"}]),
            ("commit", 1, [{"sha": str(head), "commit": {"message": "change foo", "author": {"name": "foo", "date": "2024-01-01T00:30:00Z"}}, "author": {"login": "baz"}}]),
        ]

        for kind, number, payload in records:
            connection.execute(
                "INSERT INTO record VALUES (?, ?, ?, ?)",
                ("foo/bar", kind, number, zlib.compress(json.dumps(payload).encode())),
            )

        connection.execute("INSERT INTO entry VALUES ('foo/bar', 1, 'pull', 'Change foo', 'merged', 'baz', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z', 1)")
        connection.execute("INSERT INTO entry VALUES ('foo/bar', 2, 'issue', 'Foo returns 1', 'closed', 'qux', '2023-12-31T00:00:00Z', '2024-01-03T00:00:00Z', 0)")
        connection.execute("INSERT INTO membership VALUES ('foo/bar', ?, 1)", (str(head),))
        connection.execute("INSERT INTO edge VALUES ('foo/bar', 1, 'connected', 'foo/bar', 2, 'issue', 1, '2024-01-01T00:00:00Z', 'baz', NULL)")
        connection.execute("INSERT INTO repository VALUES ('foo/bar', 'main', ?, 'Foo', 1, 1)", (str(head),))
        connection.execute("INSERT INTO meta VALUES ('built', '2024-02-01T00:00:00+00:00')")
        connection.commit()
        connection.close()
        return {"base": str(base), "head": str(head)}


class TestMirrorServer(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.shas = MirrorFixture(Path(self.directory.name)).build()
        self.client = TestClient(MirrorServer(Path(self.directory.name)).app)

    def tearDown(self):
        self.directory.cleanup()

    def test_pull_conversation(self):
        response = self.client.get("/foo/bar/pull/1")

        self.assertEqual(response.status_code, 200)
        self.assertIn("LGTM", response.text)
        self.assertIn("why 2?", response.text)
        self.assertIn('href="/foo/bar/issues/2"', response.text)
        self.assertIn('href="/foo/bar/blob/main/a.py#L2"', response.text)
        self.assertIn(f"merged commit <a class=\"sha\" href=\"/foo/bar/commit/{self.shas['head']}\">", response.text)
        self.assertIn('<span class="grow">qux</span><span class="muted" title="Left review comments">', response.text)
        self.assertIn('linked an issue <a href="/foo/bar/issues/2">#2</a> that may be closed by this pull request', response.text)

    def test_pull_files(self):
        response = self.client.get("/foo/bar/pull/1/files")

        self.assertEqual(response.status_code, 200)
        self.assertIn("return</span>", response.text)
        self.assertIn('id="diff-', response.text)

    def test_issue_and_number_redirects(self):
        self.assertEqual(self.client.get("/foo/bar/issues/1", follow_redirects=False).headers["location"], "/foo/bar/pull/1")
        self.assertEqual(self.client.get("/foo/bar/pull/2", follow_redirects=False).headers["location"], "/foo/bar/issues/2")
        self.assertEqual(self.client.get("/FOO/Bar/issues/2", follow_redirects=False).status_code, 301)

        response = self.client.get("/foo/bar/issues/2")

        self.assertIn('linked a pull request <a href="/foo/bar/issues/1">#1</a> that will close this issue', response.text)
        self.assertNotIn("<script>x()", response.text)
        self.assertIn("broken", response.text)

    def test_github_link_opens_mirror_page(self):
        response = self.client.get("/", params={"url": "https://github.com/foo/bar/pull/1#discussion_r9"}, follow_redirects=False)

        self.assertEqual(response.headers["location"], "/foo/bar/pull/1#discussion_r9")
        self.assertIn("does not point into a mirrored repository", self.client.get("/", params={"url": "https://github.com/baz/qux"}).text)

    def test_robots_admit_only_assistant_fetchers(self):
        rules = self.client.get("/robots.txt").text

        self.assertIn("User-agent: Claude-User\nAllow: /", rules)
        self.assertIn("User-agent: *\nDisallow: /", rules)
        self.assertEqual(self.client.get("/foo/bar").headers["x-robots-tag"], "noindex, nofollow")

    def test_uncollected_numbers_are_reported(self):
        response = self.client.get("/foo/bar/issues/3")

        self.assertEqual(response.status_code, 404)
        self.assertIn("not part of the collected data", response.text)
        self.assertEqual(self.client.get("/foo/bar/wiki/Home").status_code, 404)

    def test_foreign_pages_redirect_to_github(self):
        for path in ("/baz/qux/pull/7?w=1", "/baz/qux/wiki/Home", "/foo"):
            response = self.client.get(path, follow_redirects=False)

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], f"https://github.com{path}")

    def test_commit_shows_pull_membership(self):
        response = self.client.get(f"/foo/bar/commit/{self.shas['head'][:7]}")

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/foo/bar/pull/1"', response.text)
        self.assertIn('href="/foo/bar/issues/2">#2</a>', response.text)

    def test_tree_blob_and_raw(self):
        self.assertIn("README.md", self.client.get("/foo/bar").text)
        self.assertIn('id="L2"', self.client.get(f"/foo/bar/blob/{self.shas['base']}/a.py").text)
        self.assertEqual(self.client.get("/foo/bar/raw/main/a.py").text, "def foo():\n    return 2\n")
        self.assertEqual(self.client.get("/foo/bar/tree/main/a.py", follow_redirects=False).status_code, 302)
        self.assertEqual(self.client.get("/foo/bar/blob/main/missing.py").status_code, 404)

    def test_branch_with_slash_and_pull_ref(self):
        self.assertEqual(self.client.get("/foo/bar/blob/pull/1/head/a.py").status_code, 200)

    def test_expand_returns_context_rows(self):
        response = self.client.get(f"/foo/bar/expand/{self.shas['head']}/a.py", params={"start": 1, "end": 1, "shift": 0})

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-line="1"', response.text)
        self.assertIn('<span class="k">def</span>', response.text)

    def test_log_and_compare(self):
        self.assertIn("change foo", self.client.get("/foo/bar/commits/main/a.py").text)
        self.assertEqual(self.client.get(f"/foo/bar/compare/{self.shas['base']}...{self.shas['head']}").status_code, 200)


class TestSource(unittest.TestCase):
    def test_diff_paints_lines_from_full_blobs(self):
        with tempfile.TemporaryDirectory() as directory:
            shas = MirrorFixture(Path(directory)).build()
            source = Source(Path(directory) / "foo" / "bar.git")
            files = source.diff(source.commit(shas["base"]), source.commit(shas["head"]))
            source.paint(files[0], Highlighter())
            lines = [line for hunk in files[0].hunks for line in hunk.lines]

            self.assertEqual([(line.origin, line.old, line.new) for line in lines], [(" ", 1, 1), ("-", 2, None), ("+", None, 2)])
            self.assertIn('<span class="k">return</span>', lines[2].html)


    def test_diff_reports_hidden_context(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = pygit2.init_repository(str(Path(directory) / "foo.git"), bare=True)
            author = pygit2.Signature("foo", "foo@bar.baz")
            before = "".join(f"line {number}\n" for number in range(1, 31)).encode()
            after = before.replace(b"line 15\n", b"line 15 changed\n")
            commits = []

            for data in (before, after):
                builder = repository.TreeBuilder()
                builder.insert("a.txt", repository.create_blob(data), pygit2.GIT_FILEMODE_BLOB)
                commits.append(repository.create_commit(None, author, author, "foo", builder.write(), commits[-1:]))

            source = Source(Path(directory) / "foo.git")
            files = source.diff(source.commit(str(commits[0])), source.commit(str(commits[1])))
            source.paint(files[0], Highlighter())
            context = source.context(source.commit(str(commits[1])), "a.txt", files[0].tail, Highlighter())

            self.assertEqual(files[0].hunks[0].gap, DiffGap(1, 11, 0))
            self.assertEqual(files[0].tail, DiffGap(19, 30, 0))
            self.assertEqual([(line.old, line.new) for line in context][:2], [(19, 19), (20, 20)])
            self.assertEqual(len(context), 12)


if __name__ == "__main__":
    unittest.main()
