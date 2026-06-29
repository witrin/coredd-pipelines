import re

import pyarrow as pa
import polars as pl

from markdown_it import MarkdownIt

from benchmark.attribution.history import VersionControl


class MarkdownText:
    """Render markdown to plain text.

    Used to normalize commit messages and pull request descriptions before they
    reach the model: markup and link URLs carry no signal but cost tokens, and a
    normalized form lets the enricher detect when a commit message already
    contains the pull request description.
    """

    def __init__(self) -> None:
        # Emphasis is disabled so Python dunder identifiers survive: a default
        # parser reads ``__init__`` as bold markup and emits ``init``.
        self._parser = MarkdownIt("commonmark").disable(["emphasis"])
        self._whitespace = re.compile(r"\s+")
        self._sentence = re.compile(r"(?<=[.!?])\s+")

    def to_text(self, source: str) -> str:
        lines: list[str] = []
        for token in self._parser.parse(source or ""):
            if token.type == "inline":
                buffer: list[str] = []
                for child in token.children or []:
                    if child.type in ("text", "code_inline"):
                        buffer.append(child.content)
                    elif child.type in ("softbreak", "hardbreak"):
                        buffer.append(" ")
                if buffer:
                    lines.append("".join(buffer))
            elif token.type in ("fence", "code_block"):
                lines.append(token.content)
        return "\n".join(lines).strip()

    def units(self, text: str) -> list[str]:
        out: list[str] = []
        for line in text.splitlines():
            for unit in self._sentence.split(line):
                unit = unit.strip()
                if unit:
                    out.append(unit)
        return out

    def normalize(self, text: str) -> str:
        return self._whitespace.sub(" ", text).strip().lower()

    def contains(self, haystack: str, candidate: str) -> bool:
        # The candidate counts as present when nearly all of its units already
        # appear in the haystack: GitHub re-wraps and re-numbers the body it
        # copies into a squash message, so a small residue is expected.
        reference = self.normalize(haystack)
        if not reference:
            return False
        units = self.units(candidate)
        if not units:
            return False
        missing = [u for u in units if self.normalize(u) not in reference]
        return len(missing) <= int(0.15 * len(units))

    def difference(self, haystack: str, candidate: str) -> list[str]:
        reference = self.normalize(haystack)
        return [u for u in self.units(candidate) if self.normalize(u) not in reference]

    def dedupe(self, text: str) -> str:
        # Drop any line whose normalized form is already contained in a line kept
        # earlier. GitHub squashes repeat the title as a branch-commit line below
        # the subject, so without this the heading would appear twice.
        kept: list[str] = []
        seen: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            norm = self.normalize(stripped)
            if any(norm in other for other in seen):
                continue
            seen.append(norm)
            kept.append(line)
        return "\n".join(kept)


