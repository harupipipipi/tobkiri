"""Regression coverage for the bounded UI bootstrap readiness contract."""

from __future__ import annotations

import http.client
import hmac
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from core_runtime.frontend_contract_routes import (
    FrontendContractBinding,
    FrontendContractTarget,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from core_runtime.ui_readiness import (
    REQUIRED_UI_READINESS_PROBES,
    ProbeOutcome,
    ProbeSpec,
    UIReadinessChecker,
    build_ui_readiness_checker,
    defaultspack_ui_web_mounts,
    desktop_health_response_proof,
    ui_readiness_request_proof,
    ui_readiness_response_proof,
)


_ROUTES = (
    ("GET", "/api/ui/catalog"),
    ("GET", "/api/runtime-surface/settings"),
    ("GET", "/api/ai/profiles"),
    ("GET", "/api/tools/catalog"),
    ("GET", "/api/chat/conversations"),
    ("GET", "/api/chat/default-conversation"),
)


class _Dispatch:
    profile_id = "defaults"
    plan_digest = "sha256:" + "a" * 64

    def __init__(self) -> None:
        self.invocations: list[tuple[str, str, dict[str, object]]] = []

    def assert_current(self) -> None:
        return None

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        assert contract_id == f"contract.{operation_id}"

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, object],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, object]:
        del version_range
        self.invocations.append((contract_id, operation_id, dict(payload)))
        return {"state": "ready", "operation_id": operation_id}


def _bindings() -> tuple[FrontendContractBinding, ...]:
    bindings = []
    for index, (method, path) in enumerate(_ROUTES):
        operation_id = f"bootstrap.read.{index}"
        bindings.append(
            FrontendContractBinding(
                method=method,
                path=path,
                presentation="identity",
                targets=(
                    FrontendContractTarget(
                        contribution_id=f"bootstrap.{index}",
                        contract_id=f"contract.{operation_id}",
                        operation_id=operation_id,
                        provider_id=f"provider.{index}",
                        function_id=f"provider.{index}",
                    ),
                ),
            )
        )
    return tuple(bindings)


def _mounts(root: Path) -> tuple[dict[str, object], ...]:
    root.mkdir(parents=True)
    (root / "shell.html").write_text(
        "<!doctype html><link rel='stylesheet' href='/static/app.css'>"
        "<div id='root'></div><script type='module' src='/static/app.js'></script>",
        encoding="utf-8",
    )
    (root / "app.css").write_text("body {}", encoding="utf-8")
    (root / "app.js").write_text("export {};", encoding="utf-8")
    return (
        {
            "path_prefix": "/chat",
            "web_root": root,
            "spa_fallback": True,
            "index_file": "shell.html",
            "auth_required": True,
        },
        {
            "path_prefix": "/static",
            "web_root": root,
            "spa_fallback": False,
            "index_file": "shell.html",
            "auth_required": True,
        },
    )


def test_complete_bootstrap_contract_reports_every_named_probe_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core_runtime.ui_readiness.host_contract_value",
        lambda key: "desktop-bootstrap" if key == "panel_bootstrap_secret" else "",
    )
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    dispatch = _Dispatch()
    bindings = _bindings()
    checker = build_ui_readiness_checker(
        dispatch_session=dispatch,
        contract_routes={(item.method, item.path): item for item in bindings},
        web_mounts=_mounts(tmp_path / "ui"),
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        timeout_seconds=0.5,
    )

    snapshot = checker.snapshot(force=True)

    assert snapshot["status"] == "UP"
    assert snapshot["ready"] is True
    assert tuple(snapshot["probes"]) == REQUIRED_UI_READINESS_PROBES
    assert all(item["status"] == "UP" for item in snapshot["probes"].values())
    assert len(dispatch.invocations) == 6
    assert all(call[2] == {"_session_id": "ui-readiness"} for call in dispatch.invocations)
    assert dispatch.invocations[-1][1] == "bootstrap.read.5"
    assert snapshot["profile_id"] == "defaults"
    assert snapshot["plan_digest"] == _Dispatch.plan_digest
    assert str(snapshot["contract_map_digest"]).startswith("sha256:")


def test_missing_bootstrap_dependency_is_down_with_its_named_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core_runtime.ui_readiness.host_contract_value",
        lambda key: "desktop-bootstrap" if key == "panel_bootstrap_secret" else "",
    )
    bindings = tuple(item for item in _bindings() if item.path != "/api/tools/catalog")
    checker = build_ui_readiness_checker(
        dispatch_session=_Dispatch(),
        contract_routes={(item.method, item.path): item for item in bindings},
        web_mounts=_mounts(tmp_path / "ui"),
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        timeout_seconds=0.5,
    )

    snapshot = checker.snapshot(force=True)

    assert snapshot["status"] == "DOWN"
    assert snapshot["ready"] is False
    assert snapshot["probes"]["tool_catalog"] == {
        "status": "DOWN",
        "code": "BOOTSTRAP_ROUTE_MISSING",
        "message": "GET /api/tools/catalog is not captured",
        "duration_ms": snapshot["probes"]["tool_catalog"]["duration_ms"],
    }


