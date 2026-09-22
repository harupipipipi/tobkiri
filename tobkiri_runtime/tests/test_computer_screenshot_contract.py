from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract


def _controller(root: Path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import (
        BrowserComputerController,
    )

    controller = BrowserComputerController(artifact_root=root)
    controller._session_path = root / "state" / "browser_sessions.json"
    controller._approval_path = root / "state" / "approvals.json"
    return controller


def _capture_with(create_artifact):
    def capture(path, payload):
        create_artifact(path)
        return {"platform": "Darwin", "target_window": None}

    return capture


def test_controller_rejects_zero_byte_screenshot_without_leaking_path(tmp_path, monkeypatch):
    controller = _controller(tmp_path / "artifacts")
    monkeypatch.setattr(
        controller,
        "_capture_or_reuse_screenshot",
        _capture_with(lambda path: path.touch()),
    )

    result = controller.run("computer.screenshot", {"target": "desktop"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_ARTIFACT_NOT_CREATED"
    assert result["failure_stage"] == "artifact_validation"
    assert result["artifact_path_present"] is True
    assert result["artifact_file_created"] is False
    assert result["artifact_nonempty"] is False
    assert "path" not in result
    assert str(tmp_path) not in json.dumps(result)


def test_controller_rejects_symlink_screenshot_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    outside = tmp_path / "CANARY_OUTSIDE_SCREENSHOT.png"
    outside.write_bytes(b"png")
    controller = _controller(root)
    monkeypatch.setattr(
        controller,
        "_capture_or_reuse_screenshot",
        _capture_with(lambda path: path.symlink_to(outside)),
    )

    result = controller.run("computer.screenshot", {"target": "desktop"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_ARTIFACT_OUTSIDE_ROOT"
    assert result["artifact_symlink"] is True
    assert result["artifact_root_match"] is False
    assert str(outside) not in json.dumps(result)


def test_controller_rejects_cropped_artifact_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    outside = tmp_path / "CANARY_CROP_OUTSIDE.png"
    outside.write_bytes(b"png")
    controller = _controller(root)
    monkeypatch.setattr(
        controller,
        "_capture_or_reuse_screenshot",
        _capture_with(lambda path: path.write_bytes(b"png")),
    )
    monkeypatch.setattr(controller, "_apply_screenshot_crop", lambda *args: {"path": outside})

    result = controller.run("computer.screenshot", {"target": "desktop"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_ARTIFACT_OUTSIDE_ROOT"
    assert result["artifact_root_match"] is False
    assert str(outside) not in json.dumps(result)


def test_controller_target_resolution_failure_is_content_free(tmp_path, monkeypatch):
    controller = _controller(tmp_path / "artifacts")
    monkeypatch.setattr(
        controller,
        "_capture_or_reuse_screenshot",
        lambda path, payload: {
            "platform": "Darwin",
            "supported": False,
            "reason": "CANARY_PRIVATE_WINDOW_TITLE",
            "target_filter": {"title": "CANARY_PRIVATE_WINDOW_TITLE"},
        },
    )

    result = controller.run(
        "computer.screenshot",
        {"app": "ChatGPT Atlas", "title": "CANARY_PRIVATE_WINDOW_TITLE"},
        yolo_mode=True,
    )

    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_TARGET_UNAVAILABLE"
    assert result["failure_stage"] == "target_resolution"
    assert result["target_resolved"] is False
    assert result["capture_attempted"] is False
    assert result["capture_driver"] == "none"
    assert result["target_binding_source"] == "enumerated_match"
    assert "CANARY" not in json.dumps(result)


def test_controller_model_copy_exception_is_fixed_content_free_failure(tmp_path, monkeypatch):
    controller = _controller(tmp_path / "artifacts")
    monkeypatch.setattr(
        controller,
        "_capture_or_reuse_screenshot",
        _capture_with(lambda path: path.write_bytes(b"png")),
    )
    monkeypatch.setattr(
        controller,
        "_model_screenshot_copy",
        lambda path: (_ for _ in ()).throw(OSError("CANARY_PRIVATE_DISK_ERROR")),
    )

    result = controller.run("computer.screenshot", {"target": "desktop"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_MODEL_ARTIFACT_NOT_CREATED"
    assert result["failure_stage"] == "model_copy"
    assert result["artifact_file_created"] is True
    assert result["model_file_created"] is False
    assert "CANARY" not in json.dumps(result)


def _reported_screenshot(primary: Path, model: Path) -> dict[str, object]:
    return {
        "action": "computer.screenshot",
        "screenshot_path": str(primary),
        "model_image_path": str(model),
        "screenshot_supported": True,
        "target_resolved": True,
        "capture_attempted": True,
        "capture_succeeded": True,
        "artifact_path_present": True,
        "model_path_present": True,
        "artifact_file_created": True,
        "model_file_created": True,
        "artifact_root_match": True,
        "screenshot_contract_valid": True,
        "capture_driver": "mac_swift_host",
        "target_binding_source": "persisted_selection",
    }


@pytest.mark.parametrize("artifact_kind", ["zero", "symlink", "outside"])
def test_helper_revalidates_primary_screenshot_artifact(tmp_path, artifact_kind):
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    root = tmp_path / "artifacts"
    root.mkdir()
    model = root / "model.png"
    model.write_bytes(b"model")
    primary = root / "primary.png"
    if artifact_kind == "zero":
        primary.touch()
    elif artifact_kind == "symlink":
        outside = tmp_path / "CANARY_SYMLINK_TARGET.png"
        outside.write_bytes(b"outside")
        primary.symlink_to(outside)
    else:
        primary = tmp_path / "CANARY_OUTSIDE_PRIMARY.png"
        primary.write_bytes(b"outside")

    envelope = _computer_result_envelope(
        "computer.screenshot",
        _reported_screenshot(primary, model),
        artifact_root=root,
    )

    assert envelope["ok"] is False
    assert envelope["error_code"] == "SCREENSHOT_COMPLETION_NOT_VERIFIED"
    assert envelope["result"]["failure_stage"] == "helper_contract"
    assert envelope["result"]["screenshot_contract_valid"] is False
    serialized = json.dumps(envelope)
    assert str(primary) not in serialized
    assert "CANARY" not in serialized


def test_helper_revalidates_model_artifact_instead_of_trusting_boolean(tmp_path):
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    root = tmp_path / "artifacts"
    root.mkdir()
    primary = root / "primary.png"
    primary.write_bytes(b"primary")
    missing_model = root / "CANARY_MISSING_MODEL.png"

    envelope = _computer_result_envelope(
        "computer.screenshot",
        _reported_screenshot(primary, missing_model),
        artifact_root=root,
    )

    assert envelope["ok"] is False
    assert envelope["result"]["artifact_file_created"] is True
    assert envelope["result"]["model_file_created"] is False
    assert str(missing_model) not in json.dumps(envelope)


def test_screenshot_trace_facts_allow_only_fixed_contract_values():
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts

    facts = result_trace_facts(
        {
            "action": "computer.screenshot",
            "is_error": True,
            "screenshot_supported": True,
            "target_resolved": False,
            "capture_attempted": False,
            "capture_succeeded": False,
            "artifact_path_present": False,
            "model_path_present": False,
            "artifact_file_created": False,
            "model_file_created": False,
            "artifact_root_match": False,
            "screenshot_contract_valid": False,
            "capture_driver": "CANARY_PRIVATE_DRIVER",
            "target_binding_source": "CANARY_PRIVATE_TITLE",
            "failure_stage": "CANARY_PRIVATE_STAGE",
            "error_code": "CANARY_PRIVATE_ERROR",
            "screenshot_path": "/CANARY/private.png",
            "title": "CANARY_PRIVATE_TITLE",
            "pid": 99123,
        }
    )

    assert facts["screenshot_supported"] is True
    assert facts["screenshot_contract_valid"] is False
    assert facts.get("capture_driver") is None
    assert facts.get("target_binding_source") is None
    assert facts.get("failure_stage") is None
    assert facts.get("error_code") is None
    assert "CANARY" not in json.dumps(facts)


def test_defaultspack_block_hides_router_exception_text(monkeypatch):
    import ecosystem.defaultspack.blocks.tool.browser_computer as block

    monkeypatch.setattr(
        block,
        "run_computer_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("CANARY_PRIVATE_ROUTER_ERROR")),
    )

    result = block.run({"action": "computer.screenshot", "payload": {}}, {})

    assert result["status"] == "error"
    assert "CANARY" not in json.dumps(result)
    assert "Browser computer action failed." in json.dumps(result)


def test_direct_http_owned_workspace_reaches_helper_as_allowed_artifact_root(tmp_path, monkeypatch):
    from core_runtime.host_broker.computer_host_helper import (
        _computer_result_envelope,
        _validated_artifact_root,
    )
    import ecosystem.defaultspack.blocks.tool.browser_computer as block

    chat_store = tmp_path / "chat" / "conversations.json"
    workspace = chat_store.parent / "conversations" / "direct-http" / "workspace"
    artifact_root = workspace / "tools" / "computer"
    artifact_root.mkdir(parents=True)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(chat_store))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_DIRECT_CONVERSATION_WORKSPACE", str(workspace))
    captured = {}

    def fake_router(action, payload, context=None, *, artifact_root=None, **kwargs):
        captured["artifact_root"] = artifact_root
        allowed_root = _validated_artifact_root(str(artifact_root))
        primary = allowed_root / "primary.png"
        model = allowed_root / "model.png"
        primary.write_bytes(b"primary")
        model.write_bytes(b"model")
        reported = _reported_screenshot(primary, model)
        envelope = _computer_result_envelope(action, reported, artifact_root=allowed_root)
        assert envelope["ok"] is True
        return envelope["result"]

    monkeypatch.setattr(block, "run_computer_action", fake_router)

    response = block.run({"action": "computer.screenshot", "payload": {}}, {})

    assert response["status"] == "ok"
    assert captured["artifact_root"] == artifact_root.resolve()
    assert captured["artifact_root"] is not None


def test_direct_http_workspace_env_rejects_path_outside_owned_chat_store(tmp_path, monkeypatch):
    import ecosystem.defaultspack.blocks.tool.browser_computer as block

    chat_store = tmp_path / "chat" / "conversations.json"
    outside = tmp_path / "CANARY_OUTSIDE" / "workspace"
    outside.mkdir(parents=True)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(chat_store))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_DIRECT_CONVERSATION_WORKSPACE", str(outside))

    assert block._artifact_root({}) is None


def test_viewer_client_preserves_only_safe_screenshot_failure_facts(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret")
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {
            "ok": False,
            "error": {"code": "SCREENSHOT_COMPLETION_NOT_VERIFIED", "message": "CANARY_RAW_ERROR"},
            "result": {
                "screenshot_supported": True,
                "target_resolved": True,
                "capture_attempted": True,
                "capture_succeeded": True,
                "artifact_path_present": True,
                "model_path_present": True,
                "artifact_file_created": False,
                "model_file_created": False,
                "artifact_root_match": False,
                "screenshot_contract_valid": False,
                "capture_driver": "mac_swift_host",
                "target_binding_source": "persisted_selection",
                "failure_stage": "helper_contract",
                "screenshot_path": "/CANARY/private.png",
                "title": "CANARY_PRIVATE_TITLE",
                "unknown": "CANARY_UNKNOWN",
            },
        },
    )

    result = client.run_computer("computer.screenshot", {})

    assert result["error_code"] == "SCREENSHOT_COMPLETION_NOT_VERIFIED"
    assert result["reason"] == "Computer screenshot completion could not be verified."
    assert result["capture_driver"] == "mac_swift_host"
    assert result["failure_stage"] == "helper_contract"
    assert result["screenshot_contract_valid"] is False
    assert "CANARY" not in json.dumps(result)
    assert "screenshot_path" not in result
    assert "unknown" not in result
