from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
MIC_PERMISSION = "host.microphone.capture"
CAMERA_PERMISSION = "host.camera.capture"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_ambient_router_requires_enabled_monitor_and_rumi_permissions(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    disabled = router.submit_event(
        {
            "source": "microphone",
            "trigger": "voice_wake",
            "audio_embedding": [1.0, 0.0, 0.0],
        }
    )
    assert disabled["status"] == "ignored"
    assert disabled["reason"] == "ambient_monitor.disabled"

    router.start_monitor()
    denied = router.submit_event(
        {
            "source": "microphone",
            "trigger": "voice_wake",
            "audio_embedding": [1.0, 0.0, 0.0],
        }
    )
    assert denied["status"] == "denied"
    assert set(denied["missing_permissions"]) == {MIC_PERMISSION, "ambient.trigger.dispatch"}


def test_voice_wake_enrolls_first_audio_sample_and_matches_by_embedding(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    enrolled = router.submit_event(
        {
            "source": "microphone",
            "trigger": "voice_wake",
            "audio_embedding": [1.0, 0.0, 0.0],
            "wake_phrase": "this text must not be used",
            "metadata": {"audio_blob": "raw-audio", "image_frame": "raw-image"},
        }
    )
    assert enrolled["status"] == "enrolled"
    assert enrolled["reason"] == "voice_wake.first_sample_enrolled"

    rejected = router.submit_event(
        {
            "source": "microphone",
            "trigger": "voice_wake",
            "audio_embedding": [0.0, 1.0, 0.0],
            "wake_phrase": "this text must not be used",
        }
    )
    assert rejected["status"] == "ignored"
    assert rejected["reason"] == "voice_wake.classifier_rejected"

    matched = router.submit_event(
        {
            "source": "microphone",
            "trigger": "voice_wake",
            "audio_embedding": [0.98, 0.02, 0.0],
            "wake_phrase": "different text still matches by audio embedding",
        }
    )
    assert matched["status"] == "open_input"
    assert matched["focus_composer"] is True

    audit_records = [
        json.loads(line)
        for line in (tmp_path / "ambient-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    forbidden_keys = {"audio_embedding", "wake_phrase", "audio_blob", "image_frame", "dataUrl"}
    assert not _contains_any_key(audit_records, forbidden_keys)


def test_pinch_and_agent_dispatch_share_ambient_router(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.materialization import (
        AMBIENT_FINGER_RECORDING_AI_INPUT_ID,
        AMBIENT_FINGER_RECORDING_CONTEXT_POLICY_ID,
    )
    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    pinch_start = router.submit_event(
        {
            "source": "camera",
            "trigger": "pinch",
            "confidence": 0.93,
            "duration_ms": 420,
            "mode": "record_audio_start",
            "metadata": {"hand": "Right", "normalized_distance": 0.21},
        }
    )
    assert pinch_start["status"] == "recording_started"
    assert pinch_start["capture_started"] is True

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": "", "conversation_id": "conv-1"}) as submit:
        pinch_release = router.submit_event(
            {
                "source": "camera",
                "trigger": "pinch",
                "confidence": 0.94,
                "duration_ms": 900,
                "mode": "dispatch_audio",
                "conversation_id": "conv-1",
                "attachments": [
                    {
                        "name": "pinch.webm",
                        "type": "audio/webm",
                        "size": 1234,
                        "dataUrl": "data:audio/webm;base64,AAAA",
                        "ephemeral": True,
                        "do_not_persist": True,
                        "transcript": "今日の予定を確認して",
                        "transcript_source": "web_speech_api",
                    }
                ],
                "metadata": {"hand": "Right", "normalized_distance": 0.42},
            },
            {"conversation_id": "conv-1"},
        )

    assert pinch_release["status"] == "ok"
    assert pinch_release["conversation_id"] == "conv-1"
    envelope = submit.call_args.args[0]
    assert envelope.delivery["action_id"] == "chat.message"
    assert envelope.input == "今日の予定を確認して"
    assert "pinch.webm" not in envelope.input
    assert envelope.params["tool_policy"] == {
        "template_ai_input_id": AMBIENT_FINGER_RECORDING_AI_INPUT_ID,
        "template_tool_policy_id": "ambient_finger_recording_tools",
    }
    assert envelope.metadata["ambient"]["template"] == {
        "ai_input_id": AMBIENT_FINGER_RECORDING_AI_INPUT_ID,
        "context_policy_id": AMBIENT_FINGER_RECORDING_CONTEXT_POLICY_ID,
    }
    assert envelope.attachments[0]["type"] == "audio/webm"
    assert "dataUrl" not in envelope.attachments[0]
    assert envelope.attachments[0]["do_not_persist"] is True

    router.submit_event(
        {
            "source": "microphone",
            "trigger": "voice_wake",
            "audio_embedding": [1.0, 0.0, 0.0],
        }
    )
    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        dispatched = router.submit_event(
            {
                "source": "microphone",
                "trigger": "voice_wake",
                "audio_embedding": [1.0, 0.0, 0.0],
                "input_text": "delegate this task",
                "action_id": "agent.delegate",
                "conversation_id": "conv-1",
            },
            {"conversation_id": "conv-1"},
        )

    assert dispatched["status"] == "ok"
    envelope = submit.call_args.args[0]
    assert envelope.delivery["action_id"] == "agent.delegate"
    assert envelope.source["provider"] == "ambient"
    assert envelope.target["conversation_id"] == "conv-1"


def test_ambient_audio_payload_aliases_materialize_ephemeral_attachment_metadata():
    from domain.ambient.materialization import materialize_ambient_event_attachments

    attachments = materialize_ambient_event_attachments(
        {
            "audioDataUrl": "data:audio/webm;base64,AAAA",
            "audio_mime_type": "audio/webm",
            "audio_name": "ambient-pinch-1.webm",
            "audio_size": 4,
            "transcription": "録音メモ",
            "transcript_source": "web_speech_api",
        },
        event_id="evt-audio",
    )

    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["name"] == "ambient-pinch-1.webm"
    assert attachment["type"] == "audio/webm"
    assert attachment["source"] == "ambient.camera_pinch_hold"
    assert attachment["ephemeral"] is True
    assert attachment["do_not_persist"] is True
    assert attachment["transcription"] == "録音メモ"
    assert attachment["transcript_source"] == "web_speech_api"
    assert attachment["metadata"]["ambient_event_id"] == "evt-audio"
    assert attachment["metadata"]["privacy"] == "ephemeral_audio"


def test_ambient_audio_payload_dedupes_top_level_and_attachment_audio():
    from domain.ambient.materialization import materialize_ambient_event_attachments

    attachments = materialize_ambient_event_attachments(
        {
            "audio_data_url": "data:audio/webm;base64,AAAA",
            "audio_mime_type": "audio/webm",
            "audio_name": "ok-mark-recording.webm",
            "attachments": [
                {
                    "id": "ambient-audio-1",
                    "name": "ok-mark-recording.webm",
                    "type": "audio/webm",
                    "dataUrl": "data:audio/webm;base64,AAAA",
                    "ephemeral": True,
                    "do_not_persist": True,
                }
            ],
        },
        event_id="evt-audio",
    )

    assert len(attachments) == 1
    assert attachments[0]["id"] == "ambient-audio-1"
    assert attachments[0]["metadata"]["ambient_event_id"] == "evt-audio"


def test_debug_qa_ok_mark_dispatch_strips_media_and_keeps_transcript_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": "", "conversation_id": "conv-debug"}) as submit:
        dispatched = router.submit_event(
            {
                "source": "camera",
                "trigger": "pinch",
                "confidence": 1,
                "duration_ms": 0,
                "mode": "dispatch_audio",
                "action_id": "chat.message",
                "input_text": "文字起こし:\nブラウザQAです",
                "conversation_id": "conv-debug",
                "metadata": {
                    "panel": "ambient_mini_window",
                    "debug_qa": True,
                    "simulated_ok_mark": True,
                },
                "attachments": [
                    {
                        "name": "debug-ok-mark.webm",
                        "type": "audio/webm",
                        "size": 0,
                        "dataUrl": "data:audio/webm;base64,AAAA",
                        "audio": "raw-audio",
                        "blob": "raw-blob",
                        "ephemeral": True,
                        "do_not_persist": True,
                        "transcript": "ブラウザQAです",
                        "transcript_source": "debug_qa",
                    }
                ],
            },
            {"conversation_id": "conv-debug"},
        )

    assert dispatched["status"] == "ok"
    envelope = submit.call_args.args[0]
    assert envelope.input == "ブラウザQAです"
    assert envelope.metadata["ambient"]["metadata"]["debug_qa"] is True
    assert envelope.metadata["ambient"]["metadata"]["simulated_ok_mark"] is True
    attachment = envelope.attachments[0]
    assert attachment["type"] == "audio/webm"
    assert attachment["size"] == 0
    assert attachment["ephemeral"] is True
    assert attachment["do_not_persist"] is True
    assert attachment["transcript"] == "ブラウザQAです"
    assert attachment["transcript_source"] == "debug_qa"
    assert "dataUrl" not in attachment
    assert "audio" not in attachment
    assert "blob" not in attachment


def test_gesture_choice_does_not_dispatch_numeric_reply_without_audio(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        ignored = router.submit_event(
            {
                "source": "camera",
                "trigger": "gesture_choice",
                "mode": "choice_response",
                "choice": 3,
                "confidence": 0.96,
                "duration_ms": 3000,
                "conversation_id": "conv-choice",
                "metadata": {"hold_ms": 3000, "pinch_armed": True},
            },
            {"conversation_id": "conv-choice"},
        )

    assert ignored["status"] == "ignored"
    assert ignored["reason"] == "gesture_choice.chat_dispatch_disabled"
    submit.assert_not_called()


def test_ambient_routing_can_create_session_or_per_trigger_chats(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "conversations.json"))

    from domain.ambient.router import AmbientTriggerRouter
    from domain.chat.store import ChatStore

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({
        "routing": {
            "mode": "startup_new_chat",
            "group_id": "gesture",
            "group_title": "Gesture",
            "model": "opencode-go/kimi-k2.6",
        }
    })

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        first = router.submit_event(_pinch_audio_payload())
        second = router.submit_event(_pinch_audio_payload())

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    first_envelope = submit.call_args_list[0].args[0]
    second_envelope = submit.call_args_list[1].args[0]
    assert first_envelope.target["conversation_id"] == second_envelope.target["conversation_id"]
    created = ChatStore().get_conversation(first_envelope.target["conversation_id"])
    assert created["group_id"] == "gesture"
    assert created["model"] == "opencode-go/kimi-k2.6"

    router.configure({"routing": {"mode": "always_new_chat", "group_id": "gesture"}})
    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        router.submit_event(_pinch_audio_payload())
        router.submit_event(_pinch_audio_payload())

    assert submit.call_args_list[0].args[0].target["conversation_id"] != submit.call_args_list[1].args[0].target["conversation_id"]

    router.configure({"routing": {"mode": "always_new_chat", "group_enabled": False, "group_id": "gesture"}})
    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        router.submit_event(_pinch_audio_payload())

    ungrouped_id = submit.call_args.args[0].target["conversation_id"]
    ungrouped = ChatStore().get_conversation(ungrouped_id)
    assert ungrouped["group_id"] is None
    assert "group_id" not in ungrouped.get("metadata", {})


def test_selected_chat_routing_passes_saved_model_as_turn_param(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({
        "routing": {
            "mode": "selected_chat",
            "conversation_id": "conv-selected",
            "model": "opencode-go/kimi-k2.6",
        }
    })

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        dispatched = router.submit_event(_pinch_audio_payload(), {"conversation_id": "conv-selected"})

    assert dispatched["status"] == "ok"
    envelope = submit.call_args.args[0]
    assert envelope.target["conversation_id"] == "conv-selected"
    assert envelope.chat["conversation_id"] == "conv-selected"
    assert envelope.params["model"] == "opencode-go/kimi-k2.6"
    assert envelope.params["tool_policy"]["template_ai_input_id"] == "ambient_finger_recording"


def test_finger_recording_dispatch_merges_template_policy_with_selected_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    payload = _pinch_audio_payload()
    payload["params"] = {
        "tool_selection": {
            "mode": "manual",
            "include": ["browser", "local_file"],
            "scope": "turn",
            "must_use": False,
        },
        "tool_policy": {
            "selected_tools": ["browser", "local_file"],
        },
    }

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": "", "conversation_id": "conv-tools"}) as submit:
        dispatched = router.submit_event(payload, {"conversation_id": "conv-tools"})

    assert dispatched["status"] == "ok"
    envelope = submit.call_args.args[0]
    assert envelope.tools == ["browser", "local_file"]
    assert envelope.params["tool_selection"] == {
        "mode": "manual",
        "include": ["browser", "local_file"],
        "scope": "turn",
        "must_use": False,
    }
    assert envelope.params["tool_policy"] == {
        "selected_tools": ["browser", "local_file"],
        "template_ai_input_id": "ambient_finger_recording",
        "template_tool_policy_id": "ambient_finger_recording_tools",
    }
    assert envelope.metadata["ambient"]["template"] == {
        "ai_input_id": "ambient_finger_recording",
        "context_policy_id": "ambient_audio_transcript",
    }


def test_ambient_template_catalog_projects_settings_policy_and_external_hook():
    from domain.templates.projectors import build_template_catalog

    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)
    template_ids = {item["id"] for item in catalog["templates"]}
    ai_input_ids = {item["id"] for item in catalog["ai_inputs"]}
    context_policy_ids = {item["id"] for item in catalog["context_policies"]}
    tool_policy_ids = {item["id"] for item in catalog["tool_policies"]}
    permission_ids = {item["permission_id"] for item in catalog["permissions"]}
    external_template_ids = {item["id"] for item in catalog["external_io_templates"]}
    ambient_section = next(item for item in catalog["settings_sections"] if item["id"] == "ambient")
    ambient_fields = {item["id"]: item for item in ambient_section["fields"]}

    assert "rumi.ambient_trigger.default" in template_ids
    assert "ambient_finger_recording" in ai_input_ids
    assert "ambient_audio_transcript" in context_policy_ids
    assert "ambient_finger_recording_tools" in tool_policy_ids
    assert {"host.microphone.capture", "host.camera.capture", "ambient.trigger.dispatch"} <= permission_ids
    assert "ambient.input.webhook" in external_template_ids
    assert ambient_fields["ambient.monitor.enabled"]["type"] == "toggle"
    assert ambient_fields["ambient.camera.lock"]["type"] == "device_lock"
    assert ambient_fields["ambient.camera.lock"]["visible_when"] == {
        "field": "ambient.monitor.enabled",
        "truthy": True,
    }
    assert ambient_fields["ambient.routing.mode"]["type"] == "select"
    assert ambient_fields["ambient.routing.model"]["type"] == "model_select"
    assert ambient_fields["ambient.routing.model"]["visible_when"] == {
        "field": "ambient.monitor.enabled",
        "truthy": True,
    }
    assert ambient_fields["ambient.routing.group_enabled"]["type"] == "toggle"
    assert ambient_fields["ambient.provider_keys"]["type"] == "api_key_setup"


def test_always_new_chat_uses_selected_route_model_for_created_conversation(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "conversations.json"))

    from domain.ambient.router import AmbientTriggerRouter
    from domain.chat.store import ChatStore

    route_model = "opencode-go/deepseek-v4-flash"
    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({"routing": {"mode": "always_new_chat", "group_id": "gesture", "model": route_model}})

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        dispatched = router.submit_event(_pinch_audio_payload())

    assert dispatched["status"] == "ok"
    envelope = submit.call_args.args[0]
    created = ChatStore().get_conversation(envelope.target["conversation_id"])
    assert created["model"] == route_model
    assert envelope.chat["model"] == route_model
    assert envelope.params["model"] == route_model
    serialized = json.dumps({"conversation": created, "chat": envelope.chat, "params": envelope.params}, ensure_ascii=False)
    assert "ollama/llama3.1:8b" not in serialized


def test_always_new_chat_prefers_current_conversation_model_when_route_model_blank(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "conversations.json"))

    from domain.ambient.router import AmbientTriggerRouter
    from domain.chat.store import ChatStore

    current_model = "opencode-go/deepseek-v4-flash"
    source_conversation = ChatStore().create_conversation(model=current_model)
    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({"routing": {"mode": "always_new_chat", "group_id": "gesture", "model": ""}})

    payload = _pinch_audio_payload()
    payload["conversation_id"] = source_conversation["id"]
    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        dispatched = router.submit_event(payload, {"conversation_id": source_conversation["id"]})

    assert dispatched["status"] == "ok"
    envelope = submit.call_args.args[0]
    assert envelope.target["conversation_id"] != source_conversation["id"]
    created = ChatStore().get_conversation(envelope.target["conversation_id"])
    assert created["model"] == current_model
    assert envelope.chat["model"] == current_model
    assert envelope.params["model"] == current_model
    serialized = json.dumps({"conversation": created, "chat": envelope.chat, "params": envelope.params}, ensure_ascii=False)
    assert "ollama/llama3.1:8b" not in serialized


def test_selected_chat_uses_conversation_model_instead_of_stale_route_model(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "conversations.json"))

    from domain.ambient.router import AmbientTriggerRouter
    from domain.chat.store import ChatStore

    selected_model = "opencode-go/deepseek-v4-flash"
    selected = ChatStore().create_conversation(model=selected_model)
    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({
        "routing": {
            "mode": "selected_chat",
            "conversation_id": selected["id"],
            "model": "ollama/llama3.1:8b",
        }
    })

    payload = _pinch_audio_payload()
    payload["model"] = "ollama/llama3.1:8b"
    payload["params"] = {"model": "ollama/llama3.1:8b"}

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit:
        dispatched = router.submit_event(payload, {"conversation_id": selected["id"]})

    assert dispatched["status"] == "ok"
    assert dispatched["resolved_model"] == selected_model
    envelope = submit.call_args.args[0]
    assert envelope.params["model"] == selected_model
    assert envelope.chat["conversation_id"] == selected["id"]
    assert "ollama/llama3.1:8b" not in json.dumps(envelope.as_dict(), ensure_ascii=False)


def test_ambient_audio_release_without_transcript_transcribes_for_text_model(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    transcript = "会議メモをまとめて"
    with (
        patch(
            "domain.ambient.router.transcribe_ambient_audio",
            return_value={
                "status": "ok",
                "text": transcript,
                "source": "openai",
                "model": "openai/gpt-4o-mini-transcribe",
                "results": [{"index": 0, "text": transcript, "source": "openai", "model": "openai/gpt-4o-mini-transcribe"}],
            },
        ) as transcribe,
        patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": ""}) as submit,
    ):
        dispatched = router.submit_event(_pinch_audio_payload(transcript=None), {"conversation_id": "conv-text"})

    assert dispatched["status"] == "ok"
    transcribe.assert_called_once()
    envelope = submit.call_args.args[0]
    assert envelope.input == transcript
    assert envelope.attachments[0]["transcript"] == transcript
    assert "dataUrl" not in envelope.attachments[0]
    assert "data:audio" not in json.dumps(envelope.attachments, ensure_ascii=False)


def test_ambient_transcription_test_runs_without_monitor_and_does_not_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    transcript = "マイクテストです"
    with (
        patch(
            "domain.ambient.router.transcribe_ambient_audio",
            return_value={
                "status": "ok",
                "text": transcript,
                "source": "local_whisper",
                "model": "local-whisper",
                "results": [{"index": 0, "text": transcript, "source": "local_whisper", "model": "local-whisper"}],
            },
        ) as transcribe,
        patch("domain.ambient.router.submit_input") as submit,
    ):
        result = router.submit_event(
            {
                "source": "microphone",
                "trigger": "transcription_test",
                "mode": "transcribe_audio_test",
                "audio_data_url": "data:audio/webm;base64,AAAA",
                "audio_mime_type": "audio/webm",
            }
        )

    assert result["status"] == "ok"
    assert result["transcript"] == transcript
    assert result["transcription"]["source"] == "local_whisper"
    transcribe.assert_called_once()
    assert transcribe.call_args.kwargs["target_supports_audio"] is False
    submit.assert_not_called()

    audit_text = (tmp_path / "ambient-audit.jsonl").read_text(encoding="utf-8")
    assert transcript not in audit_text
    assert "data:audio" not in audit_text


def test_local_whisper_status_detects_bundled_whisper_cpp(monkeypatch, tmp_path):
    from domain.ambient.local_transcription import local_whisper_status

    app_dir = tmp_path / "app"
    bin_dir = app_dir / "bundled" / "whisper" / "bin"
    model_dir = app_dir / "bundled" / "whisper" / "models"
    bin_dir.mkdir(parents=True)
    model_dir.mkdir(parents=True)
    whisper_cli = bin_dir / ("whisper-cli.exe" if sys.platform.startswith("win") else "whisper-cli")
    whisper_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    whisper_cli.chmod(0o755)
    (model_dir / "ggml-tiny.bin").write_bytes(b"tiny")

    monkeypatch.setenv("RUMI_APP_DIR", str(app_dir))
    monkeypatch.delenv("RUMI_LOCAL_WHISPER_COMMAND", raising=False)
    monkeypatch.delenv("RUMI_WHISPER_CPP_BIN", raising=False)
    monkeypatch.delenv("WHISPER_CPP_BIN", raising=False)
    monkeypatch.delenv("RUMI_LOCAL_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("WHISPER_CPP_MODEL", raising=False)
    monkeypatch.delenv("RUMI_WHISPER_MODEL_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    status = local_whisper_status()

    assert status["configured"] is True
    assert status["status"] == "local_whisper_configured"
    assert status["command"] == str(whisper_cli)
    assert status["model"] == str(model_dir / "ggml-tiny.bin")
    assert status["model_quality"] == "fast"


def test_local_whisper_prefers_small_then_base_before_tiny(monkeypatch, tmp_path):
    from domain.ambient.local_transcription import local_whisper_status

    app_dir = tmp_path / "app"
    bin_dir = app_dir / "bundled" / "whisper" / "bin"
    model_dir = app_dir / "bundled" / "whisper" / "models"
    bin_dir.mkdir(parents=True)
    model_dir.mkdir(parents=True)
    whisper_cli = bin_dir / "whisper-cli"
    whisper_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    whisper_cli.chmod(0o755)
    for name in ("ggml-tiny.bin", "ggml-base.bin", "ggml-small.bin"):
        (model_dir / name).write_bytes(name.encode("utf-8"))

    monkeypatch.setenv("RUMI_APP_DIR", str(app_dir))
    _clear_local_whisper_env(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    status = local_whisper_status()

    assert status["model"] == str(model_dir / "ggml-small.bin")
    assert status["model_quality"] == "quality"


def test_local_whisper_status_treats_custom_command_as_executable_without_model(monkeypatch, tmp_path):
    from domain.ambient import local_transcription

    _clear_local_whisper_env(monkeypatch)
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_COMMAND", "/mock/transcribe --input {audio}")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setattr(local_transcription, "_configured_model", lambda _env: "")

    status = local_transcription.local_whisper_status()

    assert status["configured"] is True
    assert status["status"] == "local_whisper_configured"
    assert status["command"] == "/mock/transcribe --input {audio}"
    assert status["engine"] == "command"
    assert status["model"] == ""


def test_local_whisper_status_detects_faster_whisper_library(monkeypatch):
    from domain.ambient import local_transcription

    _clear_local_whisper_env(monkeypatch)
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_ALLOW_DOWNLOAD", "1")
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_MODEL", "small")
    monkeypatch.setattr(local_transcription, "_configured_command", lambda _env: None)
    monkeypatch.setattr(local_transcription, "_has_python_module", lambda name: name == "faster_whisper")

    status = local_transcription.local_whisper_status()

    assert status["configured"] is True
    assert status["status"] == "local_whisper_configured"
    assert status["engine"] == "faster_whisper"


def test_local_whisper_status_detects_openai_whisper_library(monkeypatch):
    from domain.ambient import local_transcription

    _clear_local_whisper_env(monkeypatch)
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_ALLOW_DOWNLOAD", "1")
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_MODEL", "base")
    monkeypatch.setattr(local_transcription, "_configured_command", lambda _env: None)
    monkeypatch.setattr(local_transcription, "_has_python_module", lambda name: name == "whisper")

    status = local_transcription.local_whisper_status()

    assert status["configured"] is True
    assert status["status"] == "local_whisper_configured"
    assert status["engine"] == "whisper"


def test_text_model_audio_uses_local_whisper_fallback(monkeypatch, tmp_path):
    _clear_local_whisper_env(monkeypatch)
    monkeypatch.setenv("RUMI_AMBIENT_TRANSCRIPTION_LANGUAGE", "ja")
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH",
        str(tmp_path / "ambient-audit.jsonl"),
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "conversations.json"))

    from domain.ambient.router import AmbientTriggerRouter

    class NoTranscriptionClient:
        def __init__(self):
            self._providers = {}

        def transcribe(self, model, audio, params):
            raise AssertionError("cloud transcription should not be attempted")

    route_model = "opencode-go/deepseek-v4-flash"
    transcript = "録音をローカルで文字起こししました"
    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({
        "routing": {"mode": "always_new_chat", "group_id": "gesture", "model": route_model}
    })

    with (
        patch("domain.ai_client.client.AIClient", return_value=NoTranscriptionClient()),
        patch(
            "domain.ambient.local_transcription.transcribe_local_audio",
            return_value={
                "status": "ok",
                "text": transcript,
                "source": "local_whisper",
                "model": "local-whisper:ggml-base.bin",
            },
        ) as local_transcribe,
        patch(
            "domain.ambient.router.submit_input",
            return_value={"status": "ok", "assistant_text": ""},
        ) as submit,
    ):
        dispatched = router.submit_event(_pinch_audio_payload(transcript=None))

    assert dispatched["status"] == "ok"
    local_transcribe.assert_called_once()
    assert local_transcribe.call_args.kwargs["language"] == "ja"
    assert "BlackHole" in local_transcribe.call_args.kwargs["prompt"]
    assert "OKマーク" in local_transcribe.call_args.kwargs["prompt"]
    envelope = submit.call_args.args[0]
    assert envelope.input == transcript
    assert "pinch.webm" not in envelope.input
    assert envelope.attachments[0]["transcript"] == transcript
    assert envelope.attachments[0]["transcript_source"] == "local_whisper"
    assert envelope.attachments[0]["transcription_model"] == "local-whisper:ggml-base.bin"
    assert "dataUrl" not in envelope.attachments[0]
    assert "data:audio" not in json.dumps(envelope.attachments, ensure_ascii=False)
    assert envelope.params["model"] == route_model
    assert dispatched["audio_delivery"] == {
        "mode": "transcript",
        "target_capability": "text",
        "model": route_model,
    }


def test_audio_tagged_model_keeps_audio_passthrough(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH",
        str(tmp_path / "ambient-audit.jsonl"),
    )

    from domain.ambient.router import AmbientTriggerRouter

    route_model = "provider/audio-model"
    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({"routing": {"mode": "selected_chat", "model": route_model}})

    with (
        patch(
            "domain.ambient.router.model_input_capability",
            return_value={
                "kind": "audio",
                "supports_audio_input": True,
                "supports_image_input": False,
                "configured": True,
                "provider_id": "provider",
            },
        ),
        patch(
            "domain.ambient.router.transcribe_ambient_audio",
            return_value={
                "status": "unavailable",
                "code": "no_transcription_model",
                "reason": "No configured transcription model is available.",
                "text": "",
            },
        ) as transcribe,
        patch(
            "domain.ambient.router.submit_input",
            return_value={"status": "ok", "assistant_text": ""},
        ) as submit,
    ):
        dispatched = router.submit_event(_pinch_audio_payload(transcript=None))

    assert dispatched["status"] == "ok"
    transcribe.assert_called_once()
    envelope = submit.call_args.args[0]
    assert envelope.params["model"] == route_model
    assert envelope.attachments[0]["dataUrl"].startswith("data:audio/webm")
    assert envelope.attachments[0]["transcription_status"] == "unavailable"
    assert dispatched["audio_delivery"]["mode"] == "audio_direct"
    assert dispatched["audio_delivery"]["target_capability"] == "audio"


def test_multimodal_vision_without_audio_requires_transcription(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH",
        str(tmp_path / "ambient-audit.jsonl"),
    )

    from domain.ambient.router import AmbientTriggerRouter

    route_model = "provider/vision-model"
    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({"routing": {"mode": "selected_chat", "model": route_model}})

    with (
        patch(
            "domain.ambient.router.model_input_capability",
            return_value={
                "kind": "multimodal_no_audio",
                "supports_audio_input": False,
                "supports_image_input": True,
                "configured": True,
                "provider_id": "provider",
            },
        ),
        patch(
            "domain.ambient.router.transcribe_ambient_audio",
            return_value={
                "status": "unavailable",
                "code": "local_whisper_not_configured",
                "reason": "ローカルWhisperが未設定です。",
                "text": "",
            },
        ) as transcribe,
        patch("domain.ambient.router.submit_input") as submit,
    ):
        result = router.submit_event(_pinch_audio_payload(transcript=None))

    assert result["status"] == "transcription_required"
    assert result["reason"] == "ambient.audio_transcription_unavailable"
    assert result["transcription"]["code"] == "local_whisper_not_configured"
    assert result["audio_delivery"]["mode"] == "transcription_required"
    assert result["audio_delivery"]["target_capability"] == "multimodal_no_audio"
    transcribe.assert_called_once()
    submit.assert_not_called()


def test_ambient_audio_release_without_transcript_requires_transcription_for_text_model(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    with (
        patch(
            "domain.ambient.router.transcribe_ambient_audio",
            return_value={
                "status": "unavailable",
                "code": "no_transcription_model",
                "reason": "No configured transcription model is available.",
                "text": "",
            },
        ) as transcribe,
        patch("domain.ambient.router.submit_input") as submit,
    ):
        result = router.submit_event(_pinch_audio_payload(transcript=None), {"conversation_id": "conv-text"})

    assert result["status"] == "transcription_required"
    assert result["reason"] == "ambient.audio_transcription_unavailable"
    assert result["transcription"]["code"] == "no_transcription_model"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "pinch.webm" not in serialized
    assert "ambient-pinch" not in serialized
    assert "data:audio" not in serialized
    assert "AAAA" not in serialized
    transcribe.assert_called_once()
    submit.assert_not_called()


def test_ambient_audio_release_without_local_whisper_blocks_text_model(monkeypatch, tmp_path):
    _clear_local_whisper_env(monkeypatch)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH",
        str(tmp_path / "ambient-audit.jsonl"),
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "conversations.json"))

    from domain.ambient import local_transcription
    from domain.ambient.router import AmbientTriggerRouter

    monkeypatch.setattr(local_transcription.shutil, "which", lambda _name: None)
    monkeypatch.setattr(local_transcription, "_default_model_candidates", lambda _env: [])
    monkeypatch.setattr(local_transcription, "_has_python_module", lambda _name: False)

    class NoTranscriptionClient:
        def __init__(self):
            self._providers = {}

        def transcribe(self, model, audio, params):
            raise AssertionError("cloud transcription should not be attempted")

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({
        "routing": {
            "mode": "always_new_chat",
            "group_id": "gesture",
            "model": "opencode-go/deepseek-v4-flash",
        }
    })

    with (
        patch("domain.ai_client.client.AIClient", return_value=NoTranscriptionClient()),
        patch("domain.ambient.router.submit_input") as submit,
    ):
        result = router.submit_event(_pinch_audio_payload(transcript=None))

    assert result["status"] == "transcription_required"
    assert result["reason"] == "ambient.audio_transcription_unavailable"
    assert result["transcription"]["code"] == "local_whisper_not_configured"
    assert "ローカルWhisper" in result["transcription"]["reason"]
    serialized = json.dumps(result, ensure_ascii=False)
    assert "pinch.webm" not in serialized
    assert "ambient-pinch" not in serialized
    assert "data:audio" not in serialized
    assert "AAAA" not in serialized
    submit.assert_not_called()


def test_local_whisper_command_uses_secure_temp_audio_and_deletes_it(monkeypatch, tmp_path):
    _clear_local_whisper_env(monkeypatch)

    from domain.ambient import local_transcription

    model_path = tmp_path / "ggml-base.bin"
    model_path.write_bytes(b"model")
    monkeypatch.setenv("WHISPER_CPP_BIN", "/mock/bin/whisper-cli")
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(model_path))
    monkeypatch.setenv("FFMPEG_BIN", "/mock/bin/ffmpeg")

    conversion_source_paths = []
    whisper_audio_paths = []

    def fake_run_subprocess(argv, *, timeout_seconds):
        del timeout_seconds
        if Path(argv[0]).name == "ffmpeg":
            source_path = Path(argv[argv.index("-i") + 1])
            converted_path = Path(argv[-1])
            assert source_path.exists()
            conversion_source_paths.append(source_path)
            converted_path.write_bytes(b"RIFF converted wav")
            return local_transcription.CommandResult(returncode=0, stdout="", stderr="")
        audio_path = Path(argv[argv.index("-f") + 1])
        output_prefix = Path(argv[argv.index("-of") + 1])
        assert argv[argv.index("-l") + 1] == "auto"
        assert argv[argv.index("--prompt") + 1] == "BlackHole is a product name."
        assert audio_path.exists()
        assert audio_path.suffix == ".wav"
        assert audio_path.read_bytes() == b"RIFF converted wav"
        whisper_audio_paths.append(audio_path)
        output_prefix.with_suffix(".txt").write_text(
            "ローカル文字起こし\n",
            encoding="utf-8",
        )
        return local_transcription.CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_transcription, "_run_subprocess", fake_run_subprocess)

    result = local_transcription.transcribe_local_audio(
        "data:audio/webm;base64,AAA=",
        mime_type="audio/webm",
        prompt="BlackHole is a product name.",
    )

    assert result["status"] == "ok"
    assert result["text"] == "ローカル文字起こし"
    assert result["source"] == "local_whisper"
    assert result["model"] == "local-whisper:ggml-base.bin"
    assert local_transcription.local_whisper_status()["status"] == "local_whisper_configured"
    assert conversion_source_paths
    assert whisper_audio_paths
    assert not conversion_source_paths[0].exists()
    assert not whisper_audio_paths[0].exists()


def test_local_whisper_command_placeholder_can_receive_prompt(monkeypatch, tmp_path):
    _clear_local_whisper_env(monkeypatch)

    from domain.ambient import local_transcription

    command_log = []
    model_path = tmp_path / "ggml-small.bin"
    model_path.write_bytes(b"model")
    monkeypatch.setenv(
        "RUMI_LOCAL_WHISPER_COMMAND",
        "/mock/bin/whisper-cli -m {model} -f {audio} --prompt {prompt} -otxt -of {output_prefix}",
    )
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(model_path))

    def fake_run_subprocess(argv, *, timeout_seconds):
        del timeout_seconds
        command_log.append(argv)
        assert argv[argv.index("--prompt") + 1] == "BlackHole is a product name."
        output_txt = Path(argv[argv.index("-of") + 1]).with_suffix(".txt")
        output_txt.write_text("BlackHoleのテストです。\n", encoding="utf-8")
        return local_transcription.CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_transcription, "_run_subprocess", fake_run_subprocess)

    result = local_transcription.transcribe_local_audio(
        "data:audio/wav;base64,AAA=",
        mime_type="audio/wav",
        prompt="BlackHole is a product name.",
    )

    assert result["status"] == "ok"
    assert result["text"] == "BlackHoleのテストです。"
    assert command_log


