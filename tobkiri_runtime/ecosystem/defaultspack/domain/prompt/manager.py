"""Legacy prompt composition facade with no authoritative local writer.

Pack/component prompt sources remain readable for chat composition. Authored
records and every mutation are projected through the active global Prompt
Studio contracts; this module owns no prompt persistence.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any

from core_runtime.resolved_profile_scope import effective_pack_ids

from ..extensions.runtime import get_extension_registry, get_extensions_root
from .component_prompts import component_prompt_records
from .studio_client import authored_prompts, write_authored_prompt
from .template import PromptTemplate
from .trust import prompt_pack_is_trusted, prompt_pack_source_is_trusted


_PROMPT_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ABSENT_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()


def _active_profile_required() -> str:
    from core_runtime.profile_paths import active_profile_id

    profile_id = str(active_profile_id() or "").strip()
    if not profile_id:
        raise RuntimeError("active profile is required for prompt authoring")
    return profile_id


def _legacy_prompt(record: dict[str, Any]) -> dict[str, Any]:
    prompt_id = str(record.get("prompt_id") or "")
    body = str(record.get("body") or "")
    return {
        **record,
        "id": prompt_id,
        "name": prompt_id,
        "content": body,
        "body": body,
        "variables": _normalize_variables(record.get("variables") or []),
    }


def _read_pack_id(pack_root: Path) -> str:
    try:
        raw = json.loads((pack_root / "pack.v4.json").read_text(encoding="utf-8"))
        pack_id = str((raw.get("pack") or {}).get("id") or "").strip()
        if pack_id:
            return pack_id
    except Exception:
        pass
    return ""


def _safe_extension_prompt_path(
    extensions_root: Path,
    prompt_id: str,
    template_file: str,
) -> Path | None:
    """Resolve an extension prompt body below its prompt directory only."""
    if not prompt_id or not _PROMPT_ID_SAFE_RE.fullmatch(prompt_id):
        return None
    relative_file = Path(str(template_file or "prompt.md").strip() or "prompt.md")
    if relative_file.is_absolute():
        return None
    try:
        prompt_dir = (extensions_root / "prompts" / prompt_id).resolve()
        candidate = (prompt_dir / relative_file).resolve()
        candidate.relative_to(prompt_dir)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


# ---------------------------------------------------------------------------
# PromptManager
# ---------------------------------------------------------------------------
class PromptManager:
    """Compose prompt sources and adapt legacy calls to the global owner."""

    def __init__(self):
        self._prompts: dict[str, dict] = {}
        self._name_index: dict[str, str] = {}  # name → id
        self._system_prompt: str = ""
        self._loaded = False

    # -- 永続化 ---------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """Load authored records through the optional global owner contract."""
        if self._loaded:
            return
        self._loaded = True
        try:
            from core_runtime.profile_paths import active_profile_id

            profile_id = active_profile_id()
        except Exception:
            profile_id = None
        if not profile_id:
            return
        for item in authored_prompts(profile_id):
            prompt_id = str(item.get("prompt_id") or "").strip()
            if not prompt_id:
                continue
            record = {
                **item,
                "id": prompt_id,
                "name": prompt_id,
                "content": str(item.get("body") or ""),
            }
            self._prompts[prompt_id] = record
            self._name_index[prompt_id] = prompt_id

    def _canonical_prompt_path(self, prompt_id: str) -> Path | None:
        """Locate the canonical in-pack prompt file for a given prompt_id.

        Used as a fallback when an extension override does not ship a body, so
        we don't have to keep a duplicate prompt.md in extensions/prompts/<id>/.
        """
        if not prompt_id or not _PROMPT_ID_SAFE_RE.match(prompt_id):
            return None
        base = Path(os.path.dirname(os.path.realpath(__file__)))
        candidates = [
            base.parent / "prompts" / prompt_id / "prompt.md",
            base.parent / "prompts" / (prompt_id + ".system.md"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _extension_prompts(self) -> dict[str, dict]:
        prompts: dict[str, dict] = {}
        try:
            registry = get_extension_registry()
            extensions_root = get_extensions_root()
            for manifest in registry.prompts().list(enabled_only=True):
                prompt_id = str(manifest.get("id", "")).strip()
                if not prompt_id:
                    continue
                source_pack_id = str(
                    manifest.get("source_pack_id")
                    or manifest.get("_source_pack_id")
                    or extensions_root.parent.name
                ).strip()
                if not prompt_pack_source_is_trusted(source_pack_id, manifest.get("source_path", "")):
                    continue
                template_file = str(
                    (manifest.get("config", {}) or {}).get("template_file", "prompt.md")
                ).strip() or "prompt.md"
                prompt_path = _safe_extension_prompt_path(
                    extensions_root,
                    prompt_id,
                    template_file,
                )
                body = ""
                source = "extension"
                if prompt_path is not None and prompt_path.is_file():
                    try:
                        body = prompt_path.read_text(encoding="utf-8").strip()
                    except (OSError, UnicodeDecodeError):
                        body = ""
                if not body:
                    canonical = self._canonical_prompt_path(prompt_id)
                    if canonical is not None:
                        body = canonical.read_text(encoding="utf-8").strip()
                        source = "canonical_fallback"
                prompts[prompt_id] = {
                    "id": prompt_id,
                    "name": prompt_id,
                    "content": body,
                    "body": body,
                    "description": str(manifest.get("description", "")),
                    "variables": list((manifest.get("config", {}) or {}).get("variables", [])),
                    "metadata": {
                        "source": source,
                        "source_pack_id": source_pack_id,
                        "manifest_path": manifest.get("source_path", ""),
                    },
                    "created_at": "",
                    "updated_at": "",
                    "read_only": True,
                    "source_pack_id": source_pack_id,
                }
        except Exception:
            return {}
        return prompts

    def _pack_prompts(self) -> dict[str, dict]:
        prompts: dict[str, dict] = {}
        ecosystem_root = Path(__file__).resolve().parents[3]
        if not ecosystem_root.exists():
            return prompts
        for pack_id in sorted(effective_pack_ids()):
            pack_root = ecosystem_root / pack_id
            if not pack_root.is_dir() or _read_pack_id(pack_root) != pack_id:
                continue
            prompt_dir = pack_root / "prompts"
            if not prompt_dir.exists():
                continue
            source_pack_id = pack_id
            if not prompt_pack_is_trusted(source_pack_id):
                continue
            for prompt_path in sorted(prompt_dir.glob("*.system.md")):
                try:
                    body = prompt_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                prompt_id = prompt_path.name.removesuffix(".system.md")
                prompts[prompt_id] = {
                    "id": prompt_id,
                    "name": prompt_id,
                    "content": body,
                    "body": body,
                    "description": "",
                    "variables": [],
                    "metadata": {
                        "source": "pack",
                        "source_pack_id": source_pack_id,
                        "path": str(prompt_path),
                    },
                    "created_at": "",
                    "updated_at": "",
                    "read_only": True,
                    "source_pack_id": source_pack_id,
                }
        return prompts

    def _component_prompts(self) -> dict[str, dict]:
        return component_prompt_records()

    # -- 一覧 ---------------------------------------------------------------
    def list_prompts(self) -> list[dict]:
        """保存されたプロンプト一覧を返す。"""
        self._ensure_loaded()
        combined = dict(self._pack_prompts())
        combined.update(self._component_prompts())
        combined.update(self._extension_prompts())
        combined.update(self._prompts)
        return list(combined.values())

    # -- 取得 ---------------------------------------------------------------
    def get_prompt(self, prompt_id: str) -> dict | None:
        """ID でプロンプトを取得する。存在しなければ None。"""
        self._ensure_loaded()
        prompt = self._prompts.get(prompt_id)
        if prompt is not None:
            return prompt
        return (
            self._extension_prompts().get(prompt_id)
            or self._component_prompts().get(prompt_id)
            or self._pack_prompts().get(prompt_id)
        )

    def get_prompt_by_name(self, name: str) -> dict | None:
        """name でプロンプトを取得する。存在しなければ None。"""
        self._ensure_loaded()
        pid = self._name_index.get(name)
        if pid is not None:
            return self._prompts.get(pid)
        return (
            self._extension_prompts().get(name)
            or self._component_prompts().get(name)
            or self._pack_prompts().get(name)
        )

    # -- 作成 ---------------------------------------------------------------
    def create_prompt(self, data: dict) -> dict:
        """新規プロンプトを作成して返す。

        Args:
            data: {"name": str, "content": str, "variables": [...], ...}
                  content は body のエイリアスとして扱う。
                  新形式の "body", "description", "metadata" も受け付ける。
        Returns:
            作成されたプロンプト dict
        """
        self._ensure_loaded()
        name = str(data.get("name") or data.get("prompt_id") or uuid.uuid4().hex[:8])
        body = data.get("body", data.get("content", ""))
        description = data.get("description", "")
        metadata = data.get("metadata", {})

        # variables: 旧形式 [str, ...] と新形式 [{"name":...}, ...] の両方を受け付ける
        raw_vars = data.get("variables", [])
        variables = _normalize_variables(raw_vars)

        profile_id = _active_profile_required()
        result = write_authored_prompt(
            profile_id,
            "save",
            {
                "prompt_id": name,
                "body": body,
                "description": description,
                "variables": [item["name"] for item in variables],
                "metadata": metadata,
                "expected_body_hash": _ABSENT_HASH,
            },
        )
        prompt = _legacy_prompt(result.get("prompt") or {})
        self._prompts[name] = prompt
        self._name_index[name] = name
        return prompt

    # -- 更新 ---------------------------------------------------------------
    def update_prompt(self, name: str, updates: dict) -> dict | None:
        """既存プロンプトを更新する。

        Args:
            name:    プロンプト名
            updates: 更新するフィールド dict
        Returns:
            更新後のプロンプト dict。見つからなければ None。
        """
        self._ensure_loaded()
        pid = self._name_index.get(name)
        if pid is None:
            return None
        prompt = self._prompts.get(pid)
        if prompt is None:
            return None

        if updates.get("name") not in (None, name):
            raise ValueError("prompt rename requires explicit create/delete migration")
        result = write_authored_prompt(
            _active_profile_required(),
            "save",
            {
                "prompt_id": name,
                "body": updates.get("body", updates.get("content", prompt.get("body", ""))),
                "description": updates.get("description", prompt.get("description", "")),
                "variables": [
                    item["name"]
                    for item in _normalize_variables(updates.get("variables", prompt.get("variables", [])))
                ],
                "metadata": updates.get("metadata", prompt.get("metadata", {})),
                "expected_body_hash": str(prompt.get("body_hash") or _ABSENT_HASH),
            },
        )
        updated = _legacy_prompt(result.get("prompt") or {})
        self._prompts[pid] = updated
        return updated

    # -- 削除 ---------------------------------------------------------------
    def delete_prompt(self, name: str) -> bool:
        """プロンプトを削除する。成功時 True、見つからなければ False。"""
        self._ensure_loaded()
        pid = self._name_index.get(name)
        if pid is None:
            return False
        prompt = self._prompts.get(pid)
        if prompt is None:
            return False
        write_authored_prompt(
            _active_profile_required(),
            "delete",
            {
                "prompt_id": name,
                "expected_body_hash": str(prompt.get("body_hash") or _ABSENT_HASH),
            },
        )
        del self._prompts[pid]
        del self._name_index[name]
        return True

    # -- テンプレート変換 -----------------------------------------------------
    def to_template(self, name: str) -> PromptTemplate | None:
        """保存済みプロンプトを PromptTemplate に変換する。"""
        prompt = self.get_prompt_by_name(name)
        if prompt is None:
            return None
        return PromptTemplate(
            name=prompt.get("name", ""),
            description=prompt.get("description", ""),
            variables=prompt.get("variables", []),
            body=prompt.get("body", prompt.get("content", "")),
            metadata=prompt.get("metadata", {}),
        )

    def create_from_template(self, template: PromptTemplate) -> dict:
        """PromptTemplate からプロンプトを作成する。"""
        return self.create_prompt(template.to_dict())

    # -- コンテキスト変数注入 ---------------------------------------------------
    @staticmethod
    def inject_context_variables(
        variables: dict,
        context: dict | None = None,
    ) -> dict:
        """context dict から特殊変数を variables に注入する。

        注入されるキー:
            context.total_tokens    — context["total_tokens"] (int, default 0)
            context.message_count   — context["message_count"] (int, default 0)
            context.messages        — context["messages"] (str / list, default "")
            context.system_prompt   — context["system_prompt"] (str, default "")
            context.conversation_id — context["conversation_id"] (str, default "")
            context.knowledge       — context["knowledge"] (str, default "")
            context.memory          — context["memory"] (str, default "")

        既にユーザーが明示的に指定した値は上書きしない。
        """
        if context is None:
            return variables

        ctx_mapping = {
            "context.total_tokens": "total_tokens",
            "context.message_count": "message_count",
            "context.messages": "messages",
            "context.system_prompt": "system_prompt",
            "context.conversation_id": "conversation_id",
            "context.knowledge": "knowledge",
            "context.memory": "memory",
        }
        merged = dict(variables)
        for template_key, ctx_key in ctx_mapping.items():
            if template_key not in merged and ctx_key in context:
                value = context[ctx_key]
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                merged[template_key] = value
        return merged

    # -- システムプロンプト ---------------------------------------------------
    def get_system_prompt(self) -> str:
        """システムプロンプトを取得する。"""
        prompt = self.get_prompt_by_name("system")
        return str((prompt or {}).get("body") or self._system_prompt)

    def set_system_prompt(self, content: str) -> str:
        """システムプロンプトを設定して返す。"""
        current = self.get_prompt_by_name("system")
        if current is None:
            self.create_prompt({"name": "system", "body": str(content)})
        else:
            self.update_prompt("system", {"body": str(content)})
        self._system_prompt = str(content)
        return self._system_prompt


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------
def _normalize_variables(raw: list) -> list[dict]:
    """変数リストを正規化する。

    旧形式 ["var1", "var2"] → [{"name": "var1", ...}, ...]
    新形式 [{"name": "var1", "type": "string", ...}] → そのまま
    """
    if not raw:
        return []
    normalized = []
    for item in raw:
        if isinstance(item, str):
            normalized.append({
                "name": item,
                "type": "string",
                "default": None,
                "required": False,
            })
        elif isinstance(item, dict):
            normalized.append({
                "name": item.get("name", ""),
                "type": item.get("type", "string"),
                "default": item.get("default"),
                "required": item.get("required", False),
            })
        # 不明な型は無視
    return normalized


# ---------------------------------------------------------------------------
# モジュールレベル シングルトン
# ---------------------------------------------------------------------------
_manager = PromptManager()


def get_manager() -> PromptManager:
    """共有 PromptManager インスタンスを返す。"""
    return _manager
