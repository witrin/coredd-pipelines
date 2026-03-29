from __future__ import annotations

import contextvars
import json
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterator

import pyarrow as pa

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.outputs import LLMResult


class Trace:
    def __init__(
        self,
        seed: str | None = None,
        metadata: dict[str, object] | None = None,
        hook: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.id = seed or uuid.uuid4().hex
        self.metadata = dict(metadata or {})
        self._hook = hook
        self._events: list[dict[str, object]] = []
        self._sequence = 0

    def append(
        self,
        *,
        timestamp: datetime | None = None,
        node: str | None,
        span: str,
        parent: str | None,
        event: str,
        payload: object = None,
    ) -> None:
        self._sequence += 1
        current = {
            "trace": self.id,
            "sequence": self._sequence,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "node": node,
            "span": span,
            "parent": parent,
            "event": event,
            "payload": self._encode_payload(payload),
        }
        self._events.append(current)
        if self._hook is None:
            return
        try:
            self._hook(dict(current))
        except Exception:
            pass

    def events(self) -> Iterator[dict[str, object]]:
        for event in self._events:
            yield dict(event)

    def to_table(self) -> pa.Table:
        events = self._events
        return pa.table(
            {
                "trace": pa.array([e["trace"] for e in events], type=pa.string()),
                "sequence": pa.array([e["sequence"] for e in events], type=pa.int64()),
                "timestamp": pa.array(
                    [e["timestamp"] for e in events],
                    type=pa.timestamp("us", tz="UTC"),
                ),
                "node": pa.array([e["node"] for e in events], type=pa.string()),
                "span": pa.array([e["span"] for e in events], type=pa.string()),
                "parent": pa.array([e["parent"] for e in events], type=pa.string()),
                "event": pa.array([e["event"] for e in events], type=pa.string()),
                "payload": pa.array([e["payload"] for e in events], type=pa.string()),
            }
        )

    def metadata_table(self) -> pa.Table:
        if not self.metadata:
            return pa.table({})

        return pa.table({key: [value] for key, value in self.metadata.items()})

    @classmethod
    def _encode_payload(cls, payload: object) -> str:
        return json.dumps(payload, default=cls._json_default, sort_keys=True)

    @staticmethod
    def _json_default(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)


class TraceRuntime:
    _trace: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
        "attribution_trace",
        default=None,
    )

    @classmethod
    def activate(
        cls,
        trace: Trace | None,
    ) -> object:
        return cls._trace.set(trace)

    @classmethod
    def deactivate(
        cls,
        token: object,
    ) -> None:
        cls._trace.reset(token)

    @classmethod
    def current_trace(cls) -> Trace | None:
        return cls._trace.get()


