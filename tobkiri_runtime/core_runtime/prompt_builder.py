"""Prompt Builder — ステップバイステップでプロンプトを構築するビルダー。

既存の domain/prompt モジュール (manager.py, template.py, renderer.py) を
読み取り専用で使用し、高度なプロンプト構築機能を提供する。

機能:
    - セクション単位の段階的プロンプト構築
    - 条件付きセクション（コンテキスト長やパターンに応じた有効/無効制御）
    - 変数埋め込み
    - プロンプト継承（ベースプロンプトのセクションをオーバーライド）
    - PromptTemplate への変換

セクションデータ形式:
    {
        "id":        str,            # セクション一意ID
        "label":     str,            # 表示用ラベル
        "body":      str,            # テンプレート本文
        "order":     int,            # 並び順
        "enabled":   bool,           # 有効/無効
        "condition":  dict | None,   # 条件定義 (None=常に有効)
    }

条件定義:
    {
        "field":    str,     # 評価対象フィールド名 (context変数名)
        "operator": str,     # "eq","neq","gt","gte","lt","lte","contains","matches"
        "value":    Any      # 比較値
    }
"""

from __future__ import annotations

import copy
import re
import types
import uuid
from collections.abc import Callable
from typing import Any

__all__ = ["PromptBuilder", "evaluate_condition"]


_VAR_PATTERN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class _CorePromptTemplate:
    def __init__(
        self,
        name: str = "",
        description: str = "",
        variables: list[dict] | None = None,
        body: str = "",
        metadata: dict | None = None,
    ):
        self.name = name
        self.description = description
        self.variables: list[dict] = list(variables or [])
        self.body = body
        self.metadata: dict = dict(metadata or {})

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "variables": copy.deepcopy(self.variables),
            "body": self.body,
            "metadata": copy.deepcopy(self.metadata),
        }


def _render_template(template: str, variables: dict | None = None) -> str:
    if not variables:
        return template

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key in variables:
            return str(variables[key])
        return match.group(0)

    return _VAR_PATTERN.sub(_replace, template)


# ---------------------------------------------------------------------------
# 条件評価
# ---------------------------------------------------------------------------

def evaluate_condition(condition: dict | None, variables: dict) -> bool:
    """条件定義を評価して真偽を返す。

    condition が None または空の場合は常に True（セクション有効）。

    Args:
        condition: {"field": str, "operator": str, "value": Any}
        variables: 現在のコンテキスト変数 dict

    Returns:
        条件を満たす場合 True
    """
    if not condition:
        return True

    field = condition.get("field", "")
    operator = condition.get("operator", "eq")
    expected = condition.get("value")

    actual = variables.get(field)

    # フィールドが存在しない場合: eq で None と比較 → expected が None なら True
    if actual is None and field not in variables:
        if operator == "eq":
            return expected is None
        if operator == "neq":
            return expected is not None
        return False

    try:
        if operator == "eq":
            return actual == expected
        if operator == "neq":
            return actual != expected
        if operator == "gt":
            if actual is None or expected is None:
                return False
            return float(actual) > float(expected)
        if operator == "gte":
            if actual is None or expected is None:
                return False
            return float(actual) >= float(expected)
        if operator == "lt":
            if actual is None or expected is None:
                return False
            return float(actual) < float(expected)
        if operator == "lte":
            if actual is None or expected is None:
                return False
            return float(actual) <= float(expected)
        if operator == "contains":
            return str(expected) in str(actual)
        if operator == "matches":
            return bool(re.search(str(expected), str(actual)))
    except (TypeError, ValueError):
        return False

    return False


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

