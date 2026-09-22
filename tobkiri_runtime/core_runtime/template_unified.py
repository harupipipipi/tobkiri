"""domain.template.unified — tool と prompt の統一テンプレート変換ロジック。

既存の domain/tool, domain/prompt は一切変更しない。
ここでは両方の構造を理解し、相互変換を行うアダプター層を提供する。

統一テンプレート形式 (UnifiedTemplate):
    {
        "name":        str,
        "description": str,
        "parameters":  {"type": "object", "properties": {...}, "required": [...]},
        "template":    str,       # 実行テンプレート / プロンプト本文
        "metadata":    dict,
        "source_type": "tool" | "prompt" | "unified"
    }
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

__all__ = [
    "UnifiedTemplate",
    "tool_to_unified",
    "prompt_to_unified",
    "unified_to_tool",
    "unified_to_prompt",
    "convert_tool_to_prompt",
    "convert_prompt_to_tool",
]


# {{variable}} パターン
_VAR_PATTERN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


# ---------------------------------------------------------------------------
# UnifiedTemplate — 統一テンプレートデータ構造
# ---------------------------------------------------------------------------

class UnifiedTemplate:
    """tool と prompt の共通構造を表現するクラス。

    Attributes:
        name:        テンプレート名
        description: 説明
        parameters:  JSON Schema 形式の入力スキーマ
        template:    テンプレート文字列 ({{var}} を含む)
        metadata:    任意のメタデータ
        source_type: 元の型 ("tool", "prompt", "unified")
    """

    __slots__ = ("name", "description", "parameters", "template", "metadata", "source_type")

    def __init__(
        self,
        name: str = "",
        description: str = "",
        parameters: dict | None = None,
        template: str = "",
        metadata: dict | None = None,
        source_type: str = "unified",
    ):
        self.name = name
        self.description = description
        self.parameters = dict(parameters or {"type": "object", "properties": {}, "required": []})
        self.template = template
        self.metadata = dict(metadata or {})
        self.source_type = source_type

    # ------------------------------------------------------------------
    # シリアライズ
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """JSON シリアライズ可能な dict を返す。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": copy.deepcopy(self.parameters),
            "template": self.template,
            "metadata": copy.deepcopy(self.metadata),
            "source_type": self.source_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UnifiedTemplate":
        """dict から UnifiedTemplate を復元する。"""
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            parameters=data.get("parameters"),
            template=data.get("template", ""),
            metadata=data.get("metadata"),
            source_type=data.get("source_type", "unified"),
        )

    def to_json(self) -> str:
        """JSON 文字列にエクスポートする。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "UnifiedTemplate":
        """JSON 文字列からインポートする。"""
        data = json.loads(json_str)
        return cls.from_dict(data)

    # ------------------------------------------------------------------
    # 変数抽出
    # ------------------------------------------------------------------

    def extract_variable_names(self) -> list[str]:
        """template 内の {{...}} から変数名を抽出して返す。"""
        return list(dict.fromkeys(_VAR_PATTERN.findall(self.template)))

    def extract_user_variables(self) -> list[str]:
        """context.* を除いたユーザー変数のみ返す。"""
        return [v for v in self.extract_variable_names() if not v.startswith("context.")]

    def extract_context_variables(self) -> list[str]:
        """context.* 変数のみ返す。"""
        return [v for v in self.extract_variable_names() if v.startswith("context.")]

    # ------------------------------------------------------------------
    # repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"UnifiedTemplate(name={self.name!r}, source_type={self.source_type!r}, "
            f"params={len(self.parameters.get('properties', {}))}, "
            f"template_len={len(self.template)})"
        )


# ---------------------------------------------------------------------------
# tool → UnifiedTemplate 変換
# ---------------------------------------------------------------------------

def tool_to_unified(tool_def: dict) -> UnifiedTemplate:
    """ToolRegistry のツール定義 dict から UnifiedTemplate を生成する。

    tool_def の形式:
        {
            "tool_id": str,
            "name": str,
            "summary": str,
            "tags": [str],
            "schema": {"parameters": {JSON Schema}},
            "execution": {"type": str, "body": str, ...},
            "handler_code": str (optional),
        }

    変換ルール:
        - name → name
        - summary → description
        - schema.parameters → parameters
        - execution.body があればそれを template にする (prompt type ツール)
        - なければパラメータから template 雛形を自動生成
        - handler_code があれば metadata.handler_code に保存
    """
    name = tool_def.get("name", tool_def.get("tool_id", ""))
    description = tool_def.get("summary", "")
    parameters = copy.deepcopy(
        tool_def.get("schema", {}).get("parameters", {"type": "object", "properties": {}, "required": []})
    )

    execution = tool_def.get("execution", {})
    exec_type = execution.get("type", "local")

    # template の決定
    if exec_type == "prompt" and execution.get("body"):
        template = execution["body"]
    else:
        # パラメータからテンプレート雛形を生成
        template = _generate_template_from_parameters(name, description, parameters)

    metadata = {
        "source_type": "tool",
        "original_tool_id": tool_def.get("tool_id", ""),
        "original_tags": tool_def.get("tags", []),
        "original_execution_type": exec_type,
    }
    handler_code = tool_def.get("handler_code")
    if handler_code:
        metadata["handler_code"] = handler_code

    return UnifiedTemplate(
        name=name,
        description=description,
        parameters=parameters,
        template=template,
        metadata=metadata,
        source_type="tool",
    )


# ---------------------------------------------------------------------------
# prompt → UnifiedTemplate 変換
# ---------------------------------------------------------------------------

def prompt_to_unified(prompt_def: dict) -> UnifiedTemplate:
    """PromptManager のプロンプト定義 dict から UnifiedTemplate を生成する。

    prompt_def の形式:
        {
            "id": str,
            "name": str,
            "content": str,
            "body": str,
            "description": str,
            "variables": [{"name": str, "type": str, "default": Any, "required": bool}],
            "metadata": dict,
        }

    変換ルール:
        - name → name
        - description → description
        - body (or content) → template
        - variables → parameters (JSON Schema に変換)
    """
    name = prompt_def.get("name", "")
    description = prompt_def.get("description", "")
    body = prompt_def.get("body", prompt_def.get("content", ""))
    variables = prompt_def.get("variables", [])

    # variables → JSON Schema parameters
    parameters = _variables_to_json_schema(variables)

    metadata = {
        "source_type": "prompt",
        "original_prompt_id": prompt_def.get("id", ""),
    }
    orig_metadata = prompt_def.get("metadata")
    if orig_metadata:
        metadata["original_metadata"] = copy.deepcopy(orig_metadata)

    return UnifiedTemplate(
        name=name,
        description=description,
        parameters=parameters,
        template=body,
        metadata=metadata,
        source_type="prompt",
    )


# ---------------------------------------------------------------------------
# UnifiedTemplate → tool 定義変換
# ---------------------------------------------------------------------------

def unified_to_tool(ut: UnifiedTemplate) -> dict:
    """UnifiedTemplate から function facade 形式のツール定義 dict を生成する。

    Prompt text is passive. Converted prompt-like templates never create an
    executable ``execution.type="prompt"`` tool; callers must go through the
    trusted prompt_render function if they need rendered text.

    Returns:
        ToolRegistry 互換の tool_def dict
    """
    parameters = copy.deepcopy(ut.parameters)
    tags = ["template-converted"]
    original_tags = ut.metadata.get("original_tags")
    if isinstance(original_tags, list):
        for tag in original_tags:
            if tag not in tags:
                tags.append(tag)

    tool_def: dict[str, Any] = {
        "tool_id": ut.name,
        "name": ut.name,
        "summary": ut.description,
        "tags": tags,
        "schema": {
            "parameters": parameters,
        },
        "execution": {
            "type": "rumi_function",
            "qualified_name": "defaultspack:prompt_render",
        },
        "metadata": {
            "template_facade_preview": True,
            "template_body": ut.template,
        },
    }

    # Keep legacy handler code as inert metadata; dynamic execution is no
    # longer an authoring path for converted templates.
    handler_code = ut.metadata.get("handler_code")
    if handler_code:
        tool_def["metadata"]["legacy_handler_code"] = handler_code

    return tool_def


# ---------------------------------------------------------------------------
# UnifiedTemplate → prompt 定義変換
# ---------------------------------------------------------------------------

def unified_to_prompt(ut: UnifiedTemplate) -> dict:
    """UnifiedTemplate からプロンプト定義 dict を生成する。

    Returns:
        PromptManager.create_prompt() に渡せる dict
    """
    variables = _json_schema_to_variables(ut.parameters)

    return {
        "name": ut.name,
        "content": ut.template,
        "body": ut.template,
        "description": ut.description,
        "variables": variables,
        "metadata": copy.deepcopy(ut.metadata),
    }


# ---------------------------------------------------------------------------
# 直接変換: tool → prompt
# ---------------------------------------------------------------------------

def convert_tool_to_prompt(tool_def: dict) -> dict:
    """ツール定義からプロンプト定義を直接生成する。"""
    unified = tool_to_unified(tool_def)
    return unified_to_prompt(unified)


# ---------------------------------------------------------------------------
# 直接変換: prompt → tool
# ---------------------------------------------------------------------------

def convert_prompt_to_tool(prompt_def: dict) -> dict:
    """プロンプト定義からツール定義を直接生成する。"""
    unified = prompt_to_unified(prompt_def)
    return unified_to_tool(unified)


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------

def _variables_to_json_schema(variables: list[dict]) -> dict:
    """プロンプトの variables リストを JSON Schema に変換する。

    variables: [{"name": str, "type": str, "default": Any, "required": bool}, ...]
    """
    properties: dict[str, dict] = {}
    required: list[str] = []

    for var in variables:
        var_name = var.get("name", "")
        if not var_name:
            continue
        if var_name.startswith("context."):
            continue
        prop: dict[str, Any] = {"type": var.get("type", "string")}
        default = var.get("default")
        if default is not None:
            prop["default"] = default
        properties[var_name] = prop
        if var.get("required", False):
            required.append(var_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _json_schema_to_variables(schema: dict) -> list[dict]:
    """JSON Schema parameters をプロンプトの variables リストに変換する。"""
    properties = schema.get("properties", {})
    required_set = set(schema.get("required", []))
    variables: list[dict] = []

    for var_name, var_def in properties.items():
        variables.append({
            "name": var_name,
            "type": var_def.get("type", "string"),
            "default": var_def.get("default"),
            "required": var_name in required_set,
        })

    return variables


def _generate_template_from_parameters(name: str, description: str, parameters: dict) -> str:
    """パラメータ定義からプロンプトテンプレートの雛形を生成する。"""
    lines: list[str] = []
    if description:
        lines.append(description)
        lines.append("")

    properties = parameters.get("properties", {})
    if properties:
        for var_name in properties:
            lines.append(f"{var_name}: {{{{{var_name}}}}}")
    else:
        lines.append("(no parameters)")

    return "\n".join(lines)
