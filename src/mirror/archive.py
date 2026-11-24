import argparse
import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from google.cloud import storage

from benchmark.definitions.partitions import Shard


class MirrorSchema:
    """Creates the SQLite schema the mirror server reads from."""

    def create(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE record (
                repo TEXT NOT NULL,
                kind TEXT NOT NULL,
                number INTEGER NOT NULL,
                payload BLOB NOT NULL,
                PRIMARY KEY (repo, kind, number)
            );
            CREATE TABLE entry (
                repo TEXT NOT NULL,
                number INTEGER NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                state TEXT NOT NULL,
                author TEXT,
                created TEXT,
                closed TEXT,
                comments INTEGER NOT NULL,
                PRIMARY KEY (repo, number)
            );
            CREATE INDEX entry_kind ON entry (repo, kind, number);
            CREATE TABLE membership (
                repo TEXT NOT NULL,
                sha TEXT NOT NULL,
                number INTEGER NOT NULL,
                PRIMARY KEY (repo, sha, number)
            );
            CREATE TABLE edge (
                repo TEXT NOT NULL,
                number INTEGER NOT NULL,
                kind TEXT NOT NULL,
                target TEXT NOT NULL,
                related INTEGER NOT NULL,
                type TEXT,
                close INTEGER NOT NULL,
                creation TEXT,
                actor TEXT,
                sha TEXT
            );
            CREATE INDEX edge_number ON edge (repo, number);
            CREATE INDEX edge_related ON edge (target, related);
            CREATE TABLE repository (
                name TEXT PRIMARY KEY,
                branch TEXT,
                head TEXT,
                description TEXT,
                pulls INTEGER NOT NULL,
                issues INTEGER NOT NULL
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )


class MirrorArchive:
    """Materializes the collected GitHub data of the labeled repositories into a local mirror root.

    The mirror holds exactly what the collection captured: the raw pull request and
    issue payloads of ``scope-change-raw`` and ``scope-issue-raw``, the reference edges
    of ``scope-reference-edge`` and the bare clone of ``scope-source-raw``. Nothing is
    fetched from GitHub. The repositories are the ones that carry annotator labels in
    ``audit-change-annotation`` or ``filter-change-annotation``.
    """

    def __init__(
        self,
        client: storage.Client,
        bucket: str,
        root: Path,
        versions: dict[str, str],
        location: str = "main",
    ) -> None:
        self._client = client
        self._bucket = client.bucket(bucket)
        self._root = root
        self._versions = versions
        self._location = location

    def build(self) -> list[str]:
        """Write ``mirror.sqlite`` and one bare repository per labeled repository below the root."""
        repos = self.repositories()
        self._root.mkdir(parents=True, exist_ok=True)
        database = self._root / "mirror.sqlite"
        partial = self._root / "mirror.sqlite.partial"
        partial.unlink(missing_ok=True)

        connection = sqlite3.connect(partial)
        MirrorSchema().create(connection)

        records = self._records(repos)
        self._insert(connection, records)
        self._edges(connection, repos)

        names = sorted(
            self._tables("scope-repository-stationary", columns=["name"])[0]
            .column("name")
            .to_pylist()
        )

        for repo in repos:
            self._source(repo, names)

        self._summarize(connection, repos)
        connection.execute(
            "INSERT INTO meta VALUES (?, ?)",
            ("built", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        connection.execute(
            "INSERT INTO meta VALUES (?, ?)",
            ("versions", json.dumps(self._versions, sort_keys=True)),
        )
        connection.commit()
        connection.close()
        partial.replace(database)
        return repos

    def repositories(self) -> list[str]:
        """Return the repositories that carry at least one annotator label."""
        names: set[str] = set()

        for key in ("audit-change-annotation", "filter-change-annotation"):
            for table in self._tables(key, columns=["repository"]):
                names.update(table.column("repository").to_pylist())

        return sorted(names)

    def _prefix(self, key: str) -> str:
        return f"assets/{key}/version={self._versions[key]}/location={self._location}/"

    def _tables(
        self, key: str, columns: list[str] | None = None, repos: list[str] | None = None
    ) -> list[pa.Table]:
        blobs = [
            blob
            for blob in self._client.list_blobs(self._bucket, prefix=self._prefix(key))
            if blob.name.endswith(".parquet")
        ]

        with ThreadPoolExecutor(max_workers=16) as pool:
            tables = list(pool.map(self._read, blobs, [columns] * len(blobs), [repos] * len(blobs)))

        return [table for table in tables if table.num_rows]

    def _read(self, blob, columns: list[str] | None, repos: list[str] | None) -> pa.Table:
        table = pq.read_table(io.BytesIO(blob.download_as_bytes()), columns=columns)

        if repos is None:
            return table

        return table.filter(pc.is_in(table.column("name"), pa.array(repos)))

    def _records(self, repos: list[str]) -> pa.Table:
        tables = self._tables("scope-change-raw", repos=repos) + self._tables(
            "scope-issue-raw", repos=repos
        )
        return pa.concat_tables(tables, promote_options="permissive")

    def _insert(self, connection: sqlite3.Connection, records: pa.Table) -> None:
        import zlib

        seen: set[tuple[str, str, int]] = set()

        for repo, kind, number, payload in zip(
            records.column("name").to_pylist(),
            records.column("type").to_pylist(),
            records.column("number").to_pylist(),
            records.column("payload").to_pylist(),
        ):
            # The raw assets carry a "default" partition that duplicates every shard,
            # so the first occurrence of (repo, kind, number) wins.
            if (repo, kind, number) in seen:
                continue

            seen.add((repo, kind, number))
            connection.execute(
                "INSERT INTO record VALUES (?, ?, ?, ?)",
                (repo, kind, number, zlib.compress(payload.encode("utf-8"), 6)),
            )
            data = json.loads(payload)

            if kind == "commit":
                connection.executemany(
                    "INSERT OR IGNORE INTO membership VALUES (?, ?, ?)",
                    [(repo, item["sha"], number) for item in data],
                )

            if kind not in ("pr", "issue"):
                continue

            merged = kind == "pr" and bool(data.get("merged_at"))
            connection.execute(
                "INSERT OR REPLACE INTO entry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo,
                    number,
                    "pull" if kind == "pr" else "issue",
                    data.get("title") or "",
                    "merged" if merged else data.get("state") or "closed",
                    (data.get("user") or {}).get("login"),
                    data.get("created_at"),
                    data.get("closed_at"),
                    int(data.get("comments") or 0),
                ),
            )

            if kind == "pr" and data.get("merge_commit_sha"):
                connection.execute(
                    "INSERT OR IGNORE INTO membership VALUES (?, ?, ?)",
                    (repo, data["merge_commit_sha"], number),
                )

    def _edges(self, connection: sqlite3.Connection, repos: list[str]) -> None:
        for table in self._tables("scope-reference-edge", repos=repos):
            for row in table.to_pylist():
                related = row["related"] or {}
                connection.execute(
                    "INSERT INTO edge VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["name"],
                        row["number"],
                        row["kind"],
                        related.get("name") or row["name"],
                        related.get("number"),
                        related.get("type"),
                        int(bool(row["close"])),
                        row["creation"],
                        row["actor"],
                        row["commit"],
                    ),
                )

    def _source(self, repo: str, names: list[str]) -> None:
        target = self._root / f"{repo}.git"

        if target.is_dir():
            return

        prefix = self._prefix("scope-source-raw") + f"partition={Shard.to_space(names, repo)}/"
        archives = [
            blob
            for blob in self._client.list_blobs(self._bucket, prefix=prefix)
            if blob.name.endswith(".tar.gz")
        ]

        if len(archives) != 1:
            raise FileNotFoundError(f"Expected one source archive under {prefix}")

        with tempfile.TemporaryDirectory(dir=self._root) as directory:
            work = Path(directory)
            archive = work / "source.tar.gz"
            archives[0].download_to_filename(str(archive))

            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(work, filter="data")

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work / "repository.git"), str(target))

    def _summarize(self, connection: sqlite3.Connection, repos: list[str]) -> None:
        import pygit2

        stationary = {
            row["name"]: row
            for table in self._tables(
                "scope-repository-stationary", columns=["name", "description"]
            )
            for row in table.to_pylist()
        }

        for repo in repos:
            handle = pygit2.Repository(str(self._root / f"{repo}.git"))
            branch = handle.head.shorthand if not handle.head_is_unborn else None
            head = str(handle.head.target) if branch else None
            pulls, issues = (
                connection.execute(
                    "SELECT COUNT(*) FROM entry WHERE repo = ? AND kind = ?", (repo, kind)
                ).fetchone()[0]
                for kind in ("pull", "issue")
            )
            connection.execute(
                "INSERT INTO repository VALUES (?, ?, ?, ?, ?, ?)",
                (
                    repo,
                    branch,
                    head,
                    (stationary.get(repo) or {}).get("description"),
                    pulls,
                    issues,
                ),
            )


