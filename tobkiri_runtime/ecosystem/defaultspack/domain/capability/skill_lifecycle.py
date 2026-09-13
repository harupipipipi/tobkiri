"""One runtime authority for Skill discovery and enablement."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterable


class SkillLifecycleStore:
    """Apply durable user enablement without mutating signed manifests."""

    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (
            Path(__file__).resolve().parents[2]
            / "user_data"
            / "shared"
            / "capabilities"
            / "skill-lifecycle.json"
        )

    def apply(self, skills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            states = self._read()
        result: list[dict[str, Any]] = []
        for raw in skills:
            if not isinstance(raw, dict):
                continue
            skill = dict(raw)
            skill_id = str(skill.get("id") or "").strip()
            state = states.get(skill_id)
            if isinstance(state, dict) and "enabled" in state:
                skill["enabled"] = bool(state["enabled"])
                skill["lifecycle"] = dict(state)
            if skill.get("enabled", True):
                result.append(skill)
        return result

    def list(self, skills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            states = self._read()
        result = []
        for raw in skills:
            if not isinstance(raw, dict):
                continue
            skill_id = str(raw.get("id") or "").strip()
            if not skill_id:
                continue
            state = states.get(skill_id, {})
            result.append(
                {
                    "id": skill_id,
                    "enabled": bool(state.get("enabled", raw.get("enabled", True))),
                    "source_path": str(raw.get("source_path") or ""),
                    "schema_version": str(raw.get("schema_version") or ""),
                }
            )
        return sorted(result, key=lambda item: item["id"])

    def set_enabled(
        self,
        skill_id: str,
        enabled: bool,
        known_skills: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        known = {
            str(skill.get("id") or "")
            for skill in known_skills
            if isinstance(skill, dict)
        }
        if skill_id not in known:
            raise KeyError(skill_id)
        with self._lock:
            states = self._read()
            states[skill_id] = {"enabled": bool(enabled)}
            self._write(states)
        return {"id": skill_id, "enabled": bool(enabled)}

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, value: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
