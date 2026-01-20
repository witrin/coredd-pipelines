"""Global excepthook that routes uncaught exceptions into logging.

Importing this module installs the hook so uncaught exceptions are captured and
emitted as structured payloads to the configured logger instead of touching
stdout or stderr directly.
"""

from __future__ import annotations

import os
import sys
import traceback
import threading
import logging
from datetime import datetime, timezone
from types import TracebackType
from typing import Optional, Type


class JsonlExceptionHook:
    """Encapsulates installation and handling of a logging-based excepthook."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)
        chain_env = os.getenv("JSONL_EXCEPTION_CHAIN", "").strip().lower()
        self._chain_previous = chain_env in {"1", "true", "yes", "on"}
        self._previous_hook = sys.excepthook
        self._installed = False

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _handle(
        self,
        exc_type: Type[BaseException],
        exc: BaseException,
        tb: Optional[TracebackType],
    ) -> None:
        record = {
            "timestamp": self._timestamp(),
            "type": f"{exc_type.__module__}.{exc_type.__name__}",
            "message": str(exc),
            "stack": traceback.format_exception(exc_type, exc, tb),
        }
        try:
            self._logger.error(
                "unhandled_exception",
                extra={"payload": record},
                exc_info=(exc_type, exc, tb),
            )
        except Exception:  # pragma: no cover - logging must never block exceptions.
            pass

        if self._chain_previous and self._previous_hook is not None:
            self._previous_hook(exc_type, exc, tb)

    def _handle_thread_exception(self, args: threading.ExceptHookArgs) -> None:
        self._handle(args.exc_type, args.exc_value, args.exc_traceback)

    def _handle_unraisable(self, unraisable: sys.UnraisableHookArgs) -> None:
        if unraisable.exc_value is None:
            return
        self._handle(
            type(unraisable.exc_value),
            unraisable.exc_value,
            unraisable.exc_traceback,
        )

    def install(self) -> None:
        if self._installed:
            return
        self._previous_hook = sys.excepthook
        sys.excepthook = self._handle
        threading.excepthook = self._handle_thread_exception  # type: ignore[assignment]
        sys.unraisablehook = self._handle_unraisable  # type: ignore[assignment]
        self._installed = True


_HOOK = JsonlExceptionHook()
_HOOK.install()


__all__ = ["JsonlExceptionHook"]
