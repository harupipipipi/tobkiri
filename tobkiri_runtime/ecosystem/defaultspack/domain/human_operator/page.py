from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from domain.ai_client.provider_trace import redact_sensitive_value, sanitize_trace_value
from domain.chat.ir_legacy_adapter import ir_to_legacy_standard_messages, stored_messages_to_ir
from domain.chat.run_request import (
    _chat_references,
    _conversation_system_prompt,
    _conversation_with_active_profile_prompt,
    _format_chat_references_for_prompt,
    _load_active_startup_profile,
)
from domain.chat.store import ChatStore
from domain.prompt.manager import get_manager
from domain.prompt.studio_client import compact_prompt_via_owner

from .session_store import session_route_path


def append_manual_message(
    conversation_id: str,
    session_id: str,
    *,
    role: str,
    raw_text: str,
    content_format: str,
    operator_id: str,
    operator_marker: str,
    reason: str,
    command: str,
) -> dict[str, Any]:
    normalized_role = "assistant" if str(role or "").strip().lower() == "assistant" else "user"
    content = parse_manual_message_content(raw_text, content_format)
    if not content:
        raise ValueError("message text is required")
    timestamp_ms = int(time.time() * 1000)
    metadata = {
        "source": "human_operator",
        "human_operator": {
            "source": "human_operator",
            "conversation_id": str(conversation_id or ""),
            "session_id": str(session_id or ""),
            "operator_id": str(operator_id or "").strip(),
            "operator_marker": str(operator_marker or "").strip() or "local_human_operator",
            "inserted_role": normalized_role,
            "timestamp_ms": timestamp_ms,
            "reason": str(reason or "").strip(),
            "command": str(command or "").strip(),
        }
    }
    message = ChatStore().add_message(
        conversation_id,
        {
            "role": normalized_role,
            "content": content,
            "metadata": metadata,
        },
    )
    if message is None:
        raise RuntimeError("failed to append human-operator message")
    return message


def parse_manual_message_content(raw_text: str, content_format: str) -> list[Any]:
    text = str(raw_text or "")
    if not text.strip():
        return []
    if str(content_format or "").strip().lower() != "json":
        return [{"type": "text", "text": text}]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON content: {}".format(exc)) from exc
    if isinstance(parsed, str):
        return [{"type": "text", "text": parsed}]
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return parsed
    raise ValueError("JSON content must be a string, object, or array")


