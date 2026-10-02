"""
transport/cli.py — CLI transport for defaults pack.

Supports:
  - Interactive mode (REPL)
  - One-shot mode (--message)
  - Pipe input mode (stdin is not a TTY)
  - HTTP backend mode (--http, connects to localhost:8766)
  - Direct backend mode (default, imports blocks directly)
  - JSON output mode (--json)
  - InterfaceRegistry registration as io.cli.server
"""

import sys
import os
import json
import argparse

# Ensure pack root is importable
_pack_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _pack_root not in sys.path:
    sys.path.insert(0, _pack_root)

from blocks._common import error, timestamp  # noqa: E402
from bridge.block_adapter import invoke_block  # noqa: E402
from transport.registry import flow_http_output_is_compatible  # noqa: E402
from transport.cli_formatter import (  # noqa: E402
    format_json,
    stream_print,
    stream_print_line,
    extract_text_from_response,
    print_prompt,
    print_assistant_label,
    print_system_message,
    print_error_message,
    print_welcome,
)
from transport.cli_commands import execute_command  # noqa: E402


# ── Configuration ────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "default_model": "default",
    "system_prompt": "",
    "http_host": "127.0.0.1",
    "http_port": 8766,
    "stream": True,
}


def _config_path():
    pack_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    return os.path.join(pack_root, "user_data", "cli_config.json")


