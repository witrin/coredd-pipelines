from __future__ import annotations

import collections
import json
import tarfile
import tempfile
from datetime import datetime, timezone
from io import TextIOBase
from pathlib import Path
from typing import Any, Callable, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pygit2
from google.cloud import storage
from benchmark.attribution.domain import Change, Commit, FileOperation, OperationType


class VersionControl:
    """Git operations backed by a pygit2 repository."""

    def __init__(
        self,
        repository: pygit2.Repository,
        filter: Callable[[str, str], str] | None = None,
    ) -> None:
        self._repository = repository
        self._cache: dict[str, set[str]] = {}
        self._filter = filter

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
        filter: Callable[[str, str], str] | None = None,
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
        return VersionControl(pygit2.Repository(str(git_dir)), filter=filter)

    @staticmethod
    def _find_git_dir(repo_dir: Path) -> Path:
        for candidate in repo_dir.rglob("HEAD"):
            parent = candidate.parent
            if (parent / "objects").is_dir() and (parent / "refs").is_dir():
                return parent

        raise FileNotFoundError(f"No bare git repository found under {repo_dir}")

    def parents(self, commit: str) -> list[str]:
        """Return parent SHAs of *commit*."""
        obj = self._repository.get(commit)
        if obj is None:
            raise ValueError(f"Commit {commit} not found in repository")

        c = obj.peel(pygit2.Commit)
        return [str(pid) for pid in c.parent_ids]

    def dump(self, paths: list[list[str]], target: str | Path) -> None:
        target = Path(target)
        trees = target / "trees"
        blobs = target / "blobs"
        target.mkdir(parents=True, exist_ok=True)
        trees.mkdir(parents=True, exist_ok=True)
        blobs.mkdir(parents=True, exist_ok=True)

        shas = self._commits(paths)
        parents: dict[str, list[str]] = {sha: [] for sha in shas}

        for path in paths:
            for index, sha in enumerate(path):
                lineage = [] if index == 0 else [path[index - 1]]

                if parents[sha] and parents[sha] != lineage:
                    raise ValueError(f"Commit {sha} has inconsistent parents")

                parents[sha] = lineage

        commits: dict[str, dict[str, object]] = {}

        for sha in shas:
            obj = self._repository.get(sha)
            if obj is None:
                raise ValueError(f"Commit {sha} not found")

            commit = obj.peel(pygit2.Commit)
            message = commit.message.strip()

            if self._filter is not None:
                message = self._filter("message", message)

            self._dump_tree(commit.tree, target)
            commits[sha] = {
                "tree": str(commit.tree.id),
                "message": message,
                "time": datetime.fromtimestamp(
                    commit.commit_time,
                    tz=timezone.utc,
                ).isoformat(),
                "author": commit.author.name,
                "parents": parents[sha],
            }

        with open(target / "meta.json", "w", encoding="utf-8") as file:
            json.dump(
                {
                    "commits": commits,
                },
                file,
                sort_keys=True,
            )

    def _commits(self, paths: list[list[str]]) -> set[str]:
        result: set[str] = set()

        for path in paths:
            result.update(path)

        return result

    def _dump_tree(self, tree: pygit2.Tree, target: Path) -> None:
        tree_path = target / "trees" / f"{tree.id}.json"

        if tree_path.exists():
            return

        with open(tree_path, "w", encoding="utf-8") as file:
            file.write('{"files":{')
            self._dump_tree_items(tree, "", file, target, True)
            file.write("}}")

    def _dump_tree_items(
        self,
        tree: pygit2.Tree,
        prefix: str,
        file: TextIOBase,
        target: Path,
        first: bool,
    ) -> bool:
        current = first

        for entry in sorted(tree, key=lambda item: item.name):
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            obj = self._repository.get(entry.id)

            if isinstance(obj, pygit2.Tree):
                current = self._dump_tree_items(obj, path, file, target, current)
                continue

            if not isinstance(obj, pygit2.Blob):
                continue

            self._dump_blob(obj, target)

            if not current:
                file.write(",")

            file.write(json.dumps(path))
            file.write(":")
            file.write(json.dumps(str(obj.id)))
            current = False

        return current

    def _dump_blob(self, blob: pygit2.Blob, target: Path) -> None:
        path = target / "blobs" / str(blob.id)

        if path.exists():
            return

        path.write_bytes(blob.data)

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
                    hunk.orig_start_line_number + line - hunk.final_start_line_number
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
                ops.append(
                    FileOperation(
                        base=None, after=delta.new_file.path, operation=op_type
                    )
                )
            elif op_type == OperationType.DELETE:
                ops.append(
                    FileOperation(
                        base=delta.old_file.path, after=None, operation=op_type
                    )
                )
            else:
                ops.append(
                    FileOperation(
                        base=delta.old_file.path,
                        after=delta.new_file.path,
                        operation=op_type,
                    )
                )

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
        message = c.message.strip()
        if self._filter is not None:
            message = self._filter("message", message)
        return Commit(
            sha=commit,
            message=message,
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

    def _files(self, commit: str) -> list[dict[str, object]]:
        obj = self._repository.get(commit)
        if obj is None:
            raise ValueError(f"Commit {commit} not found")

        values: list[dict[str, object]] = []
        self._walk(obj.peel(pygit2.Commit).tree, "", values)
        values.sort(key=lambda value: str(value["path"]))
        return values

    def _walk(
        self,
        tree: pygit2.Tree,
        prefix: str,
        values: list[dict[str, object]],
    ) -> None:
        for entry in tree:
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            obj = self._repository.get(entry.id)
            if isinstance(obj, pygit2.Tree):
                self._walk(obj, path, values)
                continue
            if isinstance(obj, pygit2.Blob):
                values.append(
                    {
                        "path": path,
                        "content": obj.data,
                    }
                )

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


class VersionControlSnapshot:
    """Snapshot-backed git operations replayed into a temporary pygit2 repository."""

    def __init__(
        self,
        repository: pygit2.Repository,
        work: tempfile.TemporaryDirectory[str],
        forward: dict[str, str],
        reverse: dict[str, str],
    ) -> None:
        self._repository = repository
        self._work = work
        self._forward = forward
        self._reverse = reverse
        self._runtime = VersionControl(repository)
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._work.cleanup()
        self._closed = True

    def __enter__(self) -> VersionControlSnapshot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @classmethod
    def load(cls, source: str | Path) -> VersionControlSnapshot:
        source = Path(source)
        meta = cls._load_meta(source)
        commits = cls._commits(meta)
        cls._validate(source, commits)

        work = tempfile.TemporaryDirectory()
        repository = pygit2.init_repository(work.name, bare=True)
        forward: dict[str, str] = {}
        reverse: dict[str, str] = {}
        snapshot = cls(repository, work, forward, reverse)

        trees: dict[str, pygit2.Oid] = {}
        blobs: dict[str, pygit2.Oid] = {}
        pending = list(commits)

        while pending:
            current: list[str] = []
            progress = False

            for sha in pending:
                item = commits[sha]
                parents = cast(list[str], item["parents"])

                if any(parent not in forward for parent in parents):
                    current.append(sha)
                    continue

                tree = snapshot._tree(source, str(item["tree"]), trees, blobs)
                timestamp = datetime.fromisoformat(str(item["time"]))
                moment = int(timestamp.astimezone(timezone.utc).timestamp())
                author = str(item["author"])
                signature = pygit2.Signature(
                    author,
                    "snapshot@test.local",
                    moment,
                )
                imported = str(
                    repository.create_commit(
                        None,
                        signature,
                        signature,
                        str(item["message"]),
                        tree,
                        [pygit2.Oid(hex=forward[parent]) for parent in parents],
                    )
                )
                forward[sha] = imported
                reverse[imported] = sha
                progress = True

            if progress:
                pending = current
                continue

            raise ValueError("Snapshot contains unresolved parent references.")

        return snapshot

    @classmethod
    def _load_meta(cls, source: Path) -> dict[str, object]:
        with open(source / "meta.json", "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError("Snapshot meta.json must contain an object.")

        return cast(dict[str, object], data)

    @classmethod
    def _commits(cls, meta: dict[str, object]) -> dict[str, dict[str, object]]:
        commits = meta.get("commits")

        if not isinstance(commits, dict):
            raise ValueError("Snapshot meta.json is missing commits.")

        result: dict[str, dict[str, object]] = {}

        for sha, item in commits.items():
            if not isinstance(item, dict):
                raise ValueError(f"Snapshot commit {sha} must contain an object.")
            result[str(sha)] = dict(item)

        return result

    @classmethod
    def _validate(
        cls,
        source: Path,
        commits: dict[str, dict[str, object]],
    ) -> None:
        seen: set[str] = set()

        for sha, item in commits.items():
            tree = item.get("tree")
            parents = item.get("parents")

            if not isinstance(tree, str):
                raise ValueError(f"Snapshot commit {sha} is missing tree.")

            if not isinstance(parents, list):
                raise ValueError(f"Snapshot commit {sha} is missing parents.")

            for parent in parents:
                if not isinstance(parent, str):
                    raise ValueError(f"Snapshot commit {sha} has invalid parents.")

                if parent not in commits:
                    raise ValueError(
                        f"Snapshot commit {sha} references missing parent {parent}."
                    )

            tree_path = source / "trees" / f"{tree}.json"

            if not tree_path.exists():
                raise ValueError(f"Snapshot tree {tree} is missing.")

            if tree in seen:
                continue

            seen.add(tree)
            with open(tree_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict):
                raise ValueError(f"Snapshot tree {tree} must contain an object.")

            files = data.get("files")

            if not isinstance(files, dict):
                raise ValueError(f"Snapshot tree {tree} is missing files.")

            for path, blob in files.items():
                if not isinstance(path, str) or not isinstance(blob, str):
                    raise ValueError(f"Snapshot tree {tree} contains invalid entries.")

                if not (source / "blobs" / blob).exists():
                    raise ValueError(f"Snapshot blob {blob} is missing.")

    def parents(self, commit: str) -> list[str]:
        return [
            self._reverse[parent] for parent in self._runtime.parents(self._map(commit))
        ]

    def blame(
        self, path: str, commit: str, lines: list[tuple[int, int]]
    ) -> list[tuple[str | None, tuple[int, int] | None]]:
        result = self._runtime.blame(path, self._map(commit), lines)
        return [
            (
                None if sha is None else self._reverse[sha],
                interval,
            )
            for sha, interval in result
        ]

    def materialize(
        self, base: str, after: str, directory: str | Path
    ) -> tuple[Path, Path, list[FileOperation]]:
        return self._runtime.materialize(
            self._map(base),
            self._map(after),
            directory,
        )

    def manifest(self, base: str, after: str) -> list[FileOperation]:
        return self._runtime.manifest(self._map(base), self._map(after))

    def meta(self, commit: str) -> Commit:
        value = self._runtime.meta(self._map(commit))
        return Commit(
            sha=commit,
            message=value.message,
            time=value.time,
            author=value.author,
        )

    def log(self, base: str, after: str, merge: bool = False) -> list[str]:
        return [
            self._reverse[sha]
            for sha in self._runtime.log(
                self._map(base),
                self._map(after),
                merge=merge,
            )
        ]

    def diff(
        self,
        base: str,
        after: str,
        *,
        context: int = 0,
    ) -> pygit2.Diff:
        return self._runtime.diff(self._map(base), self._map(after), context=context)

    def blob(self, commit: str, path: str) -> bytes:
        return self._runtime.blob(self._map(commit), path)

    def _map(self, commit: str) -> str:
        try:
            return self._forward[commit]
        except KeyError as error:
            raise ValueError(f"Commit {commit} not found in snapshot") from error

    def _tree(
        self,
        source: Path,
        tree: str,
        trees: dict[str, pygit2.Oid],
        blobs: dict[str, pygit2.Oid],
    ) -> pygit2.Oid:
        if tree in trees:
            return trees[tree]

        with open(source / "trees" / f"{tree}.json", "r", encoding="utf-8") as file:
            data = json.load(file)

        files = data["files"]
        root: dict[str, Any] = {}

        for path, blob in cast(dict[str, str], files).items():
            parts = str(path).split("/")
            current = root
            for part in parts[:-1]:
                current = cast(dict[str, Any], current.setdefault(part, {}))
            current[parts[-1]] = self._blob(source, blob, blobs)

        result = self._write(root)
        trees[tree] = result
        return result

    def _blob(
        self,
        source: Path,
        blob: str,
        blobs: dict[str, pygit2.Oid],
    ) -> pygit2.Oid:
        if blob in blobs:
            return blobs[blob]

        data = (source / "blobs" / blob).read_bytes()
        result = self._repository.create_blob(data)
        blobs[blob] = result
        return result

    def _write(self, node: dict[str, Any]) -> pygit2.Oid:
        builder = self._repository.TreeBuilder()

        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                builder.insert(name, self._write(value), pygit2.GIT_FILEMODE_TREE)
                continue
            builder.insert(
                name,
                cast(pygit2.Oid, value),
                pygit2.GIT_FILEMODE_BLOB,
            )

        return builder.write()


class PlatformCache:
    """Parquet-backed cache containing PR metadata.

    Schema: name (string), type (string), number (int32), payload (large_string)
    """

    def __init__(
        self,
        table: pa.Table,
        repository: VersionControl,
        filter: Callable[[str, str], str] | None = None,
    ) -> None:
        self._table = table
        self._repository = repository
        self._filter = filter
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

            change = self._change_from_payload(
                number,
                payload,
                parents[0],
                filter=self._filter,
            )
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
        filter: Callable[[str, str], str] | None = None,
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
            raise FileNotFoundError(f"Parquet not found: gs://{bucket}/{blob_path}")

        data = blob.download_as_bytes()
        table = pq.read_table(pa.BufferReader(data))

        return PlatformCache(table, repository, filter=filter)

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
    def _change_from_payload(
        number: int,
        payload: dict,
        base: str,
        *,
        filter: Callable[[str, str], str] | None = None,
    ) -> Change:
        merge_sha = payload.get("merge_commit_sha")
        if not merge_sha:
            raise ValueError(
                f"Pull request {number} merge_commit_sha is missing or empty"
            )

        title = payload.get("title") or ""
        body = payload.get("body") or ""
        description = f"{title}\n\n{body}".strip()
        if filter is not None:
            description = filter("description", description)

        return Change(
            number=number,
            description=description,
            base=base,
            after=merge_sha,
        )
