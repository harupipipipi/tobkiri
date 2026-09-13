"""Legacy token updates are one-shot across independent callers."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from tobkiri_protocol.secure_persistence import SecureDirectory

from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import (
    BrowserComputerController,
)
from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion import (
    BrowserCompanionController,
)
from ecosystem.rumi_default_tools_pack.domain.tool.legacy_approval_lock import (
    legacy_approval_lock,
)

pytestmark = pytest.mark.contract
ACTION = "computer.semantic_action"
PAYLOAD = {"intent": "test only"}


def _controller(path: Path, kind="computer"):
    cls = BrowserComputerController if kind == "computer" else BrowserCompanionController
    controller = cls.__new__(cls)
    controller._approval_path = path
    if kind == "companion":
        controller._issue_legacy_approval = controller._issue_approval
        controller._consume_legacy_approval = lambda token, action, payload: (
            controller._consume_approval({"approval_token": token}, action, payload)
        )
    return controller


def _consume_in_process(path, token, ready, start, results, kind):
    controller = _controller(Path(path), kind)
    original = controller._read_approvals

    def slow_read():
        snapshot = original()
        time.sleep(0.05)
        return snapshot

    controller._read_approvals = slow_read
    ready.put(True)
    if not start.wait(10):
        raise TimeoutError("test start was not signaled")
    results.put(controller._consume_legacy_approval(token, ACTION, PAYLOAD))


@pytest.mark.parametrize("kind", ["computer", "companion"])
def test_same_token_is_consumed_once_across_processes(tmp_path, kind):
    path = tmp_path / "approvals.json"
    token = _controller(path, kind)._issue_legacy_approval(ACTION, PAYLOAD)
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [context.Process(
        target=_consume_in_process, args=(str(path), token, ready, start, results, kind)
    ) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=15) is True
        start.set()
        assert sorted(results.get(timeout=15) for _ in processes) == [False, False, True]
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        ready.close()
        results.close()


@pytest.mark.parametrize("kind", ["computer", "companion"])
def test_concurrent_issuance_preserves_distinct_tokens(tmp_path, kind):
    path = tmp_path / "approvals.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(
            lambda _: _controller(path, kind)._issue_legacy_approval(ACTION, PAYLOAD), range(16)
        ))
    assert len(set(tokens)) == 16
    assert set(_controller(path, kind)._read_approvals()) == set(tokens)
    for token in tokens:
        assert _controller(path, kind)._consume_legacy_approval(token, ACTION, PAYLOAD)
    assert _controller(path, kind)._read_approvals() == {}


@pytest.mark.parametrize("kind", ["computer", "companion"])
def test_failed_token_persistence_cannot_return_allow(tmp_path, monkeypatch, kind):
    controller = _controller(tmp_path / "approvals.json", kind)
    token = controller._issue_legacy_approval(ACTION, PAYLOAD)

    def fail_write(value):
        raise OSError("test disk unavailable")

    monkeypatch.setattr(controller, "_write_approvals", fail_write)
    with pytest.raises(OSError, match="disk unavailable"):
        controller._consume_legacy_approval(token, ACTION, PAYLOAD)


def test_lock_contention_times_out_without_breaking_owner(tmp_path):
    path = tmp_path / "approvals.json"
    with legacy_approval_lock(path):
        with pytest.raises(TimeoutError, match="deadline"):
            with legacy_approval_lock(path, timeout=0.02):
                pytest.fail("contender must not acquire the held lock")
    with legacy_approval_lock(path, timeout=0.02):
        assert path.with_name("approvals.json.lock").is_file()


@pytest.mark.parametrize("kind", ["computer", "companion"])
def test_partial_save_keeps_existing_tokens_and_never_allows(
    tmp_path, monkeypatch, kind
):
    controller = _controller(tmp_path / "approvals.json", kind)
    tokens = [controller._issue_legacy_approval(ACTION, PAYLOAD) for _ in range(2)]
    before = controller._read_approvals()

    def partial_write(descriptor, data):
        os.write(descriptor, data[:3])
        raise OSError("injected partial write")

    with monkeypatch.context() as patch:
        patch.setattr(SecureDirectory, "_write_all", staticmethod(partial_write))
        with pytest.raises(OSError, match="partial write"):
            controller._consume_legacy_approval(tokens[0], ACTION, PAYLOAD)
    assert controller._read_approvals() == before
    for token in tokens:
        assert controller._consume_legacy_approval(token, ACTION, PAYLOAD)
