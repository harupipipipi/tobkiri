"""Synthetic checks for inactive libc asset selection and CLI gating."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_ui_poll_libc
import fetch_sdk
import fetch_ui_poll_libc
import ui_empty_poll_assets as candidate_assets
import ui_empty_poll_source
import ui_poll_wit_workspace
from packager import package


CORE = b"\0asm\1\0\0\0"


def _fixture(root: Path) -> dict:
    inputs = {}
    original_assets = {}
    for index, name in enumerate(sorted(package.ASSETS)):
        path = root / "stock" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = CORE + f"stock-{index}".encode()
        path.write_bytes(data)
        logical = "libc.so" if name == "libc.so.zst" else name.removesuffix(".zst")
        inputs[name] = (path, logical, None)
        original_assets[name] = data

    build = root / "inactive-libc-build"
    build.mkdir()
    work = build / "work"
    (work / "cmake").mkdir(parents=True)
    (build / "build-libc.sh").write_bytes(build_ui_poll_libc.RECIPE.encode())
    receipt_path = build / "libc-build.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    original_libc = inputs["libc.so.zst"][0]
    candidate_libc = work / "cmake/libc.so"
    candidate_bytes = CORE + b"synthetic candidate module"
    candidate_libc.write_bytes(candidate_bytes)

    original_poll = (
        candidate_assets.HERE / "tests/fixtures/wasi-libc-161b3195-poll.c"
    ).read_bytes()
    candidate_poll, derivation = ui_empty_poll_source.derive(original_poll)
    source = build / "candidate-source"
    poll_path = source / ui_empty_poll_source.POLL_PATH
    poll_path.parent.mkdir(parents=True)
    poll_path.write_bytes(candidate_poll)
    (source / "source-index.txt").write_bytes(b"complete synthetic source tree\n")
    source_inventory = fetch_sdk.inventory(source)

    records = {}
    for version in ui_poll_wit_workspace.VERSIONS:
        relative = f"derived-wit/{version}"
        wit_root = work / relative
        wit_root.mkdir(parents=True)
        (wit_root / ui_poll_wit_workspace.OUTPUT).write_bytes(
            f"package wasi-libc-{version};\n".encode()
        )
        records[version] = {
            "source_relative": f"wasi/{version}/wit",
            "workspace_relative": relative,
            "before": ui_poll_wit_workspace.inventory(wit_root),
        }
    wit_workspace = ui_poll_wit_workspace.observe(work, records, successful=True)

    recipe_raw = build_ui_poll_libc.RECIPE.encode()
    report = {
        "format": "tobkiri.ui-empty-poll-libc-build/1",
        "outcome": "success",
        "build_performed": True,
        "inputs_unchanged": True,
        "wit_workspace_validated": True,
        "production_enabled": False,
        "source_pin": fetch_ui_poll_libc._pin(),
        "derivation": derivation,
        "sdk_archive": {
            "bytes": fetch_sdk.ARCHIVE_BYTES,
            "sha256": fetch_sdk.SHA256,
        },
        "build_recipe": {
            "bytes": len(recipe_raw),
            "sha256": hashlib.sha256(recipe_raw).hexdigest(),
        },
        "candidate_libc": {
            "path": "work/cmake/libc.so",
            "bytes": len(candidate_bytes),
            "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        },
        "supervision": {
            "outcome": "success",
            "exit_code": 0,
            "remaining_known_processes": [],
        },
        "original_libc": {
            "bytes": original_libc.stat().st_size,
            "sha256": hashlib.sha256(original_libc.read_bytes()).hexdigest(),
        },
        "candidate_source_inventory": source_inventory,
        "wit_workspace_before": records,
        "wit_workspace": wit_workspace,
    }
    return {
        "inputs": inputs,
        "original_assets": original_assets,
        "build": build,
        "report": report,
        "source": source,
        "work": work,
        "candidate_libc": candidate_libc,
        "candidate_source_inventory": source_inventory,
        "candidate_bytes": candidate_bytes,
        "original_poll_bytes": original_poll,
        "candidate_poll_bytes": candidate_poll,
    }


def _select(fixture: dict):
    with patch.object(
        candidate_assets.build_ui_poll_libc,
        "json_file",
        return_value=fixture["report"],
    ):
        return candidate_assets.select(fixture["inputs"], fixture["build"])


class UiEmptyPollAssetsTests(unittest.TestCase):
    def test_select_replaces_only_libc_and_leaves_stock_inputs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp).resolve())
            selected, marker, controls, snapshots = _select(fixture)
            changed = {
                name
                for name in fixture["inputs"]
                if selected[name] != fixture["inputs"][name]
            }
            self.assertEqual(changed, {"libc.so.zst"})
            candidate, logical, digest = selected["libc.so.zst"]
            self.assertEqual(logical, "libc.so")
            self.assertEqual(candidate.read_bytes(), fixture["candidate_bytes"])
            self.assertEqual(digest, marker["candidate_libc"]["sha256"])
            for name, (path, _logical, _pin) in fixture["inputs"].items():
                self.assertEqual(path.read_bytes(), fixture["original_assets"][name])
            self.assertEqual(
                snapshots[str(fixture["source"])],
                fixture["candidate_source_inventory"],
            )
            self.assertEqual(marker["changed_asset"], "libc.so.zst")
            self.assertFalse(marker["runtime_asset_override"])
            self.assertFalse(marker["production_enabled"])
            self.assertIn(str(fixture["build"] / "libc-build.json"), controls)

    def test_candidate_bytes_and_build_recipe_tampering_are_rejected(self):
        for changed in ("candidate", "recipe"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                fixture = _fixture(Path(tmp).resolve())
                path = (
                    fixture["candidate_libc"]
                    if changed == "candidate"
                    else fixture["build"] / "build-libc.sh"
                )
                path.write_bytes(path.read_bytes() + b"tamper")
                with self.assertRaises(ValueError):
                    _select(fixture)

    def test_full_candidate_source_and_wit_changes_are_rejected(self):
        for changed in ("source", "wit"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                fixture = _fixture(Path(tmp).resolve())
                if changed == "source":
                    (fixture["source"] / "unrecorded.txt").write_bytes(b"tamper")
                else:
                    wit = (
                        fixture["work"]
                        / "derived-wit/p2"
                        / ui_poll_wit_workspace.OUTPUT
                    )
                    wit.write_bytes(b"changed WIT\n")
                with self.assertRaises(ValueError):
                    _select(fixture)

    def test_unsuccessful_or_production_receipt_is_rejected(self):
        for field, value in (
            ("outcome", "failed"),
            ("inputs_unchanged", False),
            ("production_enabled", True),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                fixture = _fixture(Path(tmp).resolve())
                fixture["report"][field] = value
                with self.assertRaises(ValueError):
                    _select(fixture)

    def test_candidate_cli_requires_explicit_mode_and_nonproduction_marker(self):
        self.assertIsNone(candidate_assets.require_cli_mode({}, False))
        with self.assertRaisesRegex(ValueError, "requires candidate assets"):
            candidate_assets.require_cli_mode({}, True)
        marker = {
            "format": "tobkiri.ui-empty-poll-ci-asset/1",
            "changed_asset": "libc.so.zst",
            "runtime_asset_override": False,
            "production_enabled": False,
        }
        report = {"ui_empty_poll_candidate": marker}
        with self.assertRaisesRegex(ValueError, "explicit CI-only mode"):
            candidate_assets.require_cli_mode(report, False)
        self.assertIs(candidate_assets.require_cli_mode(report, True), marker)
        bad_marker = {**marker, "production_enabled": True}
        with self.assertRaisesRegex(ValueError, "explicit CI-only mode"):
            candidate_assets.require_cli_mode(
                {"ui_empty_poll_candidate": bad_marker}, True
            )


if __name__ == "__main__":
    unittest.main()
