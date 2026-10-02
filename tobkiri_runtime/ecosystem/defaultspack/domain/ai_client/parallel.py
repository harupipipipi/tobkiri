"""
parallel.py — ParallelCaller

複数のプロバイダー/モデルに並列でリクエストを投げ、結果をまとめて返す。
concurrent.futures.ThreadPoolExecutor を使用。
"""

import concurrent.futures
import time


class ParallelCaller:
    """複数モデルへの並列リクエストを実行する。

    Parameters
    ----------
    client : AIClient
        complete() メソッドを持つ AIClient インスタンス。
    max_workers : int
        ThreadPoolExecutor の最大ワーカー数。デフォルト 8。
    """

    def __init__(self, client, max_workers=8):
        self._client = client
        self._max_workers = max_workers

    def call(
        self,
        models,
        messages,
        tools=None,
        params=None,
        per_model_params=None,
        timeout_per_model=120,
        timeout_total=300,
        min_success=1,
    ):
        """複数モデルに並列リクエストを送り、結果を dict で返す。

        Parameters
        ----------
        models : list[str]
            モデル文字列のリスト（例: ["openai/gpt-4o", "anthropic/claude-sonnet-4-0"]）。
        messages : list[dict]
            StandardMessage 形式のメッセージリスト。
        tools : list[dict] | None
            ツール定義。全モデル共通。
        params : dict | None
            全モデル共通パラメータ。
        per_model_params : dict[str, dict] | None
            モデルごとの個別パラメータ。キーはモデル文字列。
        timeout_per_model : int | float
            各モデルの個別タイムアウト（秒）。
        timeout_total : int | float
            全体のタイムアウト（秒）。
        min_success : int
            最低成功数。この数に達したら他の完了を待たずに返す。
            0 の場合は全て完了まで待つ。

        Returns
        -------
        dict[str, dict]
            キーはモデル文字列、値は StandardResponse または
            {"error": str, "model": str} 形式のエラー辞書。
        """
        tools = tools or []
        params = params or {}
        per_model_params = per_model_params or {}
        min_success = max(0, min(min_success, len(models)))

        results = {}
        start_time = time.monotonic()

        def _call_one(model_str):
            merged_params = dict(params)
            if model_str in per_model_params:
                merged_params.update(per_model_params[model_str])
            return self._client.complete(model_str, messages, tools, merged_params)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._max_workers, len(models))
        ) as executor:
            future_to_model = {}
            for model_str in models:
                future = executor.submit(_call_one, model_str)
                future_to_model[future] = model_str

            success_count = 0
            done_futures = set()

            remaining_total = timeout_total - (time.monotonic() - start_time)
            wait_timeout = min(timeout_per_model, max(0, remaining_total))

            try:
                for future in concurrent.futures.as_completed(
                    future_to_model, timeout=wait_timeout
                ):
                    model_str = future_to_model[future]
                    done_futures.add(future)
                    try:
                        result = future.result(timeout=0)
                        results[model_str] = result
                        success_count += 1
                    except Exception as exc:
                        results[model_str] = {
                            "error": str(exc),
                            "model": model_str,
                        }

                    if min_success > 0 and success_count >= min_success:
                        break

                    elapsed = time.monotonic() - start_time
                    if elapsed >= timeout_total:
                        break
            except concurrent.futures.TimeoutError:
                pass

            for future, model_str in future_to_model.items():
                if future not in done_futures:
                    if future.done():
                        try:
                            result = future.result(timeout=0)
                            results[model_str] = result
                        except Exception as exc:
                            results[model_str] = {
                                "error": str(exc),
                                "model": model_str,
                            }
                    else:
                        future.cancel()
                        results[model_str] = {
                            "error": "timeout or cancelled",
                            "model": model_str,
                        }

        return results

    def call_with_fallback(self, models, messages, tools=None, params=None, timeout_per_model=120):
        """models を順に試し、最初に成功したものを返す。

        Parameters
        ----------
        models : list[str]
            優先順にソートされたモデル文字列リスト。
        messages : list[dict]
            StandardMessage 形式。
        tools : list[dict] | None
            ツール定義。
        params : dict | None
            パラメータ。
        timeout_per_model : int | float
            各モデルのタイムアウト（秒）。

        Returns
        -------
        dict
            StandardResponse または最後のエラー辞書。
        """
        tools = tools or []
        params = params or {}
        last_error = None

        for model_str in models:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        self._client.complete, model_str, messages, tools, params
                    )
                    result = future.result(timeout=timeout_per_model)
                    return result
            except Exception as exc:
                last_error = {"error": str(exc), "model": model_str}

        return last_error or {"error": "no models provided", "model": ""}
