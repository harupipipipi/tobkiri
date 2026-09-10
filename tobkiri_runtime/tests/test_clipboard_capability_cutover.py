"""Native adapter unit doubles and real child-pipe tests, not OS clipboard proof."""
from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tobkiri_host import clipboard_native as native
from tobkiri_host.effects import EffectDisposition, ProviderOutcome


@pytest.mark.parametrize("approved", [False, True])
def test_legacy_boolean_entrypoint_is_retired(approved):
    from ecosystem.rumi_clipboard_host_service_pack.runtime.runner import run_clipboard_host_action

    result = run_clipboard_host_action(
        "clipboard.write", {"text": "fixture", "approved": True},
        viewer_host_approved=approved,
    )
    assert result["error_type"] == "legacy_clipboard_execution_retired"
    assert result["executed"] is False


def test_availability_is_side_effect_free_and_not_consent(monkeypatch):
    adapter = native.MacOSTextClipboard()
    spawn = Mock(side_effect=AssertionError("availability spawned an effect"))
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    monkeypatch.setattr(native.sys, "platform", "linux")
    assert adapter.availability("read")["error_type"] == "unsupported_os"
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(adapter, "_trusted_command", Mock(side_effect=OSError()))
    assert adapter.availability("read")["error_type"] == "provider_unavailable"
    monkeypatch.setattr(adapter, "_trusted_command", lambda access: "/usr/bin/pbpaste")
    result = adapter.availability("read")
    assert result["available"] is True
    assert result["resource_scope"] == "global_clipboard"
    assert result["authorization"] == "checked_at_execution"
    spawn.assert_not_called()


@pytest.fixture
def adapter(monkeypatch):
    value = native.MacOSTextClipboard()
    monkeypatch.setattr(value, "availability", lambda access: {"available": True})
    monkeypatch.setattr(value, "_trusted_command", lambda access: "/usr/bin/pb" + ("paste" if access == "read" else "copy"))
    return value


def fake_process():
    return NS(returncode=0, stdin=io.BytesIO(), stdout=io.BytesIO(),
              stderr=io.BytesIO(), poll=lambda: 0)


@pytest.mark.parametrize("access,payload,output", [
    ("read", {}, "日本語\n".encode()), ("write", {"text": "日本語\n"}, b""),
])
def test_native_mock_success_has_fixed_command_and_sanitized_environment(
    adapter, monkeypatch, access, payload, output,
):
    spawn = Mock(return_value=fake_process())
    check = Mock()
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    monkeypatch.setattr(adapter, "_exchange", lambda *args: output)
    result = adapter.execute(access, payload, deadline=time.monotonic() + 2, check_authority=check)
    assert result.disposition is EffectDisposition.COMPLETED
    if access == "read":
        assert result.payload["text"] == "日本語\n"
    else:
        assert result.payload["written"] is True
    args, kwargs = spawn.call_args
    assert args == (["/usr/bin/pb" + ("paste" if access == "read" else "copy")],)
    assert kwargs.get("shell", False) is False
    assert set(kwargs["env"]) == {"LANG", "LC_CTYPE"}
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == "/"
    assert check.call_count == 2


@pytest.mark.parametrize("error,code", [
    (PermissionError(), "os_permission_denied"), (OSError(), "provider_unavailable"),
])
def test_spawn_failure_is_distinct_and_never_falls_back(adapter, monkeypatch, error, code):
    spawn = Mock(side_effect=error)
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    result = adapter.execute("write", {"text": "fixture"}, deadline=time.monotonic() + 2, check_authority=lambda: None)
    assert result.payload["error_type"] == code
    assert result.disposition is EffectDisposition.NOT_ACCEPTED
    spawn.assert_called_once()


def test_final_authority_denial_prevents_spawn(adapter, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    with pytest.raises(PermissionError):
        adapter.execute("read", {}, deadline=time.monotonic() + 2,
                        check_authority=Mock(side_effect=PermissionError("revoked")))
    spawn.assert_not_called()


@pytest.mark.parametrize("access,payload", [
    ("read", {"approved": True}), ("write", {"text": "x", "command": "evil"}),
    ("write", {"text": "x" * (native.MAX_TEXT_BYTES + 1)}),
])
def test_invalid_payload_is_rejected_before_effect(adapter, monkeypatch, access, payload):
    spawn = Mock()
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    with pytest.raises(ValueError):
        adapter.execute(access, payload, deadline=time.monotonic() + 2, check_authority=lambda: None)
    spawn.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError(), PermissionError("revoked"), OSError()])
