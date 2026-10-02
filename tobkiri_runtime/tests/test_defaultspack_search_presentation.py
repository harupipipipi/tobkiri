"""Search cannot manufacture provider policy or tool execution."""

from __future__ import annotations

import pytest

from ecosystem.defaultspack.defaultspack.search_presentation import (
    MAX_QUERY_BYTES,
    normalize_search_answer,
    present_search_answer,
)


def test_search_preserves_exact_query_and_captured_profile() -> None:
    query = "  日本語の質問\n改行も保持  "
    request = normalize_search_answer(
        {"input": query, "model": "model.saved-local"}, profile_id="defaults",
    )
    assert request["messages"] == [{"role": "user", "content": query}]
    assert request["profile_id"] == "defaults"
    assert request["model_reference"] == "model.saved-local"
    assert request["allow_failover"] is False
    assert "tools" not in request


@pytest.mark.parametrize("key", ["profile_id", "endpoint", "approved", "requirements", "tools"])
def test_search_rejects_client_policy_overrides(key: str) -> None:
    with pytest.raises(ValueError):
        normalize_search_answer(
            {"input": "question", "model": "model.local", key: "override"},
            profile_id="defaults",
        )


@pytest.mark.parametrize("model", ["", " model.local", "model.local\n", "a" * 257, {}])
def test_search_requires_one_bounded_model_reference(model: object) -> None:
    with pytest.raises(ValueError):
        normalize_search_answer({"input": "question", "model": model}, profile_id="defaults")


def test_search_bounds_unicode_bytes_and_requires_profile() -> None:
    for query in (" ", "\x00", "あ" * (MAX_QUERY_BYTES // 3 + 1)):
        with pytest.raises(ValueError):
            normalize_search_answer({"input": query, "model": "local"}, profile_id="defaults")
    with pytest.raises(ValueError):
        normalize_search_answer({"input": "question", "model": "local"}, profile_id="")


def test_answer_is_real_text_without_a_tool_execution_claim() -> None:
    result = present_search_answer({
        "output": "<script>untrusted text</script>",
        "model_id": "local/1b",
        "tool_intents": [{"name": "web_search"}],
        "conversation_id": "unverified",
    })
    assert result["answer"] == "<script>untrusted text</script>"
    assert result["used_tools"] == []
    assert result["answer_scope"] == "model_knowledge"
    assert "conversation_id" not in result


@pytest.mark.parametrize("output", [None, {}, [], "", "  "])
def test_malformed_or_empty_model_output_never_looks_successful(output: object) -> None:
    assert present_search_answer({"output": output})["status"] == "error"
