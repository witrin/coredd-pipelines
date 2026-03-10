from __future__ import annotations
from textwrap import dedent
from typing import Protocol, TypedDict

from pydantic import BaseModel, ValidationError

from benchmark.attribution.domain import (
    Anchor,
    Change,
    FileOperation,
    OperationType,
    Slice,
)
from benchmark.attribution.diff import PatchFactory, PatchScope, PatchView
from benchmark.attribution.tracing import (
    AttributionRunnable,
)


NodeState = dict[str, object]
# Open per-node working-state mapping stored under AttributionGraphState["nodes"].


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


class AttributionGraphState(TypedDict, total=False):
    """Top-level graph state.

    Stable cross-phase artifacts live at the top level.
    Node-local working state lives under ``nodes``.
    """

    correction: Change
    summary: str
    rationale: str
    files: list[str]
    introducer: str | None
    aborted: bool
    nodes: dict[str, NodeState]


class GraphNode(Protocol):
    def run(self, state: AttributionGraphState) -> dict[str, object]: ...


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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Summarize the corrective delta."""
        correction = state.get("correction")
        if correction is None or self._factory is None or self._llm is None:
            return {}

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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Derive a rationale for the corrective change."""
        correction = state.get("correction")
        summary = state.get("summary")
        if correction is None or summary is None or self._factory is None or self._llm is None:
            raise ValueError("ExplainNode requires correction, summary, factory, and llm")

        nodes = state.get("nodes") or {}
        explain = nodes.get("explain") or {}
        runs = list(explain.get("runs") or [])
        samples = explain.get("samples", self._samples)
        if samples is None or samples < 1:
            samples = self._samples
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
            "nodes": {
                **nodes,
                "explain": {
                    **explain,
                    "samples": samples,
                    "runs": next_runs,
                },
            },
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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Aggregate the repeated explain runs into a first stable result."""
        nodes = state.get("nodes") or {}
        explain = nodes.get("explain") or {}
        runs = list(explain.get("runs") or [])
        if not runs:
            raise ValueError("AggregateNode requires explain runs.")
        if self._llm is None:
            raise ValueError("AggregateNode requires llm.")

        summary = state.get("summary")
        if summary is None:
            raise ValueError("AggregateNode requires summary.")

        allowed = list(state.get("files") or [])
        allowed_set = set(allowed)
        files: list[str] = []
        seen: set[str] = set()
        hypotheses: list[ExplainRunState] = []
        for run in runs:
            rationale = run.get("rationale")
            valid_files: list[str] = []
            valid_seen: set[str] = set()
            for path in run.get("files") or []:
                if path in valid_seen or path not in allowed_set:
                    continue
                valid_seen.add(path)
                valid_files.append(path)
                if path in seen:
                    continue
                seen.add(path)
                files.append(path)
            if rationale and rationale.strip():
                hypotheses.append(
                    {
                        "index": run.get("index"),
                        "rationale": rationale.strip(),
                        "files": valid_files,
                    }
                )
        if not hypotheses:
            raise ValueError("AggregateNode requires at least one rationale.")

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
            "nodes": {
                **nodes,
                "explain": {
                    **explain,
                    "aggregate": aggregate,
                },
            },
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
            run_files = "\n".join(run.get("files") or []) or "(none)"
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
                    index=run.get("index", "?"),
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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Expand semantic context around the selected changes."""
        correction = state.get("correction")
        if correction is None:
            raise ValueError("ExpandNode requires correction.")
        if self._factory is None:
            raise ValueError("ExpandNode requires factory.")
        files = state.get("files")
        if files is None:
            return {"nodes": {**(state.get("nodes") or {}), "expand": {}}}
        selected_files = [path for path in files if path]
        if not selected_files:
            return {"nodes": {**(state.get("nodes") or {}), "expand": {}}}

        selected_paths = set(selected_files)
        operations = [
            operation
            for operation in self._factory.repository.manifest(
                correction.base,
                correction.after,
            )
            if (operation.after or operation.base) in selected_paths
        ]
        if not operations:
            return {"nodes": {**(state.get("nodes") or {}), "expand": {}}}

        expansion = self._factory.expand(
            correction.base,
            correction.after,
            files=operations,
            level=self._level,
            n=self._context,
        )
        nodes = state.get("nodes") or {}
        expand = nodes.get("expand") or {}
        return {
            "nodes": {
                **nodes,
                "expand": {
                    **expand,
                    "expansion": str(expansion),
                },
            },
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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Generate a localization hint from the expansion context."""
        correction = state.get("correction")
        rationale = state.get("rationale")
        nodes = state.get("nodes") or {}
        expand = nodes.get("expand") or {}
        expansion = expand.get("expansion")
        if self._llm is None:
            raise ValueError("HintNode requires rationale, expansion, and llm.")
        if rationale is None or expansion is None:
            return {"nodes": {**nodes, "hint": {"causes": [], "corrections": []}}}
        if not rationale.strip() or not expansion.strip():
            return {"nodes": {**nodes, "hint": {"causes": [], "corrections": []}}}
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
        paths: dict[tuple[str | None, str | None], object | None] = {}
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
            "nodes": {
                **nodes,
                "hint": {
                    "causes": causes,
                    "corrections": corrections,
                },
            },
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
        paths: dict[tuple[str | None, str | None], object | None],
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
        paths: dict[tuple[str | None, str | None], object | None],
    ) -> HintStatementState:
        for current in renderer.resolve([reference]):
            path = self._path(correction, current.file, scope, paths)
            if path is None:
                continue
            for anchor in current.anchors:
                base, after = path.resolve(anchor)
                statement = base if side == "base" else after
                if statement is None:
                    continue
                return {
                    "reference": reference,
                    "scope": statement.scope,
                    "excerpt": statement.excerpt,
                }
        return {
            "reference": reference,
            "scope": None,
            "excerpt": None,
        }

    def _path(
        self,
        correction: Change,
        file: tuple[str | None, str | None],
        scope: PatchScope,
        paths: dict[tuple[str | None, str | None], object | None],
    ) -> object | None:
        if file not in paths:
            operation = self._file(file)
            try:
                paths[file] = scope.bind(
                    operation,
                    self._factory.read(correction.base, operation.base),
                    self._factory.read(correction.after, operation.after),
                )
            except ValueError:
                paths[file] = None
        return paths[file]

    def _file(self, file: tuple[str | None, str | None]) -> FileOperation:
        if file[0] is None:
            return FileOperation(base=file[0], after=file[1], operation=OperationType.ADD)
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

    def _empty(
        self,
        nodes: dict[str, object],
    ) -> dict[str, object]:
        return {"nodes": {**nodes, "refine": {}}}

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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Reduce the expansion context to a focused view."""
        correction = state.get("correction")
        nodes = state.get("nodes") or {}
        expand = nodes.get("expand") or {}
        hint = nodes.get("hint") or {}
        expansion = expand.get("expansion")
        causes = hint.get("causes")
        if correction is None or self._factory is None:
            raise ValueError("RefineNode requires correction, expansion, causes, and factory.")
        if expansion is None or causes is None:
            return self._empty(nodes)
        if not expansion.strip() or not causes:
            return self._empty(nodes)

        references = [
            current["reference"]
            for item in causes
            for current in list(item.get("statements") or [])
            if current.get("reference")
        ]
        if not references:
            return {"nodes": {**nodes, "refine": {}}}
        try:
            slices = self._anchors(
                PatchView(
                    expansion if expansion.endswith("\n") else f"{expansion}\n"
                ).resolve(references),
                "base",
            )
            if not slices:
                return self._empty(nodes)
            refinement = self._factory.refine(
                correction.base,
                correction.after,
                slices,
                "base",
                force=True,
            )
        except Exception as error:
            raise ValueError(f"RefineNode requires parseable expanded patch content: {expansion!r}") from error

        refine = nodes.get("refine") or {}
        return {
            "nodes": {
                **nodes,
                "refine": {
                    **refine,
                    "refinement": str(refinement),
                },
            },
        }


class VerifyNode:
    """Check whether the refined corrective pair is semantically separable."""

    def __init__(
        self,
        llm: object | None = None,
    ) -> None:
        self._llm = llm

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Check whether the refined corrective pair is semantically separable."""
        rationale = state.get("rationale")
        nodes = state.get("nodes") or {}
        hint = nodes.get("hint") or {}
        refine = nodes.get("refine") or {}
        refinement = refine.get("refinement")
        causes = hint.get("causes")
        if self._llm is None:
            raise ValueError(
                "VerifyNode requires rationale, hint, refinement, and llm."
            )
        if rationale is None or refinement is None or causes is None:
            return {"nodes": {**nodes, "verify": {}}}
        if not rationale or not refinement or not causes:
            return {"nodes": {**nodes, "verify": {}}}

        try:
            renderer = PatchView(refinement)
        except Exception as error:
            raise ValueError(
                f"VerifyNode requires parseable refined patch content: {refinement!r}"
            ) from error
        base_snippet = renderer.base()
        after_snippet = renderer.after()
        if not base_snippet.strip() or not after_snippet.strip():
            return {"nodes": {**nodes, "verify": {}}}
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
        verify = nodes.get("verify") or {}
        return {
            "nodes": {
                **nodes,
                "verify": {
                    **verify,
                    "base": {
                        "defect": base.defect,
                        "reason": base.reason,
                    },
                    "after": {
                        "defect": after.defect,
                        "reason": after.reason,
                    },
                    "verified": base.defect is True and after.defect is False,
                },
            },
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
                scope = current.get("scope")
                excerpt = current.get("excerpt")
                if not scope or not excerpt:
                    continue
                key = (scope, excerpt)
                if key in seen:
                    continue
                seen.add(key)
                statements.append(f"- {scope} {excerpt}")
            if statements:
                lines.append("\n".join(statements))
                continue
            reason = item.get("reason")
            if reason and reason.strip():
                lines.append(f"- {reason.strip()}")
        if not lines:
            raise ValueError("VerifyNode requires non-empty cause reasons.")
        return "\n".join(lines)


class BlameNode:
    """Extract candidate commits from the expanded hint references."""

    def __init__(self, factory: PatchFactory | None = None) -> None:
        self._factory = factory

    def _empty(
        self,
        nodes: dict[str, object],
    ) -> dict[str, object]:
        return {
            "nodes": {
                **nodes,
                "blame": {
                    **(nodes.get("blame") or {}),
                    "candidates": [],
                },
            },
        }

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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Extract candidate commits from the expanded hint references."""
        correction = state.get("correction")
        nodes = state.get("nodes") or {}
        expand = nodes.get("expand") or {}
        hint = nodes.get("hint") or {}
        expansion = expand.get("expansion")
        causes = hint.get("causes")
        if correction is None or self._factory is None:
            raise ValueError("BlameNode requires correction, expansion, causes, and factory.")
        if expansion is None or causes is None:
            return self._empty(nodes)
        if not expansion.strip() or not causes:
            return self._empty(nodes)

        source = expansion if expansion.endswith("\n") else f"{expansion}\n"
        try:
            renderer = PatchView(source)
        except Exception as error:
            raise ValueError(
                f"BlameNode requires parseable expanded patch contentt: {source!r}"
            ) from error

        blame = nodes.get("blame") or {}
        slices = renderer.resolve(
            [
                current["reference"]
                for item in causes
                for current in list(item.get("statements") or [])
                if current.get("reference")
            ]
        )
        if not slices:
            return self._empty(nodes)

        grouped: dict[tuple[str, str | None], list[Slice]] = {}
        times: dict[str, object] = {}

        for current in slices:
            path = current.file[0]
            if path is None or path == "":
                continue
            entries = [
                (anchor.base[0], anchor.base[1] - 1)
                for anchor in current.anchors
                if anchor.base != (0, 0)
            ]
            if not entries:
                continue
            blamed = self._factory.repository.blame(path, correction.base, entries)

            for _, item in zip(entries, blamed):
                commit, origin = item
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
            "nodes": {
                **nodes,
                "blame": {
                    **blame,
                    "candidates": candidates,
                },
            },
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

    def _empty(
        self,
        nodes: dict[str, object],
        compare: dict[str, object],
        index: int,
    ) -> dict[str, object]:
        return {
            "nodes": {
                **nodes,
                "compare": {
                    **compare,
                    "index": index,
                    "candidate": {},
                    "parent": {},
                },
            },
        }

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Compare the current candidate commit against its parent."""
        rationale = state.get("rationale")
        nodes = state.get("nodes") or {}
        blame = nodes.get("blame") or {}
        hint = nodes.get("hint") or {}
        compare = nodes.get("compare") or {}
        causes = hint.get("causes")
        candidates = blame.get("candidates")
        current = compare.get("index", -1)
        if self._llm is None or self._factory is None:
            raise ValueError("CompareNode requires rationale, causes, candidates, and llm.")
        if rationale is None or causes is None or candidates is None:
            return self._empty(nodes, compare, current if current is not None else -1)
        if not rationale or not causes or not candidates:
            return self._empty(nodes, compare, current if current is not None else -1)
        index = current + 1
        if index < 0 or index >= len(candidates):
            return self._empty(nodes, compare, current)
        try:
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
        except (TypeError, KeyError, ValueError) as error:
            raise ValueError("CompareNode requires prepared candidate slices.") from error
        if not commit or not parent or not slices:
            raise ValueError("CompareNode requires prepared candidate slices.")
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
            "nodes": {
                **nodes,
                "compare": {
                    **compare,
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
                },
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
                scope = current.get("scope")
                excerpt = current.get("excerpt")
                if not scope or not excerpt:
                    continue
                key = (scope, excerpt)
                if key in seen:
                    continue
                seen.add(key)
                statements.append(f"- {scope} {excerpt}")
            if statements:
                lines.append("\n".join(statements))
                continue
            reason = item.get("reason")
            if reason and reason.strip():
                lines.append(f"- {reason.strip()}")
        if not lines:
            return ""
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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Accept the first candidate whose comparison matches the target dichotomy."""
        nodes = state.get("nodes") or {}
        compare = nodes.get("compare") or {}
        candidate = compare.get("candidate") or {}
        parent = compare.get("parent") or {}
        commit = candidate.get("commit")
        if (
            candidate.get("defect") is True
            and parent.get("defect") is False
            and commit
        ):
            return {"introducer": commit}
        return {}


class AbortNode:
    """Terminate the semantic path without attribution."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Terminate the semantic path without attribution."""
        return {"aborted": True}


class FinalizeNode:
    """Assemble the final attribution artifact."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Assemble the final attribution artifact."""
        return {"aborted": False}


class AttributionGraph:
    """Wire node instances into a minimal semantic LangGraph."""

    def __init__(
        self,
        summarize: GraphNode | None = None,
        explain: GraphNode | None = None,
        aggregate: GraphNode | None = None,
        expand: GraphNode | None = None,
        hint: GraphNode | None = None,
        refine: GraphNode | None = None,
        verify: GraphNode | None = None,
        blame: GraphNode | None = None,
        compare: GraphNode | None = None,
        select: GraphNode | None = None,
        abort: GraphNode | None = None,
        finalize: GraphNode | None = None,
    ) -> None:
        if (
            summarize is None
            or explain is None
            or aggregate is None
            or expand is None
            or hint is None
            or refine is None
            or verify is None
        ):
            raise ValueError("AttributionGraph requires explicit semantic nodes.")
        self._summarize = summarize
        self._explain = explain
        self._aggregate = aggregate
        self._expand = expand
        self._hint = hint
        self._refine = refine
        self._verify = verify
        self._blame = blame or BlameNode()
        self._compare = compare or CompareNode()
        self._select = select or SelectNode()
        self._abort = abort or AbortNode()
        self._finalize = finalize or FinalizeNode()

    def build(self) -> object:
        """Build and compile the semantic attribution graph."""
        from langchain_core.runnables import RunnableLambda
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(AttributionGraphState)
        graph.add_node(
            "summarize",
            RunnableLambda(self._summarize.run).with_config(run_name="summarize"),
        )
        graph.add_node(
            "explain",
            RunnableLambda(self._explain.run).with_config(run_name="explain"),
        )
        graph.add_node(
            "aggregate",
            RunnableLambda(self._aggregate.run).with_config(run_name="aggregate"),
        )
        graph.add_node(
            "expand",
            RunnableLambda(self._expand.run).with_config(run_name="expand"),
        )
        graph.add_node(
            "hint",
            RunnableLambda(self._hint.run).with_config(run_name="hint"),
        )
        graph.add_node(
            "refine",
            RunnableLambda(self._refine.run).with_config(run_name="refine"),
        )
        graph.add_node(
            "verify",
            RunnableLambda(self._verify.run).with_config(run_name="verify"),
        )
        graph.add_node(
            "blame",
            RunnableLambda(self._blame.run).with_config(run_name="blame"),
        )
        graph.add_node(
            "compare",
            RunnableLambda(self._compare.run).with_config(run_name="compare"),
        )
        graph.add_node(
            "select",
            RunnableLambda(self._select.run).with_config(run_name="select"),
        )
        graph.add_node(
            "abort",
            RunnableLambda(self._abort.run).with_config(run_name="abort"),
        )
        graph.add_node(
            "finalize",
            RunnableLambda(self._finalize.run).with_config(run_name="finalize"),
        )

        graph.add_edge(START, "summarize")
        graph.add_edge("summarize", "explain")
        graph.add_conditional_edges(
            "explain",
            self.route_explain,
            {"explain": "explain", "aggregate": "aggregate"},
        )
        graph.add_edge("aggregate", "expand")
        graph.add_edge("expand", "hint")
        graph.add_edge("hint", "refine")
        graph.add_edge("refine", "verify")
        graph.add_conditional_edges(
            "verify",
            self.route_verify,
            {"blame": "blame", "abort": "abort"},
        )
        graph.add_edge("blame", "compare")
        graph.add_edge("compare", "select")
        graph.add_conditional_edges(
            "select",
            self.route_select,
            {"compare": "compare", "finalize": "finalize", "abort": "abort"},
        )
        graph.add_edge("abort", END)
        graph.add_edge("finalize", END)
        return AttributionRunnable(graph.compile())

    def route_verify(self, state: AttributionGraphState) -> str:
        """Route the graph after semantic verification."""
        nodes = state.get("nodes") or {}
        verify = nodes.get("verify") or {}
        if verify.get("verified") is True:
            return "blame"

        return "abort"

    def route_explain(self, state: AttributionGraphState) -> str:
        """Route the graph while collecting repeated explain runs."""
        nodes = state.get("nodes") or {}
        explain = nodes.get("explain") or {}
        runs = list(explain.get("runs") or [])
        samples = explain.get("samples")
        if samples is None or samples < 1:
            return "explain"
        if len(runs) < min(samples, max(1, len(state.get("files") or []))):
            return "explain"
        return "aggregate"

    def route_select(self, state: AttributionGraphState) -> str:
        """Route the graph after a candidate selection step."""
        if state.get("introducer") is not None:
            return "finalize"

        nodes = state.get("nodes") or {}
        blame = nodes.get("blame") or {}
        compare = nodes.get("compare") or {}
        candidates = blame.get("candidates") or []
        if not compare or "index" not in compare:
            return "abort"
        current_index = compare.get("index", -1)
        if current_index + 1 < len(candidates):
            return "compare"

        return "abort"