def render_session_page(
    conversation_id: str,
    session_id: str,
    session: dict[str, Any],
    *,
    view: str,
    prompt_view: str,
    flash: str = "",
) -> str:
    store = ChatStore()
    conversation = store.get_conversation(conversation_id) or {}
    live_context = _build_live_context(store, conversation_id, conversation)
    launch_snapshot = redact_sensitive_value(session.get("launch_snapshot") if isinstance(session.get("launch_snapshot"), dict) else {})
    launch_messages = launch_snapshot.get("messages") if isinstance(launch_snapshot, dict) else []
    launch_messages = launch_messages if isinstance(launch_messages, list) else []
    live_messages = live_context.get("messages") if isinstance(live_context.get("messages"), list) else []
    launch_system_prompt = str(launch_snapshot.get("system_prompt") or "")
    live_system_prompt = str(live_context.get("system_prompt") or "")
    launch_prompt_text = launch_system_prompt or _text_from_messages(launch_messages, system_only=True)
    live_prompt_text = live_system_prompt or _text_from_messages(live_messages, system_only=True)
    selected_prompt_text = launch_prompt_text if prompt_view == "launch" else live_prompt_text
    prompt_label = {
        "original": "Original",
        "rough_ja": "Rough JA",
        "compact": "Compact",
        "launch": "Launch Prompt",
        "live": "Live Prompt",
    }.get(prompt_view, "Original")
    prompt_render = _prompt_variant(selected_prompt_text, prompt_view)
    page_title = str(session.get("title") or conversation.get("title") or "Human Operator Canvas")
    csrf_token = str(session.get("csrf_token") or "").strip()
    flash_label = {
        "assistant_added": "AI output appended.",
        "user_added": "User input appended.",
    }.get(flash, "")
    messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
    session_meta = {
        "session_id": session_id,
        "conversation_id": conversation_id,
        "created_at": session.get("created_at"),
        "message_count": len(messages),
        "model": launch_snapshot.get("model") if isinstance(launch_snapshot, dict) else "",
        "command": session.get("command"),
        "note": session.get("note"),
    }

    if view == "json":
        body = "".join(
            [
                _section(
                    "Session Meta",
                    _json_block(session_meta),
                ),
                _section(
                    "Launch Snapshot",
                    _json_block(launch_snapshot),
                ),
                _section(
                    "Live Context",
                    _json_block(redact_sensitive_value(live_context)),
                ),
            ]
        )
    else:
        body = "".join(
            [
                _section(
                    "Prompt View",
                    _info_card(prompt_label, prompt_render),
                ),
                _section(
                    "Live Conversation",
                    _render_messages(messages),
                ),
                _section(
                    "Launch Snapshot",
                    _render_readable_snapshot(launch_snapshot, launch_messages),
                ),
                _section(
                    "Current AI Context",
                    _render_readable_live_context(live_context, live_messages),
                ),
            ]
        )

    post_message_script = ""
    if flash:
        post_message_script = """
<script>
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage(
        { type: "rumi_human_operator_sync", conversation_id: %s, session_id: %s },
        window.location.origin,
      );
    }
  } catch (_error) {
    // Ignore parent sync issues and keep the local canvas usable.
  }
</script>
""" % (json.dumps(conversation_id, ensure_ascii=False), json.dumps(session_id, ensure_ascii=False))

    return """<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f3efe6;
        --panel: rgba(255, 252, 246, 0.96);
        --ink: #1f1c17;
        --muted: #6d655a;
        --line: rgba(63, 49, 33, 0.18);
        --accent: #b55b3c;
        --accent-2: #0f766e;
        --card: #fffdf8;
        --shadow: 0 18px 50px rgba(42, 30, 18, 0.12);
        font-family: "IBM Plex Sans", "Noto Sans JP", sans-serif;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        background:
          radial-gradient(circle at top left, rgba(181, 91, 60, 0.18), transparent 28rem),
          radial-gradient(circle at bottom right, rgba(15, 118, 110, 0.18), transparent 24rem),
          var(--bg);
        color: var(--ink);
      }}
      a {{ color: var(--accent); text-decoration: none; }}
      a:hover {{ text-decoration: underline; }}
      .page {{
        width: min(1200px, calc(100vw - 24px));
        margin: 12px auto;
        padding: 16px;
        border: 1px solid var(--line);
        border-radius: 24px;
        background: var(--panel);
        box-shadow: var(--shadow);
        backdrop-filter: blur(10px);
      }}
      .header {{
        display: grid;
        gap: 12px;
        padding-bottom: 16px;
        border-bottom: 1px solid var(--line);
      }}
      .eyebrow {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        width: fit-content;
        padding: 6px 10px;
        border-radius: 999px;
        background: rgba(181, 91, 60, 0.1);
        color: var(--accent);
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }}
      .title {{
        margin: 0;
        font-size: clamp(24px, 4vw, 42px);
        line-height: 1.06;
      }}
      .lede {{
        margin: 0;
        color: var(--muted);
        line-height: 1.6;
        max-width: 72ch;
      }}
      .flash {{
        margin-top: 4px;
        padding: 10px 12px;
        border: 1px solid rgba(15, 118, 110, 0.24);
        border-radius: 14px;
        background: rgba(15, 118, 110, 0.08);
        color: var(--accent-2);
        font-weight: 600;
      }}
      .toolbar {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 6px;
      }}
      .toolbar a {{
        padding: 8px 10px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.68);
        color: var(--ink);
        font-size: 13px;
        font-weight: 600;
      }}
      .toolbar a[data-active="true"] {{
        border-color: rgba(181, 91, 60, 0.35);
        background: rgba(181, 91, 60, 0.12);
        color: var(--accent);
      }}
      .grid {{
        display: grid;
        gap: 16px;
        margin-top: 16px;
      }}
      .composer-grid {{
        display: grid;
        gap: 16px;
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .card {{
        border: 1px solid var(--line);
        border-radius: 20px;
        background: var(--card);
        box-shadow: 0 10px 26px rgba(42, 30, 18, 0.08);
      }}
      .card-head {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 14px 16px 10px;
      }}
      .card-head h2, .card-head h3 {{
        margin: 0;
        font-size: 16px;
      }}
      .meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        padding: 0 16px 14px;
        color: var(--muted);
        font-size: 12px;
      }}
      .chip {{
        padding: 6px 10px;
        border-radius: 999px;
        background: rgba(17, 24, 39, 0.06);
      }}
      .card-body {{
        padding: 0 16px 16px;
      }}
      form {{
        display: grid;
        gap: 10px;
      }}
      textarea, select {{
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 16px;
        background: #fffefb;
        color: var(--ink);
        font: inherit;
      }}
      textarea {{
        min-height: 180px;
        resize: vertical;
        padding: 14px;
        line-height: 1.55;
      }}
      select {{
        padding: 10px 12px;
      }}
      button {{
        width: fit-content;
        padding: 11px 16px;
        border: 0;
        border-radius: 999px;
        background: linear-gradient(135deg, var(--accent), #cf7e45);
        color: white;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
      }}
      .secondary {{
        background: linear-gradient(135deg, var(--accent-2), #1f9c94);
      }}
      .section {{
        border-top: 1px solid var(--line);
        padding-top: 16px;
      }}
      .section:first-of-type {{
        border-top: 0;
        padding-top: 0;
      }}
      .section h2 {{
        margin: 0 0 12px;
        font-size: 18px;
      }}
      .info-card {{
        padding: 14px;
        border: 1px solid var(--line);
        border-radius: 16px;
        background: rgba(255, 255, 255, 0.72);
      }}
      .label {{
        display: inline-block;
        margin-bottom: 8px;
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--muted);
      }}
      .message-list {{
        display: grid;
        gap: 12px;
      }}
      .message {{
        padding: 14px;
        border: 1px solid var(--line);
        border-radius: 16px;
        background: rgba(255, 255, 255, 0.7);
      }}
      .message-role {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        color: var(--muted);
      }}
      .message-role[data-role="assistant"] {{ color: var(--accent); }}
      .message-role[data-role="tool"] {{ color: var(--accent-2); }}
      pre {{
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
        font: 13px/1.65 "IBM Plex Mono", "SFMono-Regular", monospace;
      }}
      .columns {{
        display: grid;
        gap: 16px;
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .muted {{
        color: var(--muted);
      }}
      @media (max-width: 900px) {{
        .composer-grid, .columns {{
          grid-template-columns: 1fr;
        }}
        .page {{
          width: calc(100vw - 10px);
          margin: 5px;
          padding: 12px;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="page">
      <header class="header">
        <div class="eyebrow">Human Operator Canvas</div>
        <h1 class="title">{title}</h1>
        <p class="lede">
          AI context was captured when <code>/start</code> ran. From here, one human can play both sides:
          write the next user input, write the AI output, and inspect the prompt/context that the model would see.
        </p>
        {flash_html}
        <div class="toolbar">
          {toolbar}
        </div>
      </header>

      <div class="grid">
        <section class="card">
          <div class="card-head">
            <h2>Session Controls</h2>
          </div>
          <div class="meta">
            <span class="chip">session: {session_id}</span>
            <span class="chip">conversation: {conversation_id}</span>
            <span class="chip">messages: {message_count}</span>
            <span class="chip">model: {model}</span>
            <span class="chip">command: {command}</span>
          </div>
          <div class="card-body composer-grid">
            {assistant_form}
            {user_form}
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2>Context And Transcript</h2>
          </div>
          <div class="card-body">
            {body}
          </div>
        </section>
      </div>
    </div>
    {post_message_script}
  </body>
</html>
""".format(
        title=html.escape(page_title),
        flash_html=(
            '<div class="flash">{}</div>'.format(html.escape(flash_label))
            if flash_label
            else ""
        ),
        toolbar=_toolbar(conversation_id, session_id, view=view, prompt_view=prompt_view),
        session_id=html.escape(session_id),
        conversation_id=html.escape(conversation_id),
        message_count=len(messages),
        model=html.escape(str(session_meta.get("model") or "")),
        command=html.escape(str(session_meta.get("command") or "")),
        assistant_form=_message_form(
            conversation_id,
            session_id,
            role="assistant",
            title="Add AI Output",
            subtitle="Type the assistant reply you want to inject into the conversation.",
            button_label="Append Assistant",
            button_class="",
            view=view,
            prompt_view=prompt_view,
            csrf_token=csrf_token,
        ),
        user_form=_message_form(
            conversation_id,
            session_id,
            role="user",
            title="Add User Input",
            subtitle="Type the next user message you want the conversation to contain.",
            button_label="Append User",
            button_class=" secondary",
            view=view,
            prompt_view=prompt_view,
            csrf_token=csrf_token,
        ),
        body=body,
        post_message_script=post_message_script,
    )


