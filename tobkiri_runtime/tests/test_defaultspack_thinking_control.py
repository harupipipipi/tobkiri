from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.ai_client.model_runtime_settings import (  # noqa: E402
    ModelRuntimeSettingsService,
)
from domain.ai_client.thinking_control import (  # noqa: E402
    normalize_thinking_control,
    parse_numeric_shorthand,
    serialize_thinking_control,
    validate_thinking_control,
)


NUMERIC_CONTRACT = {
    "supported": True,
    "input_schema": {
        "type": "number",
        "unit": "tokens",
        "min": 0,
        "max": 1_000_000_000,
        "step": 500,
    },
    "request_binding": {
        "path": "thinking.budget_tokens",
        "value": "$input",
    },
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1k", 1_000), ("1.5K", 1_500), ("1m", 1_000_000), ("1b", 1_000_000_000)],
)
def test_numeric_thinking_shorthand_uses_decimal_si(raw: str, expected: int) -> None:
    assert parse_numeric_shorthand(raw) == expected


@pytest.mark.parametrize("raw", ["1kk", "abc", "1.2.3k", "nan", "inf", "-1k"])
def test_numeric_thinking_shorthand_rejects_malformed_values(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_numeric_shorthand(raw)


def test_numeric_profile_validates_range_step_and_serializes_nested_path() -> None:
    valid = validate_thinking_control(NUMERIC_CONTRACT, "32k")
    wrong_type = validate_thinking_control(NUMERIC_CONTRACT, "high")
    wrong_step = validate_thinking_control(NUMERIC_CONTRACT, "1.2k")

    assert valid == {
        "valid": True,
        "raw": "32k",
        "normalized": 32_000,
        "input_type": "number",
        "unit": "tokens",
        "message": "",
    }
    assert wrong_type["valid"] is False
    assert wrong_step["valid"] is False
    assert serialize_thinking_control(NUMERIC_CONTRACT, valid["normalized"]) == {
        "thinking": {"budget_tokens": 32_000}
    }


def test_enum_and_text_profiles_own_their_allowed_values() -> None:
    enum_contract = {
        "supported": True,
        "input_schema": {
            "type": "enum",
            "values": ["low", "ultra super max"],
        },
    }
    text_contract = {
        "supported": True,
        "input_schema": {
            "type": "text",
            "pattern": r"[a-zA-Z0-9 _.-]{1,64}",
        },
    }

    assert validate_thinking_control(enum_contract, "ultra super max")["valid"] is True
    assert validate_thinking_control(enum_contract, "high")["valid"] is False
    assert validate_thinking_control(text_contract, "ultra super max")["valid"] is True
    assert validate_thinking_control(text_contract, "unsafe/field")["valid"] is False


def test_request_binding_rejects_non_thinking_payload_paths() -> None:
    contract = {
        **NUMERIC_CONTRACT,
        "request_binding": {"path": "messages.0.content", "value": "$input"},
    }

    with pytest.raises(ValueError, match="not allowed"):
        serialize_thinking_control(contract, 32_000)


def test_service_persists_raw_and_normalized_profile_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ModelRuntimeSettingsService(tmp_path)
    profile = {
        "profile_id": "example/numeric",
        "qualified_model_id": "example/numeric",
        "provider_id": "example",
        "model_id": "numeric",
        "supports_thinking": True,
        "thinking_control": NUMERIC_CONTRACT,
    }
    monkeypatch.setattr(service, "_list_profile_catalog", lambda **_kwargs: [profile])

    result = service.set_thinking_level("32k", scope="profile", profile_id="example/numeric")
    effective = service.get_effective_thinking_level("example/numeric")
    params = service.apply_thinking_control(
        "example/numeric", {"temperature": 0.2, "thinking_level": "32k"}
    )

    assert result["control"]["normalized"] == 32_000
    assert effective["level"] == 32_000
    assert effective["control"]["raw"] == "32k"
    assert params == {
        "temperature": 0.2,
        "thinking": {"budget_tokens": 32_000},
    }


def test_service_revalidates_client_stored_normalized_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ModelRuntimeSettingsService(tmp_path)
    profile = {
        "profile_id": "example/numeric",
        "qualified_model_id": "example/numeric",
        "provider_id": "example",
        "model_id": "numeric",
        "supports_thinking": True,
        "thinking_control": NUMERIC_CONTRACT,
    }
    monkeypatch.setattr(service, "_list_profile_catalog", lambda **_kwargs: [profile])
    service.update_settings(
        {
            "thinking_control_by_profile": {
                "example/numeric": {
                    "raw": "1.5k",
                    "normalized": 999_999_999,
                    "input_type": "number",
                }
            }
        }
    )

    effective = service.get_effective_thinking_level("example/numeric")

    assert effective["level"] == 1_500
    assert effective["control"]["normalized"] == 1_500


def test_legacy_profiles_remain_enum_compatible(tmp_path: Path) -> None:
    service = ModelRuntimeSettingsService(tmp_path)

    assert service.validate_thinking_level("xhigh")["valid"] is True
    assert service.validate_thinking_level("32k")["valid"] is False


def test_projected_legacy_contract_does_not_become_profile_authority() -> None:
    projected = {
        "supports_thinking": True,
        "thinking_levels": ["low", "high"],
        "thinking_control": {
            "supported": True,
            "input_schema": {"type": "enum", "values": ["low", "high"]},
            "request_binding": {},
            "source": "legacy",
        },
    }

    assert normalize_thinking_control(projected)["source"] == "legacy"
