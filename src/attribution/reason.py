from __future__ import annotations
from textwrap import dedent
from typing import TypedDict

from pydantic import BaseModel, ValidationError

from benchmark.attribution.domain import Anchor, Change, FileOperation, OperationType, Slice
from benchmark.attribution.diff.context import PatchScope
from benchmark.attribution.diff.reference import PatchFactory, PatchView


class ExplainRunState(TypedDict, total=False):
    """One explain pass result."""

    index: int
    rationale: str
    files: list[str]


class AggregateState(TypedDict, total=False):
    """Aggregated explain result."""

    rationale: str
    files: list[str]


class HintItemState(TypedDict, total=False):
    """One referenced hint item."""

    reason: str
    statements: list[HintStatementState]


class HintStatementState(TypedDict, total=False):
    """One resolved hint reference."""

    reference: str
    scope: str | None
    excerpt: str | None


class AnchorState(TypedDict, total=False):
    """One serialized anchor."""

    base: tuple[int, int]
    after: tuple[int, int]


class SliceState(TypedDict, total=False):
    """One serialized slice."""

    file: tuple[str | None, str | None]
    anchors: list[AnchorState]


class ExplainResponseSchema(BaseModel):
    rationale: str
    files: list[str]


class HintItemSchema(BaseModel):
    references: list[str]
    reason: str


class HintResponseSchema(BaseModel):
    causes: list[HintItemSchema]
    corrections: list[HintItemSchema]


class VerifyResponseSchema(BaseModel):
    defect: bool
    reason: str


class CompareResponseSchema(BaseModel):
    present: bool
    reason: str


class SummarizeInput(TypedDict, total=False):
    correction: Change


class SummarizeOutput(TypedDict):
    summary: str
    files: list[str]


class ExplainInput(TypedDict, total=False):
    correction: Change
    summary: str
    runs: list[ExplainRunState]
    samples: int


class ExplainOutput(TypedDict):
    runs: list[ExplainRunState]
    samples: int


class AggregateInput(TypedDict, total=False):
    summary: str
    files: list[str]
    runs: list[ExplainRunState]


class AggregateOutput(TypedDict):
    rationale: str
    files: list[str]
    aggregate: AggregateState


class ExpandInput(TypedDict, total=False):
    correction: Change
    files: list[str]


class ExpandOutput(TypedDict, total=False):
    expansion: str


class HintInput(TypedDict, total=False):
    correction: Change
    rationale: str
    expansion: str


class HintOutput(TypedDict):
    causes: list[HintItemState]
    corrections: list[HintItemState]


class RefineInput(TypedDict, total=False):
    correction: Change
    expansion: str
    causes: list[HintItemState]


class RefineOutput(TypedDict, total=False):
    refinement: str


class VerifyOutput(TypedDict, total=False):
    base: dict[str, object]
    after: dict[str, object]
    verified: bool


class VerifyInput(TypedDict, total=False):
    rationale: str
    causes: list[HintItemState]
    refinement: str


class BlameInput(TypedDict, total=False):
    correction: Change
    expansion: str
    causes: list[HintItemState]


class BlameOutput(TypedDict):
    candidates: list[dict[str, object]]


class CompareInput(TypedDict, total=False):
    rationale: str
    causes: list[HintItemState]
    candidates: list[dict[str, object]]
    index: int


class CompareOutput(TypedDict):
    index: int
    candidate: dict[str, object]
    parent: dict[str, object]


class SelectInput(TypedDict, total=False):
    compare: dict[str, object]


class SelectOutput(TypedDict, total=False):
    introducer: str


class AbortOutput(TypedDict):
    aborted: bool


class FinalizeOutput(TypedDict):
    aborted: bool


