"""Exercise the real sealed role loader, including dataclass registration."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["canonical", "failed_import", "occupied_name"])
def test_sealed_role_loader_module_registration(tmp_path: Path, mode: str) -> None:
    app = tmp_path / "app"
    target = app / "ecosystem/defaultspack/defaultspack/desktop_app.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(
        ROOT / ".github/scripts/sealed_python_sources/app/defaultspack_entry.py",
        app / "defaultspack_entry.py",
    )
    if mode == "failed_import":
        target.write_text("raise RuntimeError('deliberate import failure')\n")
    else:
        shutil.copyfile(
            ROOT / "tobkiri_runtime/ecosystem/defaultspack/defaultspack/desktop_app.py", target
        )
    code = '''
import importlib.util, sys, types
from pathlib import Path
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("entry_probe", root / "defaultspack_entry.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
mode = sys.argv[2]
name = "tobkiri_packaged_defaultspack"
if mode == "occupied_name":
    prior = types.ModuleType(name)
    sys.modules[name] = prior
    try:
        entry._load_desktop_app_module()
    except RuntimeError as error:
        assert "already occupied" in str(error)
    else:
        raise AssertionError("must reject occupied module name")
    assert sys.modules[name] is prior
    assert entry._TARGET_MODULE is None
elif mode == "failed_import":
    try:
        entry._load_desktop_app_module()
    except RuntimeError as error:
        assert str(error) == "deliberate import failure"
    else:
        raise AssertionError("must propagate import failure")
    assert name not in sys.modules
    assert entry._TARGET_MODULE is None
else:
    module = entry._load_desktop_app_module()
    assert module is entry._load_desktop_app_module()
    assert sys.modules[name] is module
    assert module._StartupFailure("kind", "message", "action").kind == "kind"
    assert callable(entry._load_desktop_app_main())
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(app), mode],
        cwd=tmp_path, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
