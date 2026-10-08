"""Authenticated finite Host HTTP adapter for Profile Pack revision choices."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from .profile_definition_store_v4 import (
    ProfileDefinitionNotFound,
    ProfileDefinitionStoreConflict,
)
from .profile_pack_versions import profile_pack_versions, select_profile_pack_version

READ_PATH = "/api/v4/profiles/pack-versions"
SELECT_PATH = "/api/v4/profiles/select-pack-version"


def handle_profile_pack_versions(handler: Any, method: str, path: str) -> bool:
    """Use the same session, CSRF, and store fences as Named Profile metadata."""

    if (method, path) not in {("GET", READ_PATH), ("POST", SELECT_PATH)}:
        return False
    if not handler._check_auth(method, path):
        if method == "POST":
            handler._discard_request_body()
        handler._send_mapping_result({"error": "Unauthorized", "status_code": 401})
        return True
    try:
        handler._profile_registry_store()
        if method == "GET":
            query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
            if set(query) != {"profile_id"} or len(query["profile_id"]) != 1:
                raise ValueError("Profile selector is invalid")
            result = profile_pack_versions(query["profile_id"][0])
        else:
            body = handler._parse_object_body()
            if body is None:
                return True
            result = select_profile_pack_version(body)
        handler._send_mapping_result(result)
    except ProfileDefinitionNotFound:
        handler._send_mapping_result({"error": "Profile was not found", "status_code": 404})
    except ProfileDefinitionStoreConflict:
        handler._send_mapping_result({"error": "Pack revision selection is stale", "status_code": 409})
    except (OSError, RuntimeError, ValueError, TypeError):
        handler._send_mapping_result({"error": "Pack revision selection was denied", "status_code": 400})
    return True
