from importlib.metadata import version

import pytest
import rumi_ai.__main__ as legacy_entrypoint
import tobkiri
import tobkiri.__main__ as canonical_entrypoint

pytestmark = pytest.mark.contract


def test_installed_canonical_and_legacy_modules_are_discoverable():
    assert canonical_entrypoint.main is legacy_entrypoint.main


def test_canonical_version_matches_project_metadata():
    assert tobkiri.__version__ == version("tobkiri-runtime")
