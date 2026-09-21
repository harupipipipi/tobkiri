from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _store(tmp_path: Path):
    from domain.ai_client.provider_performance import ProviderPerformanceStore

    return ProviderPerformanceStore(tmp_path / "provider_performance.sqlite3")


def _scope(store, secret: str = "secret-account-token") -> str:
    value = store.connection_scope(secret, "https://api.example.test/v1?token=hidden")
    assert value is not None
    return value


def test_hmac_scope_is_stable_non_reversible_and_endpoint_safe(tmp_path):
    store = _store(tmp_path)

    first = _scope(store)
    second = _scope(store)

    assert first == second
    assert first.startswith("hmac:")
    assert "secret-account-token" not in first
    assert store.connection_scope("other-token", "https://api.example.test/v1") != first


def test_store_records_bounded_ewma_median_and_no_sensitive_content(tmp_path):
    store = _store(tmp_path)
    scope = _scope(store)
    for tokens, seconds in ((100, 2.0), (300, 3.0), (50, 1.0)):
        summary = store.record(
            provider_id="groq",
            model_id="llama-test",
            endpoint="https://api.example.test/v1?token=hidden",
            connection_scope=scope,
            method="stream_generation",
            output_tokens=tokens,
            generation_seconds=seconds,
            ttft_seconds=0.2,
        )

    assert summary is not None
    assert summary["successful_samples"] == 3
    assert summary["median_recent_tokens_per_second"] == 50.0
    assert round(summary["ewma_tokens_per_second"], 2) == 60.5
    persisted = store.path.read_bytes()
    assert b"secret-account-token" not in persisted
    assert b"hidden" not in persisted
    assert b"user prompt" not in persisted
    assert b"assistant response" not in persisted


def test_stream_records_only_complete_final_usage(tmp_path):
    from domain.ai_client.provider_performance import track_stream

    store = _store(tmp_path)
    context = {
        "provider_id": "groq",
        "model_id": "llama-test",
        "endpoint_scope": "https://api.example.test/v1",
        "connection_scope": _scope(store),
    }
    times = iter((1.0, 2.0, 3.0))
    events = [
        {"type": "text_delta", "text": "private response"},
        {
            "type": "stream_end",
            "finish_reason": "stop",
            "usage": {"output_tokens": 20},
        },
    ]

    assert list(track_stream(events, context, 0.0, store=store, clock=lambda: next(times))) == events
    summary = store.get(
        provider_id="groq",
        model_id="llama-test",
        endpoint=context["endpoint_scope"],
        connection_scope=context["connection_scope"],
    )
    assert summary is not None
    assert summary["latest_tokens_per_second"] == 10.0
    assert summary["latest_ttft_seconds"] == 1.0
    assert b"private response" not in store.path.read_bytes()


def test_cancelled_or_usage_less_stream_is_excluded(tmp_path):
    from domain.ai_client.provider_performance import track_stream

    store = _store(tmp_path)
    context = {
        "provider_id": "cerebras",
        "model_id": "model",
        "endpoint_scope": "https://api.example.test/v1",
        "connection_scope": _scope(store),
    }

    list(
        track_stream(
            [{"type": "text_delta", "text": "partial"}],
            context,
            0.0,
            store=store,
            clock=iter((1.0, 2.0)).__next__,
        )
    )

    assert store.get(
        provider_id="cerebras",
        model_id="model",
        endpoint=context["endpoint_scope"],
        connection_scope=context["connection_scope"],
    ) is None


def test_nonstream_requires_reliable_usage_and_stays_in_separate_series(tmp_path):
    from domain.ai_client.provider_performance import record_complete_response

    store = _store(tmp_path)
    context = {
        "provider_id": "groq",
        "model_id": "model",
        "endpoint_scope": "https://api.example.test/v1",
        "connection_scope": _scope(store),
    }
    assert record_complete_response(
        {"content": "private", "usage": {}}, context, 1.0, store=store, ended_at=2.0
    ) is None
    result = record_complete_response(
        {"content": "private", "usage": {"output_tokens": 30}},
        context,
        1.0,
        store=store,
        ended_at=3.0,
    )

    assert result is not None
    assert result["method"] == "end_to_end_estimate"
    assert store.get(
        provider_id="groq",
        model_id="model",
        endpoint=context["endpoint_scope"],
        connection_scope=context["connection_scope"],
        method="stream_generation",
    ) is None


