import os
import tarfile
import tempfile
import pygit2
from io import BytesIO

import json
from datetime import date
from typing import Any, Callable, List, Optional

import pyarrow as pa

from benchmark.scope.utils import RangeSplitter

# Import BlobStream from io module
from components.io import BlobStream


class FileResponseAdapter:
    def __init__(self, file):
        self._file = file

    def iter_content(self, chunk_size=8192):
        while True:
            chunk = self._file.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self):
        self._file.close()


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
        rows: List[dict] = []

        splitter = RangeSplitter(count=self._get_count, log=self._log)
        ranges = splitter.split(since, until)

        for start, end in ranges:
            self._extract_prs_range(start, end, rows)

        pr_numbers = self._extract_pr_numbers(rows)
        for number in pr_numbers:
            self._extract_pr_commits(number, rows)
            self._extract_pr_reviews(number, rows)
            self._extract_pr_review_comments(number, rows)
            self._extract_pr_requested_reviewers(number, rows)
            self._extract_pr_comments(number, rows)
            self._extract_pr_timeline(number, rows)

        return self._build_table(rows)

    def _get_count(self, start: date, end: date) -> int:
        query = f"repo:{self.repository} is:pr is:closed closed:{start}..{end}"
        result = self.client.search_issues(query)
        return result.totalCount

    def _extract_prs_range(self, start: date, end: date, rows: List[dict]) -> None:
        query = f"repo:{self.repository} is:pr is:closed closed:{start}..{end}"
        results = self.client.search_issues(query, sort="created", order="asc")

        for issue in results:
            number = issue.raw_data["number"]
            payload = self.repo.get_pull(number).raw_data
            rows.append(
                {
                    "name": self.repository,
                    "type": "pr",
                    "number": issue.raw_data["number"],
                    "payload": json.dumps(payload),
                }
            )

    def _extract_pr_commits(self, number: int, rows: List[dict]) -> None:
        try:
            pr = self.repo.get_pull(number)
            items = [commit.raw_data for commit in pr.get_commits()]
            if items:
                rows.append(
                    {
                        "name": self.repository,
                        "type": "commit",
                        "number": number,
                        "payload": json.dumps(items),
                    }
                )
        except Exception as e:
            self._log(f"Failed to get commits for PR {number}: {e}")

    def _extract_pr_reviews(self, number: int, rows: List[dict]) -> None:
        try:
            pr = self.repo.get_pull(number)
            items = [review.raw_data for review in pr.get_reviews()]
            if items:
                rows.append(
                    {
                        "name": self.repository,
                        "type": "review",
                        "number": number,
                        "payload": json.dumps(items),
                    }
                )
        except Exception as e:
            self._log(f"Failed to get reviews for PR {number}: {e}")

    def _extract_pr_review_comments(self, number: int, rows: List[dict]) -> None:
        try:
            change = self.repo.get_pull(number)
            items = [comment.raw_data for comment in change.get_review_comments()]
            if items:
                rows.append(
                    {
                        "name": self.repository,
                        "type": "review_comment",
                        "number": number,
                        "payload": json.dumps(items),
                    }
                )
        except Exception as e:
            self._log(f"Failed to get review comments for PR {number}: {e}")

    def _extract_pr_requested_reviewers(self, number: int, rows: List[dict]) -> None:
        try:
            change = self.repo.get_pull(number)
            users, teams = change.get_review_requests()
            payload = {
                "users": [u.raw_data for u in users],
                "teams": [t.raw_data for t in teams],
            }
            rows.append(
                {
                    "name": self.repository,
                    "type": "review_request",
                    "number": number,
                    "payload": json.dumps(payload),
                }
            )
        except Exception as e:
            self._log(f"Failed to get requested reviewers for PR {number}: {e}")

    def _extract_pr_comments(self, number: int, rows: List[dict]) -> None:
        try:
            change = self.repo.get_pull(number)
            items = [comment.raw_data for comment in change.get_issue_comments()]
            if items:
                rows.append(
                    {
                        "name": self.repository,
                        "type": "comment",
                        "number": number,
                        "payload": json.dumps(items),
                    }
                )
        except Exception as e:
            self._log(f"Failed to get comments for PR {number}: {e}")

    def _extract_pr_timeline(self, number: int, rows: List[dict]) -> None:
        try:
            requester = self.client._Github__requester
            url = f"/repos/{self.repository}/issues/{number}/timeline"
            items = []

            while url:
                headers, data = requester.requestJsonAndCheck(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/vnd.github.mockingbird-preview+json"
                    },
                )
                if data:
                    items.extend(data)
                url = self._parse_next_link(headers.get("Link", ""))

            if items:
                rows.append(
                    {
                        "name": self.repository,
                        "type": "timeline",
                        "number": number,
                        "payload": json.dumps(items),
                    }
                )
        except Exception as e:
            self._log(f"Failed to get timeline for PR {number}: {e}")

    def _extract_pr_numbers(self, rows: List[dict]) -> List[int]:
        return [row["number"] for row in rows if row["type"] == "pr"]

    @staticmethod
    def _parse_next_link(link_header: str) -> Optional[str]:
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    def _build_table(self, rows: List[dict]) -> pa.Table:
        if not rows:
            return pa.table(
                {
                    "name": pa.array([], type=pa.string()),
                    "type": pa.array([], type=pa.string()),
                    "number": pa.array([], type=pa.int32()),
                    "payload": pa.array([], type=pa.large_string()),
                }
            )

        return pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    ("name", pa.string()),
                    ("type", pa.string()),
                    ("number", pa.int32()),
                    ("payload", pa.large_string()),
                ]
            ),
        )


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
        rows: List[dict] = []

        splitter = RangeSplitter(count=self._get_count, log=self._log)
        ranges = splitter.split(since, until)

        for start, end in ranges:
            self._extract_issues_range(start, end, rows)

        numbers = self._extract_issue_numbers(rows)
        for number in numbers:
            self._extract_issue_comments(number, rows)
            self._extract_issue_timeline(number, rows)

        return self._build_table(rows)

    def _get_count(self, start: date, end: date) -> int:
        query = f"repo:{self.repository} is:issue is:closed closed:{start}..{end}"
        result = self.client.search_issues(query)
        return result.totalCount

    def _extract_issues_range(self, start: date, end: date, rows: List[dict]) -> None:
        query = f"repo:{self.repository} is:issue is:closed closed:{start}..{end}"
        results = self.client.search_issues(query, sort="created", order="asc")

        for issue in results:
            rows.append(
                {
                    "name": self.repository,
                    "type": "issue",
                    "number": issue.raw_data["number"],
                    "payload": json.dumps(issue.raw_data),
                }
            )

    def _extract_issue_comments(self, number: int, rows: List[dict]) -> None:
        try:
            issue = self.repo.get_issue(number)
            items = [comment.raw_data for comment in issue.get_comments()]
            if items:
                rows.append(
                    {
                        "name": self.repository,
                        "type": "comment",
                        "number": number,
                        "payload": json.dumps(items),
                    }
                )
        except Exception as e:
            self._log(f"Failed to get comments for issue {number}: {e}")

    def _extract_issue_timeline(self, number: int, rows: List[dict]) -> None:
        try:
            requester = self.client._Github__requester
            url = f"/repos/{self.repository}/issues/{number}/timeline"
            items = []

            while url:
                headers, data = requester.requestJsonAndCheck(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/vnd.github.mockingbird-preview+json"
                    },
                )
                if data:
                    items.extend(data)
                url = self._parse_next_link(headers.get("Link", ""))

            if items:
                rows.append(
                    {
                        "name": self.repository,
                        "type": "timeline",
                        "number": number,
                        "payload": json.dumps(items),
                    }
                )
        except Exception as e:
            self._log(f"Failed to get timeline for issue {number}: {e}")

    def _extract_issue_numbers(self, rows: List[dict]) -> List[int]:
        return [row["number"] for row in rows if row["type"] == "issue"]

    @staticmethod
    def _parse_next_link(link_header: str) -> Optional[str]:
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    def _build_table(self, rows: List[dict]) -> pa.Table:
        if not rows:
            return pa.table(
                {
                    "name": pa.array([], type=pa.string()),
                    "type": pa.array([], type=pa.string()),
                    "number": pa.array([], type=pa.int32()),
                    "payload": pa.array([], type=pa.large_string()),
                }
            )

        return pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    ("name", pa.string()),
                    ("type", pa.string()),
                    ("number", pa.int32()),
                    ("payload", pa.large_string()),
                ]
            ),
        )