def test_local_whisper_binary_env_is_single_argv_even_with_windows_spaces(monkeypatch, tmp_path):
    _clear_local_whisper_env(monkeypatch)

    from domain.ambient import local_transcription

    command_path = r"C:\Program Files\Rumi AI\whisper-cli.exe"
    model_path = tmp_path / "ggml-base.bin"
    model_path.write_bytes(b"model")
    monkeypatch.setenv("WHISPER_CPP_BIN", command_path)
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(model_path))

    command_log = []

    def fake_run_subprocess(argv, *, timeout_seconds):
        del timeout_seconds
        command_log.append(argv)
        assert argv[0] == command_path
        assert argv[argv.index("-m") + 1] == str(model_path)
        output_prefix = Path(argv[argv.index("-of") + 1])
        output_prefix.with_suffix(".txt").write_text("windows path ok\n", encoding="utf-8")
        return local_transcription.CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_transcription, "_run_subprocess", fake_run_subprocess)

    result = local_transcription.transcribe_local_audio(
        "data:audio/wav;base64,AAA=",
        mime_type="audio/wav",
    )

    assert result["status"] == "ok"
    assert result["text"] == "windows path ok"
    assert command_log


def test_local_whisper_binary_env_is_single_argv_even_with_posix_spaces(monkeypatch, tmp_path):
    _clear_local_whisper_env(monkeypatch)

    from domain.ambient import local_transcription

    command_path = "/Applications/Rumi AI.app/Contents/Resources/bundled/whisper/bin/whisper-cli"
    model_path = tmp_path / "ggml-base.bin"
    model_path.write_bytes(b"model")
    monkeypatch.setenv("WHISPER_CPP_BIN", command_path)
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(model_path))

    command_log = []

    def fake_run_subprocess(argv, *, timeout_seconds):
        del timeout_seconds
        command_log.append(argv)
        assert argv[0] == command_path
        output_prefix = Path(argv[argv.index("-of") + 1])
        output_prefix.with_suffix(".txt").write_text("posix path ok\n", encoding="utf-8")
        return local_transcription.CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_transcription, "_run_subprocess", fake_run_subprocess)

    result = local_transcription.transcribe_local_audio(
        "data:audio/wav;base64,AAA=",
        mime_type="audio/wav",
    )

    assert result["status"] == "ok"
    assert result["text"] == "posix path ok"
    assert command_log


