"""
domain.tool.runtime_creator — ランタイムtool作成ロジック。

AIが自然言語で「こういうtoolが欲しい」と記述すると、
tool定義（名前、説明、パラメータスキーマ、実行ロジック）を自動生成し、
検証・登録・永続化を行う。

既存の domain/tool/ ファイルは変更しない。
"""

import ast
import json
import re
import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.tool.registry import ToolRegistry
from domain.ai_client.client import AIClient


# ======================================================================
# 定数
# ======================================================================

_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$")

_VALID_JSON_SCHEMA_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}

_RUNTIME_TAG = "runtime-created"

_GENERATE_SYSTEM_PROMPT = (
    "You are a tool definition generator for the rumiai ecosystem.\n"
    "Given a natural-language description, produce a JSON object with these keys:\n"
    "  - name: string (snake_case, letters/digits/underscores, starts with letter, max 63 chars)\n"
    "  - description: string (concise summary of the tool)\n"
    "  - parameters: JSON Schema object (type: object, with properties and required)\n"
    "  - handler_code: Python code string defining: def handler(arguments, context):\n"
    "    The handler must return a dict with keys: result (str), is_error (bool), widget (dict|None)\n"
    "\n"
    "Rules:\n"
    "  - Output ONLY valid JSON, no markdown fences, no explanation.\n"
    "  - handler_code must be self-contained. No imports of external packages.\n"
    "  - Available builtins in sandbox: abs, all, any, bool, bytes, callable, chr, dict, "
    "divmod, enumerate, filter, float, format, frozenset, getattr, hasattr, hash, hex, "
    "id, int, isinstance, issubclass, iter, len, list, map, max, min, next, oct, ord, pow, "
    "print, range, repr, reversed, round, set, slice, sorted, str, sum, tuple, type, zip.\n"
    "  - No import, no open, no exec, no eval, no compile, no __import__.\n"
    "  - parameters must be valid JSON Schema with type: object.\n"
)


# ======================================================================
# RuntimeToolCreator
# ======================================================================

