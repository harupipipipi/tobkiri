"""Review the staged WASI inputs without invoking a compiler."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_wasi
from build_native import HERE, fingerprint, sandbox_command


class WasiStageTests(unittest.TestCase):
    def test_installed_target_configuration_is_bound_to_the_generated_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve()
            headers = work / "wasi/install/include/python3.13"
            headers.mkdir(parents=True)
            for name in ("Python.h", "patchlevel.h"):
                (headers / name).write_text("/* synthetic target header */\n")
            generated = work / "wasi/pyconfig.h"
            generated.write_text("#define SIZEOF_VOID_P 4\n")
            (headers / "pyconfig.h").write_bytes(generated.read_bytes())
            result = build_wasi.target_headers(work)
            self.assertEqual(result["generated_pyconfig"], fingerprint(generated))
            (headers / "pyconfig.h").write_text("#define SIZEOF_VOID_P 8\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                build_wasi.target_headers(work)
            (headers / "pyconfig.h").unlink()
            (headers / "pyconfig.h").symlink_to(headers / "Python.h")
            with self.assertRaisesRegex(ValueError, "regular"):
                build_wasi.target_headers(work)

    def test_all_recipes_are_the_recorded_historical_bytes(self):
        for name, digest, _ in build_wasi.STAGES:
            recipe = HERE / f"trials/2026-10-19/{name}.sh"
            self.assertEqual(fingerprint(recipe)["sha256"], digest)

    def test_completed_inputs_are_read_only_inside_every_stage(self):
        source, native, sdk, work = map(Path, ("/source", "/native", "/sdk", "/work"))
        for name, _, _ in build_wasi.STAGES:
            mounts = build_wasi.mounts(source, native, sdk, work, name)
            command = sandbox_command(source, work, Path("/recipe"), mounts)
            self.assertIn((Path("/native/work/native2"), "/work/native2"), mounts)
            self.assertIn((sdk, "/sdk"), mounts)
            self.assertIn("--unshare-all", command)
            self.assertEqual(command.count("--bind"), 1)
            for path, mount in mounts:
                at = command.index(mount, command.index(str(path)) + 1)
                self.assertEqual(command[at - 2:at], ["--ro-bind", str(path)])
        cpython = build_wasi.mounts(source, native, sdk, work, "wasi-cpython")
        self.assertIn((Path("/work/wasi/deps"), "/work/wasi/deps"), cpython)
        sqlite = build_wasi.mounts(source, native, sdk, work, "wasi-sqlite")
        for output in build_wasi.STAGES[0][2]:
            self.assertIn((work / output, "/work/" + output), sqlite)
        with self.assertRaisesRegex(ValueError, "unrecorded"):
            build_wasi.mounts(source, native, sdk, work, "top-level-cargo")

    def test_local_execution_is_refused_without_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            with patch.dict(build_wasi.os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "Linux CI"):
                    build_wasi.run(Path(tmp), Path(tmp), output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
