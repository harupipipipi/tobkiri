"""Cross-language and fail-closed tests for Defaults setup v4."""

from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest

from ecosystem.defaultspack.defaultspack.setup_contract import (
    validate_defaults_setup_payload,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    create_defaultspack_kernel,
)
from core_runtime.panel_auth import PanelAuthManager, reset_panel_auth_manager_for_tests
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import SchemaValidationError


pytestmark = pytest.mark.contract


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = RUNTIME_ROOT / "tobkiri_protocol" / "fixtures"
CANONICAL_FIXTURE = FIXTURE_ROOT / "defaults_setup_v4.canonical.json"
PRE_FIX_BINDING_FIXTURE = FIXTURE_ROOT / "defaults_setup_v4.pre_fix_binding_shape.json"


def _fixture() -> dict[str, Any]:
    return json.loads(CANONICAL_FIXTURE.read_text(encoding="utf-8"))


def _resign(payload: dict[str, Any]) -> None:
    confirmation = payload["recommended_default_profile"]["confirmation"]
    confirmation["confirmation_digest"] = canonical_digest(
        {key: value for key, value in confirmation.items() if key != "confirmation_digest"}
    )


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [] if not value else [_shape(value[0])]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    return type(value).__name__


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_canonical_fixture_is_schema_and_semantically_exact() -> None:
    fixture = _fixture()
    assert validate_defaults_setup_payload(fixture) == fixture


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("unknown", True),
        lambda payload: payload["recommended_default_profile"].__setitem__("available", False),
        lambda payload: payload["recommended_default_profile"]["confirmation"].__setitem__(
            "security_epoch", "1"
        ),
        lambda payload: payload["recommended_default_profile"]["confirmation"]["bindings"][
            0
        ].__setitem__("variant_id", 7),
        lambda payload: payload["recommended_default_profile"]["confirmation"]["bindings"][
            0
        ].__setitem__("adapter_digests", ["sha256:" + "0" * 64] * 2),
    ],
)
def test_schema_rejects_unknown_unavailable_and_wrong_type_payloads(mutate) -> None:
    payload = _fixture()
    mutate(payload)
    with pytest.raises(SchemaValidationError):
        validate_defaults_setup_payload(payload)


def test_pre_fix_binding_shape_reproduces_missing_executable_placement() -> None:
    payload = _fixture()
    pre_fix = json.loads(PRE_FIX_BINDING_FIXTURE.read_text(encoding="utf-8"))
    canonical_binding = payload["recommended_default_profile"]["confirmation"]["bindings"][0]
    missing = [key for key in canonical_binding if key not in pre_fix["binding"]]
    assert missing == pre_fix["missing_canonical_fields"]
    payload["recommended_default_profile"]["confirmation"]["bindings"][0] = pre_fix["binding"]
    _resign(payload)
    with pytest.raises(SchemaValidationError, match="required propert"):
        validate_defaults_setup_payload(payload)


def test_stale_digest_duplicate_binding_and_principal_tamper_fail_closed() -> None:
    stale = _fixture()
    stale["recommended_default_profile"]["confirmation"]["catalog_revision"] = "sha256:" + "0" * 64
    with pytest.raises(SchemaValidationError, match="confirmation_digest"):
        validate_defaults_setup_payload(stale)

    duplicate = _fixture()
    bindings = duplicate["recommended_default_profile"]["confirmation"]["bindings"]
    bindings.append(copy.deepcopy(bindings[0]))
    _resign(duplicate)
    with pytest.raises(SchemaValidationError, match="duplicate"):
        validate_defaults_setup_payload(duplicate)

    principal_tamper = _fixture()
    principal_tamper["recommended_default_profile"]["confirmation"]["bindings"][0][
        "function_principal"
    ]["parent_artifact_digest"] = "sha256:" + "0" * 64
    _resign(principal_tamper)
    with pytest.raises(SchemaValidationError, match="parent artifact"):
        validate_defaults_setup_payload(principal_tamper)


def test_real_pack_api_server_payload_matches_cross_language_fixture_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture the production setup HTTP response used by the frontend parser."""

    port = _free_port()
    monkeypatch.setenv("RUMI_PORT", str(port))
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user_data"))
    monkeypatch.setenv("RUMI_LOG_DIR", str(tmp_path / "logs"))
    reset_panel_auth_manager_for_tests(PanelAuthManager(bootstrap_secret="defaults-contract-test"))
    kernel = create_defaultspack_kernel()
    try:
        kernel.run_startup_until("api_init")
        remaining = kernel.run_startup_remaining()
        assert remaining["status"] == "setup_required"
        with urlopen(f"http://127.0.0.1:{port}/api/setup/packs", timeout=5) as response:
            setup = json.load(response)["data"]
        assert validate_defaults_setup_payload(setup) == setup
        assert _shape(setup) == _shape(_fixture())
        assert set(setup["recommended_default_profile"]["confirmation"]["bindings"][0]) == {
            "caller_function_id",
            "pack_id",
            "artifact_digest",
            "function_principal",
            "contract_id",
            "operation_id",
            "domain_kind",
            "executable_catalog_digest",
            "variant_id",
            "platform",
            "architecture",
            "runtime_abi",
            "backend",
            "execution_kind",
            "authority_reference",
            "requested_scope_digest",
            "adapter_digests",
        }
    finally:
        kernel.shutdown()