class MirrorArchiveCommand:
    """Command line entry point that builds the mirror root from the bucket."""

    def run(self, arguments: list[str] | None = None) -> None:
        import os

        parser = argparse.ArgumentParser(description=MirrorArchive.__doc__)
        parser.add_argument("root", type=Path)
        parser.add_argument("--bucket", default=os.environ.get("CLOUD_BUCKET"))
        parser.add_argument("--project", default=os.environ.get("CLOUD_PROJECT"))
        parser.add_argument("--change", default="7.0.0")
        parser.add_argument("--issue", default="6.0.0")
        parser.add_argument("--edge", default="1.0.0")
        parser.add_argument("--source", default="4.0.0")
        parser.add_argument("--stationary", default="1.0.0")
        parser.add_argument("--audit", default="1.0.0")
        parser.add_argument("--filter", default="1.0.0")
        options = parser.parse_args(arguments)

        archive = MirrorArchive(
            storage.Client(project=options.project),
            options.bucket,
            options.root,
            {
                "scope-change-raw": options.change,
                "scope-issue-raw": options.issue,
                "scope-reference-edge": options.edge,
                "scope-source-raw": options.source,
                "scope-repository-stationary": options.stationary,
                "audit-change-annotation": options.audit,
                "filter-change-annotation": options.filter,
            },
        )
        repos = archive.build()
        print(f"Mirrored {len(repos)} repositories into {options.root}: {', '.join(repos)}")


if __name__ == "__main__":
    MirrorArchiveCommand().run()
