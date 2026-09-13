from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.components.registry import DomainComponentRegistry, build_domain_component_roots  # noqa: E402


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


def test_component_manifest_registry_has_no_diagnostics():
    registry = DomainComponentRegistry(build_domain_component_roots(DEFAULTSPACK_ROOT))

    assert registry.diagnostics() == []


def test_endpoint_and_audience_defaults_do_not_move_back_to_central_registries():
    endpoint_source = (DEFAULTSPACK_ROOT / "domain" / "webhook" / "endpoint_store.py").read_text(encoding="utf-8")
    audience_source = (DEFAULTSPACK_ROOT / "domain" / "external" / "audience_policy_registry.py").read_text(encoding="utf-8")

    for forbidden in ["line-main", "discord-main", "slack-main", "line.default", "discord.default", "slack.default"]:
        assert forbidden not in endpoint_source
    for forbidden in ["line.production", "discord.production", "slack.production", "DEFAULT_AUDIENCE_POLICIES"]:
        assert forbidden not in audience_source


def test_provider_layer_does_not_import_tool_registry_or_policy():
    providers_dir = DEFAULTSPACK_ROOT / "domain" / "ai_client" / "providers"
    forbidden = ("domain.tool", "tool_policy", "ToolRegistry", "ToolExecutor")

    for path in providers_dir.glob("*.py"):
        source = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith(("import ", "from "))
        )
        for token in forbidden:
            assert token not in source, f"{path} imports provider-tool coupling token {token}"


def test_prompt_layer_does_not_import_provider_or_tool_internals():
    prompt_dir = DEFAULTSPACK_ROOT / "domain" / "prompt"
    forbidden = ("domain.tool", "domain.ai_client", "ToolRegistry", "ToolExecutor")

    for path in prompt_dir.glob("*.py"):
        source = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith(("import ", "from "))
        )
        for token in forbidden:
            assert token not in source, f"{path} imports prompt coupling token {token}"


def test_legacy_ids_and_routes_are_physically_absent(tmp_path):
    del tmp_path
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/integrations/line/webhook",
        "tobkiri.integration.line.v1",
        "defaultspack.integration.line.receive",
    )


def test_compatibility_shims_import_successfully():
    from blocks.integrations import discord as discord_block  # noqa: E402
    from blocks.integrations import line as line_block  # noqa: E402
    from blocks.integrations import slack as slack_block  # noqa: E402
    from domain.gateway.channels.line import LineChannel  # noqa: E402
    from domain.webhook.url_providers.cloudflare_quick_tunnel import CloudflareQuickTunnelProvider  # noqa: E402
    from domain.webhook.url_providers.static import StaticWebhookUrlProvider  # noqa: E402

    assert callable(line_block.run)
    assert callable(discord_block.run)
    assert callable(slack_block.run)
    assert LineChannel.channel == "line"
    assert CloudflareQuickTunnelProvider.provider_id == "cloudflare_quick_tunnel"
    assert StaticWebhookUrlProvider.provider_id == "static"
