"""Finite model access through production Captured Host and approval dispatch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
import uuid
import sys
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from ecosystem.rumi_provider_registry_pack.runtime.model_access import VERSION
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from tests.test_production_frontend_contract_http import (
    _authenticate,
    _contract,
    _request,
    production_server as production_server,
)

_SAVE = "/api/ai/provider-model-access/save"
_READ = "/api/ai/provider-model-access/read"
_MARKER = "credential:synthetic-model-access-private"


@pytest.fixture
def isolated_catalog_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate disk caches and clear loaded catalog owner memory, including staged imports."""
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "catalog-user-data"))
    for module in list(sys.modules.values()):
        source = str(getattr(module, "__file__", ""))
        if source.endswith("rumi_model_catalog_pack/runtime/catalog.py"):
            monkeypatch.setattr(module, "_OPENROUTER_MEMORY_INVENTORY", None)


class _ModelsResponse:
    """Bounded synthetic HTTPS response at the public network port only."""

    status = 200
    headers: dict[str, str] = {}

    def __init__(self, models: list[dict]) -> None:
        self.body = json.dumps({"data": models}).encode()

    def __enter__(self) -> "_ModelsResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


@pytest.mark.parametrize("models", [[{"id": "vendor/model", "name": "Synthetic model"}], []])
def test_captured_catalog_filtered_live_and_empty_live(
    production_server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_catalog_cache: None,
    models: list[dict],
) -> None:
    """Nested real catalog dispatch keeps official filtered empty success distinct."""
    from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4

    server, _session, _authority = production_server
    _seed(tmp_path)
    requests: list[Any] = []
    credential_operations: list[str] = []
    original = ExactHostProviderBackendV4.invoke

    def observe(self: Any, envelope: Any) -> Any:
        if "credential" in envelope.contract_id:
            credential_operations.append(envelope.operation_id)
        return original(self, envelope)

    def open_models(request: Any, **kwargs: Any) -> _ModelsResponse:
        requests.append(request)
        return _ModelsResponse(models)

    monkeypatch.setattr(ExactHostProviderBackendV4, "invoke", observe)
    monkeypatch.setattr(urllib.request, "urlopen", open_models)
    status, result = _post(server, _headers(server), "/api/ai/provider-model-access/catalog", {
        "profile_id": "defaults", "provider_instance_id": "connection/work",
        "discovery_filters": {"output_modalities": ["text"]},
    })
    assert status == 200, result
    public = result["data"]
    assert public["status"] == "live"
    assert public["models"] == [
        {"model_id": model["id"], "display_name": model["name"]} for model in models
    ]
    assert public["profile_id"] == "defaults"
    assert public["provider_instance_id"] == "connection/work"
    assert _MARKER not in json.dumps(public)
    assert len(requests) == 1
    request = requests[0]
    url = urlsplit(request.full_url)
    assert (url.scheme, url.netloc, url.path) == ("https", "openrouter.ai", "/api/v1/models")
    assert parse_qs(url.query) == {"output_modalities": ["text"]}
    assert request.get_method() == "GET"
    assert all(name.lower() not in {"authorization", "cookie"} for name in request.headers)
    assert credential_operations == []
    assert not list((tmp_path / "catalog-user-data").rglob("openrouter.inventory.lkg.json"))


def test_captured_catalog_offline_is_unavailable_without_credentials(
    production_server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_catalog_cache: None,
) -> None:
    """Offline unfiltered discovery returns no cross-provider or bundled substitute."""
    from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4

    server, _session, _authority = production_server
    _seed(tmp_path)
    requests: list[str] = []
    credential_operations: list[str] = []
    original = ExactHostProviderBackendV4.invoke

    def observe(self: Any, envelope: Any) -> Any:
        if "credential" in envelope.contract_id:
            credential_operations.append(envelope.operation_id)
        return original(self, envelope)

    def offline(request: Any, **kwargs: Any) -> None:
        requests.append(request.full_url)
        assert all(name.lower() not in {"authorization", "cookie"} for name in request.headers)
        raise OSError("synthetic offline")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    monkeypatch.setattr(ExactHostProviderBackendV4, "invoke", observe)
    status, result = _post(server, _headers(server), "/api/ai/provider-model-access/catalog", {
        "profile_id": "defaults", "provider_instance_id": "connection/work",
    })
    assert status == 200, result
    assert result["data"]["status"] == "unavailable"
    assert result["data"]["models"] == []
    assert requests == ["https://openrouter.ai/api/v1/models?output_modalities=all"]
    assert _MARKER not in json.dumps(result)
    assert credential_operations == []


