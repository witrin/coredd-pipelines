from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone

from benchmark.attribution.evaluation import (
    KnowledgeGraphTraceProcessor,
    MessageProcessor,
    Metrics,
    RepositoryTraceProcessor,
    Span,
    TraceMonitor,
    TokenProcessor,
    TraceProcessor,
    TraceSerializer,
)
from benchmark.tracing import Trace


class TestTraceSerializer(unittest.TestCase):
    def test_pairs_start_end_into_span(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="bar",
            span="span-1",
            parent=None,
            event="node_start",
            payload={},
        )
        trace.append(
            node="bar",
            span="span-1",
            parent=None,
            event="node_end",
            payload={},
        )

        document = _serialize(TraceSerializer(), trace)

        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["name"], "node:bar")
        self.assertEqual(spans[0]["status"]["code"], "STATUS_CODE_OK")

    def test_tool_inputs_flattened(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"sha": "abc", "path": "a.py"}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": "ok"}},
        )

        document = _serialize(TraceSerializer(), trace)

        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
        attributes = _attributes(spans[0])
        self.assertEqual(attributes["tool.input.sha"]["stringValue"], "abc")
        self.assertEqual(attributes["tool.input.path"]["stringValue"], "a.py")

    def test_none_inputs_omitted(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"sha": "abc", "path": None}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": "ok"}},
        )

        document = _serialize(TraceSerializer(), trace)

        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )
        self.assertEqual(attributes["tool.input.sha"]["stringValue"], "abc")
        self.assertNotIn("tool.input.path", attributes)

    def test_llm_tokens_extracted(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="agent",
            span="span-1",
            parent=None,
            event="llm_start",
            payload={},
        )
        trace.append(
            node="agent",
            span="span-1",
            parent=None,
            event="llm_end",
            payload={
                "llm_output": {
                    "token_usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "cached_tokens": 10,
                        "reasoning_tokens": 7,
                    }
                }
            },
        )

        document = _serialize(
            TraceSerializer(processors=[TokenProcessor()]),
            trace,
        )

        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )
        self.assertEqual(attributes["llm.tokens.input"]["intValue"], "100")
        self.assertEqual(attributes["llm.tokens.output"]["intValue"], "50")
        self.assertEqual(attributes["llm.tokens.cached"]["intValue"], "10")
        self.assertEqual(attributes["llm.tokens.reasoning"]["intValue"], "7")

    def test_tool_error_sets_error_status(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_error",
            payload={"error_type": "KeyError", "error": "missing"},
        )

        document = _serialize(TraceSerializer(), trace)

        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(spans[0]["status"]["code"], "STATUS_CODE_ERROR")
        self.assertIn("missing", spans[0]["status"]["message"])

    def test_resource_attributes_include_trace_metadata(self) -> None:
        trace = Trace(seed="foo", metadata={"repository": "baz"})
        document = _serialize(TraceSerializer(), trace, resource={"correction": "qux"})

        attributes = {
            entry["key"]: entry["value"]
            for entry in document["resourceSpans"][0]["resource"]["attributes"]
        }
        self.assertEqual(attributes["repository"]["stringValue"], "baz")
        self.assertEqual(attributes["correction"]["stringValue"], "qux")

    def test_processor_adds_attributes(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"sha": "deadbeef"}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": "{}"}},
        )

        document = _serialize(
            TraceSerializer(processors=[_FixedProcessor({"attribution.foo": "bar"})]),
            trace,
        )

        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )
        self.assertEqual(attributes["attribution.foo"]["stringValue"], "bar")

    def test_processor_collision_raises(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {}},
        )

        serializer = TraceSerializer(
            processors=[
                _FixedProcessor({"attribution.foo": 1}),
                _FixedProcessor({"attribution.foo": 2}),
            ]
        )
        buffer = io.StringIO()
        with self.assertRaises(ValueError):
            serializer.serialize(trace, buffer)


