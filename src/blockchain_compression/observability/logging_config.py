"""Logging configuration helpers for CLI entry points.

Supports standard Python log levels via CLI flags or environment variable
``BLOCKCHAIN_COMPRESSION_LOG_LEVEL``, plus an opt-in single-line JSON log
format while leaving library logging unconfigured unless explicitly invoked.
"""

import json
import logging
import os

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMATS = ("text", "json")
LOG_LEVEL_ENV_VAR = "BLOCKCHAIN_COMPRESSION_LOG_LEVEL"


class _JsonLogFormatter(logging.Formatter):
    """Minimal single-line JSON formatter for CLI structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _normalize_log_level(raw: str, *, source: str) -> str:
    if not isinstance(raw, str):
        raise TypeError(f"{source} must be a string, got {type(raw).__name__}")
    normalized = raw.strip().upper()
    if normalized not in LOG_LEVELS:
        allowed = ", ".join(LOG_LEVELS)
        raise ValueError(f"invalid {source} {raw!r}; choose from {allowed}")
    return normalized


def resolve_log_level(
    cli_level: str | None = None,
    *,
    env_var: str = LOG_LEVEL_ENV_VAR,
    default: str = "INFO",
) -> str:
    """Resolve effective log level name, giving CLI precedence over ``env_var``."""
    if cli_level is not None:
        return _normalize_log_level(cli_level, source="--log-level")
    env_raw = os.environ.get(env_var)
    if env_raw is not None and env_raw.strip():
        return _normalize_log_level(env_raw, source=env_var)
    return _normalize_log_level(default, source="default log level")


def configure_logging(level_name: str, *, log_format: str = "text") -> int:
    """Configure root logging level and formatter without dropping active handlers."""
    normalized_level = _normalize_log_level(level_name, source="log level")
    if not isinstance(log_format, str):
        raise TypeError(f"log_format must be a string, got {type(log_format).__name__}")
    normalized_format = log_format.strip().lower()
    if normalized_format not in LOG_FORMATS:
        allowed = ", ".join(LOG_FORMATS)
        raise ValueError(f"invalid --log-format {log_format!r}; choose from {allowed}")

    level = getattr(logging, normalized_level)
    root = logging.getLogger()
    if not root.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(
            _JsonLogFormatter() if normalized_format == "json" else logging.Formatter("%(message)s")
        )
        root.addHandler(stream_handler)
    else:
        for handler in root.handlers:
            if normalized_format == "json":
                handler.setFormatter(_JsonLogFormatter())
            elif isinstance(handler.formatter, _JsonLogFormatter):
                handler.setFormatter(logging.Formatter("%(message)s"))
    root.setLevel(level)
    return level
