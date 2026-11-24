import re
from html import escape

from benchmark.mirror.markup import Markup


class Conversation:
    """Merges the collected comments, reviews, review threads, commits and timeline events of one issue or pull request into a single chronological list.

    Comments, reviews, review comments and commits were collected through paginated
    endpoints and are complete. The timeline was collected from its first page only,
    so it is used for the remaining event kinds and the closing and merging events are
    taken from the issue or pull request payload when the timeline does not reach them.
    """

    def __init__(self, markup: Markup, repo: str) -> None:
        self._markup = markup
        self._repo = repo
        self._issue = False

    def items(self, payload: dict, records: dict[str, list | dict | None]) -> list[dict]:
        """Return the conversation items of one issue or pull request ordered by time."""
        self._issue = "head" not in payload
        items: list[dict] = []
        threads = self._threads(records.get("review_comment") or [])
        attached: set[int] = set()

        for comment in records.get("comment") or []:
            items.append(
                {
                    "kind": "comment",
                    "time": comment.get("created_at") or "",
                    "user": self._login(comment.get("user")),
                    "association": comment.get("author_association"),
                    "html": self._markup.render(comment.get("body"), self._repo),
                    "id": comment.get("id"),
                }
            )

        for review in records.get("review") or []:
            own = [thread for thread in threads if thread["review"] == review.get("id")]
            attached.update(thread["id"] for thread in own)
            body = self._markup.render(review.get("body"), self._repo)

            if not body and not own and (review.get("state") or "").upper() == "COMMENTED":
                continue

            items.append(
                {
                    "kind": "review",
                    "time": review.get("submitted_at") or "",
                    "user": self._login(review.get("user")),
                    "state": (review.get("state") or "").lower(),
                    "html": body,
                    "threads": own,
                    "commit": review.get("commit_id"),
                    "id": review.get("id"),
                }
            )

        for thread in threads:
            if thread["id"] not in attached:
                items.append(
                    {
                        "kind": "review",
                        "time": thread["time"],
                        "user": thread["comments"][0]["user"],
                        "state": "commented",
                        "html": "",
                        "threads": [thread],
                        "commit": thread["commit"],
                        "id": None,
                    }
                )

        items.extend(self._commits(records.get("commit") or []))
        items.extend(self._events(payload, records.get("timeline") or []))
        items.sort(key=self._time)
        return self._group(items)

    def _created(self, comment: dict) -> str:
        return comment.get("created_at") or ""

    def _time(self, item: dict) -> str:
        return item["time"]

    def _login(self, user: dict | None) -> str:
        return (user or {}).get("login") or "ghost"

    def _threads(self, comments: list[dict]) -> list[dict]:
        roots: dict[int, dict] = {}
        order: list[int] = []

        for comment in sorted(comments, key=self._created):
            parent = comment.get("in_reply_to_id")
            entry = {
                "user": self._login(comment.get("user")),
                "time": comment.get("created_at") or "",
                "html": self._markup.render(comment.get("body"), self._repo),
                "id": comment.get("id"),
            }

            if parent and parent in roots:
                roots[parent]["comments"].append(entry)
                continue

            roots[comment["id"]] = {
                "id": comment["id"],
                "review": comment.get("pull_request_review_id"),
                "path": comment.get("path") or "",
                "line": comment.get("line") if comment.get("line") is not None else comment.get("original_line"),
                "commit": comment.get("commit_id") if comment.get("line") is not None else comment.get("original_commit_id"),
                "outdated": comment.get("position") is None and comment.get("line") is None,
                "hunk": self._hunk(comment.get("diff_hunk") or ""),
                "time": comment.get("created_at") or "",
                "comments": [entry],
            }
            order.append(comment["id"])

        return [roots[key] for key in order]

    def _hunk(self, text: str) -> list[dict]:
        lines = text.split("\n")
        header = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)", lines[0]) if lines else None
        old, new = (int(header.group(1)), int(header.group(2))) if header else (0, 0)
        result: list[dict] = []

        for line in lines[1:] if header else lines:
            origin = line[:1] if line[:1] in ("+", "-") else " "

            result.append(
                {
                    "origin": origin,
                    "old": None if origin == "+" else old,
                    "new": None if origin == "-" else new,
                    "text": line[1:] if line[:1] in ("+", "-", " ") else line,
                }
            )

            if origin != "+":
                old += 1

            if origin != "-":
                new += 1

        return result[-8:]

    def _commits(self, commits: list[dict]) -> list[dict]:
        result = []

        for commit in commits:
            data = commit.get("commit") or {}
            author = data.get("author") or {}
            result.append(
                {
                    "kind": "commit",
                    "time": author.get("date") or (data.get("committer") or {}).get("date") or "",
                    "sha": commit.get("sha"),
                    "headline": (data.get("message") or "").split("\n")[0],
                    "user": self._login(commit.get("author")) if commit.get("author") else author.get("name") or "",
                }
            )

        return result

    def _events(self, payload: dict, timeline: list[dict]) -> list[dict]:
        skipped = {"commented", "reviewed", "committed", "mentioned", "subscribed", "unsubscribed", "line-commented", "commit-commented"}
        events: list[dict] = []
        seen: set[str] = set()

        for event in timeline:
            kind = event.get("event") or ""

            if kind in skipped:
                continue

            text = self._describe(event)

            if text is None:
                continue

            seen.add(kind)
            events.append(
                {
                    "kind": "event",
                    "event": kind,
                    "time": event.get("created_at") or "",
                    "user": self._login(event.get("actor")),
                    "html": text,
                }
            )

        if payload.get("merged_at") and "merged" not in seen:
            merger = self._login(payload.get("merged_by")) if payload.get("merged_by") else ""
            sha = payload.get("merge_commit_sha")
            events.append(
                {
                    "kind": "event",
                    "event": "merged",
                    "time": payload["merged_at"],
                    "user": merger,
                    "html": f"merged commit {self._commit(sha)}" if sha else "merged this",
                }
            )
        elif payload.get("closed_at") and "closed" not in seen:
            reason = payload.get("state_reason")
            events.append(
                {
                    "kind": "event",
                    "event": "closed",
                    "time": payload["closed_at"],
                    "user": self._login(payload.get("closed_by")) if payload.get("closed_by") else "",
                    "html": f"closed this as {escape(reason.replace('_', ' '))}" if reason else "closed this",
                }
            )

        return events

    def _commit(self, sha: str | None) -> str:
        if not sha:
            return ""

        return f'<a class="sha" href="/{self._repo}/commit/{escape(sha)}">{escape(sha[:7])}</a>'

    def _describe(self, event: dict) -> str | None:
        kind = event.get("event") or ""
        label = event.get("label") or {}

        if kind in ("labeled", "unlabeled"):
            color = escape(label.get("color") or "888888")
            verb = "added" if kind == "labeled" else "removed"
            return f'{verb} the <span class="label" style="--label:#{color}">{escape(label.get("name") or "")}</span> label'

        if kind == "renamed":
            rename = event.get("rename") or {}
            return f"changed the title <del>{escape(rename.get('from') or '')}</del> {escape(rename.get('to') or '')}"

        if kind in ("review_requested", "review_request_removed"):
            target = event.get("requested_reviewer") or event.get("requested_team") or {}
            name = escape(target.get("login") or target.get("name") or "")
            verb = "requested a review from" if kind == "review_requested" else "removed the review request for"
            return f"{verb} <strong>{name}</strong>"

        if kind in ("assigned", "unassigned"):
            return f"{kind} <strong>{escape(self._login(event.get('assignee')))}</strong>"

        if kind in ("milestoned", "demilestoned"):
            title = escape((event.get("milestone") or {}).get("title") or "")
            verb = "added this to" if kind == "milestoned" else "removed this from"
            return f"{verb} the <strong>{title}</strong> milestone"

        if kind == "merged":
            return f"merged commit {self._commit(event.get('commit_id'))}"

        if kind == "closed":
            if event.get("commit_id"):
                return f"closed this in {self._commit(event.get('commit_id'))}"

            reason = event.get("state_reason")
            return f"closed this as {escape(reason.replace('_', ' '))}" if reason else "closed this"

        if kind == "referenced":
            sha = event.get("commit_id")
            url = event.get("commit_url") or ""
            own = f"/repos/{self._repo}/commits/".lower() in url.lower()
            reference = self._commit(sha) if own else f"<code>{escape((sha or '')[:7])}</code>"
            return f"added a commit {reference} that referenced this"

        if kind == "cross-referenced":
            source = (event.get("source") or {}).get("issue") or {}
            number = source.get("number")
            title = escape(source.get("title") or "")
            return f"mentioned this in <strong>{title}</strong> #{number}" if number else None

        if kind in ("connected", "disconnected") and event.get("subject"):
            subject = event["subject"]
            link = f'<a href="{escape(subject["href"])}">{escape(subject["label"])}</a>'

            if kind == "disconnected":
                return f"removed a link to {link}"

            if subject["kind"] == "issue":
                return f"linked an issue {link} that may be closed by this pull request"

            return f"linked a pull request {link} that will close this issue" if self._issue else f"linked a pull request {link}"

        if kind == "head_ref_force_pushed":
            return f"force-pushed the branch to {self._commit(event.get('commit_id'))}"

        return escape(kind.replace("_", " ").replace("-", " ")) + (" this" if kind in ("reopened", "locked", "unlocked", "pinned") else "")

    def _group(self, items: list[dict]) -> list[dict]:
        result: list[dict] = []

        for item in items:
            if item["kind"] == "commit" and result and result[-1]["kind"] == "commits":
                result[-1]["commits"].append(item)
                continue

            if item["kind"] == "commit":
                result.append({"kind": "commits", "time": item["time"], "user": item["user"], "commits": [item]})
                continue

            result.append(item)

        return result