class TestMessageProcessor(unittest.TestCase):
    def test_first_call_emits_all_messages(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="agent",
            span="span-1",
            parent=None,
            event="llm_start",
            payload={"messages": [
                [
                    {"type": "system", "content": "you are helpful"},
                    {"type": "human", "content": "find the bug"},
                ],
            ]},
        )
        trace.append(
            node="agent",
            span="span-1",
            parent=None,
            event="llm_end",
            payload={"generations": [[{
                "text": "looking...",
                "message": {"tool_calls": [{"name": "show", "args": {"sha": "abc"}}]},
            }]]},
        )

        document = _serialize(
            TraceSerializer(processors=[MessageProcessor()]),
            trace,
        )
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        messages = attributes["llm.messages"]["stringValue"]
        self.assertIn("SYSTEM: you are helpful", messages)
        self.assertIn("HUMAN: find the bug", messages)
        generation = attributes["llm.generation"]["stringValue"]
        self.assertIn("looking...", generation)
        self.assertIn('-> show({"sha": "abc"})', generation)

    def test_second_call_emits_only_new_messages(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="agent",
            span="span-1",
            parent=None,
            event="llm_start",
            payload={"messages": [[
                {"type": "system", "content": "you are helpful"},
                {"type": "human", "content": "find the bug"},
            ]]},
        )
        trace.append(
            node="agent",
            span="span-1",
            parent=None,
            event="llm_end",
            payload={"generations": [[{"text": "step one"}]]},
        )
        trace.append(
            node="agent",
            span="span-2",
            parent=None,
            event="llm_start",
            payload={"messages": [[
                {"type": "system", "content": "you are helpful"},
                {"type": "human", "content": "find the bug"},
                {"type": "ai", "content": "step one"},
                {"type": "tool", "content": "result of show"},
            ]]},
        )
        trace.append(
            node="agent",
            span="span-2",
            parent=None,
            event="llm_end",
            payload={"generations": [[{"text": "step two"}]]},
        )

        processor = MessageProcessor()
        document = _serialize(
            TraceSerializer(processors=[processor]),
            trace,
        )
        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]

        first = _attributes(spans[0])["llm.messages"]["stringValue"]
        self.assertIn("SYSTEM:", first)
        self.assertIn("HUMAN:", first)
        self.assertNotIn("AI:", first)

        second = _attributes(spans[1])["llm.messages"]["stringValue"]
        self.assertIn("AI: step one", second)
        self.assertIn("TOOL: result of show", second)
        self.assertNotIn("SYSTEM:", second)

    def test_skips_non_llm_spans(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {}},
        )

        document = _serialize(
            TraceSerializer(processors=[MessageProcessor()]),
            trace,
        )
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )
        self.assertNotIn("llm.messages", attributes)


class TestTraceMonitor(unittest.TestCase):
    def test_llm_output_reads_nested_cached_tokens(self) -> None:
        lines: list[str] = []
        monitor = TraceMonitor(sink=lines.append)

        monitor({
            "event": {
                "event": "llm_end",
                "node": "agent",
                "payload": {
                    "llm_output": {
                        "token_usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 50,
                            "reasoning_tokens": 7,
                            "prompt_tokens_details": {
                                "cached_tokens": 10,
                            },
                        }
                    }
                },
            }
        })
        monitor({
            "event": {
                "event": "node_end",
                "node": "LangGraph",
                "parent": None,
                "payload": {},
            }
        })

        self.assertEqual(lines[0], "Thinking #1 | input=100 output=50 cached=10 reasoning=7")
        self.assertEqual(lines[1], "Final | turns=1 input=100 output=50 cached=10 reasoning=7")

    def test_generation_usage_reads_nested_cached_tokens(self) -> None:
        lines: list[str] = []
        monitor = TraceMonitor(sink=lines.append)

        monitor({
            "event": {
                "event": "llm_end",
                "node": "agent",
                "payload": {
                    "generations": [[{
                        "message": {
                            "usage_metadata": {
                                "input_tokens": 80,
                                "output_tokens": 20,
                                "output_token_details": {
                                    "reasoning": 5,
                                },
                                "input_token_details": {
                                    "cache_read": 12,
                                },
                            }
                        }
                    }]],
                },
            }
        })

        self.assertEqual(lines[0], "Thinking #1 | input=80 output=20 cached=12 reasoning=5")


