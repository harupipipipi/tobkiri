from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


RUMI_ROOT = Path(__file__).resolve().parents[1]


def test_rumi_api_function_subprocess_has_zero_legacy_routes():
    function_dir = RUMI_ROOT / "ecosystem" / "rumi_default_tools_pack" / "functions" / "rumi_api"
    runner = RUMI_ROOT / "core_runtime" / "function_runner.py"
    payload = {
        "module_path": str(function_dir / "main.py"),
        "callable_name": "run",
        "context": {"profile_id": "defaultspack.mimo_coding_company"},
        "args": {"action": "list_routes"},
    }

    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=str(function_dir),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    output = json.loads(result.stdout)
    assert output["status"] == "ok"
    assert output["data"]["count"] == 0
    assert output["data"]["routes"] == []
    assert output["data"]["dispatch"] == "captured_v4_qualified_operations_only"


class _CapturedSession:
    profile_id = "profile:defaults"
    plan_digest = "sha256:" + "1" * 64

    def __init__(self):
        self.calls = []

    def provider_metadata(self, contract_id):
        return ({"contract_id": contract_id},)

    def invoke(self, contract_id, operation_id, payload, *, version_range=">=1,<2"):
        self.calls.append((contract_id, operation_id, payload, version_range))
        return {"value": "captured"}


def test_rumi_api_dispatches_one_qualified_operation_through_captured_session():
    from ecosystem.rumi_default_tools_pack.domain.tool import rumi_api

    session = _CapturedSession()
    result = rumi_api.run(
        {
            "action": "dispatch",
            "contract_id": "company.messaging.v1",
            "operation_id": "channels.list",
            "payload": {"workspace_id": "workspace:alpha"},
        },
        {
            "_tool_server_approved": True,
            "principal_id": "defaultspack",
            "v4_dispatch_session": session,
        },
    )

    assert result == {
        "status": "ok",
        "data": {
            "profile_id": "profile:defaults",
            "result": {"value": "captured"},
        },
    }
    assert session.calls == [
        (
            "company.messaging.v1",
            "channels.list",
            {
                "workspace_id": "workspace:alpha",
                "_contract_consumer_pack_id": "rumi_default_tools_pack",
            },
            ">=1,<2",
        )
    ]


def test_rumi_api_rejects_legacy_http_and_missing_session():
    from ecosystem.rumi_default_tools_pack.domain.tool import rumi_api

    legacy = rumi_api.run(
        {"action": "request", "method": "GET", "path": "/api/health"},
        {"_tool_server_approved": True, "principal_id": "defaultspack"},
    )
    assert legacy["error"]["code"] == "LEGACY_HTTP_DISABLED"

    missing = rumi_api.run(
        {
            "action": "dispatch",
            "contract_id": "company.messaging.v1",
            "operation_id": "channels.list",
            "payload": {},
        },
        {"_tool_server_approved": True, "principal_id": "defaultspack"},
    )
    assert missing["error"]["code"] == "V4_DISPATCH_SESSION_REQUIRED"


def test_rumi_api_rejects_forged_consumer_identity():
    from ecosystem.rumi_default_tools_pack.domain.tool import rumi_api

    result = rumi_api.run(
        {
            "action": "dispatch",
            "contract_id": "company.messaging.v1",
            "operation_id": "channels.list",
            "payload": {"_contract_consumer_pack_id": "forged"},
        },
        {
            "_tool_server_approved": True,
            "principal_id": "defaultspack",
            "v4_dispatch_session": _CapturedSession(),
        },
    )
    assert result["error"]["code"] == "FORGED_CONSUMER_IDENTITY"
