"""Contract-only vision and transcription adapter for workspace media."""

from __future__ import annotations

from typing import Any, Callable, Final, Mapping


MEDIA_INSPECT: Final[str] = "rumi.service.media.inspect.v1"
AI_IMAGE: Final[str] = "rumi.service.ai.image.v1"
AI_TRANSCRIBE: Final[str] = "rumi.service.ai.audio.transcribe.v1"
_FORBIDDEN_FIELDS: Final[frozenset[str]] = frozenset(
    {"approved", "approval_token", "authority_token", "raw_bytes", "base64"}
)


class MediaAnalysisAdapter:
    """Join media references to replaceable AI modality contracts."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Analyze an inspected artifact without capture or provider imports."""

        forbidden = sorted(_FORBIDDEN_FIELDS.intersection(payload))
        if forbidden:
            return {
                "status": "denied",
                "success": False,
                "error_type": "inline_or_authority_material_forbidden",
                "forbidden_fields": forbidden,
            }
        if name == "vision.analyze":
            return self._dispatch(
                payload,
                inspect_operation="image.inspect",
                modality_contract=AI_IMAGE,
                modality_operation="analyze",
            )
        if name == "audio.transcribe":
            return self._dispatch(
                payload,
                inspect_operation="audio.inspect",
                modality_contract=AI_TRANSCRIBE,
                modality_operation="transcribe",
            )
        raise ValueError(f"unknown media analysis operation: {name}")

    def _dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        inspect_operation: str,
        modality_contract: str,
        modality_operation: str,
    ) -> dict[str, Any]:
        media_ref = {
            "profile_id": str(payload.get("profile_id") or "default"),
            "workspace_id": _required(payload, "workspace_id"),
            "path": _required(payload, "path"),
        }
        inspected = self.client.invoke(
            MEDIA_INSPECT,
            inspect_operation,
            media_ref,
        )
        if not isinstance(inspected, Mapping) or inspected.get("success") is not True:
            return {
                "status": "unavailable",
                "success": False,
                "error_type": "media_reference_unavailable",
                "inspection": dict(inspected) if isinstance(inspected, Mapping) else {},
            }
        request = {
            "media_ref": {
                "profile_id": media_ref["profile_id"],
                "workspace_id": media_ref["workspace_id"],
                "path": media_ref["path"],
                "size": int(inspected.get("size") or 0),
                "modified_ns": int(inspected.get("modified_ns") or 0),
            },
            "instructions": str(payload.get("instructions") or "").strip(),
            "model_profile_id": str(payload.get("model_profile_id") or "").strip(),
        }
        result = self.client.invoke(
            modality_contract,
            modality_operation,
            request,
        )
        if not isinstance(result, Mapping):
            return {
                "status": "unavailable",
                "success": False,
                "error_type": "modality_provider_unavailable",
            }
        return dict(result)


def create_media_analysis_adapter(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the contract-only media analysis adapter."""

    return MediaAnalysisAdapter(client).invoke


def _required(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value

