"""Prompt resource through real capture and Broker, using isolated owner data."""

import hashlib
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from ecosystem.rumi_prompt_studio_pack.runtime import resource_host as host
from ecosystem.rumi_prompt_studio_pack.runtime.store import PromptStudioStore
from tests.conformance_support.host_profile import captured_host_profile
from tobkiri_host.errors import ProviderExecutionError


@pytest.mark.parametrize("selected", [True, False])
def test_prompt_read_needs_the_exact_captured_edge(tmp_path: Path, monkeypatch, selected):
    store = PromptStudioStore("defaults", user_data_root=tmp_path / "user-data")
    store.save(
        "system", "Use the selected prompt.",
        expected_body_hash="sha256:" + hashlib.sha256(b"").hexdigest(),
    )
    before = store.path.read_bytes()
    edge = {
        "caller_function_id": "shell.tauri.default",
        "target_provider_id": host.FUNCTION_ID,
        "contract_id": host.CONTRACT_ID,
        "operation_id": host.OPERATION_ID,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {"contract": [host.CONTRACT_ID], "operation": [host.OPERATION_ID]},
            "quotas": {}, "exact_request_digest": None, "opaque": False,
        },
    }
    with captured_host_profile(
        tmp_path, monkeypatch, packs=(host.PACK_ID,),
        edges=(edge,) if selected else (), backends=(),
    ) as (session, _authority):
        payload = {"operation": "get", "prompt_id": "system", "_session_id": "prompt-read"}
        if not selected:
            with pytest.raises(AuthorityDenied):
                session.invoke(host.CONTRACT_ID, host.OPERATION_ID, payload)
        else:
            session.assert_operation_ready(host.CONTRACT_ID, host.OPERATION_ID)
            result = session.invoke(host.CONTRACT_ID, host.OPERATION_ID, payload)
            assert result["profile_id"] == "defaults"
            assert result["prompt"]["body"] == "Use the selected prompt."
            for patch in ({"profile_id": "other"}, {"approved": True}, {"operation": "save"}):
                with pytest.raises((AuthorityDenied, ProviderExecutionError)):
                    session.invoke(host.CONTRACT_ID, host.OPERATION_ID, {**payload, **patch})
    assert store.path.read_bytes() == before
