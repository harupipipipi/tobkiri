"""Select an executor using manifest routing metadata only."""

from __future__ import annotations

from typing import Any, Callable, Mapping


def create_select_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a deterministic execution-kind selector."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"select", "resolve"}:
            raise ValueError(f"unknown executor selector operation: {name}")
        execution_kind = str(payload.get("execution_kind") or "").strip()
        if not execution_kind:
            raise ValueError("execution_kind is required")
        providers = payload.get("providers")
        providers = providers if isinstance(providers, list) else []
        exact: list[dict[str, Any]] = []
        wildcard: list[dict[str, Any]] = []
        excluded: list[dict[str, str]] = []
        for raw in providers:
            if not isinstance(raw, Mapping):
                continue
            provider = dict(raw)
            keys = {
                str(item).strip()
                for item in provider.get("routing_keys") or []
                if str(item).strip()
            }
            item_id = str(provider.get("provider_instance_id") or "")
            if execution_kind in keys:
                exact.append(provider)
            elif "*" in keys:
                wildcard.append(provider)
            else:
                excluded.append(
                    {"provider_instance_id": item_id, "reason": "kind_mismatch"}
                )
        candidates = exact or wildcard
        candidates.sort(
            key=lambda item: (
                int(item.get("priority") or 100),
                str(item.get("provider_instance_id") or ""),
                str(item.get("content_hash") or ""),
            )
        )
        if not candidates:
            return {
                "selected": None,
                "execution_kind": execution_kind,
                "excluded": excluded,
                "reason": "missing_executor",
            }
        selected = candidates[0]
        return {
            "selected": {
                "provider_instance_id": selected.get("provider_instance_id"),
                "content_hash": selected.get("content_hash"),
                "routing_match": "exact" if exact else "wildcard",
            },
            "execution_kind": execution_kind,
            "excluded": excluded,
            "reason": "selected",
        }

    return operation