class PromptBuilder:
    """ステップバイステップでプロンプトを構築するビルダー。

    使用例:
        builder = PromptBuilder(name="my_prompt")
        builder.add_section("intro", "あなたは優秀なアシスタントです。", order=0)
        builder.add_section("rules", "ルール: {{rules}}", order=1)
        builder.add_conditional_section(
            "long_context_note",
            "コンテキストが長いので要約してください。",
            order=2,
            condition={"field": "context.total_tokens", "operator": "gt", "value": 4000},
        )
        builder.set_variable("rules", "丁寧に回答する")
        template = builder.build()
    """

    def __init__(
        self,
        name: str = "",
        description: str = "",
        metadata: dict | None = None,
        *,
        prompt_template_factory: Callable[..., Any] | None = None,
        prompt_manager_factory: Callable[[], Any] | None = None,
        render_func: Callable[[str, dict | None], str] | None = None,
    ):
        self.name = name
        self.description = description
        self.metadata: dict = dict(metadata or {})
        self._sections: dict[str, dict] = {}
        self._variables: dict[str, Any] = {}
        self._parent_name: str | None = None
        self._prompt_template_factory = prompt_template_factory
        self._prompt_manager_factory = prompt_manager_factory
        self._render_func = render_func

    @classmethod
    def with_dependencies(
        cls,
        *,
        prompt_template_factory: Callable[..., Any] | None = None,
        prompt_manager_factory: Callable[[], Any] | None = None,
        render_func: Callable[[str, dict | None], str] | None = None,
    ) -> type["PromptBuilder"]:
        dependencies = {
            key: value
            for key, value in {
                "prompt_template_factory": prompt_template_factory,
                "prompt_manager_factory": prompt_manager_factory,
                "render_func": render_func,
            }.items()
            if value is not None
        }

        def bound_init(self: PromptBuilder, *args: Any, **kwargs: Any) -> None:
            for key, value in dependencies.items():
                kwargs.setdefault(key, value)
            cls.__init__(self, *args, **kwargs)

        BoundPromptBuilder = types.new_class(
            cls.__name__,
            (cls,),
            exec_body=lambda namespace: namespace.update({"__init__": bound_init}),
        )

        BoundPromptBuilder.__name__ = cls.__name__
        BoundPromptBuilder.__qualname__ = cls.__qualname__
        BoundPromptBuilder.__module__ = cls.__module__
        return BoundPromptBuilder

    # -- セクション操作 -------------------------------------------------------

    def add_section(
        self,
        section_id: str,
        body: str,
        order: int = 0,
        label: str = "",
        enabled: bool = True,
    ) -> "PromptBuilder":
        """通常セクションを追加する。

        Args:
            section_id: セクションの一意識別子
            body:       テンプレート本文（{{var}} を含められる）
            order:      並び順（小さいほど先頭）
            label:      表示用ラベル（空なら section_id を使用）
            enabled:    初期状態で有効かどうか

        Returns:
            self（メソッドチェーン用）
        """
        self._sections[section_id] = {
            "id": section_id,
            "label": label or section_id,
            "body": body,
            "order": order,
            "enabled": enabled,
            "condition": None,
        }
        return self

    def add_conditional_section(
        self,
        section_id: str,
        body: str,
        condition: dict,
        order: int = 0,
        label: str = "",
    ) -> "PromptBuilder":
        """条件付きセクションを追加する。

        セクションは condition が True と評価された場合のみ有効になる。

        Args:
            section_id: セクションの一意識別子
            body:       テンプレート本文
            condition:  {"field": str, "operator": str, "value": Any}
            order:      並び順
            label:      表示用ラベル

        Returns:
            self（メソッドチェーン用）
        """
        self._sections[section_id] = {
            "id": section_id,
            "label": label or section_id,
            "body": body,
            "order": order,
            "enabled": True,
            "condition": condition,
        }
        return self

    def remove_section(self, section_id: str) -> "PromptBuilder":
        """セクションを削除する。

        Args:
            section_id: 削除対象のセクションID

        Returns:
            self（メソッドチェーン用）
        """
        self._sections.pop(section_id, None)
        return self

    def toggle_section(self, section_id: str, enabled: bool) -> "PromptBuilder":
        """セクションの有効/無効を切り替える。

        Args:
            section_id: 対象セクションID
            enabled:    True で有効、False で無効

        Returns:
            self（メソッドチェーン用）
        """
        if section_id in self._sections:
            self._sections[section_id]["enabled"] = enabled
        return self

    def get_sections(self) -> list[dict]:
        """全セクションを order 順で返す。

        Returns:
            セクション dict のリスト
        """
        return sorted(self._sections.values(), key=lambda s: s["order"])

    # -- 変数操作 ------------------------------------------------------------

    def set_variable(self, name: str, value: Any) -> "PromptBuilder":
        """変数を設定する。

        Args:
            name:  変数名（例: "rules", "context.total_tokens"）
            value: 変数値

        Returns:
            self（メソッドチェーン用）
        """
        self._variables[name] = value
        return self

    def set_variables(self, variables: dict) -> "PromptBuilder":
        """複数の変数を一括設定する。

        Args:
            variables: {変数名: 値} の dict

        Returns:
            self（メソッドチェーン用）
        """
        self._variables.update(variables)
        return self

    def get_variables(self) -> dict:
        """現在の変数 dict を返す。"""
        return dict(self._variables)

    # -- 継承 ---------------------------------------------------------------

    def inherit_from(self, parent_name: str) -> "PromptBuilder":
        """親プロンプト名を設定する。

        build() 時に PromptManager から親プロンプトを取得し、
        親のセクション群にこのビルダーのセクションをオーバーライドして
        マージする。

        Args:
            parent_name: 親プロンプトの名前

        Returns:
            self（メソッドチェーン用）
        """
        self._parent_name = parent_name
        return self

    # -- ビルド -------------------------------------------------------------

    def _resolve_parent_sections(self) -> dict[str, dict]:
        """親プロンプトからセクション群を解決する。

        親プロンプトの metadata.sections にセクション定義が格納されている前提。
        格納されていない場合は body 全体を "main" セクションとして扱う。

        Returns:
            section_id → section dict のマッピング
        """
        if not self._parent_name:
            return {}

        if self._prompt_manager_factory is None:
            return {}

        manager = self._prompt_manager_factory()
        parent = manager.get_prompt_by_name(self._parent_name)
        if parent is None:
            return {}

        parent_meta = parent.get("metadata", {})
        parent_sections_raw = parent_meta.get("sections", None)

        if parent_sections_raw and isinstance(parent_sections_raw, list):
            result = {}
            for sec in parent_sections_raw:
                sid = sec.get("id", "")
                if sid:
                    result[sid] = copy.deepcopy(sec)
            return result

        # セクション情報がない場合、body 全体を "main" セクションとして扱う
        body = parent.get("body", parent.get("content", ""))
        return {
            "main": {
                "id": "main",
                "label": "main",
                "body": body,
                "order": 0,
                "enabled": True,
                "condition": None,
            }
        }

    def _merge_sections(self) -> list[dict]:
        """親セクションと子セクションをマージし、order 順で返す。

        子で定義された section_id と同じ ID が親にある場合、子で上書きする。
        子にしかないセクションは追加される。

        Returns:
            マージ済みセクションの order 順リスト
        """
        parent_sections = self._resolve_parent_sections()
        merged = copy.deepcopy(parent_sections)
        for sid, sec in self._sections.items():
            merged[sid] = copy.deepcopy(sec)
        return sorted(merged.values(), key=lambda s: s["order"])

    def build(self, context_variables: dict | None = None) -> Any:
        """全セクションをマージ・条件評価・レンダリングして PromptTemplate を生成する。

        Args:
            context_variables: 条件評価および変数解決に使用する追加変数。
                               set_variable() で設定した変数とマージされる。

        Returns:
            構築済みの PromptTemplate
        """
        all_vars = dict(self._variables)
        if context_variables:
            for k, v in context_variables.items():
                if k not in all_vars:
                    all_vars[k] = v

        merged_sections = self._merge_sections()
        active_bodies: list[str] = []
        active_section_records: list[dict] = []

        for sec in merged_sections:
            if not sec.get("enabled", True):
                continue
            if not evaluate_condition(sec.get("condition"), all_vars):
                continue
            active_bodies.append(sec["body"])
            active_section_records.append(copy.deepcopy(sec))

        combined_body = "\n\n".join(active_bodies)

        # 変数定義を抽出
        variable_defs = _extract_variable_defs(combined_body, all_vars)

        meta = dict(self.metadata)
        meta["sections"] = active_section_records
        if self._parent_name:
            meta["parent"] = self._parent_name

        template_factory = self._prompt_template_factory or _CorePromptTemplate
        return template_factory(
            name=self.name,
            description=self.description,
            variables=variable_defs,
            body=combined_body,
            metadata=meta,
        )

    def render(self, context_variables: dict | None = None) -> str:
        """ビルドしてレンダリングまで行い、最終文字列を返す。

        Args:
            context_variables: 追加のコンテキスト変数

        Returns:
            レンダリング済みプロンプト文字列
        """
        all_vars = dict(self._variables)
        if context_variables:
            all_vars.update(context_variables)

        template = self.build(context_variables)
        render_template = self._render_func or _render_template
        return render_template(template.body, all_vars)

    # -- シリアライズ --------------------------------------------------------

    def to_dict(self) -> dict:
        """ビルダーの現在状態を dict に変換する（永続化・API レスポンス用）。"""
        return {
            "name": self.name,
            "description": self.description,
            "metadata": copy.deepcopy(self.metadata),
            "sections": self.get_sections(),
            "variables": dict(self._variables),
            "parent": self._parent_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PromptBuilder":
        """dict からビルダーを復元する。"""
        builder = cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            metadata=data.get("metadata"),
        )
        for sec in data.get("sections", []):
            sid = sec.get("id", uuid.uuid4().hex[:8])
            condition = sec.get("condition")
            if condition:
                builder.add_conditional_section(
                    section_id=sid,
                    body=sec.get("body", ""),
                    condition=condition,
                    order=sec.get("order", 0),
                    label=sec.get("label", ""),
                )
            else:
                builder.add_section(
                    section_id=sid,
                    body=sec.get("body", ""),
                    order=sec.get("order", 0),
                    label=sec.get("label", ""),
                    enabled=sec.get("enabled", True),
                )
        for k, v in data.get("variables", {}).items():
            builder.set_variable(k, v)
        parent = data.get("parent")
        if parent:
            builder.inherit_from(parent)
        return builder


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _extract_variable_defs(body: str, known_values: dict) -> list[dict]:
    """テンプレート本文から変数定義リストを生成する。

    Args:
        body:         テンプレート本文
        known_values: 既知の変数名→値 マッピング

    Returns:
        [{"name": str, "type": str, "default": Any, "required": bool}, ...]
    """
    found_names = _VAR_PATTERN.findall(body)
    seen: set[str] = set()
    result: list[dict] = []
    for var_name in found_names:
        if var_name in seen:
            continue
        seen.add(var_name)
        default_val = known_values.get(var_name)
        result.append({
            "name": var_name,
            "type": "string",
            "default": default_val,
            "required": var_name not in known_values,
        })
    return result
