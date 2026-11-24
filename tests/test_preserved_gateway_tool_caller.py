"""Standard-library transport tests, never producer or guest/native acceptance."""

from __future__ import annotations

from contextlib import ExitStack
import copy
import errno
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import stage_preserved_gateway_tool_caller as staging


def sha(data: bytes) -> str:
    """Hash exact transport bytes without decoding their payload."""
    return hashlib.sha256(data).hexdigest()


class DecompressionTests(unittest.TestCase):
    """Exercise the reused audited XZ decoder without executable components."""

    def decode(self, data: bytes, **changes: Any) -> bytes:
        expected = b"inert bounded transport bytes"
        arguments = {
            "compressed_sha": sha(data), "artifact_sha": sha(expected),
            "artifact_bytes": len(expected),
        }
        arguments.update(changes)
        return staging.decompress_verified(data, **arguments)

    def test_exact_stream_and_dictionary_output_limits(self) -> None:
        expected = b"inert bounded transport bytes"
        compressed = lzma.compress(expected)
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        with patch.object(
            lzma, "LZMADecompressor", return_value=decoder,
        ) as factory:
            self.assertEqual(self.decode(compressed), expected)
        factory.assert_called_once_with(
            format=lzma.FORMAT_XZ, memlimit=128 * 1024 * 1024,
        )

    def test_truncated_trailing_concatenated_and_corrupt_streams(self) -> None:
        compressed = lzma.compress(b"inert bounded transport bytes")
        for changed in (
            compressed[:-1], compressed + b"\0", compressed + compressed,
            b"not an XZ stream",
        ):
            with self.subTest(length=len(changed)):
                with self.assertRaises((ValueError, lzma.LZMAError)):
                    self.decode(changed)

    def test_compressed_and_decompressed_hash_mismatch(self) -> None:
        compressed = lzma.compress(b"inert bounded transport bytes")
        for key in ("compressed_sha", "artifact_sha"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.decode(compressed, **{key: "0" * 64})

    def test_expansion_and_invalid_output_bounds_fail(self) -> None:
        compressed = lzma.compress(b"x" * 10000)
        for bound in (0, -1, 10, 128 * 1024 * 1024 + 1):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                self.decode(compressed, artifact_bytes=bound)


class TransportTests(unittest.TestCase):
    """Use test-only inert bytes and a stub graph to isolate transport failures."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixtures"
        self.fixture.mkdir()
        self.output = self.root / "prepared"
        self.pins: dict[str, Any] = {}
        self.originals: dict[str, tuple[bytes, bytes]] = {}
        for name in ("bridge", "caller"):
            directory = self.fixture / name
            directory.mkdir()
            binary = b"inert-not-a-component-" + name.encode()
            receipt = json.dumps({"inert": name}).encode()
            compressed = lzma.compress(binary)
            (directory / "component.wasm.xz").write_bytes(compressed)
            (directory / "component.build.json").write_bytes(receipt)
            self.originals[name] = (binary, receipt)
            self.pins[name] = {
                "compressed_sha256": sha(compressed),
                "compressed_bytes": len(compressed),
                "artifact_sha256": sha(binary), "artifact_bytes": len(binary),
                "receipt_sha256": sha(receipt), "receipt_bytes": len(receipt),
            }
        self.index = staging.graph.canonical({"inert_transport_test": True})
        self.index_sha = sha(self.index)
        graph_pins = {
            name: {
                key: self.pins[name][key]
                for key in ("artifact_sha256", "receipt_sha256")
            }
            for name in ("bridge", "caller")
        }
        graph_pins["source_observations_sha256"] = (
            staging.SOURCE_OBSERVATIONS_SHA256
        )
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module, name, value in (
            (staging, "FIXTURE", self.fixture),
            (staging, "FIXTURE_COMPONENTS", self.pins),
            (staging, "GRAPH_INDEX_SHA256", self.index_sha),
            (staging.graph, "REVIEWED_PINS", graph_pins),
        ):
            self.stack.enter_context(patch.object(module, name, value))
        self.materialize = self.stack.enter_context(patch.object(
            staging.graph, "materialize", side_effect=self.stub_graph,
        ))

    def stub_graph(
        self, bridge: Path, caller: Path, oracle: Path, output: Path,
        *, pins: dict[str, Any],
    ) -> dict[str, Any]:
        """Write only inert test metadata, never instantiate or compose Wasm."""
        self.assertEqual(oracle, staging.graph.ORACLE)
        self.assertEqual(pins, staging.graph.REVIEWED_PINS)
        for name, artifact in (("bridge", bridge), ("caller", caller)):
            self.assertEqual(artifact.read_bytes(), self.originals[name][0])
            self.assertEqual(
                artifact.with_suffix(".build.json").read_bytes(),
                self.originals[name][1],
            )
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            self.assertEqual(artifact.stat().st_nlink, 1)
        output.mkdir(mode=0o700)
        descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            staging.graph.caller_stage._write(descriptor, "index.json", self.index)
        finally:
            os.close(descriptor)
        return {
            "index_sha256": self.index_sha,
            **dict.fromkeys(staging.FALSE_FLAGS, False),
        }

    def assert_unpublished(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".preserved-gateway-caller-*")))

    def test_private_output_contract_and_temporary_input_cleanup(self) -> None:
        directories = []
        original = tempfile.TemporaryDirectory

        def capture(*args: Any, **kwargs: Any) -> Any:
            self.assertIsNotNone(kwargs.get("dir"))
            self.assertFalse(Path(kwargs["dir"]).is_symlink())
            directory = original(*args, **kwargs)
            directories.append(Path(directory.name))
            return directory

        default = self.root / "default-temp"
        default.mkdir()
        link = self.root / "linked-temp"
        link.symlink_to(default, target_is_directory=True)
        with (
            patch.object(tempfile, "tempdir", str(link)),
            patch.object(tempfile, "TemporaryDirectory", side_effect=capture),
        ):
            report = staging.stage_preserved(self.output)
        self.assertEqual({p.name for p in self.output.iterdir()}, {
            "graph", "materialization.json",
        })
        self.assertEqual(
            json.loads((self.output / "materialization.json").read_bytes()), report,
        )
        self.assertEqual(report["format"], "tobkiri.preserved-gateway-tool-caller/1")
        self.assertEqual(report["graph_directory"], "graph")
        self.assertEqual(report["graph_index_sha256"], self.index_sha)
        self.assertEqual(report["fixture_components"], self.pins)
        for flag in staging.FALSE_FLAGS:
            self.assertIs(report[flag], False)
        for path in (self.output, *self.output.rglob("*")):
            self.assertFalse(path.is_symlink())
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode), 0o600 if path.is_file() else 0o700,
            )
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertFalse(list(default.iterdir()))

    def test_missing_mutated_or_unsafe_fixture_members_fail_before_graph(self) -> None:
        for name in ("bridge", "caller"):
            for filename in ("component.wasm.xz", "component.build.json"):
                for mutation in (
                    "missing", "changed", "short", "long", "symlink",
                    "hardlink", "directory", "fifo",
                ):
                    with self.subTest(name=name, file=filename, mutation=mutation):
                        path = self.fixture / name / filename
                        original = path.read_bytes()
                        path.unlink()
                        spare = self.root / "spare"
                        if mutation in ("symlink", "hardlink"):
                            spare.write_bytes(original)
                            if mutation == "symlink":
                                path.symlink_to(spare)
                            else:
                                os.link(spare, path)
                        elif mutation == "directory":
                            path.mkdir()
                        elif mutation == "fifo":
                            os.mkfifo(path)
                        elif mutation == "changed":
                            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                        elif mutation == "short":
                            path.write_bytes(original[:-1])
                        elif mutation == "long":
                            path.write_bytes(original + b"x")
                        try:
                            with self.assertRaises(ValueError):
                                staging.stage_preserved(self.output)
                            self.assert_unpublished()
                            self.materialize.assert_not_called()
                        finally:
                            if mutation == "directory":
                                path.rmdir()
                            elif mutation != "missing":
                                path.unlink()
                            if spare.exists():
                                spare.unlink()
                            path.write_bytes(original)

    def test_linked_fixture_parent_is_rejected(self) -> None:
        link = self.root / "linked"
        link.symlink_to(self.fixture, target_is_directory=True)
        with patch.object(staging, "FIXTURE", link), self.assertRaises(ValueError):
            staging.stage_preserved(self.output)
        self.assert_unpublished()

    def test_receipt_authenticated_before_decompression(self) -> None:
        (self.fixture / "bridge/component.build.json").write_bytes(b"wrong")
        with patch.object(staging, "decompress_verified") as decode:
            with self.assertRaises(ValueError):
                staging.stage_preserved(self.output)
            decode.assert_not_called()
        self.assert_unpublished()

    def test_pinned_components_must_match_existing_materializer(self) -> None:
        pins = copy.deepcopy(self.pins)
        pins["caller"]["artifact_sha256"] = "0" * 64
        with patch.object(staging, "FIXTURE_COMPONENTS", pins):
            with self.assertRaises(ValueError):
                staging.stage_preserved(self.output)
        self.assert_unpublished()

    def test_source_failure_leaves_no_success_or_partial_output(self) -> None:
        self.materialize.side_effect = ValueError("current source binding differs")
        with self.assertRaisesRegex(ValueError, "source binding"):
            staging.stage_preserved(self.output)
        self.assert_unpublished()

    def test_wrong_index_bytes_fail_even_if_returned_hash_claim_matches(self) -> None:
        self.index = b"changed graph bytes"
        with self.assertRaises(ValueError):
            staging.stage_preserved(self.output)
        self.assert_unpublished()

    def test_wrong_returned_identity_and_scope_flags_are_rejected(self) -> None:
        for key in ("index_sha256", *staging.FALSE_FLAGS):
            with self.subTest(key=key):
                def changed(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    report = self.stub_graph(*args, **kwargs)
                    report[key] = "0" * 64 if key == "index_sha256" else True
                    return report

                self.materialize.side_effect = changed
                with self.assertRaises(ValueError):
                    staging.stage_preserved(self.output)
                self.assert_unpublished()

    def test_existing_destination_is_not_modified(self) -> None:
        self.output.mkdir()
        sentinel = self.output / "retained"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(ValueError):
            staging.stage_preserved(self.output)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual({p.name for p in self.output.iterdir()}, {"retained"})
        self.materialize.assert_not_called()

    def test_linked_ambiguous_and_missing_parent_destinations_rejected(self) -> None:
        link = self.root / "linked-parent"
        link.symlink_to(self.root, target_is_directory=True)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "absent", target_is_directory=True)
        for target in (
            link / "prepared", dangling, self.root / "missing/child",
            self.root / "../escape", self.root / "bad:name",
        ):
            with self.subTest(path=target), self.assertRaises(ValueError):
                staging.stage_preserved(target)
        self.materialize.assert_not_called()
        self.assert_unpublished()

    def test_atomic_race_preserves_competing_destination(self) -> None:
        original = staging.graph.bridge_pack._publish_no_replace

        def race(source: Path, destination: Path) -> None:
            destination.mkdir()
            (destination / "retained").write_bytes(b"competitor")
            original(source, destination)

        with patch.object(
            staging.graph.bridge_pack, "_publish_no_replace", side_effect=race,
        ), self.assertRaises(FileExistsError):
            staging.stage_preserved(self.output)
        self.assertEqual(
            (self.output / "retained").read_bytes(), b"competitor",
        )
        self.assertEqual({p.name for p in self.output.iterdir()}, {"retained"})
        self.assertFalse(list(self.root.glob(".preserved-gateway-caller-*")))

    def test_unsupported_atomic_publication_fails_without_fallback(self) -> None:
        with patch.object(
            staging.graph.bridge_pack, "_publish_no_replace",
            side_effect=OSError(errno.ENOTSUP, "unsupported"),
        ), self.assertRaises(OSError):
            staging.stage_preserved(self.output)
        self.assert_unpublished()

    def test_metadata_write_failure_cleans_up_validated_private_graph(self) -> None:
        original = staging.graph.caller_stage._write

        def fail(descriptor: int, relative: str, data: bytes) -> None:
            if relative == "materialization.json":
                raise OSError("injected private write failure")
            original(descriptor, relative, data)

        with patch.object(
            staging.graph.caller_stage, "_write", side_effect=fail,
        ), self.assertRaises(OSError):
            staging.stage_preserved(self.output)
        self.assert_unpublished()


class ActualFixtureTests(unittest.TestCase):
    """Validate actual preserved bytes and current plans without source execution."""

    def test_exact_archived_receipts_and_current_input_pins(self) -> None:
        archive = (
            ROOT / "tools/python313-producer/trials/2026-10-20-finalization/builds"
        )
        for name, build in (("bridge", "bridge-v2"), ("caller", "caller")):
            with self.subTest(component=name):
                pin = staging.FIXTURE_COMPONENTS[name]
                receipt = (staging.FIXTURE / name / "component.build.json").read_bytes()
                self.assertEqual(receipt, (archive / build / "component.build.json")
                                 .read_bytes())
                self.assertEqual(sha(receipt), pin["receipt_sha256"])
                self.assertEqual(len(receipt), pin["receipt_bytes"])
                self.assertEqual(
                    json.loads(receipt)["artifact_sha256"], pin["artifact_sha256"],
                )
                for key in ("artifact_sha256", "receipt_sha256"):
                    self.assertEqual(pin[key], staging.graph.REVIEWED_PINS[name][key])
        self.assertEqual(staging.GRAPH_INDEX_SHA256, (
            "0441bde565b5e1765a8fdc262e61db00a5fff0c160624d99d5d841f65c737d96"
        ))

    def test_real_fixture_materializes_exact_graph_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "prepared"
            default = root / "default-temp"
            default.mkdir()
            link = root / "linked-temp"
            link.symlink_to(default, target_is_directory=True)
            with patch.object(tempfile, "tempdir", str(link)):
                report = staging.stage_preserved(output)
            self.assertFalse(list(default.iterdir()))
            self.assertEqual(
                sha((output / "graph/index.json").read_bytes()),
                staging.GRAPH_INDEX_SHA256,
            )
            self.assertEqual(report["graph_index_sha256"], staging.GRAPH_INDEX_SHA256)
            self.assertEqual(
                json.loads((output / "materialization.json").read_bytes()), report,
            )
            for flag in staging.FALSE_FLAGS:
                self.assertIs(report[flag], False)
            self.assertEqual({p.name for p in output.iterdir()}, {
                "graph", "materialization.json",
            })
            for name in ("bridge", "caller"):
                pack = output / "graph" / name
                pin = staging.FIXTURE_COMPONENTS[name]
                self.assertEqual(sha((pack / "component.wasm").read_bytes()),
                                 pin["artifact_sha256"])
                self.assertEqual((pack / "component.wasm").stat().st_size,
                                 pin["artifact_bytes"])
                self.assertEqual(sha((pack / "component.build.json").read_bytes()),
                                 pin["receipt_sha256"])
            for path in (output, *output.rglob("*")):
                self.assertFalse(path.is_symlink())
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o600 if path.is_file() else 0o700,
                )
                if path.is_file():
                    self.assertEqual(path.stat().st_nlink, 1)

    def test_rebound_receipt_still_cannot_forge_current_source_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture"
            shutil.copytree(staging.FIXTURE, fixture)
            receipt_path = fixture / "caller/component.build.json"
            receipt = json.loads(receipt_path.read_bytes())
            receipt["source_manifest"][0]["sha256"] = "0" * 64
            receipt["source_manifest_sha256"] = sha(
                staging.graph.canonical(receipt["source_manifest"]),
            )
            forged = staging.graph.canonical(receipt)
            receipt_path.write_bytes(forged)
            pins = copy.deepcopy(staging.FIXTURE_COMPONENTS)
            pins["caller"].update(receipt_sha256=sha(forged), receipt_bytes=len(forged))
            graph_pins = copy.deepcopy(staging.graph.REVIEWED_PINS)
            graph_pins["caller"]["receipt_sha256"] = sha(forged)
            with (
                patch.object(staging, "FIXTURE", fixture),
                patch.object(staging, "FIXTURE_COMPONENTS", pins),
                patch.object(staging.graph, "REVIEWED_PINS", graph_pins),
                self.assertRaisesRegex(ValueError, "complete current source plan"),
            ):
                staging.stage_preserved(root / "prepared")
            self.assertFalse((root / "prepared").exists())
            self.assertFalse(list(root.glob(".preserved-gateway-caller-*")))

    def test_rebound_oracle_still_cannot_forge_current_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oracle = json.loads(staging.graph.ORACLE.read_bytes())
            oracle["source_bindings"][0]["sha256"] = "0" * 64
            raw = staging.graph.canonical(oracle)
            path = root / "oracle.json"
            path.write_bytes(raw)
            graph_pins = copy.deepcopy(staging.graph.REVIEWED_PINS)
            graph_pins["source_observations_sha256"] = sha(raw)
            with (
                patch.object(staging.graph, "ORACLE", path),
                patch.object(staging, "SOURCE_OBSERVATIONS_SHA256", sha(raw)),
                patch.object(staging.graph, "REVIEWED_PINS", graph_pins),
                self.assertRaises(ValueError),
            ):
                staging.stage_preserved(root / "prepared")
            self.assertFalse((root / "prepared").exists())
            self.assertFalse(list(root.glob(".preserved-gateway-caller-*")))


if __name__ == "__main__":
    unittest.main()
