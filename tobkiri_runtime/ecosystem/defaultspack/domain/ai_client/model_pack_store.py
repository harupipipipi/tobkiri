from __future__ import annotations

import json
from typing import Any

from domain.ai_client.model_pack import ModelPack
from domain.ai_client.rumi_process import ensure_default_rumi_model_pack


def _parse_jsonish(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return fallback
    return value


def _normalize_fallback(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value if str(item or "").strip()]
    elif isinstance(value, dict):
        values = [
            str(value.get("model") or value.get("profile_id") or "").strip(),
        ]
    else:
        values = []
    return [item for item in values if item]


def normalize_model_packs(value: Any, *, composite_models: Any = None) -> list[dict[str, Any]]:
    parsed = _parse_jsonish(value, [])
    if isinstance(parsed, dict):
        raw_items = [{"id": key, **(item if isinstance(item, dict) else {})} for key, item in parsed.items()]
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raw_items = []

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        pack_id = str(item.get("id") or item.get("profile_id") or item.get("name") or "").strip()
        mode = str(item.get("mode") or item.get("type") or "fallback_chain").strip() or "fallback_chain"
        if not pack_id or pack_id in seen or mode not in {"fallback_chain", "ensemble", "review_chain"}:
            continue
        raw_members = item.get("members", item.get("models", item.get("chain", [])))
        if isinstance(raw_members, str):
            raw_members = [{"model": part.strip()} for part in raw_members.split(",") if part.strip()]
        members: list[dict[str, Any]] = []
        if isinstance(raw_members, list):
            for member in raw_members:
                payload = dict(member) if isinstance(member, dict) else {"model": str(member or "").strip()}
                model = str(payload.get("model") or payload.get("profile_id") or "").strip()
                if not model:
                    continue
                members.append(
                    {
                        **payload,
                        "model": model,
                        "conditions": payload.get("conditions") if isinstance(payload.get("conditions"), dict) else payload.get("when") if isinstance(payload.get("when"), dict) else {},
                        "fallback_on": [str(item).strip() for item in (payload.get("fallback_on") if isinstance(payload.get("fallback_on"), list) else []) if str(item or "").strip()],
                    }
                )
        if not members:
            continue
        normalized.append(
            {
                "id": pack_id,
                "display_name": str(item.get("display_name") or item.get("label") or pack_id).strip(),
                "members": members,
                "rules": dict(item.get("rules") if isinstance(item.get("rules"), dict) else item.get("conditions") if isinstance(item.get("conditions"), dict) else {}),
                "fallback": _normalize_fallback(item.get("fallback")),
                "mode": mode,
                "budget": dict(item.get("budget") if isinstance(item.get("budget"), dict) else {}),
                "safety": dict(item.get("safety") if isinstance(item.get("safety"), dict) else {}),
                "metadata": dict(item.get("metadata") if isinstance(item.get("metadata"), dict) else {}),
                "source": str(item.get("source") or "model_pack").strip() or "model_pack",
                "aliases": [f"modelpack/{pack_id}", pack_id],
            }
        )
        seen.add(pack_id)

    parsed_composites = _parse_jsonish(composite_models, [])
    if isinstance(parsed_composites, dict):
        composite_items = [{"id": key, **(item if isinstance(item, dict) else {})} for key, item in parsed_composites.items()]
    elif isinstance(parsed_composites, list):
        composite_items = parsed_composites
    else:
        composite_items = []
    for composite in composite_items:
        if not isinstance(composite, dict):
            continue
        composite_id = str(composite.get("id") or composite.get("profile_id") or composite.get("name") or "").strip()
        if not composite_id or composite_id in seen:
            continue
        members = composite.get("members") if isinstance(composite.get("members"), list) else composite.get("models") if isinstance(composite.get("models"), list) else composite.get("chain") if isinstance(composite.get("chain"), list) else []
        if not members:
            continue
        normalized.append(
            {
                "id": composite_id,
                "display_name": str(composite.get("display_name") or composite.get("label") or composite_id).strip(),
                "members": [
                    member if isinstance(member, dict) else {"model": str(member or "").strip()}
                    for member in members
                    if (isinstance(member, dict) and str(member.get("model") or member.get("profile_id") or "").strip()) or str(member or "").strip()
                ],
                "rules": dict(composite.get("conditions") if isinstance(composite.get("conditions"), dict) else {}),
                "fallback": [],
                "mode": str(composite.get("mode") or composite.get("type") or "fallback_chain").strip() or "fallback_chain",
                "budget": {},
                "safety": {},
                "metadata": {
                    "composite_compat": True,
                    "merge_model": str(composite.get("merge_model") or composite.get("synthesizer_model") or "").strip(),
                    "notes": str(composite.get("notes") or "").strip(),
                },
                "source": "composite_compat",
                "aliases": [f"modelpack/{composite_id}", composite_id],
            }
        )
        seen.add(composite_id)
    return normalized


class ModelPackStore:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings if isinstance(settings, dict) else {}

    @staticmethod
    def is_model_pack_ref(value: str) -> bool:
        return str(value or "").strip().startswith("modelpack/")

    @staticmethod
    def pack_id_for_ref(value: str) -> str:
        ref = str(value or "").strip()
        return ref.split("/", 1)[1] if ref.startswith("modelpack/") else ref

    def list_packs(self) -> list[ModelPack]:
        normalized = normalize_model_packs(
            self._settings.get("model_packs"),
            composite_models=self._settings.get("composite_models"),
        )
        base_model = None
        try:
            from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

            base_model = ModelRuntimeSettingsService()._runtime_rumi_base_model(self._settings)
        except Exception:
            base_model = None
        normalized = ensure_default_rumi_model_pack(normalized, base_model=base_model)
        return [ModelPack.from_dict(item) for item in normalized if str(item.get("id") or "").strip()]

    def get(self, reference: str) -> ModelPack | None:
        needle = self.pack_id_for_ref(reference)
        if not needle:
            return None
        for pack in self.list_packs():
            if needle == pack.id or needle in set(pack.aliases):
                return pack
        return None
