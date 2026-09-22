"""Captured Pack-v4 service for local mobile pairing decisions.

The mobile pairing records live in the Defaultspack P2P store, but a desktop
decision is a Host-owned effect.  This service deliberately exposes only the
four finite review operations that are reached through the captured Broker;
it never registers an HTTP route or accepts a caller-selected profile/store.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ecosystem.defaultspack.domain.p2p.device_store import DeviceStore
from ecosystem.defaultspack.domain.p2p.pairing import PairingManager
from ecosystem.defaultspack.domain.p2p.settings import P2PSettings
from ecosystem.defaultspack.domain.p2p.token_delivery import (
    encrypt_token_delivery,
)

from .authority.device_key_registry import DeviceKeyRegistry
from .hmac_key_manager import generate_or_load_signing_key


MOBILE_PAIRING_CONTRACT = "tobkiri.host.mobile-pairing.v4"
MOBILE_PAIRING_OPERATIONS = frozenset(
    {
        "pairing.approve",
        "pairing.reject",
        "pairing.review.read",
        "pairing.status.read",
    }
)
_MAX_PAIRING_ID_LENGTH = 160
_MAX_REASON_LENGTH = 512
_MAX_SCOPE_COUNT = 32
_MAX_SCOPE_LENGTH = 128
_IDENTITY_FIELDS = frozenset(
    {
        "catalog_revision",
        "plan_digest",
        "profile_id",
        "profile_revision",
        "runtime_profile_id",
        "workspace_id",
    }
)


class MobilePairingServiceV4:
    """Perform captured desktop pairing decisions for one resolved Profile."""

    def __init__(
        self,
        *,
        profile_id: str,
        profile_revision: str,
        plan_digest: str,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._profile_id = _required_string(profile_id, "profile identity")
        self._profile_revision = _required_string(profile_revision, "profile revision")
        self._plan_digest = _required_string(plan_digest, "plan digest")
        self._fault_injector = fault_injector

    def invoke(self, operation_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one finite operation with no client-selected identity."""

        if operation_id not in MOBILE_PAIRING_OPERATIONS:
            return _error_result()
        arguments = dict(payload)
        if any(field in arguments for field in _IDENTITY_FIELDS):
            return _error_result()
        pairing_id = _pairing_id(arguments.pop("pairing_id", None))
        if not pairing_id:
            return _error_result()
        if operation_id == "pairing.status.read":
            if arguments:
                return _error_result()
            return self._status(pairing_id)
        if operation_id == "pairing.review.read":
            if arguments:
                return _error_result()
            return self._review(pairing_id)
        if operation_id == "pairing.reject":
            reason = arguments.pop("reason", "rejected")
            if arguments or not isinstance(reason, str) or len(reason) > _MAX_REASON_LENGTH:
                return _error_result()
            return self._reject(pairing_id, reason)
        claim_hash = arguments.pop("claim_hash", None)
        scopes = arguments.pop("scopes", None)
        if arguments or not _claim_hash(claim_hash) or not _scopes(scopes):
            return _error_result()
        return self._approve(pairing_id, str(claim_hash), list(scopes))

    @staticmethod
    def _manager() -> PairingManager:
        """Load only the configured local P2P store, never a request override."""

        return PairingManager(P2PSettings.from_env().store_path)

    def _status(self, pairing_id: str) -> dict[str, Any]:
        manager = self._manager()
        if not self._recover_approval(manager, pairing_id):
            return _error_result()
        session = manager.get_pairing(pairing_id)
        if session is None:
            return _error_result()
        result = session.public_dict()
        result["token_pickup_consumed_at"] = int(session.token_pickup_consumed_at)
        return result

    def _review(self, pairing_id: str) -> dict[str, Any]:
        manager = self._manager()
        if not self._recover_approval(manager, pairing_id):
            return _error_result()
        session = manager.get_pairing(pairing_id)
        if session is None:
            return _error_result()
        return session.review_dict()

    def _reject(self, pairing_id: str, reason: str) -> dict[str, Any]:
        manager = self._manager()
        if not self._recover_approval(manager, pairing_id):
            return _error_result()
        session = manager.get_pairing(pairing_id)
        if session is None:
            return _error_result()
        result = manager.reject_pairing(session.code, reason=reason or "rejected")
        if not result.get("ok") or not isinstance(result.get("pairing"), dict):
            return _error_result()
        return {"pairing": _public_pairing(result["pairing"])}

    def _approve(
        self,
        pairing_id: str,
        claim_hash: str,
        scopes: list[str],
    ) -> dict[str, Any]:
        manager = self._manager()
        if not self._recover_approval(manager, pairing_id):
            return _error_result()
        result = manager.prepare_mobile_approval(
            pairing_id,
            claim_hash=claim_hash,
            scopes=scopes,
            profile_id=self._profile_id,
            profile_revision=self._profile_revision,
            plan_digest=self._plan_digest,
        )
        if not result.get("ok") or not isinstance(result.get("pairing"), dict):
            return _error_result()
        if result.get("state") == "approved":
            return self._approved_result(result["pairing"], device_key_registered=False)
        transaction_id = str(result.get("transaction_id") or "")
        if not transaction_id:
            return _error_result()
        self._checkpoint("after_pairing_prepare")
        session = manager.get_pairing(pairing_id)
        if session is None:
            return _error_result()
        store_path = P2PSettings.from_env().store_path
        device_store = DeviceStore(store_path)
        issued_device_id = session.claimed_device_id
        try:
            existing = device_store.get_device(issued_device_id)
            if existing is not None and (
                existing.pairing_id != pairing_id
                or existing.profile_id != self._profile_id
            ):
                raise ValueError("claimed device is owned by another pairing")
            device, device_token, approval_token = device_store.issue_tokens(
                issued_device_id,
                label=session.claimed_device_label,
                public_key=session.claimed_device_public_key,
                encryption_public_key=session.claimed_device_encryption_public_key,
                scopes=list(session.approval_transaction["binding"]["scopes"]),
                pairing_id=pairing_id,
                profile_id=self._profile_id,
                activate=False,
            )
            issued_device_id = device.device_id
            self._checkpoint("after_device_issue")
            if not manager.stage_mobile_approval_device(
                pairing_id,
                transaction_id=transaction_id,
            ).get("ok"):
                raise ValueError("staged device is unavailable")
            envelope = encrypt_token_delivery(
                {
                    "device_token": device_token,
                    "approval_token": approval_token,
                    "client_access_token": device_token,
                    "approver_access_token": approval_token,
                    "device": device.as_dict(),
                    "scopes": list(device.scopes),
                    "approval_scopes": list(device.approval_scopes),
                    "profile_id": self._profile_id,
                    "confirmation_code": device.confirmation_code,
                },
                session.claimed_device_encryption_public_key,
                pairing_id=pairing_id,
                device_id=device.device_id,
            )
            if not manager.stage_mobile_approval_delivery(
                pairing_id,
                transaction_id=transaction_id,
                envelope=envelope,
            ).get("ok"):
                raise ValueError("token delivery is unavailable")
            self._checkpoint("after_delivery_stage")
            if not manager.commit_mobile_approval(
                pairing_id,
                transaction_id=transaction_id,
            ).get("ok"):
                raise ValueError("pairing commit is unavailable")
            self._checkpoint("after_pairing_commit")
            if device_store.activate_staged_device(
                issued_device_id,
                pairing_id=pairing_id,
                profile_id=self._profile_id,
            ) is None:
                raise ValueError("staged device activation is unavailable")
            if not manager.mark_mobile_device_activated(
                pairing_id,
                transaction_id=transaction_id,
            ).get("ok"):
                raise ValueError("device activation commit is unavailable")
        except Exception:
            self._abort_approval(
                manager,
                device_store,
                pairing_id,
                transaction_id=transaction_id,
                reason="token delivery failed",
            )
            return _error_result()

        key_record = self._register_device_key(
            device_id=issued_device_id,
            public_key=session.claimed_device_public_key,
        )
        completed = manager.get_pairing(pairing_id)
        return self._approved_result(
            completed.admin_dict() if completed is not None else result["pairing"],
            device_key_registered=key_record,
        )

    def _recover_approval(self, manager: PairingManager, pairing_id: str) -> bool:
        """Reconcile a pre-commit device after restart without reissuing it."""

        state = manager.mobile_approval_state(pairing_id)
        if not state.get("ok"):
            return False
        transaction = state.get("transaction")
        pairing = state.get("pairing")
        if not isinstance(transaction, dict) or not transaction:
            return True
        if not isinstance(pairing, dict):
            return False
        transaction_id = str(transaction.get("transaction_id") or "")
        binding = transaction.get("binding")
        device_id = str(transaction.get("device_id") or "")
        if (
            not transaction_id
            or not device_id
            or not self._binding_matches(binding, pairing_id, device_id)
        ):
            self._abort_approval(
                manager,
                DeviceStore(P2PSettings.from_env().store_path),
                pairing_id,
                transaction_id=transaction_id,
                reason="stale pairing approval",
            )
            return False
        device_store = DeviceStore(P2PSettings.from_env().store_path)
        device = device_store.get_device(device_id)
        matches_device = bool(
            device is not None
            and device.pairing_id == pairing_id
            and device.profile_id == self._profile_id
        )
        phase = str(transaction.get("state") or "")
        if phase == "delivery_staged":
            if device is None or not matches_device or device.status != "staged":
                self._abort_approval(
                    manager,
                    device_store,
                    pairing_id,
                    transaction_id=transaction_id,
                    reason="staged delivery recovery failed",
                )
                return False
            if not manager.commit_mobile_approval(
                pairing_id,
                transaction_id=transaction_id,
            ).get("ok"):
                return False
            phase = "device_activation_pending"
        if phase == "device_activation_pending":
            if not matches_device or device_store.activate_staged_device(
                device_id,
                pairing_id=pairing_id,
                profile_id=self._profile_id,
            ) is None:
                self._abort_approval(
                    manager,
                    device_store,
                    pairing_id,
                    transaction_id=transaction_id,
                    reason="device activation recovery failed",
                )
                return False
            return bool(
                manager.mark_mobile_device_activated(
                    pairing_id,
                    transaction_id=transaction_id,
                ).get("ok")
            )
        self._abort_approval(
            manager,
            device_store,
            pairing_id,
            transaction_id=transaction_id,
            reason="incomplete pairing approval",
        )
        return True

    def _abort_approval(
        self,
        manager: PairingManager,
        device_store: DeviceStore,
        pairing_id: str,
        *,
        transaction_id: str,
        reason: str,
    ) -> None:
        state = manager.mobile_approval_state(pairing_id)
        transaction = state.get("transaction") if isinstance(state, dict) else None
        device_id = ""
        if isinstance(transaction, dict):
            device_id = str(transaction.get("device_id") or "")
        if device_id:
            device = device_store.get_device(device_id)
            if (
                device is not None
                and device.pairing_id == pairing_id
                and device.profile_id == self._profile_id
            ):
                device_store.revoke_device(device_id)
        if transaction_id:
            manager.abort_mobile_approval(
                pairing_id,
                transaction_id=transaction_id,
                reason=reason,
            )

    def _binding_matches(
        self,
        binding: object,
        pairing_id: str,
        device_id: str,
    ) -> bool:
        if not isinstance(binding, Mapping):
            return False
        expected = {
            "pairing_id": pairing_id,
            "device_id": device_id,
            "profile_id": self._profile_id,
            "profile_revision": self._profile_revision,
            "plan_digest": self._plan_digest,
        }
        return all(str(binding.get(key) or "") == value for key, value in expected.items())

    def _approved_result(
        self,
        pairing: Mapping[str, Any],
        *,
        device_key_registered: bool,
    ) -> dict[str, Any]:
        return {
            "pairing": _public_pairing(pairing),
            "device": {
                "device_id": str(pairing.get("claimed_device_id") or pairing.get("peer_id") or ""),
                "label": str(pairing.get("claimed_device_label") or pairing.get("peer_label") or ""),
            },
            "profile_id": self._profile_id,
            "profile_revision": self._profile_revision,
            "plan_digest": self._plan_digest,
            "device_key_registered": device_key_registered,
            "token_delivery": "mobile_encrypted_pickup",
            "token_delivery_ready": True,
        }

    def _checkpoint(self, name: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(name)

    def _register_device_key(self, *, device_id: str, public_key: str) -> bool:
        """Persist a profile-scoped signing key when the device supplied one."""

        if not public_key.strip():
            return False
        from .bootstrap.profile_capture import runtime_user_data_root

        user_data = runtime_user_data_root()
        try:
            registry = DeviceKeyRegistry(
                user_data / "authority" / "device_keys",
                secret_key=generate_or_load_signing_key(
                    user_data / "permissions" / ".authority_device_key"
                ),
            )
            registry.register_device_key(
                profile_id=self._profile_id,
                device_id=device_id,
                public_key=public_key,
            )
        except (OSError, ValueError):
            return False
        return True


def _required_string(value: object, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _pairing_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_PAIRING_ID_LENGTH or "\x00" in cleaned:
        return ""
    return cleaned


def _claim_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(
            character in "0123456789abcdef"
            for character in value.removeprefix("sha256:")
        )
    )


def _scopes(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= _MAX_SCOPE_COUNT
        and all(
            isinstance(scope, str)
            and bool(scope.strip())
            and len(scope) <= _MAX_SCOPE_LENGTH
            for scope in value
        )
    )


def _public_pairing(pairing: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pairing_id": str(pairing.get("pairing_id") or ""),
        "status": str(pairing.get("status") or ""),
        "expires_at": int(pairing.get("expires_at") or 0),
    }


def _error_result() -> dict[str, Any]:
    """Return a stable public failure without reflecting pairing data."""

    return {"state": "error", "code": "invalid_request", "write_set": []}


__all__ = [
    "MOBILE_PAIRING_CONTRACT",
    "MOBILE_PAIRING_OPERATIONS",
    "MobilePairingServiceV4",
]
