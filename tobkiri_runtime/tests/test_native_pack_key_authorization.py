"""Native review lifecycle with actual temporary additive-policy persistence.

Public test-vector keys and injected Host-context loaders keep these tests
independent of Native UI, running owners and credential material. Synthetic
spent-budget rows live in real temporary AuthorityStores and must stay intact.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.native_pack_key_authorization import NativePackKeyVersionCeremony
from core_runtime import native_pack_onboarding_http as native_http
from core_runtime.pack_artifact_integrity import (
    _policy_digest,
    host_policy_identity,
    read_host_policy_snapshot,
)
from core_runtime.publisher_key_versions import FIELD, resolve_publisher_key
from tests.test_native_pack_publisher_key_versions import (
    ADAPTER, ARTIFACT, GATEWAY, NEW_FINGERPRINT, NEW_ID, NEW_PEM,
    OLD_FINGERPRINT, OLD_ID, OLD_PEM, PUBLISHER,
    SIGNED_MANIFEST, _policy,
)
from tests.test_external_pack_catalog_v4 import _signed_external_pack


pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Host policy descriptor-chain fixture requires POSIX",
)
COMPATIBILITY = "sha256:" + "c" * 64
PREDECESSOR = "sha256:" + "d" * 64


class Clock:
    """Keep exact expiry boundaries deterministic without sleeps."""

    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now


class ReviewOwner:
    """Inject backend-owned context and temporary policy/budget storage."""

    def __init__(self, root: Path) -> None:
        self.clock = Clock()
        self.ceremony = NativePackKeyVersionCeremony(clock_ms=self.clock)
        self.context: dict[str, Any] = {
            "host_owner_id": "sha256:" + "1" * 64,
            "profile_revision": "sha256:" + "2" * 64,
            "plan_digest": "sha256:" + "3" * 64,
            "activation_id": "synthetic-activation",
            "security_epoch": 1,
            "fencing_token": 5,
        }
        policy_root = root / "host-policy"
        policy_root.mkdir(mode=0o700)
        self.path = policy_root / "publisher-trust.json"
        self.initial_policy = _policy(additive=False)
        self.path.write_text(json.dumps(self.initial_policy))
        self.path.chmod(0o600)
        self.preview = {
            "pack_id": ADAPTER, "version": "1.0.1", "publisher_id": PUBLISHER,
            "key_id": NEW_ID, "artifact_digest": ARTIFACT,
            "key_fingerprint": NEW_FINGERPRINT,
            "predecessor_artifact_digest": PREDECESSOR,
            "preview_digest": "sha256:" + "4" * 64,
        }
        self.primary = AuthorityStore(root / "authority/v4.sqlite3")
        self.witness = AuthorityStore(root / "credentials/witness.sqlite3")
        self.effect_id = "synthetic-generation-budget"
        for store in (self.primary, self.witness):
            row = {
                "effect_id": self.effect_id, "state": "active",
                "kind": "credential_generation_budget_v1", "attempts": [],
            }
            store.create_host_pending_effect(self.effect_id, row)
            store.compare_and_swap_host_pending_effect(
                self.effect_id, expected_revision=1,
                payload={**row, "attempts": ["synthetic-spent-attempt"]},
            )
        self.budgets_before = (
            self.primary.get_host_pending_effect(self.effect_id),
            self.witness.get_host_pending_effect(self.effect_id),
        )

    def current_context(self) -> dict[str, Any]:
        """Return a detached fresh view controlled by the Host fixture."""
        return deepcopy(self.context)

    def prepare(self) -> dict[str, Any]:
        """Prepare through the actual ceremony; do not authorize trust."""
        return self.ceremony.prepare(
            self.preview, trust_store_path=self.path, public_key_pem=NEW_PEM,
            signed_manifest_digest=SIGNED_MANIFEST,
            compatibility_digest=COMPATIBILITY,
            context_loader=self.current_context,
        )

    def authorize(
        self, section: dict[str, Any], *, preview: dict[str, Any] | None = None,
        challenge: Any = None, digest: Any = None,
    ) -> tuple[int, str]:
        """Echo the reviewed facts using the same backend-owned context loader."""
        return self.ceremony.authorize(
            self.preview if preview is None else preview,
            challenge=section["challenge"] if challenge is None else challenge,
            authorization_digest=(
                section["authorization_digest"] if digest is None else digest
            ),
            trust_store_path=self.path, context_loader=self.current_context,
        )

    def assert_budgets_unchanged(self) -> None:
        """Verify both actual persistence owners retain their consumed row."""
        assert (
            self.primary.get_host_pending_effect(self.effect_id),
            self.witness.get_host_pending_effect(self.effect_id),
        ) == self.budgets_before

    def close(self) -> None:
        """Release both isolated persistence owners."""
        self.primary.close()
        self.witness.close()


@pytest.fixture
def owner(tmp_path: Path) -> Iterator[ReviewOwner]:
    """Use actual private temporary policy persistence for every lifecycle case."""
    value = ReviewOwner(tmp_path)
    try:
        yield value
    finally:
        value.assert_budgets_unchanged()
        value.close()


def test_exact_native_review_adds_one_scope_and_retains_old_gateway(
    owner: ReviewOwner,
) -> None:
    """Prepare writes nothing; one confirm appends exact new trust only."""
    before = owner.path.read_bytes()
    prepared = owner.prepare()
    section = prepared["key_version_authorization"]
    assert owner.path.read_bytes() == before
    assert section["predecessor_artifact_digest"] == PREDECESSOR
    assert section["compatibility_digest"] == COMPATIBILITY
    assert section["host_owner_id"] == owner.context["host_owner_id"]
    identity = owner.authorize(section)
    after = read_host_policy_snapshot(owner.path)
    assert host_policy_identity(after) == identity
    assert identity[0] == owner.initial_policy["policy_generation"] + 1
    assert after["publishers"] == owner.initial_policy["publishers"]
    assert after["install_records"] == owner.initial_policy["install_records"]
    assert resolve_publisher_key(
        after, publisher_id=PUBLISHER, key_id=NEW_ID, pack_id=ADAPTER,
        artifact_digest=ARTIFACT, signed_manifest_digest=SIGNED_MANIFEST,
    )
    assert resolve_publisher_key(
        after, publisher_id=PUBLISHER, key_id=OLD_ID, pack_id=GATEWAY,
        artifact_digest="", signed_manifest_digest="",
    )
    committed = owner.path.read_bytes()
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == committed


def test_authorized_exact_scope_needs_no_second_key_review(owner: ReviewOwner) -> None:
    """An already trusted exact artifact does not mint another pending review."""
    section = owner.prepare()["key_version_authorization"]
    owner.authorize(section)
    before = owner.path.read_bytes()
    assert owner.prepare() == owner.preview
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["legacy", "new-publisher"])
def test_existing_onboarding_paths_need_no_additive_review(
    owner: ReviewOwner, kind: str,
) -> None:
    """Legacy trust and first-publisher onboarding keep their separate ceremony."""
    before = owner.path.read_bytes()
    if kind == "legacy":
        owner.preview.update(
            pack_id=GATEWAY, key_id=OLD_ID, key_fingerprint=OLD_FINGERPRINT,
        )
    else:
        owner.preview["publisher_id"] = "new.publisher"

    def unavailable_context() -> dict[str, Any]:
        raise AssertionError("no additive review should request Authority context")

    result = owner.ceremony.prepare(
        owner.preview, trust_store_path=owner.path,
        public_key_pem=OLD_PEM if kind == "legacy" else NEW_PEM,
        signed_manifest_digest=SIGNED_MANIFEST,
        compatibility_digest=COMPATIBILITY, context_loader=unavailable_context,
    )
    assert result == owner.preview
    assert owner.path.read_bytes() == before


def test_new_key_without_retained_predecessor_is_denied_before_review(
    owner: ReviewOwner,
) -> None:
    """Additional keys cannot become first-admission publisher replacements."""
    owner.preview.pop("predecessor_artifact_digest")
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="retained predecessor"):
        owner.prepare()
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize(("field", "value"), [
    ("host_owner_id", "not-a-host-digest"),
    ("profile_revision", "sha256:" + "G" * 64),
    ("activation_id", "control\ncharacter"),
    ("security_epoch", True),
    ("fencing_token", 0),
])
def test_invalid_backend_context_cannot_prepare_key_trust(
    owner: ReviewOwner, field: str, value: Any,
) -> None:
    """Even the trusted loader must return a complete, strict context shape."""
    owner.context[field] = value
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="context|activation|fence"):
        owner.prepare()
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_context_fields_cannot_be_omitted_or_extended(
    owner: ReviewOwner, change: str,
) -> None:
    """A partial or invented owner tuple cannot become a valid Native review."""
    if change == "missing":
        owner.context.pop("host_owner_id")
    else:
        owner.context["approved"] = True
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="incomplete"):
        owner.prepare()
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("field", [
    "host_owner_id", "profile_revision", "plan_digest", "activation_id",
    "security_epoch", "fencing_token",
])
def test_fresh_authority_change_denies_and_consumes_exact_review(
    owner: ReviewOwner, field: str,
) -> None:
    """A valid echo cannot carry trust across any captured Authority boundary."""
    section = owner.prepare()["key_version_authorization"]
    original = owner.context[field]
    owner.context[field] = (
        original + 1 if isinstance(original, int)
        else "changed-activation" if field == "activation_id"
        else "sha256:" + "9" * 64
    )
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="authority changed"):
        owner.authorize(section)
    owner.context[field] = original
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize(("field", "value"), [
    ("pack_id", GATEWAY),
    ("artifact_digest", "sha256:" + "e" * 64),
    ("key_id", OLD_ID),
    ("key_fingerprint", OLD_FINGERPRINT),
    ("publisher_id", "different.publisher"),
    ("preview_digest", "sha256:" + "f" * 64),
    ("predecessor_artifact_digest", "sha256:" + "e" * 64),
    ("version", "1.0.2"),
])
def test_cross_preview_reuse_denies_and_consumes_review(
    owner: ReviewOwner, field: str, value: str,
) -> None:
    """The challenge cannot authorize another Pack, artifact or predecessor."""
    section = owner.prepare()["key_version_authorization"]
    changed = {**owner.preview, field: value}
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="changed after confirmation"):
        owner.authorize(section, preview=changed)
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


def test_wrong_authorization_digest_consumes_review_without_policy_write(
    owner: ReviewOwner,
) -> None:
    """A known challenge with a mismatched review echo is permanently retired."""
    section = owner.prepare()["key_version_authorization"]
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="changed after confirmation"):
        owner.authorize(section, digest="sha256:" + "f" * 64)
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("bad_digest", [None, True, "", {}, []])
def test_missing_or_malformed_known_challenge_echo_consumes_review(
    owner: ReviewOwner, bad_digest: Any,
) -> None:
    """Incomplete old Native echoes cannot retain an additive approval retry."""
    section = owner.prepare()["key_version_authorization"]
    before = owner.path.read_bytes()
    with pytest.raises(ValueError):
        owner.ceremony.authorize(
            owner.preview, challenge=section["challenge"],
            authorization_digest=bad_digest, trust_store_path=owner.path,
            context_loader=owner.current_context,
        )
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


def test_review_expiry_at_exact_boundary_denies_and_consumes(
    owner: ReviewOwner,
) -> None:
    """No sleeps or wall-clock races are needed to test the expiry boundary."""
    section = owner.prepare()["key_version_authorization"]
    owner.clock.now = section["expires_at_ms"]
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        owner.authorize(section)
    owner.clock.now = section["issued_at_ms"]
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


def test_ceremony_restart_invalidates_pending_review(owner: ReviewOwner) -> None:
    """A process-local challenge cannot be transferred to a replacement owner."""
    section = owner.prepare()["key_version_authorization"]
    owner.ceremony = NativePackKeyVersionCeremony(clock_ms=owner.clock)
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="missing"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


def test_policy_cas_change_denies_and_consumes_review(owner: ReviewOwner) -> None:
    """A fresh Native echo cannot append to a policy changed after review."""
    section = owner.prepare()["key_version_authorization"]
    changed = deepcopy(owner.initial_policy)
    changed["policy_generation"] += 1
    changed["policy_digest"] = _policy_digest(changed)
    owner.path.write_text(json.dumps(changed))
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="snapshot is stale"):
        owner.authorize(section)
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


def test_two_native_confirmations_can_append_only_once(owner: ReviewOwner) -> None:
    """Concurrent confirmations share one atomic process-local consumption."""
    section = owner.prepare()["key_version_authorization"]

    def confirm() -> str:
        try:
            owner.authorize(section)
        except ValueError:
            return "denied"
        return "authorized"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: confirm(), range(2)))
    assert sorted(results) == ["authorized", "denied"]
    policy = read_host_policy_snapshot(owner.path)
    assert len(policy[FIELD]["consumed_requests"]) == 1


def test_pending_review_capacity_is_bounded_and_expiry_frees_capacity(
    owner: ReviewOwner,
) -> None:
    """Native review state cannot grow without a fixed owner budget."""
    for _ in range(64):
        owner.prepare()
    before = owner.path.read_bytes()
    with pytest.raises(ValueError, match="busy"):
        owner.prepare()
    owner.clock.now += 300_000
    assert "key_version_authorization" in owner.prepare()
    assert owner.path.read_bytes() == before


class NativeHandler:
    """Record the private route's port calls without starting an HTTP server."""

    def __init__(
        self, owner: ReviewOwner, *, body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None, authenticated: bool = True,
    ) -> None:
        self.headers = headers or {}
        self.body = body or {}
        self.authenticated = authenticated
        self._native_pack_key_ceremony = owner.ceremony
        self._runtime_refresh = None
        self._policy_path = owner.path
        self.parsed = 0
        self.discarded = 0
        self.status = 0

    def _native_pack_launcher_authenticated(self) -> bool:
        return self.authenticated

    def _native_pack_trust_store(self) -> Path:
        return self._policy_path

    def _discard_request_body(self) -> None:
        self.discarded += 1

    def _parse_object_body(self) -> dict[str, Any]:
        self.parsed += 1
        return self.body

    def _send_response(self, _response: Any, status: int = 200) -> None:
        self.status = status


