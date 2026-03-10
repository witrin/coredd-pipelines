from __future__ import annotations

import collections
import json
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pygit2
from google.cloud import storage
from benchmark.attribution.domain import Change, Commit, FileOperation, OperationType


class VersionControl:
    """Git operations backed by a pygit2 repository."""

    def __init__(self, repository: pygit2.Repository) -> None:
        self._repository = repository
        self._cache: dict[str, set[str]] = {}

    @staticmethod
    def create(
        client: storage.Client,
        bucket: str,
        key: str,
        location: str,
        version: str,
        partition: str,
        *,
        workdir: str | Path | None = None,
    ) -> VersionControl:
        """Download a bare repository from GCS and return a VersionControl instance."""
        work = Path(workdir or tempfile.mkdtemp())

        blob_prefix = (
            f"assets/{key}/"
            f"version={version}/"
            f"location={location}/"
            f"partition={partition}"
        )

        gcs_bucket = client.bucket(bucket)
        blobs = list(gcs_bucket.list_blobs(prefix=blob_prefix))
        tar_blobs = [b for b in blobs if b.name.endswith(".tar.gz")]

        if not tar_blobs:
            raise FileNotFoundError(
                f"No tar.gz found under gs://{bucket}/{blob_prefix}"
            )

        archive = work / "repository.tar.gz"
        tar_blobs[0].download_to_filename(str(archive))

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(work, filter="data")
        archive.unlink()

        git_dir = VersionControl._find_git_dir(work)
        return VersionControl(pygit2.Repository(str(git_dir)))

    @staticmethod
    def _find_git_dir(repo_dir: Path) -> Path:
        for candidate in repo_dir.rglob("HEAD"):
            parent = candidate.parent
            if (parent / "objects").is_dir() and (parent / "refs").is_dir():
                return parent

        raise FileNotFoundError(
            f"No bare git repository found under {repo_dir}"
        )

    def parents(self, commit: str) -> list[str]:
        """Return parent SHAs of *commit*."""
        obj = self._repository.get(commit)
        if obj is None:
            raise ValueError(f"Commit {commit} not found in repository")

        c = obj.peel(pygit2.Commit)
        return [str(pid) for pid in c.parent_ids]

    def _first_parent_set(self, start: str) -> set[str]:
        if start in self._cache:
            return self._cache[start]

        shas: set[str] = set()
        current = start

        while current:
            shas.add(current)
            obj = self._repository.get(current)
            if obj is None:
                break
            c = obj.peel(pygit2.Commit)
            if not c.parent_ids:
                break
            current = str(c.parent_ids[0])

        self._cache[start] = shas
        return shas

    def blame(
        self, path: str, commit: str, lines: list[tuple[int, int]]
    ) -> list[tuple[str | None, tuple[int, int] | None]]:
        """Return aligned blame results for *lines* at *commit*.

        *lines* is a list of ``(start_line, end_line)`` 1-indexed inclusive
        ranges. The result has the same length and order. Each entry contains
        the blamed commit SHA and the corresponding origin line range in that
        blamed commit, or ``(None, None)`` when the range cannot be resolved to
        one contiguous blame hunk.
        """
        oid = pygit2.Oid(hex=commit)
        blame_result = self._repository.blame(path, newest_commit=oid)
        result: list[tuple[str | None, tuple[int, int] | None]] = []

        for start_line, end_line in lines:
            if start_line > end_line:
                result.append((None, None))
                continue

            commit_sha: str | None = None
            origin_start: int | None = None
            origin_end: int | None = None
            valid = True

            for line in range(start_line, end_line + 1):
                try:
                    hunk = blame_result.for_line(line)
                except IndexError:
                    valid = False
                    break

                sha = str(hunk.final_commit_id)
                origin_line = (
                    hunk.orig_start_line_number
                    + line
                    - hunk.final_start_line_number
                )

                if commit_sha is None:
                    commit_sha = sha
                    origin_start = origin_line
                    origin_end = origin_line
                    continue

                if sha != commit_sha or origin_line != origin_end + 1:
                    valid = False
                    break

                origin_end = origin_line

            if (
                not valid
                or commit_sha is None
                or origin_start is None
                or origin_end is None
            ):
                result.append((None, None))
                continue

            result.append((commit_sha, (origin_start, origin_end)))

        return result

    def materialize(
        self, base: str, after: str, directory: str | Path
    ) -> tuple[Path, Path, list[FileOperation]]:
        """Checkout tree contents of two commits into separate directories."""
        directory = Path(directory)
        base_dir = directory / "base"
        after_dir = directory / "after"

        self._checkout_tree(base, base_dir)
        self._checkout_tree(after, after_dir)

        operations = self._build_manifest(base, after)
        return base_dir, after_dir, operations

    def _checkout_tree(self, commit_sha: str, target: Path) -> None:
        obj = self._repository.get(commit_sha)
        if obj is None:
            raise ValueError(f"Commit {commit_sha} not found")

        c = obj.peel(pygit2.Commit)
        target.mkdir(parents=True, exist_ok=True)
        self._write_tree(c.tree, target)

    def _build_manifest(self, base: str, after: str) -> list[FileOperation]:
        _STATUS_MAP = {
            1: OperationType.ADD,
            2: OperationType.DELETE,
            3: OperationType.EDIT,
            4: OperationType.MOVE,
        }

        base_commit = self._repository.get(base).peel(pygit2.Commit)
        after_commit = self._repository.get(after).peel(pygit2.Commit)
        diff = self._repository.diff(base_commit.tree, after_commit.tree)

        ops: list[FileOperation] = []
        for delta in diff.deltas:
            op_type = _STATUS_MAP.get(delta.status)
            if op_type is None:
                continue
            if op_type == OperationType.ADD:
                ops.append(FileOperation(base=None, after=delta.new_file.path, operation=op_type))
            elif op_type == OperationType.DELETE:
                ops.append(FileOperation(base=delta.old_file.path, after=None, operation=op_type))
            else:
                ops.append(FileOperation(base=delta.old_file.path, after=delta.new_file.path, operation=op_type))

        return sorted(ops, key=lambda o: o.after or o.base)

    def manifest(self, base: str, after: str) -> list[FileOperation]:
        """Return file operations between *base* and *after* without writing to disk."""
        return self._build_manifest(base, after)

    def meta(self, commit: str) -> Commit:
        """Return metadata for *commit*."""
        obj = self._repository.get(commit)
        if obj is None:
            raise ValueError(f"Commit {commit} not found in repository")
        c = obj.peel(pygit2.Commit)
        return Commit(
            sha=commit,
            message=c.message.strip(),
            time=datetime.fromtimestamp(c.commit_time, tz=timezone.utc),
            author=c.author.name,
        )

    def log(self, base: str, after: str, merge: bool = False) -> list[str]:
        """Return commit SHAs between *base* and *after* (both exclusive).

        Results are sorted newest-first.  When *merge* is False (default),
        merge commits (commits with more than one parent) are excluded.
        """
        result: dict[str, int] = {}
        queue: collections.deque[str] = collections.deque()
        visited: set[str] = set()

        after_obj = self._repository.get(after)
        if after_obj is None:
            raise ValueError(f"Commit {after} not found in repository")
        after_commit = after_obj.peel(pygit2.Commit)
        for pid in after_commit.parent_ids:
            queue.append(str(pid))

        while queue:
            sha = queue.popleft()
            if sha in visited or sha == base:
                continue
            visited.add(sha)

            obj = self._repository.get(sha)
            if obj is None:
                continue
            c = obj.peel(pygit2.Commit)

            is_merge = len(c.parent_ids) > 1
            if merge or not is_merge:
                result[sha] = c.commit_time

            for pid in c.parent_ids:
                s = str(pid)
                if s != base and s not in visited:
                    queue.append(s)

        return sorted(result, key=lambda s: result[s], reverse=True)

    def _write_tree(self, tree: pygit2.Tree, target: Path) -> None:
        for entry in tree:
            path = target / entry.name

            if entry.type_str == "tree":
                path.mkdir(parents=True, exist_ok=True)
                subtree = self._repository.get(entry.id)
                self._write_tree(subtree, path)
            elif entry.type_str == "blob":
                blob = self._repository.get(entry.id)
                path.write_bytes(blob.data)

    def diff(
        self,
        base: str,
        after: str,
        *,
        context: int = 0,
    ) -> pygit2.Diff:
        """
        Return libgit2-backed diff between base and after.
        Context defaults to zero. Callers may request a regular unified-diff
        context width when needed.
        """

        base_obj = self._repository.get(base)
        if base_obj is None:
            raise ValueError(f"Commit {base} not found")

        after_obj = self._repository.get(after)
        if after_obj is None:
            raise ValueError(f"Commit {after} not found")

        base_commit = base_obj.peel(pygit2.Commit)
        after_commit = after_obj.peel(pygit2.Commit)

        diff = self._repository.diff(
            base_commit.tree,
            after_commit.tree,
            context_lines=context,
            interhunk_lines=0,
        )

        diff.find_similar()  # enables rename detection

        return diff

    def blob(self, commit: str, path: str) -> bytes:
        """
        Return raw file contents (bytes) for `path`
        at `commit`.
        """

        obj = self._repository.get(commit)
        if obj is None:
            raise ValueError(f"Commit {commit} not found")

        commit_obj = obj.peel(pygit2.Commit)

        try:
            entry = commit_obj.tree[path]
        except KeyError:
            raise FileNotFoundError(f"{path} not found in commit {commit}")

        blob = self._repository.get(entry.id)

        if not isinstance(blob, pygit2.Blob):
            raise ValueError(f"{path} is not a blob in commit {commit}")

        return blob.data

