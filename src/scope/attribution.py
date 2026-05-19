import json
import re
from typing import Any, Dict


class PushClassification:
    """Classifies trunk commits by their merge attribution into one of:
    bypassed, attributed, picked, ambiguous."""

    def __init__(self):
        self.cherry = re.compile(r"\(cherry picked from commit ([0-9a-f]{40})\)")

    def classify(self, node: Dict[str, Any], branch: str, repo: str) -> str:
        if node.get("messageBody") and self.cherry.search(node["messageBody"]):
            return "picked"
        prs = [
            p
            for p in (node.get("associatedPullRequests") or {}).get("nodes") or []
            if p.get("merged")
            and p.get("baseRefName") == branch
            and (p.get("repository") or {}).get("nameWithOwner") == repo
        ]
        if not prs:
            return "bypassed"
        if len(prs) == 1:
            return "attributed"
        if (
            sum(
                1
                for p in prs
                if (p.get("mergeCommit") or {}).get("oid") == node["oid"]
            )
            == 1
        ):
            return "attributed"
        return "ambiguous"

    def label(self, row: Dict[str, Any]) -> str:
        return self.classify(json.loads(row["payload"]), row["branch"], row["name"])
