from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core_runtime.egress_domain_controller import DomainController
from tobkiri_protocol.canonical import canonical_digest


RUNTIME = Path(__file__).resolve().parent.parent


def _selected_policy() -> tuple[dict[str, dict], dict[str, dict[str, str]]]:
    manifest = json.loads(
        (
            RUNTIME
            / "ecosystem"
            / "rumi_provider_adapters_pack"
            / "pack.v4.json"
        ).read_text(encoding="utf-8")
    )
    manifest["requirements"]["network"]["allowed_domains"] = [
        "api.example.com",
        "*.models.example",
    ]
    pack_id = manifest["pack"]["id"]
    selected = {pack_id: manifest}
    bindings = {
        pack_id: {
            "source_identity": manifest["integrity"]["source_identity"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "manifest_digest": canonical_digest(manifest),
        }
    }
    return selected, bindings


def test_domain_policy_uses_only_exact_selected_v4_binding() -> None:
    selected, bindings = _selected_policy()
    controller = DomainController.from_pack_v4_documents(selected, bindings)
    pack_id = next(iter(selected))

    assert controller.check_domain(pack_id, "api.example.com") == (True, "")
    assert controller.check_domain(pack_id, "child.models.example") == (True, "")
    assert controller.check_domain(pack_id, "models.example") == (True, "")
    assert controller.check_domain(pack_id, "evil.example")[0] is False
    assert controller.check_domain("unselected.pack", "api.example.com")[0] is False


def test_domain_policy_rejects_tamper_stale_and_extra_bindings() -> None:
    selected, bindings = _selected_policy()
    pack_id = next(iter(selected))
    tampered = copy.deepcopy(selected)
    tampered[pack_id]["requirements"]["network"]["allowed_domains"].append("evil.example")
    with pytest.raises(ValueError, match="identity mismatch"):
        DomainController.from_pack_v4_documents(tampered, bindings)

    stale = copy.deepcopy(bindings)
    stale[pack_id]["source_identity"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="identity mismatch"):
        DomainController.from_pack_v4_documents(selected, stale)

    extra = copy.deepcopy(bindings)
    extra["extra.pack"] = dict(extra[pack_id])
    with pytest.raises(ValueError, match="not exact"):
        DomainController.from_pack_v4_documents(selected, extra)


def test_empty_policy_denies_and_snapshot_cannot_be_mutated() -> None:
    controller = DomainController({})
    assert controller.check_domain("any.pack", "api.example.com")[0] is False
    with pytest.raises(RuntimeError, match="immutable"):
        controller.invalidate_cache()
