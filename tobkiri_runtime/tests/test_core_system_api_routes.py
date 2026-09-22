from __future__ import annotations

from pathlib import Path
def test_core_system_api_manifest_is_not_loaded_into_production_routes():
    from core_runtime.pack_api_server import PackAPIHandler

    assert not hasattr(PackAPIHandler, "load_api_routes")
    assert not hasattr(PackAPIHandler, "_api_route_exact")


def test_core_system_api_manifest_file_declares_expected_routes():
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "core_runtime"
        / "core_pack"
        / "core_system_api"
        / "ecosystem.json"
    )

    assert manifest_path.is_file()
    text = manifest_path.read_text(encoding="utf-8")
    assert '"path": "/api/packs"' in text
    assert '"path_pattern": "/api/packs/{pack_id}/status"' in text


def test_core_system_legacy_routes_use_typed_retirement_boundary():
    from core_runtime.pack_api_server import PackAPIHandler

    handler = object.__new__(PackAPIHandler)
    handler.path = "/api/packs"
    sent = []
    handler._send_response = lambda response, status=200: sent.append(
        (response, status)
    )

    PackAPIHandler.do_GET(handler)

    response, status = sent[0]
    assert status == 410
    assert response.data["state"] == "legacy_api_retired"
