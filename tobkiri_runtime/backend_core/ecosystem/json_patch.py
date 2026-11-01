# backend_core/ecosystem/json_patch.py
"""
RFC 6902 JSON Patch 実装

サポートする操作:
- add: 値を追加
- remove: 値を削除
- replace: 値を置換
- test: 値をテスト（一致確認）

禁止する操作（セキュリティ上の理由）:
- move: 値の移動
- copy: 値のコピー
"""

import copy
import json
import re
from typing import Any, Dict, List


class JsonPatchError(Exception):
    """JSON Patch操作エラー"""
    pass


class JsonPatchTestError(JsonPatchError):
    """test操作の失敗"""
    pass


class JsonPatchForbiddenError(JsonPatchError):
    """禁止された操作"""
    pass


def _parse_pointer(pointer: str) -> List[str]:
    """
    JSON Pointer (RFC 6901) をパース
    
    Args:
        pointer: JSON Pointer文字列（例: "/foo/bar/0"）
    
    Returns:
        パス要素のリスト
    """
    if not pointer:
        return []
    
    if pointer == "":
        return []
    
    if not pointer.startswith('/'):
        raise JsonPatchError(f"無効なJSON Pointer: '{pointer}' (スラッシュで始まる必要があります)")
    
    # 先頭のスラッシュを除去して分割
    parts = pointer[1:].split('/')
    
    # エスケープ解除: ~1 -> /, ~0 -> ~ (RFC 6901)
    # CR-5 fix: シングルパス正規表現置換で ~01 → ~1 のような
    # 多段エスケープを正しく処理する
    result = []
    for part in parts:
        part = re.sub(r'~([01])', lambda m: '/' if m.group(1) == '1' else '~', part)
        result.append(part)
    
    return result


def _get_by_pointer(doc: Any, pointer: str) -> Any:
    """
    JSON Pointerで指定された値を取得
    
    Args:
        doc: 対象ドキュメント
        pointer: JSON Pointer
    
    Returns:
        指定された値
    """
    if pointer == "":
        return doc
    
    parts = _parse_pointer(pointer)
    current = doc
    
    for i, part in enumerate(parts):
        if isinstance(current, dict):
            if part not in current:
                path = "/" + "/".join(parts[:i+1])
                raise JsonPatchError(f"パスが存在しません: {path}")
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
                if index < 0 or index >= len(current):
                    raise JsonPatchError(f"インデックスが範囲外: {part}")
                current = current[index]
            except ValueError:
                raise JsonPatchError(f"配列に対する無効なインデックス: {part}")
        else:
            path = "/" + "/".join(parts[:i])
            raise JsonPatchError(f"パス {path} はオブジェクトでも配列でもありません")
    
    return current


def _set_by_pointer(doc: Any, pointer: str, value: Any, *, replace: bool = False) -> Any:
    """
    JSON Pointerで指定された位置に値を設定
    
    Args:
        doc: 対象ドキュメント
        pointer: JSON Pointer
        value: 設定する値
    
    Returns:
        更新されたドキュメント
    """
    if pointer == "":
        return value
    
    parts = _parse_pointer(pointer)
    
    # ルートがNoneの場合は新しいオブジェクトを作成
    if doc is None:
        doc = {}
    
    current = doc
    
    # 最後の要素以外をたどる
    for i, part in enumerate(parts[:-1]):
        if isinstance(current, dict):
            if part not in current:
                # 次のパートが数値なら配列、そうでなければオブジェクトを作成
                next_part = parts[i + 1]
                try:
                    int(next_part)
                    current[part] = []
                except ValueError:
                    current[part] = {}
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
                if index < 0 or index >= len(current):
                    raise JsonPatchError(f"インデックスが範囲外: {part}")
                current = current[index]
            except ValueError:
                raise JsonPatchError(f"配列に対する無効なインデックス: {part}")
        else:
            path = "/" + "/".join(parts[:i])
            raise JsonPatchError(f"パス {path} はオブジェクトでも配列でもありません")
    
    # 最後の要素に値を設定
    last_part = parts[-1]
    
    if isinstance(current, dict):
        current[last_part] = value
    elif isinstance(current, list):
        if last_part == "-":
            if replace:
                raise JsonPatchError("replace 操作で '-' は使用できません")
            # 配列の末尾に追加
            current.append(value)
        else:
            try:
                index = int(last_part)
                if index < 0:
                    raise JsonPatchError(f"負のインデックスは許可されていません: {last_part}")
                if index > len(current):
                    raise JsonPatchError(f"インデックスが範囲外: {last_part}")
                if index == len(current):
                    if replace:
                        raise JsonPatchError(f"インデックスが範囲外: {last_part}")
                    current.append(value)
                elif replace:
                    current[index] = value
                else:
                    current.insert(index, value)
            except ValueError:
                raise JsonPatchError(f"配列に対する無効なインデックス: {last_part}")
    else:
        raise JsonPatchError("値を設定できません: 親がオブジェクトでも配列でもありません")
    
    return doc


