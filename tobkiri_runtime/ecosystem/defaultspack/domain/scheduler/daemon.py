from __future__ import annotations

import threading
from typing import Any

from domain.runtime_config import scheduler_config
from tobkiri_protocol.settings_state import SettingsOwnerPort

from .scheduler import Scheduler
from .security import scheduler_enabled


class SchedulerDaemon:
    _instance: "SchedulerDaemon | None" = None

    def __new__(cls) -> "SchedulerDaemon":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_result: dict[str, Any] = {}
        self._last_error = ""

    def start(
        self, *, settings_owner: SettingsOwnerPort | None = None,
    ) -> dict[str, Any]:
        if not scheduler_enabled():
            return {"started": False, "running": False, "reason": "scheduler disabled"}
        if self._thread is not None and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._settings_owner = settings_owner
        self._thread = threading.Thread(target=self._loop, name="defaultspack-scheduler", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "started": self._thread is not None,
            "running": self._thread is not None and self._thread.is_alive(),
            "tick_seconds": self._tick_seconds(),
            "last_result": self._last_result,
            "last_error": self._last_error,
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if scheduler_enabled():
                    self._last_result = Scheduler(
                        settings_owner=self._settings_owner
                    ).tick()
                    self._last_error = ""
            except Exception as exc:
                self._last_error = str(exc)
            self._stop.wait(self._tick_seconds())

    @staticmethod
    def _tick_seconds() -> float:
        try:
            return max(1.0, min(float(scheduler_config().get("tick_seconds", 60) or 60), 3600.0))
        except (TypeError, ValueError):
            return 60.0


def start_scheduler_daemon(
    *, settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    return SchedulerDaemon().start(settings_owner=settings_owner)


def scheduler_daemon_status() -> dict[str, Any]:
    return SchedulerDaemon().status()