def test_deleted_connection_after_approval_cannot_be_recreated(
    production_server: Any, tmp_path: Path,
) -> None:
    """A completed approval never recreates a connection deleted before resume."""
    server, _session, _authority = production_server
    registry = _seed(tmp_path)
    headers = _headers(server)
    effect = _prepare(server, headers)
    _approve(server, headers, effect)
    registry.delete("connection/work", expected_revision=1)
    before = registry.path.read_bytes()
    status, result = _post(server, headers, _SAVE, {
        "phase": "resume", "effect_id": effect["effect_id"],
    })
    assert status != 200 or result["data"]["state"] != "succeeded"
    assert registry.path.read_bytes() == before
    assert registry.snapshot()["providers"] == []


def _seed(tmp_path: Path) -> ProviderRegistry:
    """Create only synthetic connection state, with no credential material."""
    registry = ProviderRegistry("defaults", user_data_root=tmp_path / "user-data")
    registry.save(
        {
            "provider_instance_id": "connection/work",
            "adapter_id": "openai-compatible",
            "display_name": "Work",
            "endpoint": "https://openrouter.ai/api/v1",
            "credential_handle": _MARKER,
            "metadata": {"catalog_provider_id": "openrouter"},
        },
        expected_revision=0,
    )
    return registry


def _post(server: Any, headers: Mapping[str, str], path: str, body: dict) -> tuple:
    """Use the production finite HTTP transport with fresh request identity."""
    status, result, _ = _request(
        server,
        "POST",
        _contract("POST", path),
        body=body,
        headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    return status, result


def _headers(server: Any) -> dict[str, str]:
    """Authenticate through the same session/CSRF ceremony as production tests."""
    cookie, csrf, origin = _authenticate(server)
    return {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}


def _prepare(server: Any, headers: dict[str, str]) -> dict:
    """Freeze explicit empty denial as an actual pending approved mutation."""
    status, response = _post(
        server,
        headers,
        _SAVE,
        {
            "phase": "prepare",
            "effect_kind": "provider_model_access",
            "correlation_id": str(uuid.uuid4()),
            "request": {
                "profile_id": "defaults",
                "provider_instance_id": "connection/work",
                "expected_revision": 1,
                "model_access": {"version": VERSION, "mode": "explicit", "model_ids": []},
            },
        },
    )
    assert status == 200, response
    effect = response["data"]
    assert effect["state"] == "approval_pending", response
    assert _MARKER not in json.dumps(response)
    return effect


def _approve(server: Any, headers: dict[str, str], effect: dict) -> None:
    """Approve the exact durable request using the existing signed UI seam."""
    identifier = effect["approval_request_id"]
    status, detail = _post(
        server, headers, "/api/interactive-approval/v1/get", {"request_id": identifier}
    )
    assert status == 200, detail
    assert _MARKER not in json.dumps(detail)
    data = detail["data"]
    status, approved = _post(
        server,
        headers,
        "/api/interactive-approval/v1/approve",
        {
            "request_id": identifier,
            "confirmation_text": "EXECUTE",
            "ui_operator": sign_ui_operator(
                identifier,
                nonce=str(uuid.uuid4()),
                decision="approve",
                request_snapshot_digest=data["request_snapshot_digest"],
                typed_confirmation_digest=data["typed_confirmation_digest"],
            ),
        },
    )
    assert status == 200, approved


def test_captured_http_read_rejects_foreign_profile_and_forged_flags(
    production_server: Any,
    tmp_path: Path,
) -> None:
    """Read scope is captured; public approved flags never establish authority."""
    server, _session, _authority = production_server
    registry = _seed(tmp_path)
    headers = _headers(server)
    scope = {"profile_id": "defaults", "provider_instance_id": "connection/work"}
    before = registry.path.read_bytes()
    status, result = _post(server, headers, _READ, scope)
    assert status == 200, result
    assert result["data"]["profile_id"] == "defaults"
    assert result["data"]["provider_instance_id"] == "connection/work"
    assert result["data"]["registry_revision"] == 1
    assert result["data"]["model_access"] is None
    assert _MARKER not in json.dumps(result)
    for body in ({**scope, "profile_id": "foreign"}, {**scope, "approved": True}):
        status, rejected = _post(server, headers, _READ, body)
        assert status == 400, rejected
    status, rejected = _post(
        server,
        headers,
        _SAVE,
        {
            "phase": "prepare",
            "effect_kind": "provider_model_access",
            "approved": True,
            "request": {
                **scope,
                "expected_revision": 1,
                "model_access": {"version": VERSION, "mode": "all", "model_ids": []},
            },
        },
    )
    assert status == 400, rejected
    status, rejected = _post(
        server,
        headers,
        _SAVE,
        {
            "phase": "prepare",
            "effect_kind": "provider_model_access",
            "request": {
                **scope,
                "profile_id": "foreign",
                "expected_revision": 1,
                "model_access": {"version": VERSION, "mode": "all", "model_ids": []},
            },
        },
    )
    assert status == 400, rejected
    assert registry.path.read_bytes() == before


def test_model_access_requires_approval_and_resumes_once(
    production_server: Any,
    tmp_path: Path,
) -> None:
    """Real Broker approval writes empty deny policy without replacing a key."""
    server, _session, authority = production_server
    registry = _seed(tmp_path)
    original = registry.snapshot()["providers"][0]
    headers = _headers(server)
    effect = _prepare(server, headers)
    before = registry.path.read_bytes()
    status, pending = _post(
        server, headers, _SAVE, {"phase": "resume", "effect_id": effect["effect_id"]}
    )
    assert status != 200 or pending["data"]["state"] != "succeeded"
    assert registry.path.read_bytes() == before
    _approve(server, headers, effect)
    for _ in range(2):
        status, result = _post(
            server, headers, _SAVE, {"phase": "resume", "effect_id": effect["effect_id"]}
        )
        assert status == 200, result
        assert result["data"]["state"] == "succeeded", result
        assert _MARKER not in json.dumps(result)
        snapshot = registry.snapshot()
        assert snapshot["revision"] == 2
        record = snapshot["providers"][0]
        assert record["model_access"] == {"version": VERSION, "mode": "explicit", "model_ids": []}
        for key in ("credential_handle", "endpoint", "adapter_id", "metadata"):
            assert record[key] == original[key]
    assert not (tmp_path / "user-data/credentials/material-store/credentials.store.json").exists()
    assert _MARKER not in json.dumps(authority.audit_events(), default=str)


def test_approved_stale_model_access_cannot_overwrite_registry(
    production_server: Any,
    tmp_path: Path,
) -> None:
    """An intervening owner write invalidates the frozen approval's CAS."""
    server, _session, _authority = production_server
    registry = _seed(tmp_path)
    headers = _headers(server)
    effect = _prepare(server, headers)
    connection = registry.snapshot()["providers"][0]
    registry.save({**connection, "display_name": "Changed"}, expected_revision=1)
    before = registry.path.read_bytes()
    _approve(server, headers, effect)
    status, result = _post(
        server, headers, _SAVE, {"phase": "resume", "effect_id": effect["effect_id"]}
    )
    assert status != 200 or result["data"]["state"] != "succeeded"
    assert registry.path.read_bytes() == before
    assert "model_access" not in registry.snapshot()["providers"][0]
