import json
import sqlite3
import zlib
from pathlib import Path


class MirrorStore:
    """Read-only queries over the ``mirror.sqlite`` written by ``MirrorArchive``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _rows(self, query: str, parameters: tuple = ()) -> list[dict]:
        connection = self._connect()

        try:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]
        finally:
            connection.close()

    def meta(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self._rows("SELECT key, value FROM meta")}

    def repositories(self) -> list[dict]:
        return self._rows("SELECT * FROM repository ORDER BY lower(name)")

    def repository(self, name: str) -> dict | None:
        rows = self._rows("SELECT * FROM repository WHERE lower(name) = lower(?)", (name,))
        return rows[0] if rows else None

    def entry(self, repo: str, number: int) -> dict | None:
        rows = self._rows("SELECT * FROM entry WHERE repo = ? AND number = ?", (repo, number))
        return rows[0] if rows else None

    def entries(
        self,
        repo: str,
        kind: str,
        state: str = "",
        query: str = "",
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict], int]:
        """Return one page of pull requests or issues, newest first, and the total match count.

        The state ``closed`` includes merged pull requests, as on GitHub.
        """
        where, parameters = self._filter(repo, kind, state, query)
        total = self._rows(f"SELECT COUNT(*) AS count FROM entry WHERE {where}", tuple(parameters))[0]["count"]
        rows = self._rows(
            f"SELECT * FROM entry WHERE {where} ORDER BY number DESC LIMIT ? OFFSET ?",
            tuple(parameters + [size, (page - 1) * size]),
        )
        return rows, total

    def counts(self, repo: str, kind: str, query: str = "") -> dict[str, int]:
        """Return how many pull requests or issues matching ``query`` are open, closed and merged; merged ones also count as closed."""
        where, parameters = self._filter(repo, kind, "", query)
        result = {"open": 0, "closed": 0, "merged": 0}

        for row in self._rows(f"SELECT state, COUNT(*) AS count FROM entry WHERE {where} GROUP BY state", tuple(parameters)):
            result[row["state"]] = row["count"]

        result["closed"] += result["merged"]
        return result

    def _filter(self, repo: str, kind: str, state: str, query: str) -> tuple[str, list]:
        clauses = ["repo = ?", "kind = ?"]
        parameters: list = [repo, kind]

        if state == "closed":
            clauses.append("state IN ('closed', 'merged')")
        elif state:
            clauses.append("state = ?")
            parameters.append(state)

        if query.lstrip("#").isdigit():
            clauses.append("number = ?")
            parameters.append(int(query.lstrip("#")))
        elif query:
            clauses.append("(title LIKE ? OR author LIKE ?)")
            parameters.extend([f"%{query}%", f"%{query}%"])

        return " AND ".join(clauses), parameters

    def record(self, repo: str, kind: str, number: int):
        """Return the decoded raw payload of one collected record, or None when it was not collected."""
        rows = self._rows(
            "SELECT payload FROM record WHERE repo = ? AND kind = ? AND number = ?",
            (repo, kind, number),
        )

        if not rows:
            return None

        return json.loads(zlib.decompress(rows[0]["payload"]))

    def pulls(self, repo: str, sha: str) -> list[dict]:
        """Return the pull requests that contain ``sha`` as a commit or as their merge commit."""
        return self._rows(
            "SELECT e.* FROM membership m JOIN entry e ON e.repo = m.repo AND e.number = m.number "
            "WHERE m.repo = ? AND m.sha = ? ORDER BY e.number",
            (repo, sha),
        )

    def edges(self, repo: str, number: int) -> list[dict]:
        """Return the reference edges from pull request ``number`` and the ones pointing at it."""
        return self._rows(
            "SELECT * FROM edge WHERE (repo = ? AND number = ?) OR (target = ? AND related = ?) "
            "ORDER BY creation",
            (repo, number, repo, number),
        )
