"""Run ambient defaultspack tests without importing the pack into pytest."""

from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from typing import Final


PACK_ROOT: Final = Path(__file__).resolve().parents[1]
_CHILD_SENTINEL: Final = "TOBKIRI_DEFAULTSPACK_TEST_CHILD"
_CHILD_SENTINEL_VALUE: Final = "1"
_CHILD_PARENT_PID: Final = "TOBKIRI_DEFAULTSPACK_TEST_PARENT_PID"
_MAX_OUTPUT_BYTES: Final = 16 * 1024
_CHILD_TIMEOUT_SECONDS: Final = 30.0


def is_pack_test_child() -> bool:
    """Return whether this process is the ambient pack test child."""
    return os.environ.get(_CHILD_SENTINEL) == _CHILD_SENTINEL_VALUE and os.environ.get(
        _CHILD_PARENT_PID
    ) == str(os.getppid())


def run_pack_test(test_file: Path, node_id: str) -> None:
    """Run one test node from a fresh process rooted at the defaultspack."""
    relative_test_file = _validated_test_file(test_file)
    validated_node_id = _validated_node_id(node_id)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--rootdir",
        str(PACK_ROOT),
        "-p",
        "no:cacheprovider",
        "--import-mode=prepend",
        "--maxfail=1",
        "--tb=short",
        "--color=no",
        "-q",
        f"{relative_test_file.as_posix()}::{validated_node_id}",
    ]

    try:
        result = _run_child(command)
    except OSError as exc:
        raise AssertionError(f"could not start isolated defaultspack test child: {exc}") from exc

    if result.timed_out:
        raise AssertionError(
            "isolated defaultspack test child timed out after "
            f"{_CHILD_TIMEOUT_SECONDS:.1f}s\n"
            f"command: {_display_command(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    if result.returncode != 0:
        raise AssertionError(
            "isolated defaultspack test child failed "
            f"with exit code {result.returncode}\n"
            f"command: {_display_command(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _validated_test_file(test_file: Path) -> Path:
    resolved = test_file.resolve()
    try:
        relative = resolved.relative_to(PACK_ROOT)
    except ValueError as exc:
        raise ValueError(f"test file is outside pack root: {test_file}") from exc
    if relative.parts[:1] != ("tests",) or resolved.name == "conftest.py":
        raise ValueError(f"test file is not an isolated pack test: {test_file}")
    return relative


def _validated_node_id(node_id: str) -> str:
    if not node_id or "\x00" in node_id or node_id.startswith("-"):
        raise ValueError(f"invalid isolated test node id: {node_id!r}")
    for component in node_id.split("::"):
        if not component or not (component[0].isalpha() or component[0] == "_"):
            raise ValueError(f"invalid isolated test node id: {node_id!r}")
        if not all(character.isalnum() or character == "_" for character in component):
            raise ValueError(f"invalid isolated test node id: {node_id!r}")
    return node_id


def _child_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        _CHILD_SENTINEL: _CHILD_SENTINEL_VALUE,
        _CHILD_PARENT_PID: str(os.getpid()),
    }


class _ChildResult:
    def __init__(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


def _run_child(command: list[str]) -> _ChildResult:
    child = subprocess.Popen(
        command,
        cwd=PACK_ROOT,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    assert child.stdout is not None
    assert child.stderr is not None

    output = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    stream_names = {child.stdout: "stdout", child.stderr: "stderr"}
    selector = selectors.DefaultSelector()
    for stream, stream_name in stream_names.items():
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, stream_name)

    timed_out = False
    deadline = time.monotonic() + _CHILD_TIMEOUT_SECONDS
    drain_deadline: float | None = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if timed_out:
                assert drain_deadline is not None
                remaining = drain_deadline - now
                if remaining <= 0:
                    break
                wait_timeout = min(remaining, 0.1)
            else:
                remaining = deadline - now
                if remaining <= 0:
                    timed_out = True
                    _terminate_child(child)
                    drain_deadline = time.monotonic() + 1.0
                    continue
                wait_timeout = min(remaining, 0.1)

            for key, _ in selector.select(wait_timeout):
                stream = key.fileobj
                stream_name = key.data
                try:
                    chunk = os.read(stream.fileno(), 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue

                captured = output[stream_name]
                remaining_capacity = _MAX_OUTPUT_BYTES - len(captured)
                if remaining_capacity <= 0:
                    truncated[stream_name] = True
                else:
                    captured.extend(chunk[:remaining_capacity])
                    if len(chunk) > remaining_capacity:
                        truncated[stream_name] = True
    finally:
        selector.close()
        child.stdout.close()
        child.stderr.close()

    if timed_out:
        _terminate_child(child)
    returncode = child.wait()
    return _ChildResult(
        returncode=returncode,
        stdout=_decode_output(output["stdout"], truncated["stdout"]),
        stderr=_decode_output(output["stderr"], truncated["stderr"]),
        timed_out=timed_out,
    )


def _terminate_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        child.terminate()
    try:
        child.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            child.kill()
        child.wait()


def _decode_output(data: bytearray, truncated: bool) -> str:
    text = bytes(data).decode("utf-8", errors="replace")
    if truncated:
        text += f"\n[output truncated at {_MAX_OUTPUT_BYTES} bytes]"
    return text


def _display_command(command: list[str]) -> str:
    return " ".join(repr(argument) for argument in command)
