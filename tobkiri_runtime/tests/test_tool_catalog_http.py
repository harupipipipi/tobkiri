"""The normal Defaults HTTP tool picker uses its captured Registry read edge."""

from pathlib import Path
import uuid

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from tests.test_production_frontend_contract_http import (
    _authenticate,
    _contract,
    _request,
    production_server as production_server,
)


def test_defaults_tools_catalog_is_authenticated_read_only_and_not_an_execution_grant(
    production_server,
    tmp_path: Path,
) -> None:
    server, session, _authority = production_server
    route = _contract("GET", "/api/tools/catalog")
    status, _body, _ = _request(server, "GET", route)
    assert status == 401
    cookie, _csrf, _origin = _authenticate(server)
    headers = {"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())}
    status, body, _ = _request(server, "GET", route, headers=headers)
    assert status == 200, body
    catalog = body["data"]
    assert catalog["count"] == len(catalog["tools"]) == 152
    assert catalog["registry_revision"] == 0
    by_id = {item["tool_id"]: item for item in catalog["tools"]}
    assert by_id["calculator"]["summary"] == "Basic arithmetic helper."
    assert by_id["artifact_file_read"]["summary"] == (
        "Read a file from the artifact workspace."
    )
    assert by_id["artifact_file_read"]["tool_id"] == "artifact_file_read"
    assert by_id["coding_file_write"]["action_class"] == "update"
    assert by_id["coding_file_write"]["minimum_permission"] == "confirm"
    assert by_id["settings_update"]["minimum_permission"] == "confirm"
    assert by_id["memo_note_upsert"]["tool_id"] == "memo_note_upsert"
    connected_ids = {
        "calculator", "coding_file_create", "coding_file_list", "coding_file_read",
        "coding_file_search", "file_reader", "chat_list_targets", "chat_resolve_target",
        "chat_send_message",
    }
    assert {item["tool_id"] for item in catalog["tools"] if (
        item["connection_status"] == "connected"
    )} == connected_ids
    assert all(
        item["connection_status"] == "unavailable"
        for item in catalog["tools"] if item["tool_id"] not in connected_ids
    )
    assert sum(item["tool_count"] for item in catalog["services"]) == 152
    local = "tobkiri.service.tool.local.operation.v1"
    metadata = session.provider_metadata(local)
    assert {(item["function_id"], item["operation_id"]) for item in metadata} == {
        ("rumi_default_tools_pack.calculator", "rumi_default_tools_pack.calculator-evaluate"),
        ("rumi_default_tools_pack.files-read", "rumi_default_tools_pack.files-read-operation"),
        ("rumi_default_tools_pack.file-create-tool", "rumi_default_tools_pack.file-create-operation"),
        ("rumi_default_tools_pack.chat-reference-tools", "rumi_default_tools_pack.chat-reference-operation"),
        ("rumi_default_tools_pack.chat-message-tool", "rumi_default_tools_pack.chat-message-operation"),
    }
    with pytest.raises(AuthorityDenied):
        session.invoke(local, "rumi_default_tools_pack.calculator-evaluate", {
            "tool_id": "calculator", "tool_call_id": "direct-call",
            "arguments": {"expression": "6*7"}, "_session_id": "catalog-reader",
        })
    assert not (tmp_path / "user-data/packs/rumi_tool_registry_pack").exists()
    for query in ("profile_id=foreign", "operation=save", "approved=true", "pack_id=foreign"):
        status, body, _ = _request(
            server,
            "GET",
            _contract("GET", f"/api/tools/catalog?{query}"),
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert 400 <= status < 500, body
    assert not (tmp_path / "user-data/packs/rumi_tool_registry_pack").exists()
