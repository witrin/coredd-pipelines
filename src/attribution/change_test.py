from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

import polars as pl

from benchmark.attribution.change import MarkdownText, PullRequestEnricher


class TestPullRequestEnricher(unittest.TestCase):
    def _reference(self) -> pl.DataFrame:
        # The change reference: one row per PR, carrying the raw payload, the list
        # of SHAs that identify it (a branch commit and the merge/squash commit),
        # and the cleaned title+body.
        payload = json.dumps(
            {
                "merged": True,
                "merge_commit_sha": "merge-sha",
                "number": 7,
                "title": "Fix bar",
                "body": "Raw body\n- [ ] checklist item",
            }
        )
        return pl.DataFrame(
            {
                "name": ["acme/widget"],
                "number": [7],
                "payload": [payload],
                "title": ["Fix bar"],
                "body": ["Cleaned body"],
                "python": [3],
                "commits": [["branch-sha", "merge-sha"]],
            }
        )

    def _repository(self) -> Mock:
        repository = Mock(spec_set=["meta"])
        repository.meta.return_value = Mock()
        return repository

    def test_enrich_appends_cleaned_text_when_message_is_independent(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._reference()
        )

        # The commit subject does not carry the PR body, so the cleaned body is
        # added (markdown-free), never the raw checklist line. Layout: the original
        # subject first, then the cleaned description.
        out = enricher.enrich("message", "commit subject", "branch-sha")

        self.assertTrue(out.startswith("commit subject"))
        self.assertIn("Cleaned body", out)
        self.assertNotIn("checklist item", out)

    def test_enrich_replaces_when_message_already_has_pr_body(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._reference()
        )

        # A squash commit message that already carries the raw PR body (title +
        # raw body, here with markdown noise) is replaced by the cleaned body, but
        # the subject (with its "(#7)" PR id) and a commit reference are kept.
        message = (
            "Fix bar (#7)\n\nThis reverts commit deadbeef1234567.\n\n"
            "Raw body\n- [ ] checklist item"
        )
        out = enricher.enrich("message", message, "merge-sha")

        self.assertIn("Cleaned body", out)
        self.assertNotIn("--- Pull request description ---", out)
        self.assertNotIn("checklist item", out)
        self.assertIn("(#7)", out)
        self.assertIn("deadbeef1234567", out)

    def test_enrich_strips_process_trailers_keeps_issue_trailers(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._reference()
        )

        message = (
            "Fix bar\n\nCo-authored-by: Bob <b@x>\n"
            "Signed-off-by: Alice <a@x>\nFixes #123"
        )
        out = enricher.enrich("message", message, "branch-sha")

        self.assertNotIn("Signed-off-by", out)
        self.assertNotIn("Co-authored-by", out)
        self.assertIn("Fixes #123", out)

    def test_enrich_unknown_commit_is_unchanged(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._reference()
        )

        out = enricher.enrich("message", "orphan subject", "orphan-sha")

        self.assertEqual(out, "orphan subject")

    def test_enrich_ignores_non_message_kind(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._reference()
        )

        self.assertEqual(enricher.enrich("path", "v", "merge-sha"), "v")
        self.assertEqual(enricher.enrich("message", "v", None), "v")

    def test_table_keeps_pr_payload_for_platform_cache(self) -> None:
        # PlatformCache consumes table() and needs name/type/number/payload intact.
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._reference()
        )

        table = enricher.table()

        for column in ("name", "type", "number", "payload"):
            self.assertIn(column, table.column_names)


class TestMarkdownText(unittest.TestCase):
    def test_to_text_strips_markup_and_keeps_text(self) -> None:
        md = MarkdownText()
        out = md.to_text("## Title\n\nSee `foo` and [link](http://x).")
        self.assertIn("Title", out)
        self.assertIn("foo", out)
        self.assertIn("link", out)
        self.assertNotIn("http://x", out)
        self.assertNotIn("##", out)
        self.assertNotIn("`", out)

    def test_to_text_preserves_dunder_identifiers(self) -> None:
        # Emphasis is disabled so __init__ is not read as bold markup.
        md = MarkdownText()
        out = md.to_text("Bug in __init__ and __iter__ methods.")
        self.assertIn("__init__", out)
        self.assertIn("__iter__", out)

    def test_contains_is_tolerant_to_wrapping(self) -> None:
        md = MarkdownText()
        haystack = "1. Make it configurable.\n   2. Add support."
        candidate = "Make it configurable. Add support."
        self.assertTrue(md.contains(haystack, candidate))

    def test_difference_returns_only_missing_units(self) -> None:
        md = MarkdownText()
        haystack = "Add support for X."
        candidate = "Add support for X. A brand new sentence here."
        diff = md.difference(haystack, candidate)
        self.assertEqual(diff, ["A brand new sentence here."])


if __name__ == "__main__":
    unittest.main()