def test_concurrent_writes_and_corruption_recovery_are_windows_safe(tmp_path):
    store = _store(tmp_path)
    scope = _scope(store)

    def record(_: int) -> None:
        store.record(
            provider_id="groq",
            model_id="model",
            endpoint="https://api.example.test/v1",
            connection_scope=scope,
            method="stream_generation",
            output_tokens=100,
            generation_seconds=2.0,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(24)))
    summary = store.get(
        provider_id="groq",
        model_id="model",
        endpoint="https://api.example.test/v1",
        connection_scope=scope,
    )
    assert summary is not None
    assert summary["successful_samples"] == 24

    broken = _store(tmp_path / "broken")
    broken.path.parent.mkdir(parents=True, exist_ok=True)
    broken.path.write_bytes(b"not sqlite")
    recovered_scope = _scope(broken)
    assert broken.record(
        provider_id="groq",
        model_id="model",
        endpoint="https://api.example.test/v1",
        connection_scope=recovered_scope,
        method="stream_generation",
        output_tokens=10,
        generation_seconds=1.0,
    ) is not None
    assert list(broken.path.parent.glob("*.corrupt-*"))


class _Provider:
    __module__ = "domain.ai_client.providers.groq_provider"
    _api_key = "account-key"
    _base_url = "https://api.example.test/v1"


def _model(provider_id: str, model_id: str, **values):
    return {
        "id": f"{provider_id}/{model_id}",
        "provider_id": provider_id,
        "model_id": model_id,
        "type": "chat",
        "availability": {"configured": True},
        "context_window": 128_000,
        "capabilities": {"tool_calling": True, "vision": False},
        **values,
    }


def test_fast_selection_filters_capability_context_connection_and_samples(tmp_path):
    from domain.ai_client.provider_performance import select_fast_model

    store = _store(tmp_path)
    provider = _Provider()
    scope = store.connection_scope(provider._api_key, provider._base_url)
    assert scope is not None
    for model_id, tps, samples in (("fast", 100.0, 3), ("slow", 20.0, 3), ("unproven", 500.0, 2)):
        for _ in range(samples):
            store.record(
                provider_id="groq",
                model_id=model_id,
                endpoint=provider._base_url,
                connection_scope=scope,
                method="stream_generation",
                output_tokens=int(tps),
                generation_seconds=1.0,
            )

    selected = select_fast_model(
        [
            _model("groq", "fast"),
            _model("groq", "slow"),
            _model("groq", "unproven"),
            _model("groq", "vision", capabilities={"vision": True}),
        ],
        {"groq": provider},
        current_model="groq/slow",
        min_samples=3,
        requires_tools=True,
        required_context_tokens=64_000,
        store=store,
    )
    incompatible = select_fast_model(
        [_model("groq", "fast")],
        {"groq": provider},
        current_model="groq/fast",
        min_samples=3,
        requires_image=True,
        store=store,
    )

    assert selected["selected_model"] == "groq/fast"
    assert selected["reason"] == "MEASURED_DIRECT_PROVIDER_TPS"
    assert incompatible["reason"] == "INSUFFICIENT_PERFORMANCE_SAMPLES"


def test_ai_client_uses_one_central_success_hook(monkeypatch):
    import domain.ai_client.client as client_module

    class Provider:
        def complete(self, model, messages, tools, params):
            return {"content": "private", "usage": {"output_tokens": 4}}

        def stream(self, model, messages, tools, params):
            return iter([{"type": "stream_end", "usage": {"output_tokens": 4}}])

    client = object.__new__(client_module.AIClient)
    provider = Provider()
    client._model_pack_for_model = lambda model: None
    client._composite_for_model = lambda model: None
    client._call_with_api_routes = lambda *args: (None, False)
    client.resolve_provider = lambda model: (provider, "model")
    client._provider_id_for_provider = lambda resolved, model: "groq"
    client._provider_requires_authority = lambda *args: False
    client._strip_authority_params = lambda params: params
    calls = []
    monkeypatch.setattr(
        client_module,
        "provider_measurement_context",
        lambda *args: {"provider_id": "groq"},
    )
    monkeypatch.setattr(
        client_module,
        "record_complete_response",
        lambda response, context, started: calls.append((response, context)),
    )
    monkeypatch.setattr(
        client_module,
        "track_stream",
        lambda events, context, started: calls.append(("stream", context)) or events,
    )

    response = client.complete("groq/model", [{"role": "user", "content": "secret"}])
    events = list(client.stream("groq/model", [{"role": "user", "content": "secret"}]))

    assert response["usage"]["output_tokens"] == 4
    assert events[-1]["type"] == "stream_end"
    assert len(calls) == 2
    assert calls[0][1] == {"provider_id": "groq"}
    assert calls[1] == ("stream", {"provider_id": "groq"})
