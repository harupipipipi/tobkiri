# backend_core/ecosystem/spec/schema/validator.py
"""
JSON Schema 検証ユーティリティ

エコシステムの各種定義ファイルを検証する。
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# jsonschemaライブラリを使用（なければフォールバック）
try:
    from jsonschema import Draft7Validator, ValidationError as JsonSchemaValidationError
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False
    JsonSchemaValidationError = Exception


class SchemaValidationError(Exception):
    """スキーマ検証エラー"""
    
    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or []
    
    def __str__(self):
        if self.errors:
            error_list = "\n  - ".join(self.errors)
            return f"{self.args[0]}\n  - {error_list}"
        return self.args[0]


# スキーマファイルのディレクトリ
_SCHEMA_DIR = Path(__file__).parent

# スキーマキャッシュ
_schema_cache: Dict[str, dict] = {}


def _load_schema(schema_name: str) -> dict:
    """スキーマファイルを読み込む"""
    if schema_name in _schema_cache:
        return _schema_cache[schema_name]
    
    schema_file = _SCHEMA_DIR / f"{schema_name}.schema.json"
    
    if not schema_file.exists():
        raise FileNotFoundError(f"スキーマファイルが見つかりません: {schema_file}")
    
    with open(schema_file, 'r', encoding='utf-8') as f:
        schema = json.load(f)
    
    _schema_cache[schema_name] = schema
    return schema


def _validate_with_jsonschema(data: dict, schema: dict) -> List[str]:
    """jsonschemaライブラリを使用して検証"""
    validator = Draft7Validator(schema)
    errors = []
    
    for error in sorted(validator.iter_errors(data), key=lambda e: e.path):
        path = "/".join(str(p) for p in error.absolute_path)
        if path:
            errors.append(f"[/{path}] {error.message}")
        else:
            errors.append(error.message)
    
    return errors


def _validate_basic(data: dict, schema: dict) -> List[str]:
    """基本的な検証（jsonschemaがない場合のフォールバック）"""
    return _validate_basic_value(data, schema, root_schema=schema, path="")


def _validate_basic_value(value: Any, schema: dict, *, root_schema: dict, path: str) -> List[str]:
    schema = _resolve_ref(schema, root_schema)
    errors: List[str] = []

    if "const" in schema and value != schema["const"]:
        return [f"{path or '/'}: 値が const {schema['const']!r} と一致しません"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path or '/'}: 値が enum {schema['enum']} に含まれません"]

    expected_type = schema.get("type")
    if expected_type and not _basic_type_matches(value, expected_type):
        return [
            f"{path or '/'}: 型が不正です: 期待={expected_type}, 実際={type(value).__name__}"
        ]

    for conditional in schema.get("allOf", []) or []:
        conditional = _resolve_ref(conditional, root_schema)
        if "if" in conditional and "then" in conditional:
            if not _validate_basic_value(value, conditional["if"], root_schema=root_schema, path=path):
                errors.extend(
                    _validate_basic_value(value, conditional["then"], root_schema=root_schema, path=path)
                )
        else:
            errors.extend(_validate_basic_value(value, conditional, root_schema=root_schema, path=path))

    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        if all(_validate_basic_value(value, candidate, root_schema=root_schema, path=path) for candidate in any_of):
            errors.append(f"{path or '/'}: anyOf のいずれの条件にも一致しません")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for field in required:
            if field not in value:
                errors.append(f"{path or '/'}: 必須フィールド '{field}' がありません")

        properties = schema.get("properties", {})
        for field, item in value.items():
            item_path = f"{path}/{field}" if path else f"/{field}"
            if field in properties:
                errors.extend(
                    _validate_basic_value(item, properties[field], root_schema=root_schema, path=item_path)
                )

        if schema.get("additionalProperties") is False:
            allowed_props = set(properties.keys())
            for field in value.keys():
                if field not in allowed_props:
                    errors.append(f"{path or '/'}: 不明なフィールド '{field}'")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < int(min_items):
            errors.append(f"{path or '/'}: 配列の要素数が minItems {min_items} 未満です")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _validate_basic_value(
                        item,
                        item_schema,
                        root_schema=root_schema,
                        path=f"{path}/{index}" if path else f"/{index}",
                    )
                )

    pattern = schema.get("pattern")
    if pattern and isinstance(value, str):
        try:
            if not re.match(pattern, value):
                errors.append(f"{path or '/'}: 値 '{value}' がパターン '{pattern}' に一致しません")
        except re.error:
            pass

    min_length = schema.get("minLength")
    if min_length is not None and isinstance(value, str) and len(value) < int(min_length):
        errors.append(f"{path or '/'}: 文字列長が minLength {min_length} 未満です")

    max_length = schema.get("maxLength")
    if max_length is not None and isinstance(value, str) and len(value) > int(max_length):
        errors.append(f"{path or '/'}: 文字列長が maxLength {max_length} を超えています")

    return errors


def _resolve_ref(schema: dict, root_schema: dict) -> dict:
    ref = schema.get("$ref") if isinstance(schema, dict) else ""
    if not ref:
        return schema
    if not str(ref).startswith("#/"):
        return schema
    current: Any = root_schema
    for part in str(ref)[2:].split("/"):
        if not isinstance(current, dict):
            return schema
        current = current.get(part)
    return current if isinstance(current, dict) else schema


def _basic_type_matches(value: Any, expected_type: Any) -> bool:
    type_names = expected_type if isinstance(expected_type, list) else [expected_type]
    for type_name in type_names:
        if type_name == "string" and isinstance(value, str):
            return True
        if type_name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if type_name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if type_name == "boolean" and isinstance(value, bool):
            return True
        if type_name == "array" and isinstance(value, list):
            return True
        if type_name == "object" and isinstance(value, dict):
            return True
        if type_name == "null" and value is None:
            return True
    return False


def validate(
    data: dict,
    schema_name: str,
    raise_on_error: bool = True
) -> List[str]:
    """
    データをスキーマで検証
    
    Args:
        data: 検証対象のデータ
        schema_name: スキーマ名（"ecosystem", "component_manifest", "addon"）
        raise_on_error: エラー時に例外を発生させるか
    
    Returns:
        エラーメッセージのリスト（エラーがなければ空）
    
    Raises:
        SchemaValidationError: raise_on_error=True かつ検証エラーの場合
    """
    schema = _load_schema(schema_name)
    
    if HAS_JSONSCHEMA:
        errors = _validate_with_jsonschema(data, schema)
    else:
        errors = _validate_basic(data, schema)
    
    if errors and raise_on_error:
        raise SchemaValidationError(
            f"{schema_name} の検証に失敗しました",
            errors
        )
    
    return errors


def validate_ecosystem(
    data: dict,
    raise_on_error: bool = True
) -> List[str]:
    """
    ecosystem.json を検証
    
    Args:
        data: ecosystem.jsonの内容
        raise_on_error: エラー時に例外を発生させるか
    
    Returns:
        エラーメッセージのリスト
    """
    return validate(data, "ecosystem", raise_on_error)


def validate_component_manifest(
    data: dict,
    raise_on_error: bool = True
) -> List[str]:
    """
    Component manifest.json を検証
    
    Args:
        data: manifest.jsonの内容
        raise_on_error: エラー時に例外を発生させるか
    
    Returns:
        エラーメッセージのリスト
    """
    return validate(data, "component_manifest", raise_on_error)


def validate_addon(
    data: dict,
    raise_on_error: bool = True
) -> List[str]:
    """
    Addon定義を検証
    
    Args:
        data: addon.jsonの内容
        raise_on_error: エラー時に例外を発生させるか
    
    Returns:
        エラーメッセージのリスト
    """
    return validate(data, "addon", raise_on_error)


def validate_json_patch_operations(
    operations: List[dict]
) -> List[str]:
    """
    JSON Patch操作リストを検証（move/copy禁止）
    
    Args:
        operations: パッチ操作のリスト
    
    Returns:
        エラーメッセージのリスト
    """
    errors = []
    allowed_ops = {"add", "remove", "replace", "test"}
    forbidden_ops = {"move", "copy"}
    
    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            errors.append(f"操作 {i}: オブジェクトである必要があります")
            continue
        
        op_type = op.get("op")
        path = op.get("path")
        
        if not op_type:
            errors.append(f"操作 {i}: 'op' フィールドがありません")
        elif op_type in forbidden_ops:
            errors.append(f"操作 {i}: '{op_type}' は禁止されています")
        elif op_type not in allowed_ops:
            errors.append(f"操作 {i}: 不明な操作 '{op_type}'")
        
        if path is None:
            errors.append(f"操作 {i}: 'path' フィールドがありません")
        elif not isinstance(path, str):
            errors.append(f"操作 {i}: 'path' は文字列である必要があります")
        elif path and not path.startswith('/'):
            errors.append(f"操作 {i}: 'path' は '/' で始まる必要があります")
        
        if op_type in ("add", "replace", "test") and "value" not in op:
            errors.append(f"操作 {i}: '{op_type}' には 'value' が必要です")
    
    return errors


def get_schema(schema_name: str) -> dict:
    """
    スキーマを取得
    
    Args:
        schema_name: スキーマ名
    
    Returns:
        スキーマ辞書
    """
    return _load_schema(schema_name)


def list_available_schemas() -> List[str]:
    """
    利用可能なスキーマ名のリストを取得
    
    Returns:
        スキーマ名のリスト
    """
    schema_files = _SCHEMA_DIR.glob("*.schema.json")
    return [f.stem.replace(".schema", "") for f in schema_files]


def clear_schema_cache():
    """スキーマキャッシュをクリア"""
    _schema_cache.clear()
