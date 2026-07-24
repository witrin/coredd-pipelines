"""Export the raw PR and issue assets into per-object atomic event streams.

The benchmark hands each candidate a case: a repository and a cutoff date. The
candidate must not see the future, so only PR and issue activity that provably
happened before the cutoff may be visible. To make that an un-leakable filter for
the consumer (``time < D``), this script flattens the raw GitHub data into atomic,
individually timestamped events, one JSON object per line, stored per PR or issue.

The source is two Dagster raw assets in GCS, ``scope-change-raw`` (pull requests)
and ``scope-issue-raw`` (issues). Both share a long, tagged schema with columns
``name`` (owner/repo), ``type``, ``number``, and ``payload``, where ``payload`` is
raw GitHub REST JSON: a single object for the head fragment, or an array of objects
for commits, reviews, comments, review requests, and the timeline. Every fragment
of one ``(repo, number)`` is collected into one partition, so the export processes
one partition at a time and never merges across partitions.

Output layout, one file per object, one line per atomic event, lines sorted by
``time`` ascending:

    <outdir>/owner/repository/<number>.jsonl
    <outdir>/events.parquet
    <outdir>/commits.parquet
    <outdir>/README.md

Each line has exactly three fields, ``time`` (ISO-8601 UTC), ``type``, and
``payload``. The payload is the raw fragment element, unmodified: no value is
removed, mutated, or added, and ``time`` and ``type`` live only on the outer line,
never inside the payload. ``time`` is always present; any event that cannot be
dated is dropped and counted.

Event resolution. Each source row expands to zero or more events. The resulting
``type`` mirrors the source; the head object keeps its native ``pr`` or ``issue``
type. Pull requests carry seven fragment types: ``pr`` dated by ``created_at``;
``commit`` (array) dated by ``commit.author.date``; ``review`` (array) dated by
``submitted_at``; ``review_comment`` and ``comment`` (arrays) dated by
``created_at``; ``review_request`` (see below); and ``timeline`` (array), each
event dated by ``created_at`` except ``committed`` (dated by ``author.date``) and
``reviewed`` (dated by ``submitted_at``), which lack ``created_at`` but carry their
time nested. Issues carry three fragment types: ``issue`` dated by ``created_at``,
``comment`` (array) dated by ``created_at``, and ``timeline`` (array) dated by
``created_at``; the issue timeline has no ``committed`` or ``reviewed`` events.

Review requests. The ``review_request`` fragment is a snapshot of currently
requested reviewers, ``{users, teams}``, with no event time of its own. It is kept
as its own type and split per user and per team; the payload is the single user or
team object. The time is the earliest ``created_at`` among this object's timeline
``review_requested`` events whose ``requested_reviewer.login`` (user) or
``requested_team.slug``/``name`` (team) matches. When no such timeline event
exists, the time falls back to the head object's ``created_at``.

Known limitation. The ``pr`` and ``issue`` head event is dated to ``created_at``
but its payload is the object's final snapshot, so it carries fields whose real
timestamps may be after ``created_at`` (``state``, ``updated_at``, ``closed_at``,
``merged_at``, ``merge_commit_sha``, final counts, labels). A consumer filtering
``time < D`` admits the head event yet can read end-state information from it that
post-dates the cutoff. This localized leak is accepted, confined to the single head
event per object, and documented in the emitted README; time-accurate state
transitions are available as the atomic ``timeline:*`` events. Every other event's
payload is point-in-time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage

from benchmark_notebook.io import CloudStorage


class Progress:
    """Report per-asset partition progress to stderr."""

    def asset(self, key: str, done: int, total: int) -> None:
        """Report that a partition of an asset has been processed."""
        print(f"{key}: partition {done}/{total}", file=sys.stderr, flush=True)


class Partition:
    """One enumerated asset partition identified by its partition token."""

    def __init__(self, key: str, version: str, location: str, token: str) -> None:
        self.key = key
        self.version = version
        self.location = location
        self.token = token


class Catalog:
    """Enumerate the partitions of one asset at a pinned version."""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        client = storage.Client(project=project)
        self._bucket = client.bucket(bucket)

    def partitions(self, key: str, version: str, location: str) -> list[Partition]:
        """Return the partitions of an asset for one pinned version and location."""
        prefix = f"assets/{key}/version={version}/location={location}/partition="
        tokens = set()
        for blob in self._bucket.list_blobs(prefix=prefix):
            if not blob.name.endswith("data.parquet"):
                continue
            token = blob.name.split("partition=")[1].split("/")[0]
            tokens.add(token)
        return [Partition(key, version, location, token) for token in sorted(tokens)]


class Source:
    """Materialize and scan a single partition at a time."""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        self._store = CloudStorage(bucket=bucket, project=project)

    def rows(self, partition: Partition) -> list[dict]:
        """Return the rows of one partition as name, type, number, payload dicts."""
        frame = self._store.scan(
            key=partition.key,
            version=partition.version,
            location=partition.location,
            partition=partition.token,
        )
        return frame.select("name", "type", "number", "payload").collect().to_dicts()

    def attribution(self, partition: Partition) -> list[dict]:
        """Return the trunk commits of one commit-attribution partition."""
        frame = self._store.scan(
            key=partition.key,
            version=partition.version,
            location=partition.location,
            partition=partition.token,
        )
        return frame.select("name", "identifier", "payload").collect().to_dicts()


class Clock:
    """Normalize GitHub timestamps to canonical ISO-8601 UTC."""

    def iso(self, raw: str | None) -> str | None:
        """Return a canonical UTC timestamp, or None when the input is undatable."""
        if not raw:
            return None
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment.astimezone(timezone.utc).isoformat()


class Event:
    """One atomic event: a resolved time, a type, and the raw payload element."""

    def __init__(self, time: str, type: str, payload: object) -> None:
        self.time = time
        self.type = type
        self.payload = payload

    def line(self) -> str:
        """Return the JSONL line for this event."""
        return json.dumps({"time": self.time, "type": self.type, "payload": self.payload})


class Resolver:
    """Resolve source fragments into atomic, individually dated events."""

    def __init__(self) -> None:
        self._clock = Clock()
        self.dropped = 0

    def events(
        self, payloads: dict[str, object], head_time: str | None
    ) -> Iterator[Event]:
        """Yield the atomic events of one object from its fragments by type."""
        requests = self._requests(payloads.get("timeline"))
        for fragment, payload in payloads.items():
            yield from self._fragment(fragment, payload, requests, head_time)

    def _fragment(
        self, fragment: str, payload: object, requests: dict, head_time: str
    ) -> Iterator[Event]:
        if fragment in ("pr", "issue"):
            yield from self._head(fragment, payload)
        elif fragment == "commit":
            yield from self._commit(payload)
        elif fragment == "review":
            yield from self._review(payload)
        elif fragment in ("comment", "review_comment"):
            yield from self._comment(fragment, payload)
        elif fragment == "review_request":
            yield from self._request(payload, requests, head_time)
        elif fragment == "timeline":
            yield from self._timeline(payload)

    def _head(self, fragment: str, payload: object) -> Iterator[Event]:
        time = self._clock.iso(payload.get("created_at"))
        if time is None:
            self.dropped += 1
            return
        yield Event(time, fragment, payload)

    def _commit(self, payload: object) -> Iterator[Event]:
        for item in payload:
            time = self._clock.iso(item.get("commit", {}).get("author", {}).get("date"))
            if time is None:
                self.dropped += 1
                continue
            yield Event(time, "commit", item)

    def _review(self, payload: object) -> Iterator[Event]:
        for item in payload:
            time = self._clock.iso(item.get("submitted_at"))
            if time is None:
                self.dropped += 1
                continue
            yield Event(time, "review", item)

    def _comment(self, fragment: str, payload: object) -> Iterator[Event]:
        for item in payload:
            time = self._clock.iso(item.get("created_at"))
            if time is None:
                self.dropped += 1
                continue
            yield Event(time, fragment, item)

    def _request(self, payload: object, requests: dict, head_time: str | None) -> Iterator[Event]:
        for user in payload.get("users", []):
            time = requests.get(("user", user.get("login")), head_time)
            if time is None:
                self.dropped += 1
                continue
            yield Event(time, "review_request", user)
        for team in payload.get("teams", []):
            handle = team.get("slug") or team.get("name")
            time = requests.get(("team", handle), head_time)
            if time is None:
                self.dropped += 1
                continue
            yield Event(time, "review_request", team)

    def _timeline(self, payload: object) -> Iterator[Event]:
        for item in payload:
            event = item.get("event")
            if event == "committed":
                raw = item.get("author", {}).get("date")
            elif event == "reviewed":
                raw = item.get("submitted_at")
            else:
                raw = item.get("created_at")
            time = self._clock.iso(raw)
            if time is None:
                self.dropped += 1
                continue
            yield Event(time, f"timeline:{event}", item)

    def _requests(self, timeline: object) -> dict:
        found: dict = {}
        if timeline is None:
            return found
        for item in timeline:
            if item.get("event") != "review_requested":
                continue
            time = self._clock.iso(item.get("created_at"))
            if time is None:
                continue
            reviewer = item.get("requested_reviewer")
            team = item.get("requested_team")
            if reviewer:
                self._earliest(found, ("user", reviewer.get("login")), time)
            if team:
                self._earliest(found, ("team", team.get("slug") or team.get("name")), time)
        return found

    def _earliest(self, found: dict, handle: tuple, time: str) -> None:
        if handle not in found or time < found[handle]:
            found[handle] = time


class Writer:
    """Write one object's sorted events to owner/repo/number.jsonl atomically."""

    def write(self, outdir: Path, name: str, number: int, events: list[Event]) -> int:
        """Write the events and return the number of lines written."""
        owner, repo = name.split("/", 1)
        directory = outdir / owner / repo
        directory.mkdir(parents=True, exist_ok=True)
        ordered = sorted(events, key=lambda event: event.time)
        path = directory / f"{number}.jsonl"
        temp = directory / f".{number}.jsonl.tmp"
        temp.write_text("".join(f"{event.line()}\n" for event in ordered), encoding="utf-8")
        temp.replace(path)
        return len(ordered)