def test_local_whisper_faster_whisper_model_is_cached(monkeypatch):
    _clear_local_whisper_env(monkeypatch)

    from domain.ambient import local_transcription

    local_transcription._FASTER_WHISPER_MODEL_CACHE.clear()
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_ALLOW_DOWNLOAD", "1")
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_MODEL", "fake-faster-model")
    monkeypatch.setattr(local_transcription, "_configured_command", lambda _env: None)
    monkeypatch.setattr(
        local_transcription,
        "_has_python_module",
        lambda name: name == "faster_whisper",
    )

    load_count = 0
    transcribe_count = 0

    class FakeSegment:
        text = "キャッシュテスト"

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            nonlocal load_count
            load_count += 1
            assert model == "fake-faster-model"
            assert kwargs["device"] == "cpu"
            assert kwargs["compute_type"] == "int8"
            assert "local_files_only" not in kwargs

        def transcribe(self, audio_path, **kwargs):
            nonlocal transcribe_count
            transcribe_count += 1
            assert audio_path
            assert kwargs["language"] == "ja"
            return [FakeSegment()], object()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    for _ in range(2):
        result = local_transcription.transcribe_local_audio(
            "data:audio/wav;base64,AAA=",
            mime_type="audio/wav",
            language="ja",
        )
        assert result["status"] == "ok"
        assert result["text"] == "キャッシュテスト"

    assert load_count == 1
    assert transcribe_count == 2


