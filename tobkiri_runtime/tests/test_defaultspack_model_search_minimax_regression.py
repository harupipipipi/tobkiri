from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


MINIMAX_PROFILE_ID = "opencode-zen/minimax-m3-free"


def test_dynamic_catalog_models_are_projected_into_profile_picker():
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import _merge_model_profiles

    profiles = [
        {
            "profile_id": "openrouter/free",
            "qualified_model_id": "openrouter/free",
            "provider_id": "openrouter",
            "model_id": "free",
            "display_name": "Free Models Router",
        }
    ]
    models = [
        {
            "id": MINIMAX_PROFILE_ID,
            "qualified_model_id": MINIMAX_PROFILE_ID,
            "provider_id": "opencode-zen",
            "model_id": "minimax-m3-free",
            "display_name": "MiniMax M3 Free via OpenCode Zen",
            "metadata": {"inventory_source": "curated_fallback"},
        }
    ]

    merged = _merge_model_profiles(profiles, models)

    assert [profile["profile_id"] for profile in merged] == [
        "openrouter/free",
        MINIMAX_PROFILE_ID,
    ]
    assert merged[1]["provider_id"] == "opencode-zen"
    assert merged[1]["metadata"]["inventory_source"] == "curated_fallback"


def test_configured_zen_fallback_joins_nonempty_global_catalog(monkeypatch):
    from ecosystem.defaultspack.backend.ai_client import provider_catalog

    openrouter_model = {
        "id": "openrouter/free",
        "qualified_model_id": "openrouter/free",
        "provider_id": "openrouter",
        "model_id": "free",
        "display_name": "Free Models Router",
    }
    zen_model = {
        "id": MINIMAX_PROFILE_ID,
        "qualified_model_id": MINIMAX_PROFILE_ID,
        "provider_id": "opencode-zen",
        "provider": "opencode-zen",
        "model_id": "minimax-m3-free",
        "display_name": "MiniMax M3 Free via OpenCode Zen",
        "metadata": {"inventory_source": "curated_fallback"},
    }

    class _RuntimeClient:
        def list_models(self, provider=None):
            if provider and provider != "opencode-zen":
                return []
            return [zen_model]

    def fake_invoke(contract_id, operation, payload):
        del operation, payload
        if contract_id == provider_catalog._MODEL_CATALOG_CONTRACT:
            return {"models": [openrouter_model]}
        if contract_id == provider_catalog._MODEL_PROFILE_CONTRACT:
            return {"profiles": [openrouter_model]}
        raise AssertionError(contract_id)

    monkeypatch.setattr(provider_catalog, "_runtime_client", lambda: _RuntimeClient())
    monkeypatch.setattr(provider_catalog, "_invoke", fake_invoke)

    model_ids = {item["id"] for item in provider_catalog.list_model_catalog()}
    profile_ids = {item["profile_id"] for item in provider_catalog.list_profile_catalog()}

    assert MINIMAX_PROFILE_ID in model_ids
    assert MINIMAX_PROFILE_ID in profile_ids


def _catalog_model() -> dict | None:
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog

    return next(
        (
            item
            for item in list_model_catalog(provider="opencode-zen")
            if item["id"] == MINIMAX_PROFILE_ID
        ),
        None,
    )


def _catalog_profile() -> dict | None:
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog

    return next(
        (
            item
            for item in list_profile_catalog()
            if item["profile_id"] == MINIMAX_PROFILE_ID
        ),
        None,
    )


def test_defaultspack_catalog_hides_unconfigured_minimax_static_snapshot(monkeypatch):
    # External providers publish account-visible inventory only.  The checked-in
    # MiniMax example is intentionally not a public catalog fallback.
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    model = _catalog_model()
    profile = _catalog_profile()

    assert model is None
    assert profile is None


def test_defaultspack_model_search_does_not_synthesize_minimax_static_snapshot(
    monkeypatch,
):
    # A capability query cannot mint a model/profile from a checked-in external
    # snapshot; a configured live adapter must first publish the model.
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog
    from domain.ai_client.model_search import search_models

    result = search_models(
        {
            "query": "minimax m3 free",
            "provider_id": "opencode-zen",
            "requires": {"thinking": True, "vision": True},
            "max_results": 10,
        },
        profiles=list_profile_catalog(),
    )

    assert result["models"] == []
