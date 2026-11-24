"""Validate the actual source intent's independent review edges."""

import json
from pathlib import Path
from core_runtime.authority.v4 import authority_digest
from tobkiri_protocol.profile_scope import normalize_requested_scope_template


def test_exact_host_reviewer_edge_and_normal_generator_semantics():
    runtime = Path(__file__).resolve().parents[1]
    source = runtime / "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"
    intent = json.loads(source.read_text())
    caller = "rumi_host_authority_bridge_pack.host-authority.approval-policy"
    edges = [edge for edge in intent["requested_edges"] if edge["caller_function_id"] == caller]
    expected = {
        ("tobkiri.service.ai.generate.v1", "rumi_ai_gateway_pack.ai-gateway.generate"),
        ("tobkiri.resource.ui.settings.v1", "tobkiri_ui_settings_pack.settings-read"),
        ("tobkiri.resource.ai.route.quote.v1", "rumi_ai_gateway_pack.ai-gateway.route-quote"),
    }
    assert {(edge["contract_id"], edge["operation_id"]) for edge in edges} == expected
    assert len(edges) == 3
    for edge in edges:
        template = edge["requested_scope_template"]
        assert edge["authority_mode"] == "profile_grant"
        assert "semantics_digest" not in template
        # The canonical normalizer fills the exact resolved semantic digest;
        # authoring source cannot select or spoof a different semantic owner.
        digest = authority_digest(
            {"contract": edge["contract_id"], "operation": edge["operation_id"]}
        )
        normalized = normalize_requested_scope_template(
            template,
            contract_id=edge["contract_id"],
            operation_id=edge["operation_id"],
            semantics_digest=digest,
        )
        assert normalized["semantics_digest"] == digest
        assert normalized["opaque"] is False
        assert normalized["exact_request_digest"] is None
        dimensions = normalized["dimensions"]
        assert dimensions["contract"] == [edge["contract_id"]]
        assert dimensions["operation"] == [edge["operation_id"]]
        if "ui.settings" not in edge["contract_id"]:
            assert dimensions["request_surface"] == ["approval-review"]
            assert dimensions["tool_calling"] == ["false"]
        if "ai.generate" in edge["contract_id"]:
            assert dimensions["allow_failover"] == ["false"]
