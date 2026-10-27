"""Exercise real stdlib threads/contexts only when the actual guest runs.

No worker calls a native import. Failed or unconfirmed termination must send
the enclosing diagnostic runner to process isolation/exit, not Store reuse.
The host's waiter-drop observation remains distinct from individual task cancel.
"""

from __future__ import annotations

from typing import Callable


def _error(error: BaseException) -> dict:
    kinds = (RuntimeError, ValueError, TypeError, OSError, NotImplementedError)
    if type(error) not in kinds:
        return {"kind": "unclassified", "arguments_observed": False}
    args = BaseException.args.__get__(error, type(error))
    return {
        "kind": type(error).__name__,
        "arguments_observed": True,
        "args": [
            value[:512] if type(value) is str else value
            for value in args[:4]
            if type(value) in (str, int, bool, type(None))
        ],
    }


def run(checkpoint: Callable[[str], None]) -> dict:
    """Observe capability and failure stage without inventing supported behavior."""
    result = {
        "format": "tobkiri.thread-capabilities.observation.v1",
        "diagnostic_only": True,
        "whole_component_parity": False,
        "original_baseline_reader_exercised": False,
        "individual_task_cancellation_proven": False,
        "thread_supported": False,
        "termination_confirmed": False,
        "context_rules_observed": False,
    }
    phase = "starting"
    worker = None
    stop = None
    main_token = None
    variable = None
    try:
        checkpoint(phase)
        # These guest calls reuse the explicitly pre-initialized modules.
        # ContextVar/Event/worker instances below are created here; module
        # initialization and threading's internal state are recorded separately.
        phase = "import-contextvars"
        checkpoint(phase)
        import contextvars
        phase = "contextvars-imported"
        checkpoint(phase)
        phase = "import-struct"
        checkpoint(phase)
        import struct
        phase = "struct-imported"
        checkpoint(phase)
        phase = "import-sys"
        checkpoint(phase)
        import sys
        phase = "sys-imported"
        checkpoint(phase)
        phase = "import-threading"
        checkpoint(phase)
        import threading
        phase = "threading-imported"
        checkpoint(phase)

        result["python_version"] = list(sys.version_info[:3])
        result["pointer_bits"] = struct.calcsize("P") * 8
        phase = "local-context"
        checkpoint(phase)
        variable = contextvars.ContextVar("diagnostic-context", default="unset")
        main_token = variable.set("parent")
        isolated = contextvars.Context()

        def local_context() -> dict:
            before = variable.get()
            token = variable.set("separate-context")
            during = variable.get()
            variable.reset(token)
            return {"before": before, "during": during, "after": variable.get()}

        result["local_context"] = isolated.run(local_context)
        result["parent_after_context_exit"] = variable.get()
        ready = threading.Event()
        stop = threading.Event()
        values = {}
        worker_tokens = []

        def worker_body() -> None:
            try:
                values["ident"] = threading.get_ident()
                values["before"] = variable.get()
                token = variable.set("worker")
                worker_tokens.append(token)
                values["during"] = variable.get()
                try:
                    variable.reset(main_token)
                except ValueError:
                    values["parent_token_refused"] = True
                else:
                    values["parent_token_refused"] = False
                ready.set()
                values["cooperative_stop_observed"] = stop.wait(0.5)
                variable.reset(token)
                values["after_reset"] = variable.get()
            except BaseException as error:
                values["error"] = _error(error)
                ready.set()

        worker = threading.Thread(target=worker_body, name="capability-observer")
        phase = "thread-start"
        checkpoint(phase)
        worker.start()
        result["start_returned"] = True
        phase = "thread-coordination"
        checkpoint(phase)
        result["ready_observed"] = ready.wait(0.25)
        result["main_ident"] = threading.get_ident()
        result["parent_during_worker"] = variable.get()
        if worker_tokens:
            try:
                variable.reset(worker_tokens[0])
            except ValueError:
                result["worker_token_refused"] = True
            else:
                result["worker_token_refused"] = False
        # The host may hold only this DATA checkpoint while a detached task
        # retains the Store. Dropping its waiter is not aborting that task.
        phase = "worker-waiting"
        checkpoint(phase)
        stop.set()
        phase = "thread-join"
        checkpoint(phase)
        worker.join(0.25)
        result["termination_confirmed"] = not worker.is_alive()
        result["worker"] = dict(values)
        result["thread_supported"] = bool(
            result["ready_observed"]
            and "ident" in values
            and result["termination_confirmed"]
            and "error" not in values
        )
        result["context_rules_observed"] = bool(
            result["thread_supported"]
            and result["local_context"]
            == {
                "before": "unset",
                "during": "separate-context",
                "after": "unset",
            }
            and result["parent_after_context_exit"] == "parent"
            and result["parent_during_worker"] == "parent"
            and values.get("before") == "unset"
            and values.get("during") == "worker"
            and values.get("after_reset") == "unset"
            and values.get("parent_token_refused") is True
            and result.get("worker_token_refused") is True
        )
        variable.reset(main_token)
        result["parent_after_reset"] = variable.get()
        result["context_rules_observed"] &= result["parent_after_reset"] == "unset"
        phase = "completed"
        checkpoint(phase)
    except BaseException as error:
        result["failure"] = {"stage": phase, **_error(error)}
        # Avoid a cleanup retry that could conceal the actual failed stage or
        # free state still retained by an unconfirmed real worker. The isolated
        # runner must finish the process; it cannot call this a task cancellation.
    result["last_stage"] = phase
    return result
