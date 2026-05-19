from __future__ import annotations

import json

from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

import pyarrow as pa

from benchmark.scope.utils import RangeSplitter


class SearchCursor:
    """Iterator over GitHub search results with cursor pagination."""

    _MIN_PAGE_SIZE = 10

    def __init__(
        self,
        client: Any,
        query: str,
        schema: str,
        kind: str,
        page_size: int = 100,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.query = query
        self._schema = schema
        self.kind = kind
        self._page_size = page_size
        self._log = log or (lambda msg: None)
        self.total_count: int = 0
        self._cursor: Optional[str] = None
        self._buffer: List[Dict[str, Any]] = []
        self._started = False

    def __iter__(self):
        return self

    def __next__(self) -> Dict[str, Any]:
        while not self._buffer:
            if self._started and self._cursor is None:
                raise StopIteration
            self._fetch_next_page()
        return self._buffer.pop(0)

    def _fetch_next_page(self) -> None:
        try:
            response = self.client._Github__requester.graphql_query(
                self._schema,
                {
                    "q": self.query,
                    "first": self._page_size,
                    "after": self._cursor,
                    "labelLimit": 50,
                },
            )
        except Exception as e:
            message = str(e).lower()
            transient = any(
                token in message
                for token in ("502", "bad gateway", "timeout", "complexity")
            )
            if transient and self._page_size > self._MIN_PAGE_SIZE:
                self._page_size = max(self._MIN_PAGE_SIZE, self._page_size // 2)
                self._log(f"Reduced search page size to {self._page_size}")
                return
            raise

        search = response[1]["data"]["search"]
        if not self._started:
            self.total_count = int(search["issueCount"])
            self._started = True

        nodes = search["nodes"] or []
        for node in nodes:
            if node.get("__typename") == self.kind:
                self._buffer.append(node)

        self._cursor = (
            search["pageInfo"]["endCursor"]
            if search["pageInfo"]["hasNextPage"]
            else None
        )


class ReviewCursor:
    """Iterator over PR reviews with batching and cursor pagination."""

    def __init__(
        self,
        client: Any,
        owner: str,
        name: str,
        numbers: List[int],
        batch_size: int = 40,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.owner = owner
        self.name = name
        self.numbers = numbers
        self._index = 0
        self._batch_cache: Dict[int, Dict[str, Any]] = {}
        self._batch_size = batch_size
        self._log = log or (lambda msg: None)

    def __iter__(self):
        return self

    def __next__(self) -> Tuple[int, Dict[str, Any]]:
        if self._index >= len(self.numbers):
            raise StopIteration

        number = self.numbers[self._index]
        if number not in self._batch_cache:
            self._fetch_batch()

        self._index += 1
        return number, self._batch_cache.get(number, {"merged_at": None, "reviews": []})

    def _fetch_batch(self) -> None:
        remaining = self.numbers[self._index :]
        chunk = remaining[: self._batch_size]
        try:
            self._batch_cache.update(self._fetch_reviews_batch(chunk))
        except Exception as e:
            if "complexity" in str(e).lower() or "timeout" in str(e).lower():
                self._batch_size = max(5, self._batch_size // 2)
                self._log(f"Reduced review batch size to {self._batch_size}")
                self._fetch_batch()
            else:
                raise

    def _fetch_reviews_batch(self, numbers: List[int]) -> Dict[int, Dict[str, Any]]:
        variables: Dict[str, Any] = {
            "owner": self.owner,
            "name": self.name,
            "n": 100,
        }
        declarations: List[str] = []
        fragments: List[str] = []

        batch_template = """
            pr{index}: pullRequest(number: $num{index}) {{
              number
              mergedAt
              reviews(first: $n) {{
                pageInfo {{
                  hasNextPage
                  endCursor
                }}
                nodes {{
                  submittedAt
                  state
                  authorAssociation
                  author {{ __typename login }}
                }}
              }}
            }}
        """

        for index, number in enumerate(numbers):
            variables[f"num{index}"] = int(number)
            declarations.append(f"$num{index}: Int!")
            fragments.append(batch_template.format(index=index))

        query = (
            f"query Reviews($owner: String!, $name: String!, $n: Int!, {', '.join(declarations)})"
            + "{ repository(owner: $owner, name: $name) { "
            + " ".join(fragments)
            + " } }"
        )

        response = self.client._Github__requester.graphql_query(query, variables)
        repository = response[1]["data"]["repository"]

        result: Dict[int, Dict[str, Any]] = {}
        for index, _ in enumerate(numbers):
            pull_request = repository.get(f"pr{index}")
            if not pull_request:
                continue

            number = int(pull_request["number"])
            merged = pull_request.get("mergedAt")
            reviews_response = pull_request.get("reviews") or {}
            reviews = list(reviews_response.get("nodes") or [])
            page_info = reviews_response.get("pageInfo") or {}

            self._paginate_reviews(number, reviews, page_info)

            result[number] = {"merged_at": merged, "reviews": reviews}

        return result

    def _paginate_reviews(
        self,
        number: int,
        reviews: List[Dict[str, Any]],
        page_info: Dict[str, Any],
    ) -> None:
        pages = 1
        total = len(reviews)
        cursor = page_info.get("endCursor")

        page_query = """
            query ReviewPage($owner: String!, $name: String!, $num: Int!, $n: Int!, $cursor: String!) {
                repository(owner: $owner, name: $name) {
                    pullRequest(number: $num) {
                        reviews(first: $n, after: $cursor) {
                            pageInfo {
                                hasNextPage
                                endCursor
                            }
                            nodes {
                                submittedAt
                                state
                                authorAssociation
                                author { __typename login }
                            }
                        }
                    }
                }
            }
        """

        while page_info.get("hasNextPage") and pages < 10 and total < 500:
            if not cursor:
                self._log(f"Missing cursor for change {number}")
                break

            response = self.client._Github__requester.graphql_query(
                page_query,
                {
                    "owner": self.owner,
                    "name": self.name,
                    "num": number,
                    "n": 100,
                    "cursor": cursor,
                },
            )
            change = response[1]["data"]["repository"]["pullRequest"]
            reviews_data = change.get("reviews") or {}
            next_reviews = reviews_data.get("nodes") or []
            reviews.extend(next_reviews)

            page_info = reviews_data.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            pages += 1
            total += len(next_reviews)

        if page_info.get("hasNextPage"):
            self._log(
                f"Review pagination limit reached for change {number} with {pages} pages and {total} events"
            )


class ChangeReviewCollector:
    """Collects merged pull requests with their reviews."""

    def __init__(
        self,
        client: Any,
        repository: str,
        trunk: str,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self.trunk = trunk
        self.owner, self.name = repository.split("/", 1)
        self._log = log

        self.trunk_aliases = {trunk}
        if trunk != "master":
            try:
                self.client.get_repo(repository).get_branch("master")
                self.trunk_aliases.add("master")
            except Exception:
                pass

    def _is_bot_author(self, node: Dict[str, Any]) -> bool:
        return (node.get("author") or {}).get("__typename") == "Bot"

    def _process_reviews(
        self, nodes: List[Dict[str, Any]], merged: Optional[str]
    ) -> List[Dict[str, Any]]:
        reviews = []
        for node in nodes:
            submitted = node.get("submittedAt")
            if merged and submitted and submitted > merged:
                continue
            author = node.get("author") or {}
            login = author.get("login")
            if not login:
                continue
            reviews.append(
                {
                    "author": login,
                    "type": "bot" if author.get("__typename") == "Bot" else "user",
                    "state": node.get("state"),
                    "association": node.get("authorAssociation"),
                    "submitted": submitted,
                }
            )
        return reviews

    def _get_count(self, start: date, end: date) -> int:
        schema = """
            query CountSearch($q: String!) {
                search(type: ISSUE, query: $q, first: 0) { issueCount }
            }
        """
        query = f"repo:{self.repository} is:pr is:merged merged:{start}..{end}"
        response = self.client._Github__requester.graphql_query(schema, {"q": query})
        return int(response[1]["data"]["search"]["issueCount"])

    def collect(self, since: date, until: date) -> List[Dict[str, Any]]:
        schema = """
          query SearchPage($q: String!, $first: Int!, $after: String, $labelLimit: Int!) {
            search(type: ISSUE, query: $q, first: $first, after: $after) {
              issueCount
              pageInfo { hasNextPage endCursor }
              nodes {
                __typename
                ... on PullRequest {
                  number
                  baseRefName
                  createdAt
                  closedAt
                  mergedAt
                  author { __typename login }
                  additions
                  deletions
                  labels(first: $labelLimit) {
                    totalCount
                    nodes { name }
                  }
                }
              }
            }
          }
        """

        splitter = RangeSplitter(count=self._get_count, log=self._log)
        ranges = splitter.split(since, until)

        changes: List[Dict[str, Any]] = []
        total_frame = 0

        for start, end in ranges:
            query = f"repo:{self.repository} is:pr is:merged merged:{start}..{end}"
            cursor = SearchCursor(self.client, query, schema, "PullRequest")
            changes.extend(c for c in cursor if not self._is_bot_author(c))
            total_frame += cursor.total_count

        numbers = [int(x["number"]) for x in changes]
        if len(numbers) != len(set(numbers)):
            duplicates = [n for n in numbers if numbers.count(n) > 1]
            raise ValueError(f"Duplicate changes {set(duplicates)} found")
        reviews = dict(
            ReviewCursor(self.client, self.owner, self.name, numbers, log=self._log)
        )

        result = []
        for change in changes:
            number = int(change["number"])
            base = change.get("baseRefName")
            review = reviews.get(number, {})

            labels = change.get("labels") or {}
            label_nodes = labels.get("nodes") or []

            result.append(
                {
                    "name": self.repository,
                    "type": "change",
                    "number": number,
                    "author": (change.get("author") or {}).get("login"),
                    "created": change.get("createdAt"),
                    "closed": change.get("closedAt"),
                    "payload": {
                        "merged": change.get("mergedAt"),
                        "base": base,
                        "reviews": self._process_reviews(
                            review.get("reviews") or [],
                            review.get("merged_at"),
                        ),
                        "additions": change.get("additions", 0),
                        "deletions": change.get("deletions", 0),
                    },
                    # "label_count": int(labels.get("totalCount", 0)),
                    "labels": [l.get("name") for l in label_nodes if l.get("name")],
                }
            )

        return pa.Table.from_pylist(
            result,
            schema=pa.schema(
                [
                    ("name", pa.string()),
                    ("type", pa.string()),
                    ("number", pa.int64()),
                    ("author", pa.string()),
                    ("created", pa.string()),
                    ("closed", pa.string()),
                    ("labels", pa.list_(pa.string())),
                    (
                        "payload",
                        pa.struct(
                            [
                                ("merged", pa.string()),
                                ("base", pa.string()),
                                (
                                    "reviews",
                                    pa.list_(
                                        pa.struct(
                                            [
                                                ("author", pa.string()),
                                                ("type", pa.string()),
                                                ("state", pa.string()),
                                                ("association", pa.string()),
                                                ("submitted", pa.string()),
                                            ]
                                        )
                                    ),
                                ),
                                ("additions", pa.int64()),
                                ("deletions", pa.int64()),
                            ]
                        ),
                    ),
                ]
            ),
        )


class ChangeOutlineCollector:
    """Collects merged pull request text and changed file metadata."""

    def __init__(
        self,
        client: Any,
        repository: str,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self.owner, self.name = repository.split("/", 1)
        self._log = log or (lambda msg: None)

    def _is_bot_author(self, node: Dict[str, Any]) -> bool:
        return (node.get("author") or {}).get("__typename") == "Bot"

    def _get_count(self, start: date, end: date) -> int:
        schema = """
            query CountSearch($q: String!) {
                search(type: ISSUE, query: $q, first: 0) { issueCount }
            }
        """
        query = f"repo:{self.repository} is:pr is:merged merged:{start}..{end}"
        response = self.client._Github__requester.graphql_query(schema, {"q": query})
        return int(response[1]["data"]["search"]["issueCount"])

    def collect(self, since: date, until: date) -> pa.Table:
        schema = """
          query SearchPage($q: String!, $first: Int!, $after: String) {
            search(type: ISSUE, query: $q, first: $first, after: $after) {
              issueCount
              pageInfo { hasNextPage endCursor }
              nodes {
                __typename
                ... on PullRequest {
                  number
                  title
                  bodyText
                  changedFiles
                  files(first: 100) {
                    pageInfo { hasNextPage endCursor }
                    nodes { path additions deletions changeType }
                  }
                }
              }
            }
          }
        """

        splitter = RangeSplitter(count=self._get_count, log=self._log)
        ranges = splitter.split(since, until)

        changes: List[Dict[str, Any]] = []
        for start, end in ranges:
            query = f"repo:{self.repository} is:pr is:merged merged:{start}..{end}"
            cursor = SearchCursor(self.client, query, schema, "PullRequest", log=self._log)
            for change in cursor:
                if self._is_bot_author(change):
                    continue
                changes.append(change)

        numbers = [int(x["number"]) for x in changes]
        if len(numbers) != len(set(numbers)):
            duplicates = [n for n in numbers if numbers.count(n) > 1]
            raise ValueError(f"Duplicate changes {set(duplicates)} found")

        result = []
        for change in changes:
            number = int(change["number"])
            files_response = change.get("files") or {}
            file_nodes = list(files_response.get("nodes") or [])
            page_info = files_response.get("pageInfo") or {}

            self._paginate_files(number, file_nodes, page_info)

            result.append(
                {
                    "name": self.repository,
                    "number": number,
                    "title": change.get("title") or "",
                    "body": change.get("bodyText") or "",
                    "files": [
                        {
                            "path": node.get("path"),
                            "additions": int(node.get("additions") or 0),
                            "deletions": int(node.get("deletions") or 0),
                            "change": node.get("changeType"),
                        }
                        for node in file_nodes
                    ],
                }
            )

        return pa.Table.from_pylist(
            result,
            schema=pa.schema(
                [
                    ("name", pa.string()),
                    ("number", pa.int64()),
                    ("title", pa.string()),
                    ("body", pa.string()),
                    (
                        "files",
                        pa.list_(
                            pa.struct(
                                [
                                    ("path", pa.string()),
                                    ("additions", pa.int64()),
                                    ("deletions", pa.int64()),
                                    ("change", pa.string()),
                                ]
                            )
                        ),
                    ),
                ]
            ),
        )

    def _paginate_files(
        self,
        number: int,
        nodes: List[Dict[str, Any]],
        page_info: Dict[str, Any],
    ) -> None:
        pages = 1
        cursor = page_info.get("endCursor")

        schema = """
            query FilesPage($owner: String!, $name: String!, $num: Int!, $n: Int!, $cursor: String!) {
                repository(owner: $owner, name: $name) {
                    pullRequest(number: $num) {
                        files(first: $n, after: $cursor) {
                            pageInfo { hasNextPage endCursor }
                            nodes { path additions deletions changeType }
                        }
                    }
                }
            }
        """

        while page_info.get("hasNextPage") and pages < 20:
            if not cursor:
                self._log(f"Missing files cursor for change {number}")
                break

            response = self.client._Github__requester.graphql_query(
                schema,
                {
                    "owner": self.owner,
                    "name": self.name,
                    "num": number,
                    "n": 100,
                    "cursor": cursor,
                },
            )
            files_response = (
                response[1]["data"]["repository"]["pullRequest"].get("files") or {}
            )
            next_nodes = files_response.get("nodes") or []
            nodes.extend(next_nodes)

            page_info = files_response.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            pages += 1

        if page_info.get("hasNextPage"):
            self._log(
                f"File pagination limit reached for change {number} with {pages} pages"
            )


class IssueCollector:
    """Collects closed issues."""

    def __init__(
        self,
        client: Any,
        repository: str,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self._log = log

    def _get_count(self, start: date, end: date) -> int:
        """Count closed issues in date range for RangeSplitter."""
        schema = """
            query CountSearch($q: String!) {
                search(type: ISSUE, query: $q, first: 0) { issueCount }
            }
        """
        query = f"repo:{self.repository} is:issue is:closed closed:{start}..{end}"
        response = self.client._Github__requester.graphql_query(schema, {"q": query})
        return int(response[1]["data"]["search"]["issueCount"])

    def collect(self, since: date, until: date) -> pa.Table:
        schema = """
          query SearchPage($q: String!, $first: Int!, $after: String, $labelLimit: Int!) {
            search(type: ISSUE, query: $q, first: $first, after: $after) {
              issueCount
              pageInfo { hasNextPage endCursor }
              nodes {
                __typename
                ... on Issue {
                  number
                  author { __typename login }
                  createdAt
                  closedAt
                  labels(first: $labelLimit) {
                    totalCount
                    nodes { name }
                  }
                }
              }
            }
          }
        """

        splitter = RangeSplitter(count=self._get_count, log=self._log)
        ranges = splitter.split(since, until)

        issues: List[Dict[str, Any]] = []
        total = 0

        for start, end in ranges:
            query = f"repo:{self.repository} is:issue is:closed closed:{start}..{end}"
            cursor = SearchCursor(
                self.client, query, schema, "Issue", log=self._log
            )
            issues.extend(cursor)
            total += cursor.total_count

        numbers = [int(x["number"]) for x in issues]
        if len(numbers) != len(set(numbers)):
            duplicates = [n for n in numbers if numbers.count(n) > 1]
            raise ValueError(f"Duplicate issue {set(duplicates)} found")

        result = []
        for issue in issues:
            labels = issue.get("labels") or {}
            nodes = labels.get("nodes") or []

            result.append(
                {
                    "name": self.repository,
                    "type": "issue",
                    "number": int(issue["number"]),
                    "author": (issue.get("author") or {}).get("login"),
                    "created": issue.get("createdAt"),
                    "closed": issue.get("closedAt"),
                    # "label_count": int(labels.get("totalCount", 0)),
                    "labels": [l.get("name") for l in nodes if l.get("name")],
                }
            )

        return pa.Table.from_pylist(
            result,
            schema=pa.schema(
                [
                    ("name", pa.string()),
                    ("type", pa.string()),
                    ("number", pa.int64()),
                    ("author", pa.string()),
                    ("created", pa.string()),
                    ("closed", pa.string()),
                    ("labels", pa.list_(pa.string())),
                ]
            ),
        )


class CommitCursor:
    """Iterator over commits resolved by identifier with associated pull requests."""

    def __init__(
        self,
        client: Any,
        repository: str,
        ranges: Dict[str, List[str]],
        batch_size: int = 50,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self.owner, self.name = repository.split("/", 1)
        self.ranges = ranges
        self._items: List[Tuple[str, str]] = [
            (branch, identifier)
            for branch, identifiers in ranges.items()
            for identifier in identifiers
        ]
        self._index = 0
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._batch_size = batch_size
        self._log = log or (lambda message: None)

    def __iter__(self):
        return self

    def __next__(self) -> Dict[str, Any]:
        if self._index >= len(self._items):
            raise StopIteration

        branch, identifier = self._items[self._index]
        if identifier not in self._cache:
            self._fetch_batch()

        self._index += 1
        node = self._cache.get(identifier)
        if node is None:
            return {
                "name": self.repository,
                "branch": branch,
                "identifier": identifier,
                "date": None,
                "payload": None,
            }
        return {
            "name": self.repository,
            "branch": branch,
            "identifier": identifier,
            "date": node.get("committedDate"),
            "payload": json.dumps(node),
        }

    def _fetch_batch(self) -> None:
        remaining = [
            identifier
            for _, identifier in self._items[self._index:]
            if identifier not in self._cache
        ]
        chunk = remaining[: self._batch_size]
        try:
            self._cache.update(self._fetch_commits_batch(chunk))
        except Exception as e:
            if "complexity" in str(e).lower() or "timeout" in str(e).lower():
                self._batch_size = max(5, self._batch_size // 2)
                self._log(f"Reduced commit batch size to {self._batch_size}")
                self._fetch_batch()
            else:
                raise

    def _fetch_commits_batch(self, identifiers: List[str]) -> Dict[str, Dict[str, Any]]:
        variables: Dict[str, Any] = {"owner": self.owner, "name": self.name}
        declarations: List[str] = []
        fragments: List[str] = []

        template = """
            c{index}: object(oid: $id{index}) {{
              ... on Commit {{
                oid
                messageHeadline
                messageBody
                committedDate
                author {{ user {{ __typename login }} }}
                associatedPullRequests(first: 5) {{
                  nodes {{
                    number
                    merged
                    baseRefName
                    mergeCommit {{ oid }}
                    repository {{ nameWithOwner }}
                  }}
                }}
              }}
            }}
        """

        for index, identifier in enumerate(identifiers):
            variables[f"id{index}"] = identifier
            declarations.append(f"$id{index}: GitObjectID!")
            fragments.append(template.format(index=index))

        query = (
            f"query Commits($owner: String!, $name: String!, {', '.join(declarations)})"
            + "{ repository(owner: $owner, name: $name) { "
            + " ".join(fragments)
            + " } }"
        )

        response = self.client._Github__requester.graphql_query(query, variables)
        repository = response[1]["data"]["repository"]

        result: Dict[str, Dict[str, Any]] = {}
        for index, identifier in enumerate(identifiers):
            node = repository.get(f"c{index}")
            if node is not None:
                result[identifier] = node
        return result
