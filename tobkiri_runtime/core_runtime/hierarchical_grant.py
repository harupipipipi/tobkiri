"""
hierarchical_grant.py - 階層 Principal の親チェーン解決 + Grant 判定ヘルパー

principal_id が parent__child__grandchild のように階層を表す場合、
下位だけ権限を持っていても動かない。上位も許可している必要がある。
config は親で上限（intersection）。

区切り文字: __ (double underscore)

例:
- "company__team__member" の場合:
  1. "company" が grant されていること
  2. "company__team" が grant されていること
  3. "company__team__member" が grant されていること
  -> すべて OK なら許可。config は全階層の intersection。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


HIERARCHY_SEPARATOR = "__"


def parse_principal_chain(principal_id: str) -> List[str]:
    """
    principal_id を階層チェーンに展開する。

    例:
        "a__b__c" -> ["a", "a__b", "a__b__c"]
        "single" -> ["single"]
    """
    parts = principal_id.split(HIERARCHY_SEPARATOR)
    return [HIERARCHY_SEPARATOR.join(parts[:i]) for i in range(1, len(parts) + 1)]


def is_hierarchical(principal_id: str) -> bool:
    return HIERARCHY_SEPARATOR in principal_id


def get_parent(principal_id: str) -> Optional[str]:
    if HIERARCHY_SEPARATOR not in principal_id:
        return None
    parts = principal_id.split(HIERARCHY_SEPARATOR)
    return HIERARCHY_SEPARATOR.join(parts[:-1])


def get_root(principal_id: str) -> str:
    return principal_id.split(HIERARCHY_SEPARATOR)[0]


def intersect_config(configs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    config の intersection を計算する。

    ルール:
    - list 値: 共通部分（intersection）
    - bool 値: AND
    - int/float 値: min
    - str 値: 上位（最初）を採用
    - dict 値: 再帰的に intersection
    - いずれかに無いキーは除外
    """
    if not configs:
        return {}
    if len(configs) == 1:
        return dict(configs[0])
    result = dict(configs[0])
    for other in configs[1:]:
        result = _intersect_two(result, other)
    return result


def _intersect_two(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in set(a.keys()) & set(b.keys()):
        va, vb = a[key], b[key]
        if isinstance(va, list) and isinstance(vb, list):
            set_b = set(
                item for item in vb if isinstance(item, (str, int, float, bool))
            )
            result[key] = [
                item
                for item in va
                if isinstance(item, (str, int, float, bool)) and item in set_b
            ]
        elif isinstance(va, bool) and isinstance(vb, bool):
            result[key] = va and vb
        elif isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            result[key] = min(va, vb)
        elif isinstance(va, str) and isinstance(vb, str):
            result[key] = va
        elif isinstance(va, dict) and isinstance(vb, dict):
            result[key] = _intersect_two(va, vb)
    return result
