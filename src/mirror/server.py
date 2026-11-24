import argparse
import contextlib
import os
import hashlib
import mimetypes
import threading
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup as Safe
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from benchmark.mirror.code import DiffGap, Highlighter, Source
from benchmark.mirror.conversation import Conversation
from benchmark.mirror.icon import Identicon, Octicons
from benchmark.mirror.markup import Linker, Markup
from benchmark.mirror.store import MirrorStore


class Missing(Exception):
    """Raised when a page asks for something the collection does not contain."""

    def __init__(self, message: str, foreign: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.foreign = foreign


class MirrorFormat:
    """Jinja filters shared by the mirror templates."""

    def date(self, value: str | None) -> str:
        if not value:
            return ""

        return self._time(datetime.fromisoformat(value.replace("Z", "+00:00")))

    def stamp(self, value: datetime | int | None) -> str:
        if value is None:
            return ""

        if isinstance(value, int):
            value = datetime.fromtimestamp(value, timezone.utc)

        return self._time(value)

    def size(self, value: int | None) -> str:
        if value is None:
            return ""

        for unit in ("Bytes", "KB", "MB"):
            if value < 1024:
                return f"{value:.0f} {unit}" if unit == "Bytes" else f"{value:.1f} {unit}"

            value /= 1024

        return f"{value:.1f} GB"

    def anchor(self, path: str) -> str:
        return "diff-" + hashlib.sha256(path.encode("utf-8")).hexdigest()

    def days(self, commits: list) -> list[tuple[str, list]]:
        """Group commits by their author day in the given order, as GitHub's commit lists do."""
        groups: list[tuple[str, list]] = []

        for commit in commits:
            if isinstance(commit, dict):
                moment = datetime.fromisoformat(commit["commit"]["author"]["date"].replace("Z", "+00:00"))
            else:
                moment = datetime.fromtimestamp(commit.commit_time, timezone.utc)

            day = moment.strftime("%b %-d, %Y")

            if groups and groups[-1][0] == day:
                groups[-1][1].append(commit)
            else:
                groups.append((day, [commit]))

        return groups

    def window(self, page: int, pages: int) -> list[int | None]:
        """Return the page numbers a GitHub-style pager shows, with None for an ellipsis."""
        shown = sorted({1, 2, pages - 1, pages, page - 2, page - 1, page, page + 1, page + 2})
        result: list[int | None] = []

        for number in shown:
            if number < 1 or number > pages:
                continue

            if result and result[-1] is not None and number - result[-1] > 1:
                result.append(None)

            result.append(number)

        return result

    def _time(self, moment: datetime) -> str:
        full = moment.strftime("%b %-d, %Y, %-I:%M %p %Z").strip()
        return Safe(f'<time datetime="{moment.isoformat()}" title="{full}">{moment.strftime("%b %-d, %Y")}</time>')


class MirrorTree:
    """Folds the changed files of a diff into the nested file tree GitHub shows beside a diff."""

    def __init__(self, format: MirrorFormat) -> None:
        self._format = format

    def rows(self, files: list) -> list[dict]:
        """Return the tree as flat rows with a depth, directories first and single-child directories joined."""
        root: dict = {}

        for item in files:
            node = root

            for part in item.path.split("/")[:-1]:
                node = node.setdefault(part + "/", {})

            node[item.path.split("/")[-1]] = item

        rows: list[dict] = []
        self._walk(root, 0, rows)
        return rows

    def _walk(self, node: dict, depth: int, rows: list[dict]) -> None:
        folders = sorted(name for name in node if name.endswith("/"))
        leaves = sorted(name for name in node if not name.endswith("/"))

        for name in folders:
            child, label = node[name], name[:-1]

            while len(child) == 1 and next(iter(child)).endswith("/"):
                inner = next(iter(child))
                label, child = f"{label}/{inner[:-1]}", child[inner]

            rows.append({"depth": depth, "name": label, "folder": True})
            self._walk(child, depth + 1, rows)

        for name in leaves:
            item = node[name]
            rows.append(
                {"depth": depth, "name": name, "folder": False, "anchor": self._format.anchor(item.path), "status": item.status}
            )


class MirrorServer:
    """Read-only web front end over the mirror root written by ``MirrorArchive``.

    URLs follow GitHub's layout below ``/<owner>/<repo>``, so a github.com link of a
    mirrored repository maps onto the mirror by replacing the host.
    """

    def __init__(self, root: Path, threads: int = 4) -> None:
        import pygit2

        # libgit2 keeps an object cache and mapped pack windows per repository handle;
        # both are capped so the server fits next to the annotation stack.
        pygit2.settings.cache_max_size(64 * 1024 * 1024)
        pygit2.settings.mwindow_mapped_limit = 256 * 1024 * 1024
        self._root = root
        self._threads = threads
        self._store = MirrorStore(root / "mirror.sqlite")
        self._repos = [row["name"] for row in self._store.repositories()]
        self._meta = self._store.meta()
        self._local = threading.local()
        self._resolved: dict[tuple[str, str], str | None] = {}
        self._linker = Linker(self._repos, self.resolve)
        self._highlighter = Highlighter()
        self._format = MirrorFormat()
        here = Path(__file__).parent
        self._templates = Environment(
            loader=FileSystemLoader(here / "templates"),
            autoescape=select_autoescape(["html"]),
        )
        self._templates.filters.update(
            date=self._format.date,
            stamp=self._format.stamp,
            size=self._format.size,
            anchor=self._format.anchor,
            days=self._format.days,
            window=self._format.window,
        )
        self._templates.globals.update(
            meta=self._meta,
            repos=self._repos,
            icon=Octicons(here / "static" / "octicons.json"),
            avatar=Identicon(),
        )
        self._tree = MirrorTree(self._format)
        self.app = Starlette(
            routes=[
                Route("/", self.index),
                Route("/robots.txt", self.robots),
                Mount("/static", StaticFiles(directory=here / "static"), name="static"),
                Route("/{owner}/{name}", self.home),
                Route("/{owner}/{name}/pulls", self.pulls),
                Route("/{owner}/{name}/issues", self.issues),
                Route("/{owner}/{name}/pull/{number:int}", self.pull),
                Route("/{owner}/{name}/pull/{number:int}/{tab:str}", self.pull),
                Route("/{owner}/{name}/pull/{number:int}/commits/{sha:str}", self.member),
                Route("/{owner}/{name}/pulls/{number:int}", self.alias),
                Route("/{owner}/{name}/issues/{number:int}", self.issue),
                Route("/{owner}/{name}/commit/{sha:str}", self.commit),
                Route("/{owner}/{name}/commits", self.log),
                Route("/{owner}/{name}/commits/{spec:path}", self.log),
                Route("/{owner}/{name}/tree/{spec:path}", self.tree),
                Route("/{owner}/{name}/blob/{spec:path}", self.blob),
                Route("/{owner}/{name}/raw/{spec:path}", self.raw),
                Route("/{owner}/{name}/compare/{spec:path}", self.compare),
                Route("/{owner}/{name}/expand/{spec:path}", self.expand),
            ],
            exception_handlers={Missing: self.missing, 404: self.unknown},
            middleware=[Middleware(GZipMiddleware, minimum_size=1024)],
            lifespan=self.lifespan,
        )

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Starlette):
        """Limit the worker threads, since every thread keeps its own repository handles."""
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = self._threads
        yield

    def resolve(self, repo: str, sha: str) -> str | None:
        """Return the full SHA of an abbreviated commit SHA of a mirrored repository."""
        key = (repo, sha)

        if key not in self._resolved:
            commit = self._source(repo).commit(sha)
            full = str(commit.id) if commit is not None else None
            self._resolved[key] = full if full and full.startswith(sha.lower()) else None

        return self._resolved[key]

    def _source(self, repo: str) -> Source:
        sources = getattr(self._local, "sources", None)

        if sources is None:
            sources = self._local.sources = {}

        if repo not in sources:
            sources[repo] = Source(self._root / f"{repo}.git")

        return sources[repo]

    def _repo(self, request: Request) -> dict:
        owner, name = request.path_params["owner"], request.path_params["name"]
        repository = self._store.repository(f"{owner}/{name}")

        if repository is None:
            raise Missing(f"The repository {owner}/{name} is not part of the mirror.", foreign=True)

        return repository

    def _canonical(self, request: Request, repository: dict) -> Response | None:
        spelled = f"{request.path_params['owner']}/{request.path_params['name']}"

        if spelled == repository["name"]:
            return None

        path = request.url.path.replace(f"/{spelled}", f"/{repository['name']}", 1)
        return RedirectResponse(path + (f"?{request.url.query}" if request.url.query else ""), 301)

    def _render(self, template: str, status: int = 200, **context) -> HTMLResponse:
        page = self._templates.get_template(template).render(**context)
        return HTMLResponse(page, status_code=status, headers={"X-Robots-Tag": "noindex, nofollow"})

    def _markup(self, repo: str) -> Markup:
        return Markup(self._linker)


    def missing(self, request: Request, error: Missing) -> Response:
        if error.foreign:
            return self._original(request)

        return self._render("missing.html", status=404, message=error.message, repo=None)

    def unknown(self, request: Request, error: Exception) -> Response:
        parts = request.url.path.strip("/").split("/")

        # Pages outside the mirrored repositories, such as other repositories or user profiles,
        # still exist on GitHub, so links rewritten to the mirror keep working for them.
        if parts[0] != "static" and (len(parts) < 2 or self._store.repository("/".join(parts[:2])) is None):
            return self._original(request)

        return self._render("missing.html", status=404, message="This page does not exist in the mirror.", repo=None)

    def _original(self, request: Request) -> Response:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"https://github.com{request.url.path}{query}", 302)

    def robots(self, request: Request) -> Response:
        """Admit only the fetchers that chat assistants use on a user's behalf and keep crawlers out."""
        rules = "User-agent: Claude-User\nAllow: /\n\nUser-agent: ChatGPT-User\nAllow: /\n\nUser-agent: *\nDisallow: /\n"
        return PlainTextResponse(rules)

    def index(self, request: Request) -> Response:
        url = request.query_params.get("url", "").strip()

        if url:
            target = self._linker.href(url)

            if target.startswith("/"):
                return RedirectResponse(target, 302)

        return self._render(
            "index.html",
            repositories=self._store.repositories(),
            repo=None,
            url=url,
        )

    def home(self, request: Request) -> Response:
        repository = self._repo(request)
        redirect = self._canonical(request, repository)

        if redirect:
            return redirect

        source = self._source(repository["name"])
        commit = source.commit(repository["head"])
        readme = source.readme(commit, "")
        markup = self._markup(repository["name"])
        return self._render(
            "tree.html",
            repo=repository,
            tab="code",
            commit=commit,
            ref=repository["branch"],
            path="",
            crumbs=[],
            entries=source.tree(commit, ""),
            readme=(readme[0], Safe(markup.render(readme[1], repository["name"]))) if readme else None,
            latest=source.log(commit, "", 0, 1)[0],
        )

    def _list(self, request: Request, kind: str) -> Response:
        repository = self._repo(request)
        redirect = self._canonical(request, repository)

        if redirect:
            return redirect

        state = request.query_params.get("state", "")
        query = request.query_params.get("q", "").strip()
        page = max(1, int(request.query_params.get("page", "1") or 1))
        rows, total = self._store.entries(repository["name"], kind, state, query, page)

        for row in rows:
            payload = self._store.record(repository["name"], "pr" if kind == "pull" else "issue", row["number"]) or {}
            row["labels"] = payload.get("labels") or []
            row["reason"] = payload.get("state_reason")

        return self._render(
            "list.html",
            repo=repository,
            tab="pulls" if kind == "pull" else "issues",
            kind=kind,
            rows=rows,
            total=total,
            page=page,
            pages=max(1, (total + 49) // 50),
            counts=self._store.counts(repository["name"], kind, query),
            state=state,
            query=query,
        )

    def pulls(self, request: Request) -> Response:
        return self._list(request, "pull")

    def issues(self, request: Request) -> Response:
        return self._list(request, "issue")

    def alias(self, request: Request) -> Response:
        repository = self._repo(request)
        return RedirectResponse(f"/{repository['name']}/pull/{request.path_params['number']}", 301)

    def _records(self, repo: str, number: int, kinds: list[str]) -> dict:
        return {kind: self._store.record(repo, kind, number) for kind in kinds}

    def issue(self, request: Request) -> Response:
        repository = self._repo(request)
        repo, number = repository["name"], request.path_params["number"]
        entry = self._store.entry(repo, number)

        if entry is None:
            raise Missing(f"#{number} is not part of the collected data of {repo}. The collection only holds issues and pull requests that were closed during the collection window.")

        if entry["kind"] == "pull":
            return RedirectResponse(f"/{repo}/pull/{number}", 302)

        redirect = self._canonical(request, repository)

        if redirect:
            return redirect

        payload = self._store.record(repo, "issue", number)
        markup = self._markup(repo)
        records = self._records(repo, number, ["comment", "timeline"])
        truncated = len(records.get("timeline") or []) == 30
        records["timeline"] = self._history(repo, number, records.get("timeline"))
        return self._render(
            "issue.html",
            repo=repository,
            tab="issues",
            entry=entry,
            payload=payload,
            body=Safe(markup.render(payload.get("body"), repo)),
            items=self._safe(Conversation(markup, repo).items(payload, records)),
            links=self._edges(repo, number),
            truncated=truncated,
        )

    def _safe(self, items: list[dict]) -> list[dict]:
        for item in items:
            if "html" in item:
                item["html"] = Safe(item["html"])

            for thread in item.get("threads", []):
                for comment in thread["comments"]:
                    comment["html"] = Safe(comment["html"])

        return items

    def _edges(self, repo: str, number: int) -> list[dict]:
        result = []

        for edge in self._store.edges(repo, number):
            outgoing = edge["repo"] == repo and edge["number"] == number
            other = (edge["target"], edge["related"]) if outgoing else (edge["repo"], edge["number"])

            if other == (repo, number):
                continue

            local = other[0] in self._repos
            result.append(
                {
                    "kind": edge["kind"],
                    "close": bool(edge["close"]),
                    "repo": other[0],
                    "number": other[1],
                    "href": f"/{other[0]}/issues/{other[1]}" if local else f"https://github.com/{other[0]}/issues/{other[1]}",
                    "title": (self._store.entry(other[0], other[1]) or {}).get("title") if local else None,
                    "outgoing": outgoing,
                }
            )

        unique: dict[tuple[str, int], dict] = {}

        for item in result:
            key = (item["repo"], item["number"])

            if key not in unique:
                unique[key] = {**item, "kinds": []}

            kind = item["kind"].replace("_", " ")

            if kind not in unique[key]["kinds"]:
                unique[key]["kinds"].append(kind)

            unique[key]["close"] = unique[key]["close"] or item["close"]
            unique[key]["outgoing"] = unique[key]["outgoing"] or item["outgoing"]

        return list(unique.values())

    def pull(self, request: Request) -> Response:
        repository = self._repo(request)
        repo, number = repository["name"], request.path_params["number"]
        tab = request.path_params.get("tab", "conversation")
        entry = self._store.entry(repo, number)

        if entry is None:
            raise Missing(f"#{number} is not part of the collected data of {repo}. The collection only holds issues and pull requests that were closed during the collection window.")

        if entry["kind"] == "issue":
            return RedirectResponse(f"/{repo}/issues/{number}", 302)

        if tab not in ("conversation", "commits", "files"):
            raise Missing(f"The pull request tab {tab} is not available in the mirror.")

        redirect = self._canonical(request, repository)

        if redirect:
            return redirect

        payload = self._store.record(repo, "pr", number)
        markup = self._markup(repo)
        context = {
            "repo": repository,
            "tab": "pulls",
            "entry": entry,
            "payload": payload,
            "view": tab,
            "commits": self._store.record(repo, "commit", number) or [],
        }

        if tab == "conversation":
            records = self._records(repo, number, ["comment", "review", "review_comment", "commit", "timeline"])
            truncated = len(records.get("timeline") or []) == 30
            records["timeline"] = self._history(repo, number, records.get("timeline"))
            context.update(
                body=Safe(markup.render(payload.get("body"), repo)),
                items=self._safe(Conversation(markup, repo).items(payload, records)),
                links=self._edges(repo, number),
                truncated=truncated,
                reviewers=self._reviewers(payload, records),
            )

        if tab == "files":
            context.update(self._changes(repo, payload, request.query_params.get("path")))

        return self._render("pull.html", **context)

    def _history(self, repo: str, number: int, timeline: list[dict] | None) -> list[dict]:
        """Complete the collected timeline with the reference events of the edge asset.

        The timeline was collected from its first page only, while the edge asset paginated
        the commit reference and link events of every pull request on its own. Edge events
        replace timeline events of the same kind and time, since they also name the linked item.
        """
        events = list(timeline or [])

        for edge in self._store.edges(repo, number):
            outgoing = edge["repo"] == repo and edge["number"] == number

            if not edge["creation"]:
                continue

            if edge["kind"] == "commit_referenced" and outgoing and edge["sha"]:
                if any(item.get("event") == "referenced" and item.get("created_at") == edge["creation"] for item in events):
                    continue

                own = self.resolve(repo, edge["sha"]) is not None
                events.append(
                    {
                        "event": "referenced",
                        "created_at": edge["creation"],
                        "actor": {"login": edge["actor"]},
                        "commit_id": edge["sha"],
                        "commit_url": f"https://api.github.com/repos/{repo}/commits/{edge['sha']}" if own else "",
                    }
                )

            if edge["kind"] in ("connected", "disconnected"):
                other = (edge["target"], edge["related"]) if outgoing else (edge["repo"], edge["number"])
                local = other[0] in self._repos
                found = self._store.entry(other[0], other[1]) if local else None
                kind = "pr" if not outgoing or (found and found["kind"] == "pull") else "issue"
                events = [item for item in events if (item.get("event"), item.get("created_at")) != (edge["kind"], edge["creation"])]
                events.append(
                    {
                        "event": edge["kind"],
                        "created_at": edge["creation"],
                        "actor": {"login": edge["actor"]},
                        "subject": {
                            "kind": kind,
                            "href": f"/{other[0]}/issues/{other[1]}" if local else f"https://github.com/{other[0]}/issues/{other[1]}",
                            "label": f"#{other[1]}" if other[0] == repo else f"{other[0]}#{other[1]}",
                        },
                    }
                )

        return events

    def _reviewers(self, payload: dict, records: dict) -> list[dict]:
        """Rebuild GitHub's reviewer sidebar: everyone who submitted a review, plus the still pending review requests.

        Approval and change requests outrank later plain comments, a pending re-request outranks
        an earlier review, and the pull request author is never listed, as on GitHub.
        """
        author = (payload.get("user") or {}).get("login")
        states: dict[str, str] = {}

        for review in sorted(records.get("review") or [], key=self._submitted):
            name = (review.get("user") or {}).get("login")
            # GitHub requests Copilot reviews as "Copilot" but submits them from its bot account.
            name = "Copilot" if name == "copilot-pull-request-reviewer[bot]" else name
            state = (review.get("state") or "").lower()

            if not name or name == author or state == "pending":
                continue

            if state in ("approved", "changes_requested", "dismissed") or states.get(name) in (None, "dismissed"):
                states[name] = state

        for target in (payload.get("requested_reviewers") or []) + (payload.get("requested_teams") or []):
            name = target.get("login") or target.get("name")

            if name and name != author:
                states[name] = "requested"

        return [{"login": name, "state": state} for name, state in states.items()]

    def _submitted(self, review: dict) -> str:
        return review.get("submitted_at") or ""

    def _changes(self, repo: str, payload: dict, only: str | None) -> dict:
        source = self._source(repo)
        head = source.commit(payload["head"]["sha"]) or source.commit(f"pull/{payload['number']}/head")
        base = source.base(payload["base"]["sha"], str(head.id)) if head is not None else None

        if head is None or base is None:
            merge = source.commit(payload.get("merge_commit_sha") or "")

            if merge is None or not merge.parents:
                return {"files": None, "base": None, "head": None}

            head, base = merge, merge.parents[0]

        files = self._diff(source, base, head, only)
        return {"files": files, "tree": self._tree.rows(files), "base": base, "head": head, "only": only}

    def _diff(self, source: Source, base, head, only: str | None) -> list:
        files = source.diff(base, head)
        budget = 20000

        for item in files:
            size = sum(len(hunk.lines) for hunk in item.hunks)
            item.deferred = (item.path != only) if only else (size > 3000 or size > budget)

            if item.deferred:
                item.hunks = []
                continue

            budget -= size
            source.paint(item, self._highlighter)

        return files

    def member(self, request: Request) -> Response:
        repository = self._repo(request)
        return RedirectResponse(f"/{repository['name']}/commit/{request.path_params['sha']}", 302)

    def commit(self, request: Request) -> Response:
        repository = self._repo(request)
        repo = repository["name"]
        source = self._source(repo)
        commit = source.commit(request.path_params["sha"])

        if commit is None:
            raise Missing(f"The commit {request.path_params['sha']} is not part of the collected repository of {repo}.")

        redirect = self._canonical(request, repository)

        if redirect:
            return redirect

        parent = commit.parents[0] if commit.parents else None
        files = self._diff(source, parent, commit, request.query_params.get("path"))
        message = commit.message.split("\n", 1)
        markup = self._markup(repo)
        return self._render(
            "commit.html",
            repo=repository,
            tab="code",
            commit=commit,
            headline=message[0],
            body=Safe(markup.render(message[1].strip(), repo)) if len(message) > 1 and message[1].strip() else "",
            pulls=self._store.pulls(repo, str(commit.id)),
            files=files,
            tree=self._tree.rows(files),
            only=request.query_params.get("path"),
        )

    def compare(self, request: Request) -> Response:
        repository = self._repo(request)
        repo = repository["name"]
        source = self._source(repo)
        spec = request.path_params["spec"]
        left, _, right = spec.partition("...") if "..." in spec else spec.partition("..")
        head = source.commit(right)
        base = source.base(left, right) if head is not None else None

        if head is None or base is None:
            raise Missing(f"The comparison {spec} cannot be resolved in the collected repository of {repo}.")

        files = self._diff(source, base, head, request.query_params.get("path"))
        return self._render(
            "compare.html",
            repo=repository,
            tab="code",
            spec=spec,
            base=base,
            head=head,
            files=files,
            tree=self._tree.rows(files),
            only=request.query_params.get("path"),
        )

    def expand(self, request: Request) -> Response:
        """Return the hidden unchanged lines of a diff gap as table rows for the expander in diff views."""
        repository = self._repo(request)
        source, commit, ref, path = self._locate(request, repository)

        if source.kind(commit, path) != "blob":
            raise Missing(f"{path} does not exist at {ref} in {repository['name']}.")

        query = request.query_params
        gap = DiffGap(int(query.get("start", "1")), int(query.get("end", "0")), int(query.get("shift", "0")))
        lines = source.context(commit, path, gap, self._highlighter)
        return self._render("expand.html", lines=lines, anchor=self._format.anchor(path))

    def _locate(self, request: Request, repository: dict):
        source = self._source(repository["name"])
        spec = request.path_params.get("spec") or repository["branch"]
        found = source.split(spec)

        if found is None:
            raise Missing(f"The revision in {spec} is not part of the collected repository of {repository['name']}.")

        commit, path = found
        ref = spec[: len(spec) - len(path)].strip("/") if path else spec.strip("/")
        return source, commit, ref, path

    def _crumbs(self, path: str) -> list[tuple[str, str]]:
        parts = path.split("/") if path else []
        return [(part, "/".join(parts[: index + 1])) for index, part in enumerate(parts)]

    def tree(self, request: Request) -> Response:
        repository = self._repo(request)
        source, commit, ref, path = self._locate(request, repository)
        kind = source.kind(commit, path)

        if kind is None:
            raise Missing(f"{path} does not exist at {ref} in {repository['name']}.")

        if kind == "blob":
            return RedirectResponse(f"/{repository['name']}/blob/{ref}/{path}", 302)

        readme = source.readme(commit, path)
        markup = self._markup(repository["name"])
        return self._render(
            "tree.html",
            repo=repository,
            tab="code",
            commit=commit,
            ref=ref,
            path=path,
            crumbs=self._crumbs(path),
            entries=source.tree(commit, path),
            readme=(readme[0], Safe(markup.render(readme[1], repository["name"]))) if readme else None,
            latest=(source.log(commit, path, 0, 1) or [commit])[0],
        )

    def blob(self, request: Request) -> Response:
        repository = self._repo(request)
        source, commit, ref, path = self._locate(request, repository)
        kind = source.kind(commit, path)

        if kind is None:
            raise Missing(f"{path} does not exist at {ref} in {repository['name']}.")

        if kind == "tree":
            return RedirectResponse(f"/{repository['name']}/tree/{ref}/{path}", 302)

        data = source.blob(commit, path)
        guess = mimetypes.guess_type(path)[0] or ""
        binary = b"\0" in data[:8000]
        lines = None
        rendered = None

        if not binary:
            text = data.decode("utf-8", "replace")
            lines = self._highlighter.lines(text, path)

            if path.lower().endswith((".md", ".markdown")) and request.query_params.get("plain") != "1":
                rendered = Safe(self._markup(repository["name"]).render(text, repository["name"]))

        return self._render(
            "blob.html",
            repo=repository,
            tab="code",
            commit=commit,
            ref=ref,
            path=path,
            crumbs=self._crumbs(path),
            size=len(data),
            lines=[Safe(line) for line in lines] if lines is not None else None,
            image=guess.startswith("image/"),
            rendered=rendered,
            latest=(source.log(commit, path, 0, 1) or [commit])[0],
        )

    def raw(self, request: Request) -> Response:
        repository = self._repo(request)
        source, commit, ref, path = self._locate(request, repository)

        if source.kind(commit, path) != "blob":
            raise Missing(f"{path} does not exist at {ref} in {repository['name']}.")

        data = source.blob(commit, path)
        guess = mimetypes.guess_type(path)[0] or ""
        kind = guess if guess.startswith("image/") and guess != "image/svg+xml" else "text/plain; charset=utf-8"

        if b"\0" in data[:8000] and not guess.startswith("image/"):
            kind = "application/octet-stream"

        return Response(
            data,
            media_type=kind,
            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
        )

    def log(self, request: Request) -> Response:
        repository = self._repo(request)
        source, commit, ref, path = self._locate(request, repository)
        page = max(1, int(request.query_params.get("page", "1") or 1))
        commits = source.log(commit, path, (page - 1) * 50, 51)
        return self._render(
            "log.html",
            repo=repository,
            tab="code",
            ref=ref,
            path=path,
            commits=commits[:50],
            page=page,
            more=len(commits) > 50,
        )


class MirrorServerCommand:
    """Command line entry point that serves a mirror root."""

    def run(self, arguments: list[str] | None = None) -> None:
        import uvicorn

        parser = argparse.ArgumentParser(description=MirrorServer.__doc__)
        parser.add_argument("root", type=Path)
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
        options = parser.parse_args(arguments)
        app = MirrorServer(options.root).app
        uvicorn.run(app, host=options.host, port=options.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    MirrorServerCommand().run()
