"""Warm record validation must preserve real capture entry and exit checks.

These tests compose production dispatch over the sealed packaged Profile and
exercise its original capture/traversal in a detached thread. They are
lower-level guard coverage, not full Workflow monitor or HTTP cancellation proof.
Only pytest's temporary user-data tree and its real Authority store are mutated.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import ecosystem.defaultspack.domain.runtime_v4.service as runtime_service
from core_runtime.active_profile_store_v4 import (
    ActiveProfileStore,
    ActiveProfileStoreError,
)
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    _PROFILE_CAPTURE_SCOPE,
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.capture_guard_traversal_v4 import _capture_guard_traversal, _current
from core_runtime.profile_runtime_port import require_profile_runtime
from ecosystem.defaultspack.domain.runtime_v4._record_validation_cache import (
    _VALIDATION_POLICY,
    _RecordValidationCache,
)
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_workflow_v4_pack_control_integration import (
    _capture_defaultspack_dispatch,
)
from tobkiri_protocol.canonical import canonical_digest, canonical_json


def _assert_records_warm(cache: _RecordValidationCache, active: Any) -> None:
    assert runtime_service._RECORD_VALIDATION_CACHE is cache
    records = {
        "profile": active.resolved.profile,
        "profile_lock": active.resolved.lock,
        "resolved_plan": active.resolved.plan,
    }
    with cache._lock:
        keys = tuple(cache._entries)
    for kind, record in records.items():
        raw = canonical_json(record)
        assert any(
            key[0] == kind
            and key[1] == _VALIDATION_POLICY
            and key[2].validator is runtime_service.validate_document
            and key[3] == raw
            for key in keys
        ), f"{kind} must be warm for the live service validator and exact record"


def _pointer_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.lstat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _require_temp_file(path: Path, user_data: Path) -> Path:
    resolved = path.resolve(strict=True)
    assert resolved.is_relative_to(user_data.resolve(strict=True)), resolved
    assert resolved.is_file() and not path.is_symlink(), path
    return resolved


def _make_mutation(
    kind: str,
    *,
    user_data: Path,
    active: Any,
    authority: AuthorityStore,
) -> tuple[Callable[[], None], str]:
    pointer_store = ActiveProfileStore(user_data)
    pointer = pointer_store.require(verify_snapshot=True)
    pointer_path = _require_temp_file(pointer_store.path, user_data)
    pointer_bytes = pointer_path.read_bytes()
    signature = _pointer_signature(pointer_path)
    epoch = authority.security_epoch

    def check_invariants(*, unchanged_pointer: bool) -> None:
        assert authority.security_epoch == epoch
        if unchanged_pointer:
            assert pointer_path.read_bytes() == pointer_bytes
            assert _pointer_signature(pointer_path) == signature
        else:
            assert pointer_path.read_bytes() != pointer_bytes
            assert _pointer_signature(pointer_path) != signature

    if kind == "retirement":
        activation_id = str(active.activation["activation_id"])
        reservation = authority.active_activation_reservation(activation_id)
        assert reservation is not None and reservation["state"] == "active"
        reservation_id = str(reservation["reservation_id"])

        def mutate() -> None:
            # Exercise the audited Authority transition, not a direct SQL edit.
            retired = authority.transition_activation(
                reservation_id, expected_state="active", new_state="retired"
            )
            assert retired["state"] == "retired"
            assert authority.active_activation_reservation(activation_id) is None
            check_invariants(unchanged_pointer=True)

        return mutate, "active activation authority, fence, or SecurityEpoch is stale"

    if kind == "snapshot":
        snapshot_path = _require_temp_file(user_data / pointer.activation_snapshot_path, user_data)
        snapshot_bytes = snapshot_path.read_bytes()

        def mutate() -> None:
            document = json.loads(snapshot_bytes)
            document["diagnostic_mutation"] = "temporary activation envelope only"
            snapshot_path.write_bytes(canonical_json(document) + b"\n")
            assert snapshot_path.read_bytes() != snapshot_bytes
            check_invariants(unchanged_pointer=True)

        return mutate, "activation snapshot digest does not match active pointer"

    assert kind == "pointer"

    def mutate() -> None:
        document = json.loads(pointer_bytes)
        changed_digest = "sha256:" + "0" * 64
        if document["activation_snapshot_digest"] == changed_digest:
            changed_digest = "sha256:" + "1" * 64
        document["activation_snapshot_digest"] = changed_digest
        document["pointer_digest"] = canonical_digest(
            {key: value for key, value in document.items() if key != "pointer_digest"}
        )
        replacement = pointer_path.with_name("active.capture-guard-replacement.json")
        descriptor = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_json(document) + b"\n")
        os.replace(replacement, pointer_path)
        check_invariants(unchanged_pointer=False)
        # Its own digest remains valid; the snapshot binding must reject it.
        replaced = pointer_store.require(verify_snapshot=False)
        assert replaced.activation_snapshot_digest == changed_digest

    return mutate, "activation snapshot digest does not match active pointer"


def _assert_guard_denial(error: Exception, expected_reason: str) -> None:
    assert isinstance(
        error, ActiveProfileStoreError
    ) or require_profile_runtime().is_resolution_denied(error), (type(error).__name__, str(error))
    assert expected_reason in str(error)


def _run_guard_case(
    *,
    phase: str,
    dispatch: Any,
    mutate: Callable[[], None],
    assert_warm: Callable[[], None],
    expected_reason: str,
) -> None:
    # A detached thread must inherit neither caller capture nor traversal state.
    assert _PROFILE_CAPTURE_SCOPE.get() is None
    assert _current.get() is None
    dispatch.assert_current()
    assert_warm()
    stages = ["baseline-valid"]

    if phase == "entry":
        mutate()
        try:
            with _capture_guard_traversal(dispatch.assert_current):
                stages.append("body-entered")
                dispatch.assert_current()
        except Exception as error:
            _assert_guard_denial(error, expected_reason)
            assert "body-entered" not in stages
        else:
            pytest.fail("original traversal entry accepted changed Profile state")
    else:
        assert phase == "exit"
        try:
            with _capture_guard_traversal(dispatch.assert_current):
                stages.append("body-entered")
                dispatch.assert_current()
                assert_warm()
                mutate()
                # Synchronous sharing may reuse entry; exit must check afresh.
                dispatch.assert_current()
                stages.append("body-completed")
        except Exception as error:
            _assert_guard_denial(error, expected_reason)
            assert stages[-1] == "body-completed", stages
        else:
            pytest.fail("original traversal exit accepted changed Profile state")

    assert _PROFILE_CAPTURE_SCOPE.get() is None
    assert _current.get() is None
    try:
        dispatch.assert_current()
    except Exception as error:
        _assert_guard_denial(error, expected_reason)
    else:
        pytest.fail("later original guard reused a stale traversal success")


@pytest.mark.parametrize("phase", ["entry", "exit"])
@pytest.mark.parametrize("kind", ["retirement", "snapshot", "pointer"])
def test_warm_record_cache_preserves_real_capture_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    phase: str,
) -> None:
    """Reject changed state at both traversal boundaries with all records warm."""
    cache = _RecordValidationCache()
    monkeypatch.setattr(runtime_service, "_RECORD_VALIDATION_CACHE", cache)
    user_data = tmp_path / "capture-guard-user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    dispatch = None
    try:
        dispatch = _capture_defaultspack_dispatch(
            active,
            bundle_root=packaged_profile_bundle_root(),
            ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
            authority_store=authority,
        )
        # Real baseline loads populate the cache; never seed it by direct validation.
        dispatch.assert_current()
        _assert_records_warm(cache, active)
        mutate, reason = _make_mutation(
            kind, user_data=user_data, active=active, authority=authority
        )
        failures: list[BaseException] = []

        def target() -> None:
            try:
                _run_guard_case(
                    phase=phase,
                    dispatch=dispatch,
                    mutate=mutate,
                    assert_warm=lambda: _assert_records_warm(cache, active),
                    expected_reason=reason,
                )
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=target, name="workflow-attempt-fence", daemon=True)
        worker.start()
        worker.join(timeout=120)
        assert not worker.is_alive(), "detached capture guard did not finish"
        if failures:
            raise failures[0]
    finally:
        if dispatch is not None:
            dispatch.close()
        authority.close()
