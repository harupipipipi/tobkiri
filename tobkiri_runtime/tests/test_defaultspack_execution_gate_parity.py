from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


PayloadFactory = Callable[[Path], dict[str, Any]]
Entrypoint = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _assert_approval_required(result: dict[str, Any], operation: str) -> None:
    assert result["status"] == "ok"
    data = result["data"]
    assert data["approval_required"] is True
    assert data["operation"] == operation
    assert data["approval_request_id"]
    assert data["args_hash"]


def _assert_invalid_approval(result: dict[str, Any]) -> None:
    assert result["status"] == "error"
    assert result["error"]["code"].startswith("APPROVAL_")
    assert result.get("_http_status") == 403


def _exercise_denial_contract(
    tmp_path: Path,
    entrypoint: Entrypoint,
    payload_factory: PayloadFactory,
    operation: str,
) -> None:
    missing_grant = entrypoint(payload_factory(tmp_path), {})
    _assert_approval_required(missing_grant, operation)

    forged_client_flag = entrypoint(
        {**payload_factory(tmp_path), "approved": True},
        {},
    )
    _assert_approval_required(forged_client_flag, operation)

    forged_token = entrypoint(
        {**payload_factory(tmp_path), "approval_token": "forged.token"},
        {},
    )
    _assert_invalid_approval(forged_token)


def _file_write_payload(tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    return {
        "workspace_id": "trusted",
        "path": "notes.txt",
        "content": "after\n",
    }


def _tool_create_payload(tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    return {
        "name": "issue665_contract_tool",
        "description": "issue 665 contract fixture",
        "parameters": {"type": "object", "properties": {}},
        "handler_code": "def run(args, context):\n    return {'status': 'ok'}\n",
    }


def _http_server_without_starting_listener(facade: object | None = None):
    from transport.http import DefaultsHttpServer

    server = object.__new__(DefaultsHttpServer)
    server.facade = facade
    return server


def test_coding_file_write_denial_parity_across_direct_and_function_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blocks.coding.file_write import run as block_file_write
    from domain.function_runtime.dispatcher import run_defaultspack_function
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    bind_verified_coding_contracts(monkeypatch, tmp_path)

    cases: list[Entrypoint] = [
        lambda payload, context: block_file_write(payload, context),
        lambda payload, context: run_defaultspack_function(
            "coding_file_write",
            payload,
            context,
        ),
    ]

    for entrypoint in cases:
        _exercise_denial_contract(
            tmp_path,
            entrypoint,
            _file_write_payload,
            "file.write",
        )


def test_tool_create_is_retired_across_direct_and_function_entrypoints(
    tmp_path: Path,
) -> None:
    from blocks.tool.create import run as block_tool_create
    from domain.function_runtime.dispatcher import run_defaultspack_function

    cases: list[Entrypoint] = [
        lambda payload, context: block_tool_create(payload, context),
        lambda payload, context: run_defaultspack_function(
            "tool_create",
            payload,
            context,
        ),
    ]

    for entrypoint in cases:
        result = entrypoint(_tool_create_payload(tmp_path), {})
        assert result["status"] == "error"
        assert result["error"]["code"] == "MIGRATION_REQUIRED"
        assert result["error"]["details"]["migration_required"] is True


def test_legacy_http_fallback_denies_forged_coding_write_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    bind_verified_coding_contracts(monkeypatch, tmp_path)
    server = _http_server_without_starting_listener(facade=None)

    def entrypoint(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        return server._invoke_fallback_block(
            "blocks.coding.file_write",
            payload,
            {},
        )

    _exercise_denial_contract(
        tmp_path,
        entrypoint,
        _file_write_payload,
        "file.write",
    )


def test_http_function_route_adapter_preserves_function_denial_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    from tests.legacy_authority_contracts import assert_retired_module_absent
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("domain.function_runtime.bridge")
    assert_payload_mutations_denied(harness(tmp_path))


def test_issue665_first_slice_documents_remaining_execution_route_gaps() -> None:
    # Issue #665 is broader than this first slice. These surfaces are
    # intentionally allowlisted here until focused route harnesses can exercise
    # them without live bearer secrets, external webhook signatures, or
    # bootstrap state:
    #
    # - mobile bearer API paths: need a token factory/test principal fixture.
    # - webhook/preauth routes: many are ingress/read/setup routes rather than
    #   direct write-like execution gates, and need per-provider signature
    #   fixtures before parity assertions are meaningful.
    # - bootstrap routes: need explicit route-by-route classification so safe
    #   idempotent bootstrap is not conflated with host execution.
    assert True


def test_legacy_tool_invoke_requires_a_capability_plan() -> None:
    from blocks.tool.invoke import run as invoke_tool

    result = invoke_tool(
        {"tool_name": "calculator", "arguments": {"expression": "1 + 1"}},
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"