def _remove_by_pointer(doc: Any, pointer: str) -> Any:
    """
    JSON Pointerで指定された値を削除
    
    Args:
        doc: 対象ドキュメント
        pointer: JSON Pointer
    
    Returns:
        更新されたドキュメント
    """
    if pointer == "":
        raise JsonPatchError("ルートドキュメントは削除できません")
    
    parts = _parse_pointer(pointer)
    current = doc
    
    # 最後の要素以外をたどる
    for i, part in enumerate(parts[:-1]):
        if isinstance(current, dict):
            if part not in current:
                path = "/" + "/".join(parts[:i+1])
                raise JsonPatchError(f"パスが存在しません: {path}")
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
                if index < 0 or index >= len(current):
                    raise JsonPatchError(f"インデックスが範囲外: {part}")
                current = current[index]
            except ValueError:
                raise JsonPatchError(f"配列に対する無効なインデックス: {part}")
        else:
            path = "/" + "/".join(parts[:i])
            raise JsonPatchError(f"パス {path} はオブジェクトでも配列でもありません")
    
    # 最後の要素を削除
    last_part = parts[-1]
    
    if isinstance(current, dict):
        if last_part not in current:
            raise JsonPatchError(f"パスが存在しません: {pointer}")
        del current[last_part]
    elif isinstance(current, list):
        try:
            index = int(last_part)
            if index < 0 or index >= len(current):
                raise JsonPatchError(f"インデックスが範囲外: {last_part}")
            current.pop(index)
        except ValueError:
            raise JsonPatchError(f"配列に対する無効なインデックス: {last_part}")
    else:
        raise JsonPatchError("削除できません: 親がオブジェクトでも配列でもありません")
    
    return doc


def _apply_single_operation(doc: Any, operation: Dict[str, Any]) -> Any:
    """
    単一のパッチ操作を適用
    
    Args:
        doc: 対象ドキュメント
        operation: パッチ操作
    
    Returns:
        更新されたドキュメント
    """
    op = operation.get("op")
    path = operation.get("path")
    
    if not op:
        raise JsonPatchError("操作に 'op' フィールドがありません")
    
    if path is None:
        raise JsonPatchError("操作に 'path' フィールドがありません")
    
    # 禁止された操作をチェック
    if op in ("move", "copy"):
        raise JsonPatchForbiddenError(
            f"操作 '{op}' はセキュリティ上の理由で禁止されています"
        )
    
    if op == "add":
        if "value" not in operation:
            raise JsonPatchError("'add' 操作に 'value' フィールドがありません")
        return _set_by_pointer(doc, path, operation["value"])
    
    elif op == "remove":
        return _remove_by_pointer(doc, path)
    
    elif op == "replace":
        if "value" not in operation:
            raise JsonPatchError("'replace' 操作に 'value' フィールドがありません")
        # replaceは既存の値がある場合のみ有効
        _get_by_pointer(doc, path)  # 存在確認
        return _set_by_pointer(doc, path, operation["value"], replace=True)
    
    elif op == "test":
        if "value" not in operation:
            raise JsonPatchError("'test' 操作に 'value' フィールドがありません")
        
        try:
            actual = _get_by_pointer(doc, path)
        except JsonPatchError:
            raise JsonPatchTestError(f"test失敗: パス '{path}' が存在しません")
        
        expected = operation["value"]
        
        if actual != expected:
            raise JsonPatchTestError(
                f"test失敗: パス '{path}' の値が一致しません。"
                f"期待値: {json.dumps(expected, ensure_ascii=False)}, "
                f"実際の値: {json.dumps(actual, ensure_ascii=False)}"
            )
        
        return doc
    
    else:
        raise JsonPatchError(f"未知の操作: '{op}'")


