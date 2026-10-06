"""Actual receive-before-complete evidence and bounded, owner-scoped progress."""

from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from threading import Barrier, Event, Thread
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any

import pytest

from core_runtime.credential_transport import CredentialTransportDenied
from core_runtime.invocation_scope_v4 import (
    CapturedInvocationScopeV4,
    ParentInvocationScopesV4,
    assert_dispatched_invocation,
    execution_session_id,
)
from core_runtime.local_model_transport import LocalModelBinding, LocalModelTransport
from ecosystem.rumi_provider_adapters_pack.runtime.streaming import ProviderStream
from ecosystem.rumi_turn_runtime_pack.runtime import progress as progress_module
from ecosystem.rumi_turn_runtime_pack.runtime.progress import TurnProgressJournal
from tests.test_credential_broker_pack import _dispatched_envelope, _https_transport
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.provider_sse_v1 import MAX_EVENT_BYTES, SSEDecoder


def _frame(content: str = "", *, finish: str | None = None) -> bytes:
    data = {
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": finish}
        ]
    }
    return b"data: " + json.dumps(data).encode() + b"\n\n"


def _binding() -> dict[str, Any]:
    return {
        "turn_id": "turn",
        "conversation_id": "conversation",
        "conversation_revision": 2,
        "parent_id": "message.user",
        "input_digest": canonical_digest({"content": "hello"}),
        "request_id": "request",
        "ai_input_digest": canonical_digest({"messages": []}),
    }


def _journal(path: Path) -> tuple[TurnProgressJournal, str]:
    journal = TurnProgressJournal(path / "progress.sqlite3")
    identity = journal.begin(_binding(), owner="owner", capture="capture")
    journal.bind(
        identity,
        owner="owner",
        capture="capture",
        ai_input_digest=_binding()["ai_input_digest"],
        producer="producer",
        producer_input_digest="input",
    )
    journal.claim(
        identity,
        owner="owner",
        capture="capture",
        producer="producer",
        producer_input_digest="input",
        execution="lease.request",
    )
    return journal, identity


def _journal_rows(path: Path) -> tuple[int, int]:
    """Count journal rows through the same immutable SQLite URI mode."""
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return tuple(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("progress", "chunks")
        )
    finally:
        connection.close()