def _toolbar(conversation_id: str, session_id: str, *, view: str, prompt_view: str) -> str:
    items = [
        ("readable", "Readable", view, prompt_view),
        ("json", "JSON", view, prompt_view),
        ("original", "Original Prompt", view, "original"),
        ("rough_ja", "Rough JA", view, "rough_ja"),
        ("compact", "Compact Prompt", view, "compact"),
        ("launch", "Launch Prompt", view, "launch"),
        ("live", "Live Prompt", view, "live"),
    ]
    output: list[str] = []
    for value, label, current_view, current_prompt_view in items:
        if value in {"readable", "json"}:
            target_view = value
            target_prompt_view = prompt_view
            active = target_view == view
        else:
            target_view = view
            target_prompt_view = value
            active = target_prompt_view == prompt_view
        output.append(
            '<a href="{href}" data-active="{active}">{label}</a>'.format(
                href=html.escape(
                    session_route_path(
                        conversation_id,
                        session_id,
                        view=target_view,
                        prompt_view=target_prompt_view,
                    )
                ),
                active="true" if active else "false",
                label=html.escape(label),
            )
        )
    return "".join(output)


def _message_form(
    conversation_id: str,
    session_id: str,
    *,
    role: str,
    title: str,
    subtitle: str,
    button_label: str,
    button_class: str,
    view: str,
    prompt_view: str,
    csrf_token: str,
) -> str:
    action = (
        "/api/human-operator/conversations/"
        + conversation_id
        + "/sessions/"
        + session_id
        + "/messages"
    )
    placeholder = (
        "Write assistant output here.\n\nUse JSON mode only when you want to inject raw content blocks."
        if role == "assistant"
        else "Write the next user message here.\n\nUse JSON mode only when you want to inject raw content blocks."
    )
    return """
<div class="info-card">
  <div class="label">{role}</div>
  <div class="card-head" style="padding:0 0 10px;">
    <h3>{title}</h3>
  </div>
  <p class="muted" style="margin:0 0 12px;">{subtitle}</p>
  <form method="post" action="{action}">
    <input type="hidden" name="role" value="{role_value}">
    <input type="hidden" name="view" value="{view}">
    <input type="hidden" name="prompt_view" value="{prompt_view}">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <label class="label" for="{role}-format">Content Format</label>
    <select id="{role}-format" name="content_format">
      <option value="text">Plain Text</option>
      <option value="json">JSON Blocks</option>
    </select>
    <label class="label" for="{role}-text">Message</label>
    <textarea id="{role}-text" name="text" placeholder="{placeholder}"></textarea>
    <button class="{button_class}" type="submit">{button_label}</button>
  </form>
</div>
""".format(
        role=html.escape(role),
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        action=html.escape(action),
        role_value=html.escape(role),
        view=html.escape(view),
        prompt_view=html.escape(prompt_view),
        csrf_token=html.escape(csrf_token),
        placeholder=html.escape(placeholder),
        button_class=button_class.strip(),
        button_label=html.escape(button_label),
    )


