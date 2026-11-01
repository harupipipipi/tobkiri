"""
blocks/chat/setup.py - Chat component setup phase

Registers chat-related HTTP routes into the kernel's InterfaceRegistry
under the key ``io.http.route``.
"""

import sys
import os
import inspect
from functools import partial


def run(context):
    """Called by the kernel during the *setup* phase."""
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if pack_root not in sys.path:
        sys.path.insert(0, pack_root)

    interface_registry = context["interface_registry"]
    source_component = context.get("_source_component", "defaultspack:chat:chat")
    settings_owner = context.get("_settings_owner_port")

    def _lazy(module_path, func_name="run", *, settings_owner=None):
        """Return a lazy handler that imports the module on first call."""
        def handler(request_data, context):
            import importlib
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            if "settings_owner" in inspect.signature(fn).parameters:
                return fn(request_data, context, settings_owner=settings_owner)
            return fn(request_data, context)
        return handler

    owner_route = partial(_lazy, settings_owner=settings_owner)

    routes = [
        # --- Existing conversation routes ---
        ("POST", "/v1/chat/completions", _lazy("blocks.chat.send"), {}),
        ("POST", "/api/chat/conversations", _lazy("blocks.chat.create_conversation"), {}),
        ("GET", "/api/chat/conversations", _lazy("blocks.chat.list_conversations"), {}),
        ("GET", "/api/chat/conversations/{id}", _lazy("blocks.chat.get_conversation"), {"id": "conversation_id"}),
        ("POST", "/api/chat/search", _lazy("blocks.chat.search"), {}),
        ("POST", "/api/chat/handoffs", _lazy("blocks.conversation.handoff"), {}),
        ("POST", "/api/chat/steer", _lazy("blocks.conversation.steer"), {}),
        ("POST", "/api/chat/guidance", _lazy("blocks.conversation.guidance"), {}),
        ("PUT", "/api/chat/conversations/{id}", _lazy("blocks.chat.update_conversation"), {"id": "conversation_id"}),
        ("DELETE", "/api/chat/conversations/{id}", _lazy("blocks.chat.delete_conversation"), {"id": "conversation_id"}),
        ("POST", "/api/chat/conversations/{id}/messages", _lazy("blocks.chat.send"), {"id": "conversation_id"}),
        ("POST", "/api/chat/conversations/{id}/stream", _lazy("blocks.chat.stream"), {"id": "conversation_id"}),
        ("POST", "/api/chat/conversations/{id}/stop", _lazy("blocks.chat.stop"), {"id": "conversation_id"}),
        ("POST", "/api/chat/conversations/{id}/export", _lazy("blocks.chat.export_conversation"), {"id": "conversation_id"}),
        ("POST", "/api/chat/conversations/{id}/summarize", _lazy("blocks.chat.summarize_and_trim"), {"id": "conversation_id"}),
        ("POST", "/api/chat/conversations/{id}/auto-trim", _lazy("blocks.chat.auto_trim"), {"id": "conversation_id"}),
        ("GET", "/api/chat/conversations/{id}/tool-preferences", _lazy("blocks.chat.tool_preferences", "run_get"), {"id": "conversation_id"}),
        ("PUT", "/api/chat/conversations/{id}/tool-preferences", _lazy("blocks.chat.tool_preferences", "run_put"), {"id": "conversation_id"}),
        ("GET", "/api/conversations/{id}/tool-preferences", _lazy("blocks.chat.tool_preferences", "run_get"), {"id": "conversation_id"}),
        ("PUT", "/api/conversations/{id}/tool-preferences", _lazy("blocks.chat.tool_preferences", "run_put"), {"id": "conversation_id"}),
        ("GET", "/api/chat/conversations/{id}/run-results/{run_id}/browser-screenshots", _lazy("blocks.chat.browser_screenshots"), {"id": "conversation_id", "run_id": "run_id"}),
        ("GET", "/v1/conversations/{id}/run-results/{run_id}/browser-screenshots", _lazy("blocks.chat.browser_screenshots"), {"id": "conversation_id", "run_id": "run_id"}),
        # --- T11: Channel routes ---
        ("POST", "/api/chat/channels", _lazy("blocks.chat.channel.create"), {}),
        ("GET", "/api/chat/channels", _lazy("blocks.chat.channel.list"), {}),
        ("GET", "/api/chat/channels/{id}", _lazy("blocks.chat.channel.get"), {"id": "id"}),
        ("PUT", "/api/chat/channels/{id}", _lazy("blocks.chat.channel.update"), {"id": "id"}),
        ("DELETE", "/api/chat/channels/{id}", _lazy("blocks.chat.channel.delete"), {"id": "id"}),
        ("POST", "/api/chat/channels/{id}/join", _lazy("blocks.chat.channel.join"), {"id": "id"}),
        ("POST", "/api/chat/channels/{id}/leave", _lazy("blocks.chat.channel.leave"), {"id": "id"}),
        ("POST", "/api/chat/channels/{id}/messages", _lazy("blocks.chat.channel.send_message"), {"id": "id"}),
        ("GET", "/api/chat/channels/{id}/messages", _lazy("blocks.chat.channel.get_messages"), {"id": "id"}),
        ("POST", "/api/chat/channels/{id}/messages/{msg_id}/reply", _lazy("blocks.chat.channel.reply"), {"id": "id", "msg_id": "msg_id"}),
        # --- External chat integrations ---
        ("GET", "/api/integrations/secrets", _lazy("blocks.integrations.secrets"), {}),
        ("POST", "/api/integrations/secrets", _lazy("blocks.integrations.secrets"), {}),
        ("POST", "/api/integrations/slack/events", owner_route("blocks.integrations.slack"), {}),
        ("POST", "/api/integrations/line/webhook", owner_route("blocks.integrations.line"), {}),
        ("POST", "/api/integrations/discord/interactions", owner_route("blocks.integrations.discord"), {}),
        ("POST", "/api/integrations/discord/events", owner_route("blocks.integrations.discord"), {}),
        ("GET", "/api/external/tokens", _lazy("blocks.external.tokens"), {}),
        ("POST", "/api/external/tokens", _lazy("blocks.external.tokens"), {}),
        ("GET", "/api/external/sources", _lazy("blocks.external.sources"), {}),
        ("POST", "/api/external/sources", _lazy("blocks.external.sources"), {}),
        ("GET", "/api/external/templates", _lazy("blocks.external.templates"), {}),
        ("POST", "/api/external/templates", _lazy("blocks.external.templates"), {}),
        ("POST", "/api/webhooks/inbound/{webhook_id}", _lazy("blocks.webhooks.inbound"), {"webhook_id": "webhook_id"}),
        ("GET", "/api/webhooks/endpoints", _lazy("blocks.webhooks.endpoints"), {}),
        ("POST", "/api/webhooks/endpoints", _lazy("blocks.webhooks.endpoints"), {}),
        ("PUT", "/api/webhooks/endpoints/{webhook_id}", _lazy("blocks.webhooks.endpoints"), {"webhook_id": "webhook_id"}),
        ("DELETE", "/api/webhooks/endpoints/{webhook_id}", _lazy("blocks.webhooks.endpoints"), {"webhook_id": "webhook_id"}),
        ("POST", "/api/webhooks/endpoints/{webhook_id}/test", _lazy("blocks.webhooks.inbound"), {"webhook_id": "webhook_id"}),
        ("GET", "/api/webhooks/public-urls", _lazy("blocks.webhooks.public_url"), {}),
        ("POST", "/api/webhooks/public-urls", _lazy("blocks.webhooks.public_url"), {}),
        ("DELETE", "/api/webhooks/public-urls/{url_id}", _lazy("blocks.webhooks.public_url"), {"url_id": "url_id"}),
    ]

    for method, pattern, handler, path_inject in routes:
        interface_registry.register(
            "io.http.route",
            {
                "method": method,
                "pattern": pattern,
                "handler": handler,
                "path_inject": path_inject,
            },
            meta={"_source_component": source_component},
        )
