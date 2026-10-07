"""
test_desktop_app_schema.py - Tests for desktop_app schema validation
in PackImporter._validate_ecosystem_json().

Phase V-4: desktop_app:execute capability schema validation.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

# Ensure tobkiri_runtime/ is on sys.path so 'core_runtime' is importable
_THIS_DIR = Path(__file__).resolve().parent          # tests/
_REPO_DIR = _THIS_DIR.parent                         # tobkiri_runtime/
if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))

from core_runtime.pack_importer import PackImporter


# ======================================================================
# Helpers
# ======================================================================

def _make_ecosystem(
    base_dir: Path,
    data: dict,
    filename: str = "ecosystem.json",
) -> Path:
    """Write ecosystem.json to base_dir and return its path."""
    eco_path = base_dir / filename
    with open(eco_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return eco_path


def _valid_ecosystem(**overrides) -> dict:
    """Return a minimal valid ecosystem.json dict."""
    base = {
        "pack_id": "test_pack",
        "version": "1.0.0",
        "metadata": {
            "name": "Test Pack",
            "description": "A test pack",
        },
    }
    base.update(overrides)
    return base


class TestDesktopAppSchemaValidation(unittest.TestCase):
    """Tests for desktop_app section in ecosystem.json validation."""

    def setUp(self):
        self._importer = PackImporter.__new__(PackImporter)

    def _validate(self, data: dict):
        """Call _validate_ecosystem_json and return (ok, msg, parsed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            eco_path = _make_ecosystem(Path(tmpdir), data)
            return self._importer._validate_ecosystem_json(eco_path)

    # ------------------------------------------------------------------
    # Failure cases
    # ------------------------------------------------------------------

    def test_desktop_app_not_dict(self):
        """desktop_app が dict でない場合はエラー。"""
        data = _valid_ecosystem(desktop_app="not_a_dict")
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("desktop_app", msg)
        self.assertIn("dict", msg)

    def test_desktop_app_missing_command(self):
        """desktop_app.command がない場合はエラー。"""
        data = _valid_ecosystem(desktop_app={})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("command", msg)

    def test_desktop_app_command_not_str(self):
        """desktop_app.command が str でない場合はエラー。"""
        data = _valid_ecosystem(desktop_app={"command": 123})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("command", msg)

    def test_desktop_app_command_empty(self):
        """desktop_app.command が空文字の場合はエラー。"""
        data = _valid_ecosystem(desktop_app={"command": "   "})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("command", msg)
        self.assertIn("empty", msg)

    def test_desktop_app_optional_field_wrong_type_working_dir(self):
        """desktop_app.working_dir が str でない場合はエラー。"""
        data = _valid_ecosystem(desktop_app={"command": "run.sh", "working_dir": 123})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("working_dir", msg)

    def test_desktop_app_optional_field_wrong_type_env(self):
        """desktop_app.env が dict でない場合はエラー。"""
        data = _valid_ecosystem(desktop_app={"command": "run.sh", "env": "bad"})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("env", msg)

    def test_desktop_app_optional_field_wrong_type_capabilities(self):
        """desktop_app.capabilities が list でない場合はエラー。"""
        data = _valid_ecosystem(desktop_app={"command": "run.sh", "capabilities": "bad"})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("capabilities", msg)

    def test_desktop_app_optional_field_wrong_type_window(self):
        """desktop_app.window が dict でない場合はエラー。"""
        data = _valid_ecosystem(desktop_app={"command": "run.sh", "window": [1, 2]})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("window", msg)

    def test_desktop_app_optional_field_wrong_type_platforms(self):
        """desktop_app.platforms が list でない場合はエラー。"""
        data = _valid_ecosystem(desktop_app={"command": "run.sh", "platforms": {}})
        ok, msg, _ = self._validate(data)
        self.assertFalse(ok)
        self.assertIn("platforms", msg)

    # ------------------------------------------------------------------
    # Success cases
    # ------------------------------------------------------------------

    def test_desktop_app_valid_minimal(self):
        """最小限の正常な desktop_app 定義が検証を通る。"""
        data = _valid_ecosystem(desktop_app={"command": "pack-shell run"})
        ok, msg, parsed = self._validate(data)
        self.assertTrue(ok, f"Validation failed: {msg}")
        self.assertIsNotNone(parsed)

    def test_desktop_app_valid_full(self):
        """全オプション付きの正常な desktop_app 定義が検証を通る。"""
        data = _valid_ecosystem(desktop_app={
            "command": "pack-shell run",
            "working_dir": "/tmp",
            "env": {"NODE_ENV": "production"},
            "capabilities": ["network", "filesystem"],
            "window": {"title": "My App", "width": 800, "height": 600},
            "platforms": ["darwin", "win32", "linux"],
        })
        ok, msg, parsed = self._validate(data)
        self.assertTrue(ok, f"Validation failed: {msg}")
        self.assertIsNotNone(parsed)

    def test_desktop_app_absent(self):
        """desktop_app が未定義の ecosystem.json が検証を通る（オプショナル）。"""
        data = _valid_ecosystem()
        ok, msg, parsed = self._validate(data)
        self.assertTrue(ok, f"Validation failed: {msg}")
        self.assertIsNotNone(parsed)

    def test_bundled_defaultspack_declares_desktop_app(self):
        """The v4 shell contract replaces the removed ecosystem desktop field."""
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        legacy_path = _REPO_DIR / "ecosystem" / "defaultspack" / "ecosystem.json"
        self.assertFalse(legacy_path.exists())
        catalog = BundledCatalog.load(legacy_path.parent / "v4")
        self.assertIn("shell.tauri.default", catalog.packs)
        shell_doc = json.loads(
            (legacy_path.parent / "v4" / "shell.tauri.default.shell.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(shell_doc["pack_id"], "shell.tauri.default")
        self.assertEqual(shell_doc["contract_id"], "app.shell.v1")
        self.assertEqual(shell_doc["presentation"]["technology"], "tauri")
        assert_profile_resolver_requires_authority_snapshot()


if __name__ == "__main__":
    unittest.main()
