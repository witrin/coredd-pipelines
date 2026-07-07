from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from benchmark.tracing import (
    TracedRunnable,
    Trace,
    TracingCallbackHandler,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult


class TestTrace(unittest.TestCase):
    def test_trace(self) -> None:
        trace = Trace(seed="seed-123")
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"phase": "start"},
        )
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_end",
            payload={"phase": "end"},
        )

        events = list(trace.events())
        self.assertEqual(events[0]["trace"], "seed-123")
        self.assertEqual(events[1]["trace"], "seed-123")
        self.assertEqual(events[0]["sequence"], 1)
        self.assertEqual(events[1]["sequence"], 2)
        self.assertIsInstance(events[0]["payload"], str)
        self.assertEqual(json.loads(events[0]["payload"])["phase"], "start")

    def test_trace_json(self) -> None:
        trace = Trace(seed="json-default")
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"value": object()},
        )

        payload = json.loads(list(trace.events())[0]["payload"])
        self.assertIsInstance(payload["value"], str)

    def test_trace_table(self) -> None:
        trace = Trace(seed="tabular")
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"phase": "start"},
        )

        trace_table = trace.to_table()
        self.assertEqual(
            trace_table.column_names,
            [
                "trace",
                "sequence",
                "timestamp",
                "node",
                "span",
                "parent",
                "event",
                "payload",
            ],
        )

    def test_trace_roundtrip(self) -> None:
        trace = Trace(seed="roundtrip", metadata={"foo": "bar", "baz": 1})
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"phase": "start"},
        )
        trace.append(
            node="summarize",
            span="span-1",
            parent=None,
            event="node_end",
            payload={"phase": "end"},
        )

        restored = Trace.from_table(trace.to_table())

        self.assertEqual(restored.id, "roundtrip")
        self.assertEqual(restored.metadata, {"foo": "bar", "baz": 1})
        original = list(trace.events())
        revived = list(restored.events())
        self.assertEqual(len(original), len(revived))
        for before, after in zip(original, revived, strict=True):
            self.assertEqual(before["trace"], after["trace"])
            self.assertEqual(before["sequence"], after["sequence"])
            self.assertEqual(before["node"], after["node"])
            self.assertEqual(before["span"], after["span"])
            self.assertEqual(before["parent"], after["parent"])
            self.assertEqual(before["event"], after["event"])
            self.assertEqual(before["payload"], after["payload"])

        trace.append(
            node="summarize",
            span="span-2",
            parent=None,
            event="node_start",
            payload=None,
        )
        revived_after = Trace.from_table(trace.to_table())
        revived_after.append(
            node="summarize",
            span="span-2",
            parent=None,
            event="node_end",
            payload=None,
        )
        sequences = [event["sequence"] for event in revived_after.events()]
        self.assertEqual(sequences, [1, 2, 3, 4])


