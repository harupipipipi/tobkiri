"""Unit ハンドラ Mixin"""
from __future__ import annotations

from pathlib import Path

from .._helpers import _log_internal_error, _SAFE_ERROR_MSG


class UnitHandlersMixin:
    """Unit 管理 (list / publish / execute) のハンドラ"""

    def _units_list(self, store_id: str | None = None) -> dict:
        try:
            from ...store_registry import get_store_registry
            from ...unit_registry import get_unit_registry
            store_reg = get_store_registry()
            unit_reg = get_unit_registry()

            if store_id:
                store_def = store_reg.get_store(store_id)
                if store_def is None:
                    return {"units": [], "error": f"Store not found: {store_id}"}
                units = unit_reg.list_units(Path(store_def.root_path))
                for u in units:
                    u.store_id = store_id
                return {
                    "units": [u.to_dict() for u in units],
                    "count": len(units),
                    "store_id": store_id,
                }
            else:
                all_units = []
                for s in store_reg.list_stores():
                    sid = s["store_id"]
                    rp = s["root_path"]
                    units = unit_reg.list_units(Path(rp))
                    for u in units:
                        u.store_id = sid
                    all_units.extend(units)
                return {
                    "units": [u.to_dict() for u in all_units],
                    "count": len(all_units),
                }
        except Exception as e:
            _log_internal_error("units_list", e)
            return {"units": [], "error": _SAFE_ERROR_MSG}

    def _units_publish(self, body: dict) -> dict:
        store_id = body.get("store_id", "")
        source_dir = body.get("source_dir", "")
        namespace = body.get("namespace", "")
        name = body.get("name", "")
        version = body.get("version", "")

        if not store_id or not source_dir or not namespace or not name or not version:
            return {
                "success": False,
                "error": "Missing store_id, source_dir, namespace, name, or version",
            }
        try:
            from ...store_registry import get_store_registry
            from ...unit_registry import get_unit_registry
            from ...paths import is_path_within, ECOSYSTEM_DIR

            if not is_path_within(Path(source_dir), Path(ECOSYSTEM_DIR)):
                return {
                    "success": False,
                    "error": "source_dir is outside allowed directory",
                }

            store_reg = get_store_registry()
            store_def = store_reg.get_store(store_id)
            if store_def is None:
                return {"success": False, "error": f"Store not found: {store_id}"}

            unit_reg = get_unit_registry()
            result = unit_reg.publish_unit(
                store_root=Path(store_def.root_path),
                source_dir=Path(source_dir),
                namespace=namespace,
                name=name,
                version=version,
                store_id=store_id,
            )
            return result.to_dict()
        except Exception as e:
            _log_internal_error("units_publish", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _units_execute(self, body: dict) -> dict:
        principal_id = body.get("principal_id", "")
        unit_ref = body.get("unit_ref", {})
        mode = body.get("mode", "host_capability")
        args = body.get("args", {})
        timeout = body.get("timeout", 60.0)
        if not isinstance(timeout, (int, float)):
            timeout = 60.0
        else:
            timeout = min(max(timeout, 1), 300)

        if not principal_id:
            return {"success": False, "error": "Missing 'principal_id'"}
        if not unit_ref or not isinstance(unit_ref, dict):
            return {"success": False, "error": "Missing or invalid 'unit_ref'"}

        try:
            from ...unit_executor import get_unit_executor
            executor = get_unit_executor()
            result = executor.execute(
                principal_id=principal_id,
                unit_ref=unit_ref,
                mode=mode,
                args=args,
                timeout_seconds=timeout,
            )
            rd = result.to_dict()
            if not result.success and result.error_type:
                rd["error_type"] = result.error_type
            return rd
        except Exception as e:
            _log_internal_error("units_execute", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}
