"""
flow_composer.py - Flow合成・修正システム

ecosystemコンポーネントがFlowを動的に修正するための基盤。

設計原則:
- 公式は修正の「仕組み」のみ提供
- 具体的な修正ロジックはecosystem側で定義
- 安全性を考慮（不正な修正を検出）

Usage:
    composer = get_flow_composer()
    
    # modifierを収集
    modifiers = composer.collect_modifiers(interface_registry)
    
    # Flowに修正を適用
    modified_flow = composer.apply_modifiers(flow_def, modifiers)
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from .function_alias import FunctionAliasRegistry, get_function_alias_registry


@dataclass
class FlowModifier:
    """Flow修正の定義"""
    id: str
    priority: int
    target_flow: Optional[str]  # 対象Flow名（Noneなら全Flow）
    requires: Dict[str, Any]    # 適用条件
    modifications: List[Dict[str, Any]]  # 修正操作のリスト
    source_component: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "priority": self.priority,
            "target_flow": self.target_flow,
            "requires": self.requires,
            "modifications": self.modifications,
            "source_component": self.source_component
        }


class FlowComposer:
    """
    Flow合成・修正システム
    
    ecosystemコンポーネントが登録したflow.modifierを収集し、
    Flow定義に適用する。
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._applied_modifiers: List[Dict[str, Any]] = []
        self._alias_registry: Optional[FunctionAliasRegistry] = None
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def set_alias_registry(self, registry: FunctionAliasRegistry) -> None:
        """エイリアスレジストリを設定"""
        self._alias_registry = registry
    
    def collect_modifiers(self, interface_registry) -> List[FlowModifier]:
        """
        InterfaceRegistryからflow.modifierを収集
        
        Args:
            interface_registry: InterfaceRegistry インスタンス
        
        Returns:
            優先度順にソートされたFlowModifierのリスト
        """
        raw_modifiers = interface_registry.get("flow.modifier", strategy="all") or []
        
        modifiers: List[FlowModifier] = []
        for raw in raw_modifiers:
            if not isinstance(raw, dict):
                continue
            
            try:
                modifier = FlowModifier(
                    id=raw.get("id", f"modifier_{len(modifiers)}"),
                    priority=raw.get("priority", 100),
                    target_flow=raw.get("target_flow"),
                    requires=raw.get("requires", {}),
                    modifications=raw.get("modifications", []),
                    source_component=raw.get("source_component")
                )
                modifiers.append(modifier)
            except Exception:
                continue
        
        # 優先度でソート（小さい方が先）
        modifiers.sort(key=lambda m: m.priority)
        return modifiers
    
    def check_requirements(
        self,
        modifier: FlowModifier,
        interface_registry,
        available_capabilities: Dict[str, Any] | None = None
    ) -> bool:
        """
        修正の適用条件をチェック
        
        Args:
            modifier: チェックするmodifier
            interface_registry: InterfaceRegistry インスタンス
            available_capabilities: 利用可能なcapabilitiesの辞書
        
        Returns:
            条件を満たす場合True
        """
        requires = modifier.requires
        
        if not requires:
            return True
        
        # capabilities チェック
        required_caps = requires.get("capabilities", [])
        if required_caps:
            if available_capabilities is None:
                return False
            for cap in required_caps:
                if not available_capabilities.get(cap):
                    return False
        
        # modifiers チェック（他のmodifierが適用済みであること）
        required_mods = requires.get("modifiers", [])
        if required_mods:
            applied_ids = {m.get("id") for m in self._applied_modifiers}
            for mod_id in required_mods:
                if mod_id not in applied_ids:
                    return False
        
        # interfaces チェック（特定のIRキーが登録されていること）
        required_interfaces = requires.get("interfaces", [])
        if required_interfaces:
            for iface in required_interfaces:
                if interface_registry.get(iface) is None:
                    return False
        
        return True
    
    def apply_modifiers(
        self,
        flow_def: Dict[str, Any],
        modifiers: List[FlowModifier],
        interface_registry = None,
        available_capabilities: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """
        Flow定義に修正を適用
        
        Args:
            flow_def: 元のFlow定義
            modifiers: 適用するmodifierのリスト
            interface_registry: InterfaceRegistry インスタンス（条件チェック用）
            available_capabilities: 利用可能なcapabilities
        
        Returns:
            修正後のFlow定義（新しい辞書、元は変更しない）
        """
        result = copy.deepcopy(flow_def)
        
        with self._lock:
            self._applied_modifiers.clear()
            
            for modifier in modifiers:
                # 条件チェック
                if interface_registry and not self.check_requirements(
                    modifier, interface_registry, available_capabilities
                ):
                    continue
                
                # 修正を適用
                try:
                    result = self._apply_single_modifier(result, modifier)
                    self._applied_modifiers.append({
                        "id": modifier.id,
                        "applied_at": self._now_ts(),
                        "source_component": modifier.source_component
                    })
                except Exception as e:
                    # 適用失敗は記録して継続
                    print(f"[FlowComposer] Modifier '{modifier.id}' failed: {e}")
                    continue
        
        return result
    
    def _apply_single_modifier(
        self,
        flow_def: Dict[str, Any],
        modifier: FlowModifier
    ) -> Dict[str, Any]:
        """
        単一の修正を適用
        
        サポートする操作:
        - inject_before: 指定ステップの前にステップを挿入
        - inject_after: 指定ステップの後にステップを挿入
        - replace: 指定ステップを置換
        - wrap_with_loop: 指定ステップ群をループで囲む
        - remove: 指定ステップを削除
        - set_property: ステップのプロパティを設定
        """
        for modification in modifier.modifications:
            action = modification.get("action")
            
            if action == "inject_before":
                flow_def = self._action_inject(
                    flow_def, modification, "before"
                )
            elif action == "inject_after":
                flow_def = self._action_inject(
                    flow_def, modification, "after"
                )
            elif action == "replace":
                flow_def = self._action_replace(flow_def, modification)
            elif action == "wrap_with_loop":
                flow_def = self._action_wrap_loop(flow_def, modification)
            elif action == "remove":
                flow_def = self._action_remove(flow_def, modification)
            elif action == "set_property":
                flow_def = self._action_set_property(flow_def, modification)
            # 未知の操作は無視
        
        return flow_def
    
    def _find_step_index(
        self,
        steps: List[Dict[str, Any]],
        target: Dict[str, Any]
    ) -> int:
        """
        ターゲットに一致するステップのインデックスを検索
        
        target形式:
        - {"id": "step_id"}: IDで検索
        - {"function": "ai"}: 関数名（エイリアス解決あり）で検索
        - {"handler": "ai.generate"}: ハンドラ名で検索
        """
        alias_registry = self._alias_registry or get_function_alias_registry()
        
        for i, step in enumerate(steps):
            # ID検索
            if "id" in target:
                if step.get("id") == target["id"]:
                    return i
            
            # 関数名検索（エイリアス解決）
            if "function" in target:
                target_function = target["function"]
                target_aliases = alias_registry.find_all(target_function)
                
                step_handler = step.get("handler", "")
                step_function = step_handler.split(".")[0] if step_handler else ""
                step_type = step.get("type", "")
                
                # runブロック内のhandlerもチェック
                run_block = step.get("run", {})
                if isinstance(run_block, dict):
                    run_handler = run_block.get("handler", "")
                    run_function = run_handler.split(".")[0] if run_handler else ""
                    if run_function in target_aliases:
                        return i
                
                # ハンドラの先頭部分またはtypeがエイリアスに一致するか
                if step_function in target_aliases or step_type in target_aliases:
                    return i
            
            # ハンドラ名検索
            if "handler" in target:
                if step.get("handler") == target["handler"]:
                    return i
                # runブロック内もチェック
                run_block = step.get("run", {})
                if isinstance(run_block, dict):
                    if run_block.get("handler") == target["handler"]:
                        return i
        
        return -1
    
    def _action_inject(
        self,
        flow_def: Dict[str, Any],
        modification: Dict[str, Any],
        position: str  # "before" or "after"
    ) -> Dict[str, Any]:
        """inject_before / inject_after の実装"""
        target_step = modification.get("target_step", {})
        new_steps = modification.get("steps", [])
        target_pipeline = modification.get("pipeline")
        
        if not new_steps:
            return flow_def
        
        pipelines = flow_def.get("pipelines", {})
        
        for pipeline_name, steps in pipelines.items():
            if target_pipeline and pipeline_name != target_pipeline:
                continue
            
            if not isinstance(steps, list):
                continue
            
            index = self._find_step_index(steps, target_step)
            if index >= 0:
                if position == "after":
                    index += 1
                
                for j, new_step in enumerate(new_steps):
                    steps.insert(index + j, copy.deepcopy(new_step))
        
        return flow_def
    
    def _action_replace(
        self,
        flow_def: Dict[str, Any],
        modification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """replace の実装"""
        target_step = modification.get("target_step", {})
        new_steps = modification.get("steps", [])
        target_pipeline = modification.get("pipeline")
        
        pipelines = flow_def.get("pipelines", {})
        
        for pipeline_name, steps in pipelines.items():
            if target_pipeline and pipeline_name != target_pipeline:
                continue
            
            if not isinstance(steps, list):
                continue
            
            index = self._find_step_index(steps, target_step)
            if index >= 0:
                # 元のステップを削除
                steps.pop(index)
                # 新しいステップを挿入
                for j, new_step in enumerate(new_steps):
                    steps.insert(index + j, copy.deepcopy(new_step))
        
        return flow_def
    
    def _action_wrap_loop(
        self,
        flow_def: Dict[str, Any],
        modification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """wrap_with_loop の実装"""
        target_steps = modification.get("target_steps", [])  # ステップIDのリスト
        loop_config = modification.get("loop_config", {})
        target_pipeline = modification.get("pipeline")
        
        if not target_steps:
            return flow_def
        
        pipelines = flow_def.get("pipelines", {})
        
        for pipeline_name, steps in pipelines.items():
            if target_pipeline and pipeline_name != target_pipeline:
                continue
            
            if not isinstance(steps, list):
                continue
            
            # ターゲットステップのインデックスを収集
            indices = []
            for target_id in target_steps:
                for i, step in enumerate(steps):
                    if step.get("id") == target_id:
                        indices.append(i)
                        break
            
            if not indices:
                continue
            
            # 連続する範囲を特定
            indices.sort()
            start_idx = indices[0]
            end_idx = indices[-1]
            
            # 対象ステップを抽出
            loop_steps = steps[start_idx:end_idx + 1]
            
            # loopステップを作成
            loop_step = {
                "type": "loop",
                "exit_when": loop_config.get("exit_condition", "false"),
                "max_iterations": loop_config.get("max_iterations", 10),
                "steps": copy.deepcopy(loop_steps)
            }
            
            # 元のステップを削除してloopステップを挿入
            del steps[start_idx:end_idx + 1]
            steps.insert(start_idx, loop_step)
        
        return flow_def
    
    def _action_remove(
        self,
        flow_def: Dict[str, Any],
        modification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """remove の実装"""
        target_step = modification.get("target_step", {})
        target_pipeline = modification.get("pipeline")
        
        pipelines = flow_def.get("pipelines", {})
        
        for pipeline_name, steps in pipelines.items():
            if target_pipeline and pipeline_name != target_pipeline:
                continue
            
            if not isinstance(steps, list):
                continue
            
            index = self._find_step_index(steps, target_step)
            if index >= 0:
                steps.pop(index)
        
        return flow_def
    
    def _action_set_property(
        self,
        flow_def: Dict[str, Any],
        modification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """set_property の実装"""
        target_step = modification.get("target_step", {})
        properties = modification.get("properties", {})
        target_pipeline = modification.get("pipeline")
        
        pipelines = flow_def.get("pipelines", {})
        
        for pipeline_name, steps in pipelines.items():
            if target_pipeline and pipeline_name != target_pipeline:
                continue
            
            if not isinstance(steps, list):
                continue
            
            index = self._find_step_index(steps, target_step)
            if index >= 0:
                for key, value in properties.items():
                    steps[index][key] = copy.deepcopy(value)
        
        return flow_def
    
    def get_applied_modifiers(self) -> List[Dict[str, Any]]:
        """適用済みのmodifier情報を取得"""
        with self._lock:
            return list(self._applied_modifiers)
    
    def clear_applied(self) -> None:
        """適用済み情報をクリア"""
        with self._lock:
            self._applied_modifiers.clear()


# グローバル変数（後方互換のため残存。DI コンテナ優先）
_global_flow_composer: Optional[FlowComposer] = None
_composer_lock = threading.Lock()


def get_flow_composer() -> FlowComposer:
    """
    グローバルな FlowComposer を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        FlowComposer インスタンス
    """
    from .di_container import get_container
    return get_container().get("flow_composer")


def reset_flow_composer() -> FlowComposer:
    """
    FlowComposer をリセットする（テスト用）。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Returns:
        新しい FlowComposer インスタンス
    """
    global _global_flow_composer
    with _composer_lock:
        _global_flow_composer = FlowComposer()
    # DI コンテナのキャッシュも更新（_composer_lock の外で実行してデッドロック回避）
    from .di_container import get_container
    get_container().set_instance("flow_composer", _global_flow_composer)
    return _global_flow_composer
