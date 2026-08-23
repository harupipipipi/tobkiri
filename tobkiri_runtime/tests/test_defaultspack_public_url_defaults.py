from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.webhooks.public_url import run  # noqa: E402
from domain.webhook.url_defaults import (  # noqa: E402
    default_local_url,
    resolved_local_url,
)


def test_default_local_url_uses_active_runtime_bind() -> None:
    with patch.dict(
        os.environ,
        {"DEFAULTS_HTTP_HOST": "127.0.0.1", "DEFAULTS_HTTP_PORT": "8791"},
    ):
        assert default_local_url() == "http://127.0.0.1:8791"
        assert resolved_local_url("http://127.0.0.1:8766") == (
            "http://127.0.0.1:8791"
        )
        assert resolved_local_url("http://127.0.0.1:9900") == (
            "http://127.0.0.1:9900"
        )


def test_public_url_operations_share_the_runtime_default() -> None:
    provider = Mock()
    provider.create_url.return_value = {"ok": True, "public_url": "https://example.test"}
    with (
        patch.dict(
            os.environ,
            {"DEFAULTS_HTTP_HOST": "127.0.0.1", "DEFAULTS_HTTP_PORT": "8791"},
        ),
        patch("blocks.webhooks.public_url._provider", return_value=provider),
    ):
        get_result = run({"_method": "GET"}, {})
        post_result = run({"_method": "POST", "provider_id": "static"}, {})

    assert get_result["data"]["default_local_url"] == "http://127.0.0.1:8791"
    provider.create_url.assert_called_once_with(
        local_url="http://127.0.0.1:8791",
        route_path="/",
        ttl_seconds=0,
        context={},
    )
    assert post_result["data"]["public_url"] == "https://example.test"


def test_wildcard_bind_is_presented_as_reachable_loopback_url() -> None:
    with patch.dict(
        os.environ,
        {"DEFAULTS_HTTP_HOST": "0.0.0.0", "DEFAULTS_HTTP_PORT": "8791"},
    ):
        assert default_local_url() == "http://127.0.0.1:8791"
