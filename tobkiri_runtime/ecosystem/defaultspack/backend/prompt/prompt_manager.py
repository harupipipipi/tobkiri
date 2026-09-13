"""Prompt CRUD, rendering, mixing, and metadata indexing."""

from __future__ import annotations

import json
import re
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

_VAR_PATTERN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _safe_filename(name: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in name)
    return safe or "unnamed"


def _flatten_context(context: Optional[Dict[str, Any]], prefix: str = "context") -> Dict[str, Any]:
    if not context:
        return {}
    flattened: Dict[str, Any] = {}
    for key, value in context.items():
        dotted = f"{prefix}.{key}"
        if isinstance(value, dict):
            flattened.update(_flatten_context(value, prefix=dotted))
        else:
            flattened[dotted] = value
    return flattened


def render_template(template: str, variables: Optional[Dict[str, Any]] = None, context: Optional[Dict[str, Any]] = None) -> str:
    values: Dict[str, Any] = {}
    if variables:
        values.update(variables)
    values.update(_flatten_context(context))

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)

    return _VAR_PATTERN.sub(_replace, template)


@dataclass
class PromptDefinition:
    prompt_id: str = ""
    uuid: str = field(default_factory=lambda: str(_uuid.uuid4()))
    display_name: str = ""
    description: str = ""
    icon: str = ""
    system_prompt: str = ""
    template: str = ""
    content: str = ""
    body: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    mix_sources: List[str] = field(default_factory=list)
    mix_ai_model: Optional[str] = None
    python_module: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.prompt_id:
            self.prompt_id = self.display_name or "prompt"
        if not self.display_name:
            self.display_name = self.prompt_id
        if not self.template:
            self.template = self.content or self.body
        if not self.content:
            self.content = self.template or self.body
        if not self.body:
            self.body = self.template or self.content
        if not isinstance(self.variables, dict):
            if isinstance(self.variables, list):
                merged: Dict[str, Any] = {}
                for item in self.variables:
                    if isinstance(item, dict) and "name" in item:
                        merged[str(item["name"])] = item.get("default")
                self.variables = merged
            else:
                self.variables = dict(self.variables or {})

    @property
    def id(self) -> str:
        return self.prompt_id

    @property
    def prompt_uuid(self) -> str:
        return self.uuid

    @property
    def name(self) -> str:
        return self.display_name or self.prompt_id

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def render(self, variables: Optional[Dict[str, Any]] = None, context: Optional[Dict[str, Any]] = None) -> str:
        values: Dict[str, Any] = dict(self.variables)
        if variables:
            values.update(variables)
        source = self.template or self.content or self.body or self.system_prompt
        return render_template(source, variables=values, context=context)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.prompt_id,
            "prompt_id": self.prompt_id,
            "uuid": self.uuid,
            "prompt_uuid": self.uuid,
            "display_name": self.display_name or self.prompt_id,
            "name": self.display_name or self.prompt_id,
            "description": self.description,
            "icon": self.icon,
            "system_prompt": self.system_prompt,
            "template": self.template,
            "content": self.content or self.template,
            "body": self.body or self.template,
            "variables": dict(self.variables),
            "tags": list(self.tags),
            "mix_sources": list(self.mix_sources),
            "mix_ai_model": self.mix_ai_model,
            "python_module": self.python_module,
            "metadata": dict(self.metadata),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PromptDefinition":
        variables = data.get("variables", {})
        if isinstance(variables, list):
            mapped: Dict[str, Any] = {}
            for item in variables:
                if isinstance(item, dict) and "name" in item:
                    mapped[str(item["name"])] = item.get("default")
            variables = mapped
        body = data.get("body", data.get("content", ""))
        template = data.get("template", body)
        return cls(
            prompt_id=data.get("prompt_id", data.get("id", data.get("name", ""))),
            uuid=data.get("uuid", data.get("prompt_uuid", "")) or str(_uuid.uuid4()),
            display_name=data.get("display_name", data.get("name", data.get("prompt_id", ""))),
            description=data.get("description", ""),
            icon=data.get("icon", ""),
            system_prompt=data.get("system_prompt", ""),
            template=template,
            content=data.get("content", template),
            body=body,
            variables=dict(variables or {}),
            tags=list(data.get("tags", [])),
            mix_sources=list(data.get("mix_sources", [])),
            mix_ai_model=data.get("mix_ai_model"),
            python_module=data.get("python_module"),
            metadata=dict(data.get("metadata", {})),
            enabled=bool(data.get("enabled", True)),
        )


PromptEntry = PromptDefinition


