"""
router.py — Router

入力を分析して最適なモデル/パイプラインを選択する。
ルールベースルーティングを提供する。
"""


class _Rule:
    """単一のルーティングルール。"""

    __slots__ = ("name", "condition", "target")

    def __init__(self, name, condition, target):
        self.name = name
        self.condition = condition
        self.target = target


class Router:
    """ルールベースのルーティングエンジン。

    add_rule() でルールを登録し、route() で最適なターゲットを返す。
    ルールは登録順（先にマッチしたものが勝つ）。

    Parameters
    ----------
    client : AIClient | None
        プロバイダーの利用可能性チェックに使用。None の場合チェックをスキップ。
    default_target : str
        どのルールにもマッチしなかった場合のフォールバックターゲット。
    """

    def __init__(self, client=None, default_target="stub/default"):
        self._client = client
        self._default_target = default_target
        self._rules = []

    @property
    def default_target(self):
        return self._default_target

    @default_target.setter
    def default_target(self, value):
        self._default_target = value

    def add_rule(self, name, condition_func, target):
        """ルールを追加する。

        Parameters
        ----------
        name : str
            ルールの名前（デバッグ・ログ用）。
        condition_func : callable
            (messages, tools, params) -> bool を受け取る関数。
            True を返した場合にこのルールがマッチ。
        target : str
            マッチした場合のターゲット。モデル文字列またはパイプライン名。
        """
        self._rules.append(_Rule(name, condition_func, target))

    def remove_rule(self, name):
        """名前でルールを削除する。

        Parameters
        ----------
        name : str
            削除するルール名。
        """
        self._rules = [r for r in self._rules if r.name != name]

    def list_rules(self):
        """登録済みルールの名前とターゲットのリストを返す。

        Returns
        -------
        list[dict]
            [{"name": str, "target": str}, ...]
        """
        return [{"name": r.name, "target": r.target} for r in self._rules]

    def _is_target_available(self, target):
        """ターゲットが利用可能かチェックする。

        パイプライン名（"/" を含まない）の場合は常に True を返す。
        モデル文字列（"provider/model"）の場合はプロバイダーが登録されているか確認。
        """
        if "/" not in target:
            return True
        if self._client is None:
            return True
        provider_name = target.split("/", 1)[0]
        return provider_name in self._client._providers

    def route(self, messages, tools=None, params=None):
        """入力を分析し、最適なターゲットを返す。

        Parameters
        ----------
        messages : list[dict]
            StandardMessage 形式。
        tools : list[dict] | None
            ツール定義。
        params : dict | None
            パラメータ。

        Returns
        -------
        dict
            {"target": str, "rule": str | None}
            rule はマッチしたルール名。デフォルトの場合は None。
        """
        tools = tools or []
        params = params or {}

        for rule in self._rules:
            try:
                matched = rule.condition(messages, tools, params)
            except Exception:
                continue

            if matched:
                if self._is_target_available(rule.target):
                    return {"target": rule.target, "rule": rule.name}

        return {"target": self._default_target, "rule": None}
