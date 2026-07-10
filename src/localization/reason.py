from __future__ import annotations

import inspect
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolNode, create_react_agent
from openai import LengthFinishReasonError
from pydantic import BaseModel, ValidationError

from benchmark.attribution.domain import Change
from benchmark.attribution.search import (
    DuplicateCallGuard,
    ResolvedErrorFilter,
    TurnBudget,
)
from benchmark.localization.agent import LocateTool, LocateToolFactory
from benchmark.tracing import TraceRuntime


class LocateLocationState(TypedDict):
    """One defect location in the defect revision.

    ``line`` is the reference the show/grep tools print for that line: ``L<n>`` for a
    present line (context or addition, after-side number) or ``D<n>`` for a deleted
    line (base-side number). A deleted line is a valid defect location.
    """

    file: str
    line: str


class LocateInput(TypedDict, total=False):
    correction: Change
    rationale: str
    files: list[str]
    defect: Change


class LocateOutput(TypedDict, total=False):
    defect: Change
    locations: list[LocateLocationState]
    reason: str


class LocateItemSchema(BaseModel):
    file: str
    line: str


class LocateResponseSchema(BaseModel):
    locations: list[LocateItemSchema]
    reason: str | None = None


class ReferenceLine:
    """Parse and validate a ``L<n>``/``D<n>`` line reference against a diff.

    ``L<n>`` addresses an after-side line (context or addition); ``D<n>`` a deleted
    base-side line. The prefix is mandatory: a bare number is rejected, because it
    hides which side it means and would silently be tested against the after range
    even when the agent meant a deletion. The reference is valid when it falls inside
    a changed hunk of the defective change: ``L`` against the hunk's after range,
    ``D`` against its before range. Line numbers are 1-based, so ``n`` must be
    positive; ``L0``/``D0`` are malformed.
    """

    def __init__(self, reference: str) -> None:
        self.raw = str(reference).strip()
        self.deleted: bool | None = None
        self.number: int | None = None
        token = self.raw
        if token[:1] in ("L", "D") and token[1:].isdigit():
            number = int(token[1:])
            if number >= 1:
                self.deleted = token[0] == "D"
                self.number = number

    @property
    def valid_syntax(self) -> bool:
        return self.number is not None

    def classify(self, spans: "HunkSpans") -> str:
        """Return a verdict for this reference against the defect's hunk spans."""
        if self.number is None:
            return "bad_ref"
        if not spans.touches_file:
            return "wrong_file"
        inside = (
            spans.in_before(self.number)
            if self.deleted
            else spans.in_after(self.number)
        )
        return "ok" if inside else "outside_hunks"


class HunkSpans:
    """The before/after line ranges a change touches in one file."""

    def __init__(self, before: list[tuple[int, int]], after: list[tuple[int, int]]):
        self._before = before
        self._after = after

    @property
    def touches_file(self) -> bool:
        return bool(self._before or self._after)

    def in_before(self, line: int) -> bool:
        return any(lo <= line < hi for lo, hi in self._before)

    def in_after(self, line: int) -> bool:
        return any(lo <= line < hi for lo, hi in self._after)

    @classmethod
    def build(cls, repository: object, defect: Change, file: str) -> "HunkSpans":
        before: list[tuple[int, int]] = []
        after: list[tuple[int, int]] = []
        for patch in repository.diff(defect["base"], defect["after"], context=0):
            current = patch.delta.new_file.path or patch.delta.old_file.path
            if current != file:
                continue
            for hunk in patch.hunks:
                if hunk.old_lines > 0:
                    before.append((hunk.old_start, hunk.old_start + hunk.old_lines))
                if hunk.new_lines > 0:
                    after.append((hunk.new_start, hunk.new_start + hunk.new_lines))
        return cls(before, after)


