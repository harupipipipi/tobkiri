from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import replace
from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.chat.store import ChatStore
from domain.input.audio_runtime import model_input_capability
from domain.input.envelope import RumiInputEnvelope
from domain.input.submit import submit_input

from .audit import AmbientAuditLog, sanitize_for_audit
from .audio_classifier import cosine_similarity, embedding_from_payload, normalize
from .event import AmbientTriggerEvent
from .materialization import (
    AMBIENT_FINGER_RECORDING_AI_INPUT_ID,
    AMBIENT_FINGER_RECORDING_CONTEXT_POLICY_ID,
    audio_passthrough_input_text,
    audio_transcription_summary,
    materialize_ambient_event_attachments,
    transcription_result_summary,
    with_ambient_template_tool_policy,
)
from .permission_check import missing_rumi_permissions
from .store import AmbientStore
from .transcription import (
    RAW_AUDIO_ATTACHMENT_KEYS,
    attachment_transcript,
    attachment_transcript_model,
    attachment_transcript_source,
    audio_transcript_text,
    is_audio_attachment,
    mark_transcription_status,
    strip_audio_media,
    transcribe_ambient_audio,
)


ACTION_ALIASES = {
    "dispatch_input": "chat.message",
    "submit_input": "chat.message",
    "defaults.console.input": "chat.message",
    "defaultspack.console.input": "chat.message",
}

PINCH_RECORD_START_MODES = {"record_audio_start", "hold_to_record_start", "start_voice_capture"}
PINCH_RECORD_RELEASE_MODES = {"record_audio_release", "dispatch_audio", "submit_recording", "stop_voice_capture"}
APPROVAL_GESTURE_MODES = {"approval_approve", "approval_reject", "swipe_approve", "swipe_reject"}
ROUTING_MODES = {"selected_chat", "startup_new_chat", "always_new_chat"}
AI_SEND_APPROVAL_TTL_SECONDS = 5 * 60
MAX_SESSION_CONVERSATIONS = 64
MAX_PENDING_AI_SEND_APPROVALS = 64
MAX_PENDING_AUDIO_BLOBS = 64
MAX_PENDING_AUDIO_BLOB_BYTES = 10 * 1024 * 1024
MAX_PENDING_AUDIO_TOTAL_BYTES = 40 * 1024 * 1024
AMBIENT_EVENT_SETTLEMENT_TTL_SECONDS = 10 * 60
MAX_AMBIENT_EVENT_SETTLEMENTS = 128
logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {
    "chat.message",
    "run.instruction",
    "agent.delegate",
    "run.interrupt",
    "model.switch",
    "model.route",
}

_SESSION_CONVERSATION_IDS: dict[str, str] = {}
_PENDING_AI_SEND_APPROVALS: dict[str, dict[str, Any]] = {}
_PENDING_AI_SEND_AUDIO_BLOBS: dict[str, dict[str, Any]] = {}
_AMBIENT_EVENT_SETTLEMENTS: dict[str, dict[str, Any]] = {}
_AMBIENT_PENDING_LOCK = threading.RLock()


