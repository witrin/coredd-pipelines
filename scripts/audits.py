"""Export the audit annotations of the localization benchmark as JSONL.

Companion to ``issues.py`` and ``labels.py``. The audit is the human review of the
released labels: for a sampled pair an annotator judged the attribution, classified
the defect, and accepted or rejected the suggested markings. This script writes those
judgements as one JSONL record per annotation to ``<outdir>/audits.jsonl``.

The stored asset is not directly usable: its ``selections`` column is a JSON string
rather than structured data, its field names predate the benchmark's vocabulary, and
its markings carry no relation to the syntax nodes the labels are defined over.

Output record, one JSON object per line:

    {
      "repository": "owner/repo",
      "correction": "<merge commit SHA of the corrective change>",
      "defect":     "<defect commit SHA>",
      "annotator":  "<annotator id>",
      "filter":     ["intrinsic"],
      "attribution": "correct",
      "confidence": 3,
      "comment":    "",
      "selections": [
        {
          "path":   "package/module.py",
          "origin": "suggestion",
          "status": "accepted",
          "base":   "<first parent of the defect commit>",
          "after":  "<defect commit>",
          "lines":  [ {"side": "after", "line": 96} ],
          "nodes":  [ {"start": [91, 4], "end": [102, 71], "type": "block"} ]
        }
      ]
    }

Released population. Only annotations whose defect is a released label are exported.
The ``localization-change-label`` asset is the released population -- it applies the
single-branch and language-scope restrictions the benchmark is defined over -- so the
annotations are cut to it by a semi-join on the label defects, exactly as the evaluation
notebook does. Annotations outside it are counted and left out.

Field resolution. ``introducer`` becomes ``defect`` and ``categories`` becomes
``filter``. The annotation names its corrective change by pull request number; the
export carries the merge commit SHA instead, joined from ``audit-change-sample`` over
(repository, defect), so both sides of a pair are commits.

The annotation's ``rho_bin``, ``verdict`` and ``status`` are dropped: the first two are
audit bookkeeping, the third is the submission state of the annotation form, none of
which describes the judgement itself.

Selections. Each raw selection carries a ``sha`` that is always the defect commit, so
it is restated as the pair the marking was made against: ``after`` is the defect
commit, ``base`` its first parent, the diff the localization annotated. The line
markings keep their ``side`` and ``line``.

Nodes. Every marked line is lifted to the syntax node that carries it, through the
same ``NodeProjection`` the released labels were built with, so the nodes here and the
nodes in ``labels.jsonl`` are the same objects. Lines of one selection that share a
node collapse to a single entry, and a line that lifts to no node (a blank line, a
comment, a file that is not Python) contributes none.
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
        """Return the rows of an asset partition."""
        table = self._storage.asset(
            key=key, version=version, location=location, partition=partition, columns=columns
        )
        return table.to_pylist()


class Sample:
    """The audited pairs, keyed by repository and defect.

    The annotation names its corrective change by pull request number, the sample by
    merge commit SHA. The SHA is the identifier the whole localization tail keys on,
    so it is the one carried into the export.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._correction = {
            (row["name"], row["defect"]): row["correction"] for row in rows
        }

    def correction(self, name: str, defect: str) -> str:
        """Return the merge commit SHA of the corrective change of an audited pair."""
        return self._correction[(name, defect)]


class Repos:
    """Hold the cached bare repositories the projection and base lookup need."""

    def __init__(self, bucket: str, names: list[str], project: str | None = None) -> None:
        self._client = storage.Client(project=project)
        self._bucket = bucket
        self._names = sorted(names)
        self._cache: dict[str, VersionControl] = {}

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

    def base(self, name: str, commit: str) -> str:
        """Return the first parent of a commit -- the base it was diffed against."""
        return self.control(name).parents(commit)[0]


class Projection:
    """Lift marked lines to the syntax nodes that carry them."""

    def __init__(self, repos: Repos) -> None:
        self._repos = repos
        self._cache: dict[str, NodeProjection] = {}
        self.missed = 0

    def nodes(self, name: str, defect: str, path: str, lines: list[dict]) -> list[dict]:
        """Return the distinct nodes the lines of one selection lift to."""
        projection = self._projection(name)
        found: dict[tuple, dict] = {}
        for line in lines:
            node = projection.project(defect, path, int(line["line"]), line["side"])
            if node is None:
                self.missed += 1
                continue
            identity = (node.start, node.end, node.type)
            if identity in found:
                continue
            found[identity] = {
                "start": list(node.start),
                "end": list(node.end),
                "type": node.type,
            }
        return [found[key] for key in sorted(found)]

    def _projection(self, name: str) -> NodeProjection:
        if name not in self._cache:
            self._cache[name] = NodeProjection(self._repos.control(name))
        return self._cache[name]


class Selection:
    """One reviewed marking: its lines, the commits it was made against, its nodes."""

    def __init__(self, projection: Projection) -> None:
        self._projection = projection

    def of(self, name: str, defect: str, base: str, raw: dict) -> dict:
        """Return the exported form of one raw selection entry."""
        lines = [
            {"side": line["side"], "line": int(line["line"])} for line in raw["lines"]
        ]
        return {
            "path": raw["path"],
            "origin": raw["origin"],
            "status": raw["status"],
            "base": base,
            "after": defect,
            "lines": lines,
            "nodes": self._projection.nodes(name, defect, raw["path"], lines),
        }


