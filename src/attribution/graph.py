from __future__ import annotations
from typing import Protocol, TypedDict

from pydantic import BaseModel, ValidationError
from unidiff import patch

from benchmark.attribution.domain import Change
from benchmark.attribution.diff import PatchRenderer, SourceDelta
from benchmark.attribution.tracing import (
    AttributionRunnable,
)


NodeState = dict[str, object]
# Open per-node working-state mapping stored under AttributionGraphState["nodes"].


class CandidateState(TypedDict, total=False):
    """Current candidate comparison state."""

    index: int
    commit: str | None
    parent: str | None
    comparison: tuple[int, int] | None


class ExplainRunState(TypedDict, total=False):
    """One explain pass result."""

    index: int
    rationale: str
    files: list[str]


class AggregateState(TypedDict, total=False):
    """Aggregated explain result."""

    rationale: str
    files: list[str]


class ExplainState(TypedDict, total=False):
    """Explain-phase working state."""

    samples: int
    runs: list[ExplainRunState]
    aggregate: AggregateState


class ExpandState(TypedDict, total=False):
    """Expand-phase working state."""

    expansion: str


class HintState(TypedDict, total=False):
    """Hint-phase working state."""

    causes: list[HintItemState]
    corrections: list[HintItemState]


class HintItemState(TypedDict, total=False):
    """One referenced hint item."""

    references: list[str]
    reason: str


class RefineState(TypedDict, total=False):
    """Refine-phase working state."""

    refinement: str


class VerifyItemState(TypedDict, total=False):
    """One verify pass result."""

    defect: bool
    reason: str


class VerifyState(TypedDict, total=False):
    """Verify-phase working state."""

    base: VerifyItemState
    after: VerifyItemState
    verified: bool


class BlameState(TypedDict, total=False):
    """Blame-phase working state."""

    candidates: list[str]


