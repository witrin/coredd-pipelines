"""Export a persisted attribution/localization trace to OTLP/JSON.

Run inside the notebook container via ``python -m benchmark_notebook.otlp`` (see
this package's README.md). Pulls one trace asset from GCS (reusing the notebook
CloudStorage cache + credentials), turns its event stream into an OTLP trace
whose spans carry every LLM prompt and model response as span events, then writes
the OTLP/JSON file and (optionally) POSTs it straight to a running OTLP viewer at
/v1/traces.

The trace parquet schema (see benchmark.attribution.tracing.Trace) is one row
per event: trace, sequence, timestamp, node, span, parent, event, payload. Span
boundaries are the *_start / *_end event pairs sharing one ``span`` id; ``parent``
nests them. Prompts live in llm_start payload.messages/prompts, responses in
llm_end payload.generations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import datetime
from typing import Iterable, Mapping, Sequence


class TraceExporter:
    """Loads a trace asset and renders it as a single OTLP/JSON trace."""

    def __init__(self, bucket: str, project: str | None) -> None:
        from benchmark_notebook.io import CloudStorage

        self._storage = CloudStorage(bucket=bucket, project=project)

    def load(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
    ) -> "_RenderedTrace":
        from benchmark.attribution.tracing import Trace

        table = self._storage.asset(
            key=key,
            version=version,
            location=location,
            partition=partition,
        )
        trace = Trace.from_table(table)
        events = sorted(trace.events(), key=lambda event: int(event["sequence"]))
        if not events:
            raise ValueError(
                f"Trace {key} partition={partition} has no events."
            )
        return _RenderedTrace(trace_id=trace.id, events=events)


class _RenderedTrace:
    """An ordered event list turned into OTLP spans on demand."""

    def __init__(self, trace_id: str, events: Sequence[Mapping[str, object]]) -> None:
        self._trace_id = trace_id
        self._events = events

    def document(self, service: str) -> dict:
        builder = _SpanBuilder(self._trace_id)
        for event in self._events:
            builder.absorb(event)
        spans = builder.finish()
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            self._attribute("service.name", service),
                            self._attribute("trace.seed", self._trace_id),
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "benchmark.trace"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

    def _attribute(self, key: str, value: str) -> dict:
        return {"key": key, "value": {"stringValue": value}}


class _SpanBuilder:
    """Folds *_start / *_end event pairs into OTLP spans with prompt/response events."""

    def __init__(self, trace_id: str) -> None:
        self._trace_hex = self._hex(trace_id, width=32)
        self._open: dict[str, dict] = {}
        self._closed: list[dict] = []

    def absorb(self, event: Mapping[str, object]) -> None:
        kind = str(event.get("event") or "")
        span = str(event.get("span") or "")
        if not span:
            return
        nanos = self._nanos(event.get("timestamp"))
        payload = self._payload(event.get("payload"))

        if kind.endswith("_start"):
            self._begin(event, span, nanos, payload)
        elif kind.endswith("_end"):
            self._end(span, nanos, payload)
        elif kind.endswith("_error"):
            self._error(span, nanos, payload)

    def finish(self) -> list[dict]:
        # A trailing *_start without its *_end still belongs in the output; close
        # it at its own start time so the viewer renders a zero-width span rather
        # than dropping the call.
        for span in list(self._open):
            self._closed.append(self._open.pop(span))
        return self._closed

    def _begin(
        self,
        event: Mapping[str, object],
        span: str,
        nanos: int,
        payload: object,
    ) -> None:
        node = str(event.get("node") or event.get("event") or "span")
        parent = event.get("parent")
        record = {
            "traceId": self._trace_hex,
            "spanId": self._hex(span, width=16),
            "name": node,
            "kind": 1,
            "startTimeUnixNano": str(nanos),
            "endTimeUnixNano": str(nanos),
            "attributes": [self._attr("trace.node", node)],
            "events": [],
        }
        if isinstance(parent, str) and parent:
            record["parentSpanId"] = self._hex(parent, width=16)
        for note in self._prompt_events(nanos, payload):
            record["events"].append(note)
        self._open[span] = record

    def _end(self, span: str, nanos: int, payload: object) -> None:
        record = self._open.pop(span, None)
        if record is None:
            return
        record["endTimeUnixNano"] = str(nanos)
        for note in self._response_events(nanos, payload):
            record["events"].append(note)
        self._closed.append(record)

    def _error(self, span: str, nanos: int, payload: object) -> None:
        record = self._open.get(span)
        if record is None:
            return
        detail = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        record["status"] = {"code": 2, "message": detail[:512]}
        record["events"].append(
            {
                "timeUnixNano": str(nanos),
                "name": "error",
                "attributes": [self._attr("error.detail", detail)],
            }
        )

    def _prompt_events(self, nanos: int, payload: object) -> Iterable[dict]:
        if not isinstance(payload, Mapping):
            return
        for batch in payload.get("messages") or []:
            for message in batch if isinstance(batch, list) else [batch]:
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                role = str(message.get("type") or message.get("role") or "message")
                yield self._note(nanos, "prompt", role, self._text(content))
        for prompt in payload.get("prompts") or []:
            yield self._note(nanos, "prompt", "prompt", self._text(prompt))

    def _response_events(self, nanos: int, payload: object) -> Iterable[dict]:
        if not isinstance(payload, Mapping):
            return
        for batch in payload.get("generations") or []:
            for item in batch if isinstance(batch, list) else [batch]:
                if not isinstance(item, Mapping):
                    continue
                message = item.get("message")
                content = item.get("text")
                if not content and isinstance(message, Mapping):
                    content = message.get("content")
                yield self._note(nanos, "response", "assistant", self._text(content))
                calls = message.get("tool_calls") if isinstance(message, Mapping) else None
                for call in calls or []:
                    if not isinstance(call, Mapping):
                        continue
                    args = json.dumps(call.get("args", {}), ensure_ascii=False)
                    yield self._note(
                        nanos,
                        "tool_call",
                        str(call.get("name") or "tool"),
                        args,
                    )

    def _note(self, nanos: int, name: str, role: str, body: str) -> dict:
        return {
            "timeUnixNano": str(nanos),
            "name": name,
            "attributes": [
                self._attr(f"{name}.role", role),
                self._attr(f"{name}.content", body),
            ],
        }

    def _text(self, content: object) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for chunk in content:
                if isinstance(chunk, Mapping):
                    parts.append(str(chunk.get("text") or json.dumps(chunk, ensure_ascii=False)))
                else:
                    parts.append(str(chunk))
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    def _payload(self, raw: object) -> object:
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return raw or {}

    def _nanos(self, timestamp: object) -> int:
        if isinstance(timestamp, datetime):
            return int(timestamp.timestamp() * 1_000_000_000)
        if isinstance(timestamp, str):
            try:
                return int(datetime.fromisoformat(timestamp).timestamp() * 1_000_000_000)
            except ValueError:
                return 0
        return 0

    def _attr(self, key: str, value: str) -> dict:
        return {"key": key, "value": {"stringValue": value}}

    def _hex(self, value: str, width: int) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return digest[:width]


class TraceCommand:
    """CLI entry point: load, render, write, and optionally POST a trace."""

    def run(self, argv: Sequence[str] | None = None) -> int:
        args = self._parse(argv)
        exporter = TraceExporter(bucket=args.bucket, project=args.project)
        rendered = exporter.load(
            key=args.key,
            version=args.version,
            location=args.location,
            partition=args.partition,
        )
        document = rendered.document(service=args.service)

        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
        spans = len(document["resourceSpans"][0]["scopeSpans"][0]["spans"])
        print(f"Wrote {spans} spans to {args.out}")

        if args.endpoint:
            self._post(args.endpoint, document)
            print(f"Posted trace to {args.endpoint}")
        return 0

    def _post(self, endpoint: str, document: Mapping[str, object]) -> None:
        body = json.dumps(document).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status >= 300:
                raise RuntimeError(f"OTLP endpoint returned {response.status}")

    def _parse(self, argv: Sequence[str] | None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            description="Export a benchmark trace asset to OTLP/JSON.",
        )
        parser.add_argument(
            "--key",
            default="attribution-change-trace",
            help="Trace asset key (e.g. attribution-change-trace, localization-change-trace).",
        )
        parser.add_argument("--version", default="2.0.0", help="Asset version.")
        parser.add_argument("--location", default="main", help="Asset location.")
        parser.add_argument(
            "--partition",
            required=True,
            help="Trace partition, e.g. 'microsoft-Olive:2257|0' (use the stored partition key).",
        )
        parser.add_argument("--bucket", default="bug-request-bucket", help="GCS bucket.")
        parser.add_argument("--project", default="bug-request", help="GCP project.")
        parser.add_argument(
            "--service",
            default="benchmark-trace",
            help="service.name shown in the viewer.",
        )
        parser.add_argument(
            "--out",
            default="trace.otlp.json",
            help="Output OTLP/JSON path.",
        )
        parser.add_argument(
            "--endpoint",
            nargs="?",
            const="http://trace:4318/v1/traces",
            default=None,
            help="POST the trace here too. Bare flag defaults to the 'trace' compose service.",
        )
        return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(TraceCommand().run())
