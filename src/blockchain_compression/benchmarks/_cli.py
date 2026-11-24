"""Shared CLI argument and output helpers for benchmark entry points."""

import argparse
import json
import logging
from pathlib import Path

from blockchain_compression.observability import LOG_LEVEL_ENV_VAR, LOG_LEVELS


def add_common_benchmark_args(parser: argparse.ArgumentParser) -> None:
    """Register shared ``--json``, ``--output``, and ``--log-level`` options."""
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit stable machine-readable JSON instead of the human-readable table.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional file path to write benchmark output instead of stdout/logs.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help=(
            f"Logging verbosity ({', '.join(LOG_LEVELS)}). Defaults to {LOG_LEVEL_ENV_VAR} or INFO."
        ),
    )


def format_json_payload(payload: dict[str, object]) -> str:
    """Render a deterministic RFC 8259 JSON payload with sorted keys."""
    return json.dumps(payload, sort_keys=True, indent=2, allow_nan=False)


def emit_benchmark_output(
    lines_or_text: list[str] | str,
    *,
    as_json: bool,
    output_path: Path | None,
    target_logger: logging.Logger,
) -> None:
    """Write benchmark results to ``output_path``, stdout (for JSON), or ``target_logger``."""
    if isinstance(lines_or_text, list):
        rendered_lines = lines_or_text
        rendered_text = "\n".join(rendered_lines)
    else:
        rendered_text = lines_or_text
        rendered_lines = rendered_text.splitlines()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered_text + "\n", encoding="utf-8")
        return

    if as_json:
        print(rendered_text)
        return

    for line in rendered_lines:
        target_logger.info("%s", line)
