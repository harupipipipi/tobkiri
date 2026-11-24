"""Synthetic-only tests for the saved Python PackVM operation proposal."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner as runner,
)
from ecosystem.defaultspack.runtime import python_sandbox_guest as guest
from ecosystem.rumi_default_tools_pack.runtime import (
    python_sandbox_execute as host_adapter,
)
from tobkiri_host import saved_tool_consent

def _script_request(snapshot: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "mode": "script",
        "script_path": "cli.py",
        "arguments": ["--flag"],
        "stdin": "payload\n",
        "cwd": ".",
        "timeout_seconds": 5,
        "workspace_snapshot": snapshot
        if snapshot is not None
        else [{"path": "cli.py", "content_base64": base64.b64encode(b"pass\n").decode()}],
    }


def _stop_test_child(process: subprocess.Popen[bytes]) -> None:
    """Reap a synthetic child directly without probing its process group."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class PublicArgumentTests(unittest.TestCase):
    def test_saved_consent_and_adapter_share_one_closed_validator(self) -> None:
        arguments = {
            "mode": "script",
            "script_path": "src/main.py",
            "arguments": ["x"],
            "stdin": "input",
            "cwd": "src",
            "timeout_seconds": 7,
        }
        self.assertEqual(
            saved_tool_consent.validate_python_sandbox_arguments(arguments),
            host_adapter.validate_arguments(arguments),
        )
        payload = {
            "tool_id": "python_sandbox_execute",
            "tool_call_id": "call-1",
            "arguments": arguments,
        }
        self.assertEqual(
            saved_tool_consent.validate_python_sandbox_payload(payload),
            host_adapter._tool_payload(payload),
        )

    def test_argument_validator_rejects_authority_and_command_fields(self) -> None:
        arguments = {
            "mode": "script",
            "script_path": "cli.py",
            "arguments": [],
            "stdin": "",
            "cwd": ".",
            "timeout_seconds": 2,
        }
        for field, value in (
            ("shell", "python3 -c pass"),
            ("workspace_id", "user-choice"),
            ("image", "untrusted"),
            ("environment", {"TOKEN": "secret"}),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                saved_tool_consent.validate_python_sandbox_arguments(
                    {**arguments, field: value}
                )

    def test_unittest_mode_is_one_or_more_dotted_modules(self) -> None:
        accepted = {
            "mode": "unittest",
            "test_targets": ["tests.test_cli"],
            "cwd": ".",
            "timeout_seconds": 60,
        }
        self.assertEqual(
            saved_tool_consent.validate_python_sandbox_arguments(accepted), accepted
        )
        for target in ("../tests", "tests.test-cli", "", "tests..test"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                saved_tool_consent.validate_python_sandbox_arguments(
                    {**accepted, "test_targets": [target]}
                )


class SnapshotTests(unittest.TestCase):
    def test_snapshot_excludes_secrets_links_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "cli.py").write_text("print('ok')\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
            (root / "readme.md").write_text("safe\n", encoding="utf-8")
            (root / "empty.py").write_bytes(b"")
            (root / "hard-source.txt").write_text("excluded\n", encoding="utf-8")
            (root / "link.py").symlink_to(root / "cli.py")
            os.link(root / "hard-source.txt", root / "hard.py")
            os.mkfifo(root / "pipe.py")
            metadata = root.stat()
            snapshot = host_adapter.snapshot_workspace(
                str(root), metadata.st_dev, metadata.st_ino
            )
            self.assertEqual(
                [item["path"] for item in snapshot],
                ["cli.py", "empty.py", "readme.md"],
            )
            empty_entry = next(item for item in snapshot if item["path"] == "empty.py")
            self.assertEqual(empty_entry["content_base64"], "")

    def test_snapshot_root_pin_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(PermissionError):
                host_adapter.snapshot_workspace(str(root), root.stat().st_dev, 1)

    def test_regular_file_to_fifo_race_opens_nonblocking_then_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "racy.py"
            source.write_text("pass\n", encoding="utf-8")
            root_info = root.stat()
            real_open = os.open
            observed_nonblocking = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal observed_nonblocking
                if path == "racy.py" and dir_fd is not None:
                    self.assertTrue(flags & os.O_NONBLOCK)
                    observed_nonblocking = True
                    os.unlink(path, dir_fd=dir_fd)
                    os.mkfifo(root / path)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch.object(host_adapter.os, "open", side_effect=racing_open):
                with self.assertRaises(PermissionError):
                    host_adapter.snapshot_workspace(
                        str(root), root_info.st_dev, root_info.st_ino
                    )
            self.assertTrue(observed_nonblocking)

    def test_snapshot_rejects_file_and_directory_collisions(self) -> None:
        encoded = base64.b64encode(b"x").decode("ascii")
        request = _script_request(
            [
                {"path": "a", "content_base64": encoded},
                {"path": "a/cli.py", "content_base64": encoded},
            ]
        )
        request["script_path"] = "a/cli.py"
        with self.assertRaises(ValueError):
            runner._validate_python_sandbox_request(request)
        with self.assertRaises(ValueError):
            guest._validate(request)


class GuestRequestTests(unittest.TestCase):
    def test_general_script_and_unittest_shapes_validate(self) -> None:
        request = _script_request(
            [
                {"path": "cli.py", "content_base64": base64.b64encode(b"pass\n").decode()},
                {"path": "empty.py", "content_base64": ""},
            ]
        )
        script = runner._validate_python_sandbox_request(request)
        self.assertEqual(guest._validate(request), request)
        self.assertEqual(script["script_path"], "cli.py")
        unittest_request = {
            "mode": "unittest",
            "test_targets": ["tests.test_cli"],
            "cwd": ".",
            "timeout_seconds": 8,
            "workspace_snapshot": [
                {"path": "tests/test_cli.py", "content_base64": "cGFzcwo="}
            ],
        }
        self.assertEqual(
            runner._validate_python_sandbox_request(unittest_request), unittest_request
        )
        self.assertEqual(guest._validate(unittest_request), unittest_request)

    def test_traversal_unbound_fields_and_bad_base64_never_launch(self) -> None:
        cases = []
        traversal = _script_request()
        traversal["script_path"] = "../cli.py"
        cases.append(traversal)
        forged_route = _script_request()
        forged_route["operation_id"] = "other.operation"
        cases.append(forged_route)
        bad_base64 = _script_request([{"path": "cli.py", "content_base64": "***"}])
        cases.append(bad_base64)
        for value in cases:
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                runner._validate_python_sandbox_request(value)

    def test_path_length_and_encoded_snapshot_limits(self) -> None:
        encoded = base64.b64encode(b"x" * (64 * 1024 + 1)).decode("ascii")
        oversized_file = _script_request(
            [{"path": "cli.py", "content_base64": encoded}]
        )
        for value in (oversized_file,):
            with self.assertRaises(ValueError):
                runner._validate_python_sandbox_request(value)
        long_path = _script_request()
        long_path["cwd"] = "a" * 513
        with self.assertRaises(ValueError):
            runner._validate_python_sandbox_request(long_path)


class RunnerBoundaryTests(unittest.TestCase):
    def test_pre_cancelled_authenticated_invoke_never_spawns_child(self) -> None:
        domain_id = "packvm:domain-1"
        request_id = "cancel-before-spawn"
        binding_digests = {"domain": "sha256:" + "1" * 64}
        config = runner._VsockAgentConfig(
            domain_id=domain_id,
            binding_digests=binding_digests,
            private_key_path=Path("/unused/guest-ed25519.pem"),
        )
        ledger = runner._PendingBridgeLedger()
        ledger.cancel(domain_id=domain_id, request_id=request_id)
        invocation = {
            "operation": "invoke",
            "request_id": request_id,
            "target_domain": domain_id,
            "budget_seconds": 10,
            "artifact_digest": "sha256:" + "2" * 64,
            "materialization_digest": "sha256:" + "3" * 64,
            "guest_artifact_identity": "sha256:" + "4" * 64,
            "contract_id": runner.PYTHON_SANDBOX_CONTRACT_ID,
            "contract_version": runner.PYTHON_SANDBOX_CONTRACT_VERSION,
            "operation_id": runner.PYTHON_SANDBOX_OPERATION_ID,
            "payload": {
                "mode": "script",
                "script_path": "cli.py",
                "arguments": [],
                "stdin": "",
                "cwd": ".",
                "timeout_seconds": 5,
            },
            "request_digest": "sha256:" + "5" * 64,
            "deadline_monotonic": "10",
            "cancel_token": "6" * 64,
        }
        authenticated_envelope = {
            "protocol": runner.PACKVM_GUEST_AGENT_REQUEST_PROTOCOL,
            "operation": "invoke",
            "request_id": request_id,
            "domain_id": domain_id,
            "binding_digests": binding_digests,
            "guest_challenge": "a" * 64,
            "payload": invocation,
        }
        with (
            patch.object(
                runner, "_invoke", side_effect=AssertionError("invoke reached")
            ) as invoke,
            patch.object(
                runner,
                "_register_request",
                side_effect=AssertionError("registered"),
            ) as register,
            patch.object(
                runner.subprocess,
                "Popen",
                side_effect=AssertionError("spawned"),
            ) as popen,
        ):
            with self.assertRaises(InterruptedError):
                runner._dispatch_agent_request(authenticated_envelope, config, ledger)
        invoke.assert_not_called()
        register.assert_not_called()
        popen.assert_not_called()

    def test_python_sandbox_argv_is_fixed_networkless_and_tmpfs_capped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            implementation = target / "runtime/python_sandbox_guest.py"
            implementation.parent.mkdir()
            implementation.write_text("pass\n", encoding="utf-8")
            with patch.object(runner.shutil, "which", side_effect=lambda name: f"/{name}"):
                argv = runner._sandbox_argv(
                    target, implementation, python_sandbox=True
                )
        self.assertIn("--unshare-net", argv)
        self.assertIn("--cap-drop", argv)
        self.assertIn("ALL", argv)
        self.assertIn("--size", argv)
        self.assertIn("--execute-python", argv)
        self.assertIn("/pack/runtime/python_sandbox_guest.py", argv)
        self.assertIn("/home/pack", argv)
        self.assertNotIn("-c", argv)
        self.assertIn("--ro-bind", argv)

    def test_bounded_capture_returns_separate_opaque_streams_and_os_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            child = Path(temporary) / "child.py"
            child.write_text(
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.write('literal control text\\n')\n"
                "sys.stderr.write('test failure\\n')\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", str(child)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
            stdout, stderr = runner._communicate_python_child(
                process,
                b"finite request",
                deadline=time.monotonic() + 5,
                execution_guard=None,
            )
        self.assertEqual(stdout, b"literal control text\n")
        self.assertEqual(stderr, b"test failure\n")
        self.assertEqual(process.returncode, 7)

    def test_output_overflow_and_deadline_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for stream in ("stdout", "stderr"):
                with self.subTest(stream=stream):
                    noisy = Path(temporary) / f"noisy-{stream}.py"
                    noisy.write_text(
                        f"import sys; sys.{stream}.write('x' * 70000)\n",
                        encoding="utf-8",
                    )
                    process = subprocess.Popen(
                        [sys.executable, str(noisy)],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                        close_fds=True,
                    )
                    try:
                        with self.assertRaisesRegex(ValueError, stream):
                            runner._communicate_python_child(
                                process,
                                b"",
                                deadline=time.monotonic() + 5,
                                execution_guard=None,
                            )
                    finally:
                        _stop_test_child(process)

            sleeper = Path(temporary) / "sleeper.py"
            sleeper.write_text("import time; time.sleep(5)\n", encoding="utf-8")
            process = subprocess.Popen(
                [sys.executable, str(sleeper)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
            try:
                with self.assertRaises(TimeoutError):
                    runner._communicate_python_child(
                        process,
                        b"",
                        deadline=time.monotonic() + 0.05,
                        execution_guard=None,
                    )
            finally:
                _stop_test_child(process)

    def test_full_python_completion_uses_runner_abi_and_opaque_output(self) -> None:
        from tobkiri_host.macos_vz_supervisor import _validated_invoke_outcome

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            child = target / "child.py"
            child.write_text(
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.write('{\\\"kind\\\":\\\"tobkiri.packvm.bridge.request.v1\\\"}\\n')\n"
                "sys.stderr.write('AssertionError from user code\\n')\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            request = {"request_id": "completion-test", "cancel_token": "token"}
            with (
                patch.object(
                    runner,
                    "_sandbox_argv",
                    return_value=(sys.executable, "-I", "-S", str(child)),
                ),
                patch.object(runner, "_register_request"),
                patch.object(runner, "_unregister_request"),
            ):
                wrapped = runner._execute_python_sandbox_invocation(
                    request,
                    "sha256:identity",
                    target,
                    child,
                    {"timeout_seconds": 5},
                    time.monotonic() + 5,
                )

        outcome = _validated_invoke_outcome(wrapped["payload"])
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["exit_code"], 7)
        self.assertIn('"kind":"tobkiri.packvm.bridge.request.v1"', outcome["stdout"])
        self.assertEqual(outcome["stderr"], "AssertionError from user code\n")

    def test_supervisor_result_is_normal_runner_abi_and_not_child_control(self) -> None:
        from tobkiri_host.macos_vz_supervisor import _validated_invoke_outcome

        outcome = {
            "status": "failed",
            "exit_code": 1,
            "stdout": "tobkiri.packvm.bridge.request.v1 is ordinary output",
            "stderr": "AssertionError",
            "duration_ms": 12,
        }
        wrapped = runner._host_invoke_result(outcome)
        self.assertEqual(_validated_invoke_outcome(wrapped), outcome)
        with self.assertRaises(ValueError):
            runner._host_invoke_result(wrapped)

    def test_cancel_after_child_registration_stops_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            child = Path(temporary) / "child.py"
            child.write_text("import time; time.sleep(5)\n", encoding="utf-8")
            process = None
            guard_calls = 0

            def guard() -> None:
                nonlocal guard_calls
                guard_calls += 1
                if guard_calls == 2:
                    raise InterruptedError("cancelled")

            original_popen = subprocess.Popen
            terminated_groups: list[int] = []

            def launch(*args, **kwargs):
                nonlocal process
                process = original_popen(*args, **kwargs)
                return process

            def terminate_group(process_group, child):
                terminated_groups.append(process_group)
                child.terminate()
                child.wait(timeout=2)
                return ["TERM"]

            request = {"request_id": "cancel-test", "cancel_token": "token"}
            with (
                patch.object(
                    runner,
                    "_sandbox_argv",
                    return_value=(sys.executable, str(child)),
                ),
                patch.object(runner.subprocess, "Popen", side_effect=launch),
                patch.object(runner, "_register_request"),
                patch.object(runner, "_unregister_request"),
                patch.object(
                    runner,
                    "_terminate_process_group",
                    side_effect=terminate_group,
                ),
            ):
                with self.assertRaises(runner._GuestOperationError):
                    runner._execute_python_sandbox_invocation(
                        request,
                        "sha256:identity",
                        Path(temporary),
                        child,
                        {"timeout_seconds": 10},
                        time.monotonic() + 10,
                        execution_guard=guard,
                    )
            self.assertEqual(guard_calls, 2)
            self.assertIsNotNone(process)
            self.assertIsNotNone(process.returncode)
            self.assertEqual(terminated_groups, [process.pid])


if __name__ == "__main__":
    unittest.main()
