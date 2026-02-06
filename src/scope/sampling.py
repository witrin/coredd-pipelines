from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import dagster as dg


@dataclass(frozen=True)
class AttainmentConfiguration:
    tooling_start: date = date(2016, 4, 1)
    end_date: date = date(2025, 12, 31)

    nmax_changes: int = 120
    nmax_issues: int = 120

    page_size: int = 50
    change_batch: int = 25
    reviews_per_change: int = 20

    stable_windows: int = 2
    nmin_changes: int = 30
    nmin_issues: int = 30

    delta_review: float = 0.05
    delta_default: float = 0.03
    delta_label: float = 0.07
    delta_audit: float = 0.03


class QuarterIterator:
    """Iterator over calendar quarters between two dates."""

    def __init__(self, start: date, end: date):
        self.end = end
        self.current = self._quarter_start(start)

    @staticmethod
    def _quarter_index(d: date) -> int:
        return (d.month - 1) // 3

    @staticmethod
    def _quarter_start(d: date) -> date:
        q = QuarterIterator._quarter_index(d)
        return date(d.year, q * 3 + 1, 1)

    def _next_quarter(self) -> date:
        q = self._quarter_index(self.current)
        if q == 3:
            return date(self.current.year + 1, 1, 1)
        return date(self.current.year, (q + 1) * 3 + 1, 1)

    def __iter__(self):
        return self

    def __next__(self) -> Tuple[date, date, str]:
        if self.current > self.end:
            raise StopIteration
        qnext = self._next_quarter()
        until = min(qnext - timedelta(days=1), self.end)
        label = f"{self.current.year}-Q{self._quarter_index(self.current) + 1}"
        result = (self.current, until, label)
        self.current = qnext
        return result


