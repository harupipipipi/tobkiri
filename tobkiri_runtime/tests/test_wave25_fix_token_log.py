"""Wave 25 token-log coverage updated for panel-session-only Pack v4 auth."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core_runtime.pack_api_server import PackAPIHandler, PackAPIServer


def _server_source() -> str:
    path = Path(__file__).resolve().parents[1] / "core_runtime" / "pack_api_server.py"
    return path.read_text(encoding="utf-8")


class TestTokenLogPrefix:
    """The retired HMAC-token surface is physically absent and fail-closed."""

    def test_hmac_token_log_contains_prefix(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="core_runtime.pack_api_server"):
            server = PackAPIServer(port=0)
        assert not hasattr(server, "internal_token")
        assert "get_hmac_key_manager" not in _server_source()

    def test_hmac_token_stored_correctly(self):
        server = PackAPIServer(port=0)
        assert not hasattr(server, "internal_token")
        assert not hasattr(PackAPIHandler, "_hmac_key_manager")

    def test_full_token_not_in_info_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="core_runtime.pack_api_server"):
            PackAPIServer(port=0)
        assert all("internal_token" not in record.getMessage() for record in caplog.records)
        assert all("hmac_keys.json" not in record.getMessage() for record in caplog.records)

    def test_explicit_token_no_hmac_log(self, caplog):
        with pytest.raises(TypeError):
            PackAPIServer(port=0, internal_token="explicit-user-token")
        assert all(
            "HMAC-managed API token" not in record.getMessage()
            for record in caplog.records
        )

    def test_retrieval_instructions_in_log(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core_runtime.pack_api_server"):
            PackAPIServer(port=0)
        assert all("hmac_keys.json" not in record.getMessage() for record in caplog.records)

    def test_short_token_no_crash(self):
        server = PackAPIServer(port=0)
        assert not hasattr(server, "internal_token")
        assert "token_urlsafe" not in _server_source()

    def test_empty_token_no_crash(self):
        server = PackAPIServer(port=0)
        assert not hasattr(server, "internal_token")
        assert "HMAC-managed API token" not in _server_source()