def load_config():
    """Load CLI config from user_data/cli_config.json, falling back to defaults."""
    path = _config_path()
    config = dict(_DEFAULT_CONFIG)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                config.update(stored)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config):
    """Persist CLI config to user_data/cli_config.json."""
    path = _config_path()
    dir_path = os.path.dirname(path)
    if not os.path.isdir(dir_path):
        os.makedirs(dir_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _sse_events_from_result(result):
    if isinstance(result, dict) and result.get("_sse"):
        return result.get("events", [])
    if (
        isinstance(result, dict)
        and result.get("status") == "ok"
        and isinstance(result.get("data"), dict)
        and result["data"].get("_sse")
    ):
        return result["data"].get("events", [])
    return None


def _cli_chunk_from_sse_event(event):
    if isinstance(event, bytes):
        try:
            event = json.loads(event.decode("utf-8").removeprefix("data:").strip())
        except Exception:
            return None
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    if event_type == "delta":
        return {"type": "content_delta", "delta": {"text": str(event.get("delta") or "")}}
    if event_type == "content_delta":
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        text = data.get("delta") or delta.get("text") or event.get("text") or ""
        return {"type": "content_delta", "delta": {"text": str(text)}}
    if event_type in {"done", "stream_end"}:
        return {"type": "stream_end", "message": event.get("message")}
    if event_type == "error":
        err = event.get("error")
        if isinstance(err, dict):
            message = err.get("message") or str(err)
        else:
            message = str(err or event.get("message") or "Stream failed")
        return {"type": "error", "message": message}
    return None


# ── Backend Adapters ─────────────────────────────────────────


class DirectBackend:
    """Call blocks directly via Python import (no HTTP server needed)."""

    def __init__(self):
        self._context_base = {
            "flow_id": "cli_direct",
            "step_id": "cli_request",
            "phase": "execute",
            "owner_pack": "defaultspack",
            "inputs": {},
        }

    def _ctx(self):
        ctx = dict(self._context_base)
        ctx["ts"] = timestamp()
        return ctx

    def _call_block(self, module_name, params):
        return invoke_block(module_name, dict(params or {}), self._ctx())

    def _call_flow(self, flow_id, params, *, fallback_block_module=""):
        try:
            from domain.flow import FlowEngine

            result = FlowEngine().execute(flow_id, dict(params or {}), self._ctx())
            if result.is_success():
                if flow_http_output_is_compatible(
                    flow_id,
                    result.output,
                    fallback_block_module=fallback_block_module,
                ):
                    return result.output
                if not fallback_block_module:
                    return result.output
            elif not fallback_block_module:
                return result.output
        except Exception as exc:
            if not fallback_block_module:
                return error("Flow failed: " + str(exc))
        return self._call_block(fallback_block_module, params)

    def create_conversation(self, params):
        return self._call_block("blocks.chat.create_conversation", params)

    def list_conversations(self, params):
        return self._call_block("blocks.chat.list_conversations", params)

    def get_conversation(self, params):
        return self._call_block("blocks.chat.get_conversation", params)

    def update_conversation(self, params):
        return self._call_block("blocks.chat.update_conversation", params)

    def delete_conversation(self, params):
        return self._call_block("blocks.chat.delete_conversation", params)

    def send_message(self, params):
        from core_runtime.di_container import get_container
        from core_runtime.global_contract_dispatch import invoke_global_contract

        session = get_container().get_or_none("v4_dispatch_session")
        if session is None:
            return error(
                "Captured Pack v4 session is unavailable",
                "V4_SESSION_UNAVAILABLE",
            )
        payload = dict(params or {})
        request = {
            key: payload[key]
            for key in (
                "model",
                "messages",
                "tools",
                "params",
                "context",
                "runtime_context",
                "timezone",
            )
            if key in payload
        }
        if not isinstance(request.get("messages"), list):
            content = str(payload.get("content") or payload.get("message") or "")
            if content:
                request["messages"] = [{"role": "user", "content": content}]
        try:
            return invoke_global_contract(
                session,
                "conversation.turn.v1",
                "complete",
                request,
            )
        except Exception as exc:
            return error(str(exc), "V4_CONVERSATION_FAILED")

    def send_message_stream(self, conversation_id, message_content, model):
        """Reject streaming until the captured v4 plan pins a stream operation."""
        del conversation_id, message_content, model
        result = error(
            "Chat streaming is absent from the captured Pack v4 catalog",
            "V4_OPERATION_UNAVAILABLE",
        )
        events = _sse_events_from_result(result)
        if events is None:
            if isinstance(result, dict) and result.get("status") == "error":
                err = result.get("error") or {}
                message = err.get("message") if isinstance(err, dict) else str(err)
                yield {"type": "error", "message": message or "Request failed"}
                return
            text = extract_text_from_response(
                result.get("data") if isinstance(result, dict) else result
            )
            if text:
                yield {"type": "content_delta", "delta": {"text": text}}
            yield {
                "type": "stream_end",
                "message": result.get("data") if isinstance(result, dict) else None,
            }
            return
        for event in events:
            chunk = _cli_chunk_from_sse_event(event)
            if chunk is not None:
                yield chunk

    def list_models(self, params):
        return self._call_block("blocks.ai.models", params)

    def call(self, action, params):
        """Generic dispatcher."""
        method = getattr(self, action, None)
        if method is None:
            return error("Unknown action: " + action)
        return method(params)


class HttpBackend:
    """Send requests to the running HTTP server via urllib."""

    def __init__(self, host="127.0.0.1", port=8766):
        self.base_url = "http://" + host + ":" + str(port)

    def _request(self, method, path, data=None):
        import urllib.request
        import urllib.error

        url = self.base_url + path
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"} if body else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8")
                return json.loads(err_body)
            except Exception:
                return error("HTTP " + str(exc.code) + ": " + str(exc.reason))
        except urllib.error.URLError as exc:
            return error("Connection failed: " + str(exc.reason))
        except Exception as exc:
            return error("Request failed: " + str(exc))

    def create_conversation(self, params):
        return self._request("POST", "/api/chat/conversations", params)

    def list_conversations(self, params):
        return self._request("GET", "/api/chat/conversations")

    def get_conversation(self, params):
        cid = params.get("conversation_id", "")
        return self._request("GET", "/api/chat/conversations/" + cid)

    def update_conversation(self, params):
        cid = params.get("conversation_id", "")
        return self._request("PUT", "/api/chat/conversations/" + cid, params)

    def delete_conversation(self, params):
        cid = params.get("conversation_id", "")
        return self._request("DELETE", "/api/chat/conversations/" + cid)

    def send_message(self, params):
        cid = params.get("conversation_id", "")
        return self._request("POST", "/api/chat/conversations/" + cid + "/messages", params)

    def send_message_stream(self, conversation_id, message_content, model):
        """HTTP mode does not support true streaming — falls back to send_message
        and yields the full response as a single chunk."""
        result = self.send_message(
            {
                "conversation_id": conversation_id,
                "message": {"role": "user", "content": message_content},
            }
        )
        if result and result.get("status") == "ok":
            text = extract_text_from_response(result.get("data"))
            if text:
                yield {"type": "content_delta", "delta": {"text": text}}
            yield {"type": "stream_end", "message": result.get("data")}
        else:
            err_msg = "Request failed"
            if result and result.get("error"):
                err_detail = result["error"]
                if isinstance(err_detail, dict):
                    err_msg = err_detail.get("message", err_msg)
                else:
                    err_msg = str(err_detail)
            yield {"type": "error", "message": err_msg}

    def list_models(self, params):
        return self._request("GET", "/api/ai/models")

    def call(self, action, params):
        method = getattr(self, action, None)
        if method is None:
            return error("Unknown action: " + action)
        return method(params)


# ── CLI Session ──────────────────────────────────────────────