class TracingCallbackHandler(BaseCallbackHandler):
    raise_error = False

    def __init__(self, trace: Trace) -> None:
        self._trace = trace
        self._started: dict[str, datetime] = {}
        self._nodes: dict[str, str | None] = {}

    def on_chain_start(
        self,
        serialized: dict[str, object] | None,
        inputs: object,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        started_at = datetime.now(timezone.utc)
        span = str(run_id)
        node = self._run_name(serialized, kwargs)
        self._started[span] = started_at
        self._nodes[span] = node
        self._trace.append(
            timestamp=started_at,
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="node_start",
            payload={
                "input_type": type(inputs).__name__,
                "input_keys": (
                    sorted(str(key) for key in inputs.keys())
                    if isinstance(inputs, dict)
                    else None
                ),
            },
        )

    def on_chain_end(
        self,
        outputs: object,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        span = str(run_id)
        started_at = self._started.pop(span, datetime.now(timezone.utc))
        node = self._nodes.pop(span, None)
        self._trace.append(
            timestamp=datetime.now(timezone.utc),
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="node_end",
            payload={
                "output_type": type(outputs).__name__,
                "output_keys": (
                    sorted(str(key) for key in outputs.keys())
                    if isinstance(outputs, dict)
                    else None
                ),
                "duration": self._duration(started_at),
            },
        )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        span = str(run_id)
        started_at = self._started.pop(span, datetime.now(timezone.utc))
        node = self._nodes.pop(span, None)
        self._trace.append(
            timestamp=datetime.now(timezone.utc),
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="node_error",
            payload={
                "duration": self._duration(started_at),
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )

    def on_tool_start(
        self,
        serialized: dict[str, object] | None,
        input_str: str,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        inputs: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        started_at = datetime.now(timezone.utc)
        span = str(run_id)
        node = self._run_name(serialized, kwargs)
        self._started[span] = started_at
        self._nodes[span] = node
        self._trace.append(
            timestamp=started_at,
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="tool_start",
            payload={
                "serialized": serialized,
                "input": input_str,
                "inputs": inputs,
            },
        )

    def on_tool_end(
        self,
        output: object,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        span = str(run_id)
        started_at = self._started.pop(span, datetime.now(timezone.utc))
        node = self._nodes.pop(span, self._node_for_parent(parent_run_id))
        self._trace.append(
            timestamp=datetime.now(timezone.utc),
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="tool_end",
            payload={
                "duration": self._duration(started_at),
                "output": self._output(output),
                "output_type": type(output).__name__,
            },
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        span = str(run_id)
        started_at = self._started.pop(span, datetime.now(timezone.utc))
        node = self._nodes.pop(span, self._node_for_parent(parent_run_id))
        self._trace.append(
            timestamp=datetime.now(timezone.utc),
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="tool_error",
            payload={
                "duration": self._duration(started_at),
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, object] | None,
        messages: list[list[object]],
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        started_at = datetime.now(timezone.utc)
        span = str(run_id)
        self._started[span] = started_at
        self._trace.append(
            timestamp=started_at,
            node=self._node_for_parent(parent_run_id),
            span=span,
            parent=self._parent(parent_run_id),
            event="llm_start",
            payload={
                "serialized": serialized,
                "messages": [
                    [self._message(message) for message in batch] for batch in messages
                ],
                "invocation_params": kwargs.get("invocation_params"),
            },
        )
        self._nodes[span] = self._node_for_parent(parent_run_id)

    def on_llm_start(
        self,
        serialized: dict[str, object] | None,
        prompts: list[str],
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        started_at = datetime.now(timezone.utc)
        span = str(run_id)
        node = self._node_for_parent(parent_run_id)
        self._started[span] = started_at
        self._nodes[span] = node
        self._trace.append(
            timestamp=started_at,
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="llm_start",
            payload={
                "serialized": serialized,
                "prompts": prompts,
                "invocation_params": kwargs.get("invocation_params"),
            },
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        span = str(run_id)
        started_at = self._started.pop(span, datetime.now(timezone.utc))
        node = self._nodes.pop(span, self._node_for_parent(parent_run_id))

        self._trace.append(
            timestamp=datetime.now(timezone.utc),
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="llm_end",
            payload={
                "duration": self._duration(started_at),
                "llm_output": response.llm_output,
                "llm_output_type": type(response.llm_output).__name__,
                "llm_output_keys": (
                    sorted(str(key) for key in response.llm_output.keys())
                    if isinstance(response.llm_output, dict)
                    else None
                ),
                "generation_batches": len(response.generations),
                "generation_count": sum(len(batch) for batch in response.generations),
                "generations": [
                    [self._generation(item) for item in batch]
                    for batch in response.generations
                ],
                "logprobs": self._logprobs(response),
            },
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: object,
        parent_run_id: object | None = None,
        **kwargs: object,
    ) -> None:
        span = str(run_id)
        started_at = self._started.pop(span, datetime.now(timezone.utc))
        node = self._nodes.pop(span, self._node_for_parent(parent_run_id))

        self._trace.append(
            timestamp=datetime.now(timezone.utc),
            node=node,
            span=span,
            parent=self._parent(parent_run_id),
            event="llm_error",
            payload={
                "duration": self._duration(started_at),
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )

    @classmethod
    def _duration(cls, started_at: datetime) -> float:
        return (datetime.now(timezone.utc) - started_at).total_seconds() * 1000.0

    @classmethod
    def _parent(cls, parent_run_id: object | None) -> str | None:
        if parent_run_id is None:
            return None
        return str(parent_run_id)

    @classmethod
    def _run_name(
        cls,
        serialized: dict[str, object] | None,
        kwargs: dict[str, object],
    ) -> str:
        if isinstance(serialized, dict):
            name = cls._non_empty_name(serialized.get("name"))
            if name is not None:
                return name
        fallback = cls._non_empty_name(kwargs.get("name"))
        if fallback is not None:
            return fallback
        raise ValueError(
            "Tracing callback requires a non-empty node name in serialized['name'] or kwargs['name']."
        )

    @classmethod
    def _non_empty_name(cls, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if stripped == "":
            return None
        return stripped

    def _node_for_parent(self, parent_run_id: object | None) -> str | None:
        if parent_run_id is None:
            return None
        return self._nodes.get(str(parent_run_id))

    @classmethod
    def _message(cls, message: object) -> dict[str, object]:
        return {
            "type": getattr(message, "type", type(message).__name__),
            "content": cls._content(getattr(message, "content", None)),
            "id": getattr(message, "id", None),
            "name": getattr(message, "name", None),
            "additional_kwargs": getattr(message, "additional_kwargs", None),
            "response_metadata": getattr(message, "response_metadata", None),
            "tool_calls": getattr(message, "tool_calls", None),
            "invalid_tool_calls": getattr(message, "invalid_tool_calls", None),
            "tool_call_id": getattr(message, "tool_call_id", None),
            "usage_metadata": getattr(message, "usage_metadata", None),
            "artifact": getattr(message, "artifact", None),
        }

    @classmethod
    def _generation(cls, item: object) -> dict[str, object]:
        message = getattr(item, "message", None)
        return {
            "type": type(item).__name__,
            "text": getattr(item, "text", None),
            "message": cls._message(message) if message is not None else None,
            "generation_info": getattr(item, "generation_info", None),
        }

    @classmethod
    def _logprobs(cls, response: LLMResult) -> object:
        if isinstance(response.llm_output, dict) and "logprobs" in response.llm_output:
            return response.llm_output["logprobs"]

        for batch in response.generations:
            for item in batch:
                info = getattr(item, "generation_info", None)
                if isinstance(info, dict) and "logprobs" in info:
                    return info["logprobs"]
                message = getattr(item, "message", None)
                metadata = getattr(message, "response_metadata", None)
                if isinstance(metadata, dict) and "logprobs" in metadata:
                    return metadata["logprobs"]

        return None

    @classmethod
    def _output(cls, output: object) -> object:
        if hasattr(output, "content") or hasattr(output, "tool_call_id"):
            return cls._message(output)
        return output

    @classmethod
    def _content(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except Exception:
            return value


class AttributionRunnable:
    def __init__(self, compiled: object) -> None:
        self._compiled = compiled

    def invoke(
        self,
        input_state: dict[str, object],
        *,
        trace: Trace | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        if trace is None:
            return self._compiled.invoke(input_state, **kwargs)

        token = TraceRuntime.activate(trace)
        try:
            config = dict(kwargs.pop("config", {}))
            existing = config.get("callbacks")
            if existing is None:
                callbacks: list[object] = []
            elif isinstance(existing, (list, tuple)):
                callbacks = list(existing)
            else:
                callbacks = [existing]
            callbacks.append(TracingCallbackHandler(trace))
            config["callbacks"] = callbacks
            return self._compiled.invoke(input_state, config=config, **kwargs)
        finally:
            TraceRuntime.deactivate(token)

    def invoke_with_trace(
        self,
        input_state: dict[str, object],
        *,
        seed: str | None = None,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> tuple[dict[str, object], Trace]:
        trace = Trace(seed=seed, metadata=metadata)
        return self.invoke(input_state, trace=trace, **kwargs), trace

    def __getattr__(self, name: str) -> object:
        return getattr(self._compiled, name)


class ResultTable:
    @classmethod
    def from_result(cls, result: dict[str, object]) -> pa.Table:
        return pa.table(
            {
                "summary": [result.get("summary")],
                "introducer": [result.get("introducer")],
                "aborted": [result.get("aborted")],
                "state_json": [
                    json.dumps(result, default=Trace._json_default, sort_keys=True)
                ],
            }
        )
