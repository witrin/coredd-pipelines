from __future__ import annotations

from textwrap import dedent
from typing import TypedDict

from pydantic import BaseModel

from benchmark.attribution.diff.reference import PatchFactory, PatchView
from benchmark.attribution.domain import Anchor, Change, Slice
from benchmark.attribution.reason import HintItemState


class LocateLocationState(TypedDict, total=False):
    """One resolved defect location in the defect revision."""

    file: str
    line: int
    excerpt: str
    reason: str


class LocateInput(TypedDict, total=False):
    correction: Change
    rationale: str
    causes: list[HintItemState]
    expansion: str
    defect: Change


class LocateOutput(TypedDict):
    defect: Change
    locations: list[LocateLocationState]


class LocateItemSchema(BaseModel):
    line: int
    excerpt: str
    reason: str


class LocateResponseSchema(BaseModel):
    locations: list[LocateItemSchema]


class LocateNode:
    """Locate the defect statements within the known inducing commit."""

    def __init__(
        self,
        factory: PatchFactory | None = None,
        llm: object | None = None,
    ) -> None:
        self._factory = factory
        self._llm = llm

    def run(self, input: LocateInput) -> LocateOutput:
        """Locate the defect statements within the known inducing commit."""
        correction = input.get("correction")
        rationale = input.get("rationale")
        causes = input.get("causes")
        expansion = input.get("expansion")
        defect = input.get("defect")
        if self._llm is None or self._factory is None:
            raise ValueError(
                "LocateNode requires correction, rationale, causes, expansion, "
                "defect, factory, and llm."
            )
        if (
            correction is None
            or rationale is None
            or causes is None
            or expansion is None
            or defect is None
        ):
            raise ValueError(
                "LocateNode requires correction, rationale, causes, expansion, and defect."
            )
        if not rationale.strip() or not causes or not expansion.strip():
            raise ValueError(
                "LocateNode requires non-empty rationale, causes, and expansion."
            )

        source = expansion if expansion.endswith("\n") else f"{expansion}\n"
        try:
            renderer = PatchView(source)
        except Exception as error:
            raise ValueError(
                f"LocateNode requires parseable expanded patch content: {source!r}"
            ) from error

        references = [
            current["reference"]
            for item in causes
            for current in list(item.get("statements") or [])
        ]
        slices = self._slices(renderer.resolve(references), correction, defect)
        if not slices:
            return {"defect": defect, "locations": []}

        refinement = str(
            self._factory.refine(
                defect["base"],
                defect["after"],
                slices,
                "after",
                force=False,
            )
        )
        try:
            snippet = PatchView(refinement, numbered=True).after()
        except Exception as error:
            raise ValueError(
                f"LocateNode requires parseable unified patch content: {refinement!r}"
            ) from error
        if not snippet.strip():
            return {"defect": defect, "locations": []}

        hints = self._hint(causes)
        try:
            model = self._llm.with_structured_output(
                LocateResponseSchema,
                method="json_schema",
                strict=True,
            )
        except (AttributeError, TypeError) as error:
            raise ValueError(
                "LocateNode requires an llm that supports structured schema mode."
            ) from error
        payload = LocateResponseSchema.model_validate(
            model.invoke(
                self._locate_prompt(
                    rationale=rationale.strip(),
                    hints=hints,
                    snippet=snippet,
                )
            )
        )
        index = self._index(snippet)
        locations: list[LocateLocationState] = []
        for item in payload.locations:
            anchor = index.get(item.line)
            if anchor is None:
                continue
            locations.append(
                {
                    "file": anchor[0],
                    "line": item.line,
                    "excerpt": anchor[1],
                    "reason": item.reason,
                }
            )
        return {"defect": defect, "locations": locations}

    def _slices(
        self,
        slices: list[Slice],
        correction: Change,
        defect: Change,
    ) -> list[Slice]:
        commit = defect["after"]
        grouped: dict[tuple[str | None, str | None], list[Anchor]] = {}
        for current in slices:
            path = current.file[0]
            entries = [
                (anchor.base[0], anchor.base[1] - 1)
                for anchor in current.anchors
                if anchor.base != (0, 0)
            ]
            if path is None or not entries:
                continue
            blamed = self._factory.repository.blame(path, correction["base"], entries)
            for blame, _, origin in blamed:
                if blame != commit or origin is None:
                    continue
                file = (
                    (path if self._exists(defect["base"], path) else None),
                    path,
                )
                grouped.setdefault(file, []).append(
                    Anchor(
                        base=(0, 0),
                        after=(origin[0], origin[1] + 1),
                    )
                )
        return [Slice(file=file, anchors=anchors) for file, anchors in grouped.items()]

    def _exists(self, commit: str | None, path: str) -> bool:
        if commit is None:
            return False
        try:
            self._factory.repository.blob(commit, path)
        except FileNotFoundError:
            return False
        return True

    def _index(self, snippet: str) -> dict[int, tuple[str, str]]:
        result: dict[int, tuple[str, str]] = {}
        path = ""
        for line in snippet.splitlines():
            if line.startswith("File: "):
                path = line[len("File: ") :]
                continue
            head, _, text = line.partition(" | ")
            try:
                number = int(head)
            except ValueError:
                continue
            result[number] = (path, text)
        return result

    def _hint(self, causes: list[object]) -> str:
        lines: list[str] = []
        seen: set[tuple[str, str]] = set()
        for item in causes:
            statements: list[str] = []
            for current in list(item.get("statements") or []):
                scope = current["scope"]
                excerpt = current["excerpt"]
                key = (scope, excerpt)
                if key in seen:
                    continue
                seen.add(key)
                statements.append(f"- {scope} {excerpt}")
            lines.append("\n".join(statements))
        return "\n".join(lines)

    def _locate_prompt(
        self,
        *,
        rationale: str,
        hints: str,
        snippet: str,
    ) -> str:
        template = dedent(
            """
            Identify the exact lines in the provided code snippet where the defect sits.
            Each line is prefixed with its line number and a pipe separator.
            Return only line numbers that appear in the snippet and that carry the defect described by the root cause and defect statements.
            Be conservative. If a line is unrelated to the defect, do not return it.
            Your line numbers, excerpts, and reasons must agree.

            Respond only in JSON.
            Use this schema: {{"locations": [{{"line": integer, "excerpt": string, "reason": string}}]}}.

            Root cause:
            {rationale}

            Defect statements:
            {hints}

            Code snippet:
            {snippet}
            """
        ).strip()
        return template.format(
            rationale=rationale,
            hints=hints,
            snippet=snippet,
        )
