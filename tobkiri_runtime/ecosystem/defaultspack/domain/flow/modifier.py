"""ModifierLoader — Flow Modifier ローダー（最小動作版スタブ）"""

from blocks._common import ok, error, gen_id, timestamp


class ModifierLoader:
    """Flow に適用される modifier を管理するローダー

    最小動作版ではスタブ実装。modifier を読み込む骨格だけ提供する。
    将来的には modifiers/ ディレクトリからロードし、フロー実行の
    前後にフック処理を挟む。
    """

    def __init__(self):
        self._modifiers_cache = {}

    def load_modifiers(self, flow_id):
        """flow_id に適用される modifier のリストを返す

        Args:
            flow_id: フロー ID

        Returns:
            modifier のリスト（スタブ: 空リスト）
        """
        if flow_id in self._modifiers_cache:
            return self._modifiers_cache[flow_id]
        modifiers = self._discover_modifiers(flow_id)
        self._modifiers_cache[flow_id] = modifiers
        return modifiers

    def _discover_modifiers(self, flow_id):
        """flow_id に適用可能な modifier を探索する（スタブ: 空リスト）

        Args:
            flow_id: フロー ID

        Returns:
            modifier のリスト
        """
        return []

    def apply_pre_hooks(self, modifiers, context):
        """pre_step フックを実行する

        各 modifier の pre_step メソッドを順に呼び出す。
        最小動作版では modifiers は空リストのため何も実行されない。

        Args:
            modifiers: modifier のリスト
            context: FlowContext
        """
        for modifier in modifiers:
            if hasattr(modifier, "pre_step") and callable(modifier.pre_step):
                modifier.pre_step(context)

    def apply_post_hooks(self, modifiers, context, result):
        """post_step フックを実行する

        各 modifier の post_step メソッドを順に呼び出す。
        最小動作版では modifiers は空リストのため何も実行されない。

        Args:
            modifiers: modifier のリスト
            context: FlowContext
            result: FlowResult
        """
        for modifier in modifiers:
            if hasattr(modifier, "post_step") and callable(modifier.post_step):
                modifier.post_step(context, result)

    def clear_cache(self):
        """modifier キャッシュをクリアする"""
        self._modifiers_cache.clear()