class CLISession:
    """Maintains state for an interactive CLI session."""

    def __init__(self, backend, config, json_mode=False):
        self.backend = backend
        self.config = config
        self.json_mode = json_mode
        self.conversation_id = None
        self.should_exit = False
        self.backend_mode = "http" if isinstance(backend, HttpBackend) else "direct"

    def save_config(self):
        save_config(self.config)

    def backend_call(self, action, params):
        """Call backend and return result dict."""
        return self.backend.call(action, params)

    def ensure_conversation(self):
        """Ensure there is an active conversation, creating one if needed."""
        if self.conversation_id:
            return True
        model = self.config.get("default_model", "stub/default")
        result = self.backend_call("create_conversation", {"model": model})
        if result and result.get("status") == "ok":
            conv = result.get("data", {})
            cid = conv.get("id", conv.get("conversation_id", ""))
            if cid:
                self.conversation_id = cid
                print_system_message(
                    "Auto-created conversation " + cid[:8] + " (model: " + model + ")"
                )
                return True
        print_error_message("Failed to create conversation.")
        return False

    def send_and_display(self, user_input):
        """Send user message and display AI response (with streaming if possible)."""
        if not self.ensure_conversation():
            return

        model = self.config.get("default_model", "stub/default")

        if self.json_mode:
            # JSON mode: send via non-streaming call and dump raw response
            result = self.backend_call(
                "send_message",
                {
                    "conversation_id": self.conversation_id,
                    "message": {"role": "user", "content": user_input},
                },
            )
            print(format_json(result))
            return

        # Streaming display
        print_assistant_label()

        full_text_parts = []
        error_occurred = False

        for chunk in self.backend.send_message_stream(self.conversation_id, user_input, model):
            chunk_type = chunk.get("type", "")
            if chunk_type == "content_delta":
                delta_text = chunk.get("delta", {}).get("text", "")
                if delta_text:
                    full_text_parts.append(delta_text)
                    stream_print(delta_text)
            elif chunk_type == "stream_end":
                # Streaming done
                stream_print_line("")  # newline after streamed content
                break
            elif chunk_type == "error":
                error_occurred = True
                print_error_message(chunk.get("message", "Unknown error"))
                break

        if not error_occurred and not full_text_parts:
            print_system_message("(empty response)")

        stream_print_line("")  # blank line after response


# ── Interactive mode ─────────────────────────────────────────


def _run_interactive(session):
    """Run the interactive REPL loop."""
    print_welcome()

    # Try to enable readline for line editing
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    while not session.should_exit:
        try:
            print_prompt(session.conversation_id)
            line = input()
        except (EOFError, KeyboardInterrupt):
            stream_print_line("")
            print_system_message("Goodbye!")
            break

        line = line.strip()
        if not line:
            continue

        # Slash commands
        if line.startswith("/"):
            try:
                output = execute_command(session, line)
                if output is not None:
                    stream_print_line(output)
            except KeyError as exc:
                print_error_message(str(exc))
            continue

        # Regular chat message
        session.send_and_display(line)


# ── One-shot mode ────────────────────────────────────────────


def _run_oneshot(session, message):
    """Send a single message and print the response."""
    session.send_and_display(message)


# ── Pipe mode ────────────────────────────────────────────────


def _run_pipe(session):
    """Read all of stdin and send as a single message."""
    try:
        message = sys.stdin.read().strip()
    except KeyboardInterrupt:
        return
    if not message:
        print_error_message("No input received from pipe.")
        return
    session.send_and_display(message)


# ── Interface registry entry point ───────────────────────────


def start_cli_server(facade):
    """Callable registered as io.cli.server.

    Receives a KernelFacade and boots the CLI transport in interactive mode.
    """
    config = load_config()
    backend = DirectBackend()
    session = CLISession(backend, config)
    _run_interactive(session)


# ── Main ─────────────────────────────────────────────────────


def main():
    """CLI entry point — parses args and dispatches to the appropriate mode."""
    parser = argparse.ArgumentParser(
        prog="defaultspack.cli",
        description="rumi defaults — CLI transport",
    )
    parser.add_argument(
        "-m",
        "--message",
        help="Send a single message (one-shot mode)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        default=False,
        help="Use HTTP backend (connect to running HTTP server)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP server host (default: from config or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP server port (default: from config or 8766)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="json_mode",
        help="Output raw JSON responses",
    )
    parser.add_argument(
        "--conversation",
        "-c",
        default=None,
        help="Resume an existing conversation by ID (or prefix)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the default model for this session",
    )

    args = parser.parse_args()

    config = load_config()

    # Apply CLI overrides
    if args.model:
        config["default_model"] = args.model
    if args.host:
        config["http_host"] = args.host
    if args.port:
        config["http_port"] = args.port

    # Choose backend
    if args.http:
        backend = HttpBackend(
            host=config.get("http_host", "127.0.0.1"),
            port=config.get("http_port", 8766),
        )
    else:
        backend = DirectBackend()

    session = CLISession(backend, config, json_mode=args.json_mode)

    # Optionally resume a conversation
    if args.conversation:
        session.conversation_id = args.conversation

    # Determine mode
    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    if args.message:
        # One-shot mode
        _run_oneshot(session, args.message)
    elif not is_tty:
        # Pipe mode
        _run_pipe(session)
    else:
        # Interactive mode
        _run_interactive(session)
