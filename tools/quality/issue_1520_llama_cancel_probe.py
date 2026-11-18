#!/usr/bin/env python3
"""Observe slot release after a direct client closes the pinned llama.cpp stream."""

import argparse
import http.client
import json
import re
import sys
from pathlib import Path
from time import monotonic, sleep


HOST = "127.0.0.1"
MODEL = "gemma3-1b-local"
MAX_RESPONSE_BYTES = 64 * 1024
FIRST_TEXT_TIMEOUT = 30.0
BASELINE_IDLE_TIMEOUT = 90.0
SLOT_TIMEOUT = 3.0
LOG_PREFIX = "Direct model early disconnect"

# Match the live Harness test's long turn and harness-prompt token cap. The
# independent request has no generated conversation history and disables caching.
REQUEST_BODY = json.dumps(
    {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Answer in plain text. Be helpful and concise."},
            {
                "role": "user",
                "content": (
                    "Write a long detailed story with many paragraphs about a quiet forest."
                ),
            },
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 128,
        "temperature": 0,
        "cache_prompt": False,
    }
).encode("utf-8")


class ProbeError(Exception):
    """A response did not provide the evidence required by the probe."""


class ProbeArguments(argparse.ArgumentParser):
    """Keep invalid command-line values out of probe diagnostics."""

    def error(self, message: str) -> None:
        """Reject arguments without repeating their values."""
        self.exit(2, f"{LOG_PREFIX}: outcome=probe_error stage=arguments\n")


def _origin_port(origin: str) -> int:
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{3,4})", origin)
    if match is None or not 1024 <= int(match[1]) <= 65535:
        raise ProbeError
    return int(match[1])


def _authorization(key_file: Path) -> str:
    with key_file.open("rb") as key:
        token = key.read(1025)
    if len(token) > 1024:
        raise ProbeError
    token = token.rstrip()
    if not token or any(not 33 <= byte <= 126 for byte in token):
        raise ProbeError
    return "Bearer " + token.decode("ascii")


def _slots_are_idle(port: int, authorization: str, timeout: float) -> bool:
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.request(
            "GET",
            "/slots",
            headers={"Authorization": authorization, "Connection": "close"},
        )
        with connection.getresponse() as response:
            if response.status != 200:
                raise ProbeError
            body = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()

    if len(body) > MAX_RESPONSE_BYTES:
        raise ProbeError
    slots = json.loads(body)
    if not isinstance(slots, list) or not slots:
        raise ProbeError
    if any(
        not isinstance(slot, dict) or not isinstance(slot.get("is_processing"), bool)
        for slot in slots
    ):
        raise ProbeError
    return all(not slot["is_processing"] for slot in slots)


def _wait_for_first_text(response: http.client.HTTPResponse, start: float) -> float:
    if (
        response.status != 200
        or response.headers.get_content_type() != "text/event-stream"
    ):
        raise ProbeError

    while True:
        line = response.readline(MAX_RESPONSE_BYTES + 1)
        elapsed = monotonic() - start
        if not line or len(line) > MAX_RESPONSE_BYTES or elapsed > FIRST_TEXT_TIMEOUT:
            raise ProbeError
        if not line.startswith(b"data:"):
            continue
        data = line.removeprefix(b"data:").strip()
        if data == b"[DONE]":
            raise ProbeError
        event = json.loads(data)
        if not isinstance(event, dict) or "error" in event:
            raise ProbeError
        choices = event.get("choices")
        if not isinstance(choices, list):
            raise ProbeError
        for choice in choices:
            if not isinstance(choice, dict):
                raise ProbeError
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ProbeError
            text = delta.get("content")
            if isinstance(text, str) and text:
                return elapsed


def _disconnect_after_first_text(port: int, authorization: str) -> float:
    connection = http.client.HTTPConnection(HOST, port, timeout=FIRST_TEXT_TIMEOUT)
    try:
        start = monotonic()
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=REQUEST_BODY,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Connection": "close",
            },
        )
        with connection.getresponse() as response:
            return _wait_for_first_text(response, start)
    finally:
        connection.close()


def _observe_slots(
    port: int, authorization: str, timeout: float, poll_interval: float
) -> tuple[str, float]:
    start = monotonic()
    deadline = start + timeout
    remaining = timeout
    while remaining > 0:
        idle = _slots_are_idle(port, authorization, timeout=min(remaining, 5.0))
        observed_at = monotonic()
        if observed_at > deadline:
            raise ProbeError
        if idle:
            return "idle", observed_at - start
        sleep(min(poll_interval, deadline - observed_at))
        remaining = deadline - monotonic()
    return "still_processing_at_deadline", timeout


def main() -> int:
    """Emit one bounded observation, or a fixed diagnostic for execution failures."""
    parser = ProbeArguments(
        description=__doc__,
        allow_abbrev=False,
    )
    parser.add_argument("--origin", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()

    stage = "arguments"
    try:
        port = _origin_port(args.origin)
        stage = "key_file"
        authorization = _authorization(args.key_file)
        stage = "idle_check"
        baseline, idle_wait = _observe_slots(port, authorization, BASELINE_IDLE_TIMEOUT, 1.0)
        if baseline != "idle":
            raise ProbeError
        stage = "stream"
        first_text = _disconnect_after_first_text(port, authorization)
        stage = "slot_observation"
        outcome, elapsed = _observe_slots(port, authorization, SLOT_TIMEOUT, 0.05)
    except (ProbeError, OSError, http.client.HTTPException, ValueError, RecursionError):
        # Upstream bodies and exception messages can contain sensitive input.
        print(f"{LOG_PREFIX}: outcome=probe_error stage={stage}", file=sys.stderr)
        return 1

    print(
        f"{LOG_PREFIX}: outcome={outcome} first_text_seconds={first_text:.3f} "
        f"slot_observation_seconds={elapsed:.3f} baseline_idle_wait_seconds={idle_wait:.3f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
