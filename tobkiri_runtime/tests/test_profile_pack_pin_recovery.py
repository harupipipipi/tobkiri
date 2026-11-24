"""Pack-pin recovery accepts only one exact registered pin-only successor."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from core_runtime.bootstrap import profile_registry
from core_runtime import pack_control_v4
from core_runtime.bootstrap.profile_pack_pin_recovery import (
    pack_pin_predecessor_binding,
    require_exact_pack_pin_predecessor_binding,
    verified_pack_pin_successor_predecessor,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStoreConflict
from tobkiri_protocol.canonical import canonical_digest


def _profile(pin: str) -> dict[str, object]:
    return {
        "profile_id": "defaults",
        "display_name": "Defaults",
        "base": {"pack_id": "base"},
        "shell": {"pack_id": "shell"},
        "packs": [
            {"pack_id": "gateway", "artifact_digest": pin, "selected": True}
        ],
        "requested_edges": [],
        "authority_mode": "host",
    }


def _history(previous: dict[str, object], current: dict[str, object]) -> dict[str, object]:
    previous_digest = canonical_digest(previous)
    current_digest = canonical_digest(current)
    return {
        "profiles": [
            {
                "profile_id": "defaults",
                "tombstone": False,
                "current_revision": current_digest,
                "revisions": [
                    {"profile_revision": previous_digest, "profile": previous},
                    {
                        "profile_revision": current_digest,
                        "profile": current,
                        "parent_revision": previous_digest,
                    },
                ],
            }
        ]
    }


def _catalog(pin: str, kind: str = "normal_sandbox") -> SimpleNamespace:
    return SimpleNamespace(
        packs={
            "gateway": {
                "pack": {"artifact_digest": pin, "kind": kind}
            }
        }
    )


def _pointer(generation: int = 7) -> SimpleNamespace:
    payload = {
        "profile_id": "defaults",
        "profile_revision": "sha256:" + "5" * 64,
        "activation_id": "activation:defaults-12345678",
        "plan_digest": "sha256:" + "6" * 64,
        "lock_digest": "sha256:" + "7" * 64,
        "generation": generation,
    }
    return SimpleNamespace(
        to_dict=lambda: {**payload, "pointer_digest": canonical_digest(payload)}
    )


def test_accepts_direct_pack_pin_only_successor_matching_admitted_catalog() -> None:
    previous = _profile("sha256:" + "1" * 64)
    current = _profile("sha256:" + "2" * 64)

    result = verified_pack_pin_successor_predecessor(
        _history(previous, current),
        current,
        canonical_digest(previous),
        _catalog(current["packs"][0]["artifact_digest"]),
    )

    assert result == previous


def test_predecessor_binding_covers_active_pointer_and_registry_snapshot() -> None:
    previous = _profile("sha256:" + "1" * 64)
    current = _profile("sha256:" + "2" * 64)
    previous_digest = canonical_digest(previous)
    snapshot = {"generation": 12, **_history(previous, current)}
    registered = SimpleNamespace(
        profile_id="defaults",
        profile_revision=canonical_digest(current),
        profile=current,
    )
    pointer = _pointer()
    binding = pack_pin_predecessor_binding(
        pointer=pointer,
        active_definition_digest=previous_digest,
        registry_snapshot=snapshot,
        registered=registered,
    )

    require_exact_pack_pin_predecessor_binding(
        binding,
        pointer=pointer,
        active_definition_digest=previous_digest,
        registry_snapshot=snapshot,
        registered=registered,
        catalog=_catalog(current["packs"][0]["artifact_digest"]),
    )
    with pytest.raises(ProfileDefinitionStoreConflict):
        require_exact_pack_pin_predecessor_binding(
            binding,
            pointer=_pointer(generation=8),
            active_definition_digest=previous_digest,
            registry_snapshot=snapshot,
            registered=registered,
            catalog=_catalog(current["packs"][0]["artifact_digest"]),
        )
    changed_snapshot = {**snapshot, "generation": 13}
    with pytest.raises(ProfileDefinitionStoreConflict):
        require_exact_pack_pin_predecessor_binding(
            binding,
            pointer=pointer,
            active_definition_digest=previous_digest,
            registry_snapshot=changed_snapshot,
            registered=registered,
            catalog=_catalog(current["packs"][0]["artifact_digest"]),
        )


@pytest.mark.parametrize("stale", [False, True])
def test_pin_recovery_registration_rechecks_current_candidate_cas(
    tmp_path, monkeypatch, stale: bool
) -> None:
    previous = _profile("sha256:" + "1" * 64)
    current = _profile("sha256:" + "2" * 64)
    previous_digest = canonical_digest(previous)
    registered = SimpleNamespace(
        profile_id="defaults",
        profile_revision=canonical_digest(current),
        profile=current,
        tombstone=False,
    )
    snapshot = {"generation": 12, **_history(previous, current)}
    pointer = _pointer()
    binding = pack_pin_predecessor_binding(
        pointer=pointer,
        active_definition_digest=previous_digest,
        registry_snapshot=snapshot,
        registered=registered,
    )
    if stale:
        snapshot = {**snapshot, "generation": 13}
    calls: list[tuple[str, str, int]] = []

    class FakeDefinitions:
        def __init__(self, _root):
            pass

        def snapshot(self):
            return snapshot

        def get_profile(self, _profile_id, **_kwargs):
            return registered

        def require_current_profile(
            self, profile_id, *, expected_profile_revision, expected_store_generation
        ):
            calls.append(
                (profile_id, expected_profile_revision, expected_store_generation)
            )
            return registered

    tmp_path.joinpath("profiles").mkdir()
    monkeypatch.setattr(profile_registry, "ProfileDefinitionStore", FakeDefinitions)
    monkeypatch.setattr(
        profile_registry,
        "ActiveProfileStore",
        lambda _root: SimpleNamespace(load=lambda **_kwargs: pointer),
    )

    if stale:
        with pytest.raises(ProfileDefinitionStoreConflict):
            profile_registry.register_bootstrap_definition(
                tmp_path,
                current,
                approved_pack_pin_predecessor=binding,
                admitted_catalog=_catalog(current["packs"][0]["artifact_digest"]),
            )
        assert calls == []
    else:
        profile_registry.register_bootstrap_definition(
            tmp_path,
            current,
            approved_pack_pin_predecessor=binding,
            admitted_catalog=_catalog(current["packs"][0]["artifact_digest"]),
        )
        assert calls == [
            ("defaults", canonical_digest(current), 12),
        ]


def test_review_accepts_pin_only_predecessor_when_active_needs_reconfirmation(
    tmp_path, monkeypatch
) -> None:
    previous = _profile("sha256:" + "1" * 64)
    current = _profile("sha256:" + "2" * 64)
    previous_digest = canonical_digest(previous)
    current_digest = canonical_digest(current)
    pointer = SimpleNamespace(
        profile_id="defaults",
        profile_revision="sha256:" + "5" * 64,
        activation_id="activation:defaults-12345678",
        plan_digest="sha256:" + "6" * 64,
        lock_digest="sha256:" + "7" * 64,
    )
    store_snapshot = {
        "generation": 12,
        "profiles": [
            {
                "profile_id": "defaults",
                "tombstone": False,
                "current_revision": current_digest,
                "revisions": [
                    {"profile_revision": previous_digest, "profile": previous},
                    {
                        "profile_revision": current_digest,
                        "profile": current,
                        "parent_revision": previous_digest,
                    },
                ],
            }
        ],
    }
    registered = SimpleNamespace(profile=current, tombstone=False)
    active_error = RuntimeError("reconfirmation required")
    active_error.verified_profile_definition_digest = previous_digest
    active_error.verified_activation_identity = (
        pointer.profile_revision,
        pointer.activation_id,
        pointer.plan_digest,
        pointer.lock_digest,
    )
    active_error.verified_profile = previous
    catalog = SimpleNamespace(
        profiles={"defaults": previous},
        packs={
            "gateway": {
                "pack": {
                    "artifact_digest": current["packs"][0]["artifact_digest"],
                    "kind": "normal_sandbox",
                },
                "requirements": {"pack_dependencies": []},
            },
            "base": {"requirements": {"pack_dependencies": []}},
            "shell": {"requirements": {"pack_dependencies": []}},
        },
    )

    class FakeDefinitions:
        def __init__(self, _root):
            pass

        def get_profile(self, _profile_id):
            return registered

        def snapshot(self):
            return deepcopy(store_snapshot)

    class FakeAuthority:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeRuntime:
        @staticmethod
        def activation_store(**_kwargs):
            def load():
                raise active_error

            return SimpleNamespace(load_active_snapshot=load)

        @staticmethod
        def is_reconfirmation_required(error):
            return error is active_error

        @staticmethod
        def catalog_with_profiles(selected_catalog, profiles):
            return SimpleNamespace(packs=selected_catalog.packs, profiles=profiles)

        @staticmethod
        def denied(message):
            return RuntimeError(message)

    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "active.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(profile_registry, "ProfileDefinitionStore", FakeDefinitions)
    monkeypatch.setattr(profile_registry, "AuthorityStore", FakeAuthority)
    monkeypatch.setattr(
        profile_registry,
        "ActiveProfileStore",
        lambda _root: SimpleNamespace(load=lambda **_kwargs: pointer),
    )
    monkeypatch.setattr(
        pack_control_v4,
        "catalog_with_admitted_pack_closure",
        lambda selected, _ids, artifact_pins: (
            selected if artifact_pins["gateway"] == current["packs"][0]["artifact_digest"]
            else (_ for _ in ()).throw(AssertionError("wrong admitted pin")),
            (),
        ),
        raising=False,
    )

    reviewed, additional = profile_registry.bootstrap_review_catalog(
        runtime=FakeRuntime(),
        catalog=catalog,
        user_data=tmp_path,
        profile_id="defaults",
        include_source_additions=True,
    )

    assert reviewed.profiles["defaults"] == current
    assert additional == ()


@pytest.mark.parametrize("include_source_additions", [False, True])
@pytest.mark.parametrize("admitted", [False, True])
def test_review_keeps_same_definition_exact_gateway_pin_over_bundled_baseline(
    tmp_path, monkeypatch, include_source_additions: bool, admitted: bool,
) -> None:
    """An existing Pack ID cannot substitute its bundled predecessor for a pin."""
    pack_id = "rumi_ai_gateway_pack"
    selected_digest = "sha256:" + "8" * 64
    bundled_digest = "sha256:" + "c" * 64
    profile = _profile(selected_digest)
    profile["packs"][0]["pack_id"] = pack_id
    source = deepcopy(profile)
    source["packs"][0]["artifact_digest"] = None
    # A real packaged addition makes include-source review substantive while
    # retaining the already selected exact pin and unchanged authority mode.
    if include_source_additions:
        source["packs"].append({
            "pack_id": "new-provider", "artifact_digest": None, "selected": True,
        })
    pointer = SimpleNamespace(
        profile_id="defaults", profile_revision="sha256:" + "5" * 64,
        activation_id="activation:defaults-12345678",
        plan_digest="sha256:" + "6" * 64, lock_digest="sha256:" + "7" * 64,
    )
    catalog = SimpleNamespace(
        profiles={"defaults": source},
        packs={
            pack_id: {
                "pack": {"artifact_digest": bundled_digest, "kind": "host_extension"},
                "requirements": {"pack_dependencies": []},
            },
            "base": {"requirements": {"pack_dependencies": []}},
            "shell": {"requirements": {"pack_dependencies": []}},
            "new-provider": {"requirements": {"pack_dependencies": []}},
        },
    )
    active = SimpleNamespace(
        resolved=SimpleNamespace(
            profile=deepcopy(profile),
            plan={
                "profile_definition_digest": canonical_digest(profile),
                "profile_revision": pointer.profile_revision,
                "plan_digest": pointer.plan_digest,
                "bindings": [{"pack_id": pack_id, "artifact_digest": selected_digest}],
            },
            lock={"lock_digest": pointer.lock_digest},
        ),
        activation={"activation_id": pointer.activation_id},
    )

    class FakeDefinitions:
        def __init__(self, _root):
            pass

        def get_profile(self, _profile_id):
            return SimpleNamespace(profile=deepcopy(profile))

        def snapshot(self):
            pytest.fail("same active definition must not use pin-successor recovery")

    class FakeAuthority:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    runtime = SimpleNamespace(
        activation_store=lambda **_kwargs: SimpleNamespace(
            load_active_snapshot=lambda: active
        ),
        catalog_with_profiles=lambda selected, profiles: SimpleNamespace(
            packs=selected.packs, profiles=profiles
        ),
        denied=RuntimeError,
    )
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "active.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(profile_registry, "ProfileDefinitionStore", FakeDefinitions)
    monkeypatch.setattr(profile_registry, "AuthorityStore", FakeAuthority)
    monkeypatch.setattr(
        profile_registry, "ActiveProfileStore",
        lambda _root: SimpleNamespace(load=lambda **_kwargs: pointer),
    )
    calls = []

    def exact_admitted_closure(selected, wanted_ids, *, artifact_pins):
        assert selected is catalog
        assert wanted_ids == sorted({"base", "shell", pack_id})
        assert artifact_pins == {pack_id: selected_digest}
        calls.append((wanted_ids, artifact_pins))
        if not admitted:
            raise pack_control_v4.PackControlDigestMismatch("selected pin not admitted")
        result = deepcopy(selected)
        result.packs[pack_id]["pack"]["artifact_digest"] = selected_digest
        return result, set(wanted_ids)

    monkeypatch.setattr(
        pack_control_v4, "catalog_with_admitted_pack_closure", exact_admitted_closure,
    )
    options = dict(
        runtime=runtime, catalog=catalog, user_data=tmp_path, profile_id="defaults",
        include_source_additions=include_source_additions,
    )
    if not admitted:
        with pytest.raises(pack_control_v4.PackControlDigestMismatch):
            profile_registry.bootstrap_review_catalog(**options)
    else:
        reviewed, additional = profile_registry.bootstrap_review_catalog(**options)
        expected = deepcopy(profile)
        if include_source_additions:
            expected["packs"].append(source["packs"][-1])
        assert reviewed.profiles["defaults"] == expected
        assert reviewed.packs[pack_id]["pack"]["artifact_digest"] == selected_digest
        assert additional == ()
        assert catalog.packs[pack_id]["pack"]["artifact_digest"] == bundled_digest
        assert active.resolved.profile == profile
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "unrelated_field",
        "pack_order",
        "added_pack",
        "wrong_parent",
        "wrong_pin",
        "application_artifact",
        "application_manifest",
    ],
)
def test_rejects_non_pin_or_unadmitted_registry_successors(mutation: str) -> None:
    previous = _profile("sha256:" + "1" * 64)
    current = _profile("sha256:" + "2" * 64)
    registry = _history(previous, current)
    if mutation == "unrelated_field":
        current["display_name"] = "Edited"
        registry = _history(previous, current)
    elif mutation == "pack_order":
        current["packs"].append({"pack_id": "other", "artifact_digest": "sha256:" + "3" * 64})
        previous["packs"].append({"pack_id": "other", "artifact_digest": "sha256:" + "4" * 64})
        current["packs"].reverse()
        registry = _history(previous, current)
    elif mutation == "added_pack":
        current["packs"].append({"pack_id": "other", "artifact_digest": "sha256:" + "3" * 64})
        registry = _history(previous, current)
    elif mutation == "wrong_parent":
        registry["profiles"][0]["revisions"][1]["parent_revision"] = "sha256:" + "9" * 64
    elif mutation == "application_artifact":
        current["packs"][0]["role"] = "application"
        previous["packs"][0]["role"] = "application"
        registry = _history(previous, current)
    with pytest.raises(ProfileDefinitionStoreConflict, match="verified Pack pin update"):
        verified_pack_pin_successor_predecessor(
            registry,
            current,
            canonical_digest(previous),
            _catalog(
                "sha256:" + "8" * 64
                if mutation == "wrong_pin"
                else current["packs"][0]["artifact_digest"],
                "application" if mutation == "application_manifest" else "normal_sandbox",
            ),
        )

