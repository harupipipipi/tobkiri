"""The guest zipapp carries only its explicit pure continuation dependencies."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from scripts.build_packvm_guest_bundle import build_guest_bundle, main
from ecosystem.defaultspack.backend.sandbox.isolation.resources import packvm_guest_runner

ROOT = Path(__file__).resolve().parents[1]
MEMBERS = {
    "__main__.py",
    "tobkiri_host/__init__.py",
    "tobkiri_host/bounded_child_io.py",
    "tobkiri_host/continuation_chain.py",
    "tobkiri_host/continuation_envelope.py",
    "tobkiri_host/continuation_session.py",
    "tobkiri_host/saved_guest_dispatch.py",
    "tobkiri_protocol/__init__.py",
    "tobkiri_protocol/canonical.py",
    "tobkiri_protocol/errors.py",
    "tobkiri_protocol/saved_conversation.py",
}


def test_bundle_is_deterministic_and_has_exact_data_only_closure() -> None:
    content = build_guest_bundle(ROOT)
    assert content == build_guest_bundle(ROOT)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert set(archive.namelist()) == MEMBERS
        assert archive.namelist() == sorted(MEMBERS)
        for entry in archive.infolist():
            assert entry.compress_type == zipfile.ZIP_STORED
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            relative = entry.filename
            if relative == "__main__.py":
                source = Path(packvm_guest_runner.__file__)
            elif relative.endswith("__init__.py"):
                assert archive.read(relative) == b""
                continue
            else:
                source = ROOT / relative
            assert archive.read(relative) == source.read_bytes()


def test_bundle_runs_doctor_with_only_isolated_stdlib(tmp_path: Path) -> None:
    runner = tmp_path / "runner.py"
    runner.write_bytes(build_guest_bundle(ROOT))
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(runner)],
        input=json.dumps({"operation": "doctor"}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {
        "ok": True,
        "protocol": packvm_guest_runner.PROTOCOL,
        "build_id": packvm_guest_runner.BUILD_ID,
    }


def test_archive_exposes_sealing_and_chain_without_ambient_host_packages(tmp_path: Path) -> None:
    runner = tmp_path / "runner.py"
    runner.write_bytes(build_guest_bundle(ROOT))
    probe = """
import json, sys
sys.path.insert(0, sys.argv[1])
from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
from tobkiri_host.continuation_envelope import seal_continuation_intent
from tobkiri_host.continuation_session import ContinuationSession
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tobkiri_protocol.canonical import canonical_json
identity = ChainIdentity('domain', 'request', 'sha256:' + 'a' * 64, 60.0)
assert ContinuationSession.__module__ == 'tobkiri_host.continuation_session'
saved_input = {'request': {'turn_id': 'turn', 'conversation_id': 'conversation',
                         'conversation_revision': 1, 'content': 'hello'}}
assert validate_saved_conversation_input(saved_input) == saved_input
assert 'jsonschema' not in sys.modules
intent = {'kind': 'tobkiri.packvm.continuation.intent.v2', 'hop': 0,
          'target': {'contract_id': 'owner.v1', 'operation_id': 'read'},
          'payload': {}, 'state': {}}
frame = seal_continuation_intent(canonical_json(intent), identity=identity,
    hop=0, previous_digest=None, target=('owner.v1', 'read'), nonce='b' * 48)
chain = ContinuationChains(clock=lambda: 1.0)
chain.start(identity, frame=frame.frame, nonce=frame.nonce)
permit = chain.take(identity, nonce=frame.nonce, result=b'result')
chain.finish(permit)
try:
    import tobkiri_host.broker
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('Host dispatcher leaked into guest archive')
print(json.dumps({'request_id': frame.identity.request_id, 'hop': frame.hop}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", probe, str(runner)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {"request_id": "request", "hop": 0}


def test_sandbox_binds_archive_not_its_virtual_main_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = tmp_path / "runner.py"
    runner.write_bytes(build_guest_bundle(ROOT))
    target = tmp_path / "artifact"
    monkeypatch.setattr(packvm_guest_runner, "__file__", str(runner / "__main__.py"))
    monkeypatch.setattr(packvm_guest_runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv = packvm_guest_runner._sandbox_argv(target, target / "handler.py")
    assert str(runner) in argv
    assert str(runner / "__main__.py") not in argv
    assert ("--ro-bind", str(runner), "/runner.py") == tuple(
        argv[argv.index(str(runner)) - 1 : argv.index(str(runner)) + 2]
    )
    runner.unlink()
    with pytest.raises(ValueError, match="archive is unavailable"):
        packvm_guest_runner._sandbox_argv(target, target / "handler.py")


def test_builder_rejects_linked_source_instead_of_discovering_foreign_code(tmp_path: Path) -> None:
    for member in MEMBERS - {"tobkiri_host/__init__.py", "tobkiri_protocol/__init__.py"}:
        relative = (
            "ecosystem/defaultspack/backend/sandbox/isolation/resources/packvm_guest_runner.py"
            if member == "__main__.py"
            else member
        )
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    source = tmp_path / "tobkiri_host/continuation_chain.py"
    source.unlink()
    foreign = tmp_path / "foreign.py"
    shutil.copyfile(ROOT / "tobkiri_host/continuation_chain.py", foreign)
    os.link(foreign, source)
    try:
        with pytest.raises(ValueError, match="single-link"):
            build_guest_bundle(tmp_path)
    finally:
        source.unlink()


def test_packaging_invokes_bundle_builder_before_runner_digest_is_bound() -> None:
    script = (ROOT.parent / "tobkiri_launcher/scripts/build_packvm_vz_helper.sh").read_text()
    assert script.index("scripts/build_packvm_guest_bundle.py") < script.index(
        'service["guest_runner_sha256"] ='
    )


def test_cli_verification_rejects_changed_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "runner.py"
    arguments = ["builder", "--runtime-root", str(ROOT), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", arguments)
    assert main() == 0
    monkeypatch.setattr(sys, "argv", [*arguments, "--check"])
    assert main() == 0
    output.chmod(0o600)
    output.write_bytes(output.read_bytes() + b"unexpected trailing input")
    with pytest.raises(ValueError, match="canonical source closure"):
        main()


def test_cli_refuses_output_link_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "preserved.py"
    target.write_bytes(b"preserved")
    output = tmp_path / "runner.py"
    try:
        output.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links unavailable on this test host")
    monkeypatch.setattr(
        sys, "argv", ["builder", "--runtime-root", str(ROOT), "--output", str(output)]
    )
    with pytest.raises(ValueError, match="symlink"):
        main()
    assert target.read_bytes() == b"preserved"
