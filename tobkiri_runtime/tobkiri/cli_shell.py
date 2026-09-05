"""Fail-closed structured stdio entrypoint for the Tobkiri CLI Shell.

The CLI Shell accepts only the commands declared by ``cli.io.v1``.  It does
not interpret a command string, invoke a shell, or reach the Host directly.
Profile identity is never inferred from a bundled definition. The Host must
capture an active Profile v4/ResolvedPlan before exposing identity to a Shell.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO

from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.validation import validate_document

_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_PACK_ROOT = _RUNTIME_ROOT / "ecosystem" / "defaultspack"
if str(_PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACK_ROOT))

from domain.runtime_v4 import BundledCatalog, DefaultProfileV4Error  # noqa: E402

PROTOCOL = "io.tobkiri.cli.io.v1"
DEFAULT_PROFILE_ID = "defaults"
DEFAULT_OUTPUT_LIMIT = 1_048_576
MAX_OUTPUT_LIMIT = 1_048_576
INVALID_REQUEST_ID = "cli:req:invalid000"
REQUEST_ID_RE = re.compile(r"^cli:req:[a-z0-9][a-z0-9._-]{7,127}$")


class CliShellError(RuntimeError):
    """A structured CLI request was rejected without executing anything."""


def run_structured_stdio(
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    """Process newline-delimited ``cli.io.v1`` frames until EOF.

    Every emitted frame is schema-validated before it reaches stdout.  A
    request-level error is reported as a frame and does not make the process
    execute an arbitrary fallback command.
    """
    catalog = BundledCatalog.load(_PACK_ROOT / "v4")
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        response = _dispatch_line(line, catalog)
        output_stream.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")
        output_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the structured CLI Shell entrypoint."""
    parser = argparse.ArgumentParser(prog="tobkiri-cli")
    parser.add_argument(
        "--structured-stdio",
        action="store_true",
        help="read and write newline-delimited cli.io.v1 frames",
    )
    arguments = parser.parse_args(argv)
    if not arguments.structured_stdio:
        parser.error("Tobkiri CLI Shell requires --structured-stdio")
    return run_structured_stdio()


def _dispatch_line(
    line: str,
    catalog: BundledCatalog,
) -> dict[str, Any]:
    request_id = _request_id_from_line(line)
    try:
        request = validate_document(line, "cli_io")
        request_id = str(request["request_id"])
        if request["type"] != "command":
            raise CliShellError("CLI Shell accepts command frames only")
        result = _handle_request(request, catalog)
        return _validated_response(result)
    except (CliShellError, DefaultProfileV4Error, SchemaValidationError, OSError) as exc:
        return _validated_response(
            {
                "protocol": PROTOCOL,
                "type": "error",
                "request_id": request_id,
                "error": _safe_error_message(exc),
                "exit_status": 64,
            }
        )
    except Exception:
        # Do not expose implementation details or accidentally turn an
        # unexpected error into a command-execution path.
        return _validated_response(
            {
                "protocol": PROTOCOL,
                "type": "error",
                "request_id": request_id,
                "error": "CLI Shell request failed closed",
                "exit_status": 70,
            }
        )


def _handle_request(
    request: dict[str, Any],
    catalog: BundledCatalog,
) -> dict[str, Any]:
    output_limit = _output_limit(request.get("output_limit"))
    if request.get("cancel") or request.get("signal"):
        return _result(
            request,
            stdout="",
            stderr="request cancelled",
            exit_status=130,
            stream="cancelled",
            output_limit=output_limit,
        )

    command = request.get("command")
    if command == "health":
        payload = {
            "status": "ok",
            "protocol": PROTOCOL,
            "shell_provider_id": "shell.cli.default",
        }
        return _result(
            request,
            stdout=_json_text(payload),
            stderr="",
            exit_status=0,
            stream="complete",
            output_limit=output_limit,
        )
    if command == "echo":
        value = request.get("stdin")
        if value is None:
            arguments = request.get("arguments")
            value = arguments.get("text", "") if isinstance(arguments, dict) else ""
        if not isinstance(value, str):
            raise CliShellError("echo input must be text")
        return _result(
            request,
            stdout=value,
            stderr="",
            exit_status=0,
            stream="complete",
            output_limit=output_limit,
        )
    if command == "profile.identity":
        profile_id = str(request.get("profile_id") or DEFAULT_PROFILE_ID)
        if profile_id not in catalog.profiles:
            raise CliShellError(f"profile is not cataloged: {profile_id}")
        raise CliShellError(
            "profile identity requires a Host-captured active Profile v4 activation"
        )
    raise CliShellError(f"command is not declared by {PROTOCOL}: {command!r}")


def _result(
    request: dict[str, Any],
    *,
    stdout: str,
    stderr: str,
    exit_status: int,
    stream: str,
    output_limit: int,
) -> dict[str, Any]:
    if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > output_limit:
        raise CliShellError("structured output exceeds the requested limit")
    return {
        "protocol": PROTOCOL,
        "type": "result",
        "request_id": request["request_id"],
        "profile_id": request.get("profile_id") or DEFAULT_PROFILE_ID,
        "shell_provider_id": request.get("shell_provider_id") or "shell.cli.default",
        "stdout": stdout,
        "stderr": stderr,
        "exit_status": exit_status,
        "stream": stream,
    }


def _validated_response(response: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_document(response, "cli_io")
    except SchemaValidationError as exc:
        # This is an implementation invariant, not a caller-controlled error.
        fallback = {
            "protocol": PROTOCOL,
            "type": "error",
            "request_id": _safe_request_id(response.get("request_id")),
            "error": "CLI Shell emitted an invalid response",
            "exit_status": 70,
        }
        try:
            return validate_document(fallback, "cli_io")
        except SchemaValidationError as fallback_error:  # pragma: no cover
            raise RuntimeError(f"cannot validate CLI response: {exc}; {fallback_error}")


def _request_id_from_line(line: str) -> str:
    try:
        value = json.loads(line).get("request_id")
        if isinstance(value, str) and REQUEST_ID_RE.fullmatch(value):
            return value
    except (TypeError, json.JSONDecodeError):
        pass
    return INVALID_REQUEST_ID


def _safe_request_id(value: Any) -> str:
    return (
        value if isinstance(value, str) and REQUEST_ID_RE.fullmatch(value) else INVALID_REQUEST_ID
    )


def _output_limit(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CliShellError("output_limit is required")
    if value < 1 or value > MAX_OUTPUT_LIMIT:
        raise CliShellError("output_limit is outside the allowed range")
    return min(value, DEFAULT_OUTPUT_LIMIT)


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_error_message(error: Exception) -> str:
    message = str(error).strip()
    return message[:512] or "CLI Shell request was rejected"


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess smoke tests.
    raise SystemExit(main())
