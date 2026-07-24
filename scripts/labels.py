"""Export the ground-truth evaluation labels for the localization benchmark.

Companion to ``issues.py``. For each defective change -- the pull request that
introduced a bug -- this script emits the candidate node space a localizer is scored
over, together with the cutoff that scopes the case. One JSONL record per defective
change is written to ``<outdir>/labels.jsonl``.

Terminology follows the thesis: the pull request that introduced a bug is the
*defective change*, the one that fixed it the *corrective change*.

Output record, one JSON object per line:

    {
      "repository": "owner/repo",
      "pr":      <PR number of the defective change>,
      "cutoff":  "<merged_at of the defective change, ISO-8601 UTC>",
      "nodes":   [ {"path","start","end","type","base","after","bin"[,"provenance"]} ]
    }

Confidence stratum. Each node carries ``bin``, the confidence stratum (0..2) the
evaluation's label perturbation is parameterised by. A defective node takes the
reliability rho of the correction that marked it; a defect-free node has no rho of its
own and takes the change's median labelled rho. rho is binned into three equal-width
strata over [0, 1], matching ``HybridAuditSampling(bins=3)``.

Candidate space. ``nodes`` is the full node space N(c): every syntax node the defect
diff touches. Each edited line of the ``base -> defect`` diff is lifted to the node
that carries it, the same lift the localization label asset applies to its markings.
A node a correction localized is *defective* and additionally carries ``provenance``;
a node no correction named is *defect-free* and omits the field.

    "provenance": [ {"side","line","corrections":[<PR number>, ...]} ]

Each provenance entry is a marked line that lifts to the node, in the R/L convention
(``base`` for a deleted line, ``after`` for an added or context line). One line named
by several corrections merges to a single entry whose ``corrections`` lists their PR
numbers. So both a node and the lines within it deduplicate over everything but the
correction.

The annotations are the truth. Localization ran on a concrete defect commit -- the
introducer SHA attribution's agent found in git -- and diffed it against its first
parent. The pair ``base`` (the defect's first parent) and ``after`` (the defect
commit) fixes the diff a node is a candidate in, so it lives on each node and never
repeats inside the provenance.

Released set. The population is the ``localization-change-label`` asset directly: it
is one row per released defect and already applies the single-branch and
language-scope restrictions that define the released set, so consuming it verbatim
keeps this export in lockstep with every other consumer of the labels (the evaluation
notebooks read the same asset). One record is emitted per defect, keyed by the pull
request the defect belongs to.

Change resolution. A trunk commit belongs to exactly one pull request, and
``scope-commit-attribution`` carries that mapping directly from GitHub's
``associatedPullRequests`` over the first-parent trunk. It is the only sound source:
a pull request's own commit list reports every commit reachable in its branch, so a
sync or merge pull request would claim commits that belong to other changes.

Off-trunk filter. A defect that is not a trunk commit has no pull request in
``scope-commit-attribution``: it reached the integration branch through a merge
rather than as its own step, so the benchmark's trunk state never shows it. In the
released set every defect attributes, so this drop is a guard, not an expected path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from google.cloud import storage

from benchmark.attribution.history import VersionControl
from benchmark.definitions.partitions import Shard
from benchmark.localization.projection import NodeProjection
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


class Corpus:
    """Merge timestamps of the corpus pull requests."""

    def __init__(self, rows: list[dict]) -> None:
        self._merged: dict[tuple[str, int], str] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            self._merged[(row["name"], row["number"])] = payload["merged_at"]

    def merged(self, name: str, number: int) -> str:
        """Return the merged timestamp of a change."""
        return self._merged[(name, number)]


class Repos:
    """Resolve base commits and node projections through the cached bare repositories."""

    def __init__(self, bucket: str, names: list[str], project: str | None = None) -> None:
        self._client = storage.Client(project=project)
        self._bucket = bucket
        self._names = sorted(names)
        self._cache: dict[str, VersionControl] = {}
        self._projections: dict[str, NodeProjection] = {}

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

    def projection(self, name: str) -> NodeProjection:
        """Return the node projection of a repository, sharing its warm handle."""
        if name not in self._projections:
            self._projections[name] = NodeProjection(self.control(name))
        return self._projections[name]


class Clock:
    """Normalize GitHub timestamps to canonical ISO-8601 UTC."""

    def iso(self, raw: str) -> str:
        """Return a canonical UTC timestamp for a GitHub timestamp string."""
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return moment.astimezone(timezone.utc).isoformat()


class Nodes:
    """The candidate node space N(c) of a defect, defect-free and defective alike.

    N(c) is every syntax node the defect diff touches: each edited line of the
    ``base -> defect`` diff is lifted to the node that carries it, exactly as the
    localization label asset lifts markings. A defective node -- one a correction
    localized -- additionally records its provenance: the marked lines that lift to
    it, each with the corrections that named it.

    Defective and defect-free nodes share one identity, ``(path, start, end, type,
    base, after)``, so the diff-lifted nodes and the released markings deduplicate to
    one entry. The pair ``(base, after)`` fixes the diff the node is a candidate in,
    so it lives on the node and never repeats inside the provenance.

    Each node also carries ``bin``, the confidence stratum the evaluation perturbs by.
    """

    # The number of confidence strata, matching HybridAuditSampling(bins=3).
    _BINS = 3

    def __init__(self, rows: list[dict], attribution: Attribution) -> None:
        self._attribution = attribution
        self._by_defect: dict[str, list[dict]] = {}
        for row in rows:
            self._by_defect[row["defect"]] = row["label"]

    def of(
        self,
        name: str,
        defect: str,
        base: str,
        control: VersionControl,
        projection: NodeProjection,
    ) -> list[dict]:
        """Return the nodes of N(c) for a defect, defective ones carrying provenance."""
        nodes: dict[tuple, dict] = {}
        self._universe(defect, base, control, projection, nodes)
        self._marked(defect, base, projection, nodes)

        # A node carries the confidence stratum ``bin`` the evaluation perturbs by. A
        # defective node takes the reliability rho of the correction that marked it; a
        # defect-free node has no rho of its own and takes the change's median labelled
        # rho, exactly as the evaluation notebook does. rho is then binned to a stratum.
        labelled = [
            node["_rho"] for node in nodes.values() if node.get("_rho") is not None
        ]
        fallback = median(labelled) if labelled else 0.0

        ordered = []
        for node in nodes.values():
            rho = node.pop("_rho", None)
            node["bin"] = self._bin(rho if rho is not None else fallback)
            provenance = node.pop("_provenance", None)
            if provenance is not None:
                node["provenance"] = [
                    {
                        "side": side,
                        "line": line,
                        "corrections": sorted(corrections),
                    }
                    for (side, line), corrections in sorted(provenance.items())
                ]
            ordered.append(node)
        return ordered

    def _bin(self, rho: float) -> int:
        # The confidence stratum of a rho, matching HybridAuditSampling(bins=3): three
        # equal-width bins over [0, 1], clipped to the valid range. Kept inline so the
        # export shares no state with the audit sampler beyond this fixed rule.
        edge = int(rho * self._BINS)
        return max(0, min(self._BINS - 1, edge))

    def _universe(
        self,
        defect: str,
        base: str,
        control: VersionControl,
        projection: NodeProjection,
        nodes: dict[tuple, dict],
    ) -> None:
        # Every edited line of the defect diff lifts to the node that carries it; the
        # union of those nodes is N(c). A line that lifts to no node (a blank line, a
        # comment, a non-Python file) contributes none.
        for patch in control.diff(base, defect, context=0):
            new_path = patch.delta.new_file.path
            old_path = patch.delta.old_file.path
            for hunk in patch.hunks:
                for entry in hunk.lines:
                    if entry.origin == "+":
                        node = projection.project(defect, new_path, entry.new_lineno, "after")
                    elif entry.origin == "-":
                        node = projection.project(defect, old_path, entry.old_lineno, "base")
                    else:
                        continue
                    if node is not None:
                        self._ensure(nodes, node.path, node.start, node.end, node.type, base, defect)

    def _marked(
        self,
        defect: str,
        base: str,
        projection: NodeProjection,
        nodes: dict[tuple, dict],
    ) -> None:
        # The released markings are part of N(c) even when a whole-file marking does
        # not fall out of the raw diff lines. Each marking's covering lines lift to
        # the node and record their provenance -- the line and the corrections that
        # named it, merged across corrections that share one line.
        for struct in self._by_defect.get(defect, []):
            change = self._attribution.change(struct["correction"])
            correction = change[1] if change else None
            entry = self._ensure(
                nodes,
                struct["path"],
                tuple(struct["start"]),
                tuple(struct["end"]),
                struct["type"],
                base,
                defect,
            )
            # A defective node takes the reliability rho of the correction that marked
            # it. When several corrections share one node the last wins, matching the
            # evaluation notebook's span-keyed rho map.
            entry["_rho"] = struct["rho"]
            provenance = entry.setdefault("_provenance", {})
            for token in struct["lines"]:
                side = "base" if token[:1] == "R" else "after"
                line = int(token[1:])
                provenance.setdefault((side, line), set()).add(correction)

    def _ensure(
        self,
        nodes: dict[tuple, dict],
        path: str,
        start,
        end,
        type: str,
        base: str,
        after: str,
    ) -> dict:
        # A node's identity is its span in a diff -- ``(path, start, end, base,
        # after)`` -- not its type. A ``block`` and the single statement that fills it
        # share the same bytes, so they are one indistinguishable candidate; the type
        # is resolved to the more specific one. The ``(base, after)`` pair stays in the
        # key so a span shared across two defects of the change is not conflated.
        identity = (path, tuple(start), tuple(end), base, after)
        entry = nodes.get(identity)
        if entry is None:
            entry = {
                "path": path,
                "start": list(start),
                "end": list(end),
                "type": type,
                "base": base,
                "after": after,
            }
            nodes[identity] = entry
        elif self._specific(type, entry["type"]):
            entry["type"] = type
        return entry

    def _specific(self, candidate: str, current: str) -> str:
        # A ``block`` is the container the enclosed statement fills, so any non-block
        # type is the more specific reading of the same span and wins.
        return candidate != "block" and current == "block"


class Label:
    """Assemble one output record."""

    def record(
        self,
        repository: str,
        pr: int,
        cutoff: str,
        nodes: list[dict],
    ) -> dict:
        """Return the JSON record for one defective change."""
        return {
            "repository": repository,
            "pr": pr,
            "cutoff": cutoff,
            "nodes": nodes,
        }


class Export:
    """Drive the label export across the released pairs."""

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
        self._clock = Clock()
        self._label = Label()
        self._progress = Progress()
        self._records = 0
        self._kept = 0
        self._flagged = 0

    def run(self) -> dict:
        """Export the labels and return the run statistics."""
        self._outdir.mkdir(parents=True, exist_ok=True)

        self._progress.phase("loading commit attribution")
        attribution = Attribution(
            self._store.rows("scope-commit-attribution", "1.0.0", self._location, "*")
        )

        self._progress.phase("loading corpus changes")
        corpus = Corpus(
            self._store.rows(
                "filter-change-reference",
                "1.0.0",
                self._location,
                "default",
                columns=["name", "number", "payload"],
            )
        )

        self._progress.phase("loading labels")
        labels = self._store.rows(
            "localization-change-label",
            "1.0.0",
            self._location,
            "default",
            columns=["defect", "label"],
        )
        nodes = Nodes(labels, attribution)

        stationary = self._store.rows(
            "scope-repository-stationary", "1.0.0", self._location, "default", columns=["name"]
        )
        repos = Repos(self._bucket, [row["name"] for row in stationary], self._project)

        self._progress.phase("grouping by defective change")
        grouped = self._group(labels, attribution)

        self._progress.phase("resolving base commits and writing labels")
        path = self._outdir / "labels.jsonl"
        temp = self._outdir / ".labels.jsonl.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            for (name, number), defects in sorted(grouped.items()):
                line = self._line(name, number, defects, corpus, nodes, repos)
                handle.write(line + "\n")
                self._records += 1
        temp.replace(path)

        return {
            "records": self._records,
            "defects_kept": self._kept,
            "defects_unattributed": self._flagged,
        }

    def _group(self, labels: list[dict], attribution: Attribution) -> dict:
        # The label asset is the released population: it already applies the
        # single-branch and language-scope restrictions, so the records are exactly
        # its defects, grouped to the pull request each belongs to. A defect the
        # attribution cannot place on the trunk has no pull request to key a record;
        # it is counted and left out. In the released set every defect attributes, so
        # this drop is a guard rather than an expected path.
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

    def _line(
        self,
        name: str,
        number: int,
        defects: set[str],
        corpus: Corpus,
        nodes: Nodes,
        repos: Repos,
    ) -> str:
        control = repos.control(name)
        projection = repos.projection(name)
        record_nodes: list[dict] = []
        for defect in sorted(defects):
            base = repos.base(name, defect)
            record_nodes.extend(nodes.of(name, defect, base, control, projection))
        record = self._label.record(
            repository=name,
            pr=number,
            cutoff=self._clock.iso(corpus.merged(name, number)),
            nodes=record_nodes,
        )
        return json.dumps(record)


class Cli:
    """Command-line entrypoint for the label export."""

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
