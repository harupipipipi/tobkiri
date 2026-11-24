"""Generic numeric control-journal and real loopback HTTP fixture regressions.

Provisional overlay proof only. Real handler, auth, replay journal and sockets;
fixture provider dispatch, no packaged generation, remote network, or model calls.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote

import pytest

from core_runtime.control_reconciliation_v4 import (
    ControlReconciliationError, ControlReconciliationStore,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from tests.test_pack_api_server import (
    _Dispatch, _panel_session, _request, FrontendContractBinding, HTTPContractTarget,
)
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.data_codec import digest_payload, digest_record
from tests.test_numeric_workflow_paths import VALUES, TAG_OBJECT, assert_float_bits

REQUEST_ID = "21212121-2121-4212-8212-212121212121"
CONTRACT_ID = "example.generic.numeric.v1"


def begin(store: ControlReconciliationStore, request_digest: str | None = None) -> Any:
    """Reserve the same generic exact request for replay assertions."""
    return store.begin_operation(
        request_id=REQUEST_ID, session_id="session-a", operation_id="number.echo",
        contract_id=CONTRACT_ID,
        request_digest=request_digest or canonical_digest({"request": "exact"}),
    )


@pytest.mark.parametrize("value", VALUES, ids=["fraction", "negative-zero", "subnormal"])
def test_generic_numeric_result_roundtrips_restarts_and_replays_exactly(tmp_path: Path, value: float) -> None:
    path = tmp_path / "control" / "reconciliation.sqlite3"
    store = ControlReconciliationStore(path, instance_id="first")
    result = {"state": "ok", "value": value, "tag_object": deepcopy(TAG_OBJECT)}
    try:
        _, created = begin(store)
        assert created
        finished = store.finish_operation(REQUEST_ID, session_id="session-a", state="succeeded", result=result)
        assert_float_bits(finished["result"]["value"], value)
        assert finished["result_digest"] == digest_payload(result)
        assert store.finish_operation(REQUEST_ID, session_id="session-a", state="succeeded", result=result) == finished
    finally:
        store.close()
    restarted = ControlReconciliationStore(path, instance_id="restarted")
    try:
        record = restarted.operation_status(REQUEST_ID, session_id="session-a")
        replay, created = begin(restarted)
        assert not created
        assert record == replay == finished
        assert_float_bits(replay["result"]["value"], value)
        assert replay["result"]["tag_object"] == TAG_OBJECT
        with pytest.raises(ControlReconciliationError, match="binding changed"):
            begin(restarted, request_digest=canonical_digest({"request": "different"}))
    finally:
        restarted.close()


def test_legacy_canonical_control_result_and_outer_request_hashes_stay_identical(tmp_path: Path) -> None:
    result = {"state": "ok", "value": 7, "nested": [None, True, "text"]}
    request = {"method": "POST", "path": "/api/test/numeric", "contract_id": CONTRACT_ID, "operation_id": "number.echo", "payload": {"value": 7}}
    assert digest_record(request, value_paths=(("payload",),)) == canonical_digest(request)
    store = ControlReconciliationStore(tmp_path / "control.sqlite3")
    try:
        begin(store)
        finished = store.finish_operation(REQUEST_ID, session_id="session-a", state="succeeded", result=result)
        assert finished["result_digest"] == canonical_digest(result)
    finally:
        store.close()


@pytest.mark.parametrize("tamper", ["fraction", "negative-zero-sign"])
def test_control_result_tamper_is_rejected_on_restart(tmp_path: Path, tamper: str) -> None:
    path = tmp_path / "control.sqlite3"
    store = ControlReconciliationStore(path)
    original = 0.375 if tamper == "fraction" else -0.0
    try:
        begin(store)
        store.finish_operation(REQUEST_ID, session_id="session-a", state="succeeded", result={"value": original})
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        raw = connection.execute("SELECT result_json FROM control_operations WHERE request_id=?", (REQUEST_ID,)).fetchone()[0]
        result = json.loads(raw)
        result["value"] = 0.5 if tamper == "fraction" else 0.0
        connection.execute("UPDATE control_operations SET result_json=? WHERE request_id=?", (json.dumps(result), REQUEST_ID))
    reopened = ControlReconciliationStore(path)
    try:
        with pytest.raises(ControlReconciliationError, match="digest changed"):
            reopened.operation_status(REQUEST_ID, session_id="session-a")
    finally:
        reopened.close()


def test_float_control_record_metadata_remains_forbidden(tmp_path: Path) -> None:
    store = ControlReconciliationStore(tmp_path / "control.sqlite3")
    try:
        begin(store)
        with pytest.raises((ControlReconciliationError, ValueError)):
            store.finish_operation(REQUEST_ID, session_id="session-a", state="succeeded", result={"value": 0.375}, record_refs=[{"record_id": "receipt", "record_digest": "sha256:" + "1" * 64, "security_epoch": 1.5}])
        assert store.operation_status(REQUEST_ID, session_id="session-a")["state"] == "pending"
    finally:
        store.close()


@pytest.mark.parametrize("field", ["resolved_plan", "extra_metadata"])
def test_profile_ceremony_float_fields_remain_forbidden(tmp_path: Path, field: str) -> None:
    review = {
        "predecessor": {"profile_revision": "revision", "plan_digest": "plan"},
        "catalog_binding": {"profile_definition_digest": "definition", "profile_catalog_digest": "catalog", "bundle_lock_digest": "bundle"},
        "profile": {"profile_authority_snapshot_digest": "authority"},
        "resolved_plan": {"security_epoch": 1},
    }
    old_digest = canonical_digest(review)
    if field == "resolved_plan":
        review[field]["security_epoch"] = 1.5
    else:
        review[field] = {"fraction": 0.375}
    store = ControlReconciliationStore(tmp_path / "control.sqlite3")
    try:
        with pytest.raises((ControlReconciliationError, ValueError)):
            store.save_candidate(candidate_id="candidate", candidate_digest=old_digest, session_id="session-a", review=review, expires_at=1000.0)
    finally:
        store.close()


@pytest.mark.parametrize("value", VALUES, ids=["fraction", "negative-zero", "subnormal"])
def test_actual_loopback_post_numeric_payload_result_and_journal_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    class NumericDispatch(_Dispatch):
        def invoke(self, contract_id: str, operation_id: str, payload: Any, **kwargs: Any) -> Any:
            self.calls.append((contract_id, operation_id, deepcopy(payload)))
            return {"state": "ok", "value": payload["value"], "tag_object": deepcopy(payload["tag_object"])}

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path))
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    dispatch = NumericDispatch()
    binding = FrontendContractBinding(
        method="POST", path="/api/test/numeric", presentation="identity",
        route_namespace="test", application_id="test",
        targets=(HTTPContractTarget(contribution_id="test.numeric", contract_id=CONTRACT_ID, operation_id="number.echo", provider_id="test.numeric", function_id="test.numeric", allowed_payload_keys=frozenset({"value", "tag_object"})),),
    )
    server = PackAPIServer(
        port=0, dispatch_session=dispatch, contract_bindings=(binding,),
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified-desktop"),
    )
    # The fixture provides a route rather than a sealed production capture.
    monkeypatch.setattr(server, "_validate_contract_capture", lambda *args, **kwargs: None)
    server.start()
    try:
        cookie, csrf, origin = _panel_session(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf, "X-Tobkiri-Request-ID": REQUEST_ID}
        path = "/api/contracts/test/" + quote("POST /api/test/numeric", safe="")
        body = {"value": value, "tag_object": deepcopy(TAG_OBJECT)}
        status, initial, _ = _request(server, "POST", path, body=body, headers=headers)
        assert status == 200, (status, initial)
        assert_float_bits(initial["data"]["value"], value)
        assert initial["data"]["tag_object"] == TAG_OBJECT
        assert_float_bits(dispatch.calls[0][2]["value"], value)
        status, replay, _ = _request(server, "POST", path, body=body, headers=headers)
        assert status == 200, (status, replay)
        assert replay == initial
        assert len(dispatch.calls) == 1
        session_id = dispatch.calls[0][2]["_session_id"]
        record = server._operation_journal.operation_status(REQUEST_ID, session_id=session_id)
        assert record["result"] == initial["data"]
        assert record["result_digest"] == digest_payload(initial["data"])
        journal_path = server._operation_journal.path
        status, _, _ = _request(server, "POST", path, body={**body, "value": 0.625}, headers=headers)
        assert status == 409
        assert len(dispatch.calls) == 1
    finally:
        server.stop()
    reopened = ControlReconciliationStore(journal_path)
    try:
        restored = reopened.operation_status(REQUEST_ID, session_id=session_id)
        assert restored == record
        assert_float_bits(restored["result"]["value"], value)
    finally:
        reopened.close()


class _DictSubclass(dict):
    """Unsupported provider-owned container subclass for admission regression."""


class _ListSubclass(list):
    """Unsupported provider-owned container subclass for admission regression."""


@pytest.mark.parametrize("result", [
    {"value": {1: "integer key"}},
    {"value": {True: "boolean key"}},
    {"value": {None: "null key"}},
    {"value": (1, 2)},
    {"value": _DictSubclass({"number": 0.375})},
    {"value": _ListSubclass([0.375])},
    _DictSubclass({"value": 0.375}),
], ids=["integer-key", "boolean-key", "null-key", "tuple",
        "nested-dict-subclass", "list-subclass", "root-dict-subclass"])
def test_control_result_rejects_original_python_types_before_terminal_write(
    tmp_path: Path, result: Any,
) -> None:
    """A preliminary json.dumps must not launder unsupported original data."""
    store = ControlReconciliationStore(tmp_path / "control.sqlite3")
    try:
        begin(store)
        before = store.operation_status(REQUEST_ID, session_id="session-a")
        with pytest.raises(ValueError):
            store.finish_operation(
                REQUEST_ID, session_id="session-a", state="succeeded", result=result,
            )
        after = store.operation_status(REQUEST_ID, session_id="session-a")
        assert after == before
        assert after["state"] == "pending" and after["result"] is None
    finally:
        store.close()


def test_control_result_provider_mutation_cannot_split_serialization_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist/hash an independent final snapshot despite nested owner mutation."""
    from core_runtime import control_reconciliation_v4 as control

    store = ControlReconciliationStore(tmp_path / "control.sqlite3")
    result = {"value": {"number": 0.375}}
    try:
        begin(store)
        original_json = control._json
        snapshots = 0

        def mutate_provider_after_serializing(value: Any) -> str:
            nonlocal snapshots
            raw = original_json(value)
            if isinstance(value, dict) and "value" in value:
                snapshots += 1
                # The first call is disposable size preflight. The second
                # serializes the owned clone, which must share no containers.
                result["value"]["number"] = 0.625 if snapshots == 1 else 0.875
            return raw

        monkeypatch.setattr(control, "_json", mutate_provider_after_serializing)
        finished = store.finish_operation(
            REQUEST_ID, session_id="session-a", state="succeeded", result=result,
        )
        assert snapshots == 2
        assert result["value"]["number"] == 0.875
        assert finished["result"] == {"value": {"number": 0.625}}
        assert finished["result_digest"] == digest_payload(finished["result"])
        assert store.operation_status(REQUEST_ID, session_id="session-a") == finished
    finally:
        store.close()