class TestRepositoryTraceProcessor(unittest.TestCase):
    def test_show_candidate_reports_meta_and_lines(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"commit": "foo"}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": json.dumps({"sha": "foo"})}},
        )

        processor = RepositoryTraceProcessor(Metrics({
            "context.commits": {"foo"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": {("foo", "a.py", 1)},
            "context.lines.lineage": {("foo", "a.py", 2)},
            "context.lines.cross": {("foo", "b.py", 3)},
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        self.assertEqual(attributes["attribution.commits"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.meta"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.lines"]["intValue"], "3")
        self.assertEqual(attributes["attribution.context.lines.correction"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.lines.lineage"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.lines.cross"]["intValue"], "1")

    def test_blame_reports_meta_counts_only(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="blame",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"path": "a.py"}},
        )
        trace.append(
            node="blame",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={
                "output": {
                    "content": json.dumps(
                        {"commits": {"foo": {}, "bar": {}, "baz": {}}}
                    )
                }
            },
        )

        processor = RepositoryTraceProcessor(Metrics({
            "context.commits": {"foo"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": set(),
            "context.lines.lineage": set(),
            "context.lines.cross": set(),
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        self.assertEqual(attributes["attribution.commits"]["intValue"], "3")
        self.assertEqual(attributes["attribution.context.meta"]["intValue"], "1")
        self.assertNotIn("attribution.context.lines", attributes)

    def test_repeated_commit_deduplicates(self) -> None:
        trace = Trace(seed="foo")
        for span_id in ("span-1", "span-2"):
            trace.append(
                node="show",
                span=span_id,
                parent=None,
                event="tool_start",
                payload={"inputs": {"commit": "foo"}},
            )
            trace.append(
                node="show",
                span=span_id,
                parent=None,
                event="tool_end",
                payload={"output": {"content": json.dumps({"sha": "foo"})}},
            )

        processor = RepositoryTraceProcessor(Metrics({
            "context.commits": {"foo"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": {("foo", "a.py", 1)},
            "context.lines.lineage": set(),
            "context.lines.cross": set(),
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]

        first = _attributes(spans[0])
        self.assertEqual(first["attribution.commits"]["intValue"], "1")
        self.assertEqual(first["attribution.commits.total"]["intValue"], "1")
        self.assertEqual(first["attribution.context.lines"]["intValue"], "1")
        self.assertEqual(first["attribution.context.lines.total"]["intValue"], "1")

        second = _attributes(spans[1])
        self.assertEqual(second["attribution.commits"]["intValue"], "0")
        self.assertEqual(second["attribution.commits.total"]["intValue"], "1")
        self.assertEqual(second["attribution.context.lines"]["intValue"], "0")
        self.assertEqual(second["attribution.context.lines.total"]["intValue"], "1")

    def test_show_unknown_sha_has_no_lines_attributes(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"commit": "unknown"}},
        )
        trace.append(
            node="show",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": json.dumps({"sha": "unknown"})}},
        )

        processor = RepositoryTraceProcessor(Metrics({
            "context.commits": {"foo"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": set(),
            "context.lines.lineage": set(),
            "context.lines.cross": {("foo", "a.py", 1)},
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        self.assertEqual(attributes["attribution.commits"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.meta"]["intValue"], "0")
        self.assertNotIn("attribution.context.lines", attributes)


class TestKnowledgeGraphTraceProcessor(unittest.TestCase):
    def test_candidates_reports_commits(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="candidates",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"limit": 20}},
        )
        trace.append(
            node="candidates",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": json.dumps([
                {"sha": "aaa"}, {"sha": "bbb"}, {"sha": "ccc"},
            ])}},
        )

        processor = KnowledgeGraphTraceProcessor(Metrics({
            "context.commits": {"aaa", "bbb"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": set(),
            "context.lines.lineage": set(),
            "context.lines.cross": set(),
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        self.assertEqual(attributes["attribution.commits"]["intValue"], "3")
        self.assertEqual(attributes["attribution.context.meta"]["intValue"], "2")
        self.assertNotIn("attribution.context.lines", attributes)

    def test_content_reports_commits_and_lines(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="content",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"sha": "aaa"}},
        )
        trace.append(
            node="content",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": json.dumps({
                "sha": "aaa", "patch": "...",
            })}},
        )

        processor = KnowledgeGraphTraceProcessor(Metrics({
            "context.commits": {"aaa"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": set(),
            "context.lines.lineage": {("aaa", "a.py", 1), ("aaa", "a.py", 2)},
            "context.lines.cross": {("aaa", "b.py", 3)},
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        self.assertEqual(attributes["attribution.commits"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.meta"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.lines"]["intValue"], "3")
        self.assertEqual(attributes["attribution.context.lines.lineage"]["intValue"], "2")
        self.assertEqual(attributes["attribution.context.lines.cross"]["intValue"], "1")

    def test_neighbors_reports_commits(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="neighbors",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"sha": "aaa"}},
        )
        trace.append(
            node="neighbors",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": json.dumps([
                {"sha": "bbb"}, {"sha": "ccc"},
            ])}},
        )

        processor = KnowledgeGraphTraceProcessor(Metrics({
            "context.commits": {"bbb"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": set(),
            "context.lines.lineage": set(),
            "context.lines.cross": set(),
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        self.assertEqual(attributes["attribution.commits"]["intValue"], "2")
        self.assertEqual(attributes["attribution.context.meta"]["intValue"], "1")

    def test_meta_reports_single_commit(self) -> None:
        trace = Trace(seed="foo")
        trace.append(
            node="meta",
            span="span-1",
            parent=None,
            event="tool_start",
            payload={"inputs": {"node": "aaa"}},
        )
        trace.append(
            node="meta",
            span="span-1",
            parent=None,
            event="tool_end",
            payload={"output": {"content": json.dumps({
                "kind": "commit", "sha": "aaa",
            })}},
        )

        processor = KnowledgeGraphTraceProcessor(Metrics({
            "context.commits": {"aaa"},
            "context.commits.correction": frozenset(),
            "context.commits.lineage": frozenset(),
            "context.lines.correction": set(),
            "context.lines.lineage": set(),
            "context.lines.cross": set(),
        }))
        document = _serialize(TraceSerializer(processors=[processor]), trace)
        attributes = _attributes(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

        self.assertEqual(attributes["attribution.commits"]["intValue"], "1")
        self.assertEqual(attributes["attribution.context.meta"]["intValue"], "1")


class _FixedProcessor:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def process(self, span: Span) -> dict[str, object]:
        if span.kind != "tool":
            return {}
        return dict(self._values)


def _serialize(
    serializer: TraceSerializer,
    trace: Trace,
    *,
    resource: dict[str, object] | None = None,
) -> dict[str, object]:
    buffer = io.StringIO()
    serializer.serialize(trace, buffer, resource=resource)
    return json.loads(buffer.getvalue())


def _attributes(span: dict[str, object]) -> dict[str, dict[str, object]]:
    return {entry["key"]: entry["value"] for entry in span["attributes"]}


if __name__ == "__main__":
    unittest.main()