class Audit:
    """Assemble one output record from an annotation row."""

    def record(
        self,
        repository: str,
        correction: str,
        defect: str,
        annotator: str,
        filter: list[str],
        attribution: str,
        confidence: int,
        comment: str,
        selections: list[dict],
    ) -> dict:
        """Return the JSON record for one annotation."""
        return {
            "repository": repository,
            "correction": correction,
            "defect": defect,
            "annotator": annotator,
            "filter": filter,
            "attribution": attribution,
            "confidence": confidence,
            "comment": comment,
            "selections": selections,
        }


class Export:
    """Drive the audit export across the annotations."""

    def __init__(
        self,
        bucket: str,
        outdir: Path,
        annotation_version: str,
        sample_version: str,
        location: str,
        repositories: list[str],
        project: str | None = None,
    ) -> None:
        self._outdir = outdir
        self._location = location
        self._annotation_version = annotation_version
        self._sample_version = sample_version
        self._filter = set(repositories)
        self._store = Store(bucket, project)
        self._bucket = bucket
        self._project = project
        self._audit = Audit()
        self._progress = Progress()
        self._records = 0
        self._selections = 0
        self._nodes = 0
        self._unreleased = 0

    def run(self) -> dict:
        """Export the audits and return the run statistics."""
        self._outdir.mkdir(parents=True, exist_ok=True)

        self._progress.phase("loading annotations")
        annotations = self._store.rows(
            "audit-change-annotation",
            self._annotation_version,
            self._location,
            "default",
        )
        if self._filter:
            annotations = [
                row for row in annotations if row["repository"] in self._filter
            ]

        # The released population is the label asset: it applies the single-branch and
        # language-scope restrictions the benchmark is defined over. An annotation whose
        # introducer never became a released label describes a change outside that
        # population, so it is cut here by a semi-join on the label defects -- the same
        # cut the evaluation notebook makes -- rather than exported. Those are counted.
        self._progress.phase("loading released population")
        released = {
            row["defect"]
            for row in self._store.rows(
                "localization-change-label", "1.0.0", self._location, "default",
                columns=["defect"],
            )
        }
        kept = [row for row in annotations if row["introducer"] in released]
        self._unreleased = len(annotations) - len(kept)
        annotations = kept

        self._progress.phase("loading audited pairs")
        sample = Sample(
            self._store.rows(
                "audit-change-sample",
                self._sample_version,
                self._location,
                "default",
                columns=["name", "correction", "defect"],
            )
        )

        stationary = self._store.rows(
            "scope-repository-stationary", "1.0.0", self._location, "default", columns=["name"]
        )
        repos = Repos(self._bucket, [row["name"] for row in stationary], self._project)
        projection = Projection(repos)
        selection = Selection(projection)

        self._progress.phase("projecting markings and writing audits")
        path = self._outdir / "audits.jsonl"
        temp = self._outdir / ".audits.jsonl.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            for row in annotations:
                handle.write(self._line(row, repos, selection, sample) + "\n")
                self._records += 1
        temp.replace(path)

        return {
            "records": self._records,
            "selections": self._selections,
            "nodes": self._nodes,
            "lines_without_node": projection.missed,
            "annotations_unreleased": self._unreleased,
        }

    def _line(self, row: dict, repos: Repos, selection: Selection, sample: Sample) -> str:
        name = row["repository"]
        defect = row["introducer"]
        base = repos.base(name, defect)
        selections = []
        for raw in json.loads(row["selections"] or "[]"):
            entry = selection.of(name, defect, base, raw)
            selections.append(entry)
            self._selections += 1
            self._nodes += len(entry["nodes"])
        record = self._audit.record(
            repository=name,
            correction=sample.correction(name, defect),
            defect=defect,
            annotator=row["annotator"],
            filter=list(row["categories"] or []),
            attribution=row["attribution"],
            confidence=row["confidence"],
            comment=row["comment"],
            selections=selections,
        )
        return json.dumps(record)


class Cli:
    """Command-line entrypoint for the audit export."""

    def run(self, argv: list[str] | None = None) -> None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--outdir", type=Path, required=True)
        parser.add_argument(
            "--bucket", default=os.environ.get("CLOUD_BUCKET", "bug-request-bucket")
        )
        parser.add_argument("--annotation-version", default="1.0.0")
        parser.add_argument("--sample-version", default="4.0.0")
        parser.add_argument("--location", default="main")
        parser.add_argument("--repository", action="append", default=[])
        arguments = parser.parse_args(argv)
        export = Export(
            bucket=arguments.bucket,
            outdir=arguments.outdir.resolve(),
            annotation_version=arguments.annotation_version,
            sample_version=arguments.sample_version,
            location=arguments.location,
            repositories=arguments.repository,
        )
        stats = export.run()
        for name, value in stats.items():
            print(f"{name}: {value}")


if __name__ == "__main__":
    Cli().run()
