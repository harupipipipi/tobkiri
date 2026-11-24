"""Private Native signed-Pack onboarding, separate from browser capabilities."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest

from .api.api_response import APIResponse

logger = logging.getLogger(__name__)
_FAILURE_STAGES = frozenset({
    "trust", "preview", "review_parameters", "review_context", "prepare",
    "authorize", "commit",
})


def _log_failure_stage(stage: Any, *, publication_unknown: bool = False) -> None:
    """Emit only a fixed Host stage; diagnostics never change the response."""
    label = stage if type(stage) is str and stage in _FAILURE_STAGES else "trust"
    message = (
        "Host Extension admission outcome requires reconciliation stage=%s"
        if publication_unknown is True
        else "Native signed Pack verification denied stage=%s"
    )
    try:
        logger.warning(message, label)
    except Exception:
        pass


def _current_review_context() -> dict[str, Any]:
    """Capture current Host facts without accepting an owner from the request."""
    from .authority.v4 import AuthorityStore
    from .bootstrap.profile_capture import (
        activation_audit_receipt,
        capture_active_profile,
        invalidate_profile_capture_scope,
        profile_capture_scope,
        runtime_user_data_root,
    )

    root = runtime_user_data_root().resolve(strict=True)
    with profile_capture_scope():
        invalidate_profile_capture_scope()
        active = capture_active_profile()
        activation_audit_receipt(active, base_dir=root)
        activation = active.activation
        with AuthorityStore(root / "authority" / "v4.sqlite3") as authority:
            if authority.security_epoch != activation["security_epoch"]:
                raise ValueError("Native publisher review Authority epoch changed")
    return {
        "host_owner_id": canonical_digest(
            {
                "schema": "io.tobkiri.native-publisher-owner.v1",
                "root": str(root),
            }
        ),
        "profile_revision": str(activation["profile_revision"]),
        "plan_digest": str(activation["plan_digest"]),
        "activation_id": str(activation["activation_id"]),
        "security_epoch": activation["security_epoch"],
        "fencing_token": activation["fencing_token"],
    }


def handle_native_pack_onboarding(handler: Any, *, commit: bool) -> None:
    """Require Native authentication before parsing paths or approval echoes."""
    if (
        handler.headers.get("Origin") is not None
        or not handler._native_pack_launcher_authenticated()
    ):
        handler._discard_request_body()
        handler._send_response(APIResponse(False, error="Unauthorized"), 401)
        return
    if os.name == "nt":
        handler._discard_request_body()
        handler._send_response(
            APIResponse(False, error="Signed Pack onboarding requires a POSIX Host"),
            409,
        )
        return
    body = handler._parse_object_body()
    if body is None:
        return
    ceremony = handler._native_pack_key_ceremony

    def discard_echo() -> None:
        challenge = body.get("key_version_authorization_challenge")
        if commit and ceremony is not None and isinstance(challenge, str):
            ceremony.discard(challenge)

    required = {"source_root", "public_key_path"}
    echoed = {"key_version_authorization_challenge", "key_version_authorization_digest"}
    if commit:
        required.add("preview_digest")
    fields = set(body)
    if fields != required and not (commit and fields == required | echoed):
        discard_echo()
        handler._send_response(APIResponse(False, error="Invalid Pack selection"), 400)
        return
    source_value, key_value = body.get("source_root"), body.get("public_key_path")
    if any(
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not Path(value).is_absolute()
        for value in (source_value, key_value)
    ) or (
        commit
        and any(
            not isinstance(body[name], str)
            or not body[name]
            or "\x00" in body[name]
            or len(body[name]) > 256
            for name in fields - {"source_root", "public_key_path"}
        )
    ):
        discard_echo()
        handler._send_response(APIResponse(False, error="Invalid Pack selection"), 400)
        return
    source, key = Path(source_value), Path(key_value)
    # Import through the canonical module at call time, preserving test seams.
    from . import native_pack_onboarding as onboarding
    from .external_pack_catalog_v4 import ExternalPackAdmissionOutcomeUnknown

    stage = "trust"

    def review_context() -> dict[str, Any]:
        nonlocal stage
        parent_stage = stage
        stage = "review_context"
        result = _current_review_context()
        stage = parent_stage
        return result

    try:
        trust_store = handler._native_pack_trust_store()
        if commit:
            if trust_store is None:
                raise ValueError("Host publisher trust store is not configured")
            arguments: dict[str, Any] = {}
            if echoed.issubset(fields):
                stage = "authorize"
                if ceremony is None:
                    raise ValueError("Native publisher key review is unavailable")
                stage = "preview"
                fresh = onboarding.preview_native_signed_pack(
                    source,
                    key,
                    trust_store_path=trust_store,
                )
                if fresh["preview_digest"] != body["preview_digest"]:
                    raise ValueError("Native signed Pack preview changed")
                stage = "authorize"
                arguments["key_version_policy_identity"] = ceremony.authorize(
                    fresh,
                    challenge=body["key_version_authorization_challenge"],
                    authorization_digest=body["key_version_authorization_digest"],
                    trust_store_path=trust_store,
                    context_loader=review_context,
                )
            stage = "commit"
            entry = onboarding.commit_native_signed_pack(
                source,
                key,
                expected_preview_digest=body["preview_digest"],
                trust_store_path=trust_store,
                **arguments,
            )
            refreshed = False
            refresh = handler._runtime_refresh
            if refresh is not None and not entry.get("reconciliation_needed"):
                try:
                    refresh(None)
                    refreshed = True
                except Exception:
                    logger.warning("Onboarded Pack catalog refresh failed")
            if entry.get("reconciliation_needed"):
                logger.warning(
                    "Host Extension admission committed; reconciliation required"
                )
            result = {
                "pack_id": str(entry["pack_id"]),
                "artifact_digest": str(entry["artifact_digest"]),
                "publisher_id": str(entry["publisher_id"]),
                "catalog_refreshed": refreshed,
            }
        else:
            stage = "preview"
            result = onboarding.preview_native_signed_pack(
                source,
                key,
                trust_store_path=trust_store,
            )
            if ceremony is not None and trust_store is not None:
                stage = "review_parameters"
                parameters = onboarding.native_key_version_review_parameters(
                    source, key, result, trust_store_path=trust_store,
                )
                stage = "prepare"
                result = ceremony.prepare(
                    result,
                    trust_store_path=trust_store,
                    context_loader=review_context,
                    **parameters,
                )
    except ExternalPackAdmissionOutcomeUnknown:
        discard_echo()
        _log_failure_stage(stage, publication_unknown=True)
        handler._send_response(
            APIResponse(False, error="Pack admission outcome requires reconciliation"),
            409,
        )
        return
    except Exception:
        discard_echo()
        # Do not put caller paths, public review challenges, or secrets in logs.
        _log_failure_stage(stage)
        handler._send_response(
            APIResponse(False, error="Signed Pack verification was denied"),
            409,
        )
        return
    warning = (
        "Pack admission committed; catalog reconciliation is required"
        if commit and entry.get("reconciliation_needed")
        else None
    )
    if warning is not None:
        # Native v1 reads envelope.error only on a failed HTTP/API outcome.
        handler._send_response(APIResponse(False, data=result, error=warning), 409)
    else:
        handler._send_response(APIResponse(True, data=result))
