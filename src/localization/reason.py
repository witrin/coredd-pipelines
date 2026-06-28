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
            Locate the lines in the inducing change that carry the defect the
            correction repairs.

            You are given the root cause of the defect and the refined context of
            how the correction fixed it. These describe the fix; the defect itself
            lives in the inducing change, which is NOT shown to you — you must find
            it with the tools.

            Workflow:
            1. From the root cause and the fix, identify the code that carries the
               defect. Grep the inducing change snapshot for that code and show the
               relevant file to read it.
            2. If the code is not directly present — renamed, moved, or in another
               file — grep for related identifiers to find where it lives.
            3. You may read the correction to understand the intended fix or to
               match code that was renamed between the two.
            4. Report the file and line of each location that carries the defect.
               Be conservative: report a line only if it carries the defect.

            A defect lives in source code. Report only lines of source code. Never
            report a line in documentation, configuration, data, or other non-code
            files, even if it mentions the defect — you may read such files for
            context, but they cannot be a defect location.
            """
        )

    def _message(
        self,
        *,
        rationale: str,
        refinement: str,
    ) -> str:
        return inspect.cleandoc(
            """
            # Root cause
            {rationale}

            # How the correction fixed it (refined context)
            {refinement}
            """
        ).format(rationale=rationale, refinement=refinement)

