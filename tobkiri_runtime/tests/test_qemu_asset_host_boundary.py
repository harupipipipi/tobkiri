"""Host QEMU supervisors must not import a concrete product Pack."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", [
    "tobkiri_host.linux_qemu_supervisor",
    "tobkiri_host.windows_whpx_supervisor",
    "tobkiri_host.windows_pe_dependencies",
])
def test_host_module_import_does_not_load_product_pack(module):
    script = """
import builtins
import importlib
import sys
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name == 'ecosystem' or name.startswith('ecosystem.'):
        raise AssertionError('Host must not import a product Pack: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = checked
importlib.import_module(sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, module],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
