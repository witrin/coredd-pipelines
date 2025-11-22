"""Global JSONL excepthook used to trap unhandled exceptions.

The hook is installed as soon as this module is imported, which allows any
application to serialize uncaught exceptions to JSONL before they hit stdout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Optional, Type


class JsonlExceptionHook:
    """Encapsulates installation and handling of a JSONL-based excepthook."""

    def __init__(self, destination: str | None = None) -> None:
        env_destination = os.getenv("JSONL_EXCEPTION_DEST", "").strip()
        self._destination = (destination or env_destination or "stdout").lower()
        chain_env = os.getenv("JSONL_EXCEPTION_CHAIN", "").strip().lower()
        self._chain_previous = chain_env in {"1", "true", "yes", "on"}
        self.log_path: Path | None = None
        self._previous_hook = sys.excepthook
        self._installed = False

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _append_record(self, payload: dict[str, object]) -> None:
        serialized = json.dumps(payload, ensure_ascii=False)

        if self._destination == "stdout":
            print(serialized, file=sys.stdout, flush=True)
            return

        if self._destination == "stderr":
            print(serialized, file=sys.stderr, flush=True)
            return

        target = Path(self._destination)
        try:
            if target.parent and target.parent != Path("."):
                target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
        except Exception:
            print(serialized, file=sys.stdout, flush=True)
        else:
            self.log_path = target

    def _format_exception(
        self,
        exc_type: Type[BaseException],
        exc: BaseException,
        tb: Optional[TracebackType],
    ) -> dict[str, object]:
        return {
            "timestamp": self._timestamp(),
            "type": f"{exc_type.__module__}.{exc_type.__name__}",
            "message": str(exc),
            "stack": traceback.format_exception(exc_type, exc, tb),
        }

    def handle(
        self,
        exc_type: Type[BaseException],
        exc: BaseException,
        tb: Optional[TracebackType],
    ) -> None:
        record = self._format_exception(exc_type, exc, tb)
        try:
            self._append_record(record)
        except Exception:  # pragma: no cover - logging must never block exceptions.
            pass

        if self._chain_previous and self._previous_hook is not None:
            self._previous_hook(exc_type, exc, tb)

    def install(self) -> None:
        if self._installed:
            return
        self._previous_hook = sys.excepthook
        sys.excepthook = self.handle
        self._installed = True


_HOOK = JsonlExceptionHook()
_HOOK.install()


__all__ = ["JsonlExceptionHook"]