class AttainmentSampler:
    def __init__(
        self,
        client: Any,
        configuration: AttainmentConfiguration,
        repository: str,
        trunk: str,
    ):
        self.client = client
        self.configuration = configuration
        self.repository = repository
        self.trunk = trunk
        self.owner, self.name = repository.split("/", 1)

    def _search_page(
        self,
        query_str: str,
        first: int,
        after: Optional[str],
    ) -> Tuple[int, List[Dict[str, Any]], Optional[str]]:
        query = """
          query SearchPage($q: String!, $first: Int!, $after: String) {
            search(type: ISSUE, query: $q, first: $first, after: $after) {
              issueCount
              pageInfo { hasNextPage endCursor }
              nodes {
                __typename
                ... on PullRequest {
                  number
                  baseRefName
                  author { login }
                }
                ... on Issue {
                  number
                  labels { totalCount }
                }
              }
            }
          }
        """
        resp = self.client._Github__requester.graphql_query(
            query, {"q": query_str, "first": first, "after": after}
        )
        s = resp[1]["data"]["search"]
        nodes = s["nodes"] or []
        end = s["pageInfo"]["endCursor"] if s["pageInfo"]["hasNextPage"] else None
        return int(s["issueCount"]), nodes, end

    def _paginate(
        self,
        query: str,
        type_filter: str,
        max_items: int,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Paginate through search results and pick items of given type."""
        frame, nodes, cursor = self._search_page(query, first=self.configuration.page_size, after=None)
        need = min(max_items, frame)
        picked: List[Dict[str, Any]] = []

        while nodes and len(picked) < need:
            for n in nodes:
                if n.get("__typename") == type_filter:
                    picked.append(n)
                    if len(picked) >= need:
                        break
            if cursor is None or len(picked) >= need:
                break
            _, nodes, cursor = self._search_page(query, first=self.configuration.page_size, after=cursor)

        return frame, picked

    def _reviews_for_changes(
        self,
        numbers: List[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        configuration = self.configuration

        vars_: Dict[str, Any] = {"owner": self.owner, "name": self.name, "n": configuration.reviews_per_change}
        decls: List[str] = []
        parts: List[str] = []

        for i, num in enumerate(numbers):
            vars_[f"num{i}"] = int(num)
            decls.append(f"$num{i}: Int!")
            parts.append(
                f"""
                c{i}: pullRequest(number: $num{i}) {{
                  number
                  reviews(last: $n) {{
                    nodes {{ author {{ login isBot }} }}
                  }}
                }}
                """
            )

        query = (
            f"query Reviews($owner: String!, $name: String!, $n: Int!, {', '.join(decls)})"
            + "{ repository(owner: $owner, name: $name) { "
            + " ".join(parts)
            + " } }"
        )

        resp = self.client._Github__requester.graphql_query(query, vars_)
        repo = resp[1]["data"]["repository"]

        out: Dict[int, List[Dict[str, Any]]] = {}
        for i, _ in enumerate(numbers):
            pr = repo.get(f"c{i}")
            if pr:
                out[int(pr["number"])] = (pr.get("reviews") or {}).get("nodes") or []
        return out

    def changes(self, since: date, until: date) -> Dict[str, Any]:
        configuration = self.configuration

        q = f"repo:{self.repository} is:pr is:merged merged:{since}..{until}"
        frame, picked = self._paginate(q, "PullRequest", configuration.nmax_changes)

        numbers = [int(x["number"]) for x in picked]
        author_by_num = {int(x["number"]): (x.get("author") or {}).get("login") for x in picked}
        base_by_num = {int(x["number"]): x.get("baseRefName") for x in picked}

        reviews_by_num: Dict[int, List[Dict[str, Any]]] = {}
        for i in range(0, len(numbers), configuration.change_batch):
            chunk = numbers[i : i + configuration.change_batch]
            reviews_by_num.update(self._reviews_for_changes(chunk))

        sample: List[Dict[str, Any]] = []
        for num in numbers:
            rev_nodes = reviews_by_num.get(num, [])
            reviews: List[Dict[str, Any]] = []
            for rn in rev_nodes:
                a = rn.get("author") or {}
                login = a.get("login")
                if not login:
                    continue
                reviews.append({"author": login, "type": "bot" if a.get("isBot") else "user"})

            base_ref = base_by_num.get(num)
            sample.append(
                {
                    "number": int(num),
                    "author": author_by_num.get(num),
                    "base_ref": base_ref,
                    "targets_trunk": base_ref == self.trunk,
                    "reviews": reviews,
                }
            )

        return {"frame": int(frame), "sample": sample}

    def issues(self, since: date, until: date) -> Dict[str, Any]:
        configuration = self.configuration

        q = f"repo:{self.repository} is:issue is:closed closed:{since}..{until}"
        frame, picked = self._paginate(q, "Issue", configuration.nmax_issues)

        sample: List[Dict[str, Any]] = []
        for it in picked:
            labels = int((it.get("labels") or {}).get("totalCount", 0))
            sample.append(
                {
                    "number": int(it["number"]),
                    "label_count": labels,
                    "has_label": labels > 0,
                }
            )

        return {"frame": int(frame), "sample": sample}

    def check(
        self,
        changes_sample: List[Dict[str, Any]],
        issues_sample: List[Dict[str, Any]],
        ref_review: float,
        ref_audit: float,
        ref_label: float,
        ref_default: float,
    ) -> bool:
        """Check if sample meets stability criteria relative to reference shares."""
        cfg = self.configuration
        n_changes = len(changes_sample)
        n_issues = len(issues_sample)

        if n_changes < cfg.nmin_changes or n_issues < cfg.nmin_issues:
            return False

        k_default = sum(c["targets_trunk"] for c in changes_sample)
        k_labeled = sum(it["has_label"] for it in issues_sample)
        k_indep = 0
        k_audit_only = 0

        for c in changes_sample:
            author = c.get("author")
            reviewers = c.get("reviews") or []
            if reviewers:
                has_indep = any(
                    rv["type"] == "user" and author is not None and rv["author"] != author
                    for rv in reviewers
                )
                if has_indep:
                    k_indep += 1
                else:
                    k_audit_only += 1

        review_share = k_indep / n_changes
        audit_share = k_audit_only / n_changes
        default_share = k_default / n_changes
        label_share = k_labeled / n_issues

        return (
            review_share >= ref_review - cfg.delta_review
            and default_share >= ref_default - cfg.delta_default
            and label_share >= ref_label - cfg.delta_label
            and audit_share <= ref_audit + cfg.delta_audit
        )
