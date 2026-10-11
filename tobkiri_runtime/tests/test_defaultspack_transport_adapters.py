from __future__ import annotations

import importlib
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestDefaultspackTransportAdapters(unittest.TestCase):
    def test_http_transport_does_not_direct_import_block_run(self):
        transport_path = (
            Path(__file__).resolve().parent.parent
            / "ecosystem"
            / "defaultspack"
            / "transport"
            / "http.py"
        )
        source = transport_path.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"from blocks\.[\w\.]+ import run")
        self.assertIn("invoke_block(", source)

    def test_cli_transport_does_not_direct_import_block_run(self):
        transport_path = (
            Path(__file__).resolve().parent.parent
            / "ecosystem"
            / "defaultspack"
            / "transport"
            / "cli.py"
        )
        source = transport_path.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"from blocks\.[\w\.]+ import run")
        self.assertIn("invoke_block(", source)

    def test_block_adapter_recovers_from_foreign_blocks_package(self):
        repo_root = Path(__file__).resolve().parent.parent
        pack_root = repo_root / "ecosystem" / "defaultspack"
        original_path = list(sys.path)
        original_modules = dict(sys.modules)

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_root = Path(temp_dir)
            fake_blocks = fake_root / "blocks"
            fake_blocks.mkdir()
            (fake_blocks / "__init__.py").write_text("# foreign blocks package\n", encoding="utf-8")
            sys.path.insert(0, str(fake_root))
            importlib.import_module("blocks")

            try:
                sys.path.insert(0, str(pack_root))
                from bridge.block_adapter import invoke_block

                result = invoke_block("blocks.tool.list", {}, {})
                self.assertEqual(result.get("status"), "ok")
                active_blocks = sys.modules.get("blocks")
                self.assertIsNotNone(active_blocks)
                active_file = getattr(active_blocks, "__file__", "")
                self.assertIn(str(pack_root), active_file)
            finally:
                sys.path[:] = original_path
                for loaded_name in list(sys.modules):
                    if loaded_name not in original_modules:
                        sys.modules.pop(loaded_name, None)
                sys.modules.update(original_modules)

    def test_block_adapter_preserves_repo_ecosystem_namespace(self):
        repo_root = Path(__file__).resolve().parent.parent
        pack_root = repo_root / "ecosystem" / "defaultspack"
        original_path = list(sys.path)
        original_modules = dict(sys.modules)

        try:
            sys.path.insert(0, str(pack_root))
            sys.path.insert(0, str(repo_root))
            ecosystem_module = importlib.import_module("ecosystem")
            importlib.import_module("ecosystem.defaultspack.backend.sandbox.template_catalog")

            from bridge.block_adapter import invoke_block

            result = invoke_block("ecosystem.defaultspack.blocks.sandbox.api", {"action": "list"}, {})

            self.assertEqual(result.get("status"), "ok")
            self.assertIs(sys.modules.get("ecosystem"), ecosystem_module)
            self.assertIn("ecosystem.defaultspack.backend.sandbox.template_catalog", sys.modules)
        finally:
            sys.path[:] = original_path
            for loaded_name in list(sys.modules):
                if loaded_name not in original_modules:
                    sys.modules.pop(loaded_name, None)
            sys.modules.update(original_modules)

    def test_chat_send_infers_computer_tools_from_compute_use_typo(self):
        repo_root = Path(__file__).resolve().parent.parent
        pack_root = repo_root / "ecosystem" / "defaultspack"
        original_path = list(sys.path)
        try:
            sys.path.insert(0, str(pack_root))
            chat_send = importlib.import_module("blocks.chat.send")
            inferred = chat_send._infer_requested_tools_from_message(
                "gemma4の高にcompute useを指示して、VivaldiでLINEを操作して"
            )
            self.assertIn("computer_use", inferred)
            self.assertIn("browser_computer", inferred)
            updated = chat_send._with_inferred_tools({"tools": []}, inferred)
            self.assertEqual(updated["tools"], ["computer_use", "browser_computer"])
        finally:
            sys.path[:] = original_path

    def test_chat_send_explicit_empty_selected_tools_blocks_inferred_computer_tools(self):
        repo_root = Path(__file__).resolve().parent.parent
        pack_root = repo_root / "ecosystem" / "defaultspack"
        original_path = list(sys.path)
        try:
            sys.path.insert(0, str(pack_root))
            chat_send = importlib.import_module("blocks.chat.send")
            updated = chat_send._with_inferred_tools(
                {
                    "tools": [],
                    "params": {"tool_policy": {"selected_tools": []}},
                    "message": {"metadata": {"selected_tools": []}},
                },
                ["computer_use", "browser_computer"],
            )
            self.assertEqual(updated["tools"], [])
        finally:
            sys.path[:] = original_path

    def test_chat_send_metadata_selected_tools_disables_auto_tool_resolution(self):
        repo_root = Path(__file__).resolve().parent.parent
        pack_root = repo_root / "ecosystem" / "defaultspack"
        original_path = list(sys.path)
        try:
            sys.path.insert(0, str(pack_root))
            chat_send = importlib.import_module("blocks.chat.send")
            captured = {}
            original_resolve = chat_send._resolve_selected_tools

            def fake_resolve(raw_tools):
                captured["raw_tools"] = raw_tools
                return [], []

            try:
                chat_send._resolve_selected_tools = fake_resolve
                updated = chat_send._available_tools(
                    {},
                    {"message": {"metadata": {"selected_tools": []}}},
                )
            finally:
                chat_send._resolve_selected_tools = original_resolve
            self.assertEqual(captured["raw_tools"], [])
            self.assertEqual(updated[0], [])
        finally:
            sys.path[:] = original_path

    def test_http_transport_drains_rejected_post_body_before_next_request(self):
        repo_root = Path(__file__).resolve().parent.parent
        pack_root = repo_root / "ecosystem" / "defaultspack"
        original_path = list(sys.path)
        try:
            sys.path.insert(0, str(pack_root))
            transport_http = importlib.import_module("transport.http")
            with mock.patch.dict(os.environ, {"DEFAULTS_HTTP_PORT": "0"}):
                server = transport_http.DefaultsHttpServer(None)
                server.start()
            try:
                port = server._server.server_address[1]
                body = b'{"action":"x"}'
                raw = (
                    b"POST /api/authority/browser-ui-operator HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Origin: http://127.0.0.1\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"\r\n"
                    + body
                    + b"GET /missing-after-drain HTTP/1.1\r\n"
                    + b"Host: 127.0.0.1\r\n"
                    + b"Connection: close\r\n\r\n"
                )
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    sock.sendall(raw)
                    sock.settimeout(5)
                    chunks = []
                    while True:
                        try:
                            chunk = sock.recv(65536)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        chunks.append(chunk)
                response = b"".join(chunks).decode("utf-8", errors="replace")
                self.assertIn("HTTP/1.1 403", response)
                self.assertIn("not found: GET /missing-after-drain", response)
                self.assertNotIn("Bad request syntax", response)
            finally:
                server.stop()
        finally:
            sys.path[:] = original_path


if __name__ == "__main__":
    unittest.main()
