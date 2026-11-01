"""Tiny offline archive fixtures; never invoke Cargo, Wasmtime or a guest."""

from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import archive_inputs
import build_rpds
from build_native import fingerprint
import fetch_registry
import fetch_rpds
import fetch_sdk
import fetch_toolchain
import fetch_validation_tools


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tar(path, members):
    """Members are (name, content, mode, kind, target); fixtures stay tiny."""
    options = {"preset": 0} if path.suffix == ".xz" else {}
    with tarfile.open(
        path, "w:xz" if path.suffix == ".xz" else "w:gz", **options
    ) as output:
        for name, content, mode, kind, target in members:
            member = tarfile.TarInfo(name)
            member.mode, member.type, member.linkname = mode, kind, target
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            output.addfile(member, io.BytesIO(content) if member.isfile() else None)


def file(name, data=b"pinned", mode=0o644):
    return name, data, mode, tarfile.REGTYPE, ""


def link(name, target, *, hard=False):
    return name, b"", 0o777, tarfile.LNKTYPE if hard else tarfile.SYMTYPE, target


class RpdsInputTests(unittest.TestCase):
    def inspect(self, archive, **kwargs):
        options = dict(
            maximum=1024 * 1024,
            max_file=1024,
            max_expanded=4096,
            max_members=100,
            allow_links=True,
        )
        options.update(kwargs)
        return archive_inputs.inspect(
            archive, "fixture", fingerprint(archive)["sha256"], **options
        )

    def source_fixture(self, directory, stack):
        root = Path(directory).resolve()
        (root / "archives").mkdir()
        lock, manifest = b"small pinned lock\n", b"small pinned manifest\n"
        source_archive = root / "archives/rpds.tar.gz"
        tar(
            source_archive,
            [
                file(fetch_rpds.PREFIX + "/Cargo.lock", lock),
                file(fetch_rpds.PREFIX + "/Cargo.toml", manifest),
                file(fetch_rpds.PREFIX + "/src/lib.rs", b"original"),
            ],
        )
        dependency = root / "archives/dep-1.0.0.crate"
        tar(dependency, [file("dep-1.0.0/src/lib.rs", b"locked dependency", 0o100644)])
        pin = {
            "name": "dep",
            "version": "1.0.0",
            "sha256": fingerprint(dependency)["sha256"],
        }
        for name, value in (
            ("ARCHIVE_SHA256", fingerprint(source_archive)["sha256"]),
            ("LOCK_SHA256", digest(lock)),
            ("MANIFEST_SHA256", digest(manifest)),
        ):
            stack.enter_context(patch.object(fetch_rpds, name, value))
        stack.enter_context(patch.object(fetch_rpds, "packages", return_value=[pin]))
        source_files = fetch_rpds.extract_source(
            source_archive.read_bytes(), root / "source"
        )
        (root / "vendor").mkdir()
        record = fetch_registry.unpack(
            dependency, root / "vendor/dep-1.0.0", pin, {"bytes": 0, "members": 0}
        )
        checksum = root / "vendor/dep-1.0.0/.cargo-checksum.json"
        checksum.write_bytes(fetch_rpds.checksum_bytes(pin, record["files"]))
        checksum.chmod(0o644)
        record["cargo_checksums"] = fingerprint(checksum)
        receipt = {
            "commit": fetch_rpds.COMMIT,
            "version": fetch_rpds.VERSION,
            "archive": fingerprint(source_archive),
            "source_files": source_files,
            "vendor_files": fetch_sdk.inventory(root / "vendor"),
            "packages": [record],
        }
        return root, receipt

    def rust_fixture(self, directory, stack):
        root = Path(directory).resolve()
        (root / "release.toml").write_bytes(b"tiny release fixture")
        archive = root / "rust-test.tar.xz"
        tar(
            archive,
            [
                file("rust-test/rustc/manifest.in", b"file:bin/rustc\n"),
                file("rust-test/rustc/bin/rustc", b"compiler data", 0o755),
                link("rust-test/rustc/bin/alias", "rustc"),
                file("rust-test/install.sh", b"never selected"),
            ],
        )
        pin = {
            "prefix": "rust-test",
            "component": "rustc",
            "sha256": fingerprint(archive)["sha256"],
        }
        stack.enter_context(
            patch.object(fetch_toolchain, "selected_inputs", return_value=[pin])
        )
        extracted = fetch_sdk.extract(archive, root / "extracted", prefix="rust-test")
        (root / "toolchain").mkdir()
        fetch_toolchain.merge_component(root / "extracted/rustc", root / "toolchain")
        receipt = {
            "version": fetch_toolchain.VERSION,
            "release_manifest": fingerprint(root / "release.toml"),
            "inputs": [
                {
                    **pin,
                    "archive": fingerprint(archive),
                    "extracted_files": len(extracted),
                }
            ],
            "files": fetch_sdk.inventory(root / "toolchain"),
        }
        return root, receipt

    def sdk_fixture(self, directory, stack):
        root = Path(directory).resolve()
        archive = root / "sdk.tar.gz"
        tar(
            archive,
            [
                file(fetch_sdk.PREFIX + "/bin/clang", b"SDK data", 0o755),
                link(fetch_sdk.PREFIX + "/bin/cc", "clang"),
                link(
                    fetch_sdk.PREFIX + "/bin/hard",
                    fetch_sdk.PREFIX + "/bin/clang",
                    hard=True,
                ),
            ],
        )
        identity = fingerprint(archive)
        stack.enter_context(patch.object(fetch_sdk, "SHA256", identity["sha256"]))
        stack.enter_context(patch.object(fetch_sdk, "ARCHIVE_BYTES", identity["bytes"]))
        files = fetch_sdk.extract(archive, root / "sdk")
        return root, {
            "archive_sha256": identity["sha256"],
            "archive_bytes": identity["bytes"],
            "files": files,
        }

    def test_source_and_vendor_receipts_agree_with_archives(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.source_fixture(directory, stack)
            build_rpds.verify_sources(root, receipt)

    def test_forged_source_receipt_cannot_authorize_changed_bytes_or_modes(self):
        for mode_only in (False, True):
            with (
                self.subTest(mode_only=mode_only),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                root, receipt = self.source_fixture(directory, stack)
                source = root / "source/src/lib.rs"
                source.chmod(0o755) if mode_only else source.write_bytes(
                    b"modified source"
                )
                receipt["source_files"] = fetch_sdk.inventory(root / "source")
                with self.assertRaisesRegex(ValueError, "pinned archive"):
                    build_rpds.verify_sources(root, receipt)

    def test_forged_vendor_and_cargo_checksum_receipt_cannot_authorize_change(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.source_fixture(directory, stack)
            dependency = root / "vendor/dep-1.0.0/src/lib.rs"
            dependency.write_bytes(b"forged dependency")
            record = receipt["packages"][0]
            record["files"]["src/lib.rs"] = {**fingerprint(dependency), "mode": "0o644"}
            checksum = root / "vendor/dep-1.0.0/.cargo-checksum.json"
            checksum.write_bytes(fetch_rpds.checksum_bytes(record, record["files"]))
            record["cargo_checksums"] = fingerprint(checksum)
            receipt["vendor_files"] = fetch_sdk.inventory(root / "vendor")
            with self.assertRaisesRegex(ValueError, "pinned archive"):
                build_rpds.verify_sources(root, receipt)

    def test_generated_checksum_bytes_and_mode_are_fixed(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.source_fixture(directory, stack)
            archive = root / "archives/dep-1.0.0.crate"
            files = fetch_rpds.registry_inventory(
                archive, receipt["packages"][0], {"bytes": 0, "members": 0}
            )
            expected = files[".cargo-checksum.json"]
            actual = root / "vendor/dep-1.0.0/.cargo-checksum.json"
            self.assertEqual(expected, {**fingerprint(actual), "mode": "0o644"})
            actual.chmod(0o600)
            receipt["vendor_files"] = fetch_sdk.inventory(root / "vendor")
            with self.assertRaisesRegex(ValueError, "pinned archives"):
                build_rpds.verify_sources(root, receipt)

    def test_changed_source_archive_fails_even_with_a_forged_archive_receipt(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.source_fixture(directory, stack)
            archive = root / "archives/rpds.tar.gz"
            with archive.open("ab") as stream:
                stream.write(b"changed")
            receipt["archive"] = fingerprint(archive)
            with self.assertRaisesRegex(ValueError, "pinned archive identity"):
                build_rpds.verify_sources(root, receipt)

    def test_rust_expansion_matches_real_merge_without_install_manifest(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.rust_fixture(directory, stack)
            build_rpds.verify_rust(root, receipt)
            self.assertNotIn("manifest.in", receipt["files"])
            self.assertNotIn("install.sh", receipt["files"])
            (root / "toolchain/bin/rustc").write_bytes(b"modified")
            receipt["files"] = fetch_sdk.inventory(root / "toolchain")
            with self.assertRaisesRegex(ValueError, "pinned component"):
                build_rpds.verify_rust(root, receipt)

    def test_rust_archive_must_match_immutable_pin_as_well_as_receipt(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.rust_fixture(directory, stack)
            archive = root / "rust-test.tar.xz"
            with archive.open("ab") as stream:
                stream.write(b"changed")
            receipt["inputs"][0]["archive"] = fingerprint(archive)
            with self.assertRaisesRegex(ValueError, "pinned archive identity"):
                build_rpds.verify_rust(root, receipt)

    def test_sdk_expansion_matches_bytes_modes_and_safe_links(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.sdk_fixture(directory, stack)
            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("whole-file SDK read")
            ):
                build_rpds.verify_sdk(root, receipt)
            (root / "sdk/bin/clang").write_bytes(b"forged SDK")
            receipt["files"] = fetch_sdk.inventory(root / "sdk")
            with self.assertRaisesRegex(ValueError, "pinned archive"):
                build_rpds.verify_sdk(root, receipt)

    def test_sdk_archive_digest_cannot_be_replaced_by_mutable_receipt(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, receipt = self.sdk_fixture(directory, stack)
            archive = root / "sdk.tar.gz"
            original = archive.read_bytes()
            archive.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            with self.assertRaisesRegex(ValueError, "pinned archive identity"):
                build_rpds.verify_sdk(root, receipt)

    def test_validation_tree_and_forged_receipt_cannot_replace_pinned_wheel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            wheel = root / "validation.whl"
            with zipfile.ZipFile(wheel, "w") as output:
                output.writestr("wasmtime/__init__.py", b"pinned Python source")
            (root / "wac-cli").write_bytes(b"pinned WAC data")
            pins = tuple(
                {"filename": path.name, **fingerprint(path)}
                for path in (root / "wac-cli", wheel)
            )
            files = fetch_validation_tools.extract_wheel(wheel, root / "wasmtime")
            receipt = {"inputs": list(pins), "files": files}
            with patch.object(fetch_validation_tools, "INPUTS", pins):
                with patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("whole-file wheel read"),
                ):
                    build_rpds.verify_validation(root, receipt)
                (root / "wasmtime/wasmtime/__init__.py").write_bytes(
                    b"forged Python source"
                )
                receipt["files"] = fetch_sdk.inventory(root / "wasmtime")
                with self.assertRaisesRegex(ValueError, "pinned wheel"):
                    build_rpds.verify_validation(root, receipt)

    def test_safe_directory_alias_can_be_followed_repeatedly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = root / "tiny.tar.gz"
            tar(
                archive,
                [
                    file("fixture/data"),
                    link("fixture/root", "."),
                    link("fixture/repeated", "root/root/root/data"),
                ],
            )
            expected = fetch_sdk.extract(archive, root / "extracted", prefix="fixture")
            self.assertEqual(self.inspect(archive)["files"], expected)

    def test_archive_safety_rejects_traversal_duplicates_links_collisions_and_modes(
        self,
    ):
        cases = [
            [file("fixture/../escape")],
            [file("/fixture/escape")],
            [file("fixture/file"), file("fixture/file")],
            [link("fixture/escape", "../../outside")],
            [link("fixture/root", "."), link("fixture/escape", "root/..")],
            [link("fixture/first", "second"), link("fixture/second", "first")],
            [link("fixture/missing", "missing-target")],
            [link("fixture/hard", "fixture/missing", hard=True)],
            [link("fixture/dir", "."), file("fixture/dir/file")],
            [file("fixture/dir/file"), link("fixture/dir", ".")],
            [file("fixture/file", mode=0o4644)],
        ]
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "tiny.tar.gz"
            for members in cases:
                with self.subTest(members=members):
                    tar(archive, members)
                    with self.assertRaises(ValueError):
                        self.inspect(archive)

    def test_archive_enforces_compressed_file_member_and_expansion_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "tiny.tar.gz"
            tar(archive, [file("fixture/a", b"abc"), file("fixture/b", b"xyz")])
            for bounds in (
                {"maximum": 1},
                {"max_file": 2},
                {"max_members": 1},
                {"max_expanded": 5},
                {"expected_bytes": 1},
            ):
                with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                    self.inspect(archive, **bounds)

    def test_archive_digest_is_rechecked_after_streaming(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "tiny.tar.gz"
            tar(archive, [file("fixture/a")])
            identity = fingerprint(archive)
            with patch.object(
                archive_inputs,
                "fingerprint",
                side_effect=[identity, {**identity, "sha256": "0" * 64}],
            ):
                with self.assertRaisesRegex(ValueError, "changed during inspection"):
                    self.inspect(archive)

    def test_configuration_is_outside_work_and_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            destination = root / "candidate"
            with (
                patch.object(sys, "platform", "linux"),
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}),
                patch.object(
                    build_rpds.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(free=fetch_rpds.DISK_FLOOR),
                ),
                patch.object(build_rpds, "verify", return_value={}),
                patch.object(
                    build_rpds,
                    "run_stage",
                    side_effect=[{}, RuntimeError("no tool execution")],
                ) as stage,
            ):
                with self.assertRaisesRegex(RuntimeError, "no tool execution"):
                    build_rpds.run(root, root, root, root, root, destination)
            config = destination / "build-config"
            self.assertFalse(config.is_relative_to(destination / "work"))
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o500)
            first, second = [call.args[1] for call in stage.call_args_list]
            self.assertIn(
                ["--ro-bind", str(config), "/build-config"],
                [first[i : i + 3] for i in range(len(first) - 2)],
            )
            self.assertIn(
                "cargo --config /build-config/cargo.toml build", build_rpds.RECIPE
            )
            self.assertIn(
                "PYO3_CONFIG_FILE=/build-config/pyo3-config.txt", build_rpds.RECIPE
            )
            self.assertNotIn("/work/cargo-home/config.toml", first)
            for source, mount in (
                (root / "wac-cli", "/wac"),
                (HERE.parents[1] / "sdk/python/composition.py", "/composition.py"),
            ):
                self.assertIn(
                    ["--ro-bind", str(source), mount],
                    [second[i : i + 3] for i in range(len(second) - 2)],
                )
            receipt = json.loads((destination / "rpds-build.json").read_bytes())
            helper = HERE.parents[1] / "sdk/python/composition.py"
            self.assertEqual(receipt["configuration"][str(helper)], fingerprint(helper))


if __name__ == "__main__":
    unittest.main()