class LocationValidator:
    """Split reported locations into accepted and rejected against the defect diff.

    A location is accepted when its ``L``/``D`` reference lands in a changed hunk of
    the defective change. Rejected locations carry a reason (``wrong_file``,
    ``outside_hunks``, ``bad_ref``) so the retry feedback can name exactly what was
    wrong while keeping the good markings fixed.
    """

    def __init__(self, repository: object, defect: Change) -> None:
        self._repository = repository
        self._defect = defect
        self._spans: dict[str, HunkSpans] = {}

    def _spans_for(self, file: str) -> HunkSpans:
        if file not in self._spans:
            self._spans[file] = HunkSpans.build(self._repository, self._defect, file)
        return self._spans[file]

    def partition(
        self, locations: list[LocateLocationState]
    ) -> tuple[list[LocateLocationState], list[tuple[LocateLocationState, str]]]:
        accepted: list[LocateLocationState] = []
        rejected: list[tuple[LocateLocationState, str]] = []
        for location in locations:
            verdict = ReferenceLine(location["line"]).classify(
                self._spans_for(location["file"])
            )
            if verdict == "ok":
                accepted.append(location)
            else:
                rejected.append((location, verdict))
        return accepted, rejected


class LocateNode:
    """Locate the defect statements within the known inducing commit."""

    def __init__(
        self,
        tool: LocateTool | LocateToolFactory | None = None,
        llm: object | None = None,
        steps: int = 12,
        retries: int = 0,
        attempts: int = 2,
    ) -> None:
        if steps < 1:
            raise ValueError("LocateNode steps must be positive.")
        if retries < 0:
            raise ValueError("LocateNode retries must be non-negative.")
        if attempts < 1:
            raise ValueError("LocateNode attempts must be positive.")
        self._tool = tool
        self._llm = llm
        self._steps = steps
        self._retries = retries
        self._attempts = attempts

    def run(self, input: LocateInput) -> LocateOutput:
        """Mark the defect locations in the inducing change via agentic search.

        The agent searches the inducing change ``defect`` with snapshot-local
        ``show``/``grep`` tools, guided by the root cause and the suspect files of
        the correction, and returns each defect location as an ``L<n>``/``D<n>``
        reference. When ``retries`` is set, a reference that does not land in a changed
        hunk of the defective change is rejected and the agent is given one more
        attempt to replace only the wrong markings, keeping the accepted ones fixed.

        The inducing change is deliberately NOT shown up front: revealing its
        diff would hand the agent the answer instead of letting it search.
        """
        correction = input.get("correction")
        rationale = input.get("rationale")
        files = input.get("files")
        defect = input.get("defect")
        if self._llm is None or self._tool is None:
            raise ValueError(
                "LocateNode requires correction, rationale, files, defect, "
                "tool, and llm."
            )
        if (
            correction is None
            or rationale is None
            or files is None
            or defect is None
        ):
            raise ValueError(
                "LocateNode requires correction, rationale, files, and defect."
            )
        if not rationale.strip():
            raise ValueError("LocateNode requires non-empty rationale.")

        tool = self._tool
        if isinstance(tool, LocateToolFactory):
            tool = tool.create(correction, defect)

        payload = self._search(
            rationale=rationale.strip(),
            files=list(files),
            defect=defect,
            correction=correction,
            tool=tool,
        )

        locations: list[LocateLocationState] = [
            {"file": item.file, "line": item.line} for item in payload.locations
        ]
        output: LocateOutput = {"defect": defect, "locations": locations}
        if payload.reason:
            output["reason"] = payload.reason
        return output

    def _search(
        self,
        *,
        rationale: str,
        files: list[str],
        defect: Change,
        correction: Change,
        tool: LocateTool,
    ) -> LocateResponseSchema:
        # Each react-agent cycle traverses 4 graph nodes (pre_model_hook, agent,
        # post_model_hook, tools) plus a final cycle to reach the structured
        # response.
        limit = (self._steps + 3 + 1) * 4
        state = {
            "messages": [
                HumanMessage(
                    content=self._message(
                        rationale=rationale,
                        files=files,
                        defect=defect,
                        correction=correction,
                    )
                )
            ],
            "remaining_steps": limit,
        }
        result, response = self._emit(tool, state, limit)

        # With retries disabled the locations are returned as the agent reports them,
        # matching the original behaviour so `retries=0` is a clean ablation baseline.
        if self._retries == 0:
            return response

        validator = LocationValidator(tool._repository, defect)
        accepted: list[LocateItemSchema] = []
        reason = response.reason

        for attempt in range(self._retries + 1):
            locations = [
                {"file": item.file, "line": item.line} for item in response.locations
            ]
            good, bad = validator.partition(locations)
            accepted.extend(
                item
                for item in response.locations
                if any(g["file"] == item.file and g["line"] == item.line for g in good)
            )
            reason = response.reason or reason
            if not bad or attempt == self._retries or not isinstance(result, dict):
                break
            # Retry: keep the good markings fixed, tell the agent exactly which
            # references were wrong and why, and ask it to replace only those.
            messages = list(result["messages"])
            messages.append(HumanMessage(content=self._feedback(accepted, bad)))
            result, response = self._emit(
                tool, {"messages": messages, "remaining_steps": limit}, limit
            )

        # Deduplicate the union of accepted markings across attempts.
        seen: set[tuple[str, str]] = set()
        unique: list[LocateItemSchema] = []
        for item in accepted:
            key = (item.file, item.line)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return LocateResponseSchema(locations=unique, reason=reason)

    def _graph(self, tool: LocateTool) -> object:
        # A fresh agent with its own ToolNode and TurnBudget. The budget is
        # stateful (it decrements per turn), so each attempt gets a new one and
        # therefore starts with the full turn allowance.
        node = ToolNode(
            tool.tools(),
            handle_tool_errors=lambda error: str(error),
            wrap_tool_call=DuplicateCallGuard(),
        )
        return create_react_agent(
            self._llm,
            tools=node,
            prompt=self._prompt(),
            response_format=(
                "Return the defect locations as a JSON object with a `locations` "
                "array of {file, line} and an optional `reason`.",
                LocateResponseSchema,
            ),
            pre_model_hook=ResolvedErrorFilter(),
            post_model_hook=TurnBudget(self._steps, max_errors=3),
        )

    def _invoke(
        self, tool: LocateTool, state: dict, limit: int
    ) -> tuple[object, LocateResponseSchema]:
        # One attempt: build a fresh agent, run it, and extract the structured
        # response. The invoke and the extraction are kept together so a
        # `LengthFinishReasonError` (from the agent's structured emit) and a
        # `ValidationError` (from parsing that emit) are raised inside the same
        # guarded scope in `_emit`.
        result = self._graph(tool).invoke(state, config={"recursion_limit": limit})
        return result, self._structured(result)

    def _emit(
        self, tool: LocateTool, state: dict, limit: int
    ) -> tuple[object, LocateResponseSchema]:
        # GLM-5.2 occasionally degenerates during the structured emit, either
        # hitting the token limit (`LengthFinishReasonError`) or producing malformed
        # JSON (`ValidationError`). The degeneration is nondeterministic, so a fresh
        # whole-agent run usually recovers; retry a bounded number of times and only
        # then let the failure propagate so the asset marks the trace aborted.
        #
        # graph.invoke appends the agent transcript to state["messages"] in place,
        # so each attempt must run against its own copy of the initial state — else
        # a retry would inherit the failed attempt's (poisoned) history and start
        # from a bloated prompt instead of the clean initial message.
        error: Exception | None = None
        for attempt in range(self._attempts):
            fresh = {**state, "messages": list(state["messages"])}
            try:
                return self._invoke(tool, fresh, limit)
            except (LengthFinishReasonError, ValidationError) as failure:
                error = failure
                self._mark(attempt, failure)
        raise error

    def _mark(self, attempt: int, failure: Exception) -> None:
        # Record the failed emit as its own event in the active trace. It both warns
        # that a further attempt is starting and cleanly separates the runs: every
        # event before this marker belongs to the failed attempt, everything after to
        # the next one. No-op when there is no active trace (e.g. unit tests).
        trace = TraceRuntime.current_trace()
        if trace is None:
            return
        trace.append(
            node="locate",
            span=f"emit-attempt-{attempt + 1}",
            parent=None,
            event="emit_attempt",
            payload={
                "attempt": attempt + 1,
                "attempts": self._attempts,
                "retrying": attempt + 1 < self._attempts,
                "error": str(failure),
                "error_type": type(failure).__name__,
            },
        )

    def _structured(self, result: object) -> LocateResponseSchema:
        structured = (
            result["structured_response"] if isinstance(result, dict) else result
        )
        if isinstance(structured, LocateResponseSchema):
            return structured
        return LocateResponseSchema.model_validate(structured)

    def _feedback(
        self,
        accepted: list[LocateItemSchema],
        rejected: list[tuple[LocateLocationState, str]],
    ) -> str:
        reasons = {
            "wrong_file": "that file is not changed by the defective change",
            "outside_hunks": "that line is not inside a changed hunk",
            "bad_ref": "that is not a valid L<n>/D<n> reference",
        }
        wrong = "\n".join(
            f"- {location['file']} {location['line']}: "
            f"{reasons.get(verdict, verdict)}"
            for location, verdict in rejected
        )
        kept = (
            "\n".join(f"- {item.file} {item.line}" for item in accepted)
            or "(none yet)"
        )
        return inspect.cleandoc(
            """
            Some reported locations are not defect locations of the defective change:
            {wrong}

            These are accepted and must stay as they are; do not report them again:
            {kept}

            Replace only the wrong ones. Report each defect location with the exact
            L<n> or D<n> reference the show/grep tools print — a deleted line uses its
            D<n> reference. Return the full corrected list.
            """
        ).format(wrong=wrong, kept=kept)

    def _prompt(self) -> str:
        # Task description and a high-level workflow. Tool semantics live in the
        # tool docstrings, which the agent reads through the function-calling
        # schemas; they are not restated here.
        return inspect.cleandoc(
            """
            Locate the source lines in the defective change that carry the defect the
            corrective change repairs.

            A line carries the defect if the root cause identifies it as responsible
            for the defect. This is independent of the corrective change: a line can
            carry the defect even if the corrective change never touches it, and a line
            the corrective change only touches incidentally (refactoring, renaming,
            formatting) does not.

            Report lines the defective change itself modified. The defect entered
            through those lines, even when the correction adds code elsewhere. Where
            the problem becomes visible later is not a defect location. When the
            correction only adds code, mark the modified lines of the defective change
            that made the addition necessary, never the place where the added code
            would go.

            Line references. In show the defective change every line carries a
            reference: `L<n>` for a present line (context or addition, an after-side
            number) and `D<n>` for a deleted line (a base-side number). Deleted lines
            are real defect locations — when the defect is that code was removed, report
            the `D<n>` reference of that removed line. grep searches a snapshot and
            never matches a deleted line; to reach one, open its window with show and
            use the `D<n>` reference shown there.

            Your initial message gives the root cause and the suspect files of the
            corrective change, plus two commit SHAs: the defective change and the
            corrective change. The defective change is not shown to you; you locate the
            defect in it with the tools. Every show and grep call takes one of these
            two SHAs.

            Workflow:
            1. From the root cause, identify the responsible code. Grep the defective
               change and read the relevant file with show; for a large diff pass
               show's start/end (L line numbers) to focus on the region.
            2. If you cannot locate it directly, keep searching the defective change
               until you do.
            3. You may show or grep the corrective change too, only to locate code or
               match it across the two changes.
            4. Report each location conservatively: a line only if the root cause makes
               it responsible, not if it is merely adjacent or incidentally touched.

            Report only code lines. Never report documentation, configuration, or data
            lines; you may read such files for context, but they are never a defect
            location.

            Return a JSON object with a `locations` array of {file, line} objects,
            where line is the exact `L<n>` or `D<n>` reference the tools print, and an
            optional `reason` string.
            """
        )

    def _message(
        self,
        *,
        rationale: str,
        files: list[str],
        defect: Change,
        correction: Change,
    ) -> str:
        listing = "\n".join(files) if files else "(none)"
        return inspect.cleandoc(
            """
            # Commits
            Defective change, where you locate the defect: {defect}
            Corrective change, for reference: {correction}
            Pass one of these two full SHAs to show and grep.

            # Root cause
            {rationale}

            # Suspect files of the corrective change
            {files}
            """
        ).format(
            rationale=rationale,
            files=listing,
            defect=defect["after"][:12],
            correction=correction["after"][:12],
        )

