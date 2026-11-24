"""Synthetic boundary tests for the UI poll C-helper stage."""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_ui_poll_oracle as oracle


def _fixture():
    source_identity = {"bytes": 11, "sha256": "1" * 64}
    sdk_manifest = {"bytes": 12, "sha256": "2" * 64}
    stages = []
    outputs = {}
    for stage_name, recipe_hash, paths in oracle.build_wasi.STAGES:
        stage_outputs = {
            relative: {"bytes": len(relative), "sha256": "3" * 64} for relative in paths
        }
        outputs.update(stage_outputs)
        stages.append(
            {
                "name": stage_name,
                "recipe_sha256": recipe_hash,
                "supervision": {"outcome": "success"},
                "outputs": stage_outputs,
            }
        )
    headers = {
        "root_relative": "wasi/install/include/python3.13",
        "files": {
            "Python.h": {"bytes": 4, "sha256": "4" * 64},
            "pyconfig.h": {"bytes": 5, "sha256": "5" * 64},
            "patchlevel.h": {"bytes": 6, "sha256": "6" * 64},
        },
        "generated_pyconfig": {"bytes": 5, "sha256": "5" * 64},
    }
    receipt = {
        "source": source_identity,
        "inputs_unchanged": True,
        "stages": stages,
        "sdk_manifest": sdk_manifest,
        "target_headers": headers,
    }
    return source_identity, sdk_manifest, receipt, headers, outputs


class BuildUiPollOracleTests(unittest.TestCase):
    def test_ci_guard_precedes_path_and_build_side_effects(self):
        for platform, actions in (("darwin", "true"), ("linux", None)):
            with (
                self.subTest(platform=platform, actions=actions),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp).resolve()
                destination = root / "new-output"
                environment = {"GITHUB_ACTIONS": actions} if actions else {}
                with (
                    patch.object(oracle.sys, "platform", platform),
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        Path, "is_absolute", side_effect=AssertionError("path accessed")
                    ) as path_check,
                    patch.object(
                        Path, "mkdir", side_effect=AssertionError("write")
                    ) as mkdir,
                    patch.object(oracle, "physical") as physical,
                    patch.object(oracle, "run_stage") as stage,
                    patch.object(oracle.fetch_sdk, "acquire") as download,
                ):
                    with self.assertRaisesRegex(ValueError, "restricted to Linux CI"):
                        oracle.run(
                            root / "source",
                            root / "wasi",
                            root / "validation",
                            destination,
                        )
                path_check.assert_not_called()
                mkdir.assert_not_called()
                physical.assert_not_called()
                stage.assert_not_called()
                download.assert_not_called()
                self.assertFalse(destination.exists())

    def _verify_wasi_mocks(self, root, receipt, headers, outputs):
        source = root / "source"
        wasi = root / "wasi"
        fingerprint_values = {
            str(wasi / "work" / relative): identity
            for relative, identity in outputs.items()
        }
        fingerprint_values[str(wasi / "sdk-input/sdk-manifest.json")] = receipt[
            "sdk_manifest"
        ]
        stack = ExitStack()
        mocks = {
            "verify_tree": stack.enter_context(
                patch.object(oracle, "verify_tree", return_value=receipt["source"])
            ),
            "report": stack.enter_context(
                patch.object(oracle.runtime_inputs, "report", return_value=receipt)
            ),
            "fingerprint": stack.enter_context(
                patch.object(
                    oracle,
                    "fingerprint",
                    side_effect=lambda path, *_args: fingerprint_values[str(path)],
                )
            ),
            "target_headers": stack.enter_context(
                patch.object(oracle.build_wasi, "target_headers", return_value=headers)
            ),
            "verify_sdk": stack.enter_context(patch.object(oracle, "verify_sdk")),
        }
        return stack, mocks, source, wasi

    def _assert_bad_header_binding(self, change):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_id, _, receipt, headers, outputs = _fixture()
            receipt = {**receipt, "source": source_id}
            if change == "missing":
                receipt.pop("target_headers")
            else:
                receipt["target_headers"] = {
                    **headers,
                    "files": {
                        **headers["files"],
                        "pyconfig.h": {"bytes": 5, "sha256": "f" * 64},
                    },
                }
            stack, mocks, source, wasi = self._verify_wasi_mocks(
                root, receipt, headers, outputs
            )
            with stack, self.assertRaisesRegex(ValueError, "generated target headers"):
                oracle.verify_wasi(source, wasi)
            mocks["target_headers"].assert_called_once()
            mocks["verify_sdk"].assert_not_called()

    def test_missing_target_header_receipt_is_rejected(self):
        self._assert_bad_header_binding("missing")

    def test_changed_target_header_receipt_is_rejected(self):
        self._assert_bad_header_binding("changed")

    def _assert_bad_wasi_binding(self, case):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, receipt, headers, outputs = _fixture()
            actual_outputs = outputs
            if case == "missing-stage":
                receipt["stages"] = receipt["stages"][:-1]
            else:
                actual_outputs = dict(outputs)
                path = next(iter(actual_outputs))
                actual_outputs[path] = {"bytes": 0, "sha256": "f" * 64}
            stack, mocks, source, wasi = self._verify_wasi_mocks(
                root, receipt, headers, actual_outputs
            )
            with stack, self.assertRaises(ValueError):
                oracle.verify_wasi(source, wasi)
            mocks["target_headers"].assert_not_called()
            if case == "missing-stage":
                mocks["fingerprint"].assert_not_called()

    def test_missing_wasi_stage_fails_before_target_header_lookup(self):
        self._assert_bad_wasi_binding("missing-stage")

    def test_changed_wasi_output_fails_before_target_header_lookup(self):
        self._assert_bad_wasi_binding("changed-output")


if __name__ == "__main__":
    unittest.main()