def test_local_whisper_openai_whisper_model_is_cached(monkeypatch):
    _clear_local_whisper_env(monkeypatch)

    from domain.ambient import local_transcription

    local_transcription._OPENAI_WHISPER_MODEL_CACHE.clear()
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_ALLOW_DOWNLOAD", "1")
    monkeypatch.setenv("RUMI_LOCAL_WHISPER_MODEL", "fake-openai-model")
    monkeypatch.setattr(local_transcription, "_configured_command", lambda _env: None)
    monkeypatch.setattr(
        local_transcription,
        "_has_python_module",
        lambda name: name == "whisper",
    )

    load_count = 0
    transcribe_count = 0

    class FakeWhisperModel:
        def transcribe(self, audio_path, **kwargs):
            nonlocal transcribe_count
            transcribe_count += 1
            assert audio_path
            assert kwargs["initial_prompt"] == "Rumi"
            return {"text": "openai whisper cache"}

    def fake_load_model(model):
        nonlocal load_count
        load_count += 1
        assert model == "fake-openai-model"
        return FakeWhisperModel()

    fake_module = types.ModuleType("whisper")
    fake_module.load_model = fake_load_model
    monkeypatch.setitem(sys.modules, "whisper", fake_module)

    for _ in range(2):
        result = local_transcription.transcribe_local_audio(
            "data:audio/wav;base64,AAA=",
            mime_type="audio/wav",
            prompt="Rumi",
        )
        assert result["status"] == "ok"
        assert result["text"] == "openai whisper cache"

    assert load_count == 1
    assert transcribe_count == 2


