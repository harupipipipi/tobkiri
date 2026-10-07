"""Real admitted test Packs, named Profile, Broker and approval integration.

Only the PackVM transport is replaced by a verified-byte subprocess adapter.
This is not a claim of VM containment, native UI, or external-provider success.
"""
from __future__ import annotations

import copy
import pytest
from pathlib import Path

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile, prepare_default_profile_confirmation,
    capture_profile, prepare_profile_confirmation,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.qa_flow_composition import PACKS, admit_test_packs, CompositionChildBackend
from tests.test_external_pack_catalog_v4 import _capture_control_session, _invoke as control_invoke
from tests.test_mcp_host_broker import _edge
from tests.test_voice_workflow_e2e_v4 import _invoke, _run_step
from tests.test_workflow_v4_pack_control_integration import _capture_defaultspack_dispatch
from tobkiri_host.backends import BackendRegistry

ROOT = Path(__file__).resolve().parents[1]
VALUE = {"text": "hello packs", "count": 4, "items": [1, 2, 3], "flag": True, "empty": None}


def test_three_independent_authored_packs_execute_under_named_profile(tmp_path, monkeypatch):
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    admit_test_packs(tmp_path)
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    control = _capture_control_session()
    for pack_id in PACKS:
        assert control_invoke(control, "pack.install", {"pack_id": pack_id})["installed"]
        candidate = control_invoke(control, "approval.candidate", {"pack_id": pack_id})
        assert control_invoke(control, "approval.approve", {
            "pack_id": pack_id, "candidate_id": candidate["candidate_id"],
        })["approved"]
        assert control_invoke(control, "pack.enable", {"pack_id": pack_id})["enabled"]
    definitions = ProfileDefinitionStore(user_data)
    profile = copy.deepcopy(dict(definitions.get_profile("defaults").profile))
    selected = {item["pack_id"] for item in profile["packs"]}
    profile["packs"].extend({"pack_id": pack, "artifact_digest": None, "role": "provider"}
                            for pack in PACKS if pack not in selected)
    profile["requested_edges"] = [
        item for item in profile["requested_edges"]
        if item["caller_function_id"] != "tobkiri.workflow.provider"
    ] + [_edge("tobkiri.workflow.provider", pack + ".provide", pack + ".v1", op)
         for pack, op in PACKS.items()]
    for operation in ("run.pause", "run.cancel"):
        if not any(item["caller_function_id"] == "shell.tauri.default"
                   and item["contract_id"] == "tobkiri.workflow.v4"
                   and item["operation_id"] == operation for item in profile["requested_edges"]):
            profile["requested_edges"].append(_edge("shell.tauri.default", "tobkiri.workflow.provider", "tobkiri.workflow.v4", operation))
    definitions.create_profile(profile, profile_id="flowqa", display_name="Independent Flow QA")
    active = capture_profile("flowqa", confirmation=prepare_profile_confirmation("flowqa"))
    authority = AuthorityStore(user_data / "authority/v4.sqlite3")
    backend = CompositionChildBackend(tmp_path / "children")
    session = _capture_defaultspack_dispatch(
        active, bundle_root=packaged_profile_bundle_root(), ecosystem_root=ROOT / "ecosystem",
        authority_store=authority, backends=BackendRegistry((backend,)),
    )
    try:
        # Selecting a Pack does not grant the Shell its provider operation.
        with pytest.raises(Exception):
            session.invoke("qa.flow.sink.v1", "collect", VALUE)
        assert backend.calls == []
        palette = _invoke(session, "operation.palette", {})
        operations = palette["operations"]
        targets = [next(item for item in operations if item["contract_id"] == pack + ".v1")
                   for pack in PACKS]
        steps = []
        for i, target in enumerate(targets):
            steps.append({
                "id": f"node{i}", "depends_on": [f"node{i-1}"] if i else [],
                "request": {
                    **{key: target[key] for key in ("contract_id", "contract_revision_digest", "operation_id", "function_principal_id")},
                    "input": {key: f"${{steps.node{i-1}.output.{key}}}" if i else value
                              for key, value in VALUE.items()},
                },
            })
        document = {"workflow_api_version": "io.tobkiri.workflow.v4", "name": "Independent Packs", "steps": steps}
        checked = _invoke(session, "definition.validate", {"document": document})
        assert not checked.get("errors"), checked
        draft = _invoke(session, "definition.create", {"definition_id": "flow.qa", "document": document})
        _invoke(session, "definition.publish", {"definition_id": "flow.qa", "if_match": draft["etag"]})
        _invoke(session, "run.create", {"definition_id": "flow.qa", "run_id": "composition-1", "inputs": {}})
        for i in range(3):
            result = _run_step(session, "composition-1", f"node{i}")
            assert result["state"] == "succeeded", result
        assert result["outcome"] == {**VALUE, "text": "HELLO PACKS", "count": 5}
        assert [call["contract"] for call in backend.calls] == [pack + ".v1" for pack in PACKS]
        assert _invoke(session, "run.get", {"run_id": "composition-1"})["run"]["state"] == "succeeded"

        # An integer output cannot enter a string input, even through a reference.
        bad = copy.deepcopy(document)
        bad["steps"][1]["request"]["input"]["text"] = "${steps.node0.output.count}"
        draft = _invoke(session, "definition.create", {"definition_id": "flow.bad", "document": bad})
        _invoke(session, "definition.publish", {"definition_id": "flow.bad", "if_match": draft["etag"]})
        _invoke(session, "run.create", {"definition_id": "flow.bad", "run_id": "bad-binding", "inputs": {}})
        assert _run_step(session, "bad-binding", "node0")["state"] == "succeeded"
        before = len(backend.calls)
        with pytest.raises(Exception):
            _run_step(session, "bad-binding", "node1")
        assert len(backend.calls) == before
        assert _invoke(session, "run.get", {"run_id": "bad-binding"})["run"]["state"] == "failed"
        assert all(item["step_id"] == "node0" for item in _invoke(session, "run.get", {"run_id": "bad-binding"})["attempts"])

        # Cancel a real pending approval before it can dispatch child code.
        _invoke(session, "run.create", {"definition_id": "flow.qa", "run_id": "cancel-pending", "inputs": {}})
        _invoke(session, "run.advance", {"run_id": "cancel-pending"})
        waiting = _invoke(session, "run.get", {"run_id": "cancel-pending"})["attempts"][0]
        assert waiting["state"] == "waiting_approval"
        before = len(backend.calls)
        _invoke(session, "run.cancel", {"run_id": "cancel-pending"})
        assert _invoke(session, "run.get", {"run_id": "cancel-pending"})["run"]["state"] == "cancelled"
        with pytest.raises(Exception):
            _invoke(session, "run.step.resume", {"run_id": "cancel-pending", "step_id": "node0"})
        assert len(backend.calls) == before

        # Pause after one successful Pack, rebuild the production capture, and
        # resume remaining Packs from the durable Profile-owned records.
        _invoke(session, "run.create", {"definition_id": "flow.qa", "run_id": "restart", "inputs": {}})
        assert _run_step(session, "restart", "node0")["state"] == "succeeded"
        before_replay = len(backend.calls)
        with pytest.raises(Exception):
            _invoke(session, "run.step.resume", {"run_id": "restart", "step_id": "node0"})
        assert len(backend.calls) == before_replay
        _invoke(session, "run.pause", {"run_id": "restart"})
        calls = backend.calls
        source_calls = sum(item["contract"] == "qa.flow.source.v1" for item in calls)
        session.close()
        authority.close()
        authority = AuthorityStore(user_data / "authority/v4.sqlite3")
        backend = CompositionChildBackend(tmp_path / "children-restarted")
        backend.calls = calls
        session = _capture_defaultspack_dispatch(
            capture_profile("flowqa"), bundle_root=packaged_profile_bundle_root(),
            ecosystem_root=ROOT / "ecosystem", authority_store=authority,
            backends=BackendRegistry((backend,)),
        )
        assert _invoke(session, "run.get", {"run_id": "restart"})["run"]["state"] == "paused"
        _invoke(session, "run.resume", {"run_id": "restart"})
        for i in (1, 2):
            assert _run_step(session, "restart", f"node{i}")["state"] == "succeeded"
        assert sum(item["contract"] == "qa.flow.source.v1" for item in backend.calls) == source_calls
        assert _invoke(session, "run.get", {"run_id": "restart"})["run"]["state"] == "succeeded"
    finally:
        session.close()
        authority.close()


