"""Public observation is bounded independently of the durable run history."""
import base64
import hashlib

from core_runtime.workflow_v4.observation import observe_workflow_run
from tobkiri_protocol.canonical import canonical_json


def run():
    return {"run_id": "run-one", "definition_id": "voice", "revision_digest": "sha256:" + "1" * 64,
            "state": "succeeded", "inputs": {"private": "do-not-project"}}


def attempt(step, outcome, number=1):
    return {"run_id": "run-one", "step_id": step, "state": "succeeded", "attempt_number": number,
            "request": {"private": "do-not-project"}, "outcome": outcome}


def test_observation_excludes_inputs_requests_arbitrary_outputs_and_foreign_runs():
    observed = observe_workflow_run(run(), [
        attempt("one", {"text": "old"}), attempt("one", {"text": "new", "credential_handle": "private"}, 2),
        {**attempt("foreign", {"text": "foreign"}), "run_id": "run-other"},
    ])
    assert observed["step_count"] == 1
    assert observed["attempts"][0]["outcome"] == {"text": "new"}
    assert "private" not in str(observed)
    assert "foreign" not in str(observed)


def test_large_audio_observation_fits_canonical_envelope_without_duplicate_payloads():
    blob = b"a" * (1024 * 1024)
    content = {"content_id": "sha256:" + hashlib.sha256(blob).hexdigest(), "media_type": "audio/mpeg",
               "data_base64": base64.b64encode(blob).decode(), "byte_size": len(blob)}
    history = [attempt(f"audio-{index}", {"content": content}) for index in range(3)]
    raw_run = {**run(), "inputs": {"audio": content}}
    observed = observe_workflow_run(raw_run, history)
    assert len(canonical_json({"result": observed, "journal_metadata": "bounded"})) < 3 * 1024 * 1024
    assert observed["details_omitted"] is True
    assert len(observed["attempts"]) == 3
    assert sum("outcome" in row for row in observed["attempts"]) == 1


def test_status_count_and_text_preview_have_explicit_bounds():
    observed = observe_workflow_run(run(), [attempt(f"step-{index}", {"text": "a" * 100000}) for index in range(300)])
    assert len(observed["attempts"]) == 256
    assert observed["step_count"] == 300
    assert observed["details_omitted"] is True
    assert len(canonical_json(observed)) < 3 * 1024 * 1024


def test_advance_response_cannot_echo_full_parallel_audio_requests():
    from types import SimpleNamespace
    from core_runtime.workflow_v4.provider import WorkflowProviderV4

    blob = b"a" * (1024 * 1024)
    content = {"content_id": "sha256:" + hashlib.sha256(blob).hexdigest(), "media_type": "audio/mpeg",
               "data_base64": base64.b64encode(blob).decode(), "byte_size": len(blob)}
    history = [{**attempt(f"parallel-{index}", {"content": content}), "request": {"audio": content}} for index in range(3)]
    durable_run = {**run(), "inputs": {"audio": content}}
    calls = []
    engine = SimpleNamespace(
        advance_run=lambda run_id: calls.append(run_id) or {"run": durable_run, "attempts": history},
        store=SimpleNamespace(get_run=lambda _: durable_run, list_attempts=lambda _: history),
    )
    provider = WorkflowProviderV4(engine)
    observed = provider.invoke("run.advance", {"run_id": "run-one"})
    assert calls == ["run-one"]
    assert len(canonical_json(observed)) < 3 * 1024 * 1024
    assert "inputs" not in observed["run"]
    assert all("request" not in row for row in observed["attempts"])
    assert observed["step_count"] == 3
    assert observed["details_omitted"] is True


def test_bounded_observation_keeps_pending_approval_ahead_of_completed_history():
    pending = {**attempt("first-pending", {}), "state": "waiting_approval"}
    history = [pending, *[attempt(f"done-{index}", {"text": "done"}) for index in range(300)]]
    observed = observe_workflow_run(run(), history)
    assert len(observed["attempts"]) == 256
    assert observed["attempts"][0]["step_id"] == "first-pending"
    assert observed["attempts"][0]["state"] == "waiting_approval"
    assert observed["details_omitted"] is True


def test_only_waiting_approval_exposes_a_bounded_request_locator():
    locator = "workflow-approval-sha256:" + "a" * 64
    waiting = {**attempt("waiting", {}), "state": "waiting_approval", "authority_reservation_id": locator}
    observed = observe_workflow_run(run(), [waiting, {**waiting, "step_id": "done", "state": "succeeded"}, {**waiting, "step_id": "invalid", "authority_reservation_id": "not-a-request"}])
    rows = {row["step_id"]: row for row in observed["attempts"]}
    assert rows["waiting"]["approval_request_id"] == locator
    assert "approval_request_id" not in rows["done"]
    assert "approval_request_id" not in rows["invalid"]
    assert all("authority_reservation_id" not in row for row in rows.values())