@pytest.mark.parametrize(("headers", "authenticated"), [
    ({"Origin": "https://browser.example"}, True),
    ({"Origin": ""}, True),
    ({}, False),
])
def test_private_onboarding_denies_browser_or_unauthenticated_before_body(
    owner: ReviewOwner, headers: dict[str, str], authenticated: bool,
) -> None:
    """Private onboarding never treats a browser request as Native consent."""
    handler = NativeHandler(owner, headers=headers, authenticated=authenticated)
    before = owner.path.read_bytes()
    native_http.handle_native_pack_onboarding(handler, commit=True)
    assert handler.status == 401
    assert handler.parsed == 0
    assert handler.discarded == 1
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("failure", [
    "fresh-preview", "preview-digest", "malformed-echo", "partial-echo",
    "extra-approval", "invalid-path",
])
def test_private_confirmation_failures_retire_known_review_before_retry(
    owner: ReviewOwner, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """Validation errors at the HTTP seam cannot leave a known approval alive."""
    from core_runtime import native_pack_onboarding as onboarding

    section = owner.prepare()["key_version_authorization"]
    body = {
        "source_root": str(owner.path.parent / "candidate"),
        "public_key_path": str(owner.path.parent / "publisher-public.pem"),
        "preview_digest": owner.preview["preview_digest"],
        "key_version_authorization_challenge": section["challenge"],
        "key_version_authorization_digest": section["authorization_digest"],
    }
    if failure == "preview-digest":
        body["preview_digest"] = "sha256:" + "f" * 64
    elif failure == "malformed-echo":
        body["key_version_authorization_digest"] = None
    elif failure == "partial-echo":
        body.pop("key_version_authorization_digest")
    elif failure == "extra-approval":
        body["approved"] = True
    elif failure == "invalid-path":
        body["source_root"] = "relative/candidate"

    def fresh_preview(_source: Path, _key: Path, **_arguments: Any) -> dict[str, Any]:
        if failure == "fresh-preview":
            raise ValueError("synthetic artifact changed")
        return dict(owner.preview)

    def unexpected_commit(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid confirmation must stop before admission")

    monkeypatch.setattr(onboarding, "preview_native_signed_pack", fresh_preview)
    monkeypatch.setattr(onboarding, "commit_native_signed_pack", unexpected_commit)
    monkeypatch.setattr(native_http, "_current_review_context", owner.current_context)
    handler = NativeHandler(owner, body=body)
    before = owner.path.read_bytes()
    native_http.handle_native_pack_onboarding(handler, commit=True)
    assert handler.status in {400, 409}
    with pytest.raises(ValueError, match="consumed"):
        owner.authorize(section)
    assert owner.path.read_bytes() == before


def test_private_confirmation_binds_admission_to_written_policy_identity(
    owner: ReviewOwner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private route writes exact trust before handing its CAS to admission."""
    from core_runtime import native_pack_onboarding as onboarding

    section = owner.prepare()["key_version_authorization"]
    body = {
        "source_root": str(owner.path.parent / "candidate"),
        "public_key_path": str(owner.path.parent / "publisher-public.pem"),
        "preview_digest": owner.preview["preview_digest"],
        "key_version_authorization_challenge": section["challenge"],
        "key_version_authorization_digest": section["authorization_digest"],
    }
    observed: list[tuple[int, str]] = []

    def admission(_source: Path, _key: Path, **arguments: Any) -> dict[str, Any]:
        policy = read_host_policy_snapshot(owner.path)
        identity = arguments["key_version_policy_identity"]
        assert identity == host_policy_identity(policy)
        assert arguments["expected_preview_digest"] == owner.preview["preview_digest"]
        assert resolve_publisher_key(
            policy, publisher_id=PUBLISHER, key_id=NEW_ID, pack_id=ADAPTER,
            artifact_digest=ARTIFACT, signed_manifest_digest=SIGNED_MANIFEST,
        )
        observed.append(identity)
        return {
            "pack_id": ADAPTER, "artifact_digest": ARTIFACT,
            "publisher_id": PUBLISHER,
        }

    monkeypatch.setattr(
        onboarding, "preview_native_signed_pack",
        lambda _source, _key, **_arguments: dict(owner.preview),
    )
    monkeypatch.setattr(onboarding, "commit_native_signed_pack", admission)
    monkeypatch.setattr(native_http, "_current_review_context", owner.current_context)
    handler = NativeHandler(owner, body=body)
    native_http.handle_native_pack_onboarding(handler, commit=True)
    assert handler.status == 200
    assert len(observed) == 1
    committed = owner.path.read_bytes()
    native_http.handle_native_pack_onboarding(handler, commit=True)
    assert handler.status == 409
    assert len(observed) == 1
    assert owner.path.read_bytes() == committed


def test_old_native_commit_cannot_add_selected_untrusted_publisher_key(
    owner: ReviewOwner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A real signed artifact still needs additive consent absent from old Native."""
    runtime_data = tmp_path / "isolated-runtime-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(runtime_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(runtime_data))
    signing_key = Ed25519PrivateKey.generate()
    source_root = tmp_path / "signed-selection"
    source_root.mkdir()
    source, _unused_fixture_policy = _signed_external_pack(
        source_root, publisher_id=PUBLISHER, signing_key=signing_key,
    )
    public_key = source_root / "separate-public.pem"
    public_key.write_bytes(signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    del signing_key
    from core_runtime.native_pack_onboarding import preview_signed_pack

    preview = preview_signed_pack(source, public_key)
    handler = NativeHandler(owner, body={
        "source_root": str(source), "public_key_path": str(public_key),
        "preview_digest": preview["preview_digest"],
    })
    before = owner.path.read_bytes()
    native_http.handle_native_pack_onboarding(handler, commit=True)
    assert handler.status == 409
    assert owner.path.read_bytes() == before
    assert FIELD not in read_host_policy_snapshot(owner.path)