class Index:
    """Accumulate a small convenience index of every emitted event."""

    def __init__(self) -> None:
        self._rows: list[dict] = []

    def add(self, repository: str, number: int, events: list[Event]) -> None:
        """Record one row per event of an already written and sorted object."""
        ordered = sorted(events, key=lambda event: event.time)
        for line, event in enumerate(ordered):
            self._rows.append(
                {
                    "repository": repository,
                    "issue": number,
                    "time": event.time,
                    "type": event.type,
                    "line": line,
                }
            )

    def write(self, outdir: Path) -> None:
        """Write the accumulated index to events.parquet."""
        table = pa.Table.from_pylist(
            self._rows,
            schema=pa.schema(
                [
                    ("repository", pa.string()),
                    ("issue", pa.int64()),
                    ("time", pa.string()),
                    ("type", pa.string()),
                    ("line", pa.int64()),
                ]
            ),
        )
        pq.write_table(table, outdir / "events.parquet")


class Commits:
    """Map every trunk commit to the pull request it belongs to.

    The mapping comes from ``scope-commit-attribution``, which walks the first-parent
    trunk and asks GitHub which pull requests a commit is associated with. That is the
    only sound direction: a pull request's own commit list reports every commit
    reachable in its branch, so a sync or merge request would claim commits belonging
    to other changes.
    """

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._seen: set[tuple[str, str]] = set()

    def add(self, rows: list[dict], keep: set[str]) -> None:
        """Record one row per trunk commit of a repository in the kept set."""
        for row in rows:
            if keep and row["name"] not in keep:
                continue
            if not row["payload"]:
                continue
            marker = (row["name"], row["identifier"])
            if marker in self._seen:
                continue
            self._seen.add(marker)
            node = json.loads(row["payload"])
            requests = (node.get("associatedPullRequests") or {}).get("nodes") or []
            for request in requests:
                owner = (request.get("repository") or {}).get("nameWithOwner")
                if request.get("merged") and owner == row["name"]:
                    self._rows.append(
                        {
                            "repository": row["name"],
                            "pr": request["number"],
                            "sha": row["identifier"],
                        }
                    )

    def write(self, outdir: Path) -> None:
        """Write the accumulated mapping to commits.parquet."""
        table = pa.Table.from_pylist(
            self._rows,
            schema=pa.schema(
                [
                    ("repository", pa.string()),
                    ("pr", pa.int64()),
                    ("sha", pa.string()),
                ]
            ),
        )
        pq.write_table(table, outdir / "commits.parquet")