@pytest.mark.parametrize("interrupt", [None, "cancel", "epoch", "revocation"])
def test_first_actual_http_chunk_is_readable_before_provider_completion(
    tmp_path: Path,
    interrupt: str | None,
) -> None:
    """A real blocking HTTP server proves progress before EOF; fences close IO."""
    release, sent, published, finished = Event(), Event(), Event(), Event()
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(_frame("first"))
            self.wfile.flush()
            sent.set()
            release.wait(5)
            try:
                self.wfile.write(
                    _frame(" second") + _frame(finish="stop") + b"data: [DONE]\n\n"
                )
                self.wfile.flush()
            except OSError:
                pass

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    authority, envelope = _dispatched_envelope(tmp_path)
    binding = LocalModelBinding(
        "profile-1", "local", f"http://127.0.0.1:{server.server_port}/v1", ("model",), 1
    )
    transport = LocalModelTransport(
        profile_id="profile-1",
        resolve_binding=lambda _: binding,
        assert_current=lambda: assert_dispatched_invocation(envelope, authority.store),
        deadline_monotonic=time.monotonic() + 5,
        cancellation=envelope.cancellation_requested,
    )
    journal, identity = _journal(tmp_path)
    cursor = 0

    def publish(event: Any) -> None:
        nonlocal cursor
        cursor += 1
        journal.publish(
            identity,
            owner="owner",
            capture="capture",
            producer="producer",
            producer_input_digest="input",
            execution="lease.request",
            cursor=cursor,
            event=event,
        )
        published.set()

    stream = ProviderStream("openai", publish)
    failures: list[Exception] = []

    def run() -> None:
        try:
            transport.stream_chat(
                provider_instance_id="local",
                deadline=time.time() + 5,
                body={
                    "model": "model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
                on_event=stream.receive,
            )
        except Exception as error:
            failures.append(error)
        finally:
            finished.set()

    worker = Thread(target=run, daemon=True)
    worker.start()
    try:
        assert sent.wait(2) and published.wait(2)
        page = journal.read(identity, owner="owner", capture="capture", cursor=0)
        assert page["events"] == [
            {"cursor": 1, "event": {"type": "text_delta", "delta": "first"}}
        ]
        assert page["provisional"] and not page["provider_complete"]
        assert not finished.is_set() and not release.is_set()
        if interrupt == "cancel":
            envelope.cancellation_requested.set()
        elif interrupt == "epoch":
            authority.store.advance_security_epoch("stream test epoch")
        elif interrupt == "revocation":
            authority.kernel.revoke(
                target_kind="function_principal",
                target_id=authority.target.principal_id,
                reason="stream test revoke",
            )
        if interrupt:
            # The server still blocks: the lease watchdog must interrupt actual IO.
            assert finished.wait(2) and failures and not release.is_set()
            assert not journal.read(
                identity, owner="owner", capture="capture", cursor=0
            )["provider_complete"]
        else:
            release.set()
            assert finished.wait(2) and not failures
            assert stream.result()["output"] == "first second"
        assert len(requests) == 1 and requests[0]["stream"] is True
        with pytest.raises(PermissionError, match="consumed"):
            transport.stream_chat(
                provider_instance_id="local",
                body={},
                deadline=time.time() + 5,
                on_event=lambda _: None,
            )
    finally:
        release.set()
        worker.join(2)
        server.shutdown()
        server.server_close()
        server_thread.join(2)


@pytest.mark.parametrize(
    "payload",
    [
        b'data: {"x":1,"x":2}\n\n',
        b'data: {"x":NaN}\n\n',
        b'data: {"x":1e999}\n\n',
        b"data: []\n\n",
        b"id: same\ndata: {}\n\nid: same\ndata: {}\n\n",
        b"data: [DONE]\n\ndata: {}\n\n",
        b"data: " + b"x" * MAX_EVENT_BYTES,
    ],
)
def test_malformed_oversized_or_replayed_sse_is_rejected(payload: bytes) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        SSEDecoder().feed(payload)


def test_decoder_handles_utf8_and_crlf_split_across_real_receive_boundaries() -> None:
    decoder = SSEDecoder()
    raw = 'data: {"text":"あ"}\r\n\r\n'.encode()
    events = []
    for byte in raw:
        events.extend(decoder.feed(bytes([byte])))
    decoder.finish()
    assert events == [{"event": "message", "data": {"text": "あ"}, "done": False}]
    decoder.feed(b"data: {")
    with pytest.raises(ValueError, match="partial"):
        decoder.finish()


def test_host_credential_frames_redact_secrets_split_between_events(
    tmp_path: Path,
) -> None:
    transport, arguments = _https_transport(tmp_path, secret="secret-test-token")

    class Response:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    _frame("before secret-"),
                    _frame("test-token after"),
                    _frame(finish="stop"),
                    b"data: [DONE]\n\n",
                ]
            )
            self.closed = False

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            self.closed = True

        def read1(self, _amount: int) -> bytes:
            return next(self.chunks, b"")

    response = Response()
    transport._opener = lambda *_args, **_kwargs: response
    stream = ProviderStream("openai", lambda _: None)
    transport.stream_json(**arguments, on_event=stream.receive)
    assert stream.result()["output"] == "before [REDACTED] after"
    assert response.closed and "secret-test-token" not in json.dumps(stream.events)
    with pytest.raises(CredentialTransportDenied):
        transport.stream_json(**arguments, on_event=stream.receive)


