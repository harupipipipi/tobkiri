"""Signed authority request, one-shot approval, deny, and audit storage."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..compat import safe_chmod
from ..hmac_key_manager import generate_or_load_signing_key
from ..paths import USER_DATA_DIR
from .models import AuthorityRequest


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_ts() -> str:
    return _now_utc().isoformat().replace("+00:00", "Z")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_filename(value: str) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return digest


_SECRET_KEY_EXACT = {
    "apikey",
    "xapikey",
    "authorization",
    "proxyauthorization",
    "bearer",
    "token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "secret",
    "password",
    "passwd",
    "cookie",
    "setcookie",
    "credential",
    "credentials",
    "clientsecret",
    "privatekey",
    "secretkey",
    "accesskey",
    "secretaccesskey",
}
_SECRET_KEY_SUFFIXES = ("token", "secret", "password", "passwd", "cookie", "credential", "credentials")
_RESOURCE_HASH_IGNORED_KEYS = frozenset({"stream"})


def _normalized_resource_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _resource_key_is_secret_like(key: str) -> bool:
    normalized = _normalized_resource_key(key)
    if not normalized:
        return False
    if normalized in _SECRET_KEY_EXACT:
        return True
    if "apikey" in normalized or "privatekey" in normalized or "secretkey" in normalized:
        return True
    return normalized.endswith(_SECRET_KEY_SUFFIXES)


def sanitize_authority_resource(resource: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in dict(resource or {}).items():
        if _resource_key_is_secret_like(key):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            output[key] = value
        elif isinstance(value, list):
            output[key] = [
                sanitize_authority_resource(item) if isinstance(item, dict) else item
                for item in value
                if isinstance(item, (str, int, float, bool, dict)) or item is None
            ]
        elif isinstance(value, dict):
            output[key] = sanitize_authority_resource(value)
    return output


class AuthorityRequestStore:
    """Persist authority state without storing secret values."""

    def __init__(self, base_dir: str | Path | None = None, hmac_key_manager: Any = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else USER_DATA_DIR / "authority"
        self._requests_dir = self._base_dir / "requests"
        self._one_shot_dir = self._base_dir / "one_shot"
        self._deny_dir = self._base_dir / "denies"
        self._audit_path = self._base_dir / "audit.jsonl"
        self._hmac_key_manager = hmac_key_manager
        self._fallback_key_path = USER_DATA_DIR / "permissions" / ".secret_key"
        self._lock = threading.RLock()
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for directory in (self._base_dir, self._requests_dir, self._one_shot_dir, self._deny_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _signing_key(self) -> bytes:
        if self._hmac_key_manager is not None:
            try:
                active = self._hmac_key_manager.get_active_key()
                if active:
                    return str(active).encode("utf-8")
            except Exception:
                pass
        return generate_or_load_signing_key(self._fallback_key_path)

    def _signature(self, payload: dict[str, Any]) -> str:
        filtered = {key: value for key, value in payload.items() if not key.startswith("_hmac")}
        return hmac.new(self._signing_key(), _canonical_json(filtered).encode("utf-8"), hashlib.sha256).hexdigest()

    def _signed(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        data["_hmac_signature"] = self._signature(data)
        return data

    def _verify(self, data: dict[str, Any]) -> bool:
        signature = str(data.get("_hmac_signature") or "")
        if not signature:
            return False
        payload = {key: value for key, value in data.items() if key != "_hmac_signature"}
        return hmac.compare_digest(signature, self._signature(payload))

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(self._signed(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            safe_chmod(tmp, 0o600)
        except (OSError, AttributeError):
            pass
        tmp.replace(path)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not self._verify(data):
            self.audit("authority_store_tampered", {"path": str(path)})
            return None
        data.pop("_hmac_signature", None)
        return data

    def resource_hash(self, resource: dict[str, Any]) -> str:
        safe_resource = self._safe_resource(resource)
        for key in _RESOURCE_HASH_IGNORED_KEYS:
            safe_resource.pop(key, None)
        return hashlib.sha256(_canonical_json(safe_resource).encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_resource(resource: dict[str, Any]) -> dict[str, Any]:
        return sanitize_authority_resource(resource)

    def create_request(
        self,
        *,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
        reason: str,
        risk_level: str,
        conversation_id: str | None = None,
        profile_id: str | None = None,
        node_id: str | None = None,
        graph_id: str | None = None,
        expires_in_seconds: int = 86400,
    ) -> AuthorityRequest:
        from .debug_cli_operator import active_authority_debug_binding

        debug_binding = active_authority_debug_binding(
            profile_id=profile_id,
            conversation_id=conversation_id,
            principal_id=principal_id,
        )
        with self._lock:
            existing = self._find_pending_request(
                principal_id,
                permission_id,
                resource,
                debug_binding=debug_binding,
            )
            if existing is not None:
                return existing
            now = _now_utc()
            request = AuthorityRequest(
                request_id="auth_" + secrets.token_urlsafe(16),
                status="pending",
                principal_id=principal_id,
                permission_id=permission_id,
                resource=self._safe_resource(resource),
                reason=reason,
                risk_level=risk_level,
                created_at=now.isoformat().replace("+00:00", "Z"),
                expires_at=(now + timedelta(seconds=max(60, int(expires_in_seconds or 86400)))).isoformat().replace("+00:00", "Z"),
                conversation_id=conversation_id,
                profile_id=profile_id,
                node_id=node_id,
                graph_id=graph_id,
                **debug_binding,
            )
            self._write_json(self._request_path(request.request_id), request.to_dict())
            self.audit(
                "authority_request_created",
                {
                    "request_id": request.request_id,
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "resource_hash": self.resource_hash(resource),
                    "risk_level": risk_level,
                },
            )
            return request

    def _find_pending_request(
        self,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
        *,
        debug_binding: dict[str, Any] | None = None,
    ) -> AuthorityRequest | None:
        wanted_hash = self.resource_hash(resource)
        expected_session = str((debug_binding or {}).get("debug_session_id") or "")
        expected_epoch = int((debug_binding or {}).get("lease_epoch") or 0)
        for request in self.list_requests("pending"):
            if request.principal_id != principal_id or request.permission_id != permission_id:
                continue
            if self.request_expired(request):
                self.set_request_status(request.request_id, "expired")
                continue
            if (
                request.debug_session_id != expected_session
                or request.lease_epoch != expected_epoch
            ):
                continue
            if self.resource_hash(request.resource) == wanted_hash:
                return request
        return None

    def _request_path(self, request_id: str) -> Path:
        return self._requests_dir / f"{_safe_filename(request_id)}.json"

    def get_request(self, request_id: str) -> AuthorityRequest | None:
        data = self._read_json(self._request_path(request_id))
        if not data:
            return None
        request = AuthorityRequest.from_dict(data)
        if self.request_expired(request) and request.status == "pending":
            self.set_request_status(request.request_id, "expired")
            return AuthorityRequest.from_dict({**request.to_dict(), "status": "expired"})
        return request

    def list_requests(self, status: str = "all") -> list[AuthorityRequest]:
        status = str(status or "all").strip().lower()
        requests: list[AuthorityRequest] = []
        for path in sorted(self._requests_dir.glob("*.json")):
            data = self._read_json(path)
            if not data:
                continue
            request = AuthorityRequest.from_dict(data)
            if request.status == "pending" and self.request_expired(request):
                self.set_request_status(request.request_id, "expired")
                request = AuthorityRequest.from_dict({**request.to_dict(), "status": "expired"})
            if status in {"", "all"} or request.status == status:
                requests.append(request)
        return sorted(requests, key=lambda item: item.created_at, reverse=True)

    def request_expired(self, request: AuthorityRequest) -> bool:
        expires_at = _parse_ts(request.expires_at)
        return bool(expires_at and expires_at <= _now_utc())

    def set_request_status(self, request_id: str, status: str) -> AuthorityRequest | None:
        if status not in {"pending", "approved", "denied", "expired"}:
            raise ValueError("invalid authority request status")
        with self._lock:
            data = self._read_json(self._request_path(request_id))
            if not data:
                return None
            data["status"] = status
            self._write_json(self._request_path(request_id), data)
            self._audit_best_effort("authority_request_status", {"request_id": request_id, "status": status})
            return AuthorityRequest.from_dict(data)

    def settle_pending_request(
        self,
        request_id: str,
        status: str,
        settle_callback=None,
        rollback_callback=None,
    ) -> dict[str, Any]:
        if status not in {"approved", "denied"}:
            raise ValueError("invalid authority terminal status")
        with self._lock:
            path = self._request_path(request_id)
            data = self._read_json(path)
            if not data:
                return {"settled": False, "request": None, "reason": "not_found"}
            request = AuthorityRequest.from_dict(data)
            if request.status != "pending":
                return {
                    "settled": False,
                    "request": request,
                    "reason": "not_pending",
                }
            if self.request_expired(request):
                data["status"] = "expired"
                self._write_json(path, data)
                self._audit_best_effort(
                    "authority_request_status",
                    {"request_id": request_id, "status": "expired"},
                )
                return {
                    "settled": False,
                    "request": AuthorityRequest.from_dict(data),
                    "reason": "expired",
                }

            result = None
            try:
                result = settle_callback(request) if callable(settle_callback) else None
                data["status"] = status
                self._write_json(path, data)
            except Exception:
                if callable(rollback_callback):
                    try:
                        rollback_callback(request, result)
                    except Exception as rollback_exc:
                        self.audit(
                            "authority_request_settlement_rollback_failed",
                            {
                                "request_id": request_id,
                                "status": status,
                                "error": str(rollback_exc),
                            },
                        )
                raise
            self._audit_best_effort("authority_request_status", {"request_id": request_id, "status": status})
            return {
                "settled": True,
                "request": AuthorityRequest.from_dict(data),
                "result": result,
            }

    def issue_one_shot(self, request: AuthorityRequest, *, expires_in_seconds: int = 86400) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _now_utc()
        record = {
            "token_id": token_id,
            "request_id": request.request_id,
            "principal_id": request.principal_id,
            "permission_id": request.permission_id,
            "resource_hash": self.resource_hash(request.resource),
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(seconds=max(60, int(expires_in_seconds or 86400)))).isoformat().replace("+00:00", "Z"),
            "consumed": False,
        }
        with self._lock:
            self._write_json(self._one_shot_dir / f"{token_id}.json", record)
            self.audit("authority_one_shot_issued", {"request_id": request.request_id, "token_id": token_id})
        return {"token": token, "token_id": token_id, "expires_at": record["expires_at"]}

    def revoke_one_shots(self, token_ids: list[str] | tuple[str, ...], *, reason: str = "rollback") -> int:
        revoked = 0
        with self._lock:
            for token_id in token_ids or ():
                normalized = str(token_id or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", normalized):
                    continue
                path = self._one_shot_dir / f"{normalized}.json"
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                revoked += 1
                self.audit(
                    "authority_one_shot_revoked",
                    {"token_id": normalized, "reason": reason},
                )
        return revoked

    def consume_one_shot(
        self,
        *,
        request_id: str,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
        token: str,
    ) -> bool:
        token_id = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        path = self._one_shot_dir / f"{token_id}.json"
        with self._lock:
            record = self._read_json(path)
            if not record:
                return False
            if record.get("consumed"):
                return False
            expires_at = _parse_ts(str(record.get("expires_at") or ""))
            if expires_at and expires_at <= _now_utc():
                return False
            if str(record.get("request_id") or "") != str(request_id or ""):
                return False
            if str(record.get("principal_id") or "") != str(principal_id or ""):
                return False
            if str(record.get("permission_id") or "") != str(permission_id or ""):
                return False
            if str(record.get("resource_hash") or "") != self.resource_hash(resource):
                return False
            record["consumed"] = True
            record["consumed_at"] = _now_ts()
            self._write_json(path, record)
            self._audit_best_effort(
                "authority_one_shot_consumed",
                {"request_id": request_id, "token_id": token_id},
            )
            return True

    def consume_one_shots_atomically(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Consume multiple one-shot approvals only if every token is still valid."""
        if not isinstance(items, list):
            return {"success": False, "reason": "invalid_items", "failed_index": 0}
        if not items:
            return {"success": True, "consumed_count": 0}

        with self._lock:
            seen_token_ids: set[str] = set()
            records: list[tuple[Path, str, dict[str, Any], dict[str, Any], int]] = []
            for index, item in enumerate(items):
                token = str(item.get("token") or "")
                if not token:
                    return {"success": False, "reason": "missing_token", "failed_index": index}
                token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
                if token_id in seen_token_ids:
                    return {"success": False, "reason": "duplicate_token", "failed_index": index}
                seen_token_ids.add(token_id)

                path = self._one_shot_dir / f"{token_id}.json"
                record = self._read_json(path) or {}
                raw_resource = item.get("resource")
                resource = raw_resource if isinstance(raw_resource, dict) else {}
                reason = self._one_shot_validation_error(
                    record,
                    request_id=str(item.get("request_id") or ""),
                    principal_id=str(item.get("principal_id") or ""),
                    permission_id=str(item.get("permission_id") or ""),
                    resource=resource,
                )
                if reason:
                    return {
                        "success": False,
                        "reason": reason,
                        "failed_index": index,
                        "request_id": str(item.get("request_id") or ""),
                        "permission_id": str(item.get("permission_id") or ""),
                    }
                records.append((path, token_id, record, item, index))

            now = _now_ts()
            written: list[tuple[Path, str, dict[str, Any]]] = []
            failed_index = 0
            try:
                for path, token_id, record, item, index in records:
                    failed_index = index
                    original_record = dict(record)
                    updated_record = dict(record)
                    updated_record["consumed"] = True
                    updated_record["consumed_at"] = now
                    self._write_json(path, updated_record)
                    written.append((path, token_id, original_record))
                    self._audit_best_effort(
                        "authority_one_shot_consumed",
                        {"request_id": str(item.get("request_id") or ""), "token_id": token_id},
                    )
            except Exception as exc:
                for restore_path, token_id, original_record in reversed(written):
                    try:
                        self._write_json(restore_path, original_record)
                        self._audit_best_effort(
                            "authority_one_shot_consume_rollback",
                            {"token_id": token_id, "reason": "consume_write_failed"},
                        )
                    except Exception as rollback_exc:
                        self._audit_best_effort(
                            "authority_one_shot_consume_rollback_failed",
                            {"token_id": token_id, "error": str(rollback_exc)},
                        )
                failed_item = records[failed_index][3]
                return {
                    "success": False,
                    "reason": "consume_write_failed",
                    "error": str(exc),
                    "failed_index": failed_index,
                    "request_id": str(failed_item.get("request_id") or ""),
                    "permission_id": str(failed_item.get("permission_id") or ""),
                }
            return {"success": True, "consumed_count": len(records)}

    def _one_shot_validation_error(
        self,
        record: dict[str, Any],
        *,
        request_id: str,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
    ) -> str:
        if not record:
            return "missing_token"
        if record.get("consumed"):
            return "token_already_consumed"
        expires_at = _parse_ts(str(record.get("expires_at") or ""))
        if expires_at and expires_at <= _now_utc():
            return "token_expired"
        if str(record.get("request_id") or "") != str(request_id or ""):
            return "request_mismatch"
        if str(record.get("principal_id") or "") != str(principal_id or ""):
            return "principal_mismatch"
        if str(record.get("permission_id") or "") != str(permission_id or ""):
            return "permission_mismatch"
        if str(record.get("resource_hash") or "") != self.resource_hash(resource):
            return "resource_mismatch"
        return ""

    def one_shot_matches_request(
        self,
        *,
        request_id: str,
        permission_id: str,
        token: str,
        principal_id: str | None = None,
        resource: dict[str, Any] | None = None,
        include_consumed: bool = False,
    ) -> bool:
        token_id = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        path = self._one_shot_dir / f"{token_id}.json"
        with self._lock:
            record = self._read_json(path)
            if not record:
                return False
            if record.get("consumed") and not include_consumed:
                return False
            expires_at = _parse_ts(str(record.get("expires_at") or ""))
            if expires_at and expires_at <= _now_utc():
                return False
            if str(record.get("request_id") or "") != str(request_id or ""):
                return False
            if str(record.get("permission_id") or "") != str(permission_id or ""):
                return False
            if principal_id and str(record.get("principal_id") or "") != str(principal_id or ""):
                return False
            if resource is not None and str(record.get("resource_hash") or "") != self.resource_hash(resource):
                return False
            return True

    def add_deny(
        self,
        *,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
        reason: str = "",
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "deny_id": "deny_" + secrets.token_urlsafe(12),
            "principal_id": principal_id,
            "permission_id": permission_id,
            "resource": self._safe_resource(resource),
            "reason": reason,
            "created_at": _now_ts(),
        }
        with self._lock:
            self._write_json(self._deny_dir / f"{_safe_filename(record['deny_id'])}.json", record)
            self.audit(
                "authority_deny_added",
                {
                    "deny_id": record["deny_id"],
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "resource_hash": self.resource_hash(resource),
                },
            )
        return record

    def remove_deny(self, deny_id: str, *, reason: str = "rollback") -> bool:
        normalized = str(deny_id or "").strip()
        if not normalized:
            return False
        path = self._deny_dir / f"{_safe_filename(normalized)}.json"
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            except OSError:
                return False
            self.audit("authority_deny_removed", {"deny_id": normalized, "reason": reason})
            return True

    def list_denies(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self._deny_dir.glob("*.json")):
            data = self._read_json(path)
            if data:
                records.append(data)
        return records

    def matching_deny(self, candidates: list[str], permission_id: str, resource: dict[str, Any]) -> dict[str, Any] | None:
        candidate_set = set(candidates)
        for deny in self.list_denies():
            if str(deny.get("permission_id") or "") != str(permission_id or ""):
                continue
            if str(deny.get("principal_id") or "") not in candidate_set:
                continue
            raw_pattern = deny.get("resource")
            pattern = raw_pattern if isinstance(raw_pattern, dict) else {}
            if self._resource_pattern_matches(pattern, resource):
                return deny
        return None

    @staticmethod
    def _resource_pattern_matches(pattern: dict[str, Any], resource: dict[str, Any]) -> bool:
        for key, expected in dict(pattern or {}).items():
            if expected in (None, "", [], {}):
                continue
            if key == "metadata":
                continue
            if resource.get(key) != expected:
                return False
        return True

    def audit(self, action: str, details: dict[str, Any] | None = None) -> None:
        record = {
            "timestamp": _now_ts(),
            "action": str(action or ""),
            "details": self._safe_resource(details or {}),
        }
        signed = self._signed(record)
        line = json.dumps(signed, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _audit_best_effort(self, action: str, details: dict[str, Any] | None = None) -> None:
        try:
            self.audit(action, details)
        except Exception:
            pass

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 200), 1000))
        try:
            lines = self._audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if self._verify(item):
                item.pop("_hmac_signature", None)
                item["verified"] = True
                events.append(item)
                continue
            events.append(
                {
                    "timestamp": _now_ts(),
                    "action": "authority_audit_tampered",
                    "details": {},
                    "verified": False,
                    "tampered": True,
                }
            )
        return events
