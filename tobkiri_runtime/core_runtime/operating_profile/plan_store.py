from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from ..hmac_key_manager import compute_data_hmac, generate_or_load_signing_key, verify_data_hmac
from ..profile_workspace import ProfileWorkspaceManager, validate_profile_id
from .constants import PLAN_SPEC_VERSION
from .models import OperatingProfile
from .provenance import canonical_json, stable_sha256
from tobkiri_protocol.validation import validate_document


DEFAULT_PLAN_TTL_SECONDS = 30 * 60
SIGNATURE_PREFIX = "hmac-sha256:"


class OperatingProfilePlanStore:
    def __init__(self, workspace_manager: ProfileWorkspaceManager | None = None) -> None:
        self.workspace_manager = workspace_manager or ProfileWorkspaceManager()

    def create_plan(
        self,
        profile_id: str,
        target_profile: OperatingProfile | Mapping[str, Any],
        *,
        actor: str = "local_user",
        reason: str = "",
        input_hash: str | None = None,
        expires_in_seconds: int = DEFAULT_PLAN_TTL_SECONDS,
    ) -> dict[str, Any]:
        safe_profile_id = validate_profile_id(profile_id)
        target = target_profile if isinstance(target_profile, OperatingProfile) else OperatingProfile.from_dict(target_profile)
        previous = self.load_active_profile(safe_profile_id)
        issued_at = int(time.time())
        unsigned = {
            "version": PLAN_SPEC_VERSION,
            "profile_id": safe_profile_id,
            "actor": str(actor),
            "reason": str(reason),
            "issued_at": issued_at,
            "expires_at": issued_at + int(expires_in_seconds),
            "settings_revision": self._settings_revision(safe_profile_id),
            "pack_digest": self._pack_digest(),
            "input_hash": str(input_hash or stable_sha256({"target_profile": target.to_dict()})),
            "target_profile": target.to_dict(),
            "previous_profile": previous.to_dict() if previous else None,
        }
        plan_id = stable_sha256(unsigned)[:24]
        plan = {**unsigned, "plan_id": plan_id}
        return {**plan, "signature": self._signature(plan)}

    def apply_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        self._verify_plan(plan, for_apply=True)
        profile_id = validate_profile_id(str(plan.get("profile_id") or ""))
        target = plan.get("target_profile")
        if not isinstance(target, Mapping):
            raise ValueError("plan target_profile must be an object")
        paths = self._paths(profile_id)
        plan_path = self._scoped(paths["plans_dir"] / f"{plan['plan_id']}.json", profile_id)
        active_path = self._scoped(paths["active_path"], profile_id)
        applied_path = self._scoped(paths["applied_path"], profile_id)
        self._atomic_write_json(plan_path, dict(plan))
        self._atomic_write_json(active_path, dict(target))
        self._atomic_write_json(applied_path, {"profile_id": profile_id, "plan_id": plan["plan_id"]})
        return {"applied": True, "profile_id": profile_id, "plan_id": plan["plan_id"], "path": str(active_path)}

    def undo_plan(self, profile_id: str, plan_id: str | None = None) -> dict[str, Any]:
        safe_profile_id = validate_profile_id(profile_id)
        paths = self._paths(safe_profile_id)
        if plan_id is None:
            applied = self._read_json(paths["applied_path"])
            plan_id = str(applied.get("plan_id") or "")
        if not plan_id:
            raise ValueError("plan_id is required for undo")
        plan_path = self._scoped(paths["plans_dir"] / f"{plan_id}.json", safe_profile_id)
        plan = self._read_json(plan_path)
        self._verify_plan(plan, for_apply=False)
        previous = plan.get("previous_profile")
        active_path = self._scoped(paths["active_path"], safe_profile_id)
        if isinstance(previous, Mapping):
            self._atomic_write_json(active_path, dict(previous))
        elif active_path.exists():
            active_path.unlink()
        undo_path = self._scoped(paths["undo_path"], safe_profile_id)
        self._atomic_write_json(undo_path, {"profile_id": safe_profile_id, "undone_plan_id": plan_id})
        return {"undone": True, "profile_id": safe_profile_id, "plan_id": plan_id, "path": str(active_path)}

    def load_active_profile(self, profile_id: str) -> OperatingProfile | None:
        path = self._paths(profile_id)["active_path"]
        data = self._read_json(path)
        if not data:
            return None
        return OperatingProfile.from_dict(data)

    def _paths(self, profile_id: str) -> dict[str, Path]:
        safe_profile_id = validate_profile_id(profile_id)
        self.workspace_manager.initialize_profile_workspace({"profile_id": safe_profile_id}, create_missing=True)
        workspace_paths = self.workspace_manager.paths_for_profile(safe_profile_id)
        root = workspace_paths.root / "operating_profile"
        plans_dir = root / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        return {
            "root": root,
            "plans_dir": plans_dir,
            "active_path": root / "active.json",
            "applied_path": root / "applied_plan.json",
            "undo_path": root / "last_undo.json",
        }

    def _verify_plan(self, plan: Mapping[str, Any], *, for_apply: bool) -> None:
        if plan.get("version") != PLAN_SPEC_VERSION:
            raise ValueError("unsupported operating profile plan version")
        signature = plan.get("signature")
        unsigned = {key: plan[key] for key in plan if key != "signature"}
        required = ("plan_id", "issued_at", "expires_at", "settings_revision", "pack_digest", "input_hash")
        missing = [key for key in required if key not in unsigned]
        if missing:
            raise ValueError("operating profile plan missing signed metadata: " + ", ".join(missing))
        if not isinstance(signature, str) or not signature.startswith(SIGNATURE_PREFIX):
            raise ValueError("operating profile plan signature mismatch")
        expected = signature.removeprefix(SIGNATURE_PREFIX)
        if not verify_data_hmac(self._signing_key(), dict(unsigned), expected):
            raise ValueError("operating profile plan signature mismatch")
        if for_apply:
            now = int(time.time())
            expires_at = int(plan.get("expires_at") or 0)
            if expires_at <= now:
                raise ValueError("operating profile plan expired")
            profile_id = validate_profile_id(str(plan.get("profile_id") or ""))
            if str(plan.get("settings_revision") or "") != self._settings_revision(profile_id):
                raise ValueError("operating profile plan settings revision mismatch")
            if str(plan.get("pack_digest") or "") != self._pack_digest():
                raise ValueError("operating profile plan pack digest mismatch")

    def _signature(self, plan_without_signature: Mapping[str, Any]) -> str:
        return SIGNATURE_PREFIX + compute_data_hmac(self._signing_key(), dict(plan_without_signature))

    def _signing_key(self) -> bytes:
        key_path = self.workspace_manager.user_data_root / "operating_profile_plan.hmac"
        return generate_or_load_signing_key(key_path, env_var="RUMI_OPERATING_PROFILE_PLAN_HMAC_KEY")

    def _settings_revision(self, profile_id: str) -> str:
        active = self.load_active_profile(profile_id)
        return stable_sha256(
            {
                "profile_id": validate_profile_id(profile_id),
                "active_profile": active.to_dict() if active else None,
            }
        )

    def _pack_digest(self) -> str:
        pack_manifest = (
            Path(__file__).resolve().parents[2]
            / "ecosystem"
            / "defaultspack"
            / "pack.v4.json"
        )
        try:
            raw = pack_manifest.read_bytes()
            manifest = validate_document(raw, "pack")
        except (OSError, ValueError) as exc:
            raise ValueError("canonical defaultspack Pack v4 artifact is unavailable") from exc
        return stable_sha256(
            {
                "pack_id": manifest["pack"]["id"],
                "source_identity": manifest["integrity"]["source_identity"],
                "artifact_digest": manifest["pack"]["artifact_digest"],
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    def _scoped(self, path: Path, profile_id: str) -> Path:
        root = self.workspace_manager.paths_for_profile(profile_id).root.resolve()
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            raise ValueError("operating profile plan path escaped profile workspace")
        return resolved

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _atomic_write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        text = canonical_json(dict(payload)) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(path)
