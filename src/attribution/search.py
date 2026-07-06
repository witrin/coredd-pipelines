from __future__ import annotations

import inspect
import json
import re
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import BaseModel, Field

from benchmark.attribution.agent import (
    DiffRenderer,
    KnowledgeGraphTool,
    KnowledgeGraphToolFactory,
    RepositoryTool,
    RepositoryToolFactory,
)
from benchmark.attribution.diff.entity import EntityPath
from benchmark.attribution.domain import Change, FileOperation
from benchmark.attribution.history import VersionControl


class SeedItem(TypedDict):
    title: str
    body: str


class SeedInput(TypedDict, total=False):
    correction: Change


class SeedOutput(TypedDict):
    base: str
    after: str
    seed: list[SeedItem]


class Seed(TypedDict):
    base: str
    after: str
    seed: list[SeedItem]


class SearchOutput(TypedDict):
    introducer: str | None


class SearchResult(BaseModel):
    introducer: str | None = Field(
        default=None,
        description="Full 40-character lowercase hex SHA of the bug-introducing commit, or null.",
    )


class KnowledgeGraphSeedNode:
    """Build the correction context used by the agentic search flow."""

    def __init__(
        self,
        repository: VersionControl | None = None,
    ) -> None:
        self._repository = repository

    def run(self, input: SeedInput) -> SeedOutput:
        correction = input.get("correction")
        if correction is None or self._repository is None:
            raise ValueError(
                "KnowledgeGraphSeedNode requires correction and repository."
            )

        base = correction["base"]
        after = correction["after"]
        seed: list[SeedItem] = [{"title": "Correction Commit", "body": after}]

        message = self._repository.meta(after).message.strip()
        if message:
            seed.append({"title": "Commit Message", "body": message})

        operations = self._repository.manifest(base, after)
        files = self._files(operations)
        if files:
            seed.append({"title": "Modified Files", "body": "\n".join(files)})

        entities = self._entities(base, after, operations)
        if entities:
            seed.append({"title": "Modified Entities", "body": "\n".join(entities)})

        return {
            "base": base,
            "after": after,
            "seed": seed,
        }

    def _files(self, operations: list[FileOperation]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for operation in operations:
            path = operation.after or operation.base
            if path in seen:
                continue
            seen.add(path)
            result.append(path)
        return result

    def _entities(
        self,
        base: str,
        after: str,
        operations: list[FileOperation],
    ) -> list[str]:
        result: set[str] = set()

        for operation in operations:
            path = operation.after or operation.base
            language = self._language(path)
            if language is None:
                continue

            resolver = EntityPath(
                operation,
                self._blob(base, operation.base),
                self._blob(after, operation.after),
                language=language,
            )
            values = resolver.resolve()

            for _, base_urn, after_urn in values:
                urn = after_urn or base_urn
                entity = self._entity(path, urn)
                if entity is None:
                    continue
                result.add(entity)

        return sorted(result)

    def _blob(self, commit: str, path: str | None) -> bytes | None:
        if path is None:
            return None
        return self._repository.blob(commit, path)

    def _language(self, path: str) -> str | None:
        if path.endswith(".py"):
            return "python"
        if path.endswith(".ts"):
            return "typescript"
        if path.endswith(".tsx"):
            return "tsx"
        if path.endswith(".js") or path.endswith(".jsx"):
            return "javascript"
        if path.endswith(".java"):
            return "java"
        return None

    def _entity(self, path: str, urn: str) -> str | None:
        if urn == "entity:program":
            return None
        return f"file={path}:{urn.removeprefix('entity:')}"


class KnowledgeGraphSearchNode:
    """Run the agentic search flow over the prepared knowledge graph."""

    def __init__(
        self,
        tool: KnowledgeGraphTool | KnowledgeGraphToolFactory | None = None,
        llm: object | None = None,
    ) -> None:
        self._tool = tool
        self._llm = llm

    def run(
        self,
        input: Seed,
        *,
        steps: int = 10,
    ) -> SearchOutput:
        base = input.get("base")
        after = input.get("after")
        seed = input.get("seed")
        if steps < 1:
            raise ValueError("KnowledgeGraphSearchNode steps must be positive.")
        if (
            base is None
            or after is None
            or seed is None
            or self._tool is None
            or self._llm is None
        ):
            raise ValueError(
                "KnowledgeGraphSearchNode requires base, after, seed, tool, and llm."
            )

        tool = self._tool
        if isinstance(tool, KnowledgeGraphToolFactory):
            tool = tool.create(base, after)

        result = self._invoke(seed, tool, steps=steps)
        return self._normalize(result)

    def _invoke(
        self,
        seed: list[SeedItem],
        tool: KnowledgeGraphTool,
        *,
        steps: int,
    ) -> object:
        tools = tool.tools()
        node = ToolNode(
            tools,
            handle_tool_errors=lambda error: str(error),
            wrap_tool_call=DuplicateCallGuard(),
        )
        prompt = inspect.cleandoc(
            """
            Identify which commit first introduced the bug fixed by the given correction.

            ## Commit Types (Priority Order)
            1. blame (score 1.0): Code deleted or modified by the correction was last
               touched by this commit. Highest priority.
            2. blame-ancestor (score 0.6): Commit before the blame commit in file
               history. Lower priority.
            3. correction-ancestor (score 0.3): Commit after blame but before the
               correction. Lowest priority.

            ## Workflow
            1. Call candidates first. Use the fitness ranking as initial guidance, but
               override it if tool evidence supports a different commit.
            2. Verify the top-ranked candidate using content before deciding:
               - Does it ADD the code that the correction REMOVES?
               - Does it introduce the LOGIC that exhibits the bug?
            3. If the top candidate is refactoring, cleanup, or import churn, reject it
               and inspect related commits via neighbors or ancestors instead.
            4. Before selecting a candidate, confirm a direct causal link to the buggy
               logic. A commit that merely exposes or triggers a pre-existing bug is
               not the introducer.

            Each candidate lists modified files and entities (programs, classes,
            methods, functions). Use these to assess structural overlap with the
            correction.

            ## Constraints
            - Limit content to 3 calls per case. Use it to verify, not to explore.
            - If content fails, decide with the evidence you already have.
            - Make at least one investigation step before deciding unless a single
              candidate is conclusive.
            - Do not inspect all candidates exhaustively. Focus on the most plausible.

            Return ONLY the full 40-character introducer SHA, or null if no justified
            decision can be made.
            """
        )
        budget = TurnBudget(steps, max_errors=2)
        # Each react-agent cycle traverses 4 graph nodes
        # (pre_model_hook, agent, post_model_hook, tools) plus a final
        # cycle of up to 4 nodes to reach generate_structured_response.
        limit = (steps + budget.max_errors + 1) * 4
        graph = create_react_agent(
            self._llm,
            tools=node,
            prompt=prompt,
            response_format=(
                "Return the final answer as a JSON object with an `introducer` "
                "field holding the full 40-character SHA, or null.",
                SearchResult,
            ),
            pre_model_hook=ResolvedErrorFilter(),
            post_model_hook=budget,
        )
        return graph.invoke(
            {
                "messages": [HumanMessage(content=self._prompt(seed))],
                "remaining_steps": limit,
            },
            config={"recursion_limit": limit},
        )

    def _prompt(self, seed: list[SeedItem]) -> str:
        return SearchPrompt().render(seed)

    def _normalize(self, value: object) -> SearchOutput:
        return SearchNormalizer().normalize(value)


class SearchPrompt:
    """Serialize a seed list into the initial user message for the search agent."""

    def render(self, seed: list[SeedItem]) -> str:
        parts = ["# Context"]
        for item in seed:
            title = item["title"].strip()
            body = item["body"].strip()
            parts.append(f"## {title}\n````text\n{body}\n````")
        return "\n\n".join(parts)


class SearchNormalizer:
    """Normalize the LangGraph search-agent output into a SearchOutput dict."""

    def normalize(self, value: object) -> SearchOutput:
        structured = value["structured_response"] if isinstance(value, dict) else value
        introducer = (
            structured.introducer
            if isinstance(structured, BaseModel)
            else structured["introducer"]
        )
        if isinstance(introducer, str):
            match = re.search(r"[0-9a-f]{7,40}", introducer)
            introducer = match.group(0) if match else None
        return {"introducer": introducer}


class IntroducerValidator:
    """Check that a reported introducer exists and predates the correction.

    The search tools already forbid the agent from inspecting commits that are not
    ancestors of the correction, but the final structured answer bypasses those
    guards. A ``null`` answer is a deliberate abstention, not an error, so it is left
    to the caller; a non-null SHA is rejected when it does not resolve in the
    repository or is not an ancestor of the correction's after commit.
    """

    def __init__(self, repository: VersionControl, after: str) -> None:
        self._repository = repository
        self._after = after

    def check(self, introducer: str | None) -> tuple[bool, str | None]:
        if introducer is None:
            return True, None
        import pygit2

        handle = self._repository.handle
        try:
            resolved = str(
                handle.revparse_single(introducer).peel(pygit2.Commit).id
            )
        except (KeyError, ValueError):
            return False, (
                f"Commit {introducer} does not exist in this repository. Return the "
                f"full SHA of a real commit that predates the correction, or null."
            )
        after_oid = pygit2.Oid(hex=self._after)
        oid = pygit2.Oid(hex=resolved)
        if oid == after_oid or not handle.descendant_of(after_oid, oid):
            return False, (
                f"Commit {introducer[:12]} does not predate the correction. The "
                f"introducer must be an ancestor of {self._after[:12]}. Return an "
                f"earlier commit, or null."
            )
        return True, None


class DuplicateCallGuard:
    """Skip tool execution when the same call was already made with identical arguments."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def __call__(self, request, execute):
        key = (
            request.tool_call["name"],
            json.dumps(request.tool_call["args"], sort_keys=True),
        )
        if key in self._seen:
            return ToolMessage(
                content="You already called this tool with the same arguments. Try a different approach or return your final answer.",
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        self._seen.add(key)
        return execute(request)


class ResolvedErrorFilter:
    """Hide resolved error messages from the LLM input.

    Used as ``pre_model_hook`` on ``create_react_agent``.  Error
    ToolMessages that the model has already reacted to are excluded
    from the messages passed to the LLM.  The underlying state and
    trace remain unchanged.
    """

    def __call__(self, state: dict) -> dict:
        messages = state["messages"]

        boundary = None
        for position in range(len(messages) - 1, -1, -1):
            candidate = messages[position]
            if isinstance(candidate, AIMessage) and candidate.tool_calls:
                boundary = position
                break

        # Collect resolved error tool_call_ids.
        resolved: set[str] = set()
        for position, message in enumerate(messages):
            if (
                isinstance(message, ToolMessage)
                and getattr(message, "status", "success") == "error"
                and boundary is not None
                and position < boundary
            ):
                resolved.add(message.tool_call_id)

        result = []
        for position, message in enumerate(messages):
            if isinstance(message, ToolMessage) and message.tool_call_id in resolved:
                continue
            if (
                isinstance(message, AIMessage)
                and message.tool_calls
                and boundary is not None
                and position < boundary
            ):
                kept = [c for c in message.tool_calls if c["id"] not in resolved]
                if not kept:
                    continue
                if len(kept) < len(message.tool_calls):
                    result.append(message.model_copy(update={"tool_calls": kept}))
                    continue
            result.append(message)

        return {"llm_input_messages": result}


class TurnBudget:
    """Count only successful turns toward a fixed budget.

    Used as ``post_model_hook`` on ``create_react_agent``.  The hook
    inspects the ToolMessages that arrived since the last model call
    and increments the turn counter only when at least one tool call
    succeeded.  When the budget is exhausted and the model still
    requests tool calls, the hook replaces the response with a
    plain AIMessage so the agent terminates gracefully.

    A separate error budget caps all-error turns to prevent infinite
    error loops.
    """

    def __init__(self, turns: int, max_errors: int = 3) -> None:
        self._turns = turns
        self._errors = max_errors
        self.max_errors = max_errors

    def __call__(self, state: dict) -> dict:
        messages = state["messages"]

        responses: list[ToolMessage] = []
        for message in reversed(messages[:-1]):
            if isinstance(message, ToolMessage):
                responses.append(message)
            else:
                break

        if responses:
            success = any(
                getattr(message, "status", "success") != "error"
                for message in responses
            )
            if success:
                self._turns -= 1
            else:
                self._errors -= 1

        head = messages[-1]
        if (
            (self._turns <= 0 or self._errors <= 0)
            and isinstance(head, AIMessage)
            and head.tool_calls
        ):
            return {
                "messages": [
                    AIMessage(
                        id=head.id,
                        content="Turn budget exhausted. Return your best answer now.",
                    )
                ]
            }

        return {"messages": []}


class RepositorySeedNode:
    """Build the repository-native seed presented to the AgentSZZ search flow."""

    def __init__(
        self, repository: VersionControl | None = None, strategy: str = "diff"
    ) -> None:
        if strategy not in {"diff", "stat"}:
            raise ValueError("RepositorySeedNode strategy must be 'diff' or 'stat'.")
        self._repository = repository
        self._renderer = DiffRenderer()
        self._strategy = strategy

    def run(self, input: SeedInput) -> SeedOutput:
        correction = input.get("correction")
        if correction is None or self._repository is None:
            raise ValueError("RepositorySeedNode requires correction and repository.")

        base = correction["base"]
        after = correction["after"]
        seed: list[SeedItem] = [{"title": "Correction Commit", "body": after}]

        message = self._repository.meta(after).message.strip()
        if message:
            seed.append({"title": "Commit Message", "body": message})

        diff = self._repository.diff(base, after, context=3)
        if self._strategy == "diff":
            # Full fix diff plus a plain list of the modified files.
            patch = self._renderer.render(diff)
            if patch:
                seed.append({"title": "Fix Diff", "body": patch})
            operations = self._repository.manifest(base, after)
            files = self._files(operations)
            if files:
                seed.append({"title": "Modified Files", "body": "\n".join(files)})
        else:
            # No full diff: only the changed files with their churn. The agent
            # pulls any file's diff on demand via show/grep.
            body = "\n".join(
                f"{entry['path']}  +{entry['added']} -{entry['deleted']}"
                for entry in self._renderer.stat(diff)
            )
            if body:
                seed.append({"title": "Modified Files", "body": body})

        return {
            "base": base,
            "after": after,
            "seed": seed,
        }

    def _files(self, operations: list[FileOperation]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for operation in operations:
            path = operation.after or operation.base
            if path is None or path in seen:
                continue
            seen.add(path)
            result.append(path)
        return result


class RepositorySearchNode:
    """Run the AgentSZZ-style repository search flow over the prepared tools."""

    def __init__(
        self,
        tool: RepositoryTool | RepositoryToolFactory | None = None,
        llm: object | None = None,
    ) -> None:
        self._tool = tool
        self._llm = llm

    def run(
        self,
        input: Seed,
        *,
        steps: int = 30,
        retries: int = 0,
    ) -> SearchOutput:
        base = input.get("base")
        after = input.get("after")
        seed = input.get("seed")
        if steps < 1:
            raise ValueError("RepositorySearchNode steps must be positive.")
        if retries < 0:
            raise ValueError("RepositorySearchNode retries must be non-negative.")
        if (
            base is None
            or after is None
            or seed is None
            or self._tool is None
            or self._llm is None
        ):
            raise ValueError(
                "RepositorySearchNode requires base, after, seed, tool, and llm."
            )

        tool = self._tool
        if isinstance(tool, RepositoryToolFactory):
            tool = tool.create(base, after)

        graph, result, limit = self._invoke(seed, tool, steps=steps)
        output = SearchNormalizer().normalize(result)

        if retries == 0:
            return output

        # Retry: the tools guard exploration, but the final SHA can still be a
        # hallucinated or post-correction commit. Validate it and give the agent one
        # more attempt with the concrete reason, keeping its full search context.
        validator = IntroducerValidator(tool._repository, tool._after)
        for _ in range(retries):
            ok, feedback = validator.check(output["introducer"])
            if ok or not isinstance(result, dict):
                break
            messages = list(result["messages"])
            messages.append(HumanMessage(content=feedback))
            result = graph.invoke(
                {"messages": messages, "remaining_steps": limit},
                config={"recursion_limit": limit},
            )
            output = SearchNormalizer().normalize(result)
        return output

    def _invoke(
        self,
        seed: list[SeedItem],
        tool: RepositoryTool,
        *,
        steps: int,
    ) -> tuple[object, object, int]:
        tools = tool.tools()
        node = ToolNode(
            tools,
            handle_tool_errors=lambda error: str(error),
            wrap_tool_call=DuplicateCallGuard(),
        )
        prompt = inspect.cleandoc(
            """
            Identify the commit that first introduced the bug fixed by the given correction.
            The initial user message contains the correction SHA, its commit message, its
            full unified diff, and the list of modified files.

            Workflow:
            1. Start with blame on the lines the fix modified or removed at the parent of the
               correction commit. For omission-style fixes that only add lines, blame the
               surrounding context or the function signature instead.
            2. When blame returns multiple distinct commits, call show on at least the most
               frequent commit and the commit directly on the buggy line. If they are the
               same commit, one show call suffices.
            3. For each distinct candidate returned by blame, call show to classify it. Treat
               refactorings, cherry-picks, merges, formatting, and other non-semantic changes
               as transparent and re-blame at the candidate's parent to trace further back.
            4. If blame traces back to the repository's initial commit (a root commit with no
               parents), treat it as a valid bug-introducing commit. Do not continue searching
               for a newer commit unless contrary evidence is present.
            5. If a commit moves or adds an entire driver, subsystem, or module into the
               repository, treat that commit as the introducer rather than tracing further
               back into the original source.
            6. Before selecting a candidate, confirm a direct causal link to the buggy logic.
               A commit that merely exposes or triggers a pre-existing bug is not the
               introducer.

            Use pickaxe to find commits that added or removed a symbol. Without a path it
            searches repository-wide; with a path it follows the file's rename history. Use
            trace to track a specific entity's history via tree-sitter. Use grep to inspect
            the codebase at a snapshot. Do not exhaust the tool budget exploring alternatives
            once you have a confirmed candidate.

            Return ONLY the full 40-character introducer SHA. Once evidence points to a
            candidate, return it — uncertainty alone is not a reason to return null.
            Return null only if no evidence was found at all.
            """
        )
        budget = TurnBudget(steps, max_errors=5)
        # Each react-agent cycle traverses 4 graph nodes
        # (pre_model_hook, agent, post_model_hook, tools) plus a final
        # cycle of up to 4 nodes to reach generate_structured_response.
        limit = (steps + budget.max_errors + 1) * 4
        graph = create_react_agent(
            self._llm,
            tools=node,
            prompt=prompt,
            response_format=(
                "Return the final answer as a JSON object with an `introducer` "
                "field holding the full 40-character SHA, or null.",
                SearchResult,
            ),
            pre_model_hook=ResolvedErrorFilter(),
            post_model_hook=budget,
        )
        result = graph.invoke(
            {
                "messages": [HumanMessage(content=SearchPrompt().render(seed))],
                "remaining_steps": limit,
            },
            config={"recursion_limit": limit},
        )
        return graph, result, limit
