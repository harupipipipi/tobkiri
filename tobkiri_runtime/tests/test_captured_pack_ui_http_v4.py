"""Production HTTP serves signed UI only after exact Profile activation."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import http.client
from pathlib import Path

import pytest

from core_runtime.bootstrap.profile_capture import (
    capture_active_profile,
    runtime_user_data_root,
)
from core_runtime.profile_pack_versions import (
    profile_pack_versions,
    select_profile_pack_version,
)
from core_runtime.pack_api_server import PackAPIServer
from ecosystem.defaultspack.defaultspack.surface_contributions import (
    defaultspack_web_mounts,
)
from tests.test_captured_pack_ui_assets_v4 import (
    RUNTIME_ROOT,
    admit_ui_source,
    signed_ui_source,
)
from tests.test_normal_pack_production_update_v4 import _activate_revision
from tests.test_production_frontend_contract_http import (
    _authenticate,
    _captured_production_server,
)
from tobkiri_host.credential_store import host_credential_store_factory


def _get(
    server: PackAPIServer,
    path: str,
    cookie: str = "",
) -> tuple[int, bytes, dict[str, str]]:
    """Read static bytes from the real authenticated HTTP boundary."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=60)
    connection.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def _select(pack_id: str, digest: str, profile_id: str) -> None:
    view = profile_pack_versions(profile_id)
    selected = next(row for row in view["packs"] if row["pack_id"] == pack_id)
    result = select_profile_pack_version(
        {
            "profile_id": profile_id,
            "pack_id": pack_id,
            "artifact_digest": digest,
            "expected_selected_digest": selected["selected_digest"],
            "expected_profile_revision": view["profile_definition_revision"],
            "expected_store_generation": view["store_generation"],
            "expected_catalog_revision": view["catalog_revision"],
        }
    )
    assert result["activation_required"] is True


def test_signed_ui_activation_rebinds_real_http_mounts_without_source_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission alone cannot switch UI; activation refresh binds all aliases."""
    baseline = RUNTIME_ROOT / "ecosystem/defaultspack"
    with contextmanager(_captured_production_server)(
        tmp_path,
        monkeypatch,
        credential_store_factory=host_credential_store_factory,
    ) as (
        server,
        old_session,
        _authority,
    ):
        server.stop()
        server._web_mounts = defaultspack_web_mounts(baseline)
        server.start()
        cookie, _csrf, _origin = _authenticate(server)
        status, before, headers = _get(server, "/static/shell-app.js", cookie)
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert before != b"console.log('signed-selected-ui');"
        active = capture_active_profile()
        protected = {
            name: deepcopy(active.resolved.lock[name])
            for name in ("base", "shell", "application")
        }
        profile_id = str(active.resolved.profile["profile_id"])
        source, public = signed_ui_source(tmp_path / "ui-package", baseline=baseline)
        cas_root, digest = admit_ui_source(source, public, runtime_user_data_root())
        assert cas_root != baseline and cas_root != source
        assert _get(server, "/static/shell-app.js", cookie)[1] == before
        _select("defaultspack", digest, profile_id)
        _activate_revision(runtime_user_data_root(), profile_id)
        updated = capture_active_profile()
        for name, identity in protected.items():
            assert updated.resolved.lock[name] == identity
        server._refresh_runtime_capture()
        fresh_cookie, _csrf, _origin = _authenticate(server)
        assert _get(server, "/static/shell-app.js")[0] == 401
        assert _get(server, "/static/shell-app.js", cookie)[0] == 401
        for path in ("/p", "/chat", "/desktops", "/approval", "/p/thread/one"):
            status, body, headers = _get(server, path, fresh_cookie)
            assert status == 200 and b"signed-selected-ui" in body
            assert headers["Cache-Control"] == "no-store"
        assert (
            _get(server, "/static/shell-app.js", fresh_cookie)[1]
            == b"console.log('signed-selected-ui');"
        )
        assert (
            _get(server, "/static/shell-app.css", fresh_cookie)[1]
            == b"body { color: green; }"
        )
        assert (
            _get(server, "/static/fonts/local.woff2", fresh_cookie)[1]
            == b"signed-local-font"
        )
        for path in ("/static/missing.js", "/static/thread", "/static/pack.v4.json"):
            assert _get(server, path, fresh_cookie)[0] == 404
        for path in ("/p/%2e%2e/shell.html", "/p/%252e%252e/shell.html"):
            assert _get(server, path, fresh_cookie)[0] == 403
        with pytest.raises(RuntimeError):
            old_session.pack_ui_assets_for("defaultspack", "shell.html")
        # A file race after the existing activation/auth check is still fenced
        # by the captured byte digest rather than by a second catalog scan.
        from core_runtime import captured_pack_ui_assets_v4 as assets_module

        original = assets_module._read_regular_from_root_descriptor

        def changed_read(
            descriptor: int,
            relative: str,
            **limits: int,
        ) -> tuple[bytes, int]:
            body, mode = original(descriptor, relative, **limits)
            if relative == "ui/shell-app.js":
                return body + b"modified", mode
            return body, mode

        monkeypatch.setattr(
            assets_module, "_read_regular_from_root_descriptor", changed_read
        )
        status, body, _headers = _get(server, "/static/shell-app.js", fresh_cookie)
        assert status == 403 and before not in body


def test_external_pack_without_ui_cannot_recover_via_legacy_source_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted non-UI successor cannot inherit the old source HTML."""
    baseline = RUNTIME_ROOT / "ecosystem/defaultspack"
    with contextmanager(_captured_production_server)(
        tmp_path,
        monkeypatch,
        credential_store_factory=host_credential_store_factory,
    ) as (
        server,
        _session,
        _authority,
    ):
        server.stop()
        server._web_mounts = defaultspack_web_mounts(baseline)
        server.start()
        active = capture_active_profile()
        profile_id = str(active.resolved.profile["profile_id"])
        source, public = signed_ui_source(
            tmp_path / "ui-package", baseline=baseline, with_ui=False
        )
        _cas_root, digest = admit_ui_source(source, public, runtime_user_data_root())
        _select("defaultspack", digest, profile_id)
        _activate_revision(runtime_user_data_root(), profile_id)
        with pytest.raises(RuntimeError, match="UI asset inventory is incomplete"):
            server._refresh_runtime_capture()
