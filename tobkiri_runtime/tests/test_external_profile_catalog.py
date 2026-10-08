"""External dispatch composition cannot change verified active closure pins."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from core_runtime import pack_control_v4
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.external_profile_catalog import catalog_for_verified_active_profile
from core_runtime.bootstrap.profile_capture import runtime_user_data_root


def _inputs(monkeypatch, tmp_path):
    digest = "sha256:" + "a" * 64
    profile = {
        "base": {"pack_id": "base"},
        "shell": {"pack_id": "shell"},
        "packs": [{"pack_id": "external", "role": "provider", "artifact_digest": digest}],
    }
    pins = [{"identity": "external", "role": "pack", "artifact_digest": digest}]
    active = SimpleNamespace(
        resolved=SimpleNamespace(
            profile=profile,
            lock={"effective_set": deepcopy(pins)},
            plan={"effective_set": deepcopy(pins)},
        )
    )
    catalog = SimpleNamespace(packs={"base": {}, "shell": {}}, profiles={"custom": profile})
    merged = SimpleNamespace(
        packs={
            **catalog.packs,
            "external": {"pack": {"kind": "normal_sandbox", "artifact_digest": digest}, "requirements": {"execution_boundary": "sandbox"}},
        },
        profiles=catalog.profiles,
    )

    def authenticated_merge(original, roots, *, artifact_pins):
        assert artifact_pins["external"] == digest
        assert original is catalog
        assert set(roots) == {"base", "shell", "external"}
        assert runtime_user_data_root() == tmp_path
        return merged, set(roots)

    monkeypatch.setattr(pack_control_v4, "catalog_with_admitted_pack_closure", authenticated_merge)
    return catalog, active, merged


def test_verified_external_catalog_is_bound_to_authority_data_root(tmp_path, monkeypatch):
    catalog, active, merged = _inputs(monkeypatch, tmp_path)
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "wrong-root"))
    assert catalog_for_verified_active_profile(catalog, active, user_data=tmp_path) is merged
    assert merged.profiles is catalog.profiles


@pytest.mark.parametrize(
    "mutation",
    [
        "profile_digest",
        "profile_role",
        "profile_duplicate",
        "lock_digest",
        "plan_digest",
        "lock_missing",
        "plan_duplicate",
        "host_kind",
        "unpinned_dependency",
    ],
)
def test_external_manifest_cannot_change_active_pins(tmp_path, monkeypatch, mutation):
    catalog, active, merged = _inputs(monkeypatch, tmp_path)
    rows = active.resolved.profile["packs"]
    if mutation == "profile_digest":
        rows[0]["artifact_digest"] = "sha256:" + "b" * 64
    elif mutation == "profile_role":
        rows[0]["role"] = "base"
    elif mutation == "profile_duplicate":
        rows.append(deepcopy(rows[0]))
    elif mutation in {"lock_digest", "plan_digest"}:
        graph = getattr(active.resolved, mutation.split("_")[0])
        graph["effective_set"][0]["artifact_digest"] = "sha256:" + "b" * 64
    elif mutation == "lock_missing":
        active.resolved.lock["effective_set"] = []
    elif mutation == "plan_duplicate":
        pins = active.resolved.plan["effective_set"]
        pins.append(deepcopy(pins[0]))
    elif mutation == "host_kind":
        merged.packs["external"]["pack"]["kind"] = "host_extension"
    else:
        merged.packs["dependency"] = deepcopy(merged.packs["external"])
    with pytest.raises(AuthorityDenied):
        catalog_for_verified_active_profile(catalog, active, user_data=tmp_path)
