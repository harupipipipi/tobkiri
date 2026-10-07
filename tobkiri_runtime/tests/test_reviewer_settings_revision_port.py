"""Private settings revision guard uses the actual read-only owner snapshot."""

from pathlib import Path
from types import SimpleNamespace
import pytest
from ecosystem.tobkiri_ui_settings_pack.runtime import settings as module
from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore


def context(root: Path) -> SimpleNamespace:
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=module.FUNCTION_ID),
        operation=SimpleNamespace(
            contract_id=module.CONTRACT_ID,
            operation_id=module.OPERATION_ID,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="verified-reader"),
    )
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(binding,),
        domain_ids={(module.CONTRACT_ID, module.OPERATION_ID, "verified-reader"): "domain"},
    )


def test_missing_configuration_reads_without_creating_files(tmp_path: Path) -> None:
    capture = context(tmp_path)
    guard = module.build_reviewer_configuration_revision(
        capture, capture.provider_bindings[0], assert_current=lambda: None
    )
    assert guard() == ("", 0)
    assert list(tmp_path.iterdir()) == []


def test_guard_detects_actual_owner_revision_and_model_changes(tmp_path: Path) -> None:
    capture = context(tmp_path)
    guard = module.build_reviewer_configuration_revision(
        capture, capture.provider_bindings[0], assert_current=lambda: None
    )
    owner = FrontendSettingsStore(tmp_path / "defaultspack/shared/frontend_settings.json")
    owner.compare_and_swap_document(
        {"tools": {"approval_reviewer_model": "registered-one"}}, expected_revision=0
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert guard() == ("registered-one", 1)
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    changed = owner.read_snapshot()
    changed["tools"]["approval_reviewer_model"] = "registered-two"
    owner.compare_and_swap_document(changed, expected_revision=1)
    assert guard() == ("registered-two", 2)


@pytest.mark.parametrize("field", ["profile_id", "user_data_root", "domain_ids"])
def test_incomplete_capture_fails_before_storage(tmp_path: Path, field: str) -> None:
    capture = context(tmp_path)
    setattr(capture, field, None if field != "domain_ids" else {})
    with pytest.raises(PermissionError):
        module.build_reviewer_configuration_revision(
            capture, capture.provider_bindings[0], assert_current=lambda: None
        )
    assert list(tmp_path.iterdir()) == []


def test_other_binding_and_stale_profile_are_rejected(tmp_path: Path) -> None:
    capture = context(tmp_path)
    with pytest.raises(PermissionError):
        module.build_reviewer_configuration_revision(capture, object(), assert_current=lambda: None)
    guard = module.build_reviewer_configuration_revision(
        capture, capture.provider_bindings[0], assert_current=lambda: None
    )
    capture.profile_id = "other"
    with pytest.raises(PermissionError):
        guard()
    assert list(tmp_path.iterdir()) == []
