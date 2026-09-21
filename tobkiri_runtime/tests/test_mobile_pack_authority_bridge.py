from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class _Decision:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id

    def to_dict(self) -> dict:
        return {
            "allowed": False,
            "approval_required": True,
            "request_id": self.request_id,
            "permission_id": "pack.approve",
        }


class _Authority:
    def __init__(self) -> None:
        self.checked = []
        self.request = {
            "request_id": "auth_pack_1",
            "status": "approved",
            "permission_id": "pack.approve",
            "resource": {
                "kind": "defaultspack.pack_request",
                "pack_request_id": "pack_req_1",
            },
        }

    def check(self, **kwargs):
        self.checked.append(kwargs)
        return _Decision("auth_pack_1")

    def get_request(self, request_id: str):
        assert request_id == "auth_pack_1"
        return {"success": True, "request": self.request}


class _PackRequest:
    request_id = "pack_req_1"

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "mode": "forced_patch",
            "actor": "defaultspack",
            "target_pack_id": "testpack",
            "staging_id": "0123456789abcdef",
            "slot": "default",
            "changed_paths": ["testpack/ecosystem.json"],
            "detected_pack_ids": ["testpack"],
            "status": "pending",
        }


class _ExtensionManager:
    def __init__(self) -> None:
        self.approved = []
        self.rejected = []

    def list_pending(self):
        return [_PackRequest()]

    def approve_request(self, *, request_id: str, reviewer: str, decision_notes: str = ""):
        self.approved.append((request_id, reviewer, decision_notes))
        return {"request_id": request_id, "status": "applied"}

    def reject_request(self, *, request_id: str, reviewer: str, reason: str = ""):
        self.rejected.append((request_id, reviewer, reason))
        return {"request_id": request_id, "status": "rejected"}


def test_pack_request_sync_creates_authority_request(monkeypatch):
    from core_runtime import authority
    from ecosystem.defaultspack.backend.pack_extension import extension_manager
    from ecosystem.defaultspack.backend.pack_extension.authority_bridge import (
        sync_pending_pack_requests_to_authority,
    )

    fake_authority = _Authority()
    fake_manager = _ExtensionManager()
    monkeypatch.setattr(authority, "get_authority_service", lambda: fake_authority)
    monkeypatch.setattr(extension_manager, "_global_extension_manager", fake_manager)

    result = sync_pending_pack_requests_to_authority()

    assert result["success"] is True
    assert result["synced"] == 1
    assert fake_authority.checked[0]["permission_id"] == "pack.approve"
    assert fake_authority.checked[0]["resource"]["pack_request_id"] == "pack_req_1"
    assert fake_authority.checked[0]["profile_id"] == "default"


def test_authority_approval_applies_pack_request(monkeypatch):
    from core_runtime import authority
    from ecosystem.defaultspack.backend.pack_extension import extension_manager
    from ecosystem.defaultspack.backend.pack_extension.authority_bridge import (
        apply_pack_decision_for_authority_request,
    )

    fake_authority = _Authority()
    fake_manager = _ExtensionManager()
    monkeypatch.setattr(authority, "get_authority_service", lambda: fake_authority)
    monkeypatch.setattr(extension_manager, "_global_extension_manager", fake_manager)

    result = apply_pack_decision_for_authority_request(
        "auth_pack_1",
        decision="approve",
        reviewer="mobile_approver:phone",
        notes="looks good",
    )

    assert result["success"] is True
    assert fake_manager.approved == [("pack_req_1", "mobile_approver:phone", "looks good")]


def test_authority_deny_rejects_pack_request(monkeypatch):
    from core_runtime import authority
    from ecosystem.defaultspack.backend.pack_extension import extension_manager
    from ecosystem.defaultspack.backend.pack_extension.authority_bridge import (
        apply_pack_decision_for_authority_request,
    )

    fake_authority = _Authority()
    fake_manager = _ExtensionManager()
    monkeypatch.setattr(authority, "get_authority_service", lambda: fake_authority)
    monkeypatch.setattr(extension_manager, "_global_extension_manager", fake_manager)

    result = apply_pack_decision_for_authority_request(
        "auth_pack_1",
        decision="deny",
        reviewer="mobile_approver:phone",
        notes="not this one",
    )

    assert result["success"] is True
    assert fake_manager.rejected == [("pack_req_1", "mobile_approver:phone", "not this one")]