@pytest.mark.parametrize(
    "change", ["owner", "profile", "plan", "epoch", "input", "execution"]
)
def test_journal_wrong_owner_capture_input_or_execution_cannot_publish(
    tmp_path: Path, change: str
) -> None:
    journal, identity = _journal(tmp_path)
    arguments = {
        "owner": "owner",
        "capture": "capture",
        "producer": "producer",
        "producer_input_digest": "input",
        "execution": "lease.request",
    }
    key = {
        "owner": "owner",
        "profile": "capture",
        "plan": "capture",
        "epoch": "capture",
        "input": "producer_input_digest",
        "execution": "execution",
    }[change]
    arguments[key] = "wrong"
    with pytest.raises(PermissionError):
        journal.publish(
            identity, **arguments, cursor=1, event={"type": "text_delta", "delta": "x"}
        )
    assert (
        journal.read(identity, owner="owner", capture="capture", cursor=0)["events"]
        == []
    )


def test_journal_replay_order_claim_expiry_and_terminal_are_fenced(
    tmp_path: Path,
) -> None:
    journal, identity = _journal(tmp_path)
    arguments = {
        "owner": "owner",
        "capture": "capture",
        "producer": "producer",
        "producer_input_digest": "input",
        "execution": "lease.request",
    }
    with pytest.raises(PermissionError, match="claimed"):
        journal.claim(identity, **arguments)
    for cursor in (0, 2, True):
        with pytest.raises((PermissionError, ValueError)):
            journal.publish(
                identity,
                **arguments,
                cursor=cursor,
                event={"type": "text_delta", "delta": "x"},
            )
    journal.publish(
        identity, **arguments, cursor=1, event={"type": "text_delta", "delta": "x"}
    )
    with pytest.raises(PermissionError):
        journal.publish(
            identity, **arguments, cursor=1, event={"type": "text_delta", "delta": "x"}
        )
    journal.publish(
        identity,
        **arguments,
        cursor=2,
        event={"type": "finish", "finish_reason": "stop"},
    )
    with pytest.raises(PermissionError):
        journal.publish(
            identity, **arguments, cursor=3, event={"type": "text_delta", "delta": "x"}
        )
    with pytest.raises(PermissionError, match="reserved"):
        journal.begin(_binding(), owner="owner", capture="capture")
    journal.clock = lambda: time.time() + 121
    with pytest.raises(PermissionError):
        journal.read(identity, owner="owner", capture="capture", cursor=0)


