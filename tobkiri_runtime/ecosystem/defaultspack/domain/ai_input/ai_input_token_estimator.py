from __future__ import annotations

import json
from typing import Any, Protocol


class TokenEstimatorProtocol(Protocol):
    def estimate_text(self, text: str) -> int:
        ...

    def estimate_json(self, value: Any) -> int:
        ...


def estimate_tokens(text: str) -> int:
    """Small, dependency-free estimate that can be swapped for provider tokenizers."""
    if not text:
        return 0
    return max(1, len(text) // 3)


def estimate_json_tokens(value: Any) -> int:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        rendered = json.dumps(str(value), ensure_ascii=False)
    return estimate_tokens(rendered)


class ApproximateTokenEstimator:
    def estimate_text(self, text: str) -> int:
        return estimate_tokens(text)

    def estimate_json(self, value: Any) -> int:
        return estimate_json_tokens(value)
