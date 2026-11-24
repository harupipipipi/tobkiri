from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
MODEL_CATALOG_ROOT = ROOT / "ecosystem" / "rumi_model_catalog_pack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("provider_model_catalog_selected")

from domain.ai_client.providers import (  # noqa: E402
    detect_available_providers,
    get_all_known_models,
    get_provider_catalog_map,
)
from domain.ai_client.providers.component_metadata import (  # noqa: E402
    _safe_catalog_file,
    model_manifests_from_provider_components,
    provider_component_metadata_map,
    provider_manifests_from_components,
)
from domain.components.registry import (  # noqa: E402
    DomainComponentRegistry,
    build_domain_component_roots,
    get_domain_component_registry,
)


OPENGATEWAY_MODELS = {
    "gitlawb-opengateway/mimo-v2.5-pro",
    "gitlawb-opengateway/mimo-v2-flash",
    "gitlawb-opengateway/mimo-v2-omni",
    "gitlawb-opengateway/mimo-v2-pro",
    "gitlawb-opengateway/mimo-v2.5",
}


def test_provider_components_include_gitlawb_and_common_provider_aliases():
    registry = DomainComponentRegistry(
        [
            *build_domain_component_roots(DEFAULTSPACK_ROOT),
            MODEL_CATALOG_ROOT / "catalog",
        ]
    )

    assert registry.get("providers", "gitlawb-opengateway").id == "gitlawb-opengateway"
    assert registry.get("providers", "gemini").id == "google"
    assert registry.get("providers", "openrouter").id == "openrouter"
    assert registry.get("providers", "groq").id == "groq"
    assert registry.get("providers", "deepseek").id == "deepseek"


def test_gitlawb_provider_component_preserves_api_key_required_allowlist_metadata():
    metadata = provider_component_metadata_map()["gitlawb-opengateway"]
    models = model_manifests_from_provider_components("gitlawb-opengateway")
    model_ids = {model["id"] for model in models}
    omni = next(model for model in models if model["id"].endswith("mimo-v2-omni"))

    assert metadata["default_base_url"] == "https://opengateway.gitlawb.com/v1"
    assert metadata["env_vars"] == ["GITLAWB_OPENGATEWAY_API_KEY"]
    assert metadata["base_url_envs"] == ["GITLAWB_OPENGATEWAY_BASE_URL"]
    assert metadata["provider_manifest"]["credential_required"] is True
    assert metadata["provider_manifest"]["api_key_env"] == "GITLAWB_OPENGATEWAY_API_KEY"
    assert model_ids == OPENGATEWAY_MODELS
    assert all(model["metadata"]["api_key_required"] is True for model in models)
    assert omni["metadata"]["vision_verified"] is True


def test_provider_catalog_interops_with_model_catalog_pack_manifests():
    catalog = get_provider_catalog_map()
    models = {model["id"]: model for model in get_all_known_models("gitlawb-opengateway")}

    assert catalog["gitlawb-opengateway"]["metadata"]["default_base_url"] == "https://opengateway.gitlawb.com/v1"
    assert set(models) == OPENGATEWAY_MODELS
    assert models["gitlawb-opengateway/mimo-v2-omni"]["metadata"]["vision_verified"] is True

    provider_manifest_path = MODEL_CATALOG_ROOT / "extensions" / "llm" / "providers" / "gitlawb-opengateway" / "manifest.json"
    provider_manifest = json.loads(provider_manifest_path.read_text(encoding="utf-8"))
    assert provider_manifest["id"] == "gitlawb-opengateway"
    assert provider_manifest["credential_required"] is True

    model_manifest_ids = {
        json.loads(path.read_text(encoding="utf-8"))["id"]
        for path in (provider_manifest_path.parent / "models").glob("*.json")
    }
    assert model_manifest_ids == OPENGATEWAY_MODELS


def test_xiaomi_mimo_provider_components_expose_token_subscription_plan():
    metadata = provider_component_metadata_map()["xiaomi-mimo-global"]
    catalog = get_provider_catalog_map()
    models = {model["id"]: model for model in get_all_known_models("xiaomi-mimo-global")}

    metadata_plan = metadata["subscription_plans"][0]
    catalog_plan = catalog["xiaomi-mimo-global"]["subscription_plans"][0]

    assert metadata_plan["id"] == "mimo_orbit_100t_grant_if_available"
    assert catalog_plan["id"] == metadata_plan["id"]
    assert catalog_plan["requires_manual_signup"] is True
    assert catalog_plan["do_not_auto_enable"] is True
    assert models["xiaomi-mimo-global/mimo-v2-flash"]["metadata"]["subscription_plan_ids"] == [
        metadata_plan["id"]
    ]


def test_untrusted_provider_component_manifest_is_not_promoted_or_imported(tmp_path, monkeypatch):
    extra_domain_root = tmp_path / "evil_pack" / "domain"
    provider_root = extra_domain_root / "providers" / "evil_validation"
    provider_root.mkdir(parents=True)
    (tmp_path / "evil_provider.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(tmp_path / 'sentinel.txt')!r}).write_text('imported', encoding='utf-8')\n"
        "class EvilProvider:\n"
        "    def __init__(self):\n"
        "        Path(__file__).with_name('sentinel.txt').write_text('instantiated', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (provider_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "evil_validation",
                "provider_id": "evil_validation",
                "category": "providers",
                "kind": "llm_provider",
                "version": "1",
                "status": "stable",
                "provider_metadata": {"display_name": "Evil", "kind": "cloud"},
                "provider_manifest": {
                    "id": "evil_validation",
                    "category": "llm_provider",
                    "version": "1",
                    "enabled": True,
                    "credential_required": False,
                    "default_base_url": "https://example.invalid/v1",
                    "entrypoint": "evil_provider:EvilProvider",
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_DOMAIN_COMPONENT_ROOTS", str(extra_domain_root))
    try:
        get_domain_component_registry(force_reload=True)

        metadata = provider_component_metadata_map()
        manifests = provider_manifests_from_components()
        available = detect_available_providers()

        assert "evil_validation" not in metadata
        assert "evil_validation" not in manifests
        assert "evil_validation" not in available
        assert not (tmp_path / "sentinel.txt").exists()
    finally:
        monkeypatch.delenv("RUMI_DEFAULTSPACK_DOMAIN_COMPONENT_ROOTS", raising=False)
        get_domain_component_registry(force_reload=True)


def test_model_catalog_file_loader_rejects_escape_and_symlink_paths(tmp_path):
    pack_root = tmp_path / "rumi_model_catalog_pack"
    catalog_root = pack_root / "catalog"
    catalog_root.mkdir(parents=True)
    inside = catalog_root / "models.json"
    inside.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    assert _safe_catalog_file(inside, root=pack_root) == inside
    assert _safe_catalog_file(catalog_root / ".." / ".." / "outside.json", root=pack_root) is None

    link = catalog_root / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this filesystem")
    assert _safe_catalog_file(link, root=pack_root) is None
