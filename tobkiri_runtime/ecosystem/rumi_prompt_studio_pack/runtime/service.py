"""Provider-free Prompt Studio action and resource contract service."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.profile_paths import active_profile_id
from core_runtime.profile_workspace import validate_profile_id

from .store import PromptStudioStore

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
_MUST_KEEP = re.compile(r"(?im)^\s*(?:must|never|always|required|do not)\b")


class PromptStudioService:
    """Dispatch global prompt contracts without provider or tool implementation imports."""

    def __init__(
        self,
        *,
        user_data_root: Path | None = None,
    ) -> None:
        self.user_data_root = user_data_root

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke one declared operation against the authoritative store."""
        data = dict(payload)
        store = self._store(data)
        operations: dict[str, Callable[[], dict[str, Any]]] = {
            "list": store.snapshot,
            "editor.load": lambda: self._editor_load(store, data),
            "active": lambda: self._active(store),
            "traces": lambda: self._traces(store, data),
            "edge_states": lambda: self._edge_states(store),
            "get": lambda: self._get(store, data),
            "save": lambda: self._save(store, data),
            "conditional": lambda: self._save(store, data),
            "inherit": lambda: self._save(store, data),
            "delete": lambda: self._delete(store, data),
            "versions": lambda: store.versions(_prompt_id(data)),
            "rollback": lambda: self._rollback(store, data),
            "diff": lambda: self._diff(store, data),
            "lint": lambda: self._lint(data),
            "compact": lambda: self._compact(data),
            "test": lambda: self._test(store, data),
            "render": lambda: self._test(store, data),
            "preview": lambda: self._test(store, data),
            "context_vars": lambda: self._context_vars(data),
            "convert": lambda: self._convert(data),
            "build": lambda: self._build(data),
            "preview_toggle": lambda: self._preview_toggle(store, data),
            "edge.toggle": lambda: self._edge_toggle(store, data, persist=True),
            "edge.preview": lambda: self._edge_toggle(store, data, persist=False),
            "toggle": lambda: self._toggle(store, data),
            "migration.inspect": lambda: self._migration_inspect(store, data),
            "migration.apply": lambda: self._migration_apply(store, data),
            "migration.import": lambda: self._migration_import(store, data),
            "migration.rollback": lambda: self._migration_rollback(store, data),
        }
        handler = operations.get(str(operation))
        if handler is None:
            raise ValueError(f"unknown Prompt Studio operation: {operation}")
        return handler()

    @staticmethod
    def _editor_load(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = store.snapshot()
        prompt_id = str(data.get("prompt_id") or data.get("name") or "").strip()
        prompts = list(snapshot.get("prompts") or [])
        selected = next(
            (item for item in prompts if item.get("prompt_id") == prompt_id),
            prompts[0] if prompts else None,
        )
        return {
            "profile_id": store.profile_id,
            "prompts": prompts,
            "selected_prompt": selected,
            "active_summary": {"segments": [], "source": "prompt-studio-store"},
            "traces": [],
            "store_revision": snapshot.get("revision", 0),
        }

    @staticmethod
    def _active(store: PromptStudioStore) -> dict[str, Any]:
        snapshot = store.snapshot()
        enabled = [
            item for item in snapshot["prompts"] if item.get("enabled", True)
        ]
        return {
            "profile_id": store.profile_id,
            "segments": enabled,
            "count": len(enabled),
            "source": "prompt-studio-store",
        }

    @staticmethod
    def _traces(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "profile_id": store.profile_id,
            "trace_id": str(data.get("trace_id") or "") or None,
            "traces": [],
            "count": 0,
        }

    @staticmethod
    def _edge_states(store: PromptStudioStore) -> dict[str, Any]:
        snapshot = store.snapshot()
        return {
            "profile_id": store.profile_id,
            "edge_states": snapshot["edge_states"],
            "store_revision": snapshot["revision"],
        }

    def _store(self, data: Mapping[str, Any]) -> PromptStudioStore:
        profile_id = str(data.get("profile_id") or active_profile_id() or "").strip()
        if not profile_id:
            raise ValueError("profile_id is required")
        return PromptStudioStore(
            validate_profile_id(profile_id),
            user_data_root=self.user_data_root,
        )

    @staticmethod
    def _get(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        prompt_id = _prompt_id(data)
        prompt = store.get(prompt_id)
        if prompt is None:
            raise KeyError(f"prompt not found: {prompt_id}")
        prompt = dict(prompt)
        prompt.pop("versions", None)
        return {"profile_id": store.profile_id, "prompt": prompt}

    @staticmethod
    def _save(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        return store.save(
            _prompt_id(data),
            str(data.get("body") or data.get("content") or ""),
            expected_body_hash=str(data.get("expected_body_hash") or ""),
            description=str(data.get("description") or ""),
            variables=_strings(data.get("variables")),
            enabled=bool(data.get("enabled", True)),
            reason=str(data.get("reason") or "manual_save"),
            metadata=(
                data.get("metadata")
                if isinstance(data.get("metadata"), Mapping)
                else None
            ),
        )

    @staticmethod
    def _delete(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        return store.delete(
            _prompt_id(data),
            expected_body_hash=str(data.get("expected_body_hash") or ""),
        )

    @staticmethod
    def _rollback(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        return store.rollback(
            _prompt_id(data),
            str(data.get("version_id") or data.get("version") or ""),
            expected_body_hash=str(data.get("expected_body_hash") or ""),
            use_previous=bool(data.get("use_previous", True)),
        )

    @staticmethod
    def _diff(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        prompt_id = _prompt_id(data)
        current = store.get(prompt_id) or {}
        base = str(data.get("base") if data.get("base") is not None else current.get("body") or "")
        draft_value = data.get("draft")
        if draft_value is None:
            draft_value = data.get("body")
        draft = str(draft_value if draft_value is not None else base)
        lines = list(
            difflib.unified_diff(
                base.splitlines(),
                draft.splitlines(),
                fromfile=f"{prompt_id}:current",
                tofile=f"{prompt_id}:draft",
                lineterm="",
            )
        )
        return {
            "profile_id": store.profile_id,
            "prompt_id": prompt_id,
            "diff": "\n".join(lines),
            "changed": bool(lines),
        }

    @staticmethod
    def _lint(data: Mapping[str, Any]) -> dict[str, Any]:
        body = _body(data)
        warnings: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for line_number, raw_line in enumerate(body.splitlines(), 1):
            normalized = " ".join(raw_line.lower().split())
            if not normalized:
                continue
            if normalized in seen:
                warnings.append(
                    {
                        "code": "duplicate_line",
                        "line": line_number,
                        "first_line": seen[normalized],
                    }
                )
            else:
                seen[normalized] = line_number
        variables = sorted(set(_VARIABLE.findall(body)))
        return {
            "ok": not warnings,
            "warnings": warnings,
            "variables": variables,
            "characters": len(body),
            "estimated_tokens": max(1, len(body) // 4) if body else 0,
        }

    @staticmethod
    def _compact(data: Mapping[str, Any]) -> dict[str, Any]:
        body = _body(data)
        target = int(data.get("target_chars") or max(1, len(body)))
        kept: list[str] = []
        seen: set[str] = set()
        for line in body.splitlines():
            normalized = " ".join(line.split())
            key = normalized.lower()
            if not normalized or key in seen:
                continue
            if len("\n".join((*kept, normalized))) > target and not _MUST_KEEP.search(line):
                continue
            kept.append(normalized)
            seen.add(key)
        compacted = "\n".join(kept)
        return {
            "original": body,
            "compacted": compacted,
            "changed": compacted != body,
            "original_characters": len(body),
            "compacted_characters": len(compacted),
        }

    def _test(
        self,
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        prompt_id = str(data.get("prompt_id") or "").strip()
        body = _body(data)
        if not body and prompt_id:
            body = str((store.get(prompt_id) or {}).get("body") or "")
        variables = (
            data.get("variables")
            if isinstance(data.get("variables"), Mapping)
            else {}
        )
        missing: list[str] = []

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                missing.append(name)
                return match.group(0)
            return str(variables[name])

        rendered = _VARIABLE.sub(replace, body)
        lint = self._lint({"body": rendered})
        return {
            "ok": not missing,
            "provider_invoked": False,
            "tool_invoked": False,
            "prompt_id": prompt_id,
            "rendered": rendered,
            "missing_variables": sorted(set(missing)),
            "lint": lint,
        }

    @staticmethod
    def _context_vars(data: Mapping[str, Any]) -> dict[str, Any]:
        variables = sorted(set(_VARIABLE.findall(_body(data))))
        return {"variables": variables, "count": len(variables)}

    @staticmethod
    def _convert(data: Mapping[str, Any]) -> dict[str, Any]:
        body = _body(data).replace("\r\n", "\n").replace("\r", "\n")
        return {"body": body, "changed": body != _body(data)}

    @staticmethod
    def _build(data: Mapping[str, Any]) -> dict[str, Any]:
        segments = data.get("segments")
        if not isinstance(segments, list):
            segments = [_body(data)]
        body = "\n\n".join(str(item).strip() for item in segments if str(item).strip())
        return {"body": body, "segments": len(segments)}

    @staticmethod
    def _preview_toggle(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        prompt_id = _prompt_id(data)
        current = store.get(prompt_id)
        if current is None:
            raise KeyError(f"prompt not found: {prompt_id}")
        return {
            "profile_id": store.profile_id,
            "prompt_id": prompt_id,
            "enabled": bool(data.get("enabled")),
            "persisted": False,
        }

    @staticmethod
    def _edge_toggle(
        store: PromptStudioStore,
        data: Mapping[str, Any],
        *,
        persist: bool,
    ) -> dict[str, Any]:
        edge_id = str(data.get("edge_id") or "").strip()
        enabled = bool(data.get("enabled", True))
        if persist:
            return store.set_edge_state(edge_id, enabled)
        return {
            "profile_id": store.profile_id,
            "edge_id": edge_id,
            "enabled": enabled,
            "persisted": False,
        }

    @staticmethod
    def _toggle(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        prompt_id = _prompt_id(data)
        current = store.get(prompt_id)
        if current is None:
            raise KeyError(f"prompt not found: {prompt_id}")
        return store.save(
            prompt_id,
            str(current.get("body") or ""),
            expected_body_hash=str(data.get("expected_body_hash") or ""),
            description=str(current.get("description") or ""),
            variables=_strings(current.get("variables")),
            enabled=bool(data.get("enabled")),
            reason="toggle",
            metadata={"toggle_only": True},
        )

    @staticmethod
    def _migration_inspect(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        del data
        inspection = store.inspect_migration()
        return {
            "profile_id": inspection.profile_id,
            "source_files": [Path(item).name for item in inspection.source_files],
            "prompt_ids": list(inspection.prompt_ids),
            "source_hash": inspection.source_hash,
            "target_exists": inspection.target_exists,
            "owner_marker_exists": inspection.owner_marker_exists,
        }

    @staticmethod
    def _migration_apply(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        marker = store.migrate_from_legacy(
            expected_source_hash=str(data.get("expected_source_hash") or ""),
        )
        return {
            key: value
            for key, value in marker.items()
            if key != "backup"
        }

    @staticmethod
    def _migration_rollback(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = store.rollback_migration(str(data.get("migration_id") or ""))
        return {
            key: value
            for key, value in result.items()
            if key != "new_store_snapshot"
        }

    @staticmethod
    def _migration_import(
        store: PromptStudioStore,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        records = data.get("records")
        if not isinstance(records, list):
            raise ValueError("migration records are required")
        marker = store.migrate_records(
            records,
            edge_states=(
                data.get("edge_states")
                if isinstance(data.get("edge_states"), Mapping)
                else None
            ),
            expected_source_hash=str(data.get("expected_source_hash") or ""),
        )
        return {key: value for key, value in marker.items() if key != "backup"}


def _prompt_id(data: Mapping[str, Any]) -> str:
    value = str(data.get("prompt_id") or data.get("name") or "").strip()
    if not value:
        raise ValueError("prompt_id is required")
    return value


def _body(data: Mapping[str, Any]) -> str:
    return str(data.get("body") or data.get("prompt") or data.get("text") or "")


def _strings(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