class RuntimeToolCreator:
    """ランタイムtool作成・検証・登録を行うファサード（シングルトン）"""

    _instance = None
    _initialized: bool

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._registry = ToolRegistry()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # AI 生成
    # ------------------------------------------------------------------

    def generate_from_description(self, description, model=None):
        """
        自然言語の説明からtool定義を自動生成する。

        引数:
            description: str — ユーザーの自然言語記述
            model: str|None — 使用するAIモデル（"provider/model"形式）

        戻り値: dict with keys: name, description, parameters, handler_code
        例外: RuntimeError — AI生成失敗時
        """
        client = AIClient()

        if model is None:
            providers = client.list_providers()
            for p in providers:
                if p["id"] != "stub":
                    models = client.list_models(provider=p["id"])
                    if models:
                        model = models[0]["id"]
                        break
            if model is None:
                raise RuntimeError(
                    "No AI provider available. Please specify a model parameter."
                )

        messages = [
            {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Generate a tool definition for the following description:\n\n"
                    "{}\n\n"
                    "Output ONLY a JSON object with keys: name, description, parameters, handler_code"
                ).format(description),
            },
        ]

        try:
            result = client.complete(model, messages)
        except Exception as exc:
            raise RuntimeError("AI completion failed: {}".format(exc))

        content = self._extract_content(result)
        if not content:
            raise RuntimeError("AI returned empty content")

        tool_def = self._parse_ai_output(content)
        return tool_def

    def _extract_content(self, result):
        """AIClient.complete の返り値からテキストを抽出する"""
        if not isinstance(result, dict):
            return ""
        content = result.get("content", "")
        if not content and "choices" in result:
            choices = result.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content = "\n".join(parts)
        return content if isinstance(content, str) else ""

    def _parse_ai_output(self, text):
        """AI出力テキストからJSON tool定義をパースする"""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            inner = []
            in_fence = False
            for line in lines:
                if line.strip().startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    inner.append(line)
            cleaned = "\n".join(inner).strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "AI output is not valid JSON: {}".format(exc)
            )

        if not isinstance(parsed, dict):
            raise RuntimeError("AI output is not a JSON object")

        required_keys = {"name", "description", "parameters", "handler_code"}
        missing = required_keys - set(parsed.keys())
        if missing:
            raise RuntimeError(
                "AI output missing keys: {}".format(", ".join(sorted(missing)))
            )

        return {
            "name": parsed["name"],
            "description": parsed["description"],
            "parameters": parsed["parameters"],
            "handler_code": parsed["handler_code"],
        }

    # ------------------------------------------------------------------
    # バリデーション
    # ------------------------------------------------------------------

    def validate_tool_definition(self, tool_def):
        """
        tool定義を検証する。

        引数: tool_def: dict with keys: name, description, parameters, handler_code
        戻り値: {"valid": bool, "errors": [str], "warnings": [str]}
        """
        errors = []
        warnings: list[str] = []

        # --- name ---
        name = tool_def.get("name")
        if not name or not isinstance(name, str):
            errors.append("name is required and must be a non-empty string")
        elif not _NAME_PATTERN.match(name):
            errors.append(
                "name must match [a-zA-Z][a-zA-Z0-9_]{{0,62}} (got '{}')".format(name)
            )
        else:
            existing = self._registry.get(name)
            if existing is not None:
                exec_type = existing.get("execution", {}).get("type", "")
                if exec_type != "dynamic":
                    errors.append(
                        "name '{}' conflicts with a built-in tool".format(name)
                    )
                else:
                    warnings.append(
                        "name '{}' already exists as a dynamic tool (will overwrite)".format(name)
                    )

        # --- description ---
        description = tool_def.get("description")
        if not description or not isinstance(description, str):
            errors.append("description is required and must be a non-empty string")

        # --- parameters ---
        parameters = tool_def.get("parameters")
        if parameters is None:
            errors.append("parameters is required")
        elif not isinstance(parameters, dict):
            errors.append("parameters must be a dict (JSON Schema object)")
        else:
            param_errors = self._validate_json_schema(parameters)
            errors.extend(param_errors)

        # --- handler_code ---
        handler_code = tool_def.get("handler_code")
        if not handler_code or not isinstance(handler_code, str):
            errors.append("handler_code is required and must be a non-empty string")
        else:
            code_errors, code_warnings = self._validate_handler_code(handler_code)
            errors.extend(code_errors)
            warnings.extend(code_warnings)

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

    def _validate_json_schema(self, schema, path="parameters"):
        """JSON Schema の基本的な妥当性を検証する"""
        errors = []
        schema_type = schema.get("type")
        if path == "parameters" and schema_type != "object":
            errors.append(
                "parameters.type must be 'object' (got '{}')".format(schema_type)
            )
            return errors

        if schema_type == "array":
            items = schema.get("items")
            if not isinstance(items, dict):
                errors.append("{}.items must be a dict".format(path))
            else:
                errors.extend(self._validate_json_schema(items, "{}.items".format(path)))
            return errors

        properties = schema.get("properties")
        if properties is not None:
            if not isinstance(properties, dict):
                errors.append("{}.properties must be a dict".format(path))
            else:
                for prop_name, prop_schema in properties.items():
                    if not isinstance(prop_schema, dict):
                        errors.append(
                            "{}.properties.{} must be a dict".format(path, prop_name)
                        )
                        continue
                    prop_type = prop_schema.get("type")
                    if prop_type is not None and prop_type not in _VALID_JSON_SCHEMA_TYPES:
                        errors.append(
                            "{}.properties.{}.type '{}' is not a valid JSON Schema type".format(
                                path, prop_name, prop_type
                            )
                        )
                    errors.extend(
                        self._validate_json_schema(
                            prop_schema,
                            "{}.properties.{}".format(path, prop_name),
                        )
                    )

        required = schema.get("required")
        if required is not None:
            if not isinstance(required, list):
                errors.append("{}.required must be a list".format(path))
            elif properties is not None and isinstance(properties, dict):
                for req_name in required:
                    if req_name not in properties:
                        errors.append(
                            "{}.required references '{}' but it is not in properties".format(
                                path, req_name
                            )
                        )

        return errors

    def _validate_handler_code(self, handler_code):
        """handler_code の構文チェックとセキュリティチェック"""
        errors = []
        warnings: list[str] = []

        if "def handler(" not in handler_code and "def handler (" not in handler_code:
            errors.append(
                "handler_code must define a function named 'handler' "
                "with signature: def handler(arguments, context):"
            )
            return errors, warnings

        # 構文チェック
        try:
            compile(handler_code, "<runtime_tool_handler>", "exec")
        except SyntaxError as exc:
            errors.append(
                "handler_code has a syntax error: line {}, {}".format(
                    exc.lineno, exc.msg
                )
            )
            return errors, warnings

        # 危険パターン検出
        dangerous_patterns = [
            (r'\b__import__\b', "__import__ is forbidden in handler_code"),
            (r'\bimport\s+', "import statements are forbidden in handler_code"),
            (r'\bfrom\s+\S+\s+import\b', "from-import statements are forbidden in handler_code"),
            (r'\bopen\s*\(', "open() is forbidden in handler_code"),
            (r'\beval\s*\(', "eval() is forbidden in handler_code"),
            (r'\bexec\s*\(', "exec() is forbidden in handler_code"),
            (r'\bcompile\s*\(', "compile() is forbidden in handler_code"),
            (r'\bglobals\s*\(', "globals() is forbidden in handler_code"),
            (r'\blocals\s*\(', "locals() is forbidden in handler_code"),
            (r'\bbreakpoint\s*\(', "breakpoint() is forbidden in handler_code"),
            (r'\b__builtins__\b', "accessing __builtins__ is forbidden in handler_code"),
            (r'\bos\.\b', "os module access is forbidden in handler_code"),
            (r'\bsys\.\b', "sys module access is forbidden in handler_code"),
            (r'\bsubprocess\b', "subprocess is forbidden in handler_code"),
        ]

        for pattern, msg in dangerous_patterns:
            if re.search(pattern, handler_code):
                errors.append(msg)

        # サンドボックス実行テスト
        if not errors:
            sandbox_errors = self._sandbox_test(handler_code)
            errors.extend(sandbox_errors)

        return errors, warnings

    def _sandbox_test(self, handler_code):
        """Statically confirm the legacy source shape without executing it."""

        try:
            tree = ast.parse(handler_code, mode="exec")
        except SyntaxError as exc:
            return ["handler_code has a syntax error: {}".format(exc.msg)]
        handlers = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "handler"
        ]
        if not handlers:
            return ["handler_code does not define a top-level 'handler' function"]
        return []

    # ------------------------------------------------------------------
    # 登録
    # ------------------------------------------------------------------

    def register_runtime_tool(self, tool_def):
        """Reject runtime Python registration and provide migration guidance."""

        del tool_def
        raise ValueError(
            "migration_required: dynamic Python Tools are retired; "
            "use a reviewed pack, MCP server, or connector"
        )

    # ------------------------------------------------------------------
    # 削除
    # ------------------------------------------------------------------

    def unregister_runtime_tool(self, name):
        """
        ランタイムtoolを削除する。

        引数: name: str
        戻り値: 削除された tool_def dict
        例外: ValueError — 見つからない / 動的ツールでない場合
        """
        existing = self._registry.get(name)
        if existing is None:
            raise ValueError("Tool '{}' not found".format(name))

        exec_type = existing.get("execution", {}).get("type", "")
        if exec_type != "dynamic":
            raise ValueError(
                "Tool '{}' is not a dynamic tool (type='{}')".format(name, exec_type)
            )

        deleted = self._registry.unregister_dynamic(name)
        if deleted is None:
            raise ValueError("Failed to delete tool '{}'".format(name))

        return deleted

    # ------------------------------------------------------------------
    # 一覧
    # ------------------------------------------------------------------

    def list_runtime_tools(self, tags=None):
        """
        ランタイムで登録された動的ツール一覧を返す。

        引数: tags: list|None — タグフィルタ
        戻り値: list of dict
        """
        all_tools = self._registry.list_tools()
        runtime_tools = []
        for tool in all_tools:
            exec_type = tool.get("execution", {}).get("type", "")
            if exec_type != "dynamic":
                continue
            if tags:
                tool_tags = set(tool.get("tags", []))
                if not set(tags) & tool_tags:
                    continue
            runtime_tools.append({
                "tool_id": tool.get("tool_id", ""),
                "name": tool.get("name", ""),
                "summary": tool.get("summary", ""),
                "tags": tool.get("tags", []),
                "created_at": tool.get("created_at", ""),
                "runtime": tool.get("runtime", False),
            })
        return runtime_tools

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------

    def persist_tool(self, name):
        """Reject persistence of retired executable Python source."""

        del name
        raise ValueError(
            "migration_required: dynamic Python Tools are retired; "
            "use a reviewed pack, MCP server, or connector"
        )