class PromptManager:
    def __init__(self, prompts_dir: Optional[Path] = None) -> None:
        self._prompts: Dict[str, PromptDefinition] = {}
        self._uuid_index: Dict[str, str] = {}
        self._prompts_dir = Path(prompts_dir) if prompts_dir is not None else None
        if self._prompts_dir and self._prompts_dir.is_dir():
            self._load_from_disk()

    def _prompt_path(self, prompt_id: str) -> Optional[Path]:
        if self._prompts_dir is None:
            return None
        return self._prompts_dir / _safe_filename(prompt_id) / "prompt.json"

    def _load_from_disk(self) -> None:
        if not self._prompts_dir:
            return
        for path in sorted(self._prompts_dir.glob("*/prompt.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                prompt = PromptDefinition.from_dict(raw)
                self._prompts[prompt.prompt_id or path.parent.name] = prompt
                self._uuid_index[prompt.uuid] = prompt.prompt_id
            except Exception:
                continue

    def _persist(self, prompt: PromptDefinition) -> None:
        path = self._prompt_path(prompt.prompt_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prompt.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def create(self, definition: PromptDefinition | Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(definition, dict):
            definition = PromptDefinition.from_dict(definition)
        if definition.prompt_id in self._prompts:
            return {"error": f"Prompt already exists: {definition.prompt_id}", "status_code": 409}
        self._prompts[definition.prompt_id] = definition
        self._uuid_index[definition.uuid] = definition.prompt_id
        self._persist(definition)
        return {"created": True, "prompt_id": definition.prompt_id, "uuid": definition.uuid}

    def read(self, prompt_id: str) -> Optional[PromptDefinition]:
        return self._prompts.get(prompt_id)

    def get(self, prompt_id: str) -> Optional[PromptDefinition]:
        return self.read(prompt_id)

    def update(self, prompt_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        prompt = self._prompts.get(prompt_id)
        if prompt is None:
            return {"error": f"Prompt not found: {prompt_id}", "status_code": 404}
        for key, value in updates.items():
            if key in {"prompt_id", "uuid", "prompt_uuid"}:
                continue
            if key in {"name", "display_name"}:
                prompt.display_name = str(value)
            elif key in {"body", "content", "template"}:
                text = str(value)
                prompt.template = text
                prompt.content = text
                prompt.body = text
            elif key == "variables":
                if isinstance(value, dict):
                    prompt.variables = dict(value)
                elif isinstance(value, list):
                    mapped: Dict[str, Any] = {}
                    for item in value:
                        if isinstance(item, dict) and "name" in item:
                            mapped[str(item["name"])] = item.get("default")
                    prompt.variables = mapped
                else:
                    prompt.variables = dict(value or {})
            elif hasattr(prompt, key):
                setattr(prompt, key, value)
        self._persist(prompt)
        return {"updated": True, "prompt_id": prompt_id}

    def delete(self, prompt_id: str) -> Dict[str, Any]:
        prompt = self._prompts.pop(prompt_id, None)
        if prompt is None:
            return {"error": f"Prompt not found: {prompt_id}", "status_code": 404}
        self._uuid_index.pop(prompt.uuid, None)
        path = self._prompt_path(prompt_id)
        if path is not None:
            path.unlink(missing_ok=True)
        return {"deleted": True, "prompt_id": prompt_id}

    def list_prompts(self) -> List[PromptDefinition]:
        return [self._prompts[key] for key in sorted(self._prompts.keys())]

    def list_all(self) -> List[Dict[str, Any]]:
        return [prompt.to_dict() for prompt in self.list_prompts()]

    def render(
        self,
        prompt_id: str,
        variables: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        prompt = self.read(prompt_id)
        if prompt is None:
            return {"error": f"Prompt not found: {prompt_id}", "status_code": 404}
        rendered = prompt.render(variables=variables, context=context)
        values = dict(prompt.variables)
        if variables:
            values.update(variables)
        return {
            "prompt_id": prompt_id,
            "system_prompt": prompt.system_prompt,
            "rendered": rendered,
            "variables_used": values,
        }

    def render_prompt(
        self,
        prompt_id: str,
        variables: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.render(prompt_id, variables=variables, context=context)

    def mix(
        self,
        prompts: Sequence[str | PromptDefinition],
        variables: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        separator: str = "\n\n---\n\n",
    ) -> Dict[str, Any]:
        resolved: List[PromptDefinition] = []
        for item in prompts:
            prompt = item if isinstance(item, PromptDefinition) else self.read(str(item))
            if prompt is None:
                continue
            resolved.append(prompt)
        if not resolved:
            return {"mixed": "", "source_ids": [], "preview": "", "status_code": 404}
        parts: List[str] = []
        for prompt in resolved:
            if prompt.system_prompt:
                parts.append(prompt.system_prompt)
            rendered = prompt.render(variables=variables, context=context)
            if rendered:
                parts.append(rendered)
        mixed = separator.join(parts)
        source_ids = [prompt.prompt_id for prompt in resolved]
        return {
            "mixed": mixed,
            "source_ids": source_ids,
            "preview": mixed[:500],
        }

    def mix_prompts(
        self,
        prompts: Sequence[str | PromptDefinition],
        variables: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        separator: str = "\n\n---\n\n",
    ) -> Dict[str, Any]:
        return self.mix(prompts, variables=variables, context=context, separator=separator)

    def preview_mix(
        self,
        prompt_ids: Sequence[str | PromptDefinition],
        variables: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.mix(prompt_ids, variables=variables, context=context)

    def get_metadata_index(self) -> List[Dict[str, Any]]:
        return [
            {
                "prompt_id": prompt.prompt_id,
                "uuid": prompt.uuid,
                "display_name": prompt.display_name,
                "icon": prompt.icon,
                "tags": list(prompt.tags),
            }
            for prompt in self.list_prompts()
        ]

    def generate_index(self) -> List[Dict[str, Any]]:
        return self.get_metadata_index()

    def to_template(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        prompt = self.read(prompt_id)
        return prompt.to_dict() if prompt is not None else None

    def create_from_template(self, template: Dict[str, Any]) -> PromptDefinition:
        prompt = PromptDefinition.from_dict(template)
        self.create(prompt)
        return prompt


_PROMPT_MANAGER: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    global _PROMPT_MANAGER
    if _PROMPT_MANAGER is None:
        _PROMPT_MANAGER = PromptManager()
    return _PROMPT_MANAGER
