from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable, Iterable, Mapping, Protocol, Sequence

from benchmark.attribution.history import VersionControlSnapshot
from benchmark.attribution.knowledge import KnowledgeGraphStore
from benchmark.attribution.tracing import Trace


Line = tuple[str, str, int]


@dataclass(frozen=True)
class Metrics:
    """A generic bag of named values keyed by dotted paths like
    ``context.lines.correction.lineage``. The dotted-key convention mirrors the OTLP
    attribute namespace, so the same keys flow through factories,
    processors, and the exported trace without renaming."""

    values: Mapping[str, object]

    def __getitem__(self, key: str) -> object:
        return self.values[key]

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


class KnowledgeGraphMetrics:
    """Factory that builds context metrics from a knowledge graph and its
    snapshot. Partitions lines into ``correction`` (the fix diff itself),
    ``correction.lineage`` (candidate lines in files the fix touches,
    resolved through renames via graph aliases), and ``total``."""

    @staticmethod
    def build(
        snapshot: VersionControlSnapshot,
        context: Path,
        correction: str,
    ) -> Metrics:
        with open(context / f"{correction}.json", "r", encoding="utf-8") as file:
            graph = KnowledgeGraphStore.load(json.load(file))

        commits = frozenset(
            payload["sha"]
            for node in graph.nodes("commit")
            for payload in [graph.payload(node)]
            if payload.get("type") != "correction"
            and isinstance(payload.get("sha"), str)
        )

        lineage_paths: set[str] = set()
        for item in snapshot.manifest(graph.base, graph.after):
            path = item.after or item.base
            if path is None:
                continue
            for alias in graph.aliases(f"file:{path}"):
                lineage_paths.add(alias.removeprefix("file:"))

        lines_correction: set[Line] = set()
        lines_lineage: set[Line] = set()
        lines_cross: set[Line] = set()
        commits_lineage: set[str] = set()

        correction_parents = snapshot.parents(correction)
        if correction_parents:
            for patch in snapshot.diff(correction_parents[0], correction):
                path = patch.delta.new_file.path or patch.delta.old_file.path
                if path is None:
                    continue
                lines_correction.update(
                    KnowledgeGraphMetrics._lines(correction, path, patch)
                )

        for sha in commits:
            parents = snapshot.parents(sha)
            if not parents:
                continue
            has_lineage = False
            for patch in snapshot.diff(parents[0], sha):
                path = patch.delta.new_file.path or patch.delta.old_file.path
                if path is None:
                    continue
                lines = KnowledgeGraphMetrics._lines(sha, path, patch)
                if not lines:
                    continue
                if path in lineage_paths:
                    lines_lineage.update(lines)
                    has_lineage = True
                else:
                    lines_cross.update(lines)
            if has_lineage:
                commits_lineage.add(sha)

        return Metrics({
            "context.commits": commits,
            "context.commits.correction": frozenset({correction}),
            "context.commits.lineage": frozenset(commits_lineage),
            "context.lines.correction": frozenset(lines_correction),
            "context.lines.lineage": frozenset(lines_lineage),
            "context.lines.cross": frozenset(lines_cross),
        })

    @staticmethod
    def _lines(sha: str, path: str, patch: object) -> frozenset[Line]:
        index = 0
        result: set[Line] = set()
        for hunk in patch.hunks:
            for line in hunk.lines:
                if line.origin in {"+", "-"}:
                    index += 1
                    result.add((sha, path, index))
        return frozenset(result)


@dataclass
class Span:
    """A single unit of work inside a trace, reconstructed by pairing the
    matching start and end events of one node, tool, or LLM call. Carries the
    timing, status, inputs, output, and any attributes that processors have
    attached to it."""

    id: str
    parent: str | None
    kind: str
    name: str | None
    start: datetime
    end: datetime | None
    status: str
    error: str | None
    inputs: object
    output: object
    payload: Mapping[str, object]
    messages: Mapping[str, object] = field(default_factory=dict)
    attributes: dict[str, object] = field(default_factory=dict)