class SourceExtractor:
    """Extracts repository snapshot using Git."""

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
        reference = self.ref
        if reference == "HEAD":
            reference = self.client.get_repo(self.repository).default_branch

        token = self.client._Github__requester._Requester__auth.token

        with tempfile.TemporaryDirectory() as temporary:
            directory = os.path.join(temporary, "repository.git")

            callbacks = pygit2.RemoteCallbacks(
                credentials=pygit2.UserPass(
                    "x-access-token",
                    token,
                )
            )

            pygit2.clone_repository(
                f"https://github.com/{self.repository}.git",
                directory,
                bare=True,
                checkout_branch=reference,
                callbacks=callbacks,
            )

            repository = pygit2.Repository(directory)

            repository.config.set_multivar(
                "remote.origin.fetch",
                "",
                "+refs/pull/*:refs/pull/*",
            )
            repository.remotes["origin"].fetch(
                callbacks=callbacks, prune=pygit2.GIT_FETCH_PRUNE
            )

            commit = repository.revparse_single(reference)

            archive = os.path.join(temporary, f"{commit.id}.tar.gz")

            with tarfile.open(archive, "w:gz") as tar:
                tar.add(directory, arcname=os.path.basename(directory))

            size = os.path.getsize(archive)

            file = open(archive, "rb")
            size = os.path.getsize(archive)

        return BlobStream(
            content_type="application/gzip",
            filename=f"{commit.id}.tar.gz",
            response=FileResponseAdapter(file),
            size=size,
        )