def test_admitted_inventory_is_scoped_and_does_not_select_packs(tmp_path, monkeypatch):
    from core_runtime.bootstrap.profile_capture import host_profile_catalog
    from core_runtime.external_pack_catalog_v4 import ExternalPackCatalogDenied
    from core_runtime.external_pack_catalog_v4 import resolve_admitted_pack_root

    host_a = tmp_path / "host-a"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(host_a))
    monkeypatch.setenv("RUMI_USER_DATA", str(host_a))
    initial = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    admit_test_packs(tmp_path)
    catalog = host_profile_catalog()
    assert set(PACKS).issubset(catalog.packs)
    current = capture_default_profile()
    assert current.resolved.plan == initial.resolved.plan
    assert current.resolved.lock == initial.resolved.lock
    assert not set(PACKS).intersection(item["pack_id"] for item in current.resolved.profile["packs"])
    other = host_profile_catalog(user_data_root=tmp_path / "host-b")
    assert not set(PACKS).intersection(other.packs)
    assert set(PACKS).issubset(host_profile_catalog().packs)

    definitions = ProfileDefinitionStore(host_a)
    invalid = copy.deepcopy(dict(definitions.get_profile("defaults").profile))
    invalid["packs"].append({"pack_id": "qa.absent", "artifact_digest": None, "role": "provider"})
    definitions.create_profile(invalid, profile_id="badqa", display_name="Unavailable test Pack")
    assert "qa.absent" not in host_profile_catalog().packs
    assert capture_default_profile().resolved.plan == initial.resolved.plan
    with pytest.raises(Exception, match="exact inventory"):
        prepare_profile_confirmation("badqa")

    # The inventory must not silently omit a tampered admitted Pack.
    root = resolve_admitted_pack_root("qa.flow.source")
    implementation = root / "runtime/invoke.py"
    original = implementation.read_bytes()
    original_mode = implementation.stat().st_mode & 0o777
    implementation.chmod(0o600)
    implementation.write_bytes(original + b"\n# tampered test bytes\n")
    try:
        with pytest.raises(ExternalPackCatalogDenied, match="digest"):
            host_profile_catalog()
    finally:
        implementation.write_bytes(original)
        implementation.chmod(original_mode)