def test_ambient_transcription_without_explicit_model_rejects_stub_only_provider(monkeypatch):
    from domain.ambient.transcription import TRANSCRIPTION_ENV_KEYS, transcribe_ambient_audio

    for env_key in TRANSCRIPTION_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)

    class StubOnlyClient:
        def __init__(self):
            self._providers = {"stub": object(), "rumi": object()}

        def transcribe(self, model, audio, params):
            raise AssertionError("implicit fallback transcription should not be attempted")

    attachment = _pinch_audio_payload(transcript=None)["attachments"][0]
    with patch("domain.ai_client.client.AIClient", return_value=StubOnlyClient()):
        result = transcribe_ambient_audio([attachment])

    assert result["status"] == "unavailable"
    assert result["code"] == "no_transcription_model"
    assert result["text"] == ""
    assert "pinch.webm" not in result["text"]
    assert "data:audio" not in result["text"]
    assert "AAAA" not in result["text"]


def test_ambient_transcription_allows_explicit_stub_model_from_params_or_env(monkeypatch):
    from domain.ambient.transcription import TRANSCRIPTION_ENV_KEYS, transcribe_ambient_audio

    class FakeTranscriptionClient:
        def __init__(self, transcript):
            self._providers = {"stub": object()}
            self.calls = []
            self.transcript = transcript

        def transcribe(self, model, audio, params):
            self.calls.append({"model": model, "audio": audio, "params": params})
            return {"text": self.transcript}

    model_ref = "stub/ambient-transcribe"
    for source in ("params", "env"):
        for env_key in TRANSCRIPTION_ENV_KEYS:
            monkeypatch.delenv(env_key, raising=False)

        params = {}
        if source == "params":
            params = {"transcription_model": model_ref}
        else:
            monkeypatch.setenv("RUMI_AMBIENT_TRANSCRIPTION_MODEL", model_ref)

        transcript = f"deterministic local transcript from {source}"
        client = FakeTranscriptionClient(transcript)
        attachment = _pinch_audio_payload(transcript=None)["attachments"][0]
        with patch("domain.ai_client.client.AIClient", return_value=client):
            result = transcribe_ambient_audio([attachment], params=params)

        assert result["status"] == "ok"
        assert result["text"] == transcript
        assert result["source"] == "stub"
        assert result["model"] == model_ref
        assert client.calls == [
            {
                "model": model_ref,
                "audio": "data:audio/webm;base64,AAAA",
                "params": {"format": "webm"},
            }
        ]