def test_write_interruption_and_cleanup_failure_remain_unknown(adapter, monkeypatch, error):
    process = fake_process()
    process.poll = lambda: None
    process.pid = 999999999
    monkeypatch.setattr(native.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(adapter, "_exchange", Mock(side_effect=error))
    monkeypatch.setattr(native.os, "killpg", Mock(side_effect=OSError("cleanup failed")))
    result = adapter.execute("write", {"text": "fixture"}, deadline=time.monotonic() + 2, check_authority=lambda: None)
    assert result.disposition is EffectDisposition.UNKNOWN
    assert result.payload is None


def test_invalid_utf8_read_does_not_expose_partial_content(adapter, monkeypatch):
    monkeypatch.setattr(native.subprocess, "Popen", Mock(return_value=fake_process()))
    monkeypatch.setattr(adapter, "_exchange", lambda *args: b"\xffsecret")
    result = adapter.execute("read", {}, deadline=time.monotonic() + 2, check_authority=lambda: None)
    assert result.disposition is EffectDisposition.NOT_ACCEPTED
    assert "text" not in result.payload


@pytest.mark.parametrize("mode", ["echo", "oversize", "timeout"])
def test_real_child_pipe_is_bounded_without_touching_clipboard(mode):
    scripts = {
        "echo": "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        "oversize": "import sys; sys.stdout.buffer.write(b'x' * 1048577)",
        "timeout": "import time; time.sleep(3)",
    }
    process = subprocess.Popen([sys.executable, "-B", "-c", scripts[mode]],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        if mode == "echo":
            result = native.MacOSTextClipboard._exchange(process, "試験".encode(), time.monotonic() + 2, lambda: None)
            assert result == "試験".encode()
        else:
            expected = ValueError if mode == "oversize" else TimeoutError
            with pytest.raises(expected):
                native.MacOSTextClipboard._exchange(process, b"", time.monotonic() + (2 if mode == "oversize" else .1), lambda: None)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.fixture
def transport_fixture():
    """Authority-shaped doubles test transport binding, not Authority issuance."""
    from core_runtime.authority.v4 import LeaseState
    from core_runtime.clipboard_transport_v4 import ClipboardTransportV4
    from tobkiri_protocol.canonical import canonical_digest

    values = dict(request_id="request", profile_id="profile", activation_id="activation",
                  activation_digest="activation-digest", plan_digest="plan",
                  profile_authority_digest="profile-authority", security_epoch=1,
                  caller_domain_id="caller-domain", caller_boot_epoch="caller-boot",
                  target_domain_id="target-domain", target_boot_epoch="target-boot",
                  fencing_token=1)
    context = NS(**values, caller_principal=NS(value="caller"), profile_revision=1)
    envelope = NS(cancellation_requested=threading.Event(), context=context, target_principal=NS(value="provider"),
                  target_domain=NS(value="target-domain"),
                  contract_id="tobkiri.resource.clipboard.v1", contract_version="1.0.0",
                  operation_id="rumi_clipboard_host_service_pack.clipboard-read",
                  payload={}, idempotency_key=None, deadline_monotonic=time.monotonic() + 10,
                  lease=NS(token=b"opaque-host-token"))
    envelope.request_digest = canonical_digest(dict(
        request_id=context.request_id, profile_revision=context.profile_revision,
        activation_digest=context.activation_digest, plan_digest=context.plan_digest,
        target="provider", contract_id=envelope.contract_id,
        contract_version=envelope.contract_version, operation_id=envelope.operation_id,
        payload={}, idempotency_key=None,
    ))
    caller = NS(principal_id="caller", parent_artifact_digest="caller-artifact")
    target = NS(principal_id="provider", parent_artifact_digest="provider-artifact",
                operation_id=envelope.operation_id)
    lease = NS(**values, caller=caller, target=target, request_digest=envelope.request_digest,
               lease_id="lease", expires_at=time.time() + 10, grant_id="grant",
               provider_authority_id="provider-authority", caller_publisher_lineage="caller-publisher",
               target_publisher_lineage="provider-publisher", host_extension_id="extension")
    record = NS(valid_from=0, expires_at=None)
    store = Mock(security_epoch=1)
    store.inspect_lease_token.return_value = lease, LeaseState.DISPATCHED
    store.is_revoked.return_value = False
    store.get_grant.return_value = NS(issued_at=0, expires_at=None)
    store.get_provider_authority.return_value = record
    store.get_host_extension_trust.return_value = NS(
        trust_id="extension", revoked=False, security_epoch=1,
        package_kind="host_extension", parent_artifact_digest="provider-artifact",
        publisher_lineage="provider-publisher", provider_principal_ids=("provider",),
        valid_from=0, expires_at=None,
    )
    store.get_domain.side_effect = lambda identity: NS(
        state=NS(value="active"), security_epoch=1,
        boot_epoch="caller-boot" if identity == "caller-domain" else "target-boot",
        principal_ids={"caller" if identity == "caller-domain" else "provider"},
    )
    transport = ClipboardTransportV4(store)
    transport._native = Mock()
    transport._native.execute.return_value = ProviderOutcome({"text": "fixture"})
    return transport, envelope, lease, store


def test_transport_mock_binding_accepts_once_and_rejects_replay(transport_fixture):
    transport, envelope, lease, store = transport_fixture
    assert transport.invoke(envelope).payload["text"] == "fixture"
    with pytest.raises(PermissionError, match="replay"):
        transport.invoke(envelope)
    transport._native.execute.assert_called_once()


@pytest.mark.parametrize("field", [
    "profile_id", "activation_id", "caller_domain_id", "target_domain_id",
    "caller_boot_epoch", "target_boot_epoch", "fencing_token", "plan_digest",
])
def test_transport_mock_context_mismatch_denies_before_effect(transport_fixture, field):
    transport, envelope, lease, store = transport_fixture
    setattr(lease, field, "different")
    with pytest.raises(PermissionError):
        transport.invoke(envelope)
    transport._native.execute.assert_not_called()


@pytest.mark.parametrize("failure", ["revoked", "expired", "no_grant", "no_provider", "payload", "caller"])
def test_transport_mock_inactive_authority_denies_before_effect(transport_fixture, failure):
    transport, envelope, lease, store = transport_fixture
    if failure == "revoked":
        store.is_revoked.return_value = True
    elif failure == "expired":
        lease.expires_at = 0
    elif failure == "no_grant":
        store.get_grant.return_value = None
    elif failure == "no_provider":
        store.get_provider_authority.return_value = None
    elif failure == "payload":
        envelope.payload = {"text": "changed"}
    else:
        lease.caller.principal_id = "spoofed"
    with pytest.raises(PermissionError):
        transport.invoke(envelope)
    transport._native.execute.assert_not_called()


def test_transport_mock_revocation_after_initial_check_is_rechecked(transport_fixture):
    transport, envelope, lease, store = transport_fixture

    def execute(access, payload, *, deadline, check_authority):
        store.is_revoked.return_value = True
        check_authority()
        pytest.fail("revoked request reached simulated native effect")

    transport._native.execute.side_effect = execute
    with pytest.raises(PermissionError, match="revoked"):
        transport.invoke(envelope)


@pytest.mark.parametrize("action,payload,contract,operation,expected", [
    ("computer.clipboard.read", {"include_content": True},
     "tobkiri.resource.clipboard.v1", "rumi_clipboard_host_service_pack.clipboard-read", {}),
    ("computer.clipboard.write", {"content": "fixture"},
     "tobkiri.action.clipboard.v1", "rumi_clipboard_host_service_pack.clipboard-write", {"text": "fixture"}),
    ("computer.clipboard.clear", {"text": "ignored"},
     "tobkiri.action.clipboard.v1", "rumi_clipboard_host_service_pack.clipboard-write", {"text": ""}),
])
def test_projection_uses_formal_contract_and_strips_boolean_authority(
    monkeypatch, action, payload, contract, operation, expected,
):
    from ecosystem.rumi_default_tools_pack.domain.tool import host_contract_adapter as projection

    session = object()
    monkeypatch.setattr(projection, "get_container", lambda: NS(get_or_none=lambda name: session))
    dispatch = Mock(return_value={"success": False, "error_type": "approval_required"})
    monkeypatch.setattr(projection, "invoke_global_contract", dispatch)
    result = projection.run_host_contract_action(
        action, {**payload, "approved": True, "viewer_host_approved": True,
                 "authority_token": "untrusted", "_host_context": "spoofed"},
        source_function_id="untrusted-label",
    )
    dispatch.assert_called_once_with(session, contract, operation, expected)
    assert result["error_type"] == "approval_required"


@pytest.mark.parametrize("result", [
    {"success": True, "text": "fixture"},
    {"success": False, "error_type": "approval_required"},
    {"success": False, "error_type": "ambiguous_effect"},
])
def test_ui_returns_native_result_without_second_intent_or_retry(monkeypatch, result):
    from ecosystem.defaultspack.domain.media import contract_adapter as media
    from core_runtime import host_intent

    dispatch = Mock(return_value=result)
    second_effect = Mock(side_effect=AssertionError("native result replayed"))
    monkeypatch.setattr(media, "invoke_media_contract", dispatch)
    monkeypatch.setattr(host_intent, "maybe_handle_host_intent_output", second_effect)
    assert media.execute_ui_host_contract(
        media.CLIPBOARD_READ, "read", {}, source_function_id="fixture",
    ) is result
    dispatch.assert_called_once()
    second_effect.assert_not_called()


@pytest.mark.parametrize("name,payload", [
    ("clipboard_read", {}), ("clipboard_write", {"content": "fixture"}),
])
def test_media_blocks_propagate_denial_without_false_success(monkeypatch, name, payload):
    import importlib
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"))
    block = importlib.import_module("blocks.media." + name)
    denied = {"success": False, "error_type": "approval_required"}
    dispatch = Mock(return_value=denied)
    monkeypatch.setattr(block, "invoke_media_contract", dispatch)
    assert block.run(payload, {}) is denied
    dispatch.assert_called_once()


@pytest.mark.parametrize("kind", ["grant", "provider"])
def test_transport_mock_expired_authority_record_denies(transport_fixture, kind):
    transport, envelope, lease, store = transport_fixture
    record = store.get_grant.return_value if kind == "grant" else store.get_provider_authority.return_value
    record.expires_at = time.time() - 1
    with pytest.raises(PermissionError):
        transport.invoke(envelope)
    transport._native.execute.assert_not_called()


def test_transport_mock_domain_boot_reuse_denies(transport_fixture):
    transport, envelope, lease, store = transport_fixture
    store.get_domain.side_effect = lambda identity: NS(
        state=NS(value="active"), security_epoch=1, boot_epoch="reused-new-process",
        principal_ids={"caller", "provider"},
    )
    with pytest.raises(PermissionError, match="execution domain changed"):
        transport.invoke(envelope)
    transport._native.execute.assert_not_called()


@pytest.mark.parametrize("bad_mode,bad_uid", [
    (0o100777, 0), (0o120755, 0), (0o100755, 501),
])
def test_os_helper_trust_rejects_writable_symlink_and_user_owned(
    monkeypatch, bad_mode, bad_uid,
):
    from pathlib import Path
    import stat

    def metadata(path):
        if str(path) == "/usr/bin/pbpaste":
            return NS(st_mode=bad_mode, st_uid=bad_uid)
        return NS(st_mode=stat.S_IFDIR | 0o755, st_uid=0)

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(ValueError, match="trust"):
        native.MacOSTextClipboard._trusted_command("read")


def test_transport_cancelled_before_effect(transport_fixture):
    transport, envelope, lease, store = transport_fixture
    envelope.cancellation_requested.set()
    with pytest.raises(PermissionError):
        transport.invoke(envelope)
    transport._native.execute.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("trust_id", "other-extension"), ("revoked", True),
    ("security_epoch", 2), ("package_kind", "normal_pack"),
    ("parent_artifact_digest", "replacement-artifact"),
    ("publisher_lineage", "other-publisher"),
    ("provider_principal_ids", ("another-provider",)),
    ("valid_from", float("inf")), ("expires_at", 0),
])
def test_transport_stale_extension_trust_denies_before_effect(
    transport_fixture, field, value,
):
    transport, envelope, lease, store = transport_fixture
    setattr(store.get_host_extension_trust.return_value, field, value)
    with pytest.raises(PermissionError, match="Host Extension trust"):
        transport.invoke(envelope)
    transport._native.execute.assert_not_called()


def test_transport_missing_extension_trust_denies_before_effect(transport_fixture):
    transport, envelope, lease, store = transport_fixture
    store.get_host_extension_trust.return_value = None
    with pytest.raises(PermissionError, match="Host Extension trust"):
        transport.invoke(envelope)
    transport._native.execute.assert_not_called()


def test_transport_extension_expiry_after_entry_rechecks_at_native_boundary(transport_fixture):
    transport, envelope, lease, store = transport_fixture
    effect = Mock()

    def native_entry(access, payload, *, deadline, check_authority):
        store.get_host_extension_trust.return_value.expires_at = time.time() - 1
        check_authority()
        effect()

    transport._native.execute.side_effect = native_entry
    with pytest.raises(PermissionError, match="Host Extension trust"):
        transport.invoke(envelope)
    effect.assert_not_called()
