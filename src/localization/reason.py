from __future__ import annotations

import inspect
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import BaseModel

from benchmark.attribution.domain import Change
from benchmark.attribution.search import (
    DuplicateCallGuard,
    ResolvedErrorFilter,
    TurnBudget,
)
from benchmark.localization.agent import LocateTool, LocateToolFactory


class LocateLocationState(TypedDict):
    """One defect location in the defect revision."""

    file: str
    line: int


class LocateInput(TypedDict, total=False):
    correction: Change
    rationale: str
    refinement: str
    defect: Change


class LocateOutput(TypedDict, total=False):
    defect: Change
    locations: list[LocateLocationState]
    reason: str


class LocateItemSchema(BaseModel):
    file: str
    line: int


class LocateResponseSchema(BaseModel):
    locations: list[LocateItemSchema]
    reason: str | None = None


class LocateNode:
    """Locate the defect statements within the known inducing commit."""

    def __init__(
        self,
        tool: LocateTool | LocateToolFactory | None = None,
        llm: object | None = None,
        steps: int = 12,
    ) -> None:
        if steps < 1:
            raise ValueError("LocateNode steps must be positive.")
        self._tool = tool
        self._llm = llm
        self._steps = steps

    def run(self, input: LocateInput) -> LocateOutput:
        """Mark the defect locations in the inducing change via agentic search.

        The agent searches the inducing change ``defect`` with snapshot-local
        ``show``/``grep`` tools, guided by the root cause and the refined context
        of the correction's fix, and returns the file and line of each defect
        location. The locations are taken as the agent reports them; no post-hoc
        validation is applied, mirroring attribution.

        The inducing change is deliberately NOT shown up front: revealing its
        diff would hand the agent the answer instead of letting it search.
        """
        correction = input.get("correction")
        rationale = input.get("rationale")
        refinement = input.get("refinement")
        defect = input.get("defect")
        if self._llm is None or self._tool is None:
            raise ValueError(
                "LocateNode requires correction, rationale, refinement, defect, "
                "tool, and llm."
            )
        if (
            correction is None
            or rationale is None
            or refinement is None
            or defect is None
        ):
            raise ValueError(
                "LocateNode requires correction, rationale, refinement, and defect."
            )
        if not rationale.strip() or not refinement.strip():
            raise ValueError(
                "LocateNode requires non-empty rationale and refinement."
            )

        tool = self._tool
        if isinstance(tool, LocateToolFactory):
            tool = tool.create(correction, defect)

        payload = self._search(
            rationale=rationale.strip(),
            refinement=refinement.strip(),
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
        refinement: str,
        defect: Change,
        correction: Change,
        tool: LocateTool,
    ) -> LocateResponseSchema:
        node = ToolNode(
            tool.tools(),
            handle_tool_errors=lambda error: str(error),
            wrap_tool_call=DuplicateCallGuard(),
        )
        budget = TurnBudget(self._steps, max_errors=3)
        # Each react-agent cycle traverses 4 graph nodes (pre_model_hook, agent,
        # post_model_hook, tools) plus a final cycle to reach the structured
        # response.
        limit = (self._steps + budget.max_errors + 1) * 4
        graph = create_react_agent(
            self._llm,
            tools=node,
            prompt=self._prompt(),
            response_format=(
                "Return the defect locations as a JSON object with a `locations` "
                "array of {file, line} and an optional `reason`.",
                LocateResponseSchema,
            ),
            pre_model_hook=ResolvedErrorFilter(),
            post_model_hook=budget,
        )
        result = graph.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=self._message(
                            rationale=rationale,
                            refinement=refinement,
                            defect=defect,
                            correction=correction,
                        )
                    )
                ],
                "remaining_steps": limit,
            },
            config={"recursion_limit": limit},
        )
        structured = (
            result["structured_response"] if isinstance(result, dict) else result
        )
        if isinstance(structured, LocateResponseSchema):
            return structured
        return LocateResponseSchema.model_validate(structured)

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

            Your initial message gives the root cause and the refined context of the
            corrective change, plus two commit SHAs: the defective change and the
            corrective change. The defective change is not shown to you; you locate the
            defect in it with the tools. Every show and grep call takes one of these
            two SHAs.

            Workflow:
            1. From the root cause, identify the responsible code. Grep the defective
               change and read the relevant file with show.
            2. If you cannot locate it directly, keep searching the defective change
               until you do.
            3. You may show or grep the corrective change too, only to locate code or
               match it across the two changes.
            4. Report each location conservatively: a line only if the root cause makes
               it responsible, not if it is merely adjacent or incidentally touched.

            Report only code lines. Never report documentation, configuration, or data
            lines; you may read such files for context, but they are never a defect
            location.

            Return a JSON object with a `locations` array of {file, line} objects and
            an optional `reason` string.
            """
        )

    def _message(
        self,
        *,
        rationale: str,
        refinement: str,
        defect: Change,
        correction: Change,
    ) -> str:
        return inspect.cleandoc(
            """
            # Commits
            Defective change, where you locate the defect: {defect}
            Corrective change, for reference: {correction}
            Pass one of these two full SHAs to show and grep.

            # Root cause
            {rationale}

            # Refined context of the corrective change
            {refinement}
            """
        ).format(
            rationale=rationale,
            refinement=refinement,
            defect=defect["after"][:12],
            correction=correction["after"][:12],
        )

