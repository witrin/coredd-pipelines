from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

import polars as pl

from benchmark.attribution.change import PullRequestEnricher


class TestPullRequestEnricher(unittest.TestCase):
    def _raw(self) -> pl.DataFrame:
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
                "type": ["pr"],
                "number": [7],
                "payload": [payload],
            }
        )

    def _reference(self) -> pl.DataFrame:
        # The change reference: one row per PR, carrying the list of SHAs that
        # identify it (a branch commit and the merge/squash commit) plus the
        # cleaned title+body.
        return pl.DataFrame(
            {
                "name": ["acme/widget"],
                "number": [7],
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

    def test_enrich_uses_cleaned_corpus_for_any_pr_commit(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._raw(), reference=self._reference()
        )

        # A branch commit (not the merge commit) is enriched with the cleaned text.
        out = enricher.enrich("message", "commit subject", "branch-sha")

        self.assertIn("--- Pull request description ---", out)
        self.assertIn("Cleaned body", out)
        self.assertNotIn("checklist item", out)

    def test_enrich_merge_commit_also_uses_cleaned_corpus(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._raw(), reference=self._reference()
        )

        out = enricher.enrich("message", "merge subject", "merge-sha")

        self.assertIn("Cleaned body", out)
        self.assertNotIn("checklist item", out)

    def test_enrich_unknown_commit_is_unchanged(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._raw(), reference=self._reference()
        )

        out = enricher.enrich("message", "orphan subject", "orphan-sha")

        self.assertEqual(out, "orphan subject")

    def test_enrich_falls_back_to_raw_without_corpus(self) -> None:
        # No corpus attached: enrich falls back to the raw frame, keyed by the
        # merge-commit SHA, with the RAW (unstripped-template) body.
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._raw()
        )

        out = enricher.enrich("message", "merge subject", "merge-sha")

        self.assertIn("--- Pull request description ---", out)
        self.assertIn("Raw body", out)
        # Fallback keys on merge-commit SHA only; a branch commit is unknown.
        self.assertEqual(
            enricher.enrich("message", "x", "branch-sha"), "x"
        )

    def test_enrich_ignores_non_message_kind(self) -> None:
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._raw(), reference=self._reference()
        )

        self.assertEqual(enricher.enrich("path", "v", "merge-sha"), "v")
        self.assertEqual(enricher.enrich("message", "v", None), "v")

    def test_table_keeps_pr_payload_for_platform_cache(self) -> None:
        # PlatformCache consumes table() and needs name/type/number/payload intact.
        enricher = PullRequestEnricher("acme/widget").attach(
            self._repository(), self._raw(), reference=self._reference()
        )

        table = enricher.table()

        for column in ("name", "type", "number", "payload"):
            self.assertIn(column, table.column_names)


if __name__ == "__main__":
    unittest.main()