class AmbientTriggerRouter:
    def __init__(self, store: AmbientStore | None = None, audit: AmbientAuditLog | None = None) -> None:
        self.store = store or AmbientStore()
        self.audit = audit or AmbientAuditLog()

    def status(self) -> dict[str, Any]:
        state = self.store.read()
        routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        session_key = self._session_route_key(routing)
        if session_key:
            with _AMBIENT_PENDING_LOCK:
                session_conversation_id = _SESSION_CONVERSATION_IDS.get(session_key)
            if session_conversation_id:
                routing["session_conversation_id"] = session_conversation_id
        state["audit_tail"] = self.audit.tail(10)
        state["allowed_actions"] = sorted(ALLOWED_ACTIONS)
        state["input_aliases"] = dict(ACTION_ALIASES)
        state["pending_approval"] = self._latest_pending_summary()
        state["local_transcription"] = _local_transcription_status()
        return state

    def start_monitor(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options if isinstance(options, dict) else {}
        return self.store.start_monitor(
            voice_wake=bool(options.get("voice_wake", True)),
            gesture_pinch=bool(options.get("gesture_pinch", True)),
        )

    def stop_monitor(self) -> dict[str, Any]:
        return self.store.stop_monitor()

    def grant_permission(self, permission_id: str, *, os_status: str | None = None) -> dict[str, Any]:
        return self.store.grant_permission(permission_id, os_status=os_status)

    def revoke_permission(self, permission_id: str) -> dict[str, Any]:
        return self.store.revoke_permission(permission_id)

    def check_os_permissions(self, statuses: dict[str, Any]) -> dict[str, Any]:
        clean_statuses = {
            str(permission_id): str(status or "unknown")
            for permission_id, status in (statuses or {}).items()
            if str(permission_id or "").strip()
        }
        state = self.store.update_os_permissions(clean_statuses)
        self.audit.record(
            {
                "event_id": f"ambient_permission_check_{int(time.time() * 1000)}",
                "source": "os",
                "trigger": "permission_check",
                "status": "checked",
                "permissions": clean_statuses,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        return state

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else payload
        state = self.store.update_routing(routing if isinstance(routing, dict) else {})
        state["audit_tail"] = self.audit.tail(10)
        state["allowed_actions"] = sorted(ALLOWED_ACTIONS)
        state["input_aliases"] = dict(ACTION_ALIASES)
        state["pending_approval"] = self._latest_pending_summary()
        return state

    def approve_pending(
        self,
        request_id: str,
        context: dict[str, Any] | None = None,
        *,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> dict[str, Any]:
        del context
        pending = self._pop_pending(request_id)
        if pending is None:
            return {
                "status": "not_found",
                "reason": "ambient.ai_send_approval_not_found",
                "request_id": str(request_id or ""),
            }
        event = pending["event"]
        action_id = str(pending.get("action_id") or self._action_id(event))
        try:
            self._record(
                event,
                "approved",
                "ambient.ai_send_approval_accepted",
                action_id=action_id,
                approval_request_id=pending["request_id"],
            )
            return self._dispatch(
                event,
                action_id,
                copy.deepcopy(pending.get("context") if isinstance(pending.get("context"), dict) else {}),
                state=copy.deepcopy(pending.get("state") if isinstance(pending.get("state"), dict) else self.store.read()),
                attachments=_rehydrate_pending_audio_media(pending),
                require_approval=False,
                approval_request_id=pending["request_id"],
                settings_owner=(
                    settings_owner
                    if settings_owner is not None
                    else pending.get("settings_owner")
                ),
            )
        finally:
            _delete_pending_audio_blobs(pending)

    def deny_pending(self, request_id: str, reason: str = "") -> dict[str, Any]:
        pending = self._pop_pending(request_id)
        if pending is None:
            return {
                "status": "not_found",
                "reason": "ambient.ai_send_approval_not_found",
                "request_id": str(request_id or ""),
            }
        try:
            event = pending["event"]
            action_id = str(pending.get("action_id") or self._action_id(event))
            return self._record(
                event,
                "denied",
                "ambient.ai_send_approval_denied",
                action_id=action_id,
                approval_request_id=pending["request_id"],
                denied_reason=str(reason or "").strip(),
            )
        finally:
            _delete_pending_audio_blobs(pending)

    def submit_event(
        self,
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
        *,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> dict[str, Any]:
        event = AmbientTriggerEvent.from_payload(payload)
        if event.mode != "dispatch_audio":
            return self._submit_event_once(payload, context)

        fingerprint = _ambient_event_fingerprint(payload)
        with _AMBIENT_PENDING_LOCK:
            _trim_ambient_event_settlements_locked()
            existing = _AMBIENT_EVENT_SETTLEMENTS.get(event.event_id)
            if existing is not None:
                if existing.get("fingerprint") != fingerprint:
                    raise ValueError("ambient event_id was already used for different content")
                if existing.get("state") == "complete":
                    result = copy.deepcopy(existing.get("result") or {})
                    if isinstance(result, dict):
                        result["idempotent_replay"] = True
                    return result
                return {
                    "status": "processing",
                    "reason": "ambient.event_processing",
                    "event_id": event.event_id,
                }
            if len(_AMBIENT_EVENT_SETTLEMENTS) >= MAX_AMBIENT_EVENT_SETTLEMENTS:
                raise ValueError("ambient event settlement capacity exceeded")
            _AMBIENT_EVENT_SETTLEMENTS[event.event_id] = {
                "state": "processing",
                "fingerprint": fingerprint,
                "updated_at_epoch": time.time(),
            }

        try:
            result = self._submit_event_once(payload, context)
        except Exception:
            with _AMBIENT_PENDING_LOCK:
                current = _AMBIENT_EVENT_SETTLEMENTS.get(event.event_id)
                if current is not None and current.get("fingerprint") == fingerprint:
                    _AMBIENT_EVENT_SETTLEMENTS.pop(event.event_id, None)
            raise
        with _AMBIENT_PENDING_LOCK:
            _AMBIENT_EVENT_SETTLEMENTS[event.event_id] = {
                "state": "complete",
                "fingerprint": fingerprint,
                "result": copy.deepcopy(result),
                "updated_at_epoch": time.time(),
            }
            _trim_ambient_event_settlements_locked()
        return result

    def event_status(self, event_id: str) -> dict[str, Any]:
        event_id = str(event_id or "").strip()
        if not event_id:
            raise ValueError("event_id is required")
        with _AMBIENT_PENDING_LOCK:
            _trim_ambient_event_settlements_locked()
            settlement = copy.deepcopy(_AMBIENT_EVENT_SETTLEMENTS.get(event_id))
        if settlement is None:
            return {"status": "not_found", "event_id": event_id}
        if settlement.get("state") != "complete":
            return {"status": "processing", "event_id": event_id}
        return {
            "status": "complete",
            "event_id": event_id,
            "result": settlement.get("result") if isinstance(settlement.get("result"), dict) else {},
        }

    def _submit_event_once(self, payload: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        event = AmbientTriggerEvent.from_payload(payload)
        state = self.store.read()
        if not bool(state.get("ambient_monitor", {}).get("enabled")) and not self._event_can_run_without_monitor(event):
            return self._record(event, "ignored", "ambient_monitor.disabled")

        service = self._service_for_event(state, event)
        if service is not None and not bool(service.get("enabled", True)):
            return self._record(event, "ignored", f"{event.source}.{event.trigger}.disabled")

        missing = missing_rumi_permissions(
            state,
            event.source,
            needs_microphone=self._event_needs_microphone(event),
        )
        if missing:
            return self._record(event, "denied", "rumi_permission_missing", missing_permissions=missing)

        if event.trigger == "voice_wake":
            voice_result = self._handle_voice_wake(event, state)
            if voice_result is not None:
                return voice_result
        elif event.source == "microphone" and event.trigger == "transcription_test":
            return self._handle_transcription_test(event, state)
        elif event.source == "camera" and event.trigger == "approval_gesture":
            if event.confidence < 0.5:
                return self._record(event, "ignored", "approval_gesture.low_confidence")
            return self._record(
                event,
                "approval_intent",
                f"approval_gesture.{event.mode or 'unknown'}",
                action_id=self._action_id(event),
                decision=str(event.payload.get("decision") or event.metadata.get("decision") or ""),
            )
        elif event.source == "camera" and event.trigger == "gesture_choice":
            return self._record(
                event,
                "ignored",
                "gesture_choice.chat_dispatch_disabled",
                action_id=self._action_id(event),
                choice=event.payload.get("choice"),
            )
        elif event.source == "camera" and event.trigger == "pinch":
            if event.confidence < 0.5:
                return self._record(event, "ignored", "pinch.low_confidence")
            if event.mode in PINCH_RECORD_START_MODES:
                return self._record(
                    event,
                    "recording_started",
                    "pinch.record_audio_start",
                    action_id=self._action_id(event),
                    capture_started=True,
                )
            cooldown_reason = self._cooldown_reason(state)
            if cooldown_reason and event.mode not in PINCH_RECORD_RELEASE_MODES:
                return self._record(event, "ignored", cooldown_reason)

        action_id = self._action_id(event)
        attachments = self._attachments_for_event(event)
        if event.mode in {"open_input", "focus_composer", ""} and not event.input_text and not attachments:
            return self._record(
                event,
                "open_input",
                "trigger_matched",
                action_id=action_id,
                open_input=True,
                focus_composer=True,
            )

        return self._dispatch(
            event,
            action_id,
            context or {},
            state=state,
            attachments=attachments,
            settings_owner=settings_owner,
        )

    def _handle_transcription_test(self, event: AmbientTriggerEvent, state: dict[str, Any]) -> dict[str, Any]:
        attachments = self._attachments_for_event(event)
        audio_items = [item for item in attachments if isinstance(item, dict) and _is_audio_attachment(item)]
        if not audio_items:
            return self._record(
                event,
                "transcription_unavailable",
                "ambient.transcription_test.audio_missing",
                transcription={
                    "status": "unavailable",
                    "code": "audio_payload_missing",
                    "reason": "テスト用の録音音声がありません。",
                },
            )
        routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        params = dict(event.payload.get("params") if isinstance(event.payload.get("params"), dict) else {})
        result = transcribe_ambient_audio(
            attachments,
            payload=event.payload,
            params=params,
            routing=routing,
            target_model_ref=_clean_string(event.payload.get("model")) or _clean_string(routing.get("model")),
            target_supports_audio=False,
        )
        transcription = transcription_result_summary(result)
        status = "ok" if result.get("status") == "ok" and str(result.get("text") or "").strip() else "transcription_unavailable"
        record = self._record(
            event,
            status,
            "ambient.transcription_test.completed" if status == "ok" else "ambient.transcription_test.unavailable",
            transcription=transcription,
            privacy={
                "audio_saved": False,
                "image_saved": False,
                "frame_saved": False,
                "image_uploaded": False,
            },
        )
        return {
            **record,
            "transcript": str(result.get("text") or "").strip(),
        }

    def _handle_voice_wake(self, event: AmbientTriggerEvent, state: dict[str, Any]) -> dict[str, Any] | None:
        embedding = embedding_from_payload(event.payload)
        if not embedding:
            return self._record(event, "ignored", "voice_wake.audio_embedding_missing")
        enrollment = state.get("voice_enrollment") if isinstance(state.get("voice_enrollment"), dict) else None
        service = state.get("services", {}).get("voice_wake_monitor", {})
        if event.mode == "enroll_wake_voice" or not enrollment:
            if event.mode == "enroll_wake_voice" or bool(service.get("auto_enroll_first_sample", True)):
                threshold = float(service.get("threshold") or 0.88)
                self.store.save_voice_enrollment(normalize(embedding), threshold=threshold)
                return self._record(event, "enrolled", "voice_wake.first_sample_enrolled", classifier="local_audio_embedding_cosine_v1")
            return self._record(event, "ignored", "voice_wake.not_enrolled")
        enrolled_embedding = enrollment.get("embedding") if isinstance(enrollment.get("embedding"), list) else []
        similarity = cosine_similarity(embedding, enrolled_embedding)
        threshold = float(enrollment.get("threshold") or service.get("threshold") or 0.88)
        if similarity < threshold:
            return self._record(
                event,
                "ignored",
                "voice_wake.classifier_rejected",
                similarity=round(similarity, 4),
                threshold=threshold,
            )
        return None

    def _dispatch(
        self,
        event: AmbientTriggerEvent,
        action_id: str,
        context: dict[str, Any],
        *,
        state: dict[str, Any],
        attachments: list[dict[str, Any]] | None = None,
        require_approval: bool = True,
        approval_request_id: str | None = None,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> dict[str, Any]:
        if action_id not in ALLOWED_ACTIONS:
            return self._record(event, "denied", "ambient.action_not_allowed", action_id=action_id)
        attachments = attachments or []
        input_text = audio_transcript_text(attachments) or event.input_text or self._default_input_text(event, attachments)
        if require_approval and self._requires_ai_send_approval(state):
            return self._queue_ai_send_approval(
                event,
                action_id,
                context,
                state=state,
                attachments=attachments,
                input_text=input_text,
                settings_owner=settings_owner,
            )
        routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        route_model = str(routing.get("model") or "").strip()
        params = dict(event.payload.get("params") if isinstance(event.payload.get("params"), dict) else {})
        if _uses_finger_recording_template(event, attachments):
            params = with_ambient_template_tool_policy(params)
        target_conversation_id = self._target_conversation_id(event, context, state, params=params)
        model_ref = self._model_ref_for_dispatch(
            event,
            context,
            state,
            target_conversation_id,
            params=params,
            routing=routing,
        )
        if model_ref:
            # The resolved model is authoritative. In selected-chat mode it is
            # read from the canonical conversation so a stale browser payload or
            # routing cache cannot silently dispatch to a previous provider.
            params["model"] = model_ref
        elif route_model and not str(params.get("model") or params.get("profile_id") or "").strip():
            params["model"] = route_model
        audio_plan = self._prepare_audio_dispatch(
            event,
            attachments,
            input_text=input_text,
            params=params,
            routing=routing,
            model_ref=model_ref,
        )
        audio_delivery = {
            "mode": str(audio_plan.get("delivery_mode") or "text"),
            "target_capability": str(audio_plan.get("target_capability") or "text"),
            "model": model_ref,
        }
        if audio_plan.get("blocked"):
            blocked = self._record(
                event,
                "transcription_required",
                "ambient.audio_transcription_unavailable",
                action_id=action_id,
                approval_request_id=approval_request_id,
                model=model_ref,
                resolved_model=model_ref,
                input_delivery=audio_delivery,
                transcription=audio_plan.get("transcription"),
            )
            blocked["audio_delivery"] = audio_delivery
            return blocked
        input_text = str(audio_plan.get("input_text") or input_text)
        attachments = list(audio_plan.get("attachments") if isinstance(audio_plan.get("attachments"), list) else attachments)
        transcription_summary = audio_plan.get("transcription") if isinstance(audio_plan.get("transcription"), dict) else {}
        chat_model = model_ref if routing.get("mode") != "selected_chat" else ""
        envelope = RumiInputEnvelope(
            role="user",
            input=input_text,
            chat={
                "conversation_id": target_conversation_id,
                **({"model": chat_model} if chat_model and routing.get("mode") != "selected_chat" else {}),
            },
            source={
                "provider": "ambient",
                "kind": f"{event.source}_{event.trigger}",
                "event_id": event.event_id,
            },
            target={
                "conversation_id": target_conversation_id,
                "direct": True,
            },
            delivery={
                "action_id": action_id,
                "open_input": event.mode in {"open_input", "focus_composer"},
            },
            metadata={
                "ambient": sanitize_for_audit(
                    {
                        "event_id": event.event_id,
                        "source": event.source,
                        "trigger": event.trigger,
                        "mode": event.mode,
                        **(
                            {
                                "approval_request_id": approval_request_id,
                                "ai_send_approval": "approved",
                            }
                            if approval_request_id
                            else {}
                        ),
                        "resolved_model": model_ref,
                        "audio_delivery": audio_delivery,
                        **({"transcription": transcription_summary} if transcription_summary else {}),
                        **(
                            {
                                "template": {
                                    "ai_input_id": AMBIENT_FINGER_RECORDING_AI_INPUT_ID,
                                    "context_policy_id": AMBIENT_FINGER_RECORDING_CONTEXT_POLICY_ID,
                                }
                            }
                            if _uses_finger_recording_template(event, attachments)
                            else {}
                        ),
                        "metadata": event.metadata,
                    }
                )
            },
            params=params,
            attachments=attachments,
            tools=_tools_for_event(event, params),
        )
        result = submit_input(
            envelope,
            context,
            **({"settings_owner": settings_owner} if settings_owner is not None else {}),
        )
        status = str(result.get("status") if isinstance(result, dict) else "ok")
        logger.info(
            "ambient dispatch submitted",
            extra={
                "source": event.source,
                "trigger": event.trigger,
                "mode": event.mode,
                "action_id": action_id,
                "status": status,
                "conversation_id_present": bool(target_conversation_id),
                "model_ref": model_ref,
                "input_length": len(input_text),
                "attachment_count": len(attachments),
                "audio_attachment_count": sum(
                    1 for item in attachments if isinstance(item, dict) and _is_audio_attachment(item)
                ),
                "transcription_status": transcription_summary.get("status", ""),
                "transcription_code": transcription_summary.get("code", ""),
                "transcription_source": transcription_summary.get("source", ""),
            },
        )
        audit = self._record(
            event,
            status,
            "trigger_dispatched",
            action_id=action_id,
            approval_request_id=approval_request_id,
            resolved_model=model_ref,
            input_delivery=audio_delivery,
            dispatch_result=result,
        )
        audit["resolved_model"] = model_ref
        audit["audio_delivery"] = audio_delivery
        if isinstance(result, dict):
            audit["dispatch"] = result
            conversation_id = _clean_string(result.get("conversation_id"))
            if conversation_id:
                audit["conversation_id"] = conversation_id
        return audit

    def _requires_ai_send_approval(self, state: dict[str, Any]) -> bool:
        routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        return _coerce_bool(routing.get("ai_send_approval_required"), False)

    def _queue_ai_send_approval(
        self,
        event: AmbientTriggerEvent,
        action_id: str,
        context: dict[str, Any],
        *,
        state: dict[str, Any],
        attachments: list[dict[str, Any]],
        input_text: str,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        request_id = f"ambient_ai_send_{uuid.uuid4().hex}"
        expires_at = now + AI_SEND_APPROVAL_TTL_SECONDS
        store_path = self._store_path_key()
        pending_event = replace(event, payload=_strip_raw_audio_from_payload(event.payload))
        pending_attachments, audio_blob_ids = _detach_pending_audio_media(
            attachments,
            request_id=request_id,
            store_path=store_path,
            expires_at_epoch=expires_at,
        )
        pending = {
            "request_id": request_id,
            "store_path": store_path,
            "event": pending_event,
            "action_id": action_id,
            "context": copy.deepcopy(context if isinstance(context, dict) else {}),
            "state": copy.deepcopy(state if isinstance(state, dict) else {}),
            "settings_owner": settings_owner,
            "attachments": pending_attachments,
            "audio_blob_ids": audio_blob_ids,
            "input_text": str(input_text or ""),
            "created_at_epoch": now,
            "expires_at_epoch": expires_at,
            "created_at": _epoch_iso(now),
        }
        self._prune_pending()
        with _AMBIENT_PENDING_LOCK:
            _PENDING_AI_SEND_APPROVALS[request_id] = pending
            _trim_pending_approvals_locked()
        return self._record(
            event,
            "approval_required",
            "ambient.ai_send_approval_required",
            action_id=action_id,
            approval_request_id=request_id,
            client_approved_flag_ignored="approved" in event.payload,
            pending_approval=self._pending_summary(pending),
        )

    def _latest_pending_summary(self) -> dict[str, Any] | None:
        self._prune_pending()
        store_path = self._store_path_key()
        with _AMBIENT_PENDING_LOCK:
            pending = [
                item for item in _PENDING_AI_SEND_APPROVALS.values()
                if item.get("store_path") == store_path
            ]
        if not pending:
            return None
        pending.sort(key=lambda item: float(item.get("created_at_epoch") or 0), reverse=True)
        summary = self._pending_summary(pending[0])
        summary["pending_count"] = len(pending)
        return summary

    def _pop_pending(self, request_id: str) -> dict[str, Any] | None:
        self._prune_pending()
        request_id = str(request_id or "").strip()
        expired_pending: dict[str, Any] | None = None
        with _AMBIENT_PENDING_LOCK:
            pending = _PENDING_AI_SEND_APPROVALS.get(request_id)
            if pending is None or pending.get("store_path") != self._store_path_key():
                return None
            now = time.time()
            if float(pending.get("expires_at_epoch") or 0) <= now:
                expired_pending = _PENDING_AI_SEND_APPROVALS.pop(request_id, None)
                if expired_pending is not None:
                    _delete_pending_audio_blobs_locked(expired_pending)
            else:
                return _PENDING_AI_SEND_APPROVALS.pop(request_id, None)
        if expired_pending is not None:
            event = expired_pending.get("event")
            if isinstance(event, AmbientTriggerEvent):
                self._record(
                    event,
                    "ignored",
                    "ambient.ai_send_approval_expired",
                    action_id=str(expired_pending.get("action_id") or ""),
                    approval_request_id=request_id,
                )
        return None

    def _prune_pending(self) -> None:
        now = time.time()
        with _AMBIENT_PENDING_LOCK:
            expired = [
                request_id
                for request_id, pending in _PENDING_AI_SEND_APPROVALS.items()
                if float(pending.get("expires_at_epoch") or 0) <= now
            ]
            for request_id in expired:
                pending = _PENDING_AI_SEND_APPROVALS.pop(request_id, None)
                if pending is not None:
                    _delete_pending_audio_blobs_locked(pending)
            expired_blobs = [
                blob_id
                for blob_id, blob in _PENDING_AI_SEND_AUDIO_BLOBS.items()
                if float(blob.get("expires_at_epoch") or 0) <= now
            ]
            for blob_id in expired_blobs:
                _PENDING_AI_SEND_AUDIO_BLOBS.pop(blob_id, None)
            _trim_pending_audio_blobs_locked()

    def _pending_summary(self, pending: dict[str, Any]) -> dict[str, Any]:
        event = pending.get("event")
        attachments = pending.get("attachments") if isinstance(pending.get("attachments"), list) else []
        state = pending.get("state") if isinstance(pending.get("state"), dict) else {}
        context = pending.get("context") if isinstance(pending.get("context"), dict) else {}
        routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        input_text = str(pending.get("input_text") or "")
        has_audio_transcript = any(
            _is_audio_attachment(item) and attachment_transcript(item)
            for item in attachments
            if isinstance(item, dict)
        )
        return {
            "request_id": str(pending.get("request_id") or ""),
            "source": event.source if isinstance(event, AmbientTriggerEvent) else "",
            "trigger": event.trigger if isinstance(event, AmbientTriggerEvent) else "",
            "mode": event.mode if isinstance(event, AmbientTriggerEvent) else "",
            "action_id": str(pending.get("action_id") or ""),
            "input_preview": "[audio transcript redacted]" if has_audio_transcript else _preview(input_text),
            "has_text": bool(input_text.strip()),
            "attachment_count": len(attachments),
            "has_audio": any(_is_audio_attachment(item) for item in attachments if isinstance(item, dict)),
            **({"transcription": audio_transcription_summary(attachments)} if has_audio_transcript else {}),
            "conversation_id": (
                _clean_string(event.payload.get("conversation_id")) if isinstance(event, AmbientTriggerEvent) else ""
            )
            or _clean_string(context.get("conversation_id"))
            or _clean_string(routing.get("conversation_id")),
            "created_at": pending.get("created_at"),
            "expires_at": _epoch_iso(float(pending.get("expires_at_epoch") or time.time())),
        }

    def _store_path_key(self) -> str:
        return str(self.store.path)

    def _target_conversation_id(
        self,
        event: AmbientTriggerEvent,
        context: dict[str, Any],
        state: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> str | None:
        routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        mode = str(routing.get("mode") or "selected_chat").strip()
        if mode not in ROUTING_MODES:
            mode = "selected_chat"
        if mode == "selected_chat":
            return _clean_string(routing.get("conversation_id")) or _clean_string(event.payload.get("conversation_id")) or _clean_string(context.get("conversation_id"))
        if mode == "always_new_chat":
            return self._create_routed_conversation(routing, params=params, context=context, event=event)["id"]
        session_key = self._session_route_key(routing)
        if not session_key:
            return self._create_routed_conversation(routing, params=params, context=context, event=event)["id"]
        with _AMBIENT_PENDING_LOCK:
            conversation_id = _SESSION_CONVERSATION_IDS.get(session_key)
        if conversation_id:
            existing = ChatStore().get_conversation(conversation_id)
            if existing:
                return conversation_id
        created = self._create_routed_conversation(routing, params=params, context=context, event=event)
        with _AMBIENT_PENDING_LOCK:
            _SESSION_CONVERSATION_IDS[session_key] = created["id"]
            while len(_SESSION_CONVERSATION_IDS) > MAX_SESSION_CONVERSATIONS:
                _SESSION_CONVERSATION_IDS.pop(next(iter(_SESSION_CONVERSATION_IDS)), None)
        return created["id"]

    def _create_routed_conversation(
        self,
        routing: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        event: AmbientTriggerEvent | None = None,
    ) -> dict[str, Any]:
        group_enabled = _coerce_bool(routing.get("group_enabled"), True)
        group_id = (_clean_string(routing.get("group_id")) or "gesture") if group_enabled else ""
        group_title = (_clean_string(routing.get("group_title")) or "Gesture") if group_enabled else ""
        model = self._routed_new_conversation_model(routing, params=params, context=context, event=event)
        return ChatStore().create_conversation(
            model=model or None,
            tags=["ambient", "gesture"],
            conversation_kind="chat",
            group_id=group_id or None,
            metadata={
                "source": "ambient",
                "mode": "gesture",
                **({"group_id": group_id, "group_title": group_title} if group_enabled else {}),
            },
        )

    def _routed_new_conversation_model(
        self,
        routing: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        event: AmbientTriggerEvent | None = None,
    ) -> str:
        params = params if isinstance(params, dict) else {}
        context = context if isinstance(context, dict) else {}
        event_payload = event.payload if isinstance(event, AmbientTriggerEvent) and isinstance(event.payload, dict) else {}
        for value in (
            params.get("model"),
            params.get("profile_id"),
            event_payload.get("model"),
            event_payload.get("profile_id"),
            routing.get("model"),
            context.get("model"),
            context.get("profile_id"),
        ):
            model_ref = _clean_string(value)
            if model_ref:
                return model_ref
        source_conversation_id = _clean_string(event_payload.get("conversation_id")) or _clean_string(context.get("conversation_id"))
        if source_conversation_id:
            conversation = ChatStore().get_conversation(source_conversation_id) or {}
            model_ref = _clean_string(conversation.get("model"))
            if model_ref:
                return model_ref
        return ""

    def _session_route_key(self, routing: dict[str, Any]) -> str:
        if str(routing.get("mode") or "") != "startup_new_chat":
            return ""
        group_enabled = _coerce_bool(routing.get("group_enabled"), True)
        group_id = (_clean_string(routing.get("group_id")) or "gesture") if group_enabled else ""
        model = _clean_string(routing.get("model")) or ""
        return f"{self.store.path}:{group_enabled}:{group_id}:{model}"

    def _model_ref_for_dispatch(
        self,
        event: AmbientTriggerEvent,
        context: dict[str, Any],
        state: dict[str, Any],
        target_conversation_id: str | None,
        *,
        params: dict[str, Any],
        routing: dict[str, Any],
    ) -> str:
        if str(routing.get("mode") or "selected_chat") == "selected_chat" and target_conversation_id:
            conversation = ChatStore().get_conversation(target_conversation_id) or {}
            conversation_model = _clean_string(conversation.get("model"))
            if conversation_model:
                return conversation_model
        for value in (
            params.get("model"),
            params.get("profile_id"),
            event.payload.get("model"),
            event.payload.get("profile_id"),
            routing.get("model"),
            context.get("model") if isinstance(context, dict) else "",
            context.get("profile_id") if isinstance(context, dict) else "",
        ):
            model_ref = _clean_string(value)
            if model_ref:
                return model_ref
        if target_conversation_id:
            conversation = ChatStore().get_conversation(target_conversation_id) or {}
            return _clean_string(conversation.get("model"))
        return ""

    def _prepare_audio_dispatch(
        self,
        event: AmbientTriggerEvent,
        attachments: list[dict[str, Any]],
        *,
        input_text: str,
        params: dict[str, Any],
        routing: dict[str, Any],
        model_ref: str,
    ) -> dict[str, Any]:
        audio_indexes = [
            index
            for index, attachment in enumerate(attachments)
            if isinstance(attachment, dict) and _is_audio_attachment(attachment)
        ]
        target_capability = model_input_capability(model_ref)
        target_capability_kind = str(target_capability.get("kind") or "text")
        if not audio_indexes:
            return {
                "input_text": input_text,
                "attachments": attachments,
                "transcription": {},
                "delivery_mode": "text",
                "target_capability": target_capability_kind,
            }

        working = [dict(item) if isinstance(item, dict) else item for item in attachments]
        supports_audio = bool(target_capability.get("supports_audio_input"))
        transcript_text = audio_transcript_text([item for item in working if isinstance(item, dict)])
        transcription = audio_transcription_summary([item for item in working if isinstance(item, dict)])
        logger.info(
            "ambient audio dispatch preparing",
            extra={
                "event_id": event.event_id,
                "source": event.source,
                "trigger": event.trigger,
                "mode": event.mode,
                "model_ref": model_ref,
                "target_supports_audio": supports_audio,
                "audio_attachment_count": len(audio_indexes),
                "provided_transcript_length": len(transcript_text),
                "provided_transcription_status": transcription.get("status", ""),
            },
        )
        if transcript_text:
            transcription["status"] = transcription.get("status") or "provided"
        else:
            result = transcribe_ambient_audio(
                working,
                payload=event.payload,
                params=params,
                routing=routing,
                target_model_ref=model_ref,
                target_supports_audio=supports_audio,
            )
            transcription = transcription_result_summary(result)
            logger.info(
                "ambient audio transcription completed",
                extra={
                    "event_id": event.event_id,
                    "model_ref": model_ref,
                    "target_supports_audio": supports_audio,
                    "status": transcription.get("status", ""),
                    "code": transcription.get("code", ""),
                    "source": transcription.get("source", ""),
                    "transcript_length": transcription.get("transcript_length", 0),
                },
            )
            if result.get("status") == "ok" and str(result.get("text") or "").strip():
                by_index = {
                    int(item.get("index")): item
                    for item in (result.get("results") if isinstance(result.get("results"), list) else [])
                    if isinstance(item, dict) and item.get("index") is not None
                }
                missing_audio_indexes = [
                    index
                    for index in audio_indexes
                    if isinstance(working[index], dict) and not attachment_transcript(working[index])
                ]
                for index in missing_audio_indexes:
                    item = by_index.get(index)
                    text = str((item or {}).get("text") or result.get("text") or "").strip()
                    if not text:
                        continue
                    attachment = dict(working[index])
                    attachment["transcript"] = text
                    attachment = mark_transcription_status(
                        attachment,
                        status="ok",
                        source=str((item or {}).get("source") or result.get("source") or "transcription"),
                        model=str((item or {}).get("model") or result.get("model") or ""),
                    )
                    working[index] = attachment
                transcript_text = audio_transcript_text([item for item in working if isinstance(item, dict)])
            elif not supports_audio:
                logger.warning(
                    "ambient audio dispatch blocked waiting for transcription",
                    extra={
                        "event_id": event.event_id,
                        "model_ref": model_ref,
                        "transcription_status": transcription.get("status", ""),
                        "transcription_code": transcription.get("code", ""),
                    },
                )
                return {
                    "blocked": True,
                    "input_text": input_text,
                    "attachments": [strip_audio_media(item) if isinstance(item, dict) else item for item in working],
                    "transcription": transcription,
                    "delivery_mode": "transcription_required",
                    "target_capability": target_capability_kind,
                }

        if supports_audio:
            if transcript_text:
                working = [
                    mark_transcription_status(
                        item,
                        status=str(transcription.get("status") or "ok"),
                        source=str(transcription.get("source") or attachment_transcript_source(item)),
                        model=str(transcription.get("model") or attachment_transcript_model(item)),
                        include_audio_with_transcript=True,
                    )
                    if isinstance(item, dict) and _is_audio_attachment(item)
                    else item
                    for item in working
                ]
                return {
                    "input_text": transcript_text,
                    "attachments": working,
                    "transcription": transcription,
                    "delivery_mode": "audio_with_transcript",
                    "target_capability": target_capability_kind,
                }
            working = [
                mark_transcription_status(
                    item,
                    status=str(transcription.get("status") or "unavailable"),
                    reason=str(transcription.get("reason") or ""),
                )
                if isinstance(item, dict) and _is_audio_attachment(item)
                else item
                for item in working
            ]
            fallback = audio_passthrough_input_text(transcription)
            return {
                "input_text": fallback,
                "attachments": working,
                "transcription": transcription,
                "delivery_mode": "audio_direct",
                "target_capability": target_capability_kind,
            }

        clean_attachments = [
            strip_audio_media(item) if isinstance(item, dict) and _is_audio_attachment(item) else item
            for item in working
        ]
        return {
            "input_text": transcript_text or input_text,
            "attachments": clean_attachments,
            "transcription": transcription,
            "delivery_mode": "transcript",
            "target_capability": target_capability_kind,
        }

    def _record(self, event: AmbientTriggerEvent, status: str, reason: str, **extra: Any) -> dict[str, Any]:
        record = {
            "event_id": event.event_id,
            "source": event.source,
            "trigger": event.trigger,
            "mode": event.mode,
            "status": status,
            "reason": reason,
            "confidence": event.confidence,
            "duration_ms": event.duration_ms,
            "created_at": event.created_at,
            **extra,
        }
        audited = self.audit.record(record)
        if status not in {"ignored", "denied"}:
            self.store.mark_trigger(audited)
        return audited

    def _service_for_event(self, state: dict[str, Any], event: AmbientTriggerEvent) -> dict[str, Any] | None:
        services = state.get("services") if isinstance(state.get("services"), dict) else {}
        if event.trigger == "voice_wake":
            return services.get("voice_wake_monitor") if isinstance(services.get("voice_wake_monitor"), dict) else None
        if event.trigger in {"pinch", "gesture_choice", "approval_gesture"}:
            return services.get("gesture_wake_monitor") if isinstance(services.get("gesture_wake_monitor"), dict) else None
        return None

    def _event_can_run_without_monitor(self, event: AmbientTriggerEvent) -> bool:
        return (
            event.source == "hook"
            and event.trigger == "external_hook"
            and event.mode == "preset_text"
        ) or (
            event.source == "microphone"
            and event.trigger == "transcription_test"
        )

    def _cooldown_reason(self, state: dict[str, Any]) -> str:
        service = state.get("services", {}).get("gesture_wake_monitor", {})
        last_trigger_at = service.get("last_trigger_at")
        if not isinstance(last_trigger_at, (int, float)):
            return ""
        cooldown_ms = int(service.get("cooldown_ms") or 0)
        elapsed_ms = (time.time() - float(last_trigger_at)) * 1000
        if cooldown_ms > 0 and elapsed_ms < cooldown_ms:
            return "pinch.cooldown"
        return ""

    def _action_id(self, event: AmbientTriggerEvent) -> str:
        configured = event.action_id or event.payload.get("next_action") or "chat.message"
        action_id = str(configured or "chat.message").strip()
        return ACTION_ALIASES.get(action_id, action_id)

    def _event_needs_microphone(self, event: AmbientTriggerEvent) -> bool:
        if event.source == "microphone":
            return True
        if event.source == "camera" and event.trigger == "pinch":
            if event.mode in PINCH_RECORD_START_MODES or event.mode in PINCH_RECORD_RELEASE_MODES:
                return True
            return bool(self._attachments_for_event(event))
        return False

    def _attachments_for_event(self, event: AmbientTriggerEvent) -> list[dict[str, Any]]:
        return materialize_ambient_event_attachments(event.payload, event_id=event.event_id)

    def _default_input_text(self, event: AmbientTriggerEvent, attachments: list[dict[str, Any]]) -> str:
        if any(_is_audio_attachment(item) for item in attachments):
            if event.source == "camera" and event.trigger == "pinch":
                return "このpinch中に録音した音声を入力として処理してください。"
            return "この録音音声を入力として処理してください。"
        if event.trigger == "gesture_choice":
            choice = str(event.payload.get("choice") or event.payload.get("finger_choice") or "").strip()
            if choice in {"2", "3", "4"}:
                return choice
        return ""


def _is_audio_attachment(attachment: dict[str, Any]) -> bool:
    return is_audio_attachment(attachment)


def _uses_finger_recording_template(
    event: AmbientTriggerEvent,
    attachments: list[dict[str, Any]],
) -> bool:
    return (
        event.source == "camera"
        and event.trigger == "pinch"
        and any(_is_audio_attachment(item) for item in attachments if isinstance(item, dict))
    )


def _detach_pending_audio_media(
    attachments: list[dict[str, Any]],
    *,
    request_id: str,
    store_path: str,
    expires_at_epoch: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    clean_attachments: list[dict[str, Any]] = []
    blob_ids: list[str] = []
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            clean_attachments.append(attachment)
            continue
        item = dict(attachment)
        if not _is_audio_attachment(item):
            clean_attachments.append(item)
            continue
        media = _raw_audio_media_from_attachment(item)
        clean_item = strip_audio_media(item)
        if media:
            metadata = dict(clean_item.get("metadata") if isinstance(clean_item.get("metadata"), dict) else {})
            media_size = _pending_audio_media_size(media)
            if media_size > MAX_PENDING_AUDIO_BLOB_BYTES:
                metadata["ambient_audio_blob_omitted"] = "blob_too_large"
                metadata["ambient_audio_blob_size"] = media_size
            else:
                blob_id = f"{request_id}:{index}:{uuid.uuid4().hex}"
                with _AMBIENT_PENDING_LOCK:
                    _trim_pending_audio_blobs_locked()
                    if (
                        len(_PENDING_AI_SEND_AUDIO_BLOBS) >= MAX_PENDING_AUDIO_BLOBS
                        or _pending_audio_total_bytes_locked() + media_size > MAX_PENDING_AUDIO_TOTAL_BYTES
                    ):
                        blob_id = ""
                    else:
                        _PENDING_AI_SEND_AUDIO_BLOBS[blob_id] = {
                            "store_path": store_path,
                            "expires_at_epoch": expires_at_epoch,
                            "size_bytes": media_size,
                            "media": media,
                        }
                if blob_id:
                    metadata["ambient_audio_blob_id"] = blob_id
                    blob_ids.append(blob_id)
                else:
                    metadata["ambient_audio_blob_omitted"] = "pending_audio_capacity_exceeded"
                    metadata["ambient_audio_blob_size"] = media_size
            clean_item["metadata"] = metadata
        clean_attachments.append(clean_item)
    return clean_attachments, blob_ids


def _rehydrate_pending_audio_media(pending: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = pending.get("attachments") if isinstance(pending.get("attachments"), list) else []
    store_path = str(pending.get("store_path") or "")
    now = time.time()
    result: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            result.append(attachment)
            continue
        item = dict(attachment)
        metadata = dict(item.get("metadata") if isinstance(item.get("metadata"), dict) else {})
        blob_id = str(metadata.get("ambient_audio_blob_id") or "").strip()
        with _AMBIENT_PENDING_LOCK:
            blob = copy.deepcopy(_PENDING_AI_SEND_AUDIO_BLOBS.get(blob_id)) if blob_id else None
        if (
            blob is not None
            and str(blob.get("store_path") or "") == store_path
            and float(blob.get("expires_at_epoch") or 0) > now
        ):
            media = blob.get("media") if isinstance(blob.get("media"), dict) else {}
            attachment_media = media.get("attachment") if isinstance(media.get("attachment"), dict) else {}
            for key, value in attachment_media.items():
                item[key] = value
            metadata_media = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
            metadata.update(metadata_media)
        metadata.pop("ambient_audio_blob_id", None)
        if metadata:
            item["metadata"] = metadata
        elif "metadata" in item:
            item.pop("metadata", None)
        result.append(item)
    return result


def _delete_pending_audio_blobs(pending: dict[str, Any]) -> None:
    with _AMBIENT_PENDING_LOCK:
        _delete_pending_audio_blobs_locked(pending)


def _delete_pending_audio_blobs_locked(pending: dict[str, Any]) -> None:
    blob_ids = pending.get("audio_blob_ids") if isinstance(pending.get("audio_blob_ids"), list) else []
    for blob_id in blob_ids:
        _PENDING_AI_SEND_AUDIO_BLOBS.pop(str(blob_id), None)


def _pending_audio_media_size(media: dict[str, Any]) -> int:
    total = 0
    for section in ("attachment", "metadata"):
        values = media.get(section) if isinstance(media.get(section), dict) else {}
        for value in values.values():
            total += _raw_media_value_size(value)
    return total


def _raw_media_value_size(value: Any) -> int:
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, bytearray):
        return len(value)
    if not isinstance(value, str):
        return 0
    text = value.strip()
    if text.startswith("data:") and "," in text:
        payload = text.split(",", 1)[1]
        return max(0, (len(payload) * 3) // 4)
    return len(text.encode("utf-8"))


def _pending_audio_total_bytes_locked() -> int:
    return sum(int(blob.get("size_bytes") or 0) for blob in _PENDING_AI_SEND_AUDIO_BLOBS.values())


def _trim_pending_audio_blobs_locked() -> None:
    now = time.time()
    for blob_id, blob in list(_PENDING_AI_SEND_AUDIO_BLOBS.items()):
        if float(blob.get("expires_at_epoch") or 0) <= now:
            _PENDING_AI_SEND_AUDIO_BLOBS.pop(blob_id, None)
    while len(_PENDING_AI_SEND_AUDIO_BLOBS) > MAX_PENDING_AUDIO_BLOBS:
        _PENDING_AI_SEND_AUDIO_BLOBS.pop(next(iter(_PENDING_AI_SEND_AUDIO_BLOBS)), None)
    while _pending_audio_total_bytes_locked() > MAX_PENDING_AUDIO_TOTAL_BYTES and _PENDING_AI_SEND_AUDIO_BLOBS:
        _PENDING_AI_SEND_AUDIO_BLOBS.pop(next(iter(_PENDING_AI_SEND_AUDIO_BLOBS)), None)


def _trim_pending_approvals_locked() -> None:
    while len(_PENDING_AI_SEND_APPROVALS) > MAX_PENDING_AI_SEND_APPROVALS:
        request_id = next(iter(_PENDING_AI_SEND_APPROVALS))
        pending = _PENDING_AI_SEND_APPROVALS.pop(request_id, None)
        if pending is not None:
            _delete_pending_audio_blobs_locked(pending)


def _strip_raw_audio_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload if isinstance(payload, dict) else {})
    for key in RAW_AUDIO_ATTACHMENT_KEYS:
        clean.pop(key, None)
    raw_attachments = clean.get("attachments")
    if isinstance(raw_attachments, list):
        clean["attachments"] = [
            strip_audio_media(item) if isinstance(item, dict) and _is_audio_attachment(item) else item
            for item in raw_attachments
        ]
    return clean


def _raw_audio_media_from_attachment(attachment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    media: dict[str, dict[str, Any]] = {}
    attachment_media = {
        key: attachment[key]
        for key in RAW_AUDIO_ATTACHMENT_KEYS
        if key in attachment and attachment.get(key) is not None
    }
    if attachment_media:
        media["attachment"] = attachment_media
    metadata = attachment.get("metadata") if isinstance(attachment.get("metadata"), dict) else {}
    metadata_media = {
        key: metadata[key]
        for key in RAW_AUDIO_ATTACHMENT_KEYS
        if key in metadata and metadata.get(key) is not None
    }
    if metadata_media:
        media["metadata"] = metadata_media
    return media


def _ambient_event_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _trim_ambient_event_settlements_locked() -> None:
    cutoff = time.time() - AMBIENT_EVENT_SETTLEMENT_TTL_SECONDS
    for event_id, settlement in list(_AMBIENT_EVENT_SETTLEMENTS.items()):
        if float(settlement.get("updated_at_epoch") or 0) <= cutoff:
            _AMBIENT_EVENT_SETTLEMENTS.pop(event_id, None)
    while len(_AMBIENT_EVENT_SETTLEMENTS) > MAX_AMBIENT_EVENT_SETTLEMENTS:
        completed_event_id = next(
            (
                event_id
                for event_id, settlement in _AMBIENT_EVENT_SETTLEMENTS.items()
                if settlement.get("state") == "complete"
            ),
            None,
        )
        if completed_event_id is None:
            break
        _AMBIENT_EVENT_SETTLEMENTS.pop(completed_event_id, None)


def _tools_for_event(event: AmbientTriggerEvent, params: dict[str, Any]) -> list[Any]:
    raw_tools = event.payload.get("tools")
    if isinstance(raw_tools, list):
        return list(raw_tools)
    tool_policy = params.get("tool_policy") if isinstance(params.get("tool_policy"), dict) else {}
    selected_tools = tool_policy.get("selected_tools")
    if isinstance(selected_tools, list):
        return [item for item in selected_tools if str(item or "").strip()]
    return []


def _model_supports_audio(model_ref: str) -> bool:
    return bool(model_input_capability(model_ref).get("supports_audio_input"))


def _local_transcription_status() -> dict[str, Any]:
    try:
        from .local_transcription import local_whisper_status

        return local_whisper_status()
    except Exception:
        return {
            "status": "local_whisper_not_configured",
            "configured": False,
            "reason": "ローカルWhisperの状態を確認できませんでした。",
        }


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _preview(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _epoch_iso(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
