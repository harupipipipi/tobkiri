"""Bootstrap review preserves authenticated external optional Pack selections."""

from pathlib import Path
import json
import re

import pytest

from core_runtime.bootstrap.profile_capture import (
    capture_active_profile,
    capture_default_profile,
    prepare_bootstrap_profile_review,
    prepare_default_profile_confirmation,
)
from core_runtime.external_pack_catalog_v4 import (
    ExternalPackCatalogDenied,
    admit_signed_external_pack,
    load_external_pack_catalog,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from ecosystem.defaultspack.domain.runtime_v4 import ProfileResolutionDenied
from tests.test_external_pack_catalog_v4 import (
    PACK_ID,
    _capture_control_session,
    _invoke,
    _signed_external_pack,
)


def _enable_signed_external_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Select a real signed CAS Pack through install, approval, and enable."""
    # Match an installed runtime distribution without mutating the shared
    # interpreter. Admission still runs its real signature, policy, and CAS
    # verification, including compatibility against the project version.
    project = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    version = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    assert version is not None
    metadata_root = tmp_path / "distribution-metadata"
    distribution = metadata_root / "tobkiri_runtime.dist-info"
    distribution.mkdir(parents=True)
    (distribution / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: tobkiri-runtime\nVersion: {version[1]}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(metadata_root))
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    source, trust_store = _signed_external_pack(tmp_path)
    admit_signed_external_pack(source, trust_store_path=trust_store)
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    session = _capture_control_session()
    assert _invoke(session, "pack.install", {"pack_id": PACK_ID})["installed"]
    candidate = _invoke(session, "approval.candidate", {"pack_id": PACK_ID})
    assert _invoke(session, "approval.approve", {
        "pack_id": PACK_ID, "candidate_id": candidate["candidate_id"],
    })["approved"]
    assert _invoke(session, "pack.enable", {"pack_id": PACK_ID})["enabled"]
    active = capture_active_profile()
    assert PACK_ID in {row["pack_id"] for row in active.resolved.plan["bindings"]}
    return user_data


@pytest.mark.parametrize("include_source_additions", [False, True])
def test_bootstrap_review_verifies_external_closure_before_binding_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    include_source_additions: bool,
) -> None:
    """External operation bindings must be verified in the admitted catalog."""
    user_data = _enable_signed_external_pack(tmp_path, monkeypatch)
    pointer_path = user_data / "profiles" / "active.json"
    activation_path = (
        user_data / "workspaces" / "defaults" / "activation" / "active.json"
    )
    before = (pointer_path.read_bytes(), activation_path.read_bytes())
    definitions = ProfileDefinitionStore(user_data).snapshot()

    if include_source_additions:
        # No packaged successor: keep the normal no-op denial reachable.
        with pytest.raises(ProfileResolutionDenied, match="requires reconfirmation"):
            prepare_bootstrap_profile_review(include_source_additions=True)
    else:
        reviewed, confirmation = prepare_bootstrap_profile_review()
        assert PACK_ID in reviewed.packs
        assert PACK_ID in {
            row["pack_id"] for row in reviewed.profiles["defaults"]["packs"]
        }
        assert confirmation["profile_id"] == "defaults"

    assert (pointer_path.read_bytes(), activation_path.read_bytes()) == before
    assert ProfileDefinitionStore(user_data).snapshot() == definitions


@pytest.mark.parametrize("tamper_target", ["catalog_hmac", "cas_artifact"])
def test_bootstrap_review_does_not_carry_tampered_external_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper_target: str,
) -> None:
    """Review ordering must preserve authenticated catalog/CAS denials."""
    user_data = _enable_signed_external_pack(tmp_path, monkeypatch)
    pointer_path = user_data / "profiles" / "active.json"
    activation_path = (
        user_data / "workspaces" / "defaults" / "activation" / "active.json"
    )
    before = (pointer_path.read_bytes(), activation_path.read_bytes())
    if tamper_target == "catalog_hmac":
        path = user_data / "pack_control" / "external_normal_pack_catalog.v4.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        catalog["signature"] = "0" * 64
        path.write_text(json.dumps(catalog), encoding="utf-8")
    else:
        path = load_external_pack_catalog().roots[PACK_ID] / "runtime" / "echo.py"
        path.chmod(0o600)
        path.write_text(
            path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8"
        )

    with pytest.raises(ExternalPackCatalogDenied):
        prepare_bootstrap_profile_review()
    assert (pointer_path.read_bytes(), activation_path.read_bytes()) == before
