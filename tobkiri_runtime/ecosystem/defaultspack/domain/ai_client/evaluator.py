"""
evaluator.py — Evaluator

複数の出力を比較・評価して最良のもの選ぶ。
基本評価・構造評価・LLM ジャッジ・カスタム評価を組み合わせる。
"""

import json

from blocks.chat._prompt_helpers import extract_text as _shared_extract_text


def _extract_text(response):
    """StandardResponse から全テキストを結合して返す。"""
    return _shared_extract_text(response)


def _is_error(response):
    """レスポンスがエラー辞書かどうか判定する。"""
    return isinstance(response, dict) and "error" in response and "content" not in response


class _Criterion:
    """単一の評価基準。"""

    __slots__ = ("name", "func", "weight")

    def __init__(self, name, func, weight):
        self.name = name
        self.func = func
        self.weight = weight


class Evaluator:
    """複数の StandardResponse を比較し、最良のものを選ぶ。

    Parameters
    ----------
    client : AIClient | None
        LLM ジャッジに使用。None の場合 LLM ジャッジは無効化される。
    judge_model : str
        LLM ジャッジに使用するモデル文字列。
    """

    def __init__(self, client=None, judge_model="openai/gpt-4o"):
        self._client = client
        self._judge_model = judge_model
        self._criteria = []
        self._register_builtin_criteria()

    def _register_builtin_criteria(self):
        """組み込みの評価基準を登録する。"""
        self._criteria.append(_Criterion("non_empty", self._check_non_empty, 10.0))
        self._criteria.append(_Criterion("min_length", self._check_min_length, 5.0))
        self._criteria.append(_Criterion("no_error", self._check_no_error, 20.0))

    # ── 組み込み評価関数 ──────────────────────────────────────────

    @staticmethod
    def _check_non_empty(response, original_messages):
        """空でないかチェック。空なら 0、テキストがあれば 1。"""
        text = _extract_text(response)
        return 1.0 if text.strip() else 0.0

    @staticmethod
    def _check_min_length(response, original_messages):
        """極端に短くないかチェック。10文字未満なら低スコア。"""
        text = _extract_text(response)
        length = len(text.strip())
        if length == 0:
            return 0.0
        if length < 10:
            return 0.3
        if length < 50:
            return 0.7
        return 1.0

    @staticmethod
    def _check_no_error(response, original_messages):
        """エラーでないかチェック。"""
        if _is_error(response):
            return 0.0
        return 1.0

    @staticmethod
    def check_json_valid(response, original_messages):
        """JSON として有効かチェックする。カスタム基準として登録可能。

        Returns
        -------
        float
            有効な JSON なら 1.0、不正なら 0.0、テキストがなければ 0.5。
        """
        text = _extract_text(response)
        if not text.strip():
            return 0.5
        try:
            json.loads(text)
            return 1.0
        except (json.JSONDecodeError, ValueError):
            return 0.0

    # ── 公開メソッド ──────────────────────────────────────────────

    def add_criterion(self, name, func, weight=1.0):
        """カスタム評価基準を追加する。

        Parameters
        ----------
        name : str
            基準名。
        func : callable
            (response, original_messages) -> float (0.0 ~ 1.0) を返す関数。
        weight : float
            重み。
        """
        self._criteria.append(_Criterion(name, func, weight))

    def remove_criterion(self, name):
        """名前で評価基準を削除する。"""
        self._criteria = [c for c in self._criteria if c.name != name]

    def score(self, response, original_messages):
        """単一レスポンスのスコアを計算する。

        Parameters
        ----------
        response : dict
            StandardResponse。
        original_messages : list[dict]
            元のメッセージリスト。

        Returns
        -------
        dict
            {"total": float, "details": dict[str, float]}
        """
        if _is_error(response):
            return {"total": 0.0, "details": {"error": 0.0}}

        details = {}
        total_weight = 0.0
        weighted_sum = 0.0

        for criterion in self._criteria:
            try:
                raw_score = criterion.func(response, original_messages)
                raw_score = max(0.0, min(1.0, float(raw_score)))
            except Exception:
                raw_score = 0.0
            details[criterion.name] = raw_score
            weighted_sum += raw_score * criterion.weight
            total_weight += criterion.weight

        total = weighted_sum / total_weight if total_weight > 0 else 0.0
        return {"total": total, "details": details}

    def llm_judge(self, results_dict, original_messages):
        """LLM に複数の回答を比較させ、最良のキーを返す。

        Parameters
        ----------
        results_dict : dict[str, dict]
            {model_name: StandardResponse} のマッピング。
        original_messages : list[dict]
            元のメッセージリスト。

        Returns
        -------
        str | None
            最良と判定されたモデルのキー。判定不能なら None。
        """
        if self._client is None:
            return None

        valid = {k: v for k, v in results_dict.items() if not _is_error(v)}
        if len(valid) < 2:
            return list(valid.keys())[0] if valid else None

        original_text = ""
        for msg in original_messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                original_text += content + "\n"
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        original_text += block.get("text", "") + "\n"

        answers_text = ""
        keys = list(valid.keys())
        for i, key in enumerate(keys):
            text = _extract_text(valid[key])
            answers_text += "--- Answer {} (key: {}) ---\n{}\n\n".format(i + 1, key, text)

        judge_messages = [
            {
                "role": "system",
                "content": (
                    "You are a judge comparing multiple AI responses. "
                    "Evaluate which answer best addresses the original request. "
                    "Respond with ONLY the key of the best answer, nothing else."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Original request:\n{}\n\n"
                    "Answers to compare:\n{}\n\n"
                    "Which answer is best? Reply with only the key."
                ).format(original_text.strip(), answers_text.strip()),
            },
        ]

        try:
            judge_response = self._client.complete(
                self._judge_model, judge_messages, [], {"max_tokens": 100}
            )
            judge_text = _extract_text(judge_response).strip()
            for key in keys:
                if key in judge_text:
                    return key
            return None
        except Exception:
            return None

    def pick_best(self, results_dict, original_messages, use_llm_judge=False):
        """複数の結果から最良のものを選択する。

        Parameters
        ----------
        results_dict : dict[str, dict]
            {model_name: StandardResponse} のマッピング。
        original_messages : list[dict]
            元のメッセージリスト。
        use_llm_judge : bool
            True の場合、スコアが僅差の上位2つに対して LLM ジャッジを実行する。

        Returns
        -------
        dict
            {"best_key": str, "best_response": dict, "scores": dict[str, dict]}
        """
        if not results_dict:
            return {
                "best_key": "",
                "best_response": {
                    "content": [{"type": "text", "text": ""}],
                    "finish_reason": "error",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    "raw_extra": {"error": "no results to evaluate"},
                },
                "scores": {},
            }

        scores = {}
        for key, response in results_dict.items():
            scores[key] = self.score(response, original_messages)

        sorted_keys = sorted(scores.keys(), key=lambda k: scores[k]["total"], reverse=True)
        best_key = sorted_keys[0]

        if use_llm_judge and len(sorted_keys) >= 2:
            top_score = scores[sorted_keys[0]]["total"]
            second_score = scores[sorted_keys[1]]["total"]
            if top_score > 0 and second_score > 0 and (top_score - second_score) < 0.15:
                top_two = {
                    sorted_keys[0]: results_dict[sorted_keys[0]],
                    sorted_keys[1]: results_dict[sorted_keys[1]],
                }
                judge_pick = self.llm_judge(top_two, original_messages)
                if judge_pick is not None:
                    best_key = judge_pick

        return {
            "best_key": best_key,
            "best_response": results_dict[best_key],
            "scores": scores,
        }
