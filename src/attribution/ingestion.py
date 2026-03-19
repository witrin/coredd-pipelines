import os
import tarfile
import tempfile
import pygit2
from io import BytesIO
from typing import Any, Callable, Optional

from components.io import BlobStream


class FileResponseAdapter:
    def __init__(self, file):
        self._file = file

    def iter_content(self, chunk_size=8192):
        while True:
            chunk = self._file.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self):
        self._file.close()


class ValidationExtractor:
    """Extracts repository snapshot for validation (SZZ replication)."""

    def __init__(
        self,
        client: Any,
        repository: str,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.repository = repository
        self._log = log or (lambda msg: None)

    def extract(self) -> BlobStream:
        default = self.client.get_repo(self.repository).default_branch
        token = self.client._Github__requester._Requester__auth.token

        with tempfile.TemporaryDirectory() as temporary:
            directory = os.path.join(temporary, "repository.git")

            callbacks = pygit2.RemoteCallbacks(
                credentials=pygit2.UserPass(
                    "x-access-token",
                    token,
                )
            )

            self._log(f"Cloning {self.repository} (bare, branch={default})")

            pygit2.clone_repository(
                f"https://github.com/{self.repository}.git",
                directory,
                bare=True,
                checkout_branch=default,
                callbacks=callbacks,
            )

            repository = pygit2.Repository(directory)
            commit = repository.revparse_single(default)

            archive = os.path.join(temporary, f"{commit.id}.tar.gz")

            with tarfile.open(archive, "w:gz") as tar:
                tar.add(directory, arcname=os.path.basename(directory))

            file = open(archive, "rb")
            size = os.path.getsize(archive)

        return BlobStream(
            content_type="application/gzip",
            filename=f"{commit.id}.tar.gz",
            response=FileResponseAdapter(file),
            size=size,
        )
