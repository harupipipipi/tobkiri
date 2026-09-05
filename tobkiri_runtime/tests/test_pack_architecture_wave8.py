"""Focused Wave 8 boundary tests (defined, not run by implementer)."""

from __future__ import annotations

import json
from pathlib import Path

from ecosystem.rumi_browser_host_service_pack.runtime.service import (
    create_browser_control,
    create_browser_observer,
)
from ecosystem.rumi_clipboard_host_service_pack.runtime.service import (
    create_clipboard_reader,
    create_clipboard_writer,
)
from ecosystem.rumi_desktop_host_service_pack.runtime.service import (
    create_desktop_control,
    create_desktop_observer,
)
from ecosystem.rumi_media_capture_host_service_pack.runtime.service import (
    create_media_capture,
)


CALLER = {
    "_contract_consumer_pack_id": "test.consumer",
    "_contract_consumer_function_id": "test.function",
}


def test_browser_observe_and_control_are_disjoint() -> None:
    observe = create_browser_observer()
    control = create_browser_control()
    denied = observe.invoke("browser.navigate", {**CALLER, "url": "https://example.test"})
    intent = control.invoke("browser.navigate", {**CALLER, "url": "https://example.test"})
    assert denied["status"] == "denied"
    assert intent["type"] == "host_intent"
    assert intent["operation"] == "host.intent.execute"
    assert intent["host_function_id"] == "browser.open_url"
    assert intent["caller"]["pack_id"] == ""
    assert "_contract_consumer_pack_id" not in intent["args"]


def test_desktop_observe_cannot_emit_input() -> None:
    observe = create_desktop_observer()
    control = create_desktop_control()
    assert observe.invoke("desktop.pointer.click", CALLER)["status"] == "denied"
    intent = control.invoke("desktop.pointer.click", {**CALLER, "x": 10, "y": 20})
    assert intent["operation"] == "host.intent.execute"
    assert intent["host_function_id"] == "computer.click"


def test_client_approval_material_is_rejected() -> None:
    service = create_desktop_control()
    result = service.invoke(
        "desktop.keyboard.type",
        {**CALLER, "text": "hello", "approved": True},
    )
    assert result["error_type"] == "client_authority_material_forbidden"


def test_clipboard_read_write_are_separate_and_bounded() -> None:
    reader = create_clipboard_reader()
    writer = create_clipboard_writer()
    assert reader.invoke(CALLER)["host_function_id"] == "computer.clipboard.read"
    assert writer.invoke({**CALLER, "text": "hello"})[
        "host_function_id"
    ] == "computer.clipboard.write"
    oversized = writer.invoke({**CALLER, "text": "x" * 1_048_577})
    assert oversized["error_type"] == "clipboard_text_too_large"


def test_media_capture_duration_is_hard_bounded() -> None:
    service = create_media_capture()
    result = service.invoke(
        "host.microphone.capture",
        {**CALLER, "duration_ms": 300_001},
    )
    assert result["error_type"] == "capture_duration_out_of_range"


def test_default_tools_adapter_has_no_direct_host_driver_imports() -> None:
    root = Path(__file__).parents[1] / "ecosystem" / "rumi_default_tools_pack"
    targets = [
        root / "functions" / "browser_computer" / "main.py",
        root / "functions" / "computer_observe" / "main.py",
        root / "domain" / "tool" / "host_contract_adapter.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in targets)
    for forbidden in (
        "BrowserComputerController",
        "create_default_computer_tool_service",
        "yolo_mode=",
        "viewer_host_approved",
        "subprocess.run",
    ):
        assert forbidden not in source


def test_viewer_helper_does_not_import_legacy_tool_controller() -> None:
    root = Path(__file__).parents[1]
    helper = (root / "core_runtime" / "host_broker" / "computer_host_helper.py").read_text(
        encoding="utf-8"
    )
    assert "domain.tool" not in helper
    assert "BrowserComputerController" not in helper
    assert "invoke_global_contract" in helper
    assert "_run_v4_host_action" in helper
    assert "computer_action_trace" in helper


def test_defaultspack_media_has_no_clipboard_or_capture_executor() -> None:
    root = Path(__file__).parents[1] / "ecosystem" / "defaultspack"
    processor = (root / "domain" / "media" / "processor.py").read_text(
        encoding="utf-8"
    )
    adapter = (root / "domain" / "media" / "contract_adapter.py").read_text(
        encoding="utf-8"
    )
    assert "pbcopy" not in processor
    assert "take_screenshot" not in processor
    assert "invoke_global_contract" in adapter


def test_defaultspack_coding_blocks_use_wave8_contract_adapter() -> None:
    root = Path(__file__).parents[1] / "ecosystem" / "defaultspack"
    targets = [
        root / "blocks" / "coding" / "terminal_stream.py",
        root / "blocks" / "coding" / "sandbox_common.py",
        root / "blocks" / "coding" / "workspace" / "_contract.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in targets)
    for forbidden in (
        "domain.coding.terminal import Terminal",
        "SandboxWorkspaceManager",
        "ManagedSandboxSupervisor",
        "WorkspaceStore",
        "subprocess.run",
        "subprocess.Popen",
    ):
        assert forbidden not in source
    assert "TERMINAL_CONTROL" in source
    assert "SANDBOX_CONTROL" in source
    assert "WORKSPACE_ACTION" in source


def test_workspace_action_receipt_binds_revision_and_metadata() -> None:
    source = (
        Path(__file__).parents[1]
        / "ecosystem"
        / "rumi_workspace_mount_pack"
        / "runtime"
        / "mounts.py"
    ).read_text(encoding="utf-8")
    assert '"expected_revision"' in source
    assert 'arguments["metadata"]' in source
    assert 'f"workspace.{name}"' in source
    assert 'authority": "workspace.mount.manage"' in source


def test_wave8_artifact_hashes_match_static_source() -> None:
    root = Path(__file__).parents[1] / "ecosystem"
    pack_ids = (
        "rumi_host_authority_bridge_pack",
        "rumi_workspace_mount_pack",
        "rumi_file_inspect_pack",
        "rumi_file_mutation_pack",
        "rumi_file_patch_pack",
        "rumi_shell_policy_pack",
        "rumi_shell_execute_pack",
        "rumi_terminal_session_pack",
        "rumi_git_read_pack",
        "rumi_git_write_pack",
        "rumi_git_publish_pack",
        "rumi_ide_bridge_service_pack",
        "rumi_coding_sandbox_service_pack",
        "rumi_browser_host_service_pack",
        "rumi_desktop_host_service_pack",
        "rumi_clipboard_host_service_pack",
        "rumi_media_capture_host_service_pack",
        "rumi_media_inspect_service_pack",
        "rumi_media_analysis_adapter_pack",
    )
    import hashlib

    for pack_id in pack_ids:
        pack_root = root / pack_id
        manifest = json.loads(
            (pack_root / "artifact-manifest.json").read_text(encoding="utf-8")
        )
        for artifact in manifest["artifacts"]:
            content = (pack_root / artifact["path"]).read_bytes()
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