class CompareState(TypedDict, total=False):
    """Compare-phase working state."""

    candidate: CandidateState


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
        diff: SourceDelta | None = None,
        llm: object | None = None,
        instruction: str | None = None,
        context: int = 3,
    ) -> None:
        if context < 0:
            raise ValueError("context must be non-negative")
        self._diff = diff
        self._llm = llm
        self._instruction = instruction
        self._context = context

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Summarize the corrective delta."""
        correction = state.get("correction")
        if correction is None or self._diff is None or self._llm is None:
            return {}

        operations = self._diff.repository.manifest(
            correction.base,
            correction.after,
        )
        patch = self._diff.patch(
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
        lines = [
            self._instruction
            or (
                "Analyze the following patch.\n"
                "Summarize the code changes and the relationships between "
                "the changed hunks or files.\n"
                "Output only the summary text."
            ),
            "",
            context_note,
            "",
            "Patch:",
            patch_text,
        ]
        return "\n".join(lines)


class ExplainNode:
    """Derive a rationale for the corrective change."""

    def __init__(
        self,
        diff: SourceDelta | None = None,
        llm: object | None = None,
        instruction: str | None = None,
        context: int = 3,
        samples: int = 3,
    ) -> None:
        if context < 0:
            raise ValueError("context must be non-negative")
        if samples < 1:
            raise ValueError("samples must be positive")
        self._diff = diff
        self._llm = llm
        self._instruction = instruction
        self._context = context
        self._samples = samples

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Derive a rationale for the corrective change."""
        correction = state.get("correction")
        summary = state.get("summary")
        if (
            correction is None
            or not isinstance(summary, str)
            or self._diff is None
            or self._llm is None
        ):
            raise ValueError("ExplainNode requires correction, summary, diff, and llm")

        nodes = state.get("nodes") or {}
        explain = nodes.get("explain") or {}
        runs = list(explain.get("runs") or [])
        samples = explain.get("samples", self._samples)
        if not isinstance(samples, int) or samples < 1:
            samples = self._samples
        operations = self._diff.repository.manifest(
            correction.base,
            correction.after,
        )
        orders = self._file_orders(operations, samples)
        selected = orders[len(runs)]
        patch = str(
            self._diff.patch(
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
        response = self._structured_model().invoke(prompt)
        rationale, files = self._parse_response(response)
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
        self._append_order(orders, seen, canonical)
        self._append_order(orders, seen, list(reversed(canonical)))
        if len(orders) >= requested_runs:
            return orders
        for offset in self._rotation_offsets(len(canonical)):
            if len(orders) >= requested_runs:
                break
            rotated = canonical[offset:] + canonical[:offset]
            self._append_order(orders, seen, rotated)
        return orders

    def _append_order(
        self,
        orders: list[list[object]],
        seen: set[tuple[str, ...]],
        operations: list[object],
    ) -> None:
        key = tuple(self._operation_name(operation) for operation in operations)
        if key in seen:
            return
        seen.add(key)
        orders.append(operations)

    def _rotation_offsets(self, size: int) -> list[int]:
        if size <= 1:
            return []
        offsets: list[int] = []
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
        return offsets

    def _commit_messages(self, correction: Change) -> list[str]:
        shas = [correction.after]
        shas.extend(
            self._diff.repository.log(
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
            message = self._diff.repository.meta(sha).message.strip()
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
        lines = [
            self._instruction
            or (
                "Use the modification summary, the pull request description, "
                "the commit messages, and the patch to identify the likely "
                "root cause and the related files among the changed files."
            ),
            "Respond only in JSON.",
            'Use this schema: {"rationale": string, "files": [string, ...]}.',
            "",
            "Summary:",
            summary,
            "",
            "Pull request description:",
            description or "(none)",
            "",
            "Commit messages in (base, after]:",
        ]
        if commit_messages:
            for message in commit_messages:
                lines.extend([message, ""])
            lines.pop()
        else:
            lines.append("(none)")
        lines.extend(
            [
                "",
                "Patch:",
                patch_text,
                "",
                "Return only files that are relevant to the root cause and appear in the provided patch.",
            ]
        )
        return "\n".join(lines)

    def _structured_model(self) -> object:
        try:
            return self._llm.with_structured_output(
                ExplainResponseSchema,
                method="json_schema",
                strict=True,
            )
        except AttributeError as error:
            raise ValueError(
                "ExplainNode requires an llm that supports structured schema mode."
            ) from error
        except TypeError as error:
            raise ValueError(
                "ExplainNode requires an llm that supports structured schema mode."
            ) from error

    def _parse_response(self, response: object) -> tuple[str, list[str]]:
        try:
            payload = ExplainResponseSchema.model_validate(response)
        except (ValidationError, TypeError, ValueError) as error:
            raise ValueError(
                "ExplainNode requires structured explain output."
            ) from error
        return payload.rationale, list(payload.files)

    @staticmethod
    def _operation_name(operation: object) -> str:
        after = getattr(operation, "after", None)
        base = getattr(operation, "base", None)
        return str(after or base or "")


class AggregateNode:
    """Aggregate the repeated explain runs into a first stable result."""

    def __init__(
        self,
        llm: object | None = None,
        instruction: str | None = None,
    ) -> None:
        self._llm = llm
        self._instruction = instruction

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
        if not isinstance(summary, str):
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
                if (
                    not isinstance(path, str)
                    or path in valid_seen
                    or path not in allowed_set
                ):
                    continue
                valid_seen.add(path)
                valid_files.append(path)
                if path in seen:
                    continue
                seen.add(path)
                files.append(path)
            if isinstance(rationale, str) and rationale.strip():
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
        lines = [
            self._instruction
            or (
                "Review the explanations from repeated explain passes for the same change.\n"
                "Merge them into one final root-cause rationale using only the provided runs.\n"
                "Output only the final rationale text."
            ),
            "",
            "Summary:",
            summary,
            "",
            "Final related files:",
        ]
        if files:
            lines.extend(files)
        else:
            lines.append("(none)")
        lines.extend(["", "Explain runs:"])
        for run in runs:
            lines.extend(
                [
                    f"Run {run.get('index', '?')}:",
                    f"Rationale: {run['rationale']}",
                    "Files:",
                ]
            )
            run_files = run.get("files") or []
            if run_files:
                lines.extend(run_files)
            else:
                lines.append("(none)")
            lines.append("")
        lines.extend(
            [
                "Output only the final rationale text.",
            ]
        )
        return "\n".join(lines)


class ExpandNode:
    """Expand semantic context around the selected changes."""

    def __init__(
        self,
        diff: SourceDelta | None = None,
        *,
        level: int = 1,
        context: int = 3,
    ) -> None:
        if level < 1:
            raise ValueError("level must be positive")
        if context < 0:
            raise ValueError("context must be non-negative")
        self._diff = diff
        self._level = level
        self._context = context

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Expand semantic context around the selected changes."""
        correction = state.get("correction")
        selected_files = list(state.get("files") or [])
        if correction is None:
            raise ValueError("ExpandNode requires correction.")
        if self._diff is None:
            raise ValueError("ExpandNode requires diff.")
        if not selected_files:
            raise ValueError("ExpandNode requires files.")

        selected_paths = set(selected_files)
        operations = [
            operation
            for operation in self._diff.repository.manifest(
                correction.base,
                correction.after,
            )
            if (operation.after or operation.base) in selected_paths
        ]
        if not operations:
            raise ValueError("ExpandNode requires files present in the correction.")

        expansion = self._diff.expand(
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
        llm: object | None = None,
        instruction: str | None = None,
    ) -> None:
        self._llm = llm
        self._instruction = instruction

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Generate a localization hint from the expansion context."""
        rationale = state.get("rationale")
        nodes = state.get("nodes") or {}
        expand = nodes.get("expand") or {}
        expansion = expand.get("expansion")
        if (
            self._llm is None
            or not isinstance(rationale, str)
            or not rationale.strip()
            or not isinstance(expansion, str)
            or not expansion.strip()
        ):
            raise ValueError("HintNode requires rationale, expansion, and llm.")

        renderer = self._renderer(expansion)
        response = self._structured_model().invoke(
            self._build_prompt(
                rationale=rationale.strip(),
                expansion=renderer.render(),
            )
        )
        try:
            payload = HintResponseSchema.model_validate(response)
        except (ValidationError, TypeError, ValueError) as error:
            raise ValueError("HintNode requires structured hint output.") from error
        causes = self._parse_items(payload.causes)
        corrections = self._parse_items(payload.corrections)
        hint_state = nodes.get("hint") or {}
        hint = {
            key: value
            for key, value in hint_state.items()
            if key not in {"references", "cause", "correction"}
        }
        return {
            "nodes": {
                **nodes,
                "hint": {
                    **hint,
                    "causes": causes,
                    "corrections": corrections,
                },
            },
        }

    def _renderer(self, expansion: str) -> PatchRenderer:
        try:
            return PatchRenderer(expansion)
        except Exception as error:
            raise ValueError(
                "HintNode requires parseable expanded patch content."
            ) from error

    def _build_prompt(
        self,
        *,
        rationale: str,
        expansion: str,
    ) -> str:
        lines = [
            self._instruction
            or (
                "Identify the lines that describe the causes of the bug and the lines that "
                "implement the corrections.\n"
                "Use the reference labels from the left column exactly as provided.\n"
                "Use only the visible referenced lines from the provided context."
            ),
            "Respond only in JSON.",
            (
                'Use this schema: {"causes": [{"references": [string, ...], "reason": string}], '
                '"corrections": [{"references": [string, ...], "reason": string}]}.'
            ),
            "",
            "Root cause rationale:",
            rationale,
            "",
            "Expanded context:",
            expansion,
        ]
        return "\n".join(lines)

    def _structured_model(self) -> object:
        try:
            return self._llm.with_structured_output(
                HintResponseSchema,
                method="json_schema",
                strict=True,
            )
        except AttributeError as error:
            raise ValueError(
                "HintNode requires an llm that supports structured schema mode."
            ) from error
        except TypeError as error:
            raise ValueError(
                "HintNode requires an llm that supports structured schema mode."
            ) from error

    def _parse_items(
        self,
        value: object,
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
                    "references": references,
                    "reason": item.reason,
                }
            )
        return parsed


class RefineNode:
    """Reduce the expansion context to a focused view."""

    def __init__(self, diff: SourceDelta | None = None) -> None:
        self._diff = diff

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Reduce the expansion context to a focused view."""
        correction = state.get("correction")
        nodes = state.get("nodes") or {}
        expand = nodes.get("expand") or {}
        hint = nodes.get("hint") or {}
        expansion = expand.get("expansion")
        causes = hint.get("causes")
        if (
            correction is None
            or self._diff is None
            or not isinstance(expansion, str)
            or not expansion.strip()
            or not isinstance(causes, list)
        ):
            raise ValueError("RefineNode requires correction, expansion, causes, and diff.")

        references = [
            reference
            for item in causes
            if isinstance(item, dict)
            for reference in list(item.get("references") or [])
            if isinstance(reference, str)
        ]
        try:
            refined = self._diff.refine(
                patch.PatchSet(
                    expansion if expansion.endswith("\n") else f"{expansion}\n"
                ),
                references,
                correction.base,
                correction.after,
            )
        except Exception as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError("RefineNode requires parseable expanded patch content.") from error

        refine = nodes.get("refine") or {}
        return {
            "nodes": {
                **nodes,
                "refine": {
                    **refine,
                    "refinement": str(refined),
                },
            },
        }


class VerifyNode:
    """Check whether the refined corrective pair is semantically separable."""

    def __init__(
        self,
        llm: object | None = None,
        instruction: str | None = None,
    ) -> None:
        self._llm = llm
        self._instruction = instruction

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Check whether the refined corrective pair is semantically separable."""
        rationale = state.get("rationale")
        nodes = state.get("nodes") or {}
        hint = nodes.get("hint") or {}
        refine = nodes.get("refine") or {}
        refinement = refine.get("refinement")
        causes = hint.get("causes")
        corrections = hint.get("corrections")
        if (
            self._llm is None
            or not isinstance(rationale, str)
            or not rationale.strip()
            or not isinstance(refinement, str)
            or not refinement.strip()
            or not isinstance(causes, list)
            or not isinstance(corrections, list)
        ):
            raise ValueError(
                "VerifyNode requires rationale, hint, refinement, and llm."
            )

        renderer = self._renderer(refinement)
        base_snippet = renderer.base()
        after_snippet = renderer.after()
        if not base_snippet.strip() or not after_snippet.strip():
            raise ValueError("VerifyNode requires projected refined contexts.")
        model = self._structured_model()
        base = self._parse_response(
            model.invoke(
                self._build_prompt(
                    rationale=rationale.strip(),
                    hints=self._hint(causes, corrections),
                    snippet=base_snippet,
                )
            )
        )
        after = self._parse_response(
            model.invoke(
                self._build_prompt(
                    rationale=rationale.strip(),
                    hints=self._hint(causes, corrections),
                    snippet=after_snippet,
                )
            )
        )
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

    def _renderer(self, refinement: str) -> PatchRenderer:
        try:
            return PatchRenderer(refinement)
        except Exception as error:
            raise ValueError(
                "VerifyNode requires parseable refined patch content."
            ) from error

    def _build_prompt(
        self,
        *,
        rationale: str,
        hints: str,
        snippet: str,
    ) -> str:
        return "\n".join(
            [
                self._instruction
                or (
                    "You will be given:\n"
                    "- a code snippet\n"
                    "- the root cause of a bug\n"
                    "- explanations of that bug\n"
                    "\n"
                    "Your task is to determine whether the provided code snippet "
                    "contains that bug."
                ),
                "Respond only in JSON.",
                'Use this schema: {"defect": boolean, "reason": string}.',
                "",
                "Root cause:",
                rationale,
                "",
                "Bug explanations:",
                hints,
                "",
                "Code snippet:",
                snippet,
            ]
        )

    def _hint(
        self,
        causes: list[object],
        corrections: list[object],
    ) -> str:
        lines = [
            f"- {item['reason'].strip()}"
            for item in causes
            if isinstance(item, dict)
            and isinstance(item.get("reason"), str)
            and item["reason"].strip()
        ]
        if not lines:
            raise ValueError("VerifyNode requires non-empty cause reasons.")
        return "\n".join(lines)

    def _structured_model(self) -> object:
        try:
            return self._llm.with_structured_output(
                VerifyResponseSchema,
                method="json_schema",
                strict=True,
            )
        except AttributeError as error:
            raise ValueError(
                "VerifyNode requires an llm that supports structured schema mode."
            ) from error
        except TypeError as error:
            raise ValueError(
                "VerifyNode requires an llm that supports structured schema mode."
            ) from error

    def _parse_response(self, response: object) -> VerifyResponseSchema:
        try:
            return VerifyResponseSchema.model_validate(response)
        except (ValidationError, TypeError, ValueError) as error:
            raise ValueError("VerifyNode requires structured verify output.") from error


class BlameNode:
    """Extract candidate commits from the expanded hint references."""

    def __init__(self, diff: SourceDelta | None = None) -> None:
        self._diff = diff

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Extract candidate commits from the expanded hint references."""
        correction = state.get("correction")
        nodes = state.get("nodes") or {}
        expand = nodes.get("expand") or {}
        hint = nodes.get("hint") or {}
        expansion = expand.get("expansion")
        causes = hint.get("causes")
        if (
            correction is None
            or self._diff is None
            or not isinstance(expansion, str)
            or not expansion.strip()
            or not isinstance(causes, list)
        ):
            raise ValueError("BlameNode requires correction, expansion, causes, and diff.")

        source = expansion if expansion.endswith("\n") else f"{expansion}\n"
        try:
            renderer = PatchRenderer(source)
            expansion = patch.PatchSet(source)
        except Exception as error:
            raise ValueError(
                "BlameNode requires parseable expanded patch content."
            ) from error

        grouped: dict[str, set[int]] = {}
        for item in causes:
            if not isinstance(item, dict):
                continue
            for reference in list(item.get("references") or []):
                if not isinstance(reference, str):
                    continue
                resolved = renderer.resolve(reference)
                if resolved is None:
                    continue
                patched = expansion[resolved.file]
                line = patched[resolved.hunk][resolved.line]
                if not isinstance(line.source_line_no, int):
                    continue
                path = patched.source_file
                if not isinstance(path, str):
                    continue
                if path.startswith("a/"):
                    path = path[2:]
                if path == "" or path == "/dev/null":
                    continue
                grouped.setdefault(path, set()).add(line.source_line_no)

        blame = nodes.get("blame") or {}
        if not grouped:
            return {
                "nodes": {
                    **nodes,
                    "blame": {
                        **blame,
                        "candidates": [],
                    },
                },
            }

        ranked: dict[str, object] = {}
        for path, lines in grouped.items():
            ranges: list[tuple[int, int]] = []
            start: int | None = None
            stop: int | None = None
            for line in sorted(lines):
                if start is None:
                    start = line
                    stop = line
                    continue
                if line == stop + 1:
                    stop = line
                    continue
                ranges.append((start, stop))
                start = line
                stop = line
            if start is not None and stop is not None:
                ranges.append((start, stop))
            for commit in self._diff.repository.blame(path, correction.base, ranges):
                if commit in ranked:
                    continue
                ranked[commit] = self._diff.repository.meta(commit).time

        candidates = [
            commit
            for commit, _ in sorted(
                ranked.items(),
                key=lambda item: item[1],
                reverse=True,
            )
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

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Compare the current candidate commit against its parent."""
        return {}


class SelectNode:
    """Accept the first candidate whose comparison matches the target dichotomy."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Accept the first candidate whose comparison matches the target dichotomy."""
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
        return {}


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
        if not isinstance(samples, int) or samples < 1:
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
        candidate = compare.get("candidate") or {}
        current_index = candidate.get("index", 0)
        if current_index + 1 < len(candidates):
            return "compare"

        return "abort"
