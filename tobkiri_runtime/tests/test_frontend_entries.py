"""Sealed Application display entries do not manufacture operation authority."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.global_contracts.capability_capture import capture_capability_binding_snapshot
from core_runtime.global_contracts.http_contract_dispatch import HTTPContractRouteError
from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
    load_frontend_contract_bindings,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from tests.test_application_frontend_contract_binding import (
    CONTEXT,
    _map_document,
    _write_application_map,
)
from tobkiri_protocol.canonical import strict_loads


def _document():
    document = _map_document("application.research")
    document["frontend"] = {
        "default_entry_id": "work",
        "entries": [
            {
                "entry_id": "work",
                "route": "/workbench",
                "match": "exact",
                "contribution_id": "research.work",
                "implementation": "research.workbench",
                "label": "Workbench",
            }
        ],
    }
    return document


def test_verified_non_chat_entry_uses_the_captured_application_and_no_new_grants(tmp_path):
    document = _document()
    path, manifest = _write_application_map(tmp_path, "application.research", document=document)
    binding = load_frontend_contract_bindings(path, manifest, **CONTEXT)[0]
    # This projection deliberately has no admitted operation target.
    session = SimpleNamespace(**CONTEXT, assert_current=lambda: None)
    snapshot = capture_capability_binding_snapshot(
        replace(binding, targets=()), session=session, catalog={}
    )
    result = DefaultspackHTTPPresentation().present_result(
        replace(binding, presentation="dynamic_pack_catalog"),
        {},
        session=session,
        routes={("POST", "/api/ui/capability/invoke"): binding},
        capability_snapshot=lambda *args, **kwargs: snapshot,
    )["dynamic_host"]
    (entry,) = result["contributions"]
    assert result["selected_entry_route"] == "/workbench"
    assert entry["route"] == "/workbench"
    assert entry["implementation"] == "research.workbench"
    assert entry["owner_pack_id"] == entry["build_identity"] == "application.research"
    assert entry["resolved_profile_id"] == "profile-a"
    assert "action_contract" not in entry and snapshot.targets == ()
    before = binding.frontend_entries
    document["frontend"]["entries"][0]["route"] = "/changed"
    assert binding.frontend_entries == before
    assert strict_loads(before)["entries"][0]["route"] == "/workbench"


def test_selected_profile_entry_is_projected_from_the_same_sealed_map(tmp_path):
    document = _document()
    document["frontend"]["entries"].append(
        {
            "entry_id": "review",
            "route": "/review",
            "match": "exact",
            "contribution_id": "research.review",
            "implementation": "research.review",
            "label": "Review",
        }
    )
    path, manifest = _write_application_map(
        tmp_path, "application.research", document=document
    )
    binding = load_frontend_contract_bindings(path, manifest, **CONTEXT)[0]
    session = SimpleNamespace(
        **CONTEXT, frontend_entry_id="review", assert_current=lambda: None
    )
    snapshot = capture_capability_binding_snapshot(
        replace(binding, targets=()), session=session, catalog={}
    )
    result = DefaultspackHTTPPresentation().present_result(
        replace(binding, presentation="dynamic_pack_catalog"),
        {},
        session=session,
        routes={("POST", "/api/ui/capability/invoke"): binding},
        capability_snapshot=lambda *args, **kwargs: snapshot,
    )["dynamic_host"]
    assert result["profile_id"] == CONTEXT["profile_id"]
    assert result["selected_entry_route"] == "/review"

    session.frontend_entry_id = "missing"
    with pytest.raises(RuntimeError, match="frontend entry is unavailable"):
        DefaultspackHTTPPresentation().present_result(
            replace(binding, presentation="dynamic_pack_catalog"),
            {},
            session=session,
            routes={("POST", "/api/ui/capability/invoke"): binding},
            capability_snapshot=lambda *args, **kwargs: snapshot,
        )


def test_frontend_map_change_invalidates_catalog_without_adding_capabilities(tmp_path):
    document = _document()
    path, manifest = _write_application_map(tmp_path, "application.research", document=document)
    first = load_frontend_contract_bindings(path, manifest, **CONTEXT)[0]
    document["frontend"]["entries"][0]["implementation"] = "research.dashboard"
    path, changed_manifest = _write_application_map(
        tmp_path, "application.research", document=document
    )
    with pytest.raises(HTTPContractRouteError):
        load_frontend_contract_bindings(path, manifest, **CONTEXT)
    second = load_frontend_contract_bindings(path, changed_manifest, **CONTEXT)[0]
    session = SimpleNamespace(**CONTEXT)
    snapshots = [
        capture_capability_binding_snapshot(
            replace(binding, targets=()), session=session, catalog={}
        )
        for binding in (first, second)
    ]
    assert snapshots[0].catalog_hash != snapshots[1].catalog_hash
    assert snapshots[0].targets == snapshots[1].targets == ()


@pytest.mark.parametrize(
    "route",
    [
        "//host",
        "https://host/chat",
        "/../chat",
        "/a//b",
        "/chat?x=1",
        "/chat#code",
        "/%63hat",
        "/chat/",
    ],
)
def test_invalid_frontend_path_is_rejected_even_in_a_digest_matched_map(tmp_path, route):
    document = _document()
    document["frontend"]["entries"][0]["route"] = route
    path, manifest = _write_application_map(tmp_path, "application.research", document=document)
    with pytest.raises(HTTPContractRouteError):
        load_frontend_contract_bindings(path, manifest, **CONTEXT)


@pytest.mark.parametrize("change", ["duplicate", "default", "extra", "overlap", "catchall"])
def test_ambiguous_or_incomplete_frontend_declaration_fails_closed(tmp_path, change):
    document = _document()
    frontend = document["frontend"]
    if change == "duplicate":
        frontend["entries"].append(dict(frontend["entries"][0]))
    elif change == "default":
        frontend["default_entry_id"] = "unknown"
    elif change == "extra":
        frontend["entries"][0]["approved"] = True
    elif change == "catchall":
        frontend["entries"][0].update(route="/", match="subpath")
    else:
        frontend["entries"][0]["match"] = "subpath"
        frontend["entries"].append(
            {
                **frontend["entries"][0],
                "entry_id": "other",
                "contribution_id": "research.other",
                "route": "/workbench/admin",
            }
        )
    path, manifest = _write_application_map(tmp_path, "application.research", document=document)
    with pytest.raises(HTTPContractRouteError):
        load_frontend_contract_bindings(path, manifest, **CONTEXT)
