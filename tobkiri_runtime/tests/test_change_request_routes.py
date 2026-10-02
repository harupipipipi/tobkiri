from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _has_change_request_backend() -> bool:
    for module_name in ("blocks.change_requests", "domain.change_request.store"):
        try:
            if importlib.util.find_spec(module_name) is not None:
                return True
        except ModuleNotFoundError:
            continue
    block_dir = DEFAULTSPACK_ROOT / "blocks" / "change_request"
    if any(block_dir.glob("*.py")):
        return True
    return False


def test_change_request_api_requires_captured_list_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/change-requests",
        "tobkiri.change-request.v1",
        "defaultspack.change-request.list",
    )


def test_change_request_routes_are_sensitive_local_routes_with_origin_and_csrf_checks():
    from domain.safety.local_guard import is_sensitive_coding_path, require_local_guard

    assert is_sensitive_coding_path("/api/change-requests", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test", "PATCH") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/refresh", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/export-patch", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/comments", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/comments", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/comments/comment_1", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/comments/comment_1", "PATCH") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/decision", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/viewed-files", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/viewed-files", "PATCH") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/viewed-files", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/checks", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/checks", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/checks/check_1", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/checks/run", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/checks/run-check", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/run-check", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/checks/run", "GET") is False
    assert is_sensitive_coding_path("/api/change-requests/cr_test/seal", "GET") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/commit", "POST") is True
    assert is_sensitive_coding_path("/api/change-requests/cr_test/commit", "GET") is False

    assert require_local_guard(
        "/api/change-requests/cr_test/seal",
        "GET",
        {"Origin": "https://example.test"},
        ("127.0.0.1", 54321),
    ) == (403, "origin not allowed for sensitive local route", "ORIGIN_DENIED")
    assert require_local_guard(
        "/api/change-requests/cr_test/export-patch",
        "POST",
        {"Origin": "http://localhost:8766"},
        ("127.0.0.1", 54321),
    ) == (403, "CSRF header required for sensitive local mutation", "CSRF_REQUIRED")
    assert require_local_guard(
        "/api/change-requests/cr_test",
        "PATCH",
        {"Origin": "http://localhost:8766"},
        ("127.0.0.1", 54321),
    ) == (403, "CSRF header required for sensitive local mutation", "CSRF_REQUIRED")
    assert require_local_guard(
        "/api/change-requests/cr_test/commit",
        "POST",
        {"Origin": "http://localhost:8766"},
        ("127.0.0.1", 54321),
    ) == (403, "CSRF header required for sensitive local mutation", "CSRF_REQUIRED")
    assert require_local_guard(
        "/api/change-requests/cr_test/export-patch",
        "POST",
        {"Origin": "http://localhost:8766", "X-Rumi-CSRF": "1"},
        ("127.0.0.1", 54321),
    ) is None


def test_change_request_commit_requires_captured_approved_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/change-requests/cr-1/commit",
        "tobkiri.change-request.v1",
        "defaultspack.change-request.commit",
    )


def test_change_request_setup_commit_route_is_default_off_and_flagged(monkeypatch):
    if not _has_change_request_backend():
        pytest.skip("change_request backend implementation is not present yet")

    from ecosystem.defaultspack.blocks.change_request import setup

    class Registry:
        def __init__(self) -> None:
            self.routes = []

        def register(self, _kind, value, meta=None):
            self.routes.append((value["method"], value["pattern"]))

    monkeypatch.delenv("RUMI_REVIEW_ENABLE_COMMIT", raising=False)
    registry = Registry()
    result = setup.run({"interface_registry": registry})
    assert ("POST", "/api/change-requests/{id}/commit") not in registry.routes
    assert "/api/change-requests/{id}/commit" not in result["registered"]

    monkeypatch.setenv("RUMI_REVIEW_ENABLE_COMMIT", "1")
    registry = Registry()
    result = setup.run({"interface_registry": registry})
    assert ("POST", "/api/change-requests/{id}/commit") in registry.routes
    assert "/api/change-requests/{id}/commit" in result["registered"]


def test_change_request_function_ids_are_registered_when_backend_exists(monkeypatch):
    if not _has_change_request_backend():
        pytest.skip("change_request backend implementation is not present yet")

    monkeypatch.delenv("RUMI_REVIEW_ENABLE_COMMIT", raising=False)
    from domain.function_runtime.registry import block_module_for

    assert block_module_for("coding_change_request_list") == "blocks.change_request.collection"
    assert block_module_for("coding_change_request_comment") == "blocks.change_request.comments"
    assert block_module_for("coding_change_request_run_check") == "blocks.change_request.checks"
    assert block_module_for("coding_change_request_commit") is None
    assert block_module_for("coding_change_request_export_patch") == "blocks.change_request.export_patch"


def test_change_request_commit_function_is_default_off_and_flagged(monkeypatch):
    if not _has_change_request_backend():
        pytest.skip("change_request backend implementation is not present yet")

    import domain.function_runtime.manifest_factory as manifest_factory
    import domain.function_runtime.registry as registry

    try:
        monkeypatch.delenv("RUMI_REVIEW_ENABLE_COMMIT", raising=False)
        importlib.reload(manifest_factory)
        importlib.reload(registry)
        assert registry.block_module_for("coding_change_request_commit") is None
        assert "coding_change_request_commit" not in manifest_factory.FUNCTION_SPECS_BY_ID

        monkeypatch.setenv("RUMI_REVIEW_ENABLE_COMMIT", "1")
        importlib.reload(manifest_factory)
        importlib.reload(registry)
        assert registry.block_module_for("coding_change_request_commit") == "blocks.change_request.commit"
        assert "coding_change_request_commit" in manifest_factory.FUNCTION_SPECS_BY_ID
    finally:
        monkeypatch.delenv("RUMI_REVIEW_ENABLE_COMMIT", raising=False)
        importlib.reload(manifest_factory)
        importlib.reload(registry)


def test_change_request_commit_function_bridge_registration_is_default_off_and_flagged(monkeypatch):
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )

    assert_retired_module_absent("domain.function_runtime.bridge")
    assert_profile_resolver_requires_authority_snapshot()