def apply_patch(
    doc: Any,
    patch: List[Dict[str, Any]],
    in_place: bool = False
) -> Any:
    """
    JSON Patchをドキュメントに適用
    
    Args:
        doc: 対象ドキュメント
        patch: パッチ操作のリスト
        in_place: Trueの場合、元のドキュメントを直接変更
    
    Returns:
        パッチ適用後のドキュメント
    
    Raises:
        JsonPatchError: パッチ操作に失敗した場合
        JsonPatchTestError: test操作が失敗した場合
        JsonPatchForbiddenError: 禁止された操作が含まれている場合
    
    Example:
        >>> doc = {"foo": {"bar": "baz"}}
        >>> patch = [
        ...     {"op": "replace", "path": "/foo/bar", "value": "qux"},
        ...     {"op": "add", "path": "/foo/new", "value": 123}
        ... ]
        >>> result = apply_patch(doc, patch)
        >>> result
        {"foo": {"bar": "qux", "new": 123}}
    """
    if not isinstance(patch, list):
        raise JsonPatchError("パッチは配列である必要があります")
    
    if not in_place:
        doc = copy.deepcopy(doc)
    
    for i, operation in enumerate(patch):
        try:
            doc = _apply_single_operation(doc, operation)
        except (JsonPatchTestError, JsonPatchForbiddenError):
            raise
        except JsonPatchError as e:
            raise JsonPatchError(f"操作 {i} でエラー: {e}")
    
    return doc


def validate_patch(patch: List[Dict[str, Any]]) -> List[str]:
    """
    パッチの構文を検証（適用せずに）
    
    Args:
        patch: パッチ操作のリスト
    
    Returns:
        エラーメッセージのリスト（空なら有効）
    """
    errors = []
    
    if not isinstance(patch, list):
        return ["パッチは配列である必要があります"]
    
    valid_ops = {"add", "remove", "replace", "test"}
    forbidden_ops = {"move", "copy"}
    
    for i, operation in enumerate(patch):
        if not isinstance(operation, dict):
            errors.append(f"操作 {i}: オブジェクトである必要があります")
            continue
        
        op = operation.get("op")
        path = operation.get("path")
        
        if not op:
            errors.append(f"操作 {i}: 'op' フィールドがありません")
        elif op in forbidden_ops:
            errors.append(f"操作 {i}: '{op}' は禁止されています")
        elif op not in valid_ops:
            errors.append(f"操作 {i}: 未知の操作 '{op}'")
        
        if path is None:
            errors.append(f"操作 {i}: 'path' フィールドがありません")
        elif not isinstance(path, str):
            errors.append(f"操作 {i}: 'path' は文字列である必要があります")
        elif path != "" and not path.startswith('/'):
            errors.append(f"操作 {i}: 'path' はスラッシュで始まる必要があります")
        
        if op in ("add", "replace", "test") and "value" not in operation:
            errors.append(f"操作 {i}: '{op}' 操作に 'value' フィールドがありません")
    
    return errors


def create_patch_operation(
    op: str,
    path: str,
    value: Any = None
) -> Dict[str, Any]:
    """
    パッチ操作を作成するヘルパー関数
    
    Args:
        op: 操作タイプ（"add", "remove", "replace", "test"）
        path: JSON Pointer
        value: 値（add, replace, testの場合に必要）
    
    Returns:
        パッチ操作の辞書
    """
    if op in ("move", "copy"):
        raise JsonPatchForbiddenError(f"操作 '{op}' は禁止されています")
    
    operation = {"op": op, "path": path}
    
    if op in ("add", "replace", "test"):
        operation["value"] = value
    
    return operation
