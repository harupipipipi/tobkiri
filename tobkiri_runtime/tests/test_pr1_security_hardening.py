from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core_runtime.capability_proxy import HostCapabilityProxyServer
from core_runtime.capability_trust_store import CapabilityTrustStore
from core_runtime.rate_limit_store import PersistentRateLimitStore


def _run_function_runner(
    tmp_path: Path,
    source: str,
    *,
    deny_child_process: bool = False,
) -> subprocess.CompletedProcess[str]:
    module_path = tmp_path / "callable_module.py"
    module_path.write_text(source, encoding="utf-8")
    runner_path = Path(__file__).resolve().parents[1] / "core_runtime" / "function_runner.py"
    payload = {
        "module_path": str(module_path),
        "callable_name": "run",
        "context": {"principal_id": "alice"},
        "args": {"value": 41},
    }
    environment = dict(os.environ)
    if deny_child_process:
        environment["RUMI_SANDBOX_DENY_CHILD_PROCESS"] = "1"
    return subprocess.run(
        [sys.executable, str(runner_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def test_function_runner_executes_callable_from_json_stdin(tmp_path):
    proc = _run_function_runner(
        tmp_path,
        "def run(context, args):\n"
        "    return {'principal_id': context['principal_id'], "
        "'value': args['value'] + 1}\n",
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"principal_id": "alice", "value": 42}


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="The production Pack function filter is installed inside Linux guests.",
)
@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess\n"
            "def run(context, args):\n"
            "    subprocess.run(['/bin/sh', '-c', 'exit 0'], check=False)\n"
            "    return {'unexpected': True}\n"
        ),
        (
            "import os\n"
            "def run(context, args):\n"
            "    os.system('/bin/sh -c true')\n"
            "    return {'unexpected': True}\n"
        ),
        (
            "import os\n"
            "def run(context, args):\n"
            "    os.execv('/bin/sh', ['/bin/sh', '-c', 'exit 0'])\n"
        ),
        (
            "import os\n"
            "def run(context, args):\n"
            "    os.posix_spawn('/bin/sh', ['/bin/sh', '-c', 'exit 0'], {})\n"
            "    return {'unexpected': True}\n"
        ),
        (
            "import os\n"
            "def run(context, args):\n"
            "    os.spawnv(os.P_WAIT, '/bin/sh', ['/bin/sh', '-c', 'exit 0'])\n"
            "    return {'unexpected': True}\n"
        ),
        (
            "import os\n"
            "def run(context, args):\n"
            "    child = os.fork()\n"
            "    if child == 0:\n"
            "        os._exit(0)\n"
            "    os.waitpid(child, 0)\n"
            "    return {'unexpected': True}\n"
        ),
    ],
)
def test_function_runner_denies_python_child_process_variants(
    tmp_path: Path,
    source: str,
) -> None:
    proc = _run_function_runner(tmp_path, source, deny_child_process=True)

    assert proc.returncode == 1
    assert proc.stderr == ""
    assert json.loads(proc.stdout) == {
        "error": "Sandbox Pack functions cannot create child processes",
        "error_type": "sandbox_policy_denied",
    }


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="The production Pack function filter is installed inside Linux guests.",
)
def test_function_runner_seccomp_denies_native_system_bypass(tmp_path: Path) -> None:
    proc = _run_function_runner(
        tmp_path,
        "import ctypes\n"
        "def run(context, args):\n"
        "    libc = ctypes.CDLL(None, use_errno=True)\n"
        "    result = libc.system(b'/bin/sh -c true')\n"
        "    return {'result': result, 'errno': ctypes.get_errno()}\n",
        deny_child_process=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"result": -1, "errno": errno.EPERM}


def test_persistent_rate_limit_store_persists_across_instances(tmp_path):
    db_path = tmp_path / "rate_limits.db"
    first = PersistentRateLimitStore(db_path)
    second = PersistentRateLimitStore(db_path)

    assert first.allow(principal_id="p1", scope="secrets.get", limit=2, now=120.0)
    assert second.allow(principal_id="p1", scope="secrets.get", limit=2, now=121.0)
    assert not first.allow(principal_id="p1", scope="secrets.get", limit=2, now=122.0)


def test_capability_trust_store_save_uses_atomic_replace(tmp_path):
    store = CapabilityTrustStore(str(tmp_path / "trust"))

    assert store.add_trust("handler.test", "a" * 64, "note")

    trust_file = tmp_path / "trust" / "trusted_handlers.json"
    assert trust_file.exists()
    assert not list((tmp_path / "trust").glob("*.tmp"))

    saved = json.loads(trust_file.read_text(encoding="utf-8"))
    assert saved["trusted"][0]["handler_id"] == "handler.test"
    assert "_hmac_signature" in saved


def test_windows_capability_tcp_fallback_defaults_to_deny(monkeypatch):
    cp_globals = HostCapabilityProxyServer.ensure_principal_socket.__globals__
    monkeypatch.setitem(cp_globals, "_IS_WINDOWS", True)
    monkeypatch.setitem(cp_globals, "_windows_tcp_fallback_enabled", lambda: False)
    monkeypatch.delenv("RUMI_ALLOW_WINDOWS_TCP_FALLBACK", raising=False)

    server = HostCapabilityProxyServer()
    success, error, path = server.ensure_principal_socket("principal-1")

    assert success is False
    assert path is None
    assert "RUMI_ALLOW_WINDOWS_TCP_FALLBACK" in error
