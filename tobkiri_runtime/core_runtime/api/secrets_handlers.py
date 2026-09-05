"""Secrets ハンドラ Mixin (W18-B: Grant API 追加)"""

from __future__ import annotations

import re

from ._helpers import _log_internal_error, _SAFE_ERROR_MSG

# secrets_store.py と同じ制約を早期にチェックする
_KEY_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")

# value の最大サイズ (1 MB)
_MAX_VALUE_BYTES = 1_048_576


def _get_secrets_grant_manager():
    """SecretsGrantManager を DI コンテナから取得"""
    from ..secrets_grant_manager import get_secrets_grant_manager as _get
    return _get()


class SecretsHandlersMixin:
    """Secrets 管理 (list / set / delete / grant / revoke) のハンドラ"""

    def _secrets_list(self) -> dict:
        try:
            from ..secrets_store import get_secrets_store
            store = get_secrets_store()
            keys = store.list_keys()
            return {
                "keys": [k.to_dict() for k in keys],
                "count": len(keys),
            }
        except Exception as e:
            _log_internal_error("secrets_list", e)
            return {"keys": [], "error": _SAFE_ERROR_MSG}

    def _secrets_set(self, body: dict) -> dict:
        key = body.get("key", "")
        value = body.get("value", "")
        if not key:
            return {"success": False, "error": "Missing 'key'"}
        if not _KEY_PATTERN.match(key):
            return {
                "success": False,
                "error": "Invalid key: must match ^[A-Z0-9_]{1,64}$",
            }
        if not isinstance(value, str):
            return {"success": False, "error": "'value' must be a string"}
        if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
            return {"success": False, "error": "Value too large (max 1 MB)"}

        try:
            from ..secrets_store import get_secrets_store
            store = get_secrets_store()
            result = store.set_secret(key, value)
            return result.to_dict()
        except Exception as e:
            _log_internal_error("secrets_set", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _secrets_delete(self, body: dict) -> dict:
        key = body.get("key", "")
        if not key:
            return {"success": False, "error": "Missing 'key'"}
        if not _KEY_PATTERN.match(key):
            return {
                "success": False,
                "error": "Invalid key: must match ^[A-Z0-9_]{1,64}$",
            }

        try:
            from ..secrets_store import get_secrets_store
            store = get_secrets_store()
            result = store.delete_secret(key)
            return result.to_dict()
        except Exception as e:
            _log_internal_error("secrets_delete", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    # ------------------------------------------------------------------ #
    # W18-B: Secret Grant API
    # ------------------------------------------------------------------ #

    def _secrets_grant(self, body: dict) -> dict:
        """POST /api/secrets/grant

        Pack に Secret へのアクセス権限を付与する。

        Body:
            pack_id: str — 対象 Pack ID
            secret_keys: List[str] — Grant する Secret キー名のリスト
        """
        pack_id = body.get("pack_id", "")
        secret_keys = body.get("secret_keys", [])

        if not pack_id:
            return {"success": False, "error": "Missing 'pack_id'"}
        if not isinstance(secret_keys, list) or not secret_keys:
            return {"success": False, "error": "'secret_keys' must be a non-empty list"}

        # キーのバリデーション
        for key in secret_keys:
            if not isinstance(key, str) or not _KEY_PATTERN.match(key):
                return {
                    "success": False,
                    "error": f"Invalid secret key '{key}': must match ^[A-Z0-9_]{{1,64}}$",
                }

        try:
            mgr = _get_secrets_grant_manager()
            grant = mgr.grant_secret_access(pack_id, secret_keys)
            return {
                "success": True,
                "pack_id": grant.pack_id,
                "granted_keys": grant.granted_keys,
            }
        except Exception as e:
            _log_internal_error("secrets_grant", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _secrets_revoke_grant(self, body: dict) -> dict:
        """POST /api/secrets/revoke-grant

        Pack から Secret アクセス権限を取り消す。

        Body:
            pack_id: str — 対象 Pack ID
            secret_keys: List[str] — Revoke する Secret キー名のリスト
        """
        pack_id = body.get("pack_id", "")
        secret_keys = body.get("secret_keys", [])

        if not pack_id:
            return {"success": False, "error": "Missing 'pack_id'"}
        if not isinstance(secret_keys, list) or not secret_keys:
            return {"success": False, "error": "'secret_keys' must be a non-empty list"}

        try:
            mgr = _get_secrets_grant_manager()
            result = mgr.revoke_secret_access(pack_id, secret_keys)
            return {"success": result, "pack_id": pack_id}
        except Exception as e:
            _log_internal_error("secrets_revoke_grant", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _secrets_grants_list(self) -> dict:
        """GET /api/secrets/grants

        全 Secret Grant 一覧を返す。
        """
        try:
            mgr = _get_secrets_grant_manager()
            grants = mgr.list_all_grants()
            return {
                "grants": {
                    pid: g.to_dict() for pid, g in grants.items()
                },
                "count": len(grants),
            }
        except Exception as e:
            _log_internal_error("secrets_grants_list", e)
            return {"grants": {}, "error": _SAFE_ERROR_MSG}

    # ------------------------------------------------------------------ #
    # W19-FIX: Missing handler methods
    # ------------------------------------------------------------------ #

    def _secrets_grants_get_pack(self, pack_id: str) -> dict:
        """GET /api/secrets/grants/{pack_id}

        指定 Pack の Secret Grant 情報を返す。
        """
        if not pack_id:
            return {"error": "Missing 'pack_id'"}

        try:
            mgr = _get_secrets_grant_manager()
            granted_keys = mgr.get_granted_keys(pack_id)
            return {
                "pack_id": pack_id,
                "granted_keys": granted_keys,
                "count": len(granted_keys),
            }
        except Exception as e:
            _log_internal_error("secrets_grants_get_pack", e)
            return {"error": _SAFE_ERROR_MSG}

    def _secrets_grants_delete_pack(self, pack_id: str) -> dict:
        """DELETE /api/secrets/grants/{pack_id}

        指定 Pack の全 Secret Grant を削除する。
        """
        if not pack_id:
            return {"error": "Missing 'pack_id'"}

        try:
            mgr = _get_secrets_grant_manager()
            result = mgr.delete_grant(pack_id)
            if result:
                return {"success": True, "pack_id": pack_id}
            else:
                return {"error": f"No grant found for pack_id '{pack_id}'"}
        except Exception as e:
            _log_internal_error("secrets_grants_delete_pack", e)
            return {"error": _SAFE_ERROR_MSG}

    def _secrets_grants_delete_key(self, pack_id: str, secret_key: str) -> dict:
        """DELETE /api/secrets/grants/{pack_id}/{secret_key}

        指定 Pack の特定キーの Grant を削除する。
        """
        if not pack_id:
            return {"error": "Missing 'pack_id'"}
        if not secret_key:
            return {"error": "Missing 'secret_key'"}

        try:
            mgr = _get_secrets_grant_manager()
            result = mgr.revoke_secret_access(pack_id, [secret_key])
            if result:
                return {"success": True, "pack_id": pack_id, "revoked_key": secret_key}
            else:
                return {"error": f"No grant found for pack_id '{pack_id}' or key '{secret_key}'"}
        except Exception as e:
            _log_internal_error("secrets_grants_delete_key", e)
            return {"error": _SAFE_ERROR_MSG}
