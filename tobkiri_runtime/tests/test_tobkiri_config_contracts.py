import logging

import pytest

from core_runtime.env_compat import read_migrated_env, reset_migration_warnings_for_tests

pytestmark = pytest.mark.contract


def test_canonical_env_wins_over_legacy(caplog):
    assert read_migrated_env("TOBKIRI_API_TOKEN", "RUMI_API_TOKEN", environ={"TOBKIRI_API_TOKEN": "new", "RUMI_API_TOKEN": "old"}) == "new"
    assert not caplog.records


def test_legacy_env_warns_once_without_secret_value(caplog):
    reset_migration_warnings_for_tests()
    caplog.set_level(logging.WARNING)
    env = {"RUMI_API_TOKEN": "super-secret"}
    assert read_migrated_env("TOBKIRI_API_TOKEN", "RUMI_API_TOKEN", environ=env) == "super-secret"
    assert read_migrated_env("TOBKIRI_API_TOKEN", "RUMI_API_TOKEN", environ=env) == "super-secret"
    assert len(caplog.records) == 1
    assert "super-secret" not in caplog.text