class SummarizeNode:
    """Summarize the corrective delta."""

    def __init__(
        self,
        factory: PatchFactory | None = None,
        llm: object | None = None,
        context: int = 3,
    ) -> None:
        if context < 0:
            raise ValueError("context must be non-negative")
        self._factory = factory
        self._llm = llm
        self._context = context

    def run(self, input: SummarizeInput) -> SummarizeOutput:
        """Summarize the corrective delta."""
        correction = input.get("correction")
        if correction is None or self._factory is None or self._llm is None:
            raise ValueError("SummarizeNode requires correction, factory, and llm.")

        operations = self._factory.repository.manifest(
            correction.base,
            correction.after,
        )
        patch = self._factory.create(
            correction.base,
            correction.after,
            files=operations,
            n=self._context,
        )
        files = [
            operation.after or operation.base
            for operation in operations
            if (operation.after or operation.base) is not None
        ]
        patch = str(patch).strip()

        summary = ""
        if patch:
            prompt = self._build_prompt(patch, context=self._context)
            response = self._llm.invoke(prompt)
            summary = response.text.strip()

        return {
            "summary": summary,
            "files": files,
        }

    def _build_prompt(self, patch_text: str, *, context: int) -> str:
        if context == 0:
            context_note = (
                "Each hunk contains changed lines only and no unchanged context."
            )
        else:
            context_note = (
                f"Each hunk may include up to {context} unchanged context lines around "
                "the edits. Treat unchanged context only as orientation."
            )
        template = dedent(
            """
            Analyze the following patch.
            Summarize the code changes and the relationships between the changed hunks or files.
            Output only the summary text.

            {context_note}

            Patch:
            {patch_text}
            """
        ).strip()
        return template.format(
            context_note=context_note,
            patch_text=patch_text,
        )


