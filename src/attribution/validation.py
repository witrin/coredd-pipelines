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
    partition: str | None = None


@dataclass(frozen=True)
class ValidationRun:
    error: str | None
    trace: Trace | None
    output: AttributionResult | None


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
        redact: Callable[[str, str], str],
    ) -> None:
        self.snapshot = snapshot
        self.source = source
        self.metrics = metrics
        self.correction = correction
        self.redact = redact

    @classmethod
    def build(
        cls,
        case: ValidationCase,
        *,
        defect: str,
        context: Path,
        sources: Path | None = None,
    ) -> ValidationSetup:
        context_root = cls._extract(context)
        root = context_root / case.repository.lower()
        if not root.exists():
            with open(next(context_root.glob("*/*/meta.json")), "r", encoding="utf-8") as file:
                root = Path(file.name).parent

        snapshot = VersionControlSnapshot.load(
            root / "snapshot", cache=root / ".git",
        )
        metrics = KnowledgeGraphMetrics.build(
            snapshot, root / "context", case.correction,
        )
        correction_message = snapshot.meta(case.correction).message
        redact = cls._redact(defect)

        return cls._assemble(
            case=case,
            snapshot=snapshot,
            source=root / "context",
            metrics=metrics,
            correction=correction_message,
            redact=redact,
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
        redact: Callable[[str, str], str],
        sources: Path | None,
    ) -> ValidationSetup:
        return cls(
            snapshot=snapshot,
            source=source,
            metrics=metrics,
            correction=correction,
            redact=redact,
        )

    @staticmethod
    def _extract(archive: Path) -> Path:
        work = Path(f"/tmp/tmp{zlib.crc32(str(archive).encode()):x}")
        if not work.exists():
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(work, filter="data")
        return work

    @staticmethod
    def _redact(defect: str) -> Callable[[str, str], str]:
        pattern = rf"(?<![0-9a-f])(?:{'|'.join(re.escape(defect[:size]) for size in range(len(defect), 4, -1))})(?![0-9a-f])"

        def apply(kind: str, value: str) -> str:
            if kind != "message":
                return value
            return re.sub(
                r"https?://github\.com/\S+", "<reference>",
                re.sub(pattern, "<commit>", value, flags=re.IGNORECASE),
            )

        return apply


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
        redact: Callable[[str, str], str],
        repository: VersionControl,
    ) -> None:
        super().__init__(
            snapshot=snapshot, source=source, metrics=metrics,
            correction=correction, redact=redact,
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
        redact: Callable[[str, str], str],
        sources: Path | None,
    ) -> RepositorySetup:
        if sources is None:
            raise ValueError("RepositorySetup requires a sources archive")
        sources_root = cls._extract(sources)
        repository = VersionControl(
            pygit2.Repository(str(sources_root / "repository.git")),
            filter=redact,
        )
        return cls(
            snapshot=snapshot,
            source=source,
            metrics=metrics,
            correction=correction,
            redact=redact,
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
        redact: Callable[[str, str], str],
        sources: Path | None,
    ) -> KnowledgeGraphSetup:
        snapshot_with_redact = VersionControlSnapshot.load(
            source.parent / "snapshot",
            cache=source.parent / ".git",
            filter=redact,
        )
        return cls(
            snapshot=snapshot_with_redact,
            source=source,
            metrics=metrics,
            correction=correction,
            redact=redact,
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