class Readme:
    """Render the dataset-card documentation for one export run."""

    def write(self, outdir: Path, stats: dict) -> None:
        """Write README.md describing the layout, semantics, and run statistics."""
        text = "\n".join(
            [
                "# PR and issue event streams",
                "",
                "Atomic, individually timestamped events for pull requests and issues, "
                "one JSON object per line, stored per object. Derived from the "
                "`scope-change-raw` (v7.0.0) and `scope-issue-raw` (v6.0.0) raw assets.",
                "",
                "## Layout",
                "",
                "```",
                "owner/repository/<number>.jsonl",
                "events.parquet",
                "commits.parquet",
                "README.md",
                "```",
                "",
                "Each line of a `.jsonl` file has exactly three fields, and lines are "
                "sorted by `time` ascending.",
                "",
                "```json",
                '{"time": "<ISO-8601 UTC>", "type": "<event type>", "payload": <raw fragment JSON>}',
                "```",
                "",
                "The payload is the raw GitHub REST fragment, unmodified: no value is "
                "removed, mutated, or added. `time` and `type` live only on the outer "
                "line. Every line carries a non-null `time`.",
                "",
                "## No-future contract",
                "",
                "The store is raw and unfiltered. The consumer enforces the cutoff `D` "
                "of a case by filtering `time < D` over the events and by pinning the "
                "git repository to state at or before `D`. Because every event is atomic "
                "and dated, `time < D` is the only operation required.",
                "",
                "## Warning: head event is an end-state snapshot",
                "",
                "The `pr` and `issue` head event is dated to `created_at`, but its "
                "payload is the object's final snapshot. Its mutable and end-state "
                "fields (`state`, `updated_at`, `closed_at`, `merged_at`, "
                "`merge_commit_sha`, final counts, labels) may describe moments after "
                "`created_at` and must not be read as known at `time`. For "
                "time-accurate state transitions use the atomic `timeline:*` events "
                "(`closed`, `merged`, `labeled`). This applies only to the single head "
                "event per object; every other event's payload is point-in-time.",
                "",
                "## Event types",
                "",
                "Pull requests emit `pr` (from `created_at`), `commit` (from "
                "`commit.author.date`), `review` (from `submitted_at`), `review_comment` "
                "and `comment` (from `created_at`), `review_request` (per requested user "
                "and team), and `timeline:<event>`.",
                "",
                "Issues emit `issue` (from `created_at`), `comment` (from `created_at`), "
                "and `timeline:<event>`.",
                "",
                "Timeline events are dated by `created_at`, except `committed` (from "
                "`author.date`) and `reviewed` (from `submitted_at`), which carry their "
                "time nested. A `review_request` user or team is dated by the earliest "
                "matching timeline `review_requested` event, and falls back to the head "
                "object's `created_at` when no match exists.",
                "",
                "## Run statistics",
                "",
                f"- repositories: {stats['repositories']}",
                f"- objects: {stats['objects']}",
                f"- events: {stats['events']}",
                f"- dropped undated events: {stats['dropped']}",
                "",
            ]
        )
        (outdir / "README.md").write_text(text, encoding="utf-8")


