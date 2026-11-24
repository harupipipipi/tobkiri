"""Bounded, provider-neutral speech directives from the voice prototype.

This module is staged as a Pack asset. The UI and transport do not invoke it yet; no capture authority is granted.
"""

from __future__ import annotations

import re
from typing import TypedDict


class SpeechSegment(TypedDict):
    """One bounded spoken segment in a provider-neutral playback plan."""

    delay: float
    text: str


SpeechPlan = TypedDict(
    "SpeechPlan", {"segments": list[SpeechSegment], "continue": bool, "finish": bool}
)

_COLON = re.compile(r"(?:(?<=^)|(?<=[\s　]))(SAY|WAIT|SCHEDULE|ASK)\s*:", re.IGNORECASE)
_BARE = re.compile(r"(?<!\S)(CONTINUE|FINISH|ASK)(?=\s*$)", re.IGNORECASE | re.MULTILINE)
_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?\s*\Z", re.IGNORECASE)
_MAX_DELAY = 120.0
_MAX_INPUT_BYTES = 16_384
_MAX_SEGMENTS = 6


def parse_directives(raw: str) -> list[tuple[str, str]]:
    """Parse line directives, including model output packed onto one line."""
    if (
        not isinstance(raw, str)
        or len(raw) > _MAX_INPUT_BYTES
        or len(raw.encode("utf-8")) > _MAX_INPUT_BYTES
    ):
        raise ValueError("voice directive input is invalid or too large")
    source = raw.replace("```", "")
    markers = [
        (match.start(), match.end(), match.group(1).lower())
        for match in _COLON.finditer(source)
    ]
    markers += [
        (match.start(), match.end(), match.group(1).lower())
        for match in _BARE.finditer(source)
    ]
    markers.sort()
    if not markers:
        content = source.strip()
        return [("say", content)] if content else []
    result: list[tuple[str, str]] = []
    if source[: markers[0][0]].strip():
        result.append(("say", source[: markers[0][0]].strip()))
    for index, (_, end, kind) in enumerate(markers):
        next_start = (
            markers[index + 1][0] if index + 1 < len(markers) else len(source)
        )
        result.append((kind, source[end:next_start].strip()))
    return result


def _seconds(raw: str) -> float:
    match = _DURATION.fullmatch(raw)
    return min(float(match.group(1)) if match else 0.0, _MAX_DELAY)


def speech_plan(raw: str) -> SpeechPlan:
    """Build a bounded playback plan; the first terminal directive wins."""
    segments: list[SpeechSegment] = []
    delay = 0.0
    for kind, argument in parse_directives(raw):
        if kind == "wait":
            delay = min(delay + _seconds(argument), _MAX_DELAY)
        elif kind == "schedule":
            seconds, separator, message = argument.partition(":")
            if separator and message.strip() and len(segments) < _MAX_SEGMENTS:
                segments.append({"delay": _seconds(seconds), "text": message.strip()})
        elif kind in {"say", "ask"}:
            if argument and len(segments) < _MAX_SEGMENTS:
                if not (
                    kind == "ask"
                    and any(item["text"] == argument for item in segments)
                ):
                    segments.append({"delay": delay, "text": argument})
                delay = 0.0
            if kind == "ask":
                return {"segments": segments, "continue": False, "finish": False}
        elif kind in {"continue", "finish"}:
            return {
                "segments": segments,
                "continue": kind == "continue",
                "finish": kind == "finish",
            }
    return {"segments": segments, "continue": False, "finish": False}