def test_control_result_preserves_initial_oversize_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing size refusal occurs without admitting/coercing provider data."""
    from core_runtime import control_reconciliation_v4 as control

    store = ControlReconciliationStore(
        tmp_path / "control.sqlite3", max_operation_result_bytes=128,
    )
    try:
        begin(store)

        def unexpected_clone(value: Any) -> Any:
            raise AssertionError("oversized result must retain the size refusal")

        monkeypatch.setattr(control, "clone_payload", unexpected_clone)
        finished = store.finish_operation(
            REQUEST_ID, session_id="session-a", state="succeeded",
            result={"value": "x" * 256},
        )
        assert finished["state"] == "indeterminate"
        assert finished["safe_error_code"] == "RESULT_TOO_LARGE"
        assert finished["result"] is None and finished["result_digest"] is None
    finally:
        store.close()


def test_control_result_rechecks_limit_after_preflight_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider changing during preflight cannot exceed the final byte limit."""
    from core_runtime import control_reconciliation_v4 as control

    store = ControlReconciliationStore(
        tmp_path / "control.sqlite3", max_operation_result_bytes=128,
    )
    result = {"value": "short"}
    try:
        begin(store)
        original_json = control._json
        mutated = False

        def grow_after_preflight(value: Any) -> str:
            nonlocal mutated
            raw = original_json(value)
            if isinstance(value, dict) and "value" in value and not mutated:
                result["value"] = "x" * 256
                mutated = True
            return raw

        monkeypatch.setattr(control, "_json", grow_after_preflight)
        finished = store.finish_operation(
            REQUEST_ID, session_id="session-a", state="succeeded", result=result,
        )
        assert mutated
        assert finished["state"] == "indeterminate"
        assert finished["safe_error_code"] == "RESULT_TOO_LARGE"
        assert finished["result"] is None and finished["result_digest"] is None
    finally:
        store.close()
