"""Synthetic private trace fixtures; these are not Linker/runtime evidence."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poll_link_trace as trace


def _records():
    return [
        ("libc.so", False, trace.CORE_HEADER + b"synthetic-libc"),
        (
            "libcomponentize_py_bindings.so",
            False,
            trace.CORE_HEADER + b"synthetic-bindings",
        ),
        (
            "cache/_tobkiri_poll_oracle.abi3.so",
            True,
            trace.CORE_HEADER + b"synthetic-helper",
        ),
    ]


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _make_trace(base: Path, *, records=None, final=b"final-a"):
    """Write only tiny fake Wasm headers and bytes, never load a Wasm runtime."""
    rows = _records() if records is None else list(records)
    root = base / "trace"
    root.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    manifest_rows = [b"tobkiri-ci-link-inputs/1"]
    helper_identity = None
    for ordinal, (name, dl_openable, content) in enumerate(rows):
        _write_private(root / f"{ordinal:04}.wasm", content)
        manifest_rows.append(
            b"\t".join(
                (
                    str(ordinal).encode("ascii"),
                    name.encode("utf-8").hex().encode("ascii"),
                    b"true" if dl_openable else b"false",
                    str(len(content)).encode("ascii"),
                )
            )
        )
        if Path(name).name == trace.HELPER_NAME:
            helper_identity = {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    _write_private(root / "libraries.tsv", b"\n".join(manifest_rows) + b"\n")
    linked = trace.COMPONENT_HEADER + b"synthetic-linked-component"
    _write_private(root / "preinit-linked-component.wasm", linked)
    _write_private(
        root / "complete.tsv",
        f"linked-before-snapshot\t{len(rows)}\t{len(linked)}\n".encode(),
    )
    component = base / "final-component.wasm"
    _write_private(component, trace.COMPONENT_HEADER + final)
    assert helper_identity is not None
    return root.resolve(), helper_identity, component


class PollLinkTraceTests(unittest.TestCase):
    def test_reads_a_complete_synthetic_trace_and_keeps_its_claims_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, helper, component = _make_trace(Path(tmp))
            result = trace.read_trace(root, helper, component)

        self.assertEqual(result["format"], "tobkiri.ci-link-input-observation/1")
        self.assertEqual([row["ordinal"] for row in result["libraries"]], [0, 1, 2])
        self.assertEqual(
            [row["name"] for row in result["libraries"]],
            [name for name, _opened, _data in _records()],
        )
        self.assertEqual(result["helper"], helper)
        self.assertEqual(
            result["files"].keys(),
            {
                "libraries.tsv",
                "0000.wasm",
                "0001.wasm",
                "0002.wasm",
                "preinit-linked-component.wasm",
                "complete.tsv",
            },
        )
        self.assertTrue(result["actual_input_observation"])
        self.assertFalse(result["production_enabled"])
        self.assertFalse(result["final_provider_resolution_verified"])
        self.assertNotEqual(
            result["final_component"], result["files"]["preinit-linked-component.wasm"]
        )

    def test_rejects_bad_ordinal_name_size_marker_and_extra_members(self):
        for mutation in ("ordinal", "name", "size", "marker", "extra"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root, helper, component = _make_trace(Path(tmp))
                manifest_path = root / "libraries.tsv"
                manifest_rows = manifest_path.read_bytes().splitlines()
                fields = manifest_rows[1].split(b"\t")
                if mutation == "ordinal":
                    fields[0] = b"9"
                    manifest_rows[1] = b"\t".join(fields)
                    _rewrite(manifest_path, b"\n".join(manifest_rows) + b"\n")
                elif mutation == "name":
                    fields[1] = b"zz"
                    manifest_rows[1] = b"\t".join(fields)
                    _rewrite(manifest_path, b"\n".join(manifest_rows) + b"\n")
                elif mutation == "size":
                    fields[3] = str(int(fields[3]) + 1).encode()
                    manifest_rows[1] = b"\t".join(fields)
                    _rewrite(manifest_path, b"\n".join(manifest_rows) + b"\n")
                elif mutation == "marker":
                    _rewrite(root / "complete.tsv", b"linked-before-snapshot\t2\t1\n")
                else:
                    _write_private(root / "unlisted", b"extra")

                with self.assertRaises(ValueError):
                    trace.read_trace(root, helper, component)

    def test_requires_private_regular_files_and_rejects_symlinks_and_hardlinks(self):
        for mutation in ("directory-mode", "file-mode", "symlink", "hardlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root, helper, component = _make_trace(base)
                if mutation == "directory-mode":
                    root.chmod(0o755)
                elif mutation == "file-mode":
                    (root / "0000.wasm").chmod(0o640)
                elif mutation == "symlink":
                    control = root / "libraries.tsv"
                    outside = base / "outside.tsv"
                    outside.write_bytes(control.read_bytes())
                    outside.chmod(0o600)
                    control.unlink()
                    control.symlink_to(outside)
                else:
                    control = root / "complete.tsv"
                    outside = base / "outside-marker"
                    outside.write_bytes(control.read_bytes())
                    outside.chmod(0o600)
                    control.unlink()
                    os.link(outside, control)
                with self.assertRaises(ValueError):
                    trace.read_trace(root, helper, component)

    def test_enforces_member_and_aggregate_bounds_and_exact_helper_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, helper, component = _make_trace(Path(tmp))
            wrong_helper = {
                "bytes": helper["bytes"],
                "sha256": hashlib.sha256(b"other").hexdigest(),
            }
            with self.assertRaises(ValueError):
                trace.read_trace(root, wrong_helper, component)

        with tempfile.TemporaryDirectory() as tmp:
            root, helper, component = _make_trace(Path(tmp))
            with patch.object(trace, "MAXIMUM_MEMBERS", 2):
                with self.assertRaises(ValueError):
                    trace.read_trace(root, helper, component)

        with tempfile.TemporaryDirectory() as tmp:
            root, helper, component = _make_trace(Path(tmp))
            with patch.object(trace, "TOTAL_LIMIT", 64):
                with self.assertRaises(ValueError):
                    trace.read_trace(root, helper, component)

    def test_pair_allows_only_libc_bytes_to_change_and_requires_distinct_outputs(self):
        original_records = _records()
        candidate_records = list(original_records)
        candidate_records[0] = (
            "libc.so",
            False,
            trace.CORE_HEADER + b"candidate-libc",
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            original = trace.read_trace(
                *_make_trace(
                    base / "original", records=original_records, final=b"out-a"
                )
            )
            candidate = trace.read_trace(
                *_make_trace(
                    base / "candidate", records=candidate_records, final=b"out-b"
                )
            )
            pair = trace.compare_traces(original, candidate)

        self.assertEqual(pair["changed_libraries"], ["libc.so"])
        self.assertTrue(pair["same_helper_and_generated_bindings"])
        self.assertFalse(pair["final_provider_resolution_verified"])

        for mutation in ("helper", "bindings", "order", "same-final", "unchanged-libc"):
            with self.subTest(mutation=mutation):
                altered = copy.deepcopy(candidate)
                if mutation == "helper":
                    altered["helper"]["sha256"] = "0" * 64
                elif mutation == "bindings":
                    row = altered["libraries"][1]
                    row["sha256"] = "f" * 64
                    row["bytes"] += 1
                elif mutation == "order":
                    altered["libraries"][0], altered["libraries"][1] = (
                        altered["libraries"][1],
                        altered["libraries"][0],
                    )
                elif mutation == "same-final":
                    altered["final_component"] = copy.deepcopy(
                        original["final_component"]
                    )
                else:
                    altered["libraries"][0] = copy.deepcopy(original["libraries"][0])
                with self.assertRaises(ValueError):
                    trace.compare_traces(original, altered)


def _rewrite(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