def test_missing_referenced_module_is_a_named_static_bundle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core_runtime.ui_readiness.host_contract_value",
        lambda key: "desktop-bootstrap" if key == "panel_bootstrap_secret" else "",
    )
    mounts = _mounts(tmp_path / "ui")
    (tmp_path / "ui" / "app.js").unlink()
    bindings = _bindings()
    checker = build_ui_readiness_checker(
        dispatch_session=_Dispatch(),
        contract_routes={(item.method, item.path): item for item in bindings},
        web_mounts=mounts,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        timeout_seconds=0.5,
    )

    snapshot = checker.snapshot(force=True)

    assert snapshot["status"] == "DOWN"
    assert snapshot["probes"]["static_bundle"]["code"] == ("STATIC_BUNDLE_ASSETS_MISSING")


def test_stale_capture_fails_each_contract_probe_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StaleDispatch(_Dispatch):
        def assert_current(self) -> None:
            raise RuntimeError("test stale capture detail must not be returned")

    monkeypatch.setattr(
        "core_runtime.ui_readiness.host_contract_value",
        lambda key: "desktop-bootstrap" if key == "panel_bootstrap_secret" else "",
    )
    bindings = _bindings()
    checker = build_ui_readiness_checker(
        dispatch_session=_StaleDispatch(),
        contract_routes={(item.method, item.path): item for item in bindings},
        web_mounts=_mounts(tmp_path / "ui"),
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        timeout_seconds=0.5,
    )

    snapshot = checker.snapshot(force=True)

    assert snapshot["status"] == "DOWN"
    assert snapshot["probes"]["ui_catalog"]["code"] == "STALE_RESOLUTION"
    assert "test stale capture" not in json.dumps(snapshot)


def test_issue_625_hanging_bootstrap_probe_is_bounded_and_not_spawned_again() -> None:
    release = threading.Event()
    calls = 0

    def hanging_probe() -> ProbeOutcome:
        nonlocal calls
        calls += 1
        release.wait(5)
        return ProbeOutcome()

    checker = UIReadinessChecker(
        (ProbeSpec("ui_catalog", hanging_probe),),
        timeout_seconds=0.05,
        cache_seconds=0,
    )
    started = time.monotonic()
    first = checker.snapshot(force=True)
    elapsed = time.monotonic() - started
    second = checker.snapshot(force=True)
    release.set()

    assert elapsed < 0.5
    assert first["status"] == "DOWN"
    assert first["probes"]["ui_catalog"]["code"] == "PROBE_TIMEOUT"
    assert second["probes"]["ui_catalog"]["code"] == "PROBE_STILL_RUNNING"
    assert calls == 1


def test_profile_reconfirmation_allows_only_the_authenticated_recovery_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))

    class _RecoveryLifecycle:
        def check_setup_status(self) -> dict[str, object]:
            return {"needs_setup": True}

        def get_health(self) -> dict[str, object]:
            return {"runtime_status": "profile_reconfirmation_required"}

    monkeypatch.setattr(
        "core_runtime.ui_readiness.host_contract_value",
        lambda key: "desktop-bootstrap" if key == "panel_bootstrap_secret" else "",
    )
    checker = build_ui_readiness_checker(
        dispatch_session=None,
        contract_routes={},
        web_mounts=_mounts(tmp_path / "ui"),
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        timeout_seconds=0.5,
    )
    server = PackAPIServer(
        port=0,
        app_lifecycle_manager=_RecoveryLifecycle(),
        ui_readiness_checker=checker,
    )

    snapshot = server.ui_readiness_snapshot(force=True)

    assert snapshot["status"] == "DEGRADED"
    assert snapshot["ready"] is True
    assert snapshot["mode"] == "profile_reconfirmation_required"
    assert snapshot["probes"]["static_bundle"]["status"] == "UP"
    assert snapshot["probes"]["chat_route"]["status"] == "UP"
    assert snapshot["probes"]["auth_session"]["status"] == "UP"
    assert snapshot["probes"]["conversation_bootstrap"]["status"] == "DOWN"


