"""REST API extractors for raw GitHub data ingestion."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable, List, Optional

import pyarrow as pa
import requests

from benchmark.scope.utils import RangeSplitter

# Import BlobStream from io module
from components.io import BlobStream


class ChangeExtractor:
    """Extracts change data via GitHub REST API for a repository and time window."""

    def __init__(
        self,
        client: Any,
        repository: str,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self.repo = client.get_repo(repository)
        self._log = log or (lambda msg: None)

    def extract(self, since: date, until: date) -> pa.Table:
        """
        Extract all PR-related data for the given date range.
        """
        rows: List[dict] = []

        # 1. List closed PRs (with range splitting for 1000 limit protection)
        splitter = RangeSplitter(count=self._get_count, log=self._log)
        ranges = splitter.split(since, until)

        for start, end in ranges:
            self._extract_prs_range(start, end, rows)

        # 2. For each PR, extract related data
        pr_numbers = self._extract_pr_numbers(rows)
        for number in pr_numbers:
            self._extract_pr_reviews(number, rows)
            self._extract_pr_review_comments(number, rows)
            self._extract_pr_requested_reviewers(number, rows)
            self._extract_pr_comments(number, rows)
            self._extract_pr_timeline(number, rows)

        return self._build_table(rows)

    def _get_count(self, start: date, end: date) -> int:
        """Count closed PRs in date range via Search API."""
        query = f"repo:{self.repository} is:pr is:closed closed:{start}..{end}"
        result = self.client.search_issues(query)
        return result.totalCount

    def _extract_prs_range(self, start: date, end: date, rows: List[dict]) -> None:
        """Collect PRs for a single date range (guaranteed <= 1000)."""
        query = f"repo:{self.repository} is:pr is:closed closed:{start}..{end}"
        results = self.client.search_issues(query, sort="created", order="asc")

        page_num = 0
        page_items: List[dict] = []

        for issue in results:
            raw = issue.raw_data
            page_items.append(raw)

            if len(page_items) >= 100:
                rows.append({
                    "name": self.repository,
                    "type": "pr",
                    "query": f"closed:{start}..{end}",
                    "page": page_num,
                    "payload": json.dumps(page_items),
                })
                page_num += 1
                page_items = []

        if page_items:
            rows.append({
                "name": self.repository,
                "type": "pr",
                "query": f"closed:{start}..{end}",
                "page": page_num,
                "payload": json.dumps(page_items),
            })

    def _extract_pr_reviews(self, number: int, rows: List[dict]) -> None:
        """Collect reviews for a PR."""
        try:
            pr = self.repo.get_pull(number)
            reviews = pr.get_reviews()

            page_num = 0
            page_items: List[dict] = []

            for review in reviews:
                page_items.append(review.raw_data)

                if len(page_items) >= 100:
                    rows.append({
                        "name": self.repository,
                        "type": "review",
                        "query": f"pr={number}",
                        "page": page_num,
                        "payload": json.dumps(page_items),
                    })
                    page_num += 1
                    page_items = []

            if page_items:
                rows.append({
                    "name": self.repository,
                    "type": "review",
                    "query": f"pr={number}",
                    "page": page_num,
                    "payload": json.dumps(page_items),
                })
        except Exception as e:
            self._log(f"Failed to get reviews for PR {number}: {e}")

    def _extract_pr_review_comments(self, number: int, rows: List[dict]) -> None:
        """Collect review comments (inline diff comments) for a PR."""
        try:
            pr = self.repo.get_pull(number)
            comments = pr.get_review_comments()

            page_num = 0
            page_items: List[dict] = []

            for comment in comments:
                page_items.append(comment.raw_data)

                if len(page_items) >= 100:
                    rows.append({
                        "name": self.repository,
                        "type": "review_comment",
                        "query": f"pr={number}",
                        "page": page_num,
                        "payload": json.dumps(page_items),
                    })
                    page_num += 1
                    page_items = []

            if page_items:
                rows.append({
                    "name": self.repository,
                    "type": "review_comment",
                    "query": f"pr={number}",
                    "page": page_num,
                    "payload": json.dumps(page_items),
                })
        except Exception as e:
            self._log(f"Failed to get review comments for PR {number}: {e}")

    def _extract_pr_requested_reviewers(self, number: int, rows: List[dict]) -> None:
        """Collect requested reviewers for a PR."""
        try:
            pr = self.repo.get_pull(number)
            users, teams = pr.get_review_requests()

            payload = {
                "users": [u.raw_data for u in users],
                "teams": [t.raw_data for t in teams],
            }

            rows.append({
                "name": self.repository,
                "type": "review_request",
                "query": f"pr={number}",
                "page": 0,
                "payload": json.dumps(payload),
            })
        except Exception as e:
            self._log(f"Failed to get requested reviewers for PR {number}: {e}")

    def _extract_pr_comments(self, number: int, rows: List[dict]) -> None:
        """Collect issue comments on the PR discussion thread."""
        try:
            pr = self.repo.get_pull(number)
            comments = pr.get_issue_comments()

            page_num = 0
            page_items: List[dict] = []

            for comment in comments:
                page_items.append(comment.raw_data)

                if len(page_items) >= 100:
                    rows.append({
                        "name": self.repository,
                        "type": "comment",
                        "query": f"pr={number}",
                        "page": page_num,
                        "payload": json.dumps(page_items),
                    })
                    page_num += 1
                    page_items = []

            if page_items:
                rows.append({
                    "name": self.repository,
                    "type": "comment",
                    "query": f"pr={number}",
                    "page": page_num,
                    "payload": json.dumps(page_items),
                })
        except Exception as e:
            self._log(f"Failed to get comments for PR {number}: {e}")

    def _extract_pr_timeline(self, number: int, rows: List[dict]) -> None:
        """Collect timeline events for a PR."""
        try:
            requester = self.client._Github__requester
            url = f"/repos/{self.repository}/issues/{number}/timeline"

            page_num = 0
            while url:
                headers, data = requester.requestJsonAndCheck(
                    "GET",
                    url,
                    headers={"Accept": "application/vnd.github.mockingbird-preview+json"},
                )

                if data:
                    rows.append({
                        "name": self.repository,
                        "type": "timeline",
                        "query": f"pr={number}",
                        "page": page_num,
                        "payload": json.dumps(data),
                    })
                    page_num += 1

                # Parse Link header for next page
                link_header = headers.get("Link", "")
                url = self._parse_next_link(link_header)
        except Exception as e:
            self._log(f"Failed to get timeline for PR {number}: {e}")

    def _extract_pr_numbers(self, rows: List[dict]) -> List[int]:
        """Extract PR numbers from extracted PR list data."""
        numbers = []
        for row in rows:
            if row["type"] == "pr":
                items = json.loads(row["payload"])
                numbers.extend(item["number"] for item in items)
        return numbers

    @staticmethod
    def _parse_next_link(link_header: str) -> Optional[str]:
        """Extract 'next' URL from Link header."""
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    def _build_table(self, rows: List[dict]) -> pa.Table:
        if not rows:
            return pa.table({
                "name": pa.array([], type=pa.string()),
                "type": pa.array([], type=pa.string()),
                "query": pa.array([], type=pa.string()),
                "page": pa.array([], type=pa.int32()),
                "payload": pa.array([], type=pa.large_string()),
            })

        return pa.Table.from_pylist(rows, schema=pa.schema([
            ("name", pa.string()),
            ("type", pa.string()),
            ("query", pa.string()),
            ("page", pa.int32()),
            ("payload", pa.large_string()),
        ]))


class IssueExtractor:
    """Extracts issue data via GitHub REST API for a repository and time window."""

    def __init__(
        self,
        client: Any,
        repository: str,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self.repo = client.get_repo(repository)
        self._log = log or (lambda msg: None)

    def extract(self, since: date, until: date) -> pa.Table:
        """
        Extract all issue-related data for the given date range.
        """
        rows: List[dict] = []

        # 1. List closed issues (with range splitting for 1000 limit protection)
        splitter = RangeSplitter(count=self._get_count, log=self._log)
        ranges = splitter.split(since, until)

        for start, end in ranges:
            self._extract_issues_range(start, end, rows)

        # 2. For each issue, extract related data
        issue_numbers = self._extract_issue_numbers(rows)
        for number in issue_numbers:
            self._extract_issue_comments(number, rows)
            self._extract_issue_timeline(number, rows)

        return self._build_table(rows)

    def _get_count(self, start: date, end: date) -> int:
        """Count closed issues in date range via Search API."""
        query = f"repo:{self.repository} is:issue is:closed closed:{start}..{end}"
        result = self.client.search_issues(query)
        return result.totalCount

    def _extract_issues_range(self, start: date, end: date, rows: List[dict]) -> None:
        """Collect issues for a single date range (guaranteed <= 1000)."""
        query = f"repo:{self.repository} is:issue is:closed closed:{start}..{end}"
        results = self.client.search_issues(query, sort="created", order="asc")

        page_num = 0
        page_items: List[dict] = []

        for issue in results:
            raw = issue.raw_data
            page_items.append(raw)

            if len(page_items) >= 100:
                rows.append({
                    "name": self.repository,
                    "type": "issue",
                    "query": f"closed:{start}..{end}",
                    "page": page_num,
                    "payload": json.dumps(page_items),
                })
                page_num += 1
                page_items = []

        if page_items:
            rows.append({
                "name": self.repository,
                "type": "issue",
                "query": f"closed:{start}..{end}",
                "page": page_num,
                "payload": json.dumps(page_items),
            })

    def _extract_issue_comments(self, number: int, rows: List[dict]) -> None:
        """Collect comments for an issue."""
        try:
            issue = self.repo.get_issue(number)
            comments = issue.get_comments()

            page_num = 0
            page_items: List[dict] = []

            for comment in comments:
                page_items.append(comment.raw_data)

                if len(page_items) >= 100:
                    rows.append({
                        "name": self.repository,
                        "type": "comment",
                        "query": f"issue={number}",
                        "page": page_num,
                        "payload": json.dumps(page_items),
                    })
                    page_num += 1
                    page_items = []

            if page_items:
                rows.append({
                    "name": self.repository,
                    "type": "comment",
                    "query": f"issue={number}",
                    "page": page_num,
                    "payload": json.dumps(page_items),
                })
        except Exception as e:
            self._log(f"Failed to get comments for issue {number}: {e}")

    def _extract_issue_timeline(self, number: int, rows: List[dict]) -> None:
        """Collect timeline events for an issue."""
        try:
            requester = self.client._Github__requester
            url = f"/repos/{self.repository}/issues/{number}/timeline"

            page_num = 0
            while url:
                headers, data = requester.requestJsonAndCheck(
                    "GET",
                    url,
                    headers={"Accept": "application/vnd.github.mockingbird-preview+json"},
                )

                if data:
                    rows.append({
                        "name": self.repository,
                        "type": "timeline",
                        "query": f"issue={number}",
                        "page": page_num,
                        "payload": json.dumps(data),
                    })
                    page_num += 1

                # Parse Link header for next page
                link_header = headers.get("Link", "")
                url = self._parse_next_link(link_header)
        except Exception as e:
            self._log(f"Failed to get timeline for issue {number}: {e}")

    def _extract_issue_numbers(self, rows: List[dict]) -> List[int]:
        """Extract issue numbers from extracted issue list data."""
        numbers = []
        for row in rows:
            if row["type"] == "issue":
                items = json.loads(row["payload"])
                numbers.extend(item["number"] for item in items)
        return numbers

    @staticmethod
    def _parse_next_link(link_header: str) -> Optional[str]:
        """Extract 'next' URL from Link header."""
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    def _build_table(self, rows: List[dict]) -> pa.Table:
        if not rows:
            return pa.table({
                "name": pa.array([], type=pa.string()),
                "type": pa.array([], type=pa.string()),
                "query": pa.array([], type=pa.string()),
                "page": pa.array([], type=pa.int32()),
                "payload": pa.array([], type=pa.large_string()),
            })

        return pa.Table.from_pylist(rows, schema=pa.schema([
            ("name", pa.string()),
            ("type", pa.string()),
            ("query", pa.string()),
            ("page", pa.int32()),
            ("payload", pa.large_string()),
        ]))


class SourceExtractor:
    """Extracts repository source tarball via GitHub REST API."""

    def __init__(
        self,
        client: Any,
        repository: str,
        ref: str = "HEAD",
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self.ref = ref
        self._log = log or (lambda msg: None)

    def extract(self) -> BlobStream:
        """
        Download repository tarball as a streaming blob.

        Returns a BlobStream that gets uploaded directly to GCS.
        """
        repo = self.client.get_repo(self.repository)

        # Get default branch if ref is HEAD
        actual_ref = self.ref
        if actual_ref == "HEAD":
            actual_ref = repo.default_branch

        # Get archive URL via PyGithub
        archive_url = repo.get_archive_link("tarball", ref=actual_ref)

        # Stream download using requests (available via PyGithub dependency)
        token = self.client._Github__requester._Requester__auth.token

        response = requests.get(
            archive_url,
            headers={"Authorization": f"token {token}"},
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

        size = int(response.headers.get("Content-Length", 0))

        return BlobStream(
            content_type="application/gzip",
            filename=f"{self.repository.replace('/', '-')}-{actual_ref[:8]}.tar.gz",
            response=response,
            size=size,
        )