class PlatformCache:
    """Parquet-backed cache containing PR metadata.

    Schema: name (string), type (string), number (int32), payload (large_string)
    """

    def __init__(self, table: pa.Table, repository: VersionControl) -> None:
        self._table = table
        self._repository = repository
        self._cache: dict[str, Change] = self._build_cache()

    def _build_cache(self) -> dict[str, Change]:
        """Pre-compute a merge-commit-SHA → Change lookup dictionary."""
        import pyarrow.compute as pc

        cache: dict[str, Change] = {}
        pr_mask = pc.equal(self._table["type"], "pr")
        prs = self._table.filter(pr_mask)

        for i in range(prs.num_rows):
            number = prs.column("number")[i].as_py()
            payload = json.loads(prs.column("payload")[i].as_py())

            if not payload.get("merged"):
                continue

            commit = payload.get("merge_commit_sha")
            if not commit:
                raise ValueError(
                    f"Pull request {number} merge_commit_sha is missing or empty"
                )

            try:
                parents = self._repository.parents(commit)
            except Exception as e:
                raise ValueError(
                    f"Pull request {number} merge_commit_sha is missing or invalid"
                ) from e
            # if len(parent_shas) != 2:
            #     raise NotImplementedError(
            #         f"Pull request {number} merge_commit_sha {merge_sha} has "
            #         f"{len(parent_shas)} parent(s), expected 2 (merge commit). "
            #         f"Squash and rebase are not yet supported."
            #     )

            change = self._change_from_payload(number, payload, parents[0])
            cache[commit] = change

        return cache

    @staticmethod
    def create(
        client: storage.Client,
        bucket: str,
        key: str,
        location: str,
        version: str,
        partition: str,
        repository: VersionControl,
        filter: function = None
    ) -> PlatformCache:
        """Download a Parquet asset from GCS and return a PlatformCache instance."""
        gcs_bucket = client.bucket(bucket)
        blob_path = (
            f"assets/{key}/"
            f"version={version}/"
            f"location={location}/"
            f"partition={partition}/"
            "data.parquet"
        )

        blob = gcs_bucket.blob(blob_path)
        if not blob.exists():
            raise FileNotFoundError(
                f"Parquet not found: gs://{bucket}/{blob_path}"
            )

        data = blob.download_as_bytes()
        table = pq.read_table(pa.BufferReader(data))
        
        if filter:
            table = filter(table)
        
        return PlatformCache(table, repository)

    def load(self, number: int) -> Change:
        """Load a Change by PR number."""
        import pyarrow.compute as pc

        mask = pc.and_(
            pc.equal(self._table["type"], "pr"),
            pc.equal(self._table["number"], number),
        )
        filtered = self._table.filter(mask)

        if filtered.num_rows == 0:
            raise KeyError(f"PR #{number} not found in cache")

        payload = json.loads(filtered.column("payload")[0].as_py())
        commit = payload.get("merge_commit_sha")
        if not commit:
            raise ValueError(
                f"Pull request {number} merge_commit_sha is missing or empty"
            )
        
        try:
            return self._cache[commit]
        except KeyError:
            raise KeyError(f"Commit {commit} not found in any cached pull request")
        # parent_shas = self._repository.parents(merge_sha)
        # if len(parent_shas) != 2:
        #     raise NotImplementedError(
        #         f"Pull request {number} merge_commit_sha {merge_sha} has "
        #         f"{len(parent_shas)} parent(s), expected 2 (merge commit). "
        #         f"Squash and rebase are not yet supported."
        #     )
        # return self._change_from_payload(number, payload, parent_shas[0])

    def resolve(self, commit: str) -> Change:
        """Look up the Change by merge commit SHA."""
        try:
            return self._cache[commit]
        except KeyError:
            raise KeyError(f"Commit {commit} not found in any cached pull request")

    @staticmethod
    def _change_from_payload(number: int, payload: dict, base: str) -> Change:
        merge_sha = payload.get("merge_commit_sha")
        if not merge_sha:
            raise ValueError(
                f"Pull request {number} merge_commit_sha is missing or empty"
            )

        title = payload.get("title") or ""
        body = payload.get("body") or ""
        description = f"{title}\n\n{body}".strip()

        return Change(
            number=number,
            description=description,
            base=base,
            after=merge_sha,
        )