class TraceProcessor(Protocol):
    """Plug-in that inspects a reconstructed span and returns additional
    attributes to attach to it. Serializers run every registered processor
    over every span, so processors are the hook for turning raw trace data
    into domain metrics without changing the serializer itself."""

    def process(self, span: Span) -> Mapping[str, object]: ...


class AttributionTraceProcessor:
    """Base class for processors that annotate tool spans with attribution
    context metrics. Subclasses implement ``_shas`` and ``_lines`` to
    extract commits and lines from the tool-specific payloads."""

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics
        all_lines = (
            set(metrics.get("context.lines.correction") or ())
            | set(metrics.get("context.lines.lineage") or ())
            | set(metrics.get("context.lines.cross") or ())
        )
        self._by_commit: dict[str, set[Line]] = {}
        self._by_path: dict[str, dict[str, set[Line]]] = {}
        for line in all_lines:
            sha, path, _ = line
            self._by_commit.setdefault(sha, set()).add(line)
            self._by_path.setdefault(sha, {}).setdefault(path, set()).add(line)
        self._seen_commits: set[str] = set()
        self._seen_lines: set[Line] = set()

    def process(self, span: Span) -> Mapping[str, object]:
        if span.kind != "tool":
            return {}
        if not isinstance(span.name, str):
            return {}
        context_commits = (
            self._metrics["context.commits"]
            | self._metrics["context.commits.correction"]
        )
        correction_lines = self._metrics["context.lines.correction"]
        lineage_lines = self._metrics["context.lines.lineage"]
        cross_lines = self._metrics["context.lines.cross"]
        attributes: dict[str, object] = {}
        shas = self._shas(span.name, span.inputs, span.output)
        if shas is not None:
            new_shas = shas - self._seen_commits
            self._seen_commits.update(shas)
            attributes["attribution.commits"] = len(new_shas)
            attributes["attribution.commits.total"] = len(shas)
            attributes["attribution.context.meta"] = sum(1 for sha in new_shas if sha in context_commits)
        lines = self._lines(span.name, span.inputs)
        if lines is not None:
            new_lines = lines - self._seen_lines
            self._seen_lines.update(lines)
            context_lines = new_lines & (correction_lines | lineage_lines | cross_lines)
            correction = len(new_lines & correction_lines)
            lineage = len(new_lines & lineage_lines)
            cross = len(new_lines & cross_lines)
            attributes["attribution.context.lines"] = len(context_lines)
            attributes["attribution.context.lines.total"] = len(lines & (correction_lines | lineage_lines | cross_lines))
            attributes["attribution.context.lines.correction"] = correction
            attributes["attribution.context.lines.lineage"] = lineage
            attributes["attribution.context.lines.cross"] = cross
        return attributes

    def _shas(
        self,
        name: str,
        inputs: object,
        output: object,
    ) -> set[str] | None:
        return None

    def _lines(
        self,
        name: str,
        inputs: object,
    ) -> frozenset[Line] | None:
        return None

    def _content(self, output: object) -> object:
        if isinstance(output, dict):
            content = output.get("content")
            if isinstance(content, (dict, list)):
                return content
            if isinstance(content, str):
                try:
                    return json.loads(content)
                except (TypeError, ValueError):
                    return None
            return None
        return None

    def _lines_for_sha(
        self, sha: str, path: str | None,
    ) -> frozenset[Line] | None:
        if path is None:
            bucket = self._by_commit.get(sha)
            return frozenset(bucket) if bucket is not None else None
        paths = self._by_path.get(sha)
        if paths is None:
            return None
        bucket = paths.get(str(path))
        return frozenset(bucket) if bucket is not None else None


