"""Logging bootstrap used across pipelines containers.

The module exposes a single class, ``LoggingBootstrap``, which centralises the
logic for installing a stdout handler with either JSON or text formatting.
Importing this file creates one instance and configures logging immediately.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict


class LoggingBootstrap:
    """Encapsulates all logging initialisation for the container runtime."""

    _BASIC_CONFIG_PATCHED = False
    _LOGGER_CLASS_PATCHED = False
    _ORIGINAL_LOGGER_CLASS = logging.getLoggerClass()
    _SKIP_KEYS = {
        "args",
        "msg",
        "exc_info",
        "exc_text",
        "stack_info",
        "name",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "asctime",
    }

    class _JsonFormatter(logging.Formatter):
        """Minimal JSON formatter that emits Cloud Logging friendly records."""

        def format(self, record: logging.LogRecord) -> str:  # noqa: D401
            payload: Dict[str, Any] = {
                "severity": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
                "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            }

            if record.exc_info:
                payload["traceback"] = self.formatException(record.exc_info)

            for key, value in record.__dict__.items():
                if key.startswith("_") or key in payload or key in LoggingBootstrap._SKIP_KEYS:
                    continue
                try:
                    json.dumps({key: value})
                except (TypeError, ValueError):
                    continue
                payload[key] = value

            return json.dumps(payload, ensure_ascii=True)

    def __init__(self) -> None:
        self._skip_setup = os.getenv("SKIP_DEFAULT_LOGGING_SETUP") == "1"
        self._skip_reset = os.getenv("SKIP_LOGGER_RESET") == "1"
        self._allow_basic = os.getenv("ALLOW_BASIC_LOGGING") == "1"
        self._level = self._coerce_level(os.getenv("LOG_LEVEL", "INFO"))
        self._emit_json = os.getenv("LOG_FORMAT", "json").lower() == "json"
        self._text_format = os.getenv("LOG_TEXT_FORMAT", "%(asctime)s %(levelname)s [%(name)s] %(message)s")
        self._text_datefmt = os.getenv("LOG_DATE_FORMAT", "%Y-%m-%dT%H:%M:%S%z")

    def configure(self) -> None:
        if self._skip_setup:
            return

        self._install_logger_guard()

        root_logger = logging.getLogger()

        if self._already_configured(root_logger):
            return

        root_logger.handlers.clear()
        root_logger.setLevel(self._level)

        handler = logging.StreamHandler(sys.stdout)
        handler._bootstrap_configured = True  # type: ignore[attr-defined]
        handler._bootstrap_allow = True  # type: ignore[attr-defined]
        handler.setFormatter(self._build_formatter())
        root_logger.addHandler(handler)

        if not self._skip_reset:
            self._reset_child_loggers(root_logger)

        if not self._allow_basic:
            self._patch_basic_config()

    def _build_formatter(self) -> logging.Formatter:
        if self._emit_json:
            return self._JsonFormatter()
        return logging.Formatter(self._text_format, datefmt=self._text_datefmt)

    @staticmethod
    def _coerce_level(name: str) -> int:
        value = getattr(logging, name.upper(), None)
        if isinstance(value, int):
            return value
        return logging.INFO

    @staticmethod
    def _already_configured(root_logger: logging.Logger) -> bool:
        return any(getattr(handler, "_bootstrap_configured", False) for handler in root_logger.handlers)

    @staticmethod
    def _reset_child_loggers(root_logger: logging.Logger) -> None:
        for instance in list(logging.root.manager.loggerDict.values()):
            if not isinstance(instance, logging.Logger) or instance is root_logger:
                continue
            instance.handlers.clear()
            instance.propagate = True

    @classmethod
    def _patch_basic_config(cls) -> None:
        if cls._BASIC_CONFIG_PATCHED:
            return

        original_basic_config = logging.basicConfig

        def _basic_config(*args: Any, **kwargs: Any) -> None:
            level: Any = None
            if args:
                level = args[0]
            if kwargs.get("level") is not None:
                level = kwargs["level"]
            if isinstance(level, int):
                logging.getLogger().setLevel(level)
            # Ignore all other parameters to avoid adding extra handlers.

        _basic_config.__doc__ = original_basic_config.__doc__  # type: ignore[attr-defined]
        logging.basicConfig = _basic_config  # type: ignore[assignment]
        cls._BASIC_CONFIG_PATCHED = True

    @classmethod
    def _install_logger_guard(cls) -> None:
        if cls._LOGGER_CLASS_PATCHED:
            return

        original_cls = cls._ORIGINAL_LOGGER_CLASS

        class _BootstrapLogger(original_cls):  # type: ignore[type-arg]
            def addHandler(self, handler: logging.Handler) -> None:  # type: ignore[override]
                if getattr(handler, "_bootstrap_allow", False):
                    super().addHandler(handler)

        logging.setLoggerClass(_BootstrapLogger)
        for instance in list(logging.root.manager.loggerDict.values()):
            if isinstance(instance, logging.Logger):
                instance.__class__ = _BootstrapLogger  # type: ignore[assignment]
        cls._LOGGER_CLASS_PATCHED = True


LoggingBootstrap().configure()