def _build_live_context(store: ChatStore, conversation_id: str, conversation: dict[str, Any]) -> dict[str, Any]:
    current_node_id = str(conversation.get("current_node_id") or "").strip()
    if current_node_id:
        message_chain = store.get_message_chain(conversation_id, current_node_id)
    else:
        message_chain = list(conversation.get("messages") or [])
    active_profile = _load_active_startup_profile()
    effective_conversation = _conversation_with_active_profile_prompt(conversation, active_profile)
    manager = get_manager()
    system_prompt = _conversation_system_prompt(effective_conversation, manager)
    chat_ir = stored_messages_to_ir(conversation_id, message_chain)
    standard_messages = ir_to_legacy_standard_messages(chat_ir)
    if system_prompt and (not standard_messages or standard_messages[0].get("role") != "system"):
        standard_messages.insert(0, {"role": "system", "content": system_prompt})
    chat_references = _chat_references(store, conversation_id, None)
    reference_prompt = _format_chat_references_for_prompt(chat_references)
    if reference_prompt:
        insert_at = 1 if standard_messages and standard_messages[0].get("role") == "system" else 0
        standard_messages.insert(insert_at, {"role": "system", "content": reference_prompt})
    return redact_sensitive_value(
        {
            "system_prompt": system_prompt,
            "messages": standard_messages,
            "chat_references": chat_references,
            "active_profile_id": active_profile.get("profile_id") if isinstance(active_profile, dict) else "",
            "conversation_model": conversation.get("model"),
            "conversation_title": conversation.get("title"),
        }
    )