class RepositoryTraceProcessor(AttributionTraceProcessor):
    """Handles the repository-native tools: ``show``, ``blame``,
    ``pickaxe``, ``trace``, and ``grep``."""

    def _shas(
        self,
        name: str,
        inputs: object,
        output: object,
    ) -> set[str] | None:
        data = self._content(output)
        if name == "show":
            sha = data.get("sha") if isinstance(data, dict) else None
            return {sha} if isinstance(sha, str) else set()
        if name == "grep":
            commit = inputs.get("commit") if isinstance(inputs, dict) else None
            if not isinstance(commit, str):
                return set()
            hits = data if isinstance(data, list) else []
            return {commit} if hits else set()
        if name == "blame":
            if not isinstance(data, dict):
                return set()
            commits = data.get("commits")
            if isinstance(commits, dict):
                return {sha for sha in commits if isinstance(sha, str)}
            return set()
        if name in {"pickaxe", "trace"}:
            if not isinstance(data, list):
                return set()
            return {
                row["sha"]
                for row in data
                if isinstance(row, dict) and isinstance(row.get("sha"), str)
            }
        return None

    def _lines(
        self,
        name: str,
        inputs: object,
    ) -> frozenset[Line] | None:
        if name != "show":
            return None
        if not isinstance(inputs, dict):
            return None
        sha = inputs.get("commit")
        if not isinstance(sha, str):
            return None
        return self._lines_for_sha(sha, inputs.get("path"))


class KnowledgeGraphTraceProcessor(AttributionTraceProcessor):
    """Handles the knowledge-graph tools: ``candidates``, ``neighbors``,
    ``meta``, and ``content``."""

    def _shas(
        self,
        name: str,
        inputs: object,
        output: object,
    ) -> set[str] | None:
        data = self._content(output)
        if name in {"candidates", "neighbors"}:
            if not isinstance(data, list):
                return set()
            return {
                row["sha"]
                for row in data
                if isinstance(row, dict) and isinstance(row.get("sha"), str)
            }
        if name == "meta":
            if not isinstance(data, dict):
                return set()
            sha = data.get("sha")
            return {sha} if isinstance(sha, str) else set()
        if name == "content":
            if not isinstance(data, dict):
                return set()
            sha = data.get("sha")
            return {sha} if isinstance(sha, str) else set()
        return None

    def _lines(
        self,
        name: str,
        inputs: object,
    ) -> frozenset[Line] | None:
        if name != "content":
            return None
        if not isinstance(inputs, dict):
            return None
        sha = inputs.get("sha")
        if not isinstance(sha, str):
            return None
        return self._lines_for_sha(sha, inputs.get("path"))


class MessageProcessor:
    """Emits the LLM conversation for each span: the new input messages
    since the previous call and the assistant's response. Messages that
    were already present in an earlier span are skipped so the exported
    trace stays compact."""

    def __init__(self) -> None:
        self._seen = 0

    def process(self, span: Span) -> Mapping[str, object]:
        if span.kind != "llm":
            return {}
        attributes: dict[str, object] = {}
        messages = self._messages(span.messages)
        new = messages[self._seen:]
        self._seen = len(messages)
        if new:
            attributes["llm.messages"] = "\n\n".join(
                f"{m.get('type', '?').upper()}: {m.get('content') or ''}" for m in new
            )
        generation = self._generation(span.payload)
        if generation is not None:
            attributes["llm.generation"] = generation
        return attributes

    def _messages(self, payload: Mapping[str, object]) -> list[dict[str, object]]:
        raw = payload.get("messages") or []
        if not isinstance(raw, list):
            return []
        batch = raw[0] if raw and isinstance(raw[0], list) else raw
        return [m for m in batch if isinstance(m, dict)]

    def _generation(self, payload: Mapping[str, object]) -> str | None:
        for batch in payload.get("generations") or []:
            if not isinstance(batch, list):
                continue
            for item in batch:
                if not isinstance(item, dict):
                    continue
                parts: list[str] = []
                text = item.get("text")
                if text:
                    parts.append(str(text))
                msg = item.get("message")
                if isinstance(msg, dict):
                    for call in msg.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        name = call.get("name", "?")
                        args = json.dumps(call.get("args", {}))
                        parts.append(f"-> {name}({args})")
                if parts:
                    return "\n".join(parts)
        return None


