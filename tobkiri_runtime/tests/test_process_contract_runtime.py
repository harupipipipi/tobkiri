from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core_runtime.capability_binding_registration import _ProcessContractOperation

pytestmark = pytest.mark.contract


def test_process_contract_disables_bytecode_with_isolated_python(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(stdout=json.dumps({"status": "ok", "value": {}}))

    monkeypatch.setattr(
        "core_runtime.capability_binding_registration.subprocess.run",
        fake_run,
    )
    location = SimpleNamespace(pack_dir=tmp_path / "ecosystem" / "sample_pack")

    _ProcessContractOperation(
        module="ecosystem.sample_pack.runtime.process",
        pack_location=location,
    )("list", {})

    command = captured["command"]
    assert command[1:4] == ["-B", "-s", "-E"]
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_process_contract_real_child_keeps_bundle_tree_bytecode_free(
    monkeypatch,
    tmp_path,
):
    runtime_root = tmp_path / "runtime-root"
    pack_dir = runtime_root / "ecosystem" / "sample_pack"
    module_dir = pack_dir / "runtime"
    module_dir.mkdir(parents=True)
    for package_dir in (runtime_root / "ecosystem", pack_dir, module_dir):
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "sibling.py").write_text("VALUE = 'loaded'\n", encoding="utf-8")
    (module_dir / "process.py").write_text(
        "import json, os, sys\n"
        "from .sibling import VALUE\n"
        "request = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'status': 'ok', 'value': {\n"
        "    'value': VALUE,\n"
        "    'dont_write_bytecode': sys.dont_write_bytecode,\n"
        "    'ignore_environment': sys.flags.ignore_environment,\n"
        "    'no_user_site': sys.flags.no_user_site,\n"
        "    'user_data': os.environ.get('RUMI_USER_DATA'),\n"
        "    'secret_visible': 'PROCESS_CONTRACT_TEST_SECRET' in os.environ,\n"
        "}}))\n",
        encoding="utf-8",
    )
    user_data = tmp_path / "Application Support" / "Tobkiri"
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv("PROCESS_CONTRACT_TEST_SECRET", "must-not-leak")

    result = _ProcessContractOperation(
        module="ecosystem.sample_pack.runtime.process",
        pack_location=SimpleNamespace(pack_dir=pack_dir),
    )("inspect", {})

    assert result == {
        "value": "loaded",
        "dont_write_bytecode": True,
        "ignore_environment": 1,
        "no_user_site": 1,
        "user_data": str(user_data),
        "secret_visible": False,
    }
    assert not list(runtime_root.rglob("__pycache__"))
    assert not list(runtime_root.rglob("*.pyc"))
