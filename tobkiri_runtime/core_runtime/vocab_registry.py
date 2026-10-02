"""
vocab_registry.py - 語彙統一システム (DI Container 対応)

双方向の同義語解決とデータ変換機能を提供する。

設計原則:
- 公式は「仕組み」のみ提供、具体的な語彙はecosystemが定義
- グループ内の全ての語は同義（双方向解決）
- 変換スクリプトでデータ形式の変換も可能

vocab.txt形式:
    # コメント
    tool, function_calling, tools, tooluse, tool_use
    thinking_budget, reasoning_effort, reasoning_budget

    グループ内の最初の語が「優先語（preferred）」となる。

変換スクリプト:
    converters/{from}_to_{to}.py に convert(data) 関数を定義。
    例: converters/tool_to_function_calling.py

Usage:
    vr = get_vocab_registry()
    
    # 同義語解決
    vr.resolve("function_calling")  # → "tool"（優先語）
    vr.resolve("tool")              # → "tool"（自身が優先語）
    vr.resolve("unknown")           # → "unknown"（未登録はそのまま）
    
    # グループ取得
    vr.get_group("tool")  # → ["tool", "function_calling", "tools", ...]
    
    # 同義判定
    vr.is_synonym("tool", "function_calling")  # → True
    
    # データ変換
    result = vr.convert("tool", "function_calling", {"tools": [...]})
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


MAX_NORMALIZE_DEPTH = 5

VOCAB_FILENAME = "vocab.txt"
CONVERTERS_DIRNAME = "converters"

logger = logging.getLogger(__name__)


# ===================================================================
# C-2-impl: CollisionStrategy
# ===================================================================

class CollisionStrategy(Enum):
    """normalize_dict_keys() での衝突解決戦略"""
    KEEP_FIRST = "keep_first"     # 先勝ち（新デフォルト）
    KEEP_LAST = "keep_last"       # 後勝ち（旧動作）
    RAISE = "raise"               # 例外送出
    MERGE_LIST = "merge_list"     # リストにマージ
    WARN = "warn"                 # 警告ログ + keep_first


DEFAULT_COLLISION_STRATEGY = CollisionStrategy.WARN


class VocabKeyCollisionError(Exception):
    """CollisionStrategy.RAISE 時に送出される例外"""
    def __init__(self, key: str, existing_value: Any, new_value: Any):
        self.key = key
        self.existing_value = existing_value
        self.new_value = new_value
        super().__init__(
            f"Vocab key collision on '{key}': "
            f"existing={existing_value!r}, new={new_value!r}"
        )


# ===================================================================
# C-3-L1: ConverterPolicy
# ===================================================================

@dataclass
class ConverterPolicy:
    """converter のロードポリシー"""
    allow_external: bool = False
    require_trusted: bool = True
    max_file_size_bytes: int = 100_000
    blocked_imports: Set[str] = field(default_factory=lambda: {
        "subprocess", "os.system", "shutil.rmtree",
        "socket", "http", "urllib", "ctypes",
    })


# ===================================================================
# C-3-L3: ConverterASTChecker
# ===================================================================

class ConverterASTChecker:
    """AST レベルのセキュリティ検査"""

    _DANGEROUS_CALLS: Set[str] = {"exec", "eval", "compile", "__import__"}

    def check(
        self, source_code: str, blocked_imports: Set[str]
    ) -> Tuple[bool, List[str]]:
        """
        AST 検査。blocked_imports に該当する import があれば拒否。

        Returns:
            (is_safe, warnings)
        """
        warn_list: List[str] = []

        try:
            tree = ast.parse(source_code)
        except SyntaxError as e:
            return False, [f"SyntaxError: {e}"]

        for node in ast.walk(tree):
            # --- Import 検査 ---
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if self._is_blocked(alias.name, blocked_imports):
                        warn_list.append(
                            f"Blocked import: '{alias.name}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    if self._is_blocked(full, blocked_imports) or self._is_blocked(module, blocked_imports):
                        warn_list.append(
                            f"Blocked import: 'from {module} import {alias.name}'"
                        )

            # --- 危険な関数呼び出し検査 ---
            if isinstance(node, ast.Call):
                func_name = self._extract_call_name(node)
                if func_name in self._DANGEROUS_CALLS:
                    warn_list.append(
                        f"Dangerous call: '{func_name}()'"
                    )

        is_safe = len(warn_list) == 0
        return is_safe, warn_list

    @staticmethod
    def _is_blocked(name: str, blocked: Set[str]) -> bool:
        """name が blocked のいずれかに前方一致するか判定"""
        for b in blocked:
            if name == b or name.startswith(b + "."):
                return True
        return False

    @staticmethod
    def _extract_call_name(node: ast.Call) -> str:
        """Call ノードから関数名を抽出（簡易版）"""
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

    @staticmethod
    def _extract_module_names(node: ast.AST) -> List[str]:
        """Import / ImportFrom ノードからモジュール名を抽出する。

        Returns:
            モジュール名のリスト。ImportFrom の場合は module 自体と
            module.alias の両方を含む。
        """
        names: List[str] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                names.append(module)
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                names.append(full)
        return names

    def check_with_locals(
        self, converter_path: Path, blocked_imports: Set[str]
    ) -> Tuple[bool, List[str]]:
        """
        Level 1 AST 検査: converter + 同一ディレクトリの .py を再帰走査。

        converter がローカルモジュールを import している場合、
        そのモジュールも再帰的に検査し、blocked import の迂回を防止する。

        同一ディレクトリ外のファイルは検査しない（Level 1 の仕様）。
        visited set により循環 import を安全に処理する。

        Args:
            converter_path: converter ファイルのパス
            blocked_imports: ブロックする import 名の集合

        Returns:
            (is_safe, violations)
        """
        violations: List[str] = []
        converter_dir = converter_path.parent.resolve()
        visited: Set[Path] = set()

        def _check(target: Path) -> None:
            resolved = target.resolve()
            if resolved in visited:
                return
            visited.add(resolved)

            try:
                source = target.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                return

            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue

                # converter 外への相対 import (level >= 2) はスキップ
                if isinstance(node, ast.ImportFrom) and (node.level or 0) >= 2:
                    continue

                for name in self._extract_module_names(node):
                    if self._is_blocked(name, blocked_imports):
                        violations.append(
                            f"{target.name}: blocked import '{name}'"
                        )

                    # ローカルファイルへの再帰走査
                    top = name.split(".")[0]
                    if top:
                        local = converter_dir / f"{top}.py"
                        if local.exists() and local.resolve() != resolved:
                            _check(local)

        _check(converter_path)

        is_safe = len(violations) == 0
        return is_safe, violations


# ===================================================================
# C-3-L2: ConverterIntegrityChecker
# ===================================================================

class ConverterIntegrityChecker:
    """converter ファイルの整合性検証"""

    def __init__(self, policy: ConverterPolicy | None = None):
        self._policy = policy or ConverterPolicy()
        self._ast_checker = ConverterASTChecker()

    def check_file(self, file_path: Path) -> Tuple[bool, List[str]]:
        """
        converter ファイルを検証する。

        Returns:
            (is_safe, warnings)
        """
        warn_list: List[str] = []

        # 1. ファイル存在チェック
        if not file_path.exists():
            return False, ["File does not exist"]

        # 2. ファイルサイズチェック
        size = file_path.stat().st_size
        if size > self._policy.max_file_size_bytes:
            return False, [
                f"File too large: {size} bytes "
                f"(max {self._policy.max_file_size_bytes})"
            ]

        # 3. ソース読み込み
        try:
            source = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return False, [f"Cannot read file: {e}"]

        # 4. AST パース
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return False, [f"SyntaxError: {e}"]

        # 5. convert() 関数定義チェック
        has_convert = any(
            isinstance(node, ast.FunctionDef) and node.name == "convert"
            for node in ast.walk(tree)
        )
        if not has_convert:
            warn_list.append("No 'convert()' function defined")

        # 6. blocked imports / 危険呼び出し (AST Checker に委譲)
        ast_safe, ast_warnings = self._ast_checker.check(
            source, self._policy.blocked_imports
        )
        warn_list.extend(ast_warnings)

        # 7. Level 1: ローカル依存の再帰検査
        local_safe, local_warnings = self._ast_checker.check_with_locals(
            file_path, self._policy.blocked_imports
        )
        # 重複を除外してマージ
        existing = set(warn_list)
        for w in local_warnings:
            if w not in existing:
                warn_list.append(w)
                existing.add(w)

        is_safe = len(warn_list) == 0
        return is_safe, warn_list


# ===================================================================
# データクラス (既存)
# ===================================================================

@dataclass
class VocabGroup:
    """同義語グループ"""
    preferred: str
    members: Set[str]
    source_pack: Optional[str] = None


@dataclass
class ConverterInfo:
    """変換スクリプト情報"""
    from_term: str
    to_term: str
    file_path: Path
    source_pack: Optional[str] = None
    _cached_fn: Optional[Callable] = field(default=None, repr=False)


class VocabRegistry:
    """
    語彙統一レジストリ
    
    双方向の同義語解決とデータ変換を提供する。
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._term_to_group: Dict[str, str] = {}
        self._groups: Dict[str, VocabGroup] = {}
        self._converters: Dict[Tuple[str, str], ConverterInfo] = {}
        self._loaded_packs: Set[str] = set()
        self._group_counter: int = 0
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def register_group(
        self,
        terms: List[str],
        source_pack: Optional[str] = None
    ) -> str:
        """同義語グループを登録"""
        if not terms:
            return ""
        
        terms = [t.strip().lower() for t in terms if t.strip()]
        if not terms:
            return ""
        
        with self._lock:
            preferred = terms[0]
            
            existing_group_ids = set()
            for term in terms:
                if term in self._term_to_group:
                    existing_group_ids.add(self._term_to_group[term])
            
            if existing_group_ids:
                target_group_id = sorted(existing_group_ids, key=lambda x: int(x[1:]))[0]
                target_group = self._groups[target_group_id]
                
                for gid in existing_group_ids:
                    if gid != target_group_id:
                        old_group = self._groups[gid]
                        target_group.members.update(old_group.members)
                        for member in old_group.members:
                            self._term_to_group[member] = target_group_id
                        del self._groups[gid]
                
                for term in terms:
                    target_group.members.add(term)
                    self._term_to_group[term] = target_group_id
                
                return target_group_id
            
            else:
                self._group_counter += 1
                group_id = f"g{self._group_counter}"
                
                group = VocabGroup(
                    preferred=preferred,
                    members=set(terms),
                    source_pack=source_pack
                )
                
                self._groups[group_id] = group
                
                for term in terms:
                    self._term_to_group[term] = group_id
                
                return group_id
    
    def register_synonym(
        self,
        term1: str,
        term2: str,
        source_pack: Optional[str] = None
    ) -> str:
        """2つの語を同義として登録"""
        return self.register_group([term1, term2], source_pack)
    
    def resolve(self, term: str, to_preferred: bool = True) -> str:
        """語を解決"""
        term_lower = term.strip().lower()
        
        with self._lock:
            group_id = self._term_to_group.get(term_lower)
            if group_id is None:
                return term
            
            if to_preferred:
                return self._groups[group_id].preferred
            return term
    
    def resolve_to(self, term: str, target: str) -> str:
        """語を特定のターゲット語に解決"""
        term_lower = term.strip().lower()
        target_lower = target.strip().lower()
        
        with self._lock:
            group_id = self._term_to_group.get(term_lower)
            if group_id is None:
                return term
            
            group = self._groups[group_id]
            if target_lower in group.members:
                return target
            return term
    
    def get_group(self, term: str) -> List[str]:
        """語が属するグループの全メンバーを取得"""
        term_lower = term.strip().lower()
        
        with self._lock:
            group_id = self._term_to_group.get(term_lower)
            if group_id is None:
                return [term]
            
            group = self._groups[group_id]
            members = sorted(group.members)
            if group.preferred in members:
                members.remove(group.preferred)
            return [group.preferred] + members
    
    def is_synonym(self, term1: str, term2: str) -> bool:
        """2つの語が同義かどうか判定"""
        t1 = term1.strip().lower()
        t2 = term2.strip().lower()
        
        if t1 == t2:
            return True
        
        with self._lock:
            g1 = self._term_to_group.get(t1)
            g2 = self._term_to_group.get(t2)
            
            if g1 is None or g2 is None:
                return False
            
            return g1 == g2
    
    def get_preferred(self, term: str) -> str:
        """語の優先語を取得"""
        return self.resolve(term, to_preferred=True)
    
    def register_converter(
        self,
        from_term: str,
        to_term: str,
        file_path: Path,
        source_pack: Optional[str] = None,
        policy: Optional[ConverterPolicy] = None,
    ) -> bool:
        """変換スクリプトを登録"""
        if not file_path.exists():
            return False
        
        # --- policy チェック (C-3-L1) ---
        if policy is not None:
            checker = ConverterIntegrityChecker(policy)
            is_safe, warnings_list = checker.check_file(file_path)
            if not is_safe:
                for w in warnings_list:
                    logger.warning(
                        "Converter rejected (%s -> %s): %s",
                        from_term, to_term, w,
                    )
                self._log_converter_policy_rejection(
                    from_term, to_term, file_path, warnings_list
                )
                return False
            elif warnings_list:
                for w in warnings_list:
                    logger.warning(
                        "Converter warning (%s -> %s): %s",
                        from_term, to_term, w,
                    )

            # require_trusted チェック（将来拡張ポイント）
            if policy.require_trusted:
                logger.info(
                    "Converter trust check deferred (%s -> %s): "
                    "trust store integration pending",
                    from_term, to_term,
                )
        
        from_lower = from_term.strip().lower()
        to_lower = to_term.strip().lower()
        
        with self._lock:
            key = (from_lower, to_lower)
            self._converters[key] = ConverterInfo(
                from_term=from_lower,
                to_term=to_lower,
                file_path=file_path,
                source_pack=source_pack
            )
        
        return True
    
    def _log_converter_policy_rejection(
        self,
        from_term: str,
        to_term: str,
        file_path: Path,
        warnings_list: List[str],
    ) -> None:
        """converter ポリシー拒否を監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_security_event(
                event_type="converter_policy_rejection",
                severity="warning",
                description=f"Converter rejected: {from_term} -> {to_term}",
                details={
                    "from_term": from_term,
                    "to_term": to_term,
                    "file_path": str(file_path),
                    "warnings": warnings_list,
                },
            )
        except Exception:
            pass
    
    def has_converter(self, from_term: str, to_term: str) -> bool:
        """変換スクリプトが存在するか確認"""
        from_lower = from_term.strip().lower()
        to_lower = to_term.strip().lower()
        
        with self._lock:
            return (from_lower, to_lower) in self._converters
    
    def convert(
        self,
        from_term: str,
        to_term: str,
        data: Any,
        context: Dict[str, Any] | None = None,
        log_success: bool = False
    ) -> Tuple[Any, bool]:
        """
        データを変換
        
        Args:
            from_term: 変換元の語
            to_term: 変換先の語
            data: 変換するデータ
            context: 変換コンテキスト
            log_success: 成功時も監査ログに記録するか（デフォルトはFalse、ログ過多防止）
        
        Returns:
            (変換後のデータ, 成功したか)
        """
        from_lower = from_term.strip().lower()
        to_lower = to_term.strip().lower()
        
        with self._lock:
            key = (from_lower, to_lower)
            converter_info = self._converters.get(key)
        
        if converter_info is None:
            return data, False
        
        convert_fn = self._get_converter_function(converter_info)
        if convert_fn is None:
            self._log_conversion(
                from_term=from_term,
                to_term=to_term,
                success=False,
                error="Failed to load converter function",
                converter_info=converter_info
            )
            return data, False
        
        try:
            if context is not None:
                result = convert_fn(data, context)
            else:
                result = convert_fn(data)
            
            if log_success:
                self._log_conversion(
                    from_term=from_term,
                    to_term=to_term,
                    success=True,
                    converter_info=converter_info
                )
            
            return result, True
        except Exception as e:
            self._log_conversion(
                from_term=from_term,
                to_term=to_term,
                success=False,
                error=str(e),
                error_type=type(e).__name__,
                converter_info=converter_info
            )
            print(f"[VocabRegistry] Converter error ({from_term} -> {to_term}): {e}")
            return data, False
    
    def _log_conversion(
        self,
        from_term: str,
        to_term: str,
        success: bool,
        error: str | None = None,
        error_type: str | None = None,
        converter_info: ConverterInfo | None = None
    ) -> None:
        """変換の監査ログを記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            
            details: Dict[str, str | None] = {
                "from_term": from_term,
                "to_term": to_term,
            }
            
            if converter_info:
                details["converter_file"] = str(converter_info.file_path)
                details["source_pack"] = converter_info.source_pack
            
            if error:
                details["error"] = error
            if error_type:
                details["error_type"] = error_type
            
            audit.log_system_event(
                event_type="vocab_conversion",
                success=success,
                details=details
            )
        except Exception:
            pass
    
    def _get_converter_function(self, info: ConverterInfo) -> Optional[Callable]:
        """変換関数を取得"""
        if info._cached_fn is not None:
            return info._cached_fn
        
        try:
            module_name = f"vocab_converter_{info.from_term}_{info.to_term}_{abs(hash(str(info.file_path)))}"
            spec = importlib.util.spec_from_file_location(module_name, str(info.file_path))
            
            if spec is None or spec.loader is None:
                return None
            
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            
            fn = getattr(module, "convert", None)
            if fn is None or not callable(fn):
                return None
            
            info._cached_fn = fn
            return fn
        
        except Exception as e:
            print(f"[VocabRegistry] Failed to load converter: {info.file_path}: {e}")
            return None
    
    def load_vocab_file(
        self,
        file_path: Path,
        source_pack: Optional[str] = None
    ) -> int:
        """vocab.txtファイルを読み込み"""
        if not file_path.exists():
            return 0
        
        count = 0
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    
                    if not line or line.startswith('#'):
                        continue
                    
                    if ',' in line:
                        terms = [t.strip() for t in line.split(',')]
                    elif '=' in line:
                        terms = [t.strip() for t in line.split('=')]
                    else:
                        continue
                    
                    terms = [t for t in terms if t]
                    if len(terms) >= 2:
                        self.register_group(terms, source_pack)
                        count += 1
        
        except Exception as e:
            print(f"[VocabRegistry] Failed to load vocab file: {file_path}: {e}")
        
        return count
    
    def load_converters_dir(
        self,
        dir_path: Path,
        source_pack: Optional[str] = None
    ) -> int:
        """convertersディレクトリから変換スクリプトを読み込み"""
        if not dir_path.exists() or not dir_path.is_dir():
            return 0
        
        count = 0
        
        for py_file in dir_path.glob("*.py"):
            name = py_file.stem
            
            if "_to_" not in name:
                continue
            
            parts = name.split("_to_", 1)
            if len(parts) != 2:
                continue
            
            from_term, to_term = parts
            
            if self.register_converter(from_term, to_term, py_file, source_pack):
                count += 1
        
        return count
    
    def load_pack_vocab(
        self,
        pack_subdir: Path,
        pack_id: str
    ) -> Dict[str, int]:
        """Packから語彙と変換スクリプトを読み込み"""
        if pack_id in self._loaded_packs:
            return {"groups": 0, "converters": 0}
        
        vocab_file = pack_subdir / VOCAB_FILENAME
        converters_dir = pack_subdir / CONVERTERS_DIRNAME
        
        groups = self.load_vocab_file(vocab_file, pack_id)
        converters = self.load_converters_dir(converters_dir, pack_id)
        
        self._loaded_packs.add(pack_id)
        
        return {"groups": groups, "converters": converters}
    
    def list_groups(self) -> List[Dict[str, Any]]:
        """全グループを取得"""
        with self._lock:
            return [
                {
                    "id": gid,
                    "preferred": g.preferred,
                    "members": sorted(g.members),
                    "source_pack": g.source_pack
                }
                for gid, g in self._groups.items()
            ]
    
    def normalize_dict_keys(
        self,
        data,
        max_depth: int = MAX_NORMALIZE_DEPTH,
        _current_depth: int = 0,
        collision_strategy: CollisionStrategy | None = None,
        on_collision: Callable[[object, object, object], object] | None = None,
    ):
        """
        dict のキーを優先語（preferred）に正規化する。

        Flow ctx 格納時に呼ばれる。トップレベルおよびネストされた dict の
        キーを vocab グループの preferred term に変換する。

        - ``_`` プレフィックス付きキーは正規化しない（内部制御用）
        - list 内の dict も再帰的に処理する
        - 深さ制限付き（デフォルト MAX_NORMALIZE_DEPTH=5）
        - collision_strategy=None の場合は DEFAULT_COLLISION_STRATEGY (WARN)
        - on_collision が指定された場合はカスタムコールバック:
          on_collision(key, existing_value, new_value) -> value

        Returns:
            (normalized_data, changes) — changes は
            ``[(original_key, preferred_key), ...]`` のリスト。
            変換が発生しなかった場合は空リスト。
        """
        if not isinstance(data, dict) or _current_depth > max_depth:
            return data, []

        strategy = collision_strategy or DEFAULT_COLLISION_STRATEGY
        changes: list[tuple[object, object]] = []
        normalized: dict[object, object] = {}

        with self._lock:
            for key, value in data.items():
                # 内部制御キー（_xxx）はスキップ
                if isinstance(key, str) and key.startswith("_"):
                    new_key = key
                else:
                    new_key = self._resolve_key_unlocked(key)
                    if new_key != key:
                        changes.append((key, new_key))

                # ネストされた dict / list 内の dict も再帰処理
                if isinstance(value, dict) and _current_depth < max_depth:
                    value, sub_changes = self.normalize_dict_keys(
                        value, max_depth, _current_depth + 1,
                        collision_strategy=strategy,
                        on_collision=on_collision,
                    )
                    changes.extend(sub_changes)
                elif isinstance(value, list) and _current_depth < max_depth:
                    new_list = []
                    for item in value:
                        if isinstance(item, dict):
                            item, sub_changes = self.normalize_dict_keys(
                                item, max_depth, _current_depth + 1,
                                collision_strategy=strategy,
                                on_collision=on_collision,
                            )
                            changes.extend(sub_changes)
                        new_list.append(item)
                    value = new_list

                # --- 衝突解決 ---
                if new_key in normalized:
                    existing_value = normalized[new_key]
                    # 後方互換: COLLISION ログエントリは常に追加
                    changes.append((f"COLLISION:{key}", new_key))

                    # 監査ログ記録
                    self._log_collision(new_key, existing_value, value, strategy)

                    if on_collision is not None:
                        # カスタムコールバック優先
                        normalized[new_key] = on_collision(
                            new_key, existing_value, value
                        )
                    elif strategy == CollisionStrategy.KEEP_FIRST:
                        pass  # 先勝ち: 既存値を維持
                    elif strategy == CollisionStrategy.KEEP_LAST:
                        normalized[new_key] = value
                    elif strategy == CollisionStrategy.RAISE:
                        raise VocabKeyCollisionError(
                            new_key, existing_value, value
                        )
                    elif strategy == CollisionStrategy.MERGE_LIST:
                        if isinstance(existing_value, list):
                            existing_value.append(value)
                        else:
                            normalized[new_key] = [existing_value, value]
                    elif strategy == CollisionStrategy.WARN:
                        logger.warning(
                            "Vocab key collision on '%s': "
                            "keeping first value, discarding new",
                            new_key,
                        )
                        # WARN = 警告 + keep_first
                    else:
                        pass  # fallback: keep_first
                else:
                    normalized[new_key] = value

        return normalized, changes

    def _resolve_key_unlocked(self, key: str) -> str:
        """ロック取得済みの状態でキーを解決する（内部用）"""
        if not isinstance(key, str):
            return key
        key_lower = key.strip().lower()
        group_id = self._term_to_group.get(key_lower)
        if group_id is None:
            return key
        return self._groups[group_id].preferred

    def _log_collision(
        self,
        key: str,
        existing_value: Any,
        new_value: Any,
        strategy: CollisionStrategy,
    ) -> None:
        """衝突イベントを監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="vocab_key_collision",
                success=True,
                details={
                    "key": key,
                    "strategy": strategy.value,
                    "existing_value_type": type(existing_value).__name__,
                    "new_value_type": type(new_value).__name__,
                },
            )
        except Exception:
            pass

    def list_converters(self) -> List[Dict[str, Any]]:
        """全変換スクリプトを取得"""
        with self._lock:
            return [
                {
                    "from": c.from_term,
                    "to": c.to_term,
                    "file": str(c.file_path),
                    "source_pack": c.source_pack
                }
                for c in self._converters.values()
            ]
    
    def get_registration_summary(self) -> Dict[str, Any]:
        """
        登録状況のサマリーを取得（どのpackがどの語彙を登録したか）
        
        Returns:
            {
                "groups": {pack_id: [groups...]},
                "converters": {pack_id: [converters...]},
                "loaded_packs": [pack_ids...],
                "totals": {"groups": n, "converters": m}
            }
        """
        with self._lock:
            groups_by_pack: Dict[str, List[Dict[str, Any]]] = {}
            for gid, group in self._groups.items():
                pack_id = group.source_pack or "_unknown"
                if pack_id not in groups_by_pack:
                    groups_by_pack[pack_id] = []
                groups_by_pack[pack_id].append({
                    "id": gid,
                    "preferred": group.preferred,
                    "members": sorted(group.members),
                })
            
            converters_by_pack: Dict[str, List[Dict[str, Any]]] = {}
            for converter in self._converters.values():
                pack_id = converter.source_pack or "_unknown"
                if pack_id not in converters_by_pack:
                    converters_by_pack[pack_id] = []
                converters_by_pack[pack_id].append({
                    "from": converter.from_term,
                    "to": converter.to_term,
                    "file": str(converter.file_path),
                })
            
            return {
                "groups_by_pack": groups_by_pack,
                "converters_by_pack": converters_by_pack,
                "loaded_packs": sorted(self._loaded_packs),
                "totals": {
                    "groups": len(self._groups),
                    "converters": len(self._converters),
                    "packs": len(self._loaded_packs),
                }
            }
    
    def clear(self) -> None:
        """全データをクリア"""
        with self._lock:
            self._term_to_group.clear()
            self._groups.clear()
            self._converters.clear()
            self._loaded_packs.clear()
            self._group_counter = 0


def get_vocab_registry() -> VocabRegistry:
    """
    グローバルな VocabRegistry を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        VocabRegistry インスタンス
    """
    from .di_container import get_container
    return get_container().get("vocab_registry")


def reset_vocab_registry() -> VocabRegistry:
    """
    VocabRegistry をリセットする（テスト用）。

    新しい空の VocabRegistry を生成し、DI コンテナに設定する。

    Returns:
        新しい VocabRegistry インスタンス
    """
    from .di_container import get_container
    new_instance = VocabRegistry()
    get_container().set_instance("vocab_registry", new_instance)
    return new_instance
