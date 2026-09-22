"""Authenticated HTTP coverage for editing a non-active Profile composition."""
from __future__ import annotations

from tests.test_production_frontend_contract_http import (
    _authenticate,
    _request,
    production_server,
)


def test_composition_http_is_authenticated_cas_bound_and_preserves_execution(production_server):
    server, _session, _authority = production_server
    status, _, _ = _request(server, "GET", "/api/v4/profiles/catalog")
    assert status == 401
    cookie, csrf, origin = _authenticate(server)
    read_headers = {"Cookie": cookie}
    write_headers = {**read_headers, "Origin": origin, "X-Rumi-CSRF": csrf}

    def read(path):
        status, payload, _ = _request(server, "GET", path, headers=read_headers)
        assert status == 200, payload
        return payload["data"]

    initial = read("/api/v4/profiles")
    status, payload, _ = _request(server, "POST", "/api/v4/profiles/create", body={
        "profile_id": "editing", "display_name": "Editing", "source_profile_id": "defaults",
        "expected_store_generation": initial["generation"],
    }, headers=write_headers)
    assert status == 200, payload
    created = payload["data"]
    profile = created["changed_profile"]
    catalog = read("/api/v4/profiles/catalog")
    pack_ids = [row["pack_id"] for row in profile["profile"]["packs"] if row["role"] != "application"]
    pack_ids.remove("rumi_conversation_store_pack")
    body = {
        "profile_id": "editing", "display_name": "Editing",
        "expected_profile_revision": profile["profile_revision"],
        "expected_store_generation": created["generation"],
        "composition": {
            "pack_ids": pack_ids,
            "profile_catalog_digest": catalog["profile_catalog_digest"],
            "bundle_lock_digest": catalog["bundle_lock_digest"],
        },
    }
    status, _, _ = _request(server, "POST", "/api/v4/profiles/update", body=body, headers=read_headers)
    assert status == 403
    status, payload, _ = _request(server, "POST", "/api/v4/profiles/update", body=body, headers=write_headers)
    assert status == 200, payload
    updated = payload["data"]
    assert updated["changed_profile"]["parent_revision"] == profile["profile_revision"]
    for key in ("active_profile_id", "active_profile_revision", "active_profile_definition_revision"):
        assert updated[key] == initial[key]
    assert updated["changed_profile"]["profile"]["authority_references"] == []
    assert updated["changed_profile"]["profile"]["state"] == "needs_resolution"
    status, _, _ = _request(server, "POST", "/api/v4/profiles/update", body=body, headers=write_headers)
    assert status in {400, 409}
    assert read("/api/v4/profiles")["generation"] == updated["generation"]
