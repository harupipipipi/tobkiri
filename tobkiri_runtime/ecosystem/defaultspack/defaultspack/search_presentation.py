"""Finite Search requests over the same captured Defaults AI Gateway."""

from __future__ import annotations

from typing import Mapping
import uuid


SEARCH_ANSWER_TARGET = (
    "defaults.search.answer",
    "tobkiri.service.ai.generate.v1",
    "rumi_ai_gateway_pack.ai-gateway.generate",
    "rumi_ai_gateway_pack.ai-gateway.generate",
    "rumi_ai_gateway_pack.ai-gateway.generate",
)
MAX_QUERY_BYTES = 60 * 1024


def normalize_search_answer(
    payload: Mapping[str, object], *, profile_id: str,
) -> dict[str, object]:
    """Bind a single text query to the captured Profile and selected model."""
    if not profile_id or set(payload) != {"input", "model"}:
        raise ValueError("Search accepts only input and model")
    query, model = payload["input"], payload["model"]
    if (
        not isinstance(query, str)
        or not query.strip()
        or "\x00" in query
        or len(query.encode("utf-8")) > MAX_QUERY_BYTES
    ):
        raise ValueError("Search requires bounded nonempty text")
    if (
        not isinstance(model, str)
        or not 0 < len(model) <= 256
        or model.strip() != model
        or any(ord(char) < 32 for char in model)
    ):
        raise ValueError("Search requires an explicit model reference")
    return {
        "request_id": str(uuid.uuid4()),
        "profile_id": profile_id,
        "model_reference": model,
        "messages": [{"role": "user", "content": query}],
        "requirements": {"modalities": ["text"], "request_surface": "chat"},
        "parameters": {"max_tokens": 512},
        "allow_failover": False,
    }


def present_search_answer(result: Mapping[str, object]) -> dict[str, object]:
    """Render real model text without claiming web research or tool execution."""
    answer = result.get("output")
    if not isinstance(answer, str) or not answer.strip():
        return {
            "status": "error",
            "error": {
                "code": "EMPTY_ANSWER" if answer == "" else "INVALID_ANSWER",
                "message": "モデルから回答を受け取れませんでした。もう一度お試しください。",
            },
        }
    model = result.get("model_id")
    return {
        "status": "ok",
        "answer": answer,
        "model": model if isinstance(model, str) else "",
        "answer_scope": "model_knowledge",
        "used_tools": [],
    }