class TokenProcessor:
    """Extracts prompt, completion, cached, and reasoning token counts from
    an LLM span's payload and exposes them as ``llm.tokens.input``,
    ``llm.tokens.output``, ``llm.tokens.cached``, and
    ``llm.tokens.reasoning`` attributes. Handles the shape differences
    between LangChain's ``token_usage`` summaries and the per-generation
    ``usage_metadata`` format so callers do not have to care which one the
    underlying model returned."""

    def process(self, span: Span) -> Mapping[str, object]:
        if span.kind != "llm":
            return {}
        usage = self._usage(span.payload)
        if usage is None:
            return {}
        return {
            "llm.tokens.input": usage.get("input", 0),
            "llm.tokens.output": usage.get("output", 0),
            "llm.tokens.cached": usage.get("cached", 0),
            "llm.tokens.reasoning": usage.get("reasoning", 0),
        }

    def _usage(self, payload: Mapping[str, object]) -> Mapping[str, int] | None:
        output = payload.get("llm_output")
        if isinstance(output, dict):
            usage = output.get("token_usage")
            if isinstance(usage, dict):
                values = self._values(usage)
                if values is not None:
                    return values
        generations = payload.get("generations")
        if not isinstance(generations, list):
            return None
        for batch in generations:
            if not isinstance(batch, list):
                continue
            for item in batch:
                if not isinstance(item, dict):
                    continue
                message = item.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage_metadata")
                if not isinstance(usage, dict):
                    continue
                values = self._values(usage)
                if values is not None:
                    return values
        return None

    def _values(self, usage: Mapping[str, object]) -> Mapping[str, int] | None:
        prompt = self._pick(usage, "prompt_tokens", "input_tokens")
        completion = self._pick(usage, "completion_tokens", "output_tokens")
        if prompt is None and completion is None:
            return None
        return {
            "input": prompt or 0,
            "output": completion or 0,
            "cached": self._cached(usage),
            "reasoning": self._reasoning(usage),
        }

    def _pick(self, usage: Mapping[str, object], *keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return None

    def _cached(self, usage: Mapping[str, object]) -> int:
        for key in ("cached_tokens", "cache_read_input_tokens", "prompt_cache_hit_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                return value
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            value = details.get("cached_tokens")
            if isinstance(value, int):
                return value
        details = usage.get("input_token_details")
        if isinstance(details, dict):
            for key in ("cache_read", "cached"):
                value = details.get(key)
                if isinstance(value, int):
                    return value
        return 0

    def _reasoning(self, usage: Mapping[str, object]) -> int:
        for key in ("reasoning_tokens",):
            value = usage.get(key)
            if isinstance(value, int):
                return value
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            value = details.get("reasoning_tokens")
            if isinstance(value, int):
                return value
        details = usage.get("output_token_details")
        if isinstance(details, dict):
            for key in ("reasoning", "reasoning_tokens"):
                value = details.get(key)
                if isinstance(value, int):
                    return value
        return 0


class TraceMonitor:
    """Hook for ``ValidationExecutor`` that emits one-line summaries per
    trace event. Pass ``print`` as sink for notebook use or
    ``context.log.info`` for Dagster assets."""

    def __init__(self, sink: Callable[[str], None] = print) -> None:
        self._sink = sink
        self._turn = 0
        self._total_input = 0
        self._total_output = 0
        self._total_cached = 0
        self._total_reasoning = 0

    def __call__(self, envelope: dict[str, object]) -> None:
        event = envelope["event"]
        if not isinstance(event, dict):
            return
        kind = str(event.get("event", ""))
        node = str(event.get("node") or "")
        raw = event.get("payload")
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not isinstance(payload, dict):
            return

        if kind == "llm_end":
            self._on_llm_end(payload)
        elif kind == "tool_end":
            self._on_tool_end(node, payload)
        elif kind in {"llm_error", "tool_error"}:
            label = "LLM" if kind == "llm_error" else f"TOOL {node}"
            self._sink(f"Error #{self._turn} | {label}: {payload.get('error_type')}: {payload.get('error')}")
        elif kind == "node_end" and node == "LangGraph" and not event.get("parent"):
            self._sink(
                f"Final | turns={self._turn}"
                f" input={self._total_input}"
                f" output={self._total_output}"
                f" cached={self._total_cached}"
                f" reasoning={self._total_reasoning}"
            )

    def _on_llm_end(self, payload: dict[str, object]) -> None:
        self._turn += 1
        tokens = self._tokens(payload)
        self._total_input += tokens.get("input", 0)
        self._total_output += tokens.get("output", 0)
        self._total_cached += tokens.get("cached", 0)
        self._total_reasoning += tokens.get("reasoning", 0)
        self._sink(
            f"Thinking #{self._turn}"
            f" | input={tokens.get('input', 0)}"
            f" output={tokens.get('output', 0)}"
            f" cached={tokens.get('cached', 0)}"
            f" reasoning={tokens.get('reasoning', 0)}"
        )
        for batch in payload.get("generations") or []:
            for g in (batch if isinstance(batch, list) else [batch]):
                if not isinstance(g, dict):
                    continue
                for call in (g.get("message") or {}).get("tool_calls") or []:
                    args = json.dumps(call.get("args", {}))
                    if len(args) > 80:
                        args = args[:80] + "..."
                    self._sink(f"  → {call.get('name')}({args})")

    def _on_tool_end(self, node: str, payload: dict[str, object]) -> None:
        result = (payload.get("output") or {}).get("content")
        if isinstance(result, (dict, list)):
            result = json.dumps(result)
        if isinstance(result, str) and len(result) > 80:
            result = result[:80] + "..."
        self._sink(f"  Calling #{self._turn} | tool={node} result={result}")

    def _pick(self, usage: Mapping[str, object], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return 0

    def _cached(self, usage: Mapping[str, object]) -> int:
        for key in ("cached_tokens", "cache_read_input_tokens", "prompt_cache_hit_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                return value
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            value = details.get("cached_tokens")
            if isinstance(value, int):
                return value
        details = usage.get("input_token_details")
        if isinstance(details, dict):
            for key in ("cache_read", "cached"):
                value = details.get(key)
                if isinstance(value, int):
                    return value
        return 0

    def _reasoning(self, usage: Mapping[str, object]) -> int:
        for key in ("reasoning_tokens",):
            value = usage.get(key)
            if isinstance(value, int):
                return value
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            value = details.get("reasoning_tokens")
            if isinstance(value, int):
                return value
        details = usage.get("output_token_details")
        if isinstance(details, dict):
            for key in ("reasoning", "reasoning_tokens"):
                value = details.get(key)
                if isinstance(value, int):
                    return value
        return 0

    def _tokens(self, payload: dict[str, object]) -> dict[str, int]:
        output = payload.get("llm_output")
        if isinstance(output, dict):
            usage = output.get("token_usage")
            if isinstance(usage, dict):
                return {
                    "input": self._pick(usage, "prompt_tokens", "input_tokens"),
                    "output": self._pick(usage, "completion_tokens", "output_tokens"),
                    "cached": self._cached(usage),
                    "reasoning": self._reasoning(usage),
                }
        for batch in payload.get("generations") or []:
            for g in (batch if isinstance(batch, list) else [batch]):
                if not isinstance(g, dict):
                    continue
                usage = (g.get("message") or {}).get("usage_metadata")
                if isinstance(usage, dict):
                    return {
                        "input": self._pick(usage, "prompt_tokens", "input_tokens"),
                        "output": self._pick(usage, "completion_tokens", "output_tokens"),
                        "cached": self._cached(usage),
                        "reasoning": self._reasoning(usage),
                    }
        return {}


class TraceSerializer:
    """Writes a trace to a stream as an OTLP-JSON document so it can be
    opened in any off-the-shelf OpenTelemetry viewer. Pairs start and end
    events into spans, runs every registered processor to enrich them, and
    streams the result directly to the output without materializing the
    whole document in memory."""

    def __init__(self, processors: Sequence[TraceProcessor] = ()) -> None:
        self._processors = tuple(processors)

    def serialize(
        self,
        trace: Trace,
        out: IO[str],
        *,
        resource: Mapping[str, object] | None = None,
    ) -> None:
        spans = self._spans(trace)
        trace_id = self._trace_id(trace.id)
        writer = _JsonWriter(out)
        writer.open_object()
        writer.field("resourceSpans")
        writer.open_array()
        writer.open_object()
        writer.field("resource")
        writer.write_value(self._resource(trace, resource))
        writer.field("scopeSpans")
        writer.open_array()
        writer.open_object()
        writer.field("scope")
        writer.write_value({"name": "benchmark.attribution"})
        writer.field("spans")
        writer.open_array()
        for span in spans:
            self._enrich(span)
            writer.write_item(self._otlp(span, trace_id))
        writer.close_array()
        writer.close_object()
        writer.close_array()
        writer.close_object()
        writer.close_array()
        writer.close_object()

    def _spans(self, trace: Trace) -> Iterable[Span]:
        pending: dict[str, dict[str, object]] = {}
        result: list[Span] = []
        for event in trace.events():
            kind = str(event["event"])
            span = str(event["span"])
            payload = json.loads(event["payload"])
            if kind.endswith("_start"):
                pending[span] = {
                    "id": span,
                    "parent": event["parent"],
                    "kind": self._kind(kind),
                    "name": event["node"],
                    "start": event["timestamp"],
                    "inputs": payload.get("inputs"),
                    "messages": payload,
                }
                continue
            if kind.endswith("_end") or kind.endswith("_error"):
                start = pending.pop(span, None)
                status = "error" if kind.endswith("_error") else "ok"
                error = None
                if status == "error":
                    error = f"{payload.get('error_type')}: {payload.get('error')}"
                output = payload.get("output") if status == "ok" else None
                if start is None:
                    result.append(
                        Span(
                            id=span,
                            parent=event["parent"],
                            kind=self._kind(kind),
                            name=event["node"],
                            start=event["timestamp"],
                            end=event["timestamp"],
                            status=status,
                            error=error,
                            inputs=None,
                            output=output,
                            payload=payload,
                        )
                    )
                    continue
                result.append(
                    Span(
                        id=str(start["id"]),
                        parent=start["parent"] if start["parent"] is not None else None,  # type: ignore[arg-type]
                        kind=str(start["kind"]),
                        name=start["name"] if start["name"] is None or isinstance(start["name"], str) else None,  # type: ignore[arg-type]
                        start=start["start"],  # type: ignore[arg-type]
                        end=event["timestamp"],
                        status=status,
                        error=error,
                        inputs=start["inputs"],
                        output=output,
                        payload=payload,
                        messages=start.get("messages") or {},
                    )
                )
        return result

    def _enrich(self, span: Span) -> None:
        for processor in self._processors:
            attributes = processor.process(span)
            for key, value in attributes.items():
                if key in span.attributes:
                    raise ValueError(
                        f"Attribute collision on span {span.id!r}: {key!r}"
                    )
                span.attributes[key] = value

    def _kind(self, event: str) -> str:
        return event.rsplit("_", 1)[0]

    def _resource(
        self,
        trace: Trace,
        resource: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        attributes = dict(trace.metadata)
        if resource is not None:
            attributes.update(resource)
        return {"attributes": self._attributes(attributes)}

    def _otlp(self, span: Span, trace_id: str) -> Mapping[str, object]:
        attributes = dict(span.attributes)
        attributes.setdefault("span.kind", span.kind)
        if span.name is not None:
            attributes.setdefault("node.name", span.name)
        if span.inputs is not None:
            self._flatten("tool.input", span.inputs, attributes)
        end = span.end or span.start
        status = {"code": "STATUS_CODE_OK" if span.status == "ok" else "STATUS_CODE_ERROR"}
        if span.error is not None:
            status["message"] = span.error
        return {
            "traceId": trace_id,
            "spanId": self._span_id(span.id),
            "parentSpanId": self._span_id(span.parent) if span.parent else "",
            "name": self._name(span),
            "kind": "SPAN_KIND_INTERNAL",
            "startTimeUnixNano": self._nanos(span.start),
            "endTimeUnixNano": self._nanos(end),
            "attributes": self._attributes(attributes),
            "status": status,
        }

    def _name(self, span: Span) -> str:
        if span.kind == "tool" and isinstance(span.name, str):
            return f"tool:{span.name}"
        if span.kind == "llm" and isinstance(span.name, str):
            return f"llm:{span.name}"
        if span.kind == "node" and isinstance(span.name, str):
            return f"node:{span.name}"
        return span.kind

    def _flatten(
        self,
        prefix: str,
        value: object,
        target: dict[str, object],
    ) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                self._flatten(f"{prefix}.{key}", nested, target)
            return
        target[prefix] = value

    def _attributes(self, attributes: Mapping[str, object]) -> list[Mapping[str, object]]:
        return [
            {"key": key, "value": self._any_value(value)}
            for key, value in attributes.items()
        ]

    def _any_value(self, value: object) -> Mapping[str, object]:
        if isinstance(value, bool):
            return {"boolValue": value}
        if isinstance(value, int):
            return {"intValue": str(value)}
        if isinstance(value, float):
            return {"doubleValue": value}
        if isinstance(value, str):
            return {"stringValue": value}
        if isinstance(value, (list, tuple)):
            return {
                "arrayValue": {
                    "values": [self._any_value(item) for item in value]
                }
            }
        return {"stringValue": json.dumps(value, default=str, sort_keys=True)}

    def _nanos(self, value: datetime | object) -> str:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return str(int(moment.timestamp() * 1_000_000_000))

    def _trace_id(self, seed: str) -> str:
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    def _span_id(self, seed: str) -> str:
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


class _JsonWriter:
    """Minimal streaming JSON writer used by the serializer to emit deeply
    nested documents one token at a time. Tracks whether the current
    container is an object or an array and whether the next entry needs a
    leading comma, so the serializer can interleave structural tokens with
    pre-serialized values without buffering the whole output."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._stack: list[str] = []
        self._first: list[bool] = []

    def open_object(self) -> None:
        self._prefix_item()
        self._stream.write("{")
        self._stack.append("object")
        self._first.append(True)

    def close_object(self) -> None:
        self._stack.pop()
        self._first.pop()
        self._stream.write("}")

    def open_array(self) -> None:
        self._prefix_item()
        self._stream.write("[")
        self._stack.append("array")
        self._first.append(True)

    def close_array(self) -> None:
        self._stack.pop()
        self._first.pop()
        self._stream.write("]")

    def field(self, name: str) -> None:
        if not self._first[-1]:
            self._stream.write(",")
        self._first[-1] = False
        self._stream.write(json.dumps(name))
        self._stream.write(":")

    def write_value(self, value: object) -> None:
        self._stream.write(json.dumps(value, default=str))

    def write_item(self, value: object) -> None:
        self._prefix_item()
        self._stream.write(json.dumps(value, default=str))

    def _prefix_item(self) -> None:
        if not self._stack:
            return
        if self._stack[-1] != "array":
            return
        if not self._first[-1]:
            self._stream.write(",")
        self._first[-1] = False
