"""Export the repository cohort of the benchmark as JSONL.

Companion to ``labels.py`` and ``audits.py``. The six benchmark repositories are a
stratified sample from a cohort of repositories whose review and integration
workflows are observable. This script describes that cohort: one JSONL record per
cohort repository is written to ``<outdir>/cohort.jsonl``, carrying the scores the
repository passed the construction funnel with and the descriptive activity and
scale metrics the thesis reports.

Output record, one JSON object per line:

    {
      "repository":    "owner/repo",
      "organization":  "owner",
      "review":        {"score": 0.87, "low": 0.81, "high": 0.93, "coverage": 0.84},
      "stationarity":  {"onset": 0.0, "kind": "stationary"},
      "bypass":        {"share": 0.01, "attributed": 1234, "bypassed": 12,
                        "picked": 3, "ambiguous": 0},
      "merges":        {"total": 585, "rate": 8.2},
      "reviewers":     {"total": 24, "rate": 3.1},
      "scale":         {"sloc": 48210, "core": 4, "age": 71},
      "participation": {"share": 0.44, "stratum": "mid", "sampled": false}
    }

Field groups. ``review`` holds the Wilson-interval eligibility score of repository
selection with its interval bounds, both measured on the sampling window, and the
peer-review coverage over the full merged history -- a merged pull request counts as
covered when at least one user other than its author reviewed it. ``stationarity``
is the classification of the fitted review probability profile: the onset is the
earliest point (as a fraction of the history) after which the profile varies by at
most epsilon, and the kind is ``stationary`` (onset zero) or ``stationary tail``
(onset within omega). ``bypass`` restates the integration gate: the share of trunk
commits that entered without a merged pull request, over the commits whose class is
decided (cherry-picks and ambiguous attributions are excluded from the
denominator). ``merges`` and ``reviewers`` are totals over the full history with
per-month rates: merged pull requests per month of history, and the mean number of
distinct reviewers active per month. ``scale`` holds the Python source lines at the
released head, the core contributors (the smallest set of commit authors covering
at least 80% of trunk commits), and the age of the history in months.
``participation`` is the discussion rate -- the share of merged pull requests with
at least one comment by a user other than the author before close -- with the
stratum it falls into and whether the repository was drawn into the audit sample.

Sources. The cohort membership is the ``scope-repository-cohort`` asset, the
selection scores come from ``scope-repository-signal``, review and discussion
metrics from ``scope-issue-catalog`` and ``scope-change-discussion``, commit
attribution (bypass, core contributors, age) from ``scope-commit-attribution``,
the review profiles from ``scope-repository-review``, and the sampled flag from
``scope-repository-sample``. The stationarity thresholds and the stratum cuts
restate the constants of the corresponding assets; they are not re-estimated here.

SLOC. Python source lines are counted with ``cloc`` over the tree at each
repository's released head, materialised from the bare-git tarball of
``scope-source-raw``. The ``cloc`` binary must be on PATH. Submodule gitlinks and
blobs missing from the pack are skipped. ``canonical/mysql-operator`` was archived
on a stray ``readme`` branch, so its head is overridden to ``origin/main``.

Writes are atomic: the export is written to a temp file and ``replace``d into
place, so an interrupted run never leaves a half-written ``cohort.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import polars as pl
import pygit2
from google.cloud import storage

from benchmark.attribution.history import VersionControl
from benchmark.definitions.partitions import Shard
from benchmark.scope.attribution import PushClassification
from benchmark.scope.profiling import Stationarity
from benchmark_notebook.io import CloudStorage

# Constants restated from the scope assets: the stationarity thresholds of
# scope-repository-stationary and the discussion-rate cuts of scope-repository-sample.
EPSILON = 0.01
OMEGA = 0.10
BREAKS = [0.32, 0.48]
STRATA = ["low", "mid", "high"]

# Days per month used to convert the history span into months.
MONTH = 30.44

# canonical/mysql-operator was archived on a stray "readme" branch (its recorded
# trunk is wrong upstream); the real default branch is present as origin/main.
OVERRIDE = {"canonical/mysql-operator": "refs/remotes/origin/main"}

GITLINK = 0o160000


class Progress:
    """Report loading phases to stderr."""

    def phase(self, message: str) -> None:
        """Report the start of a loading phase."""
        print(message, file=sys.stderr, flush=True)


class Store:
    """Read parquet assets through the notebook cloud storage cache."""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        self._storage = CloudStorage(bucket=bucket, project=project)

    def frame(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """Return an asset partition as a polars frame, over every partition on a wildcard."""
        table = self._storage.asset(
            key=key, version=version, location=location, partition=partition, columns=columns
        )
        return pl.from_arrow(table)


def peer_reviews(catalog: pl.DataFrame) -> pl.DataFrame:
    """Return one row per review by a user other than the change author.

    The catalog rows carry the review events of a merged pull request as a list;
    bot reviews and self-reviews do not constitute peer review and are dropped.
    """
    return (
        catalog.select("name", "author", "number", "closed", "reviews")
        .explode("reviews")
        .with_columns(
            pl.col("reviews").struct.field("type").alias("kind"),
            pl.col("reviews").struct.field("author").alias("reviewer"),
        )
        .filter((pl.col("kind") == "user") & (pl.col("reviewer") != pl.col("author")))
    )


def review_metrics(catalog: pl.DataFrame) -> pl.DataFrame:
    """Return merges, peer-review coverage, and reviewer metrics per repository.

    Coverage is the share of merged pull requests with at least one peer review.
    ``reviewers`` counts distinct reviewers over the full history, ``reviewer_rate``
    the mean number of distinct reviewers active per month with review activity.
    """
    reviews = peer_reviews(catalog)

    rate = (
        reviews.with_columns(
            pl.col("closed")
            .str.to_datetime("%Y-%m-%dT%H:%M:%SZ", time_zone="UTC")
            .dt.truncate("1mo")
            .alias("month")
        )
        .group_by("name", "month")
        .agg(pl.col("reviewer").n_unique().alias("active"))
        .group_by("name")
        .agg(pl.col("active").mean().alias("reviewer_rate"))
    )

    return (
        catalog.select("name", "number")
        .join(
            reviews.select("name", "number")
            .unique()
            .with_columns(pl.lit(True).alias("peer")),
            on=["name", "number"],
            how="left",
        )
        .with_columns(pl.col("peer").fill_null(False))
        .group_by("name")
        .agg(
            pl.len().alias("merges"),
            pl.col("peer").mean().alias("coverage"),
        )
        .join(
            reviews.group_by("name").agg(
                pl.col("reviewer").n_unique().alias("reviewers")
            ),
            on="name",
            how="left",
        )
        .with_columns(pl.col("reviewers").fill_null(0))
        .join(rate, on="name", how="left")
        .with_columns(pl.col("reviewer_rate").fill_null(0.0))
    )


def participation_metrics(
    catalog: pl.DataFrame, discussion: pl.DataFrame
) -> pl.DataFrame:
    """Return the discussion rate per repository.

    A merged pull request counts as discussed when at least one user other than
    its author commented before it closed; the rate is the discussed share over
    every merged pull request, including those without any comment thread.
    """
    return (
        discussion.join(
            catalog.select("name", "number", "author", "closed").rename(
                {"author": "pr_author"}
            ),
            on=["name", "number"],
            how="inner",
        )
        .explode("comments")
        .unnest("comments")
        .filter(
            (pl.col("type") == "user")
            & (pl.col("author") != pl.col("pr_author"))
            & (pl.col("created") <= pl.col("closed"))
        )
        .group_by("name", "number")
        .agg(pl.len().alias("comments"))
        .with_columns((pl.col("comments") > 0).alias("discussed"))
        .join(catalog.select("name", "number"), on=["name", "number"], how="right")
        .with_columns(pl.col("discussed").fill_null(False))
        .group_by("name")
        .agg(pl.col("discussed").mean().alias("share"))
    )


def contribution_metrics(attribution: pl.DataFrame) -> pl.DataFrame:
    """Return the history age in months and the core contributors per repository.

    The core contributors are the smallest set of commit authors that together
    cover at least 80% of the attributed trunk commits; commits without a
    resolvable login are excluded from the covering.
    """
    commits = attribution.with_columns(
        pl.col("payload").str.json_path_match("$.author.user.login").alias("login"),
        pl.col("payload")
        .str.json_path_match("$.committedDate")
        .str.to_datetime("%Y-%m-%dT%H:%M:%SZ", time_zone="UTC")
        .alias("committed"),
    )

    age = commits.group_by("name").agg(
        ((pl.col("committed").max() - pl.col("committed").min()).dt.total_days() / MONTH)
        .round(0)
        .cast(pl.Int64)
        .alias("age")
    )

    core = (
        commits.filter(pl.col("login").is_not_null())
        .group_by("name", "login")
        .agg(pl.len().alias("commits"))
        .sort(["name", "commits"], descending=[False, True])
        .with_columns(
            (
                pl.col("commits").cum_sum().over("name")
                / pl.col("commits").sum().over("name")
            ).alias("covered"),
            pl.int_range(1, pl.len() + 1).over("name").alias("rank"),
        )
        .filter(pl.col("covered") >= 0.80)
        .group_by("name")
        .agg(pl.col("rank").min().alias("core"))
    )

    return age.join(core, on="name", how="inner")


def bypass_metrics(attribution: pl.DataFrame) -> pl.DataFrame:
    """Return the bypass share and the commit class counts per repository.

    Each trunk commit is classified by its merge attribution; the share is
    bypassed over bypassed plus attributed, so cherry-picks and ambiguous
    attributions do not enter the denominator.
    """
    classifier = PushClassification()
    classified = attribution.with_columns(
        pl.struct(["payload", "branch", "name"])
        .map_elements(classifier.label, return_dtype=pl.Utf8)
        .alias("class")
    )

    counts = (
        classified.group_by("name", "class")
        .len()
        .pivot(on="class", index="name", values="len")
    )
    for klass in ("bypassed", "attributed", "picked", "ambiguous"):
        if klass not in counts.columns:
            counts = counts.with_columns(pl.lit(0).cast(pl.UInt32).alias(klass))

    return counts.fill_null(0).with_columns(
        (pl.col("bypassed") / (pl.col("bypassed") + pl.col("attributed"))).alias(
            "bypass_share"
        )
    )


def stationarity_metrics(reviews: pl.DataFrame) -> pl.DataFrame:
    """Return the stationarity onset and kind of each fitted review profile."""
    fitted = reviews.filter(pl.col("profile").is_not_null())
    probability = np.stack(
        [np.asarray(p[1], float) for p in fitted["profile"].to_list()]
    )
    metrics = Stationarity(epsilon=EPSILON, omega=OMEGA).transform(probability)
    return pl.DataFrame(
        {
            "name": fitted["name"].to_list(),
            "onset": metrics["onset"],
            "kind": [str(kind) for kind in metrics["kind"]],
        }
    )


def record(row: dict, sampled: bool) -> dict:
    """Return the JSON record of one cohort repository."""

    def rounded(value) -> float:
        return round(float(value), 4)

    return {
        "repository": row["name"],
        "organization": row["name"].split("/", 1)[0],
        "review": {
            "score": rounded(row["review_score"]),
            "low": rounded(row["review_low"]),
            "high": rounded(row["review_high"]),
            "coverage": rounded(row["coverage"]),
        },
        "stationarity": {
            "onset": rounded(row["onset"]),
            "kind": row["kind"],
        },
        "bypass": {
            "share": rounded(row["bypass_share"]),
            "attributed": int(row["attributed"]),
            "bypassed": int(row["bypassed"]),
            "picked": int(row["picked"]),
            "ambiguous": int(row["ambiguous"]),
        },
        "merges": {
            "total": int(row["merges"]),
            "rate": rounded(row["merges"] / row["age"]),
        },
        "reviewers": {
            "total": int(row["reviewers"]),
            "rate": rounded(row["reviewer_rate"]),
        },
        "scale": {
            "sloc": int(row["sloc"]),
            "core": int(row["core"]),
            "age": int(row["age"]),
        },
        "participation": {
            "share": rounded(row["share"]),
            "stratum": row["stratum"],
            "sampled": sampled,
        },
    }


class Sloc:
    """Count Python source lines at each repository's released head.

    The bare-git tarball of ``scope-source-raw`` is downloaded and extracted into a
    temporary directory, the tree at the head commit is materialised file by file,
    and ``cloc`` reports the Python code lines over it. The shard of a repository
    is its rank in the full stationary name set, so the stationary names are held
    even when only a subset of repositories is counted.
    """

    def __init__(
        self,
        bucket: str,
        stationary: list[str],
        version: str = "4.0.0",
        location: str = "main",
        project: str | None = None,
    ) -> None:
        self._client = storage.Client(project=project)
        self._bucket = bucket
        self._stationary = sorted(stationary)
        self._version = version
        self._location = location

    def of(self, name: str) -> int:
        """Return the Python code lines at the released head of a repository."""
        with tempfile.TemporaryDirectory() as work:
            control = VersionControl.create(
                self._client,
                bucket=self._bucket,
                key="scope-source-raw",
                location=self._location,
                version=self._version,
                partition=Shard.to_space(self._stationary, name),
                workdir=work,
            )
            git = control.handle
            head = (
                git.lookup_reference(OVERRIDE[name]) if name in OVERRIDE else git.head
            )
            index = pygit2.Index()
            index.read_tree(head.peel(pygit2.Tree))

            tree = Path(work) / "tree"
            for entry in index:
                if entry.mode == GITLINK or entry.id not in git:
                    continue
                destination = tree / entry.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(git[entry.id].data)

            report = json.loads(
                subprocess.run(
                    ["cloc", "--json", "--quiet", str(tree)],
                    capture_output=True,
                    text=True,
                ).stdout
                or "{}"
            )
            return int(report.get("Python", {}).get("code", 0))


class Export:
    """Drive the cohort export across the repositories."""

    def __init__(
        self,
        bucket: str,
        outdir: Path,
        location: str,
        repositories: list[str],
        project: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._outdir = outdir
        self._location = location
        self._filter = set(repositories)
        self._project = project
        self._store = Store(bucket, project)
        self._progress = Progress()

    def run(self) -> dict:
        """Export the cohort and return the run statistics."""
        self._outdir.mkdir(parents=True, exist_ok=True)

        self._progress.phase("loading cohort membership")
        cohort = self._store.frame(
            "scope-repository-cohort", "6.0.0", self._location, "default",
            columns=["name"],
        ).select("name")
        names = sorted(cohort["name"].to_list())
        if self._filter:
            unknown = sorted(self._filter - set(names))
            if unknown:
                raise ValueError(
                    "repositories not in the cohort: " + ", ".join(unknown)
                )
            names = [name for name in names if name in self._filter]
            cohort = cohort.filter(pl.col("name").is_in(names))

        self._progress.phase("loading selection scores")
        signal = self._store.frame(
            "scope-repository-signal", "2.0.0", self._location, "default",
            columns=["name", "review_score", "review_low", "review_high"],
        ).join(cohort, on="name", how="inner")

        self._progress.phase("loading issue catalog")
        catalog = (
            self._store.frame(
                "scope-issue-catalog", "4.0.0", self._location, "default",
                columns=["name", "type", "author", "number", "closed", "payload"],
            )
            .filter(pl.col("type") == "change")
            .select(
                "name",
                "author",
                "number",
                "closed",
                pl.col("payload").struct.field("reviews").alias("reviews"),
            )
            .join(cohort, on="name", how="inner")
        )

        self._progress.phase("loading discussions")
        discussion = self._store.frame(
            "scope-change-discussion", "1.0.0", self._location, "default",
        ).join(cohort, on="name", how="inner")

        self._progress.phase("loading commit attribution")
        attribution = (
            self._store.frame(
                "scope-commit-attribution", "1.0.0", self._location, "default",
                columns=["name", "branch", "payload"],
            )
            .filter(pl.col("payload").is_not_null())
            .join(cohort, on="name", how="inner")
        )

        self._progress.phase("loading review profiles")
        reviews = self._store.frame(
            "scope-repository-review", "3.0.0", self._location, "*",
            columns=["name", "profile"],
        ).join(cohort, on="name", how="inner")

        self._progress.phase("loading audit sample")
        sampled = set(
            self._store.frame(
                "scope-repository-sample", "1.0.0", self._location, "default",
                columns=["name"],
            )["name"].to_list()
        )

        stationary = sorted(
            self._store.frame(
                "scope-repository-stationary", "1.0.0", self._location, "default",
                columns=["name"],
            )["name"].to_list()
        )

        self._progress.phase("counting source lines")
        sloc = Sloc(
            self._bucket, stationary, location=self._location, project=self._project
        )
        lines = []
        for name in names:
            self._progress.phase(f"counting {name}")
            lines.append({"name": name, "sloc": sloc.of(name)})

        frame = (
            review_metrics(catalog)
            .join(participation_metrics(catalog, discussion), on="name", how="inner")
            .join(contribution_metrics(attribution), on="name", how="inner")
            .join(bypass_metrics(attribution), on="name", how="inner")
            .join(stationarity_metrics(reviews), on="name", how="inner")
            .join(signal, on="name", how="inner")
            .join(pl.DataFrame(lines), on="name", how="inner")
            .with_columns(
                pl.col("share")
                .cut(breaks=BREAKS, labels=STRATA)
                .cast(pl.Utf8)
                .alias("stratum")
            )
            .sort("name")
        )
        if frame.height != len(names):
            missing = sorted(set(names) - set(frame["name"].to_list()))
            raise ValueError(
                "repositories without complete metrics: " + ", ".join(missing)
            )

        self._progress.phase("writing cohort")
        path = self._outdir / "cohort.jsonl"
        temp = self._outdir / ".cohort.jsonl.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            for row in frame.iter_rows(named=True):
                handle.write(json.dumps(record(row, row["name"] in sampled)) + "\n")
        temp.replace(path)

        return {
            "repositories": frame.height,
            "sampled": sum(1 for name in names if name in sampled),
        }


class Cli:
    """Command-line entrypoint for the cohort export."""

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
