"""Secret grant routes plus v4 artifact integrity (no legacy Registry scan)."""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from ecosystem.defaultspack.domain.runtime_v4 import BundleIntegrityError, BundledCatalog
from tests.v4_batch_support import assert_legacy_registry_fails_closed


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


@pytest.fixture()
def api_server() -> Iterator[PackAPIServer]:
    """Run the current loopback boundary used by the retired-route contract."""
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="test-bootstrap"),
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    body: object = None,
):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    connection.request(
        method,
        path,
        body=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
    )
    response = connection.getresponse()
    payload = response.read().decode("utf-8")
    connection.close()
    return response.status, json.loads(payload)


def _assert_retired(
    server: PackAPIServer,
    method: str,
    path: str,
    body: object = None,
) -> None:
    status, data = _request(server, method, path, body)
    assert status == 410
    assert data["success"] is False
    assert data["data"] == {
        "api_version": "io.tobkiri.pack-api.v4",
        "state": "legacy_api_retired",
        "retired_route": path,
        "write_set": [],
    }
    assert data["error"] == (
        "Legacy API route is retired; use an exact Pack v4 operation"
    )


class TestSecretGrantRouting:
    def test_get_grants_list_authenticated(self, api_server):
        _assert_retired(api_server, "GET", "/api/secrets/grants")

    def test_get_grants_list_unauthenticated(self, api_server):
        _assert_retired(api_server, "GET", "/api/secrets/grants")

    def test_get_grant_existing_pack(self, api_server):
        _assert_retired(api_server, "GET", "/api/secrets/grants/test_pack")

    def test_get_grant_nonexistent_pack(self, api_server):
        _assert_retired(api_server, "GET", "/api/secrets/grants/missing")

    def test_post_grant_success(self, api_server):
        _assert_retired(
            api_server,
            "POST",
            "/api/secrets/grants/mypack",
            {"secret_keys": ["API_KEY", "DB_PASS"]},
        )

    @pytest.mark.parametrize("body", [{}, {"secret_keys": []}])
    def test_post_grant_invalid_body(self, api_server, body):
        _assert_retired(api_server, "POST", "/api/secrets/grants/mypack", body)

    def test_post_grant_unauthenticated(self, api_server):
        _assert_retired(
            api_server,
            "POST",
            "/api/secrets/grants/mypack",
            {"secret_keys": ["KEY1"]},
        )

    def test_delete_grant_existing(self, api_server):
        _assert_retired(api_server, "DELETE", "/api/secrets/grants/del_pack")

    def test_delete_grant_nonexistent(self, api_server):
        _assert_retired(api_server, "DELETE", "/api/secrets/grants/no_such_pack")

    def test_delete_grant_specific_key(self, api_server):
        _assert_retired(api_server, "DELETE", "/api/secrets/grants/key_pack/KEY1")

    def test_delete_grant_key_unauthenticated(self, api_server):
        _assert_retired(api_server, "DELETE", "/api/secrets/grants/key_pack/KEY1")

    def test_pack_id_traversal_is_rejected(self, api_server):
        _assert_retired(
            api_server,
            "POST",
            "/api/secrets/grants/..%2F..%2Fetc",
            {"secret_keys": ["KEY1"]},
        )

    def test_secret_key_format_is_validated(self, api_server):
        _assert_retired(
            api_server,
            "POST",
            "/api/secrets/grants/mypack",
            {"secret_keys": ["invalid-key!"]},
        )


def test_legacy_registry_json_scan_fails_closed() -> None:
    assert_legacy_registry_fails_closed()


def test_v4_catalog_has_hashed_artifacts() -> None:
    catalog = BundledCatalog.load(BUNDLE)
    assert all(item["pack"]["artifact_digest"].startswith("sha256:") for item in catalog.packs.values())


def test_v4_catalog_rejects_manifest_drift(tmp_path: Path) -> None:
    import shutil

    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    manifest = copied / "packs" / "defaultspack.pack.v4.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(BundleIntegrityError, match="digest changed"):
        BundledCatalog.load(copied)