class Export:
    """Drive the full export across both assets."""

    def __init__(
        self,
        bucket: str,
        outdir: Path,
        change_version: str,
        issue_version: str,
        location: str,
        repositories: list[str],
        index: bool,
        project: str | None = None,
    ) -> None:
        self._outdir = outdir
        self._change_version = change_version
        self._issue_version = issue_version
        self._location = location
        self._filter = set(repositories)
        self._catalog = Catalog(bucket, project)
        self._source = Source(bucket, project)
        self._writer = Writer()
        self._progress = Progress()
        self._index = Index() if index else None
        self._commits = Commits() if index else None
        self._seen: set[tuple[str, int]] = set()
        self._repositories: set[str] = set()
        self._objects = 0
        self._events = 0
        self._dropped = 0

    def run(self) -> dict:
        """Export both assets and return the run statistics."""
        self._outdir.mkdir(parents=True, exist_ok=True)
        self._asset("scope-change-raw", self._change_version)
        self._asset("scope-issue-raw", self._issue_version)
        stats = {
            "repositories": len(self._repositories),
            "objects": self._objects,
            "events": self._events,
            "dropped": self._dropped,
        }
        if self._index is not None:
            self._index.write(self._outdir)
            self._attribution()
            self._commits.write(self._outdir)
        Readme().write(self._outdir, stats)
        return stats

    def _attribution(self) -> None:
        key = "scope-commit-attribution"
        partitions = self._catalog.partitions(key, "1.0.0", self._location)
        total = len(partitions)
        for done, partition in enumerate(partitions, start=1):
            self._commits.add(self._source.attribution(partition), self._filter)
            self._progress.asset(key, done, total)

    def _asset(self, key: str, version: str) -> None:
        partitions = self._catalog.partitions(key, version, self._location)
        total = len(partitions)
        for done, partition in enumerate(partitions, start=1):
            self._partition(partition)
            self._progress.asset(key, done, total)

    def _partition(self, partition: Partition) -> None:
        objects: dict = {}
        for row in self._source.rows(partition):
            if self._filter and row["name"] not in self._filter:
                continue
            key = (row["name"], row["number"])
            objects.setdefault(key, {})[row["type"]] = json.loads(row["payload"])
        for (name, number), payloads in objects.items():
            self._object(name, number, payloads)

    def _object(self, name: str, number: int, payloads: dict) -> None:
        if (name, number) in self._seen:
            return
        self._seen.add((name, number))
        head = payloads.get("pr") or payloads.get("issue") or {}
        head_time = Clock().iso(head.get("created_at"))
        resolver = Resolver()
        events = list(resolver.events(payloads, head_time))
        self._dropped += resolver.dropped
        if not events:
            return
        written = self._writer.write(self._outdir, name, number, events)
        if self._index is not None:
            self._index.add(name, number, events)
        self._repositories.add(name)
        self._objects += 1
        self._events += written


class Cli:
    """Command-line entrypoint for the event export."""

    def run(self, argv: list[str] | None = None) -> None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--outdir", type=Path, required=True)
        parser.add_argument(
            "--bucket", default=os.environ.get("CLOUD_BUCKET", "bug-request-bucket")
        )
        parser.add_argument("--change-version", default="7.0.0")
        parser.add_argument("--issue-version", default="6.0.0")
        parser.add_argument("--location", default="main")
        parser.add_argument("--repository", action="append", default=[])
        parser.add_argument(
            "--index", action=argparse.BooleanOptionalAction, default=True
        )
        arguments = parser.parse_args(argv)
        export = Export(
            bucket=arguments.bucket,
            outdir=arguments.outdir.resolve(),
            change_version=arguments.change_version,
            issue_version=arguments.issue_version,
            location=arguments.location,
            repositories=arguments.repository,
            index=arguments.index,
        )
        stats = export.run()
        for name, value in stats.items():
            print(f"{name}: {value}")


if __name__ == "__main__":
    Cli().run()