class PullRequestEnricher:
    """Build the per-repository change frame and enrich commit messages with PR text."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._frame: pl.DataFrame | None = None
        self._text: dict[str, dict[str, str]] = {}
        self._markdown = MarkdownText()
        # Git process trailers (authorship/review metadata), matched anywhere in
        # the message since squashed multi-commit messages repeat them per commit.
        # Issue trailers (Fixes/Closes/Resolves/Refs) are deliberately kept.
        self._trailer = re.compile(
            r"(?i)^\s*(?:signed-off-by|co-authored-by|reviewed-by|acked-by"
            r"|tested-by|reported-by|cc):\s"
        )
        # A prose commit reference (e.g. "This reverts commit <sha>"): a real
        # identifier worth keeping when the squash body would otherwise replace it.
        self._commit_ref = re.compile(r"(?i)\bcommit\s+[0-9a-f]{7,40}\b")

    def attach(
        self,
        repository: VersionControl,
        reference: pl.DataFrame,
    ) -> "PullRequestEnricher":
        # The change reference carries, per PR, the raw payload, the cleaned
        # title/body, and the list of every SHA that identifies the PR (branch
        # commits and the trunk merge/squash commit). Both the PR frame consumed by
        # PlatformCache and the commit -> cleaned-text map consumed by enrich() are
        # derived from it, so no raw change stream needs re-bundling here.
        scoped = reference.filter(pl.col("name") == self._name)

        self._frame = (
            scoped.with_columns(
                pl.col("payload").str.json_path_match("$.merge_commit_sha").alias("sha"),
                pl.lit("pr").alias("type"),
            )
            .filter(pl.col("sha").is_not_null())
            .filter(
                pl.col("sha").map_elements(
                    lambda sha: self._reachable(repository, sha),
                    return_dtype=pl.Boolean,
                )
            )
            .select("name", "type", "number", "payload", "sha")
        )
        self._bind_text(scoped)
        return self

    def _bind_text(self, scoped: pl.DataFrame) -> None:
        # Explode the per-PR commit lists so every SHA of a PR — not only its merge
        # commit — maps to the same text the filter classified on. Two variants are
        # kept per SHA: the cleaned title+body (what gets shown to the model) and
        # the raw payload title+body (what the squash commit message is compared
        # against to detect that the description is already present).
        rows = (
            scoped.with_columns(
                (pl.col("title") + "\n\n" + pl.col("body")).str.strip_chars().alias("clean"),
                (
                    pl.col("payload").str.json_path_match("$.title").fill_null("")
                    + "\n\n"
                    + pl.col("payload").str.json_path_match("$.body").fill_null("")
                )
                .str.strip_chars()
                .alias("raw"),
            )
            .explode("commits")
            .select(pl.col("commits").alias("sha"), "clean", "raw")
        )
        self._text = {
            row["sha"]: {"clean": row["clean"], "raw": row["raw"]}
            for row in rows.iter_rows(named=True)
            if row["sha"] and row["clean"]
        }

    def _reachable(self, repository: VersionControl, sha: str) -> bool:
        try:
            repository.meta(sha)
        except Exception:
            return False
        return True

    def table(self) -> pa.Table:
        return self._frame.to_arrow()

    def _strip_trailers(self, message: str) -> str:
        return "\n".join(
            line
            for line in (message or "").splitlines()
            if not self._trailer.match(line)
        )

    def _compose(self, subject: str, description: str, tail: list[str]) -> str:
        # Layout: the original subject (with its "(#number)" id) always first, a
        # blank line, then the cleaned pull request description, then any remaining
        # commit lines (e.g. SHA references). Empty sections are skipped, and
        # lines already covered earlier are dropped so a heading never repeats.
        blocks = [subject, description, "\n".join(tail)]
        return self._markdown.dedupe("\n\n".join(b for b in blocks if b.strip()))

    def enrich(self, kind: str, value: str, sha: str | None) -> str:
        if kind != "message" or sha is None:
            return value
        entry = self._text.get(sha)
        if entry is None:
            return value
        md = self._markdown
        git_text = md.to_text(self._strip_trailers(value))
        clean_pr = md.to_text(entry["clean"])
        raw_pr = md.to_text(self._strip_trailers(entry["raw"]))
        lines = git_text.splitlines()
        subject = lines[0] if lines else ""
        # When the commit message already carries the (raw) pull request body —
        # the common GitHub squash case — replace its body with the cleaned one so
        # the description appears once, markdown-free. The subject and any commit
        # references survive (PR id and SHA links).
        if md.contains(git_text, raw_pr):
            refs = [line for line in lines[1:] if self._commit_ref.search(line)]
            description = "\n".join(md.difference(subject, clean_pr))
            return self._compose(subject, description, refs)
        # Otherwise the commit message is independent (true merge, commit list).
        # Keep its body as plain text below the description, appending only the
        # cleaned units it lacks.
        body = "\n".join(lines[1:])
        extra = "\n".join(md.difference(git_text, clean_pr))
        description = "\n\n".join(b for b in (extra, body) if b.strip())
        return self._compose(subject, description, [])