def test_progress_resource_reads_open_readonly_and_leave_journal_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resource reads must work without SQLite write access or journal mutation."""
    journal, identity = _journal(tmp_path)
    journal.publish(
        identity,
        owner="owner",
        capture="capture",
        producer="producer",
        producer_input_digest="input",
        execution="lease.request",
        cursor=1,
        event={"type": "text_delta", "delta": "first"},
    )
    original_mode = journal.path.stat().st_mode & 0o777
    before = journal.path.read_bytes(), journal.path.stat().st_mtime_ns
    before_rows = _journal_rows(journal.path)
    original_connect = sqlite3.connect
    connections: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connections.append((args, kwargs))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(progress_module.sqlite3, "connect", connect)
    try:
        journal.path.chmod(0o400)
        try:
            assert journal.find("turn", owner="owner", capture="capture") == identity
            assert journal.read(identity, owner="owner", capture="capture", cursor=0)[
                "events"
            ] == [{"cursor": 1, "event": {"type": "text_delta", "delta": "first"}}]
        finally:
            journal.path.chmod(original_mode)
    finally:
        monkeypatch.undo()
    assert connections == [
        ((f"{journal.path.resolve().as_uri()}?mode=ro",), {"timeout": 1, "uri": True}),
        ((f"{journal.path.resolve().as_uri()}?mode=ro",), {"timeout": 1, "uri": True}),
    ]
    assert (journal.path.read_bytes(), journal.path.stat().st_mtime_ns) == before
    assert _journal_rows(journal.path) == before_rows


def test_expired_progress_resource_does_not_delete_its_journal_rows(
    tmp_path: Path,
) -> None:
    """Expiry denies resource reads and waits for a future write to purge rows."""
    journal, identity = _journal(tmp_path)
    before = journal.path.read_bytes(), _journal_rows(journal.path)
    journal.clock = lambda: time.time() + 121

    with pytest.raises(KeyError, match="unavailable"):
        journal.find("turn", owner="owner", capture="capture")
    with pytest.raises(PermissionError, match="expired"):
        journal.read(identity, owner="owner", capture="capture", cursor=0)

    assert (journal.path.read_bytes(), _journal_rows(journal.path)) == before


def test_missing_progress_resource_journal_is_unavailable_without_creating_it(
    tmp_path: Path,
) -> None:
    """A missing owner journal is unavailable instead of being initialized by a read."""
    journal = TurnProgressJournal(tmp_path / "missing-progress.sqlite3")

    with pytest.raises(KeyError, match="unavailable"):
        journal.find("turn", owner="owner", capture="capture")

    assert not journal.path.exists()


def test_raw_provider_thinking_is_only_a_state_not_public_text() -> None:
    published: list[Any] = []
    stream = ProviderStream("openai", published.append)
    stream.receive(
        {"data": {"choices": [{"delta": {"reasoning_content": "private reasoning"}}]}}
    )
    assert published == [{"type": "thinking_delta", "delta": ""}]


def test_actual_lease_scope_rejects_profile_plan_request_and_epoch_changes(
    tmp_path: Path,
) -> None:
    authority, envelope = _dispatched_envelope(tmp_path)
    assert_dispatched_invocation(envelope, authority.store)
    for field, value in (
        ("profile_id", "other"),
        ("plan_digest", canonical_digest("other")),
        ("security_epoch", 99),
        ("request_id", "other"),
    ):
        with pytest.raises(PermissionError):
            assert_dispatched_invocation(
                replace(envelope, context=replace(envelope.context, **{field: value})),
                authority.store,
            )
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(
            replace(envelope, request_digest=canonical_digest("changed")),
            authority.store,
        )


def test_concurrent_actual_parent_leases_have_distinct_scopes_and_stable_owner(
    tmp_path: Path,
) -> None:
    """Two authorized parents share a Shell owner without sharing execution authority."""
    from core_runtime.bootstrap.production_v4 import _nested_host_provider_session_id
    from tests.test_tobkiri_host_authority_v4_adapter import _adapter, _queries

    authority, first = _dispatched_envelope(tmp_path)
    second_context = replace(first.context, request_id="second.request")
    digest = canonical_digest("second payload")
    _, query = _queries(authority, second_context, digest)
    adapter = _adapter(authority)
    second_lease = adapter.authorize_and_issue_lease(query)
    adapter.recheck_effect_boundary(
        second_context, first.target_principal, second_lease
    )
    second = replace(
        first, context=second_context, request_digest=digest, lease=second_lease
    )
    assert _nested_host_provider_session_id(first) == _nested_host_provider_session_id(
        second
    )
    assert execution_session_id(first) != execution_session_id(second)
    assert execution_session_id(first).startswith("session.host-execution.")
    registry, barrier = ParentInvocationScopesV4(), Barrier(2)

    def run(envelope: Any) -> None:
        scope = CapturedInvocationScopeV4(
            envelope, lambda: assert_dispatched_invocation(envelope, authority.store)
        )
        session = execution_session_id(envelope)
        registry.retain(session, scope)
        registry.retain(session, scope)
        barrier.wait(timeout=2)
        assert registry.lookup(session).envelope is envelope
        registry.lookup(session).assert_current()
        registry.release(session)
        assert registry.lookup(session).envelope is envelope
        barrier.wait(timeout=2)
        registry.release(session)
        assert registry.lookup(session) is None

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, (first, second)))
    registry.retain("foreign.session", CapturedInvocationScopeV4(first, lambda: None))
    with pytest.raises(PermissionError, match="parent changed"):
        registry.retain(
            "foreign.session", CapturedInvocationScopeV4(second, lambda: None)
        )
    registry.release("foreign.session")
    assert registry.lookup("foreign.session") is None


def test_revoked_actual_lease_is_fenced_before_host_contribution_entry(
    tmp_path: Path,
) -> None:
    """Generic entry guards protect a provider that performs no own lease check."""
    from types import SimpleNamespace
    from core_runtime.host_provider_backend_v4 import (
        ExactHostProviderBackendV4,
        HostProviderContributionV4,
    )

    authority, envelope = _dispatched_envelope(tmp_path)
    calls: list[Any] = []
    contribution = HostProviderContributionV4(
        envelope.contract_id,
        envelope.contract_version,
        envelope.operation_id,
        envelope.target_principal.value,
        authority.target.parent_artifact_digest,
        authority.target.function_implementation_digest,
        envelope.target_domain.value,
        lambda *_args: calls.append("entered") or {"ok": True},
    )
    backend = ExactHostProviderBackendV4(
        (contribution,),
        backend_id="scope.test",
        profile_id=envelope.context.profile_id,
        plan_digest=envelope.context.plan_digest,
        security_epoch=envelope.context.security_epoch,
        invocation_context=lambda request: SimpleNamespace(
            assert_current=lambda: assert_dispatched_invocation(
                request, authority.store
            )
        ),
    )
    backend.invoke(envelope)
    assert calls == ["entered"]
    authority.kernel.revoke(
        target_kind="function_principal",
        target_id=authority.target.principal_id,
        reason="entry test revoke",
    )
    with pytest.raises(PermissionError):
        backend.invoke(envelope)
    assert calls == ["entered"]


def test_tool_stage_is_bound_to_exact_producer_and_terminal_result(tmp_path: Path) -> None:
    """Only the claimed producer can append a finite tool stage."""
    journal, identity = _journal(tmp_path)
    arguments = dict(
        owner="owner",
        capture="capture",
        producer="producer",
        producer_input_digest="input",
        execution="lease.request",
    )
    started = {
        "type": "tool_started",
        "tool_id": "file/read",
        "tool_call_id": "call",
        "arguments": {"path": "sample.txt"},
    }
    with pytest.raises(PermissionError):
        journal.publish(identity, **{**arguments, "producer": "foreign"}, cursor=1, event=started)
    journal.publish(identity, **arguments, cursor=1, event=started)
    page = journal.read(identity, owner="owner", capture="capture", cursor=0)
    assert page["progress_id"] == identity
    assert page["provider_complete"] is False
    completed = {
        "type": "tool_completed",
        "tool_id": "file/read",
        "tool_call_id": "call",
        "status": "success",
        "content": '{"status":"success","result":"done","error":null}',
    }
    journal.publish(identity, **arguments, cursor=2, event=completed)
    assert (
        journal.read(identity, owner="owner", capture="capture", cursor=1)["provider_complete"]
        is True
    )
    with pytest.raises(PermissionError):
        journal.publish(identity, **arguments, cursor=3, event=started)


@pytest.mark.parametrize("extra", ["approved", "grant", "execution", "result"])
def test_tool_progress_events_cannot_carry_authority(extra: str) -> None:
    from tobkiri_protocol.turn_progress_v1 import validate_event

    with pytest.raises(ValueError):
        validate_event(
            {
                "type": "tool_started",
                "tool_id": "read",
                "tool_call_id": "call",
                "arguments": {},
                extra: True,
            }
        )


def test_stream_tool_fragments_normalize_without_publishing_execution_claims() -> None:
    """A tool round assembles exact JSON args and remains non-authoritative."""
    from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import create_tool_intent_operation

    published = []
    stream = ProviderStream("openai", published.append)
    for part in (
        {"index": 0, "id": "call-1", "function": {"name": "file.read", "arguments": '{"path":'}},
        {"index": 0, "function": {"arguments": '"sample.txt"}'}},
    ):
        stream.receive({"data": {"choices": [{"index": 0, "delta": {"tool_calls": [part]}}]}})
    stream.receive({"data": {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}})
    stream.receive({"done": True})
    result = stream.result()
    normalized = create_tool_intent_operation(None)("normalize", {
        "request_id": "request", "intents": result["tool_intents"],
    })["intents"]
    assert normalized == [{
        "intent_id": "call-1", "request_id": "request", "operation": "file.read",
        "arguments": {"path": "sample.txt"}, "authority_granted": False,
        "approved": False, "approval_status": "unrequested", "executes": False,
    }]
    assert published == [{"type": "finish", "finish_reason": "tool_calls"}]
