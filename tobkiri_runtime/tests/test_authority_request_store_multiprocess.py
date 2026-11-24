from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _HmacKey:
    def get_active_key(self) -> str:
        return "authority-multiprocess-test-key-" + ("x" * 32)


def _widen_one_shot_write_race(store: Any) -> None:
    original_write = store._write_json

    def delayed_process_safe_write(path: Path, payload: dict[str, Any]) -> None:
        if path.parent.name == "one_shot" and payload.get("consumed") is True:
            # A per-process temporary path ensures the test observes the CAS
            # result instead of a shared-temporary-file error.
            time.sleep(0.25)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(store._signed(payload), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
            return
        original_write(path, payload)

    store._write_json = delayed_process_safe_write


def _consume_in_process(
    base_dir: str,
    request_id: str,
    resource: dict[str, Any],
    token: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    from core_runtime.authority.request_store import AuthorityRequestStore

    store = AuthorityRequestStore(base_dir, hmac_key_manager=_HmacKey())
    _widen_one_shot_write_race(store)
    ready.put(os.getpid())
    if not start.wait(timeout=10):
        results.put({"error": "start_timeout"})
        return
    try:
        consumed = store.consume_one_shot(
            request_id=request_id,
            principal_id="profile:work",
            permission_id="host.process.exec_guarded",
            resource=resource,
            token=token,
        )
    except Exception as exc:  # pragma: no cover - asserted through child result
        results.put({"error": f"{type(exc).__name__}: {exc}"})
        return
    results.put({"consumed": consumed})


def _consume_batch_in_process(
    base_dir: str,
    items: list[dict[str, Any]],
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    from core_runtime.authority.request_store import AuthorityRequestStore

    store = AuthorityRequestStore(base_dir, hmac_key_manager=_HmacKey())
    _widen_one_shot_write_race(store)
    ready.put(os.getpid())
    if not start.wait(timeout=10):
        results.put({"error": "start_timeout"})
        return
    try:
        outcome = store.consume_one_shots_atomically(items)
    except Exception as exc:  # pragma: no cover - asserted through child result
        results.put({"error": f"{type(exc).__name__}: {exc}"})
        return
    results.put(outcome)


def test_one_shot_consume_is_atomic_across_processes(tmp_path: Path) -> None:
    from core_runtime.authority.request_store import AuthorityRequestStore

    base_dir = tmp_path / "authority"
    store = AuthorityRequestStore(base_dir, hmac_key_manager=_HmacKey())
    resource = {
        "kind": "command_host_operation",
        "metadata": {"execution_scope_sha256": "a" * 64},
    }
    request = store.create_request(
        principal_id="profile:work",
        permission_id="host.process.exec_guarded",
        resource=resource,
        reason="multiprocess one-shot test",
        risk_level="high",
        profile_id="work",
    )
    issued = store.issue_one_shot(request)

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_consume_in_process,
            args=(
                str(base_dir),
                request.request_id,
                resource,
                issued["token"],
                ready,
                start,
                results,
            ),
        )
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=15)
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=5) for _ in processes]
    assert [outcome.get("consumed") for outcome in outcomes].count(True) == 1
    assert [outcome.get("consumed") for outcome in outcomes].count(False) == 3
    assert all("error" not in outcome for outcome in outcomes)

    persisted = store._read_json(store._one_shot_dir / f"{issued['token_id']}.json")
    assert persisted is not None
    assert persisted["consumed"] is True
    assert persisted["request_id"] == request.request_id


def test_batch_one_shot_consume_is_atomic_across_processes(tmp_path: Path) -> None:
    from core_runtime.authority.request_store import AuthorityRequestStore

    base_dir = tmp_path / "authority"
    store = AuthorityRequestStore(base_dir, hmac_key_manager=_HmacKey())
    items: list[dict[str, Any]] = []
    for suffix in ("a", "b"):
        resource = {
            "kind": "command_host_operation",
            "metadata": {"execution_scope_sha256": suffix * 64},
        }
        request = store.create_request(
            principal_id="profile:work",
            permission_id="host.process.exec_guarded",
            resource=resource,
            reason="multiprocess batch one-shot test",
            risk_level="high",
            profile_id="work",
        )
        issued = store.issue_one_shot(request)
        items.append(
            {
                "request_id": request.request_id,
                "principal_id": request.principal_id,
                "permission_id": request.permission_id,
                "resource": resource,
                "token": issued["token"],
            }
        )

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_consume_batch_in_process,
            args=(str(base_dir), items, ready, start, results),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=15)
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=5) for _ in processes]
    assert [outcome.get("success") for outcome in outcomes].count(True) == 1
    assert [outcome.get("success") for outcome in outcomes].count(False) == 1
    assert all("error" not in outcome for outcome in outcomes)
