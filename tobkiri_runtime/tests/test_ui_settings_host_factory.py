"""Settings read identity checks run before any state or dependency access."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.tobkiri_ui_settings_pack.runtime.settings import (
    CONTRACT_ID,
    FUNCTION_ID,
    OPERATION_ID,
    SettingsReadHostFactoryV4,
)


def _context(root: Path) -> Any:
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=FUNCTION_ID, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=CONTRACT_ID, operation_id=OPERATION_ID, contract_version="1.0.0"
        ),
        principal_ref=SimpleNamespace(value="reader"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(binding,),
        domain_ids={(CONTRACT_ID, OPERATION_ID, "reader"): "domain"},
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "other"},
        {"approved": True},
        {"root": "/tmp/other"},
        {"operation": "write"},
        {"full": True},
    ],
)
def test_settings_rejects_request_widening_before_dependency_access(
    tmp_path: Path,
    patch: dict[str, Any],
) -> None:
    reader = SettingsReadHostFactoryV4().capture(_context(tmp_path))
    with pytest.raises(PermissionError, match="request is invalid"):
        reader.contributions[0].invoke(OPERATION_ID, {"profile_id": "defaults", **patch}, None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["domain_ids", "profile_id", "user_data_root"])
def test_settings_rejects_incomplete_capture(tmp_path: Path, field: str) -> None:
    context = _context(tmp_path)
    setattr(context, field, {} if field == "domain_ids" else None)
    with pytest.raises(PermissionError):
        SettingsReadHostFactoryV4().capture(context)
    assert list(tmp_path.iterdir()) == []


def test_settings_rejects_other_operation_before_dependency_access(tmp_path: Path) -> None:
    reader = SettingsReadHostFactoryV4().capture(_context(tmp_path))
    with pytest.raises(PermissionError, match="request is invalid"):
        reader.contributions[0].invoke("write", {"profile_id": "defaults"}, None)
    assert list(tmp_path.iterdir()) == []