class TestTracingCallbackHandler(unittest.TestCase):
    def test_callback_node(self) -> None:
        trace = Trace(seed="node-trace")
        handler = TracingCallbackHandler(trace)

        handler.on_chain_start(
            {"name": "summarize"},
            {"correction": "value"},
            run_id="node-1",
        )
        handler.on_chain_end(
            {"summary": "ok"},
            run_id="node-1",
        )

        events = list(trace.events())
        self.assertEqual(
            [event["event"] for event in events], ["node_start", "node_end"]
        )
        self.assertEqual(events[0]["span"], events[1]["span"])
        self.assertIsNone(events[0]["parent"])
        self.assertIsNone(events[1]["parent"])
        self.assertEqual(events[0]["node"], "summarize")
        self.assertEqual(events[1]["node"], "summarize")

    def test_callback_missing(self) -> None:
        trace = Trace(seed="node-trace-missing-name")
        handler = TracingCallbackHandler(trace)

        with self.assertRaises(ValueError):
            handler.on_chain_start(
                None,
                {"correction": "value"},
                run_id="node-1",
            )

    def test_callback_fallback(self) -> None:
        trace = Trace(seed="node-trace-kwargs-name")
        handler = TracingCallbackHandler(trace)

        handler.on_chain_start(
            None,
            {"correction": "value"},
            run_id="node-1",
            name=" summarize ",
        )
        handler.on_chain_end(
            {"summary": "ok"},
            run_id="node-1",
        )

        events = list(trace.events())
        self.assertEqual(
            [event["event"] for event in events], ["node_start", "node_end"]
        )
        self.assertEqual(events[0]["node"], "summarize")
        self.assertEqual(events[1]["node"], "summarize")

    def test_callback_llm(self) -> None:
        trace = Trace(seed="llm-trace")
        handler = TracingCallbackHandler(trace)
        handler.on_chain_start(
            {"name": "summarize"},
            {"correction": "value"},
            run_id="node-1",
        )
        handler.on_chat_model_start(
            {"name": "ChatOpenAI"},
            [[HumanMessage(content="prompt")]],
            run_id="llm-1",
            parent_run_id="node-1",
            invocation_params={"model": "fake"},
        )
        handler.on_llm_end(
            LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            text="hello",
                            message=AIMessage(
                                content="hello",
                                id="msg-1",
                                additional_kwargs={"tool": "meta"},
                                response_metadata={
                                    "logprobs": {"token_logprobs": [-0.1]}
                                },
                                tool_calls=[
                                    {
                                        "id": "call-1",
                                        "name": "meta",
                                        "args": {"node": "commit:1"},
                                        "type": "tool_call",
                                    }
                                ],
                                invalid_tool_calls=[
                                    {
                                        "id": "invalid-1",
                                        "name": "oops",
                                        "args": "broken",
                                        "type": "invalid_tool_call",
                                        "error": "bad args",
                                    }
                                ],
                                usage_metadata={
                                    "input_tokens": 1,
                                    "output_tokens": 1,
                                    "total_tokens": 2,
                                },
                            ),
                            generation_info={"finish_reason": "stop"},
                        )
                    ]
                ],
                llm_output={"token_usage": {"total_tokens": 2}},
            ),
            run_id="llm-1",
            parent_run_id="node-1",
        )

        events = list(trace.events())
        self.assertEqual(
            [event["event"] for event in events],
            ["node_start", "llm_start", "llm_end"],
        )
        self.assertEqual(events[1]["parent"], "node-1")
        self.assertEqual(events[2]["parent"], "node-1")
        self.assertEqual(events[1]["span"], events[2]["span"])
        self.assertEqual(events[1]["node"], "summarize")
        self.assertEqual(events[2]["node"], "summarize")

        start_payload = json.loads(events[1]["payload"])
        self.assertIn("messages", start_payload)
        self.assertEqual(
            start_payload["messages"],
            [
                [
                    {
                        "type": "human",
                        "content": "prompt",
                        "id": None,
                        "name": None,
                        "additional_kwargs": {},
                        "response_metadata": {},
                        "tool_calls": None,
                        "invalid_tool_calls": None,
                        "tool_call_id": None,
                        "usage_metadata": None,
                        "artifact": None,
                    }
                ]
            ],
        )
        end_payload = json.loads(events[2]["payload"])
        self.assertEqual(end_payload["llm_output"]["token_usage"]["total_tokens"], 2)
        self.assertIn("llm_output_keys", end_payload)
        self.assertIn("generation_count", end_payload)
        self.assertEqual(end_payload["generations"][0][0]["text"], "hello")
        self.assertEqual(
            end_payload["generations"][0][0]["message"]["content"],
            "hello",
        )
        self.assertEqual(end_payload["generations"][0][0]["message"]["id"], "msg-1")
        self.assertEqual(
            end_payload["generations"][0][0]["message"]["tool_calls"],
            [
                {
                    "id": "call-1",
                    "name": "meta",
                    "args": {"node": "commit:1"},
                    "type": "tool_call",
                }
            ],
        )
        self.assertEqual(
            end_payload["generations"][0][0]["message"]["invalid_tool_calls"],
            [
                {
                    "id": "invalid-1",
                    "name": "oops",
                    "args": "broken",
                    "type": "invalid_tool_call",
                    "error": "bad args",
                }
            ],
        )
        self.assertEqual(
            end_payload["generations"][0][0]["message"]["usage_metadata"],
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        self.assertEqual(
            end_payload["generations"][0][0]["generation_info"]["finish_reason"],
            "stop",
        )
        self.assertEqual(end_payload["logprobs"]["token_logprobs"], [-0.1])

    def test_callback_tool(self) -> None:
        trace = Trace(seed="tool-trace")
        handler = TracingCallbackHandler(trace)
        handler.on_chain_start(
            {"name": "agent"},
            {"messages": ["value"]},
            run_id="node-1",
        )
        handler.on_tool_start(
            {"name": "content"},
            '{"sha":"deadbeef"}',
            run_id="tool-1",
            parent_run_id="node-1",
            inputs={"sha": "deadbeef"},
        )
        handler.on_tool_end(
            ToolMessage(
                content=json.dumps({"sha": "deadbeef", "patch": "diff --git"}),
                name="content",
                tool_call_id="call-1",
            ),
            run_id="tool-1",
            parent_run_id="node-1",
        )

        events = list(trace.events())
        self.assertEqual(
            [event["event"] for event in events],
            ["node_start", "tool_start", "tool_end"],
        )
        self.assertEqual(events[1]["node"], "content")
        self.assertEqual(events[2]["node"], "content")
        self.assertEqual(events[1]["parent"], "node-1")
        self.assertEqual(events[2]["parent"], "node-1")

        start_payload = json.loads(events[1]["payload"])
        self.assertEqual(start_payload["input"], '{"sha":"deadbeef"}')
        self.assertEqual(start_payload["inputs"], {"sha": "deadbeef"})

        end_payload = json.loads(events[2]["payload"])
        self.assertEqual(end_payload["output_type"], "ToolMessage")
        self.assertEqual(
            end_payload["output"],
            {
                "type": "tool",
                "content": {"sha": "deadbeef", "patch": "diff --git"},
                "id": None,
                "name": "content",
                "additional_kwargs": {},
                "response_metadata": {},
                "tool_calls": None,
                "invalid_tool_calls": None,
                "tool_call_id": "call-1",
                "usage_metadata": None,
                "artifact": None,
            },
        )


class TestTracedRunnable(unittest.TestCase):
    def test_invoke_trace(self) -> None:
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"summary": "ok", "aborted": False}
        runnable = TracedRunnable(compiled)

        result, trace = runnable.invoke_with_trace(
            {"correction": "value"},
            seed="seed-xyz",
        )

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(trace.id, "seed-xyz")
        self.assertEqual(compiled.invoke.call_args.args[0], {"correction": "value"})
        self.assertIn("config", compiled.invoke.call_args.kwargs)
        callbacks = compiled.invoke.call_args.kwargs["config"]["callbacks"]
        self.assertEqual(len(callbacks), 1)
        self.assertIsInstance(callbacks[0], TracingCallbackHandler)

    def test_invoke_kwargs(self) -> None:
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"summary": "ok", "aborted": False}
        runnable = TracedRunnable(compiled)

        result = runnable.invoke(
            {"correction": "value"},
            config={"tags": ["demo"]},
        )

        self.assertEqual(result["summary"], "ok")
        compiled.invoke.assert_called_once_with(
            {"correction": "value"},
            config={"tags": ["demo"]},
        )
