"""Shared Host-owned provider runtime assertions for compatibility tests."""

import hashlib
import json
from pathlib import Path
from typing import Any


def exercise_captured_provider_send(
    tmp_path: Path,
    monkeypatch: Any,
    provider_id: str,
    *,
    endpoint: str,
    model_id: str = "account-visible-model",
) -> dict[str, Any]:
    """Send through the public v4 adapter with exact registry and credential calls."""
    del monkeypatch
    from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
        REGISTRY_CONTRACT,
        REGISTRY_OPERATION,
        create_generate_operation,
    )
    from tests.test_defaultspack_provider_program import _v4_provider_fixture

    fixture, broker = _v4_provider_fixture(
        tmp_path,
        provider_id,
        endpoint=endpoint,
    )
    metadata = fixture.dispatch.provider_metadata("tobkiri.resource.ai.provider.registry.v1")
    assert len(metadata) == 1
    assert metadata[0]["provider_instance_id"] == f"provider.{provider_id}"
    canary = f"{provider_id}-credential-canary"
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class CapturedHostClient:
        def invoke(
            self,
            contract_id: str,
            operation: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            calls.append((contract_id, operation, dict(payload)))
            if contract_id == REGISTRY_CONTRACT and operation == REGISTRY_OPERATION:
                return {"providers": list(metadata)}
            raise AssertionError("unexpected captured Host operation")

        def post_json_with_credential(self, **payload: Any) -> dict[str, Any]:
            assert payload["credential_handle"] == metadata[0]["credential_handle"]
            assert payload["provider_instance_id"] == f"provider.{provider_id}"
            assert payload["credential_scope"] == "ai.generate"
            captured.update(
                url=payload["endpoint"],
                headers={"Authorization": "<Host-bound>"},
                body=dict(payload["body"]),
                request={"credential_handle": payload["credential_handle"]},
            )
            assert fixture.resolve_api_key(broker) == canary
            return {
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }

    captured: dict[str, Any] = {}
    result = create_generate_operation(CapturedHostClient())(
        "generate",
        {
            "profile_id": "defaults",
            "provider_id": provider_id,
            "model_id": f"{provider_id}/{model_id}",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert result["output"] == "ok"
    assert captured["url"] == f"{endpoint.rstrip('/')}/chat/completions"
    assert captured["body"]["model"] == model_id
    assert captured["headers"]["Authorization"] == "<Host-bound>"
    assert calls[0] == (
        REGISTRY_CONTRACT,
        REGISTRY_OPERATION,
        {"profile_id": "defaults"},
    )
    assert len(calls) == 1
    assert canary not in json.dumps(result, sort_keys=True)
    assert canary not in json.dumps(metadata, sort_keys=True)
    captured["headers"]["Authorization"] = "<redacted>"
    public_evidence = {
        "result": result,
        "captured": captured,
        "calls": calls,
        "provider_id": provider_id,
        "credential_bound": True,
        "credential_digest": hashlib.sha256(canary.encode("utf-8")).hexdigest(),
    }
    assert canary not in json.dumps(public_evidence, sort_keys=True)
    return public_evidence
