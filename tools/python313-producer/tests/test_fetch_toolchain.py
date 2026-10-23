import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_toolchain as rust


def release_fixture():
    lines = [f'date = "{rust.DATE}"']
    for package, target, _, digest in rust.INPUTS:
        prefix = f"{package}-{rust.VERSION}-{target}"
        lines += [f'[pkg.{package}.target.{target}]', 'available = true',
                  f'xz_url = "https://static.rust-lang.org/dist/{rust.DATE}/{prefix}.tar.xz"',
                  f'xz_hash = "{digest}"']
    return ("\n".join(lines) + "\n").encode()


class ToolchainTests(unittest.TestCase):
    def test_release_and_each_archive_are_independently_pinned(self):
        data = release_fixture()
        with patch.object(rust, "MANIFEST_SHA256", hashlib.sha256(data).hexdigest()):
            selected = rust.selected_inputs(data)
            self.assertEqual(len(selected), 4)
            self.assertEqual({x["target"] for x in selected}, {rust.HOST, rust.TARGET})
            with self.assertRaisesRegex(ValueError, "manifest identity"):
                rust.selected_inputs(data + b"\n")
        for modified in (data.replace(b'available = true', b'available = false', 1),
                         data.replace(rust.INPUTS[0][3].encode(), b"0" * 64),
                         data.replace(b"static.rust-lang.org", b"different.invalid"),
                         data.replace(rust.DATE.encode(), b"2026-03-06", 1)):
            with patch.object(rust, "MANIFEST_SHA256", hashlib.sha256(modified).hexdigest()):
                with self.assertRaises(ValueError):
                    rust.selected_inputs(modified)

    def component(self, root, path="bin/rustc", content=b"not executed"):
        root.mkdir()
        (root / "manifest.in").write_text(f"file:{path}\n")
        output = root / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        output.chmod(0o755)
        return root

    def test_component_merge_retains_bytes_modes_and_rejects_changed_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            component = self.component(root / "component")
            destination = root / "toolchain"
            destination.mkdir()
            before = rust.inventory(component)
            rust.merge_component(component, destination)
            self.assertEqual((destination / "bin/rustc").read_bytes(), b"not executed")
            self.assertEqual((destination / "bin/rustc").stat().st_mode & 0o777, 0o755)
            self.assertFalse((destination / "manifest.in").exists())
            self.assertEqual(rust.inventory(component), before)
            rust.merge_component(component, destination)
            changed = self.component(root / "different", content=b"different")
            with self.assertRaisesRegex(ValueError, "collision"):
                rust.merge_component(changed, destination)
            self.assertEqual((destination / "bin/rustc").read_bytes(), b"not executed")

    def test_merge_rejects_link_parent_before_creating_outside_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            component = self.component(root / "component", "lib/new/file")
            destination = root / "toolchain"
            destination.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (destination / "lib").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "traverses a link"):
                rust.merge_component(component, destination)
            self.assertEqual(list(outside.iterdir()), [])

    def test_internal_links_preserved_and_outward_links_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            component = self.component(root / "component")
            (component / "bin/rustc-alias").symlink_to("rustc")
            destination = root / "toolchain"
            destination.mkdir()
            rust.merge_component(component, destination)
            self.assertEqual((destination / "bin/rustc-alias").read_bytes(), b"not executed")
            (root / "outside").write_bytes(b"preserve")
            (component / "escape").symlink_to("../outside")
            with self.assertRaises(ValueError):
                rust.merge_component(component, destination)
            self.assertFalse((destination / "escape").exists())
            self.assertEqual((root / "outside").read_bytes(), b"preserve")

    def test_xz_archive_keeps_fixed_prefix_and_remaining_expansion_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = root / "input.tar.xz"
            prefix = "rustc-test"
            with tarfile.open(archive, "w:xz") as output:
                info = tarfile.TarInfo(prefix + "/rustc/bin/rustc")
                info.size = 3
                info.mode = 0o755
                output.addfile(info, io.BytesIO(b"abc"))
            entries = rust.extract(archive, root / "valid", prefix=prefix, max_expanded=3)
            self.assertEqual(entries["rustc/bin/rustc"]["sha256"], hashlib.sha256(b"abc").hexdigest())
            for name, kwargs in (("wrong", {"prefix": "wrong"}),
                                 ("over", {"prefix": prefix, "max_expanded": 2}),
                                 ("default", {})):
                with self.assertRaises(ValueError):
                    rust.extract(archive, root / name, **kwargs)

    def test_local_invocation_stops_before_network_or_output(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(rust.sys, "platform", "darwin"), \
                patch.object(rust, "urlopen", side_effect=AssertionError("network started")):
            destination = Path(directory) / "new"
            with self.assertRaisesRegex(ValueError, "Linux CI"):
                rust.acquire(destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