def test_http_health_stays_liveness_only_and_readiness_requires_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    calls = 0

    def catalog_probe() -> ProbeOutcome:
        nonlocal calls
        calls += 1
        return ProbeOutcome(
            status="DOWN",
            code="CATALOG_UNAVAILABLE",
            message="catalog is unavailable",
        )

    checker = UIReadinessChecker(
        (
            ProbeSpec(
                "ui_catalog",
                catalog_probe,
            ),
        ),
        timeout_seconds=0.2,
    )
    monkeypatch.setattr(
        "core_runtime.pack_api_server.host_contract_value",
        lambda key: "desktop-bootstrap" if key == "panel_bootstrap_secret" else "",
    )
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    login_code = manager.issue_login_code()["code"]
    panel_session = manager.exchange_code(str(login_code))
    assert panel_session is not None
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        ui_readiness_checker=checker,
    )
    server.start()
    try:
        unauthorized_status, _unauthorized = _get_json(
            server.port,
            "/ui-readiness",
            expected_status=401,
        )
        challenge = "test-readiness-challenge"
        forged_challenge = "oracle-forgery"
        _health_status, signed_health = _get_json(
            server.port,
            "/health",
            headers={
                "X-Rumi-Desktop-Health-Challenge": (
                    f"ui-readiness-request:{forged_challenge}"
                )
            },
        )
        forged_authorization = signed_health["data"]["desktop_challenge_response"]
        forged_status, _forged = _get_json(
            server.port,
            "/ui-readiness",
            headers={
                "X-Rumi-Desktop-Health-Challenge": forged_challenge,
                "X-Tobkiri-UI-Readiness-Authorization": forged_authorization,
            },
            expected_status=401,
        )
        readiness_status, readiness = _get_json(
            server.port,
            "/ui-readiness",
            headers={
                "X-Rumi-Desktop-Health-Challenge": challenge,
                "X-Tobkiri-UI-Readiness-Authorization": (
                    ui_readiness_request_proof(
                        "desktop-bootstrap",
                        challenge,
                    )
                ),
            },
        )
        panel_status, panel_readiness = _get_json(
            server.port,
            "/ui-readiness",
            headers={"Cookie": f"rumi_panel_session={panel_session['session_id']}"},
        )
        health_status, health = _get_json(server.port, "/health")
    finally:
        server.stop()

    assert unauthorized_status == 401
    assert forged_status == 401
    assert signed_health["data"]["desktop_challenge_response"] == (
        desktop_health_response_proof(
            "desktop-bootstrap",
            f"ui-readiness-request:{forged_challenge}",
        )
    )
    assert readiness_status == 200
    assert readiness["data"]["status"] == "DOWN"
    assert readiness["data"]["probes"]["ui_catalog"]["code"] == "CATALOG_UNAVAILABLE"
    assert hmac.compare_digest(
        readiness["data"]["desktop_challenge_response"],
        ui_readiness_response_proof("desktop-bootstrap", challenge),
    )
    assert panel_status == 200
    assert panel_readiness["data"]["status"] == "DOWN"
    assert "desktop_challenge_response" not in panel_readiness["data"]
    assert health_status == 200
    assert health["data"]["status"] == "ok"
    assert "ui_readiness" not in health["data"]
    assert calls == 1


def test_production_contract_map_names_every_unpublished_bootstrap_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = Path(__file__).resolve().parents[1]
    contract_map = json.loads(
        (
            runtime_root
            / "ecosystem"
            / "defaultspack"
            / "defaultspack"
            / "frontend_contract_map.v4.json"
        ).read_text(encoding="utf-8")
    )
    routes = {
        (str(item["method"]), str(item["path"]))
        for item in contract_map["routes"]
    }

    published = {route for route in _ROUTES if route in routes}
    assert published == {
        ("GET", "/api/ui/catalog"),
        ("GET", "/api/runtime-surface/settings"),
    }
    mounts = defaultspack_ui_web_mounts()
    assert {mount["path_prefix"] for mount in mounts} == {"/chat", "/static"}
    assert all(
        (mount["web_root"] / mount["index_file"]).is_file()
        for mount in mounts
    )
    monkeypatch.setattr(
        "core_runtime.ui_readiness.host_contract_value",
        lambda key: "desktop-bootstrap" if key == "panel_bootstrap_secret" else "",
    )
    bindings = tuple(
        binding
        for binding in _bindings()
        if (binding.method, binding.path) in routes
    )
    checker = build_ui_readiness_checker(
        dispatch_session=_Dispatch(),
        contract_routes={(item.method, item.path): item for item in bindings},
        web_mounts=mounts,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        timeout_seconds=0.5,
    )

    snapshot = checker.snapshot(force=True)

    assert snapshot["status"] == "DOWN"
    assert snapshot["probes"]["ui_catalog"]["status"] == "UP"
    assert snapshot["probes"]["settings"]["status"] == "UP"
    for name in (
        "model_catalog",
        "tool_catalog",
        "conversation_bootstrap",
        "default_conversation_load",
    ):
        assert snapshot["probes"][name]["code"] == "BOOTSTRAP_ROUTE_MISSING"


def _get_json(
    port: int,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    expected_status: int = 200,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", path, headers=dict(headers or {}))
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    assert response.status == expected_status
    return response.status, payload
