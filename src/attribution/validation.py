from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from benchmark.attribution.domain import Change
from benchmark.attribution.graph import AttributionGraphBuilder, AttributionResult
from benchmark.attribution.history import PlatformCache, VersionControl
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
