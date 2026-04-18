from __future__ import annotations

import re
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import pygit2

from benchmark.attribution.domain import Change
from benchmark.attribution.evaluation import KnowledgeGraphMetrics, Metrics
from benchmark.attribution.graph import AttributionGraphBuilder, AttributionResult
from benchmark.attribution.history import (
    PlatformCache,
    VersionControl,
    VersionControlSnapshot,
)
from benchmark.attribution.tracing import Trace


@dataclass(frozen=True)
class ValidationCase:
    repository: str
    correction: str
    defects: tuple[str, ...] = ()
    partition: str | None = None


@dataclass(frozen=True)
class ValidationRun:
    error: str | None
    trace: Trace | None
    output: AttributionResult | None


class ValidationRedaction:
    """Base redaction policy for validation inputs.

    Subclasses can override ``message`` to sanitize commit messages while
    leaving all other metadata kinds untouched by default.
    """

    def __call__(
        self,
        kind: str,
        value: str,
        identifier: str | None = None,
    ) -> str:
        return self.redact(kind, value, identifier)

    def redact(
        self,
        kind: str,
        value: str,
        identifier: str | None = None,
    ) -> str:
        if kind != "message":
            return value
        return self.message(value, identifier)

    def message(
        self,
        value: str,
        identifier: str | None = None,
    ) -> str:
        return value


class GitHubRedaction(ValidationRedaction):
    """Case-aware validation redaction for GitHub-style commit metadata.

    Applies strict reference filtering only to the configured correction
    commit message while leaving other commit messages unchanged.
    """

    def __init__(
        self,
        correction: str | None = None,
        defects: Sequence[str] = (),
    ) -> None:
        self._correction = correction
        self._defects = tuple(
            value.lower()
            for value in defects
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", value)
        )

    def message(
        self,
        value: str,
        identifier: str | None = None,
    ) -> str:
        if identifier != self._correction:
            return value
        current = self._drop_fields(value)
        current = self._drop_inline(current)
        current = self._drop_number(current)
        return self._normalize(current)

    def _drop_fields(
        self,
        value: str,
    ) -> str:
        result: list[str] = []

        for line in value.splitlines():
            if self._field(line):
                continue
            result.append(line.rstrip())

        return "\n".join(result)

    def _field(
        self,
        line: str,
    ) -> bool:
        return re.match(
            r"^\s*(Fixes|Closes|Resolves|Link):\s+.*$",
            line,
            flags=re.IGNORECASE,
        ) is not None

    def _drop_inline(
        self,
        value: str,
    ) -> str:
        current = re.sub(
            r"https?://github\.com/[^/\s]+/[^/\s]+/commit/([0-9a-fA-F]{7,40})\b",
            self._commit_url,
            value,
        )
        current = re.sub(
            r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+/commits/([0-9a-fA-F]{7,40})\b",
            self._commit_url,
            current,
        )
        current = re.sub(
            r"https?://github\.com/[^/\s]+/[^/\s]+/issues/\d+\b",
            "<reference>",
            current,
        )
        current = re.sub(
            r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+\b",
            "<reference>",
            current,
        )
        current = re.sub(
            r"\b[Cc]ommit\s+([0-9a-fA-F]{7,40})(?:\s*\((?:\".*?\"|'.*?'|[^)]*?)\))?",
            self._commit_ref,
            current,
        )
        current = re.sub(
            r"(?<![0-9a-fA-F])([0-9a-fA-F]{7,40})(?![0-9a-fA-F])",
            self._sha_ref,
            current,
        )
        return current

    def _commit_url(
        self,
        match: re.Match[str],
    ) -> str:
        return "<reference>" if self._defect(match.group(1)) else match.group(0)

    def _commit_ref(
        self,
        match: re.Match[str],
    ) -> str:
        return "<commit>" if self._defect(match.group(1)) else match.group(0)

    def _sha_ref(
        self,
        match: re.Match[str],
    ) -> str:
        token = match.group(1).lower()
        return "<commit>" if self._defect(token) else match.group(0)

    def _defect(
        self,
        value: str,
    ) -> bool:
        token = value.lower()
        return any(
            defect == token or defect.startswith(token)
            for defect in self._defects
        )

    def _drop_number(
        self,
        value: str,
    ) -> str:
        return re.sub(r"(?<!\w)#\d+\b", "<reference>", value)

    def _normalize(
        self,
        value: str,
    ) -> str:
        lines = [line.rstrip() for line in value.splitlines()]
        current = "\n".join(lines).strip()
        return re.sub(r"\n{3,}", "\n\n", current)


