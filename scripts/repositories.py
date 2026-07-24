"""Download the benchmark repository tarballs from GCS into a target directory.

Companion to ``labels.py``. The CoReDD package lifts a localizer's predicted lines
to syntax nodes against the concrete git history of each benchmark repository, and it
needs that history on the local filesystem. This script fetches the bare-git
repositories -- one ``.tar.gz`` per repository -- so a later step can extract and
open them with pygit2. It only downloads; it does not extract, and it pulls in no
pygit2.

Source. The tarballs live in GCS under the ``scope-source-raw`` asset, one archive
per partition:

    assets/<key>/version=<version>/location=<location>/partition=<shard>

with exactly one ``.tar.gz`` blob under each partition prefix. This is the same
layout ``benchmark.attribution.history.VersionControl.create`` reads: build the
prefix, ``list_blobs(prefix=...)``, take the ``.tar.gz`` blob, ``download_to_filename``.

Shard mechanism. The ``<shard>`` of a repository is not derived from its name alone;
it is ``Shard.to_space(names, name)`` over the *full sorted list* of stationary
repository names -- the population of the ``scope-repository-stationary`` asset. The
space prefix hashes the whole set, and the element index is that repository's rank in
the sorted set, so the correct shard for one repository cannot be computed without the
entire name list. This script therefore always loads the stationary names first, even
when only a single repository is requested.

Output layout. Each archive is written to

    <outdir>/<owner>/<repo>.tar.gz

with the ``<owner>`` directory created as needed. Writes are atomic: the blob is
downloaded to a temp file in the owner directory and then ``replace``d into place, so
an interrupted run never leaves a half-written archive at the final path.

Selection. With no ``--repository`` given, every stationary repository is loaded. When
one or more ``--repository`` names are given, only those are loaded, and each must be a
stationary repository -- a name absent from the stationary set has no shard and is a
hard error, since ``Shard.to_space`` requires the element in the list.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from google.cloud import storage

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


def blob_prefix(key: str, version: str, location: str, shard: str) -> str:
    """Return the GCS blob prefix a repository's tarball lives under.

    The layout matches ``VersionControl.create``: the asset key, then the
    version/location partition axes, then the shard partition. Exactly one
    ``.tar.gz`` blob lives under the returned prefix.
    """
    return (
        f"assets/{key}/"
        f"version={version}/"
        f"location={location}/"
        f"partition={shard}"
    )


def target_path(outdir: Path, name: str) -> Path:
    """Return the local path a repository's tarball is written to.

    A repository name is ``owner/repo``; its archive lands at
    ``<outdir>/<owner>/<repo>.tar.gz``. A name with more than one slash keeps every
    segment but the last as nested owner directories.
    """
    owner, _, repo = name.rpartition("/")
    return outdir / owner / f"{repo}.tar.gz"


def select(names: list[str], requested: list[str]) -> list[str]:
    """Return the repository names to load, in the stationary set's sorted order.

    With no request, every stationary repository is selected. With a request, only the
    named repositories are selected -- each must be a stationary repository, since a
    name absent from the set has no shard. The result is ordered by the stationary set
    so the download order is stable regardless of request order.
    """
    stationary = sorted(names)
    if not requested:
        return stationary
    known = set(stationary)
    missing = sorted(set(requested) - known)
    if missing:
        raise ValueError(
            "repositories not in the stationary set: " + ", ".join(missing)
        )
    wanted = set(requested)
    return [name for name in stationary if name in wanted]


class Repositories:
    """Download the tarball of each selected repository from GCS."""

    def __init__(
        self,
        bucket: str,
        outdir: Path,
        location: str,
        version: str,
        key: str,
        requested: list[str],
        project: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._outdir = outdir
        self._location = location
        self._version = version
        self._key = key
        self._requested = requested
        self._project = project
        self._store = Store(bucket, project)
        self._progress = Progress()
        self._client = storage.Client(project=project)
        self._loaded = 0
        self._bytes = 0

    def run(self) -> dict:
        """Download the selected tarballs and return the run statistics."""
        self._outdir.mkdir(parents=True, exist_ok=True)

        self._progress.phase("loading stationary repository names")
        stationary = [
            row["name"]
            for row in self._store.rows(
                "scope-repository-stationary",
                "1.0.0",
                "main",
                "default",
                columns=["name"],
            )
        ]

        names = select(stationary, self._requested)
        self._progress.phase(f"downloading {len(names)} repositories")

        gcs_bucket = self._client.bucket(self._bucket)
        for name in names:
            shard = Shard.to_space(stationary, name)
            self._progress.phase(f"fetching {name}")
            size = self._download(gcs_bucket, name, shard)
            self._loaded += 1
            self._bytes += size

        return {
            "repositories": self._loaded,
            "bytes": self._bytes,
        }

    def _download(self, gcs_bucket, name: str, shard: str) -> int:
        prefix = blob_prefix(self._key, self._version, self._location, shard)
        blobs = list(gcs_bucket.list_blobs(prefix=prefix))
        tar_blobs = [blob for blob in blobs if blob.name.endswith(".tar.gz")]
        if not tar_blobs:
            raise FileNotFoundError(
                f"No tar.gz found under gs://{self._bucket}/{prefix}"
            )
        blob = tar_blobs[0]

        destination = target_path(self._outdir, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.parent / f".{destination.name}.tmp"
        blob.download_to_filename(str(temp))
        temp.replace(destination)
        return destination.stat().st_size


class Cli:
    """Command-line entrypoint for the repository download."""

    def run(self, argv: list[str] | None = None) -> None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--outdir", type=Path, required=True)
        parser.add_argument("--repository", action="append", default=[])
        parser.add_argument(
            "--bucket", default=os.environ.get("CLOUD_BUCKET", "bug-request-bucket")
        )
        parser.add_argument("--version", default="4.0.0")
        parser.add_argument("--location", default="main")
        parser.add_argument("--key", default="scope-source-raw")
        arguments = parser.parse_args(argv)
        repositories = Repositories(
            bucket=arguments.bucket,
            outdir=arguments.outdir.resolve(),
            location=arguments.location,
            version=arguments.version,
            key=arguments.key,
            requested=arguments.repository,
        )
        stats = repositories.run()
        for name, value in stats.items():
            print(f"{name}: {value}")


if __name__ == "__main__":
    Cli().run()