def test_model_input_capability_uses_builtin_manifest_without_full_catalog(monkeypatch):
    from domain.ai_client import model_search
    from domain.input.audio_runtime import (
        _built_in_model_metadata,
        _cached_model_input_capability,
        _static_provider_model_metadata,
        model_input_capability,
    )

    def fail_catalog_lookup(*_args, **_kwargs):
        raise AssertionError("ambient capability lookup must not rebuild the full model catalog")

    monkeypatch.setattr(model_search, "get_model_capabilities", fail_catalog_lookup)
    _cached_model_input_capability.cache_clear()
    _built_in_model_metadata.cache_clear()
    _static_provider_model_metadata.cache_clear()

    text_only = model_input_capability("opencode-go/deepseek-v4-flash")
    vision_only = model_input_capability("opencode-go/qwen3.7-plus")
    unknown = model_input_capability("custom-provider/unknown-model")

    assert text_only["kind"] == "text"
    assert text_only["supports_audio_input"] is False
    assert vision_only["kind"] == "multimodal_no_audio"
    assert vision_only["supports_image_input"] is True
    assert vision_only["supports_audio_input"] is False
    assert unknown["kind"] == "text"
    assert unknown["supports_audio_input"] is False


def test_ai_send_approval_mode_holds_ambient_input_until_server_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({"routing": {"ai_send_approval_required": True}})

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": "hi"}) as submit:
        pending = router.submit_event(
            {
                "source": "hook",
                "trigger": "external_hook",
                "mode": "preset_text",
                "action_id": "chat.message",
                "input_text": "hello",
                "approved": True,
            }
        )

        assert pending["status"] == "approval_required"
        assert pending["reason"] == "ambient.ai_send_approval_required"
        assert pending["client_approved_flag_ignored"] is True
        submit.assert_not_called()

        request_id = pending["approval_request_id"]
        status = router.status()
        assert status["routing"]["ai_send_approval_required"] is True
        assert status["pending_approval"]["request_id"] == request_id
        assert status["pending_approval"]["input_preview"] == "hello"

        approved = router.approve_pending(request_id)

    assert approved["status"] == "ok"
    envelope = submit.call_args.args[0]
    assert envelope.input == "hello"
    assert envelope.delivery["action_id"] == "chat.message"
    assert envelope.metadata["ambient"]["approval_request_id"] == request_id
    assert router.status()["pending_approval"] is None


