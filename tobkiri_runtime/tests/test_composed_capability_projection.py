"""Exercise the actual composition projection through its four-argument contract."""

import ast
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Mapping

from core_runtime.authority.v4 import AuthorityDenied
from tobkiri_host.errors import HostCoreError

ROOT = Path(__file__).resolve().parents[1]


def test_actual_composition_projection_signature_and_native_receipt_active_mode():
    source = ast.parse(
        (ROOT / "core_runtime/bootstrap/saved_tool_policy_composition_v4.py").read_text()
    )
    compose = next(
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "compose_saved_tool_policy_v4"
    )
    projection = next(
        node
        for node in compose.body
        if isinstance(node, ast.FunctionDef) and node.name == "project"
    )
    capture = {
        "conversation": "conversation",
        "workspace": "/owned",
        "context": {"profile_id": "profile"},
        "mode": "full",
    }
    root = NS(
        capture=lambda: capture,
        receipt_record=lambda: (2, {"state": "committed_selection", "capture": capture}),
        command=NS(expires_at=100),
    )
    namespace = {
        "Any": Any,
        "Mapping": Mapping,
        "assert_current_capture": lambda: None,
        "reviewer_capture": lambda invocation, context: NS(assert_current=lambda: None),
        "AuthorityDenied": AuthorityDenied,
        "PermissionError": PermissionError,
        "HostCoreError": HostCoreError,
        "profile_id": "profile",
        "kernel": NS(policy_roots={"actual": root}),
    }
    exec(
        compile(
            ast.Module(body=[projection], type_ignores=[]), "actual-composition-project", "exec"
        ),
        namespace,
    )
    reply = namespace["project"](
        object(), "conversation", "workspace", NS(canonical_root=Path("/owned"))
    )
    assert reply == {
        "available_modes": ["ask", "agent", "full"],
        "active_mode": "full",
        "reason": "available",
    }
    draft = namespace["project"](object(), None, "workspace", NS(canonical_root=Path("/owned")))
    assert draft["active_mode"] == "ask"