def _section(title: str, content: str) -> str:
    return """
<section class="section">
  <h2>{title}</h2>
  {content}
</section>
""".format(title=html.escape(title), content=content)


def _info_card(label: str, content: str) -> str:
    return """
<div class="info-card">
  <div class="label">{label}</div>
  {content}
</div>
""".format(label=html.escape(label), content=content)


def _json_block(value: Any) -> str:
    return "<pre>{}</pre>".format(
        html.escape(json.dumps(sanitize_trace_value(value), ensure_ascii=False, indent=2))
    )


def _render_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return '<div class="info-card"><p class="muted" style="margin:0;">No messages yet.</p></div>'
    rows: list[str] = ['<div class="message-list">']
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        created_at = _format_ms(message.get("created_at"))
        rows.append(
            """
<article class="message">
  <div class="message-role" data-role="{role}">{role} <span class="muted">{created_at}</span></div>
  {content}
</article>
""".format(
                role=html.escape(role),
                created_at=html.escape(created_at),
                content=_render_content_blocks(message.get("content")),
            )
        )
    rows.append("</div>")
    return "".join(rows)


def _render_readable_snapshot(snapshot: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    context = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else {}
    tool_names = snapshot.get("tool_names") if isinstance(snapshot.get("tool_names"), list) else []
    prompt_text = str(snapshot.get("system_prompt") or "")
    planning = context.get("provider_planning") if isinstance(context.get("provider_planning"), dict) else {}
    cards = [
        _info_card("Launch Prompt", "<pre>{}</pre>".format(html.escape(prompt_text or "(none)"))),
        _info_card("Tool Names", "<pre>{}</pre>".format(html.escape("\n".join(str(item) for item in tool_names) or "(none)"))),
        _info_card("Launch Messages", _render_message_preview(messages)),
    ]
    if planning:
        cards.append(_info_card("Provider Planning", _json_block(planning)))
    if context:
        cards.append(_info_card("Context Summary", _json_block(_compact_context_summary(context))))
    return '<div class="columns">{}</div>'.format("".join(cards))


def _render_readable_live_context(live_context: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    system_prompt = str(live_context.get("system_prompt") or "")
    references = live_context.get("chat_references") if isinstance(live_context.get("chat_references"), dict) else {}
    return '<div class="columns">{}{}</div>'.format(
        _info_card("System Prompt", "<pre>{}</pre>".format(html.escape(system_prompt or "(none)"))),
        _info_card("Messages To Model", _render_message_preview(messages)),
    ) + _info_card("Chat References", _json_block(references))


def _render_message_preview(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "<pre>(none)</pre>"
    parts = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        parts.append("[{}]\n{}".format(role, _content_to_text(message.get("content"))))
    return "<pre>{}</pre>".format(html.escape("\n\n".join(parts)))


def _render_content_blocks(content: Any) -> str:
    if isinstance(content, str):
        return "<pre>{}</pre>".format(html.escape(content))
    if not isinstance(content, list):
        return "<pre>{}</pre>".format(html.escape(json.dumps(sanitize_trace_value(content), ensure_ascii=False, indent=2)))
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
            continue
        parts.append(json.dumps(sanitize_trace_value(block), ensure_ascii=False, indent=2))
    return "<pre>{}</pre>".format(html.escape("\n\n".join(parts)))


def _compact_context_summary(context: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "conversation_id",
        "conversation_workspace_dir",
        "history_json_path",
        "model",
        "chat_params",
        "provider_capabilities",
        "provider_planning",
        "active_startup_profile_id",
        "profile_graph_selection",
        "runtime_profile_key",
        "capability_graph",
    }
    return {key: context.get(key) for key in keep if key in context}


def _prompt_variant(prompt_text: str, prompt_view: str) -> str:
    text = str(prompt_text or "")
    if prompt_view == "rough_ja":
        return "<pre>{}</pre>".format(html.escape(_rough_translate_prompt_text(text)))
    if prompt_view == "compact":
        compacted = compact_prompt_via_owner(text)
        return "<pre>{}</pre>".format(
            html.escape(str(compacted.get("suggested_prompt") or text or "(none)"))
        )
    return "<pre>{}</pre>".format(html.escape(text or "(none)"))


def _rough_translate_prompt_text(text: str) -> str:
    if not text.strip():
        return "(none)"
    if sum(1 for char in text if ord(char) > 127) > max(8, len(text) // 12):
        return text
    replacements = [
        ("You are", "あなたは"),
        ("Return", "返してください"),
        ("Do not", "しないでください"),
        ("Don't", "しないでください"),
        ("Always", "常に"),
        ("Never", "決して"),
        ("When", "もし"),
        ("If", "もし"),
        ("Use", "使ってください"),
        ("Avoid", "避けてください"),
        ("Keep", "維持してください"),
        ("Prefer", "優先してください"),
        ("Required", "必須"),
        ("Output", "出力"),
        ("Context", "コンテキスト"),
        ("Goal", "目標"),
        ("Tools", "ツール"),
        ("System", "システム"),
        ("User", "ユーザー"),
        ("Assistant", "アシスタント"),
        ("Important", "重要"),
    ]
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line
        for source, target in replacements:
            if line.startswith(source):
                line = target + line[len(source):]
                break
        lines.append(line)
    return "\n".join(lines)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(sanitize_trace_value(content), ensure_ascii=False, indent=2)
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(json.dumps(sanitize_trace_value(block), ensure_ascii=False, indent=2))
    return "\n\n".join(parts)


def _text_from_messages(messages: list[dict[str, Any]], *, system_only: bool = False) -> str:
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if system_only and str(message.get("role") or "") != "system":
            continue
        text = _content_to_text(message.get("content"))
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)


def _format_ms(value: Any) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value) / 1000.0))
