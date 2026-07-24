"""Export the label-perturbation Beta-posterior counters for the evaluation.

Companion to ``labels.py``. The localization evaluation perturbs its ground-truth
labels by two independent processes -- a false-drop rate alpha and a false-add rate
gamma-plus -- whose priors are Beta posteriors accumulated over the audited changes.
This script materialises those counters exactly as ``notebooks/evaluation/result.ipynb``
computes them live (code cell ``387170e6``), so a downstream consumer reads them as an
asset rather than re-deriving them from the raw audit assets.

The counters are stratified by the reliability stratum ``rho_bin`` (0..2, three
equal-width bins over [0, 1], matching ``HybridAuditSampling(bins=3)``) and are
reported at the syntax *node* granularity. For each stratum a counter is a pair
``(a_j, b_j)`` -- the two shape parameters of a Beta posterior.

Two families of counter are emitted:

  alpha  -- the false-drop counter. Passed changes lift each audited selection marking
            to its node and split the decision into added / rejected / accepted; a_j is
            rejected markings, b_j accepted. Failed changes contribute their pipeline
            trace markings, all counted incorrect (a_j).

  gamma  -- the false-add (omission) counter. For each audited change N(c) is
            enumerated from the introducer diff (base -> introducer, context 0), each
            edited line lifted to the syntax node it carries and keyed on its span. a_j
            is the count of added (manual) nodes; b_j the rightly-unmarked remainder
            |N(c)| - marked - added. Failed changes add |N(c)| - |trace markings|.

Released population. The audited changes are cut to the released set by a semi-join
against the ``localization-change-label`` asset on the introducer defect, exactly as
the notebook does: the label asset already applies the single-branch and language-scope
restrictions, so consuming it verbatim keeps this export in lockstep with the notebook.

Output ``noise.json``, a single JSON object:

    {
      "bins": 3,
      "alpha": {"0": [a, b], "1": [a, b], "2": [a, b]},
      "gamma": {"0": [a, b], "1": [a, b], "2": [a, b]}
    }

Every stratum 0..2 is always present in each counter; a stratum absent from a
computed counter is filled with ``[0, 0]``. Keys are the stratum indices as strings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import polars as pl
from google.cloud import storage

from benchmark.attribution.history import VersionControl
from benchmark.definitions.partitions import Shard
from benchmark.localization.projection import NodeProjection
from benchmark_notebook.io import CloudStorage

# The number of reliability strata, matching HybridAuditSampling(bins=3).
BINS = 3


class Progress:
    """Report loading phases to stderr."""

    def phase(self, message: str) -> None:
        """Report the start of a loading phase."""
        print(message, file=sys.stderr, flush=True)


class Store:
    """Read parquet assets through the notebook cloud storage cache as polars frames."""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        self._storage = CloudStorage(bucket=bucket, project=project)

    def asset(
        self,
        key: str,
        version: str = "1.0.0",
        location: str = "main",
        partition: str = "default",
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """Return an asset partition (or every partition on a wildcard) as a frame."""
        table = self._storage.asset(
            key=key,
            version=version,
            location=location,
            partition=partition,
            columns=columns,
        )
        return pl.from_arrow(table)


class Repos:
    """One NodeProjection per repository, built lazily and reused across lifts."""

    def __init__(self, bucket: str, names: list[str], project: str | None = None) -> None:
        self._client = storage.Client(project=project)
        self._bucket = bucket
        self._names = sorted(names)
        self._projections: dict[str, NodeProjection] = {}

    def projection(self, name: str) -> NodeProjection:
        """Return the node projection of a repository, sharing its warm handle."""
        if name not in self._projections:
            self._projections[name] = NodeProjection(
                VersionControl.create(
                    self._client,
                    bucket=self._bucket,
                    key="scope-source-raw",
                    location="main",
                    version="4.0.0",
                    partition=Shard.to_space(self._names, name),
                )
            )
        return self._projections[name]


def complete_bins(counter: dict[int, tuple[int, int]]) -> dict[str, list[int]]:
    """Fill a rho_bin -> (a_j, b_j) map to all strata 0..BINS-1 as a string-keyed record.

    Every stratum 0..BINS-1 is present in the result; a stratum missing from the input
    is filled with (0, 0). Keys are the stratum indices as strings and values the
    ``[a_j, b_j]`` pair, so the result serialises straight to JSON.
    """
    result: dict[str, list[int]] = {}
    for index in range(BINS):
        pair = counter.get(index, (0, 0))
        result[str(index)] = [int(pair[0]), int(pair[1])]
    return result


def record(
    counts_node: dict[int, tuple[int, int]],
    gamma_node: dict[int, tuple[int, int]],
) -> dict:
    """Assemble the ``noise.json`` object from the two computed counters.

    ``counts_node`` is the alpha counter, ``gamma_node`` the gamma-plus counter. Each is
    a rho_bin -> (a_j, b_j) map; every stratum is completed to a (0, 0) default so the
    three strata are always present.
    """
    return {
        "bins": BINS,
        "alpha": complete_bins(counts_node),
        "gamma": complete_bins(gamma_node),
    }


class Counters:
    """Compute the alpha and gamma-plus counters over the released audited changes.

    This is a 1:1 port of code cell ``387170e6`` in
    ``notebooks/evaluation/result.ipynb``: the same audit assets, the same semi-join to
    the released label population, the same node lifts, and the same aggregations. It
    returns the two node-level rho_bin -> (a_j, b_j) maps the notebook prints.
    """

    def __init__(self, store: Store, repos: Repos, progress: Progress) -> None:
        self._store = store
        self._repos = repos
        self._progress = progress

    def compute(self) -> dict:
        """Return the assembled ``noise.json`` object."""
        counts_node, gamma_node = self._counters()
        return record(counts_node, gamma_node)

    def _counters(self) -> tuple[dict, dict]:
        store = self._store
        repos = self._repos

        self._progress.phase("loading audit annotations")
        # Reproduce the unconditional alpha counters from audit/outcome.ipynb: passed
        # changes contribute their audited node decisions, failed changes contribute
        # their pipeline markings (all incorrect, Y_c = empty). a_j = rejected + fail,
        # b_j = accepted.
        annotation = store.asset(
            key="audit-change-annotation",
            version="1.0.0",
            location="main",
            partition="default",
        ).with_columns(
            pl.when(~pl.col("categories").list.contains("intrinsic"))
            .then(pl.lit("stage_1"))
            .when(pl.col("attribution") == "wrong")
            .then(pl.lit("stage_2"))
            .otherwise(pl.lit("passed"))
            .alias("stage")
        )

        self._progress.phase("loading released labels")
        # The released population is the label asset: it applies the single-branch and
        # language-scope restrictions itself, so the audited changes are cut to it by a
        # semi-join rather than by repeating either rule here.
        label = store.asset(
            key="localization-change-label",
            version="1.0.0",
            location="main",
            partition="default",
            columns=["defect"],
        )
        annotation = annotation.join(
            label.select(pl.col("defect").alias("introducer")),
            on="introducer",
            how="semi",
        )
        self._progress.phase(
            f"audited changes in the released population: {annotation.height}"
        )

        # Passed changes: lift each selection marking to its node.
        mark_rows = []
        for row in annotation.filter(pl.col("stage") == "passed").iter_rows(named=True):
            projection = repos.projection(row["repository"])
            for block in json.loads(row["selections"]):
                origin = "manual" if block["origin"] == "manual" else "suggested"
                status = "accepted" if origin == "manual" else block["status"]
                for line in block["lines"]:
                    node = projection.project(
                        row["introducer"], block["path"], line["line"], line["side"]
                    )
                    mark_rows.append(
                        {
                            "repository": row["repository"],
                            "number": row["number"],
                            "rho_bin": row["rho_bin"],
                            "path": block["path"],
                            "origin": origin,
                            "status": status,
                            "start": None if node is None else list(node.start),
                            "end": None if node is None else list(node.end),
                            "type": None if node is None else node.type,
                        }
                    )
        marks = pl.DataFrame(mark_rows)

        node_marks = (
            marks.filter(pl.col("type").is_not_null())
            .group_by("repository", "number", "rho_bin", "path", "start", "end", "type")
            .agg(
                pl.col("origin").min().alias("origin"),
                (pl.col("status") == "rejected").any().alias("any_rejected"),
            )
            .with_columns(
                pl.when(pl.col("origin") == "manual")
                .then(pl.lit("added"))
                .when(pl.col("any_rejected"))
                .then(pl.lit("rejected"))
                .otherwise(pl.lit("accepted"))
                .alias("decision")
            )
        )

        self._progress.phase("loading failed-change traces")
        # Failed changes: pipeline markings from the trace, all incorrect.
        sample = store.asset(
            key="audit-change-sample", version="4.0.0", partition="default"
        ).select(
            pl.col("name").alias("repository"),
            "correction",
            pl.col("defect").alias("introducer"),
        )
        trace = store.asset(
            key="localization-change-trace",
            version="3.1.0",
            partition="*",
            columns=["name", "correction", "defect", "locations"],
        ).unique(subset=["name", "correction", "defect"])
        failed = (
            annotation.filter(pl.col("stage") != "passed")
            .join(sample, on=["repository", "introducer"], how="left")
            .join(
                trace.select(
                    pl.col("name").alias("repository"),
                    "correction",
                    pl.col("defect").alias("introducer"),
                    "locations",
                ),
                on=["repository", "correction", "introducer"],
                how="left",
            )
        )

        fail_node_rows = []
        for row in failed.iter_rows(named=True):
            projection = repos.projection(row["repository"])
            seen_nodes = set()
            for m in json.loads(row["locations"] or "[]"):
                token = str(m["line"])
                side = "base" if token[:1] == "D" else "after"
                number = token[1:] if token[:1] in ("L", "D") else token
                if not number.isdigit():
                    continue
                node = projection.project(row["introducer"], m["file"], int(number), side)
                if node is None:
                    continue
                key = (node.path, node.start, node.end, node.type)
                if key not in seen_nodes:
                    seen_nodes.add(key)
                    fail_node_rows.append({"rho_bin": row["rho_bin"]})

        fail_nodes = (
            pl.DataFrame(fail_node_rows).group_by("rho_bin").len().rename({"len": "fail"})
        )

        def counters(passed_frame, fail_frame):
            return (
                passed_frame.join(fail_frame, on="rho_bin", how="left")
                .fill_null(0)
                .with_columns(
                    (pl.col("rejected") + pl.col("fail")).alias("a_j"),
                    pl.col("accepted").alias("b_j"),
                )
                .sort("rho_bin")
            )

        # Node level (A10).
        passed_nodes = (
            node_marks.filter(pl.col("decision") != "added")
            .group_by("rho_bin")
            .agg(
                (pl.col("decision") == "rejected").sum().alias("rejected"),
                (pl.col("decision") == "accepted").sum().alias("accepted"),
            )
        )
        counts_node_frame = counters(passed_nodes, fail_nodes)

        counts_node = {
            r["rho_bin"]: (r["a_j"], r["b_j"])
            for r in counts_node_frame.iter_rows(named=True)
        }
        self._progress.phase(f"node counts a_j/b_j: {counts_node}")

        # Omission gamma+ counters, reproduced over the audited changes as in
        # audit/outcome.ipynb: N(c) is enumerated per audited change from the introducer
        # diff, a_j = added nodes, b_j = the rightly-unmarked nodes
        # |N(c)| - |Y_c| - |A_c|. Node level, unconditional (fails contribute their trace
        # markings with A_c = 0).
        #
        # N(c) keys on the span (path, start, end) only, matching the label asset: a
        # block and the statement filling it are one candidate. Y_c below is subtracted
        # from this N(c), so it keys the same way.
        def change_nodes(repository, commit):
            projection = repos.projection(repository)
            vc = projection._repository
            base = vc.parents(commit)[0]
            entries = set()
            for patch in vc.diff(base, commit, context=0):
                new_path, old_path = patch.delta.new_file.path, patch.delta.old_file.path
                for hunk in patch.hunks:
                    for entry in hunk.lines:
                        if entry.origin == "-":
                            node = projection.project(
                                commit, old_path, entry.old_lineno, "base"
                            )
                        elif entry.origin == "+":
                            node = projection.project(
                                commit, new_path, entry.new_lineno, "after"
                            )
                        else:
                            continue
                        if node is not None:
                            entries.add((node.path, node.start, node.end))
            return entries

        self._progress.phase("enumerating N(c) per audited change")
        audit_nc = {
            (row["repository"], row["number"]): change_nodes(
                row["repository"], row["introducer"]
            )
            for row in annotation.iter_rows(named=True)
        }
        audit_n = pl.DataFrame(
            [
                {
                    "repository": repo,
                    "number": number,
                    "n_c": len(nodes),
                }
                for (repo, number), nodes in audit_nc.items()
            ]
        )

        passed_nodes_gamma = (
            node_marks.group_by("repository", "number", "rho_bin")
            .agg(
                (pl.col("decision") != "added").sum().alias("marked"),
                (pl.col("decision") == "added").sum().alias("added"),
            )
            .join(
                audit_n.select("repository", "number", "n_c"),
                on=["repository", "number"],
                how="left",
            )
            .group_by("rho_bin")
            .agg(
                pl.col("added").sum().alias("a_j"),
                (pl.col("n_c") - pl.col("marked") - pl.col("added")).sum().alias("b_j"),
            )
        )

        # Fails contribute rightly-unmarked (A_c = 0): b_j += n_c - |Y_c_trace|. Y_c keys
        # on the span only, so it aligns with the span-only N(c) it is subtracted from.
        fail_gamma_rows = []
        for row in failed.iter_rows(named=True):
            projection = repos.projection(row["repository"])
            nodes = set()
            for m in json.loads(row["locations"] or "[]"):
                token = str(m["line"])
                side = "base" if token[:1] == "D" else "after"
                num = token[1:] if token[:1] in ("L", "D") else token
                if not num.isdigit():
                    continue
                node = projection.project(row["introducer"], m["file"], int(num), side)
                if node is None:
                    continue
                nodes.add((node.path, node.start, node.end))
            universe = audit_nc.get((row["repository"], row["number"]), set())
            fail_gamma_rows.append(
                {
                    "rho_bin": row["rho_bin"],
                    "n_c": len(universe),
                    "y_nodes": len(nodes),
                }
            )
        fail_gamma = pl.DataFrame(fail_gamma_rows)
        fail_node_gamma = fail_gamma.group_by("rho_bin").agg(
            (pl.col("n_c") - pl.col("y_nodes")).sum().alias("b_j")
        )

        def merge_gamma(passed, fail):
            return (
                passed.join(fail, on="rho_bin", how="left", suffix="_fail")
                .fill_null(0)
                .with_columns((pl.col("b_j") + pl.col("b_j_fail")).alias("b_j"))
                .select("rho_bin", "a_j", "b_j")
                .sort("rho_bin")
            )

        gamma_node = {
            r["rho_bin"]: (r["a_j"], r["b_j"])
            for r in merge_gamma(passed_nodes_gamma, fail_node_gamma).iter_rows(named=True)
        }
        self._progress.phase(f"gamma+ node a_j/b_j: {gamma_node}")

        return counts_node, gamma_node


class Export:
    """Drive the counter export and write the atomic ``noise.json``."""

    def __init__(
        self,
        bucket: str,
        outdir: Path,
        location: str,
        project: str | None = None,
    ) -> None:
        self._outdir = outdir
        self._location = location
        self._bucket = bucket
        self._project = project
        self._progress = Progress()

    def run(self) -> dict:
        """Export the counters, write noise.json, and return the record."""
        self._outdir.mkdir(parents=True, exist_ok=True)

        store = Store(self._bucket, self._project)

        self._progress.phase("loading stationary repositories")
        stationary = store.asset(
            key="scope-repository-stationary",
            version="1.0.0",
            location=self._location,
            partition="default",
            columns=["name"],
        )
        repos = Repos(
            self._bucket, stationary["name"].to_list(), self._project
        )

        counters = Counters(store, repos, self._progress)
        document = counters.compute()

        self._progress.phase("writing noise.json")
        path = self._outdir / "noise.json"
        temp = self._outdir / ".noise.json.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        temp.replace(path)

        return document


class Cli:
    """Command-line entrypoint for the noise-counter export."""

    def run(self, argv: list[str] | None = None) -> None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--outdir", type=Path, required=True)
        parser.add_argument(
            "--bucket", default=os.environ.get("CLOUD_BUCKET", "bug-request-bucket")
        )
        parser.add_argument("--location", default="main")
        arguments = parser.parse_args(argv)
        export = Export(
            bucket=arguments.bucket,
            outdir=arguments.outdir.resolve(),
            location=arguments.location,
        )
        document = export.run()
        print(json.dumps(document))


if __name__ == "__main__":
    Cli().run()
