"""Export the unified diff of every defective change in the localization benchmark.

Companion to ``labels.py``. Where ``labels.py`` emits the candidate node space a
localizer is scored over, this script emits the raw material that space is lifted
from: the ``base -> defect`` unified diff of each defective change. One ``.diff``
file per defect is written under a per-pull-request directory.

Terminology follows the thesis: the pull request that introduced a bug is the
*defective change*. The defect commit is the introducer SHA attribution found in
git; ``base`` is its first parent -- the commit it was diffed against -- and
``after`` is the defect commit itself. That ``(base, after)`` pair is exactly the
one carried on every node of the matching ``labels.jsonl`` record.

Output layout, one file per defect, sorted-hunk unified diff text:

    <outdir>/owner/repository/<pr number>/<base sha>..<after sha>.diff

A pull request directory holds one diff per defect commit it introduced, so a
change that introduced several defects yields several files, each named by its
own ``base..after`` pair.

Diff provenance. The diff is produced through ``VersionControl.diff`` -- the same
libgit2-backed diff the pipeline uses -- and never through the ``git`` CLI. This
keeps the exported diffs byte-comparable with what the pipeline sees: rename and
copy detection (``find_similar``) and ``interhunk_lines=0`` are applied
identically. Only the context width differs from the pipeline's node lift: this
export uses ``context=3`` (git's default) so the files are readable, review-ready
unified diffs rather than the zero-context diff the node projection walks.

Released set. The population is the ``localization-change-label`` asset directly,
consumed verbatim exactly as ``labels.py`` consumes it, then grouped to the pull
request each defect belongs to via ``scope-commit-attribution``. A defect the
attribution cannot place on the trunk has no pull request to key a directory; it
is counted and left out. In the released set every defect attributes, so this
drop is a guard, not an expected path -- and the emitted diff count matches the
``labels.jsonl`` record count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google.cloud import storage

from benchmark.attribution.history import VersionControl
from benchmark.definitions.partitions import Shard
from benchmark_notebook.io import CloudStorage


class Progress:
    """Report loading phases to stderr."""

    def phase(self, message: str) -> None:
        """Report the start of a loading phase."""
        print(message, file=sys.stderr, flush=True)


class Store:
    """Read parquet assets through the notebook cloud storage cache."""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        self._storage = CloudStorage(bucket=bucket, project=project)

    def rows(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
        columns: list[str] | None = None,
    ) -> list[dict]:
        """Return the rows of an asset partition, or of every partition on a wildcard."""
        table = self._storage.asset(
            key=key, version=version, location=location, partition=partition, columns=columns
        )
        return table.to_pylist()


class Attribution:
    """Map a trunk commit to the pull request GitHub associates with it."""

    def __init__(self, rows: list[dict]) -> None:
        self._change: dict[str, tuple[str, int]] = {}
        for row in rows:
            if not row["payload"]:
                continue
            node = json.loads(row["payload"])
            requests = (node.get("associatedPullRequests") or {}).get("nodes") or []
            for request in requests:
                merged = request.get("merged")
                owner = (request.get("repository") or {}).get("nameWithOwner")
                if merged and owner == row["name"]:
                    self._change[row["identifier"]] = (row["name"], request["number"])

    def change(self, commit: str) -> tuple[str, int] | None:
        """Return the (repository, number) of the pull request a trunk commit belongs to."""
        return self._change.get(commit)


class Repos:
    """Resolve base commits and diffs through the cached bare repositories."""

    def __init__(self, bucket: str, names: list[str], project: str | None = None) -> None:
        self._client = storage.Client(project=project)
        self._bucket = bucket
        self._names = sorted(names)
        self._cache: dict[str, VersionControl] = {}

    def base(self, name: str, commit: str) -> str:
        """Return the first parent of a commit -- the base it was diffed against."""
        return self.control(name).parents(commit)[0]

    def control(self, name: str) -> VersionControl:
        """Return the version control handle of a repository."""
        if name not in self._cache:
            self._cache[name] = VersionControl.create(
                self._client,
                bucket=self._bucket,
                key="scope-source-raw",
                location="main",
                version="4.0.0",
                partition=Shard.to_space(self._names, name),
            )
        return self._cache[name]


class Unidiff:
    """Render the unified-diff text of a base -> after change through the own lib."""

    #: git's default context width -- readable diffs, not the zero-context node lift.
    CONTEXT = 3

    def text(self, control: VersionControl, base: str, after: str) -> str:
        """Return the unified-diff text of ``base -> after``.

        The diff comes from ``VersionControl.diff`` (libgit2), never from the
        ``git`` CLI, so rename/copy detection and interhunk settings match the
        pipeline. ``pygit2.Diff.patch`` is the full unified-diff text; it is
        ``None`` for an empty diff, where the concatenated patch texts -- also
        empty -- are the correct fallback.
        """
        diff = control.diff(base, after, context=self.CONTEXT)
        patch = diff.patch
        if patch is not None:
            return patch
        return "".join(entry.text or "" for entry in diff)


class Export:
    """Drive the diff export across the released defective changes."""

    def __init__(
        self,
        bucket: str,
        outdir: Path,
        location: str,
        repositories: list[str],
        project: str | None = None,
    ) -> None:
        self._outdir = outdir
        self._location = location
        self._filter = set(repositories)
        self._store = Store(bucket, project)
        self._bucket = bucket
        self._project = project
        self._unidiff = Unidiff()
        self._progress = Progress()
        self._records = 0
        self._kept = 0
        self._flagged = 0

    def run(self) -> dict:
        """Export the diffs and return the run statistics."""
        self._outdir.mkdir(parents=True, exist_ok=True)

        self._progress.phase("loading commit attribution")
        attribution = Attribution(
            self._store.rows("scope-commit-attribution", "1.0.0", self._location, "*")
        )

        self._progress.phase("loading labels")
        labels = self._store.rows(
            "localization-change-label",
            "1.0.0",
            self._location,
            "default",
            columns=["defect"],
        )

        stationary = self._store.rows(
            "scope-repository-stationary", "1.0.0", self._location, "default", columns=["name"]
        )
        repos = Repos(self._bucket, [row["name"] for row in stationary], self._project)

        self._progress.phase("grouping by defective change")
        grouped = self._group(labels, attribution)

        self._progress.phase("resolving base commits and writing diffs")
        for (name, number), defects in sorted(grouped.items()):
            self._write(name, number, defects, repos)
            self._records += 1

        return {
            "records": self._records,
            "defects_kept": self._kept,
            "defects_unattributed": self._flagged,
        }

    def _group(self, labels: list[dict], attribution: Attribution) -> dict:
        # The label asset is the released population: it already applies the
        # single-branch and language-scope restrictions, so the diffs are exactly
        # its defects, grouped to the pull request each belongs to. A defect the
        # attribution cannot place on the trunk has no pull request to key a
        # directory; it is counted and left out. This mirrors ``labels.py`` so the
        # emitted diff count matches the label record count.
        grouped: dict[tuple[str, int], set[str]] = {}
        for row in labels:
            defect = row["defect"]
            change = attribution.change(defect)
            if change is None:
                self._flagged += 1
                continue
            if self._filter and change[0] not in self._filter:
                continue
            self._kept += 1
            grouped.setdefault(change, set()).add(defect)
        return grouped

    def _write(self, name: str, number: int, defects: set[str], repos: Repos) -> None:
        # One directory per pull request, one diff file per defect it introduced.
        # Each file is named by its own ``base..after`` pair -- the pair carried on
        # the matching ``labels.jsonl`` record's nodes.
        control = repos.control(name)
        owner, repository = name.split("/", 1)
        directory = self._outdir / owner / repository / str(number)
        directory.mkdir(parents=True, exist_ok=True)
        for defect in sorted(defects):
            base = repos.base(name, defect)
            text = self._unidiff.text(control, base, defect)
            path = directory / f"{base}..{defect}.diff"
            temp = directory / f".{base}..{defect}.diff.tmp"
            temp.write_text(text, encoding="utf-8")
            temp.replace(path)


class Cli:
    """Command-line entrypoint for the diff export."""

    def run(self, argv: list[str] | None = None) -> None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--outdir", type=Path, required=True)
        parser.add_argument(
            "--bucket", default=os.environ.get("CLOUD_BUCKET", "bug-request-bucket")
        )
        parser.add_argument("--location", default="main")
        parser.add_argument("--repository", action="append", default=[])
        arguments = parser.parse_args(argv)
        export = Export(
            bucket=arguments.bucket,
            outdir=arguments.outdir.resolve(),
            location=arguments.location,
            repositories=arguments.repository,
        )
        stats = export.run()
        for name, value in stats.items():
            print(f"{name}: {value}")


if __name__ == "__main__":
    Cli().run()
