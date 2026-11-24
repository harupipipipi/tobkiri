from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from core_runtime.pack_artifact_integrity import write_host_install_record


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def test_frontend_capability_uses_persisted_plan_and_exact_catalog(
    monkeypatch,
) -> None:
    from blocks.ui import frontend_capability

    frontend_capability._SEEN_REQUESTS.clear()
    plan = SimpleNamespace(plan_hash="plan-1", profile_id="profile-1")
    contribution = SimpleNamespace(
        contribution_id="feature.route",
        owner_pack_id="feature-pack",
        resolved_plan_hash="plan-1",
        action_contract=None,
        data_source_contract="rumi.resource.feature.read.v1",
        isolated=None,
    )
    catalog = SimpleNamespace(
        plan_hash="plan-1",
        catalog_hash="sha256:catalog-1",
        contributions=(contribution,),
    )
    monkeypatch.setattr(
        frontend_capability,
        "persisted_resolved_profile",
        lambda: plan,
    )
    monkeypatch.setattr(
        frontend_capability,
        "build_frontend_catalog",
        lambda _plan: catalog,
    )
    monkeypatch.setattr(
        frontend_capability,
        "invoke_global_contract",
        lambda _registry, contract_id, operation, payload: {
            "contract_id": contract_id,
            "operation": operation,
            "payload": payload,
        },
    )
    request = {
        "request_id": "request-1",
        "expires_at": time.time() + 30,
        "profile_id": "profile-1",
        "plan_hash": "plan-1",
        "catalog_hash": "sha256:catalog-1",
        "owner_pack_id": "feature-pack",
        "contribution_id": "feature.route",
        "contract_id": "rumi.resource.feature.read.v1",
        "payload": {"operation": "read", "input": {"id": "feature"}},
    }

    result = frontend_capability.run(request, {"v4_dispatch_session": object()})

    assert result["status"] == "ok"
    assert result["data"]["payload"] == {
        "id": "feature",
        "profile_id": "profile-1",
    }


def test_frontend_capability_rejects_a_catalog_not_seen_by_the_host(
    monkeypatch,
) -> None:
    from blocks.ui import frontend_capability

    frontend_capability._SEEN_REQUESTS.clear()
    plan = SimpleNamespace(plan_hash="plan-1", profile_id="profile-1")
    catalog = SimpleNamespace(
        plan_hash="plan-1",
        catalog_hash="sha256:catalog-2",
        contributions=(),
    )
    monkeypatch.setattr(
        frontend_capability,
        "persisted_resolved_profile",
        lambda: plan,
    )
    monkeypatch.setattr(
        frontend_capability,
        "build_frontend_catalog",
        lambda _plan: catalog,
    )
    request = {
        "request_id": "request-2",
        "expires_at": time.time() + 30,
        "profile_id": "profile-1",
        "plan_hash": "plan-1",
        "catalog_hash": "sha256:catalog-1",
        "owner_pack_id": "feature-pack",
        "contribution_id": "feature.route",
        "contract_id": "rumi.resource.feature.read.v1",
        "payload": {"operation": "read", "input": {}},
    }

    result = frontend_capability.run(request, {"v4_dispatch_session": object()})

    assert result["status"] == "error"
    assert result["error"]["code"] == "STALE_CATALOG"


def test_isolated_asset_is_bound_to_persisted_plan_and_artifact_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from blocks.ui import isolated_pack_asset

    pack = tmp_path / "feature-pack"
    ui = pack / "ui"
    ui.mkdir(parents=True)
    asset = ui / "index.html"
    asset_bytes = b"<main>isolated</main>"
    asset.write_bytes(asset_bytes)
    artifact_manifest = {
        "artifacts": [
            {"path": "ui/index.html", "sha256": _sha256(asset_bytes)}
        ]
    }
    artifact_raw = json.dumps(artifact_manifest).encode("utf-8")
    (pack / "artifact-manifest.json").write_bytes(artifact_raw)
    ecosystem_manifest = {
        "pack_id": "feature-pack",
        "provenance": {"content_hash": _sha256(artifact_raw)},
        "metadata": {
            "integrity": {"artifact_manifest": "artifact-manifest.json"}
        },
    }
    ecosystem_path = pack / "ecosystem.json"
    ecosystem_path.write_text(json.dumps(ecosystem_manifest), encoding="utf-8")
    trust_store = tmp_path / "host-policy" / "publisher-trust.json"
    write_host_install_record(
        trust_store,
        pack_id="feature-pack",
        install_path=pack,
        record={
            "signature_required": False,
            "developer_mode": True,
            "publisher_id": "",
            "key_id": "",
            "installed_version": "unknown",
            "signed_manifest_path": "",
            "contract_versions": {},
            "requested_capabilities": [],
        },
    )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(trust_store))
    monkeypatch.setenv("RUMI_PACK_DEVELOPER_MODE", "1")
    plan = SimpleNamespace(
        effective_pack_set=("feature-pack",),
        packs=(
            SimpleNamespace(
                pack_id="feature-pack",
                content_hash=_sha256(artifact_raw),
            ),
        ),
    )
    location = SimpleNamespace(
        pack_subdir=pack,
        ecosystem_json_path=ecosystem_path,
    )
    monkeypatch.setattr(
        isolated_pack_asset,
        "persisted_resolved_profile",
        lambda: plan,
    )
    monkeypatch.setattr(
        isolated_pack_asset,
        "resolve_pack_locations",
        lambda _pack_ids: (location,),
    )

    result = isolated_pack_asset.run(
        {"pack_id": "feature-pack", "asset_path": "index.html"},
        {},
    )

    assert result["status_code"] == 200
    assert result["body"] == asset_bytes.decode("utf-8")
    assert result["headers"]["Content-Security-Policy"].startswith(
        "default-src 'none'"
    )

    asset.write_text("<main>tampered</main>", encoding="utf-8")
    tampered = isolated_pack_asset.run(
        {"pack_id": "feature-pack", "asset_path": "index.html"},
        {},
    )

    assert tampered["status"] == "error"
    assert tampered["error"]["code"] == "PACK_INTEGRITY_FAILED"
