from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_defaultspack_uses_stub_default_without_cloud_key(tmp_path):
    import domain.chat.store as chat_store

    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
    from domain.ai_client.model_runtime_settings import DEFAULT_MODEL
    from domain.frontend.registry import FrontendRegistry
    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    service = ModelRuntimeSettingsService(tmp_path)
    registry = FrontendRegistry(tmp_path)

    assert service.get_preferred_model() == "stub/default"
    assert DEFAULT_MODEL == "stub/default"
    assert not hasattr(chat_store, "DEFAULT_CHAT_MODEL")
    assert registry._default_settings()["models"]["model_api_routes"] == ""
    assert "model_api_routes" not in registry._default_settings()["apis"]
    assert_profile_resolver_requires_authority_snapshot()