def test_ai_send_approval_pending_summary_does_not_expose_audio_data(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient import router as router_module
    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({"routing": {"ai_send_approval_required": True}})

    with patch("domain.ambient.router.submit_input") as submit:
        pending = router.submit_event(_pinch_audio_payload())

    assert pending["status"] == "approval_required"
    submit.assert_not_called()
    summary = router.status()["pending_approval"]
    assert summary["has_audio"] is True
    assert summary["attachment_count"] == 1
    assert "data:audio" not in json.dumps(summary)
    assert "AAAA" not in json.dumps(summary)
    stored = router_module._PENDING_AI_SEND_APPROVALS[pending["approval_request_id"]]
    assert "data:audio" not in repr(stored)
    assert "AAAA" not in repr(stored)
    blob_ids = stored["audio_blob_ids"]
    assert len(blob_ids) == 1
    assert blob_ids[0] in router_module._PENDING_AI_SEND_AUDIO_BLOBS

    router.deny_pending(pending["approval_request_id"])
    assert blob_ids[0] not in router_module._PENDING_AI_SEND_AUDIO_BLOBS


def test_ai_send_approval_pending_audio_respects_capacity_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient import router as router_module
    from domain.ambient.router import AmbientTriggerRouter

    with router_module._AMBIENT_PENDING_LOCK:
        router_module._PENDING_AI_SEND_APPROVALS.clear()
        router_module._PENDING_AI_SEND_AUDIO_BLOBS.clear()
    monkeypatch.setattr(router_module, "MAX_PENDING_AUDIO_BLOB_BYTES", 4)

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(MIC_PERMISSION, os_status="granted")
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")
    router.configure({"routing": {"ai_send_approval_required": True}})

    payload = _pinch_audio_payload()
    payload["attachments"][0]["dataUrl"] = "data:audio/webm;base64," + ("A" * 64)
    pending = router.submit_event(payload)

    stored = router_module._PENDING_AI_SEND_APPROVALS[pending["approval_request_id"]]
    assert stored["audio_blob_ids"] == []
    assert router_module._PENDING_AI_SEND_AUDIO_BLOBS == {}
    metadata = stored["attachments"][0]["metadata"]
    assert metadata["ambient_audio_blob_omitted"] == "blob_too_large"
    assert "ambient_audio_blob_id" not in metadata


def test_ambient_preset_hello_uses_submit_input_path_without_approval_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.grant_permission("ambient.trigger.dispatch")

    with patch("domain.ambient.router.submit_input", return_value={"status": "ok", "assistant_text": "hi"}) as submit:
        result = router.submit_event(
            {
                "source": "hook",
                "trigger": "external_hook",
                "mode": "preset_text",
                "action_id": "chat.message",
                "input_text": "hello",
            }
        )

    assert result["status"] == "ok"
    envelope = submit.call_args.args[0]
    assert envelope.input == "hello"
    assert envelope.source["provider"] == "ambient"
    assert envelope.delivery["action_id"] == "chat.message"


def test_approval_gesture_is_audited_without_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    router.start_monitor()
    router.grant_permission(CAMERA_PERMISSION, os_status="granted")
    router.grant_permission("ambient.trigger.dispatch")

    with patch("domain.ambient.router.submit_input") as submit:
        result = router.submit_event(
            {
                "source": "camera",
                "trigger": "approval_gesture",
                "mode": "swipe_reject",
                "decision": "reject",
                "confidence": 0.91,
                "metadata": {"approval_kind": "runtime"},
            }
        )

    assert result["status"] == "approval_intent"
    assert result["decision"] == "reject"
    submit.assert_not_called()


def test_os_permission_check_updates_status_without_granting_rumi_permissions(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    state = router.check_os_permissions({MIC_PERMISSION: "denied", CAMERA_PERMISSION: "granted"})

    assert state["permissions"]["os"][MIC_PERMISSION]["status"] == "denied"
    assert state["permissions"]["os"][CAMERA_PERMISSION]["status"] == "granted"
    assert state["permissions"]["rumi"][MIC_PERMISSION]["granted"] is False
    audit_records = [
        json.loads(line)
        for line in (tmp_path / "ambient-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert audit_records[-1]["trigger"] == "permission_check"


def test_ambient_permission_function_requires_signed_viewer_operator(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_PANEL_BOOTSTRAP_SECRET", "test-ambient-secret")

    from blocks.ambient import permissions
    from core_runtime.authority.ui_operator import sign_ui_operator
    from core_runtime.host_contract import bind_host_contract

    unsigned = permissions.run({"action": "grant", "permission_id": MIC_PERMISSION})

    assert unsigned["status"] == "error"
    assert unsigned["error"]["code"] == "AMBIENT_PERMISSION_UI_OPERATOR_REQUIRED"

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "profile:test",
            "values": {"panel_bootstrap_secret": "test-ambient-secret"},
        }
    ):
        signed = permissions.run(
            {
                "action": "grant",
                "permission_id": MIC_PERMISSION,
                "ui_operator": sign_ui_operator(
                    "rumi_ambient_trigger_pack", nonce="ambient-grant"
                ),
            }
        )

    assert signed["status"] == "ok"
    assert signed["data"]["permissions"]["rumi"][MIC_PERMISSION]["granted"] is True
    assert signed["data"]["authority"]["request_id"] == "rumi_ambient_trigger_pack"
    assert signed["data"]["authority"]["ui_operator"] is True


def test_ambient_permission_function_rejects_wrong_operator_request(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_PANEL_BOOTSTRAP_SECRET", "test-ambient-secret")

    from blocks.ambient import permissions
    from core_runtime.authority.ui_operator import sign_ui_operator
    from core_runtime.host_contract import bind_host_contract

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "profile:test",
            "values": {"panel_bootstrap_secret": "test-ambient-secret"},
        }
    ):
        result = permissions.run(
            {
                "action": "grant",
                "permission_id": MIC_PERMISSION,
                "ui_operator": sign_ui_operator(
                    "different-request", nonce="ambient-wrong"
                ),
            }
        )

    assert result["status"] == "error"
    assert result["error"]["code"] == "AMBIENT_PERMISSION_UI_OPERATOR_REQUIRED"
    assert "request mismatch" in result["error"]["message"]


def test_ambient_permission_check_function_updates_only_os_state_without_operator(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_PANEL_BOOTSTRAP_SECRET", "test-ambient-secret")

    from blocks.ambient import permissions

    checked = permissions.run(
        {
            "action": "check_os",
            "statuses": {MIC_PERMISSION: "granted", CAMERA_PERMISSION: "denied"},
        }
    )

    assert checked["status"] == "ok"
    assert checked["data"]["permissions"]["os"][MIC_PERMISSION]["status"] == "granted"
    assert checked["data"]["permissions"]["os"][CAMERA_PERMISSION]["status"] == "denied"
    assert checked["data"]["permissions"]["rumi"][MIC_PERMISSION]["granted"] is False
    assert checked["data"]["permissions"]["rumi"][CAMERA_PERMISSION]["granted"] is False


def test_ambient_permission_revoke_function_requires_signed_viewer_operator(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(tmp_path / "ambient-state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    monkeypatch.setenv("RUMI_PANEL_BOOTSTRAP_SECRET", "test-ambient-secret")

    from blocks.ambient import permissions
    from core_runtime.authority.ui_operator import sign_ui_operator
    from core_runtime.host_contract import bind_host_contract

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "profile:test",
            "values": {"panel_bootstrap_secret": "test-ambient-secret"},
        }
    ):
        grant_operator = sign_ui_operator(
            "rumi_ambient_trigger_pack", nonce="ambient-grant"
        )
        assert permissions.run(
            {
                "action": "grant",
                "permission_id": MIC_PERMISSION,
                "ui_operator": grant_operator,
            }
        )["status"] == "ok"

        unsigned = permissions.run(
            {"action": "revoke", "permission_id": MIC_PERMISSION}
        )

        assert unsigned["status"] == "error"
        assert unsigned["error"]["code"] == "AMBIENT_PERMISSION_UI_OPERATOR_REQUIRED"

        revoked = permissions.run(
            {
                "action": "revoke",
                "permission_id": MIC_PERMISSION,
                "ui_operator": sign_ui_operator(
                    "rumi_ambient_trigger_pack", nonce="ambient-revoke"
                ),
            }
        )

    assert revoked["status"] == "ok"
    assert revoked["data"]["permissions"]["rumi"][MIC_PERMISSION]["granted"] is False


def test_ambient_store_migrates_legacy_gesture_release_threshold(monkeypatch, tmp_path):
    state_path = tmp_path / "ambient-state.json"
    state_path.write_text(
        json.dumps({
            "services": {
                "gesture_wake_monitor": {
                    "detector": "thumb_tip_index_tip_distance_v1",
                    "release_threshold": 0.38,
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(state_path))

    from domain.ambient.store import AmbientStore

    state = AmbientStore().read()

    assert state["services"]["gesture_wake_monitor"]["release_threshold"] == 0.46
    assert state["services"]["gesture_wake_monitor"]["detector"] == "ok_mark_thumb_index_open_fingers_v1"


def test_ambient_routes_and_functions_are_registered():
    from domain.function_runtime.registry import block_module_for, default_args_for, get_spec
    from transport.registry import canonical_http_route_specs, load_legacy_http_route_allowlist

    routes = canonical_http_route_specs()
    legacy_routes = load_legacy_http_route_allowlist()
    assert not any(route.pattern.startswith("/api/ambient/") for route in routes)
    assert not any("/api/ambient/" in key[1] for key in legacy_routes)
    assert ("GET", "/host-permissions") in {
        (route.method, route.pattern) for route in routes
    }

    function_ids = {
        "ambient_status",
        "ambient_monitor_start",
        "ambient_monitor_stop",
        "ambient_configure",
        "ambient_event_submit",
        "ambient_permission_grant",
        "ambient_permission_revoke",
        "ambient_permission_check",
    }
    assert all(get_spec(function_id) is not None for function_id in function_ids)
    assert block_module_for("ambient_event_submit") == "blocks.ambient.event_submit"
    assert block_module_for("ambient_configure") == "blocks.ambient.config"
    assert default_args_for("ambient_monitor_stop") == {"action": "stop"}
    assert default_args_for("ambient_permission_check") == {"action": "check_os"}
    assert get_spec("ambient_monitor_start").requires == (
        MIC_PERMISSION,
        CAMERA_PERMISSION,
        "ambient.trigger.dispatch",
    )


def test_composer_transcription_route_is_transient_and_does_not_dispatch(monkeypatch):
    from blocks.ambient import transcription

    captured = {}

    def fake_transcribe(attachments, **kwargs):
        captured["attachments"] = attachments
        captured["kwargs"] = kwargs
        return {
            "status": "ok",
            "text": "こんにちは",
            "source": "local_whisper",
            "model": "local-whisper",
        }

    monkeypatch.setattr(transcription, "transcribe_ambient_audio", fake_transcribe)
    result = transcription.run(
        {
            "audio_data_url": "data:audio/webm;base64,AAAA",
            "audio_mime_type": "audio/webm",
            "audio_size": 4,
            "audio_name": "voice.webm",
            "model": "opencode-zen/mimo-v2.5-free",
            "params": {"language": "ja"},
            "metadata": {"target_supports_audio": False},
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["transcript"] == "こんにちは"
    assert result["data"]["transcription"]["source"] == "local_whisper"
    assert captured["attachments"][0]["do_not_persist"] is True
    assert captured["kwargs"]["target_model_ref"] == "opencode-zen/mimo-v2.5-free"
    assert captured["kwargs"]["target_supports_audio"] is False


def test_composer_transcription_route_rejects_oversize_audio_before_provider_call(monkeypatch):
    from blocks.ambient import transcription

    called = False

    def fake_transcribe(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(transcription, "transcribe_ambient_audio", fake_transcribe)
    monkeypatch.setattr(transcription, "MAX_AUDIO_BYTES", 2)
    result = transcription.run(
        {
            "audio_data_url": "data:audio/webm;base64,AAAA",
            "audio_size": 0,
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "AUDIO_PAYLOAD_TOO_LARGE"
    assert called is False


def test_composer_transcription_uses_decoded_bytes_not_caller_declared_size(monkeypatch):
    from blocks.ambient import transcription

    called = False

    def fake_transcribe(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(transcription, "MAX_AUDIO_BYTES", 2)
    monkeypatch.setattr(transcription, "transcribe_ambient_audio", fake_transcribe)
    result = transcription.run(
        {
            # `AAAA` decodes to three bytes, while the caller claims none.
            "audio_data_url": "data:audio/webm;base64,AAAA",
            "audio_size": 0,
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "AUDIO_PAYLOAD_TOO_LARGE"
    assert called is False


def test_composer_transcription_rejects_nested_or_duplicate_media_before_provider_call(monkeypatch):
    from blocks.ambient import transcription

    called = False

    def fake_transcribe(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(transcription, "transcribe_ambient_audio", fake_transcribe)
    payload = {
        "audio_data_url": "data:audio/webm;base64,AAAA",
        "audio_mime_type": "audio/webm",
        "attachments": [
            {
                "type": "audio/webm",
                "dataUrl": "data:audio/webm;base64,AAAA",
            }
        ],
    }
    result = transcription.run(payload, {})

    assert result["status"] == "error"
    assert result["error"]["code"] == "AUDIO_PAYLOAD_INVALID"
    assert called is False

    result = transcription.run(
        {
            "audio_data_url": "data:audio/webm;base64,AAAA",
            "audio": "data:audio/webm;base64,AAAA",
        },
        {},
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "AUDIO_PAYLOAD_INVALID"
    assert called is False


def test_composer_transcription_rejects_invalid_or_mismatched_audio_mime(monkeypatch):
    from blocks.ambient import transcription

    monkeypatch.setattr(
        transcription,
        "transcribe_ambient_audio",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not transcribe")),
    )
    malformed = transcription.run(
        {"audio_data_url": "data:audio/webm,not-base64"},
        {},
    )
    assert malformed["status"] == "error"
    assert malformed["error"]["code"] == "AUDIO_PAYLOAD_INVALID"

    mismatched = transcription.run(
        {
            "audio_data_url": "data:audio/webm;base64,AAAA",
            "audio_mime_type": "audio/mpeg",
        },
        {},
    )
    assert mismatched["status"] == "error"
    assert mismatched["error"]["code"] == "AUDIO_PAYLOAD_INVALID"


def test_ambient_events_viewer_token_satisfies_local_ui_context():
    from transport import http
    from core_runtime.host_contract import bind_host_contract

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "profile:test",
            "values": {"desktop_api_token": "viewer-local-token"},
        }
    ):
        assert http._local_ui_approval_route_authorized(
            "POST",
            "/api/ambient/events",
            {"Authorization": "Bearer viewer-local-token"},
        )
        assert not http._local_ui_approval_route_authorized(
            "POST",
            "/api/ambient/events",
            {},
        )
        assert http._requires_sensitive_http_auth(
            "POST",
            "/api/ambient/transcriptions",
        ) is False


def test_ambient_monitor_start_function_returns_browser_owned_contract(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    main_path = DEFAULTSPACK_ROOT / "functions" / "ambient_monitor_start" / "main.py"
    spec = importlib.util.spec_from_file_location("ambient_monitor_start_main", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.run(
        {
            "owner_pack": "rumi_ambient_trigger_pack",
            "function_id": "ambient_monitor_start",
            "conversation_id": "conversation-ambient",
        },
        {"max_duration_ms": 30_000, "sample_rate": 16_000, "channels": 1},
    )
    assert result["success"] is True
    assert result["type"] == "ambient_browser_monitor_contract"
    assert result["status"] == "browser_owned_monitor"
    assert result["capture_owner"] == "defaultspack_webapp"
    assert result["contract"]["camera"] == "browser.getUserMedia"
    assert result["contract"]["microphone"] == "browser.getUserMedia"
    assert result["host_stream"]["requested"] is False
    assert result["host_stream"]["available"] is False
    assert result["capture_options"]["privacy_mode"] == "audio_embedding_or_ephemeral_recording"
    assert result["consumer"] == {
        "pack_id": "rumi_ambient_trigger_pack",
        "function_id": "ambient_audio_classifier",
    }


def test_rumi_ambient_trigger_pack_metadata_exposes_install_prompt_permissions_and_surfaces():
    pack_json = ROOT / "ecosystem" / "setup_pack" / "rumi_ambient_trigger_pack" / "pack.json"
    pack = json.loads(pack_json.read_text(encoding="utf-8"))
    assert pack["supports_all_ok"] is False
    assert pack["required_permissions"] == [
        MIC_PERMISSION,
        CAMERA_PERMISSION,
        "ambient.trigger.dispatch",
    ]
    assert "マイク/カメラ" in pack["install_prompt"]["title"]
    assert pack["install_surfaces"] == ["small_window", "defaultspack_input"]
    assert "LINE" not in pack["install_prompt"]["surface_question"]
    assert "external_input" not in pack["overlap_policy"]

    extension_json = ROOT / "ecosystem" / "rumi_ambient_trigger_pack" / "frontend_extensions" / "ambient_trigger.ui.json"
    extension = json.loads(extension_json.read_text(encoding="utf-8"))
    surface_ids = {surface["id"] for surface in extension["surfaces"]}
    assert surface_ids == {"ambient_mini_window", "defaultspack_input"}
    assert "LINE" not in extension["install_prompt"]["surface_question"]
    assert extension["privacy"]["store_audio"] is False
    assert extension["privacy"]["store_images"] is False
    assert extension["privacy"]["gesture_choice"]["choices"] == [2, 3, 4]
    assert extension["privacy"]["gesture_choice"]["requires_audio"] is False
    assert extension["privacy"]["gesture_choice"]["profile_mutation"] is False
    assert extension["privacy"]["approval_gesture"]["requires_thumb_index_contact"] is False


def test_ambient_status_hides_legacy_external_hooks(monkeypatch, tmp_path):
    store_path = tmp_path / "ambient-state.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH", str(store_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH", str(tmp_path / "ambient-audit.jsonl"))
    store_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "defaultspack_input": {"enabled": True, "profile": "defaults.console.input"},
                    "line": {"enabled": True, "profile": "legacy.line"},
                    "discord": {"enabled": True, "profile": "legacy.discord"},
                    "web": {"enabled": True, "profile": "legacy.web"},
                }
            }
        ),
        encoding="utf-8",
    )

    from domain.ambient.router import AmbientTriggerRouter

    router = AmbientTriggerRouter()
    status = router.status()

    assert status["hooks"] == {
        "defaultspack_input": {"enabled": True, "profile": "defaults.console.input"},
    }

    router.start_monitor()
    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert persisted["hooks"] == {
        "defaultspack_input": {"enabled": True, "profile": "defaults.console.input"},
    }


def _contains_any_key(value, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(key in keys or _contains_any_key(item, keys) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_any_key(item, keys) for item in value)
    return False


def _clear_local_whisper_env(monkeypatch) -> None:
    for key in (
        "RUMI_LOCAL_WHISPER_COMMAND",
        "RUMI_WHISPER_CPP_BIN",
        "WHISPER_CPP_BIN",
        "RUMI_LOCAL_WHISPER_MODEL",
        "WHISPER_CPP_MODEL",
        "RUMI_WHISPER_MODEL_PATH",
        "RUMI_LOCAL_WHISPER_DIR",
        "RUMI_LOCAL_WHISPER_ALLOW_DOWNLOAD",
        "RUMI_WHISPER_ALLOW_DOWNLOAD",
        "RUMI_LOCAL_WHISPER_TIMEOUT_SECONDS",
        "RUMI_FFMPEG_BIN",
        "FFMPEG_BIN",
    ):
        monkeypatch.delenv(key, raising=False)


def _pinch_audio_payload(transcript: str | None = "今日の予定を確認して") -> dict:
    attachment = {
        "name": "pinch.webm",
        "type": "audio/webm",
        "size": 1234,
        "dataUrl": "data:audio/webm;base64,AAAA",
        "ephemeral": True,
        "do_not_persist": True,
    }
    if transcript is not None:
        attachment["transcript"] = transcript
        attachment["transcript_source"] = "web_speech_api"
    return {
        "source": "camera",
        "trigger": "pinch",
        "confidence": 0.94,
        "duration_ms": 900,
        "mode": "dispatch_audio",
        "attachments": [attachment],
        "metadata": {"hand": "Right", "normalized_distance": 0.42},
    }
