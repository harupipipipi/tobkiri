from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.components.manifest import DomainComponent  # noqa: E402
from domain.components.registry import DomainComponentRegistry, build_domain_component_roots  # noqa: E402
from domain.frontend.registry import FrontendRegistry  # noqa: E402
from transport.registry import build_fallback_http_routes, component_http_route_specs  # noqa: E402


class _FakeServer:
    def __getattr__(self, name):
        if str(name).startswith("_handle_authority_"):
            return lambda *_args, **_kwargs: {"status": "ok"}
        raise AttributeError(name)

    def _invoke_fallback_block(self, *args, **kwargs):
        return {"status": "ok", "args": args, "kwargs": kwargs}

    def _handle_health(self, *_args, **_kwargs):
        return {"status": "ok"}

    def _handle_context_info(self, *_args, **_kwargs):
        return {"status": "ok"}

    def _handle_desktop_system_info(self, *_args, **_kwargs):
        return {"status": "ok"}

    def _handle_chat_redirect(self, *_args, **_kwargs):
        return {"status": "ok"}

    def _handle_static(self, *_args, **_kwargs):
        return {"status": "ok"}

    def _handle_static_file(self, *_args, **_kwargs):
        return {"status": "ok"}


def test_component_route_specs_include_manifest_backed_routes():
    route_pairs = {(spec.method, spec.pattern, spec.block_module) for spec in component_http_route_specs()}

    assert ("POST", "/api/integrations/line/webhook", "blocks.integrations.line") in route_pairs
    assert ("POST", "/api/integrations/discord/interactions", "blocks.integrations.discord") in route_pairs
    assert ("POST", "/api/integrations/slack/events", "blocks.integrations.slack") in route_pairs
    assert ("GET", "/api/ui/catalog", "blocks.ui.catalog") in route_pairs


def test_component_routes_do_not_join_the_host_route_table():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/integrations/line/webhook",
        "tobkiri.integration.line.v1",
        "defaultspack.integration.line.receive",
    )


def test_ui_catalog_exposes_component_route_and_surface_metadata():
    registry = DomainComponentRegistry(build_domain_component_roots(DEFAULTSPACK_ROOT))
    assert registry.get("ui_surfaces", "default_shell").id == "default_shell"
    assert registry.get("transports", "http").id == "http"

    with patch("domain.frontend.registry.AIClient") as mock_client:
        mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
        catalog = FrontendRegistry(DEFAULTSPACK_ROOT).build_catalog()

    route_pairs = {
        (route["method"], route["path"])
        for route in catalog["routes"]["manifest_backed"]
    }
    sidebar_ids = {item["id"] for item in catalog["sidebar"]["items"]}

    assert ("POST", "/api/integrations/line/webhook") in route_pairs
    assert ("GET", "/api/ui/catalog") in route_pairs
    assert "component-manifests" in sidebar_ids


def test_component_route_specs_reject_shared_root_source_pack_spoof(tmp_path):
    manifest_path = tmp_path / "shared" / "transports" / "pwn" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    component = DomainComponent(
        category="transports",
        component_id="pwn",
        manifest={
            "id": "pwn",
            "category": "transports",
            "kind": "transport",
            "version": "1",
            "status": "stable",
            "source_pack_id": "defaultspack",
            "routes": [
                {
                    "method": "GET",
                    "path": "/pwn-shared-spoof",
                    "block_module": "blocks.ui.catalog",
                }
            ],
        },
        manifest_path=manifest_path,
        source_pack_id="",
    )

    class _Registry:
        def list(self):
            return [component]

    with patch("transport.registry.get_domain_component_registry", return_value=_Registry()):
        assert component_http_route_specs() == []


def test_component_route_specs_reject_untrusted_block_modules(tmp_path):
    manifest_path = tmp_path / "evil" / "domain" / "transports" / "pwn" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    component = DomainComponent(
        category="transports",
        component_id="pwn",
        manifest={
            "id": "pwn",
            "category": "transports",
            "kind": "transport",
            "version": "1",
            "status": "stable",
            "source_pack_id": "evil_pack",
            "routes": [
                {
                    "method": "GET",
                    "path": "/pwn-autovalidator",
                    "block_module": "ecosystem.evil_pack.blocks.pwn",
                }
            ],
        },
        manifest_path=manifest_path,
        source_pack_id="evil_pack",
    )

    class _Registry:
        def list(self):
            return [component]

    with patch("transport.registry.get_domain_component_registry", return_value=_Registry()):
        assert component_http_route_specs() == []
        routes = build_fallback_http_routes(_FakeServer())

    assert not any(
        method == "GET" and "pwn-autovalidator" in compiled.pattern
        for method, compiled, *_rest in routes
    )
