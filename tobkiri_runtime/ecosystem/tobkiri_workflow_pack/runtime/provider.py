"""Workflow Pack v4 executable entrypoint.

The Host constructs :class:`WorkflowProviderV4` with captured provider
dependencies.  This module deliberately registers no HTTP route or legacy
Function/Interface registry entry.
"""

import hashlib
import json
from pathlib import Path


def _verify_backend_integrity() -> None:
    """Fail closed if an imported Workflow backend module is not Pack-pinned."""

    runtime_root = Path(__file__).resolve().parents[3]
    integrity_path = Path(__file__).resolve().parents[1] / "backend-integrity.v4.json"
    if integrity_path.is_symlink() or not integrity_path.is_file():
        raise RuntimeError("Workflow v4 backend integrity record is unavailable")
    payload = json.loads(integrity_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "io.tobkiri.workflow-backend-integrity.v4" or not isinstance(
        payload.get("files"), dict
    ):
        raise RuntimeError("Workflow v4 backend integrity record is invalid")
    for relative, expected in payload["files"].items():
        path = (runtime_root / relative).resolve()
        try:
            path.relative_to(runtime_root)
        except ValueError as exc:
            raise RuntimeError("Workflow v4 backend path escaped its root") from exc
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Workflow v4 backend module is unavailable")
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("Workflow v4 backend integrity verification failed")


_verify_backend_integrity()

from core_runtime.workflow_v4 import WorkflowProviderV4  # noqa: E402
from core_runtime.workflow_v4.integration import (  # noqa: E402
    WORKFLOW_HOST_PROVIDER_FACTORY,
)

HOST_PROVIDER_FACTORY = WORKFLOW_HOST_PROVIDER_FACTORY

__all__ = ["HOST_PROVIDER_FACTORY", "WorkflowProviderV4"]