class ExplainNode:
    """Derive a rationale for the corrective change."""

    def __init__(
        self,
        factory: PatchFactory | None = None,
        llm: object | None = None,
        context: int = 3,
        samples: int = 3,
    ) -> None:
        if context < 0:
            raise ValueError("context must be non-negative")
        if samples < 1:
            raise ValueError("samples must be positive")
        self._factory = factory
        self._llm = llm
        self._context = context
        self._samples = samples

    def run(self, input: ExplainInput) -> ExplainOutput:
        """Derive a rationale for the corrective change."""
        correction = input.get("correction")
        summary = input.get("summary")
        if (
            correction is None
            or summary is None
            or self._factory is None
            or self._llm is None
        ):
            raise ValueError(
                "ExplainNode requires correction, summary, factory, and llm"
            )

        runs = list(input.get("runs") or [])
        samples = input.get("samples", self._samples)
        if samples is None or samples < 1:
            raise ValueError("ExplainNode requires positive samples.")
        operations = self._factory.repository.manifest(
            correction.base,
            correction.after,
        )
        orders = self._file_orders(operations, samples)
        selected = orders[len(runs)]
        patch = str(
            self._factory.create(
                correction.base,
                correction.after,
                files=selected,
                n=self._context,
            )
        ).strip()
        commit_messages = self._commit_messages(correction)
        prompt = self._build_prompt(
            summary=summary,
            description=correction.description,
            commit_messages=commit_messages,
            patch_text=patch,
        )
        try:
            response = self._llm.with_structured_output(
                ExplainResponseSchema,
                method="json_schema",
                strict=True,
            ).invoke(prompt)
        except (AttributeError, TypeError) as error:
            raise ValueError(
                "ExplainNode requires an llm that supports structured schema mode."
            ) from error
        try:
            payload = ExplainResponseSchema.model_validate(response)
        except (ValidationError, TypeError, ValueError) as error:
            raise ValueError(
                "ExplainNode requires structured explain output."
            ) from error
        rationale, files = payload.rationale, list(payload.files)
        run: ExplainRunState = {
            "index": len(runs),
            "rationale": rationale,
            "files": files,
        }
        next_runs = [*runs, run]
        return {
            "samples": samples,
            "runs": next_runs,
        }

    def _file_orders(
        self,
        operations: list[object],
        requested_runs: int,
    ) -> list[list[object]]:
        canonical = list(operations)
        orders: list[list[object]] = []
        seen: set[tuple[str, ...]] = set()
        for current in (canonical, list(reversed(canonical))):
            key = tuple(
                str(
                    getattr(operation, "after", None)
                    or getattr(operation, "base", None)
                    or ""
                )
                for operation in current
            )
            if key in seen:
                continue
            seen.add(key)
            orders.append(current)
        if len(orders) >= requested_runs:
            return orders
        size = len(canonical)
        offsets: list[int] = []
        if size > 1:
            for offset in ((size + 1) // 2, 1, size // 2, 2):
                if 0 < offset < size and offset not in offsets:
                    offsets.append(offset)
            left = 3
            right = size - 1
            while left <= right:
                if 0 < right < size and right not in offsets:
                    offsets.append(right)
                if 0 < left < size and left not in offsets:
                    offsets.append(left)
                left += 1
                right -= 1
        for offset in offsets:
            if len(orders) >= requested_runs:
                break
            rotated = canonical[offset:] + canonical[:offset]
            key = tuple(
                str(
                    getattr(operation, "after", None)
                    or getattr(operation, "base", None)
                    or ""
                )
                for operation in rotated
            )
            if key in seen:
                continue
            seen.add(key)
            orders.append(rotated)
        return orders

    def _commit_messages(self, correction: Change) -> list[str]:
        shas = [correction.after]
        shas.extend(
            self._factory.repository.log(
                correction.base,
                correction.after,
                merge=True,
            )
        )
        seen: set[str] = set()
        messages: list[str] = []
        for sha in shas:
            if sha in seen:
                continue
            seen.add(sha)
            message = self._factory.repository.meta(sha).message.strip()
            if message:
                messages.append(message)
        return messages

    def _build_prompt(
        self,
        *,
        summary: str,
        description: str,
        commit_messages: list[str],
        patch_text: str,
    ) -> str:
        messages = "\n".join(commit_messages) if commit_messages else "(none)"
        template = dedent(
            """
            Use the modification summary, the pull request description, the commit messages, and the patch to identify the likely root cause and the related files among the changed files.
            Respond only in JSON.
            Use this schema: {{"rationale": string, "files": [string, ...]}}.

            Summary:
            {summary}

            Pull request description:
            ```markdown
            {description}
            ```

            Commit messages in (base, after]:
            {messages}

            Patch:
            {patch_text}

            Return only files that are relevant to the root cause and appear in the provided patch.
            """
        ).strip()
        return template.format(
            summary=summary,
            description=description or "(none)",
            messages=messages,
            patch_text=patch_text,
        )


class AggregateNode:
    """Aggregate the repeated explain runs into a first stable result."""

    def __init__(
        self,
        llm: object | None = None,
    ) -> None:
        self._llm = llm

    def run(self, input: AggregateInput) -> AggregateOutput:
        """Aggregate the repeated explain runs into a first stable result."""
        runs = list(input.get("runs") or [])
        if not runs:
            raise ValueError("AggregateNode requires explain runs.")
        if self._llm is None:
            raise ValueError("AggregateNode requires llm.")

        summary = input.get("summary")
        if summary is None:
            raise ValueError("AggregateNode requires summary.")

        files: list[str] = []
        seen: set[str] = set()
        hypotheses: list[ExplainRunState] = []
        for run in runs:
            valid_files: list[str] = []
            valid_seen: set[str] = set()
            for path in run["files"]:
                if path in valid_seen:
                    continue
                valid_seen.add(path)
                valid_files.append(path)
                if path in seen:
                    continue
                seen.add(path)
                files.append(path)
            hypotheses.append(
                {
                    "index": run["index"],
                    "rationale": run["rationale"],
                    "files": valid_files,
                }
            )

        response = self._llm.invoke(self._build_prompt(summary, files, hypotheses))
        rationale = response.text.strip()
        if not rationale:
            raise ValueError("AggregateNode requires non-empty rationale output.")
        aggregate: AggregateState = {
            "rationale": rationale,
            "files": files,
        }
        return {
            "files": files,
            "rationale": rationale,
            "aggregate": aggregate,
        }

    def _build_prompt(
        self,
        summary: str,
        files: list[str],
        runs: list[ExplainRunState],
    ) -> str:
        rendered = "\n".join(files) if files else "(none)"
        values: list[str] = []
        for run in runs:
            run_files = "\n".join(run["files"]) or "(none)"
            item = dedent(
                """
                Run {index}:
                Rationale: {rationale}
                Files:
                {files}
                """
            ).strip()
            values.append(
                item.format(
                    index=run["index"],
                    rationale=run["rationale"],
                    files=run_files,
                )
            )
        template = dedent(
            """
            Review the explanations from repeated explain passes for the same change.
            Merge them into one final root-cause rationale using only the provided runs.
            Output only the final rationale text.

            Summary:
            {summary}

            Final related files:
            {rendered}

            Explain runs:
            {runs}

            Output only the final rationale text.
            """
        ).strip()
        return template.format(
            summary=summary,
            rendered=rendered,
            runs="\n".join(values),
        )


class ExpandNode:
    """Expand semantic context around the selected changes."""

    def __init__(
        self,
        factory: PatchFactory | None = None,
        *,
        level: int = 1,
        context: int = 3,
    ) -> None:
        if level < 1:
            raise ValueError("level must be positive")
        if context < 0:
            raise ValueError("context must be non-negative")
        self._factory = factory
        self._level = level
        self._context = context

    def run(self, input: ExpandInput) -> ExpandOutput:
        """Expand semantic context around the selected changes."""
        correction = input.get("correction")
        if correction is None:
            raise ValueError("ExpandNode requires correction.")
        if self._factory is None:
            raise ValueError("ExpandNode requires factory.")
        files = input.get("files")
        if files is None:
            raise ValueError("ExpandNode requires files.")
        selected_files = [path for path in files if path]
        if not selected_files:
            raise ValueError("ExpandNode requires non-empty files.")

        selected_paths = set(selected_files)
        operations = [
            operation
            for operation in self._factory.repository.manifest(
                correction.base,
                correction.after,
            )
            if (operation.after or operation.base) in selected_paths
        ]

        expansion = self._factory.expand(
            correction.base,
            correction.after,
            files=operations,
            level=self._level,
            n=self._context,
        )
        return {
            "expansion": str(expansion),
        }


class HintNode:
    """Generate a localization hint from the expansion context."""

    def __init__(
        self,
        factory: PatchFactory | None = None,
        llm: object | None = None,
    ) -> None:
        self._factory = factory
        self._llm = llm

    def run(self, input: HintInput) -> HintOutput:
        """Generate a localization hint from the expansion context."""
        correction = input.get("correction")
        rationale = input.get("rationale")
        expansion = input.get("expansion")
        if self._llm is None:
            raise ValueError("HintNode requires rationale, expansion, and llm.")
        if rationale is None or expansion is None:
            raise ValueError("HintNode requires rationale and expansion.")
        if not rationale.strip() or not expansion.strip():
            raise ValueError("HintNode requires non-empty rationale and expansion.")
        if correction is None or self._factory is None:
            raise ValueError(
                "HintNode requires correction, rationale, expansion, factory, and llm."
            )

        try:
            renderer = PatchView(expansion)
        except Exception as error:
            raise ValueError(
                f"HintNode requires parseable expanded patch content: {expansion!r}"
            ) from error
        try:
            response = self._llm.with_structured_output(
                HintResponseSchema,
                method="json_schema",
                strict=True,
            ).invoke(
                self._build_prompt(
                    rationale=rationale.strip(),
                    expansion=renderer.render(),
                )
            )
        except (AttributeError, TypeError) as error:
            raise ValueError(
                "HintNode requires an llm that supports structured schema mode."
            ) from error
        try:
            payload = HintResponseSchema.model_validate(response)
        except (ValidationError, TypeError, ValueError) as error:
            raise ValueError("HintNode requires structured hint output.") from error
        scope = PatchScope()
        paths: dict[tuple[str | None, str | None], object] = {}
        causes = self._parse_items(
            correction,
            renderer,
            payload.causes,
            "base",
            scope,
            paths,
        )
        corrections = self._parse_items(
            correction,
            renderer,
            payload.corrections,
            "after",
            scope,
            paths,
        )
        return {
            "causes": causes,
            "corrections": corrections,
        }

    def _build_prompt(
        self,
        *,
        rationale: str,
        expansion: str,
    ) -> str:
        template = dedent(
            """
            Identify every line that is part of the defect and every line that is part of the fix.
            Put defect lines only in `causes`.
            Put fix lines only in `corrections`.
            Do not place fix lines in `causes`.
            Do not place defect lines in `corrections`.
            Use the reference labels from the left column exactly as provided.

            Respond only in JSON.
            Use this schema: {{"causes": [{{"references": [string, ...], "reason": string}}], "corrections": [{{"references": [string, ...], "reason": string}}]}}.

            Root cause rationale:
            {rationale}

            Expanded context:
            {expansion}
            """
        ).strip()
        return template.format(
            rationale=rationale,
            expansion=expansion,
        )

    def _parse_items(
        self,
        correction: Change,
        renderer: PatchView,
        value: object,
        side: str,
        scope: PatchScope,
        paths: dict[tuple[str | None, str | None], object],
    ) -> list[HintItemState]:
        parsed: list[HintItemState] = []
        for item in value:
            references = list(item.references)
            if not references:
                raise ValueError(
                    "HintNode entries must include non-empty string references."
                )
            parsed.append(
                {
                    "reason": item.reason,
                    "statements": [
                        self._resolve(
                            correction,
                            renderer,
                            reference,
                            side,
                            scope,
                            paths,
                        )
                        for reference in references
                    ],
                }
            )
        return parsed

    def _resolve(
        self,
        correction: Change,
        renderer: PatchView,
        reference: str,
        side: str,
        scope: PatchScope,
        paths: dict[tuple[str | None, str | None], object],
    ) -> HintStatementState:
        for current in renderer.resolve([reference]):
            path = self._path(correction, current.file, scope, paths)
            for anchor in current.anchors:
                base, after = path.resolve(anchor)
                statement = base if side == "base" else after
                if statement is None:
                    raise ValueError(
                        "HintNode references must resolve on the selected side."
                    )
                return {
                    "reference": reference,
                    "scope": statement.scope,
                    "excerpt": statement.excerpt,
                }
        raise ValueError("HintNode references must resolve to one statement.")

    def _path(
        self,
        correction: Change,
        file: tuple[str | None, str | None],
        scope: PatchScope,
        paths: dict[tuple[str | None, str | None], object],
    ) -> object:
        if file not in paths:
            operation = self._file(file)
            paths[file] = scope.bind(
                operation,
                self._factory.read(correction.base, operation.base),
                self._factory.read(correction.after, operation.after),
            )
        return paths[file]

    def _file(self, file: tuple[str | None, str | None]) -> FileOperation:
        if file[0] is None:
            return FileOperation(
                base=file[0], after=file[1], operation=OperationType.ADD
            )
        if file[1] is None:
            return FileOperation(
                base=file[0],
                after=file[1],
                operation=OperationType.DELETE,
            )
        return FileOperation(base=file[0], after=file[1], operation=OperationType.EDIT)


class RefineNode:
    """Reduce the expansion context to a focused view."""

    def __init__(self, factory: PatchFactory | None = None) -> None:
        self._factory = factory

    def _anchors(
        self,
        slices: list[Slice],
        anchor: str,
    ) -> list[Slice]:
        result: list[Slice] = []
        for item in slices:
            anchors = [
                current
                for current in item.anchors
                if (
                    current.base != (0, 0)
                    if anchor == "base"
                    else current.after != (0, 0)
                )
            ]
            if anchors:
                result.append(Slice(file=item.file, anchors=anchors))
        return result

    def run(self, input: RefineInput) -> RefineOutput:
        """Reduce the expansion context to a focused view."""
        correction = input.get("correction")
        expansion = input.get("expansion")
        causes = input.get("causes")
        if correction is None or self._factory is None:
            raise ValueError(
                "RefineNode requires correction, expansion, causes, and factory."
            )
        if expansion is None or causes is None:
            raise ValueError("RefineNode requires expansion and causes.")
        if not expansion.strip() or not causes:
            raise ValueError("RefineNode requires non-empty expansion and causes.")

        references = [
            current["reference"]
            for item in causes
            for current in list(item.get("statements") or [])
        ]
        try:
            slices = self._anchors(
                PatchView(
                    expansion if expansion.endswith("\n") else f"{expansion}\n"
                ).resolve(references),
                "base",
            )
            refinement = self._factory.refine(
                correction.base,
                correction.after,
                slices,
                "base",
                force=True,
            )
        except Exception as error:
            raise ValueError(
                f"RefineNode requires parseable expanded patch content: {expansion!r}"
            ) from error

        return {
            "refinement": str(refinement),
        }


class VerifyNode:
    """Check whether the refined corrective pair is semantically separable."""

    def __init__(
        self,
        llm: object | None = None,
    ) -> None:
        self._llm = llm

    def run(self, input: VerifyInput) -> VerifyOutput:
        """Check whether the refined corrective pair is semantically separable."""
        rationale = input.get("rationale")
        refinement = input.get("refinement")
        causes = input.get("causes")
        if self._llm is None:
            raise ValueError(
                "VerifyNode requires rationale, hint, refinement, and llm."
            )
        if rationale is None or refinement is None or causes is None:
            raise ValueError("VerifyNode requires rationale, causes, and refinement.")
        if not rationale or not refinement or not causes:
            raise ValueError(
                "VerifyNode requires non-empty rationale, causes, and refinement."
            )

        try:
            renderer = PatchView(refinement)
        except Exception as error:
            raise ValueError(
                f"VerifyNode requires parseable refined patch content: {refinement!r}"
            ) from error
        base_snippet = renderer.base()
        after_snippet = renderer.after()
        if not base_snippet.strip() or not after_snippet.strip():
            raise ValueError("VerifyNode requires non-empty base and after snippets.")
        try:
            model = self._llm.with_structured_output(
                VerifyResponseSchema,
                method="json_schema",
                strict=True,
            )
        except (AttributeError, TypeError) as error:
            raise ValueError(
                "VerifyNode requires an llm that supports structured schema mode."
            ) from error
        try:
            base = VerifyResponseSchema.model_validate(
                model.invoke(
                    self._build_prompt(
                        rationale=rationale.strip(),
                        hints=self._hint(causes),
                        snippet=base_snippet,
                    )
                )
            )
            after = VerifyResponseSchema.model_validate(
                model.invoke(
                    self._build_prompt(
                        rationale=rationale.strip(),
                        hints=self._hint(causes),
                        snippet=after_snippet,
                    )
                )
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise ValueError("VerifyNode requires structured verify output.") from error
        return {
            "base": {
                "defect": base.defect,
                "reason": base.reason,
            },
            "after": {
                "defect": after.defect,
                "reason": after.reason,
            },
            "verified": base.defect is True and after.defect is False,
        }

    def _build_prompt(
        self,
        *,
        rationale: str,
        hints: str,
        snippet: str,
    ) -> str:
        template = dedent(
            """
            Determine whether the provided code snippet contains the defect described by the root cause and defect statements.
            Be conservative. If you are unsure, return `defect: false`.
            Your boolean and your reason must agree.

            Respond only in JSON.
            Use this schema: {{"defect": boolean, "reason": string}}.

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

    def _hint(
        self,
        causes: list[object],
    ) -> str:
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


class BlameNode:
    """Extract candidate commits from the expanded hint references."""

    def __init__(self, factory: PatchFactory | None = None) -> None:
        self._factory = factory

    def _exists(
        self,
        commit: str | None,
        path: str,
    ) -> bool:
        if commit is None:
            return False
        try:
            self._factory.repository.blob(commit, path)
        except FileNotFoundError:
            return False
        return True

    def run(self, input: BlameInput) -> BlameOutput:
        """Extract candidate commits from the expanded hint references."""
        correction = input.get("correction")
        expansion = input.get("expansion")
        causes = input.get("causes")
        if correction is None or self._factory is None:
            raise ValueError(
                "BlameNode requires correction, expansion, causes, and factory."
            )
        if expansion is None or causes is None:
            raise ValueError("BlameNode requires expansion and causes.")
        if not expansion.strip() or not causes:
            raise ValueError("BlameNode requires non-empty expansion and causes.")

        source = expansion if expansion.endswith("\n") else f"{expansion}\n"
        try:
            renderer = PatchView(source)
        except Exception as error:
            raise ValueError(
                f"BlameNode requires parseable expanded patch contentt: {source!r}"
            ) from error

        references = [
            current["reference"]
            for item in causes
            for current in list(item.get("statements") or [])
        ]
        slices = renderer.resolve(references)

        grouped: dict[tuple[str, str | None], list[Slice]] = {}
        times: dict[str, object] = {}

        for current in slices:
            path = current.file[0]
            entries = [
                (anchor.base[0], anchor.base[1] - 1)
                for anchor in current.anchors
                if anchor.base != (0, 0)
            ]
            blamed = self._factory.repository.blame(path, correction.base, entries)

            for commit, _, origin in blamed:
                if commit is None or origin is None:
                    continue
                parents = self._factory.repository.parents(commit)
                parent = parents[0] if parents else None
                anchor = Anchor(
                    base=(0, 0),
                    after=(origin[0], origin[1] + 1),
                )
                grouped.setdefault((commit, parent), []).append(
                    Slice(
                        file=((path if self._exists(parent, path) else None), path),
                        anchors=[anchor],
                    )
                )
                if commit not in times:
                    times[commit] = self._factory.repository.meta(commit).time

        candidates = [
            {
                "commit": commit,
                "parent": parent,
                "slices": [
                    {
                        "file": item.file,
                        "anchors": [
                            {
                                "base": anchor.base,
                                "after": anchor.after,
                            }
                            for anchor in item.anchors
                        ],
                    }
                    for item in grouped[(commit, parent)]
                ],
            }
            for commit, parent in sorted(
                grouped,
                key=lambda key: times[key[0]],
                reverse=True,
            )
            if grouped[(commit, parent)]
        ]
        return {
            "candidates": candidates,
        }


class CompareNode:
    """Compare the current candidate commit against its parent."""

    def __init__(
        self,
        factory: PatchFactory | None = None,
        llm: object | None = None,
    ) -> None:
        self._factory = factory
        self._llm = llm

    def run(self, input: CompareInput) -> CompareOutput:
        """Compare the current candidate commit against its parent."""
        rationale = input.get("rationale")
        causes = input.get("causes")
        candidates = input.get("candidates")
        current = input.get("index", -1)
        if self._llm is None or self._factory is None:
            raise ValueError(
                "CompareNode requires rationale, causes, candidates, and llm."
            )
        if rationale is None or causes is None or candidates is None:
            raise ValueError("CompareNode requires rationale, causes, and candidates.")
        if not rationale or not causes or not candidates:
            raise ValueError(
                "CompareNode requires non-empty rationale, causes, and candidates."
            )
        index = current + 1
        if index < 0 or index >= len(candidates):
            raise ValueError("CompareNode requires an available next candidate.")
        item = dict(candidates[index])
        commit = item["commit"]
        parent = item["parent"]
        slices = [
            Slice(
                file=tuple(value["file"]),
                anchors=[
                    Anchor(
                        base=tuple(anchor["base"]),
                        after=tuple(anchor["after"]),
                    )
                    for anchor in value["anchors"]
                ],
            )
            for value in item["slices"]
        ]
        refinement = str(
            self._factory.refine(
                parent,
                commit,
                slices,
                "after",
                force=False,
            )
        )
        try:
            renderer = PatchView(refinement)
        except Exception as error:
            raise ValueError(
                f"CompareNode requires parseable unified patch content: {refinement!r}"
            ) from error
        before = renderer.base()
        after = renderer.after()

        hints = self._hint(causes)
        try:
            presence = self._llm.with_structured_output(
                CompareResponseSchema,
                method="json_schema",
                strict=True,
            )
            assessment = self._llm.with_structured_output(
                VerifyResponseSchema,
                method="json_schema",
                strict=True,
            )
        except (AttributeError, TypeError) as error:
            raise ValueError(
                "CompareNode requires an llm that supports structured schema mode."
            ) from error
        candidate_presence, candidate_assessment = self._compare(
            presence=presence,
            assessment=assessment,
            rationale=rationale.strip(),
            hints=hints,
            snippet=after,
        )
        parent_presence, parent_assessment = self._compare(
            presence=presence,
            assessment=assessment,
            rationale=rationale.strip(),
            hints=hints,
            snippet=before,
        )
        return {
            "index": index,
            "candidate": {
                "commit": commit,
                "presence": {
                    "passed": candidate_presence.present,
                    "reason": candidate_presence.reason,
                },
                "assessment": {
                    "passed": candidate_assessment.defect,
                    "reason": candidate_assessment.reason,
                },
                "defect": (
                    candidate_presence.present is True
                    and candidate_assessment.defect is True
                ),
            },
            "parent": {
                "commit": parent,
                "presence": {
                    "passed": parent_presence.present,
                    "reason": parent_presence.reason,
                },
                "assessment": {
                    "passed": parent_assessment.defect,
                    "reason": parent_assessment.reason,
                },
                "defect": (
                    parent_presence.present is True
                    and parent_assessment.defect is True
                ),
            },
        }

    def _compare(
        self,
        *,
        presence: object,
        assessment: object,
        rationale: str,
        hints: str,
        snippet: str,
    ) -> tuple[CompareResponseSchema, VerifyResponseSchema]:
        if not snippet.strip():
            return (
                CompareResponseSchema(
                    present=False,
                    reason="Code snippet is empty.",
                ),
                VerifyResponseSchema(
                    defect=False,
                    reason="Code snippet is empty.",
                ),
            )
        return (
            CompareResponseSchema.model_validate(
                presence.invoke(self._presence_prompt(hints=hints, snippet=snippet))
            ),
            VerifyResponseSchema.model_validate(
                assessment.invoke(
                    self._assessment_prompt(
                        rationale=rationale,
                        hints=hints,
                        snippet=snippet,
                    )
                )
            ),
        )

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

    def _presence_prompt(self, *, hints: str, snippet: str) -> str:
        template = dedent(
            """
            Determine whether the provided code snippet matches the defect statements.
            Be conservative. If you are unsure, return `present: false`.
            Your boolean and your reason must agree.

            Respond only in JSON.
            Use this schema: {{"present": boolean, "reason": string}}.

            Defect statements:
            {hints}

            Code snippet:
            {snippet}
            """
        ).strip()
        return template.format(
            hints=hints,
            snippet=snippet,
        )

    def _assessment_prompt(
        self,
        *,
        rationale: str,
        hints: str,
        snippet: str,
    ) -> str:
        template = dedent(
            """
            Determine whether the provided code snippet contains the defect described by the root cause and defect statements.
            Be conservative. If you are unsure, return `defect: false`.
            Your boolean and your reason must agree.

            Respond only in JSON.
            Use this schema: {{"defect": boolean, "reason": string}}.

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


class SelectNode:
    """Accept the first candidate whose comparison matches the target dichotomy."""

    def run(self, input: SelectInput) -> SelectOutput:
        """Accept the first candidate whose comparison matches the target dichotomy."""
        compare = input["compare"]
        candidate = compare["candidate"]
        parent = compare["parent"]
        commit = candidate["commit"]
        if candidate["defect"] is True and parent["defect"] is False and commit:
            return {"introducer": commit}
        return {}


class AbortNode:
    """Terminate the semantic path without attribution."""

    def run(self, input: dict[str, object]) -> AbortOutput:
        """Terminate the semantic path without attribution."""
        return {"aborted": True}


class FinalizeNode:
    """Assemble the final attribution artifact."""

    def run(self, input: dict[str, object]) -> FinalizeOutput:
        """Assemble the final attribution artifact."""
        return {"aborted": False}