class ValidationSetup:
    """Prepares everything a validation run needs from a single case:
    extracts archives, loads a snapshot, builds metrics, and constructs the
    redaction filter. Subclasses extend ``_assemble`` to add
    candidate-specific state such as a live git repository."""

    def __init__(
        self,
        snapshot: VersionControlSnapshot,
        source: Path,
        metrics: Metrics,
        correction: str,
        redaction: ValidationRedaction,
    ) -> None:
        self.snapshot = snapshot
        self.source = source
        self.metrics = metrics
        self.correction = correction
        self.redaction = redaction

    @classmethod
    def build(
        cls,
        case: ValidationCase,
        *,
        context: Path,
        sources: Path | None = None,
        redaction: ValidationRedaction | None = None,
    ) -> ValidationSetup:
        if redaction is None:
            redaction = GitHubRedaction(
                correction=case.correction,
                defects=case.defects,
            )

        root = cls._extract(context) / case.repository.lower()
        if not root.exists():
            with open(next(root.parent.glob("*/*/meta.json")), "r", encoding="utf-8") as file:
                root = Path(file.name).parent

        snapshot = VersionControlSnapshot.load(
            root / "snapshot", cache=root / ".git",
        )
        metrics = KnowledgeGraphMetrics.build(
            snapshot, root / "context", case.correction,
        )
        correction = redaction.redact(
            "message",
            snapshot.meta(case.correction).message,
            case.correction,
        )

        return cls._assemble(
            case=case,
            snapshot=snapshot,
            source=root / "context",
            metrics=metrics,
            correction=correction,
            redaction=redaction,
            sources=sources,
        )

    @classmethod
    def _assemble(
        cls,
        *,
        case: ValidationCase,
        snapshot: VersionControlSnapshot,
        source: Path,
        metrics: Metrics,
        correction: str,
        redaction: ValidationRedaction,
        sources: Path | None,
    ) -> ValidationSetup:
        return cls(
            snapshot=snapshot,
            source=source,
            metrics=metrics,
            correction=correction,
            redaction=redaction,
        )

    @staticmethod
    def _extract(archive: Path) -> Path:
        work = Path(f"/tmp/tmp{zlib.crc32(str(archive).encode()):x}")
        if not work.exists():
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(work, filter="data")
        return work


class RepositorySetup(ValidationSetup):
    """Extends the base setup with a live pygit2-backed repository loaded
    from the sources archive, suitable for candidates that explore the full
    git history directly."""

    def __init__(
        self,
        snapshot: VersionControlSnapshot,
        source: Path,
        metrics: Metrics,
        correction: str,
        redaction: ValidationRedaction,
        repository: VersionControl,
    ) -> None:
        super().__init__(
            snapshot=snapshot, source=source, metrics=metrics,
            correction=correction, redaction=redaction,
        )
        self.repository = repository

    @classmethod
    def _assemble(
        cls,
        *,
        case: ValidationCase,
        snapshot: VersionControlSnapshot,
        source: Path,
        metrics: Metrics,
        correction: str,
        redaction: ValidationRedaction,
        sources: Path | None,
    ) -> RepositorySetup:
        if sources is None:
            raise ValueError("RepositorySetup requires a sources archive")
        sources_root = cls._extract(sources)
        repository = VersionControl(
            pygit2.Repository(str(sources_root / "repository.git")),
            filter=redaction,
        )
        return cls(
            snapshot=snapshot,
            source=source,
            metrics=metrics,
            correction=correction,
            redaction=redaction,
            repository=repository,
        )


class KnowledgeGraphSetup(ValidationSetup):
    """Base setup is sufficient for knowledge-graph-backed candidates that
    operate on the snapshot rather than a live repository."""

    @classmethod
    def _assemble(
        cls,
        *,
        case: ValidationCase,
        snapshot: VersionControlSnapshot,
        source: Path,
        metrics: Metrics,
        correction: str,
        redaction: ValidationRedaction,
        sources: Path | None,
    ) -> KnowledgeGraphSetup:
        snapshot = VersionControlSnapshot.load(
            source.parent / "snapshot",
            cache=source.parent / ".git",
            filter=redaction,
        )
        return cls(
            snapshot=snapshot,
            source=source,
            metrics=metrics,
            correction=correction,
            redaction=redaction,
        )


class ValidationExecutor:
    """Execute one attribution candidate over multiple validation cases."""

    def __init__(
        self,
        candidate: AttributionGraphBuilder,
        cache: PlatformCache,
        repository: Callable[[ValidationCase], VersionControl],
        hook: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._candidate = candidate
        self._cache = cache
        self._repository = repository
        self._hook = hook

    def run(self, cases: Sequence[ValidationCase]) -> list[ValidationRun]:
        result: list[ValidationRun] = []

        for case in cases:
            trace: Trace | None = None
            hook = None if self._hook is None else self._wrap(case, lambda: trace)
            trace = Trace(metadata={"repository": case.repository}, hook=hook)
            try:
                repository = self._repository(case)
                change = self._change(repository, case)
                graph = self._candidate.create(repository)
                output = graph.invoke(change, trace=trace)
            except Exception as error:
                result.append(
                    ValidationRun(
                        error=str(error),
                        trace=trace,
                        output=None,
                    )
                )
                continue

            result.append(
                ValidationRun(
                    error=None,
                    trace=trace,
                    output=output,
                )
            )

        return result

    def _wrap(
        self,
        case: ValidationCase,
        trace: Callable[[], Trace | None],
    ) -> Callable[[dict[str, object]], None]:
        hook = self._hook
        if hook is None:
            raise ValueError("ValidationExecutor hook wrapper requires a hook.")

        def current(event: dict[str, object]) -> None:
            value = trace()
            hook(
                {
                    "repository": case.repository,
                    "correction": case.correction,
                    "partition": case.partition,
                    "trace": None if value is None else value.id,
                    "event": event,
                }
            )

        return current

    def _change(
        self,
        repository: VersionControl,
        case: ValidationCase,
    ) -> Change:
        try:
            return self._cache.resolve(case.correction)
        except KeyError:
            pass

        parents = repository.parents(case.correction)
        base = case.correction if not parents else parents[0]

        try:
            description = repository.meta(case.correction).message
        except Exception:
            description = ""

        return Change(
            number=0,
            description=description,
            base=base,
            after=case.correction,
        )
