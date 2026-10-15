"""
pipeline.py — Pipeline

ParallelCaller, Router, Evaluator を組み合わせた多段パイプラインの定義・実行。
"""

from domain.ai_client.parallel import ParallelCaller
from domain.ai_client.router import Router
from domain.ai_client.evaluator import Evaluator


class Pipeline:
    """多段パイプラインの定義と実行を管理する。

    Parameters
    ----------
    client : AIClient
        AIClient インスタンス。
    """

    def __init__(self, client):
        self._client = client
        self._pipelines = {}
        self._parallel = ParallelCaller(client)
        self._router = Router(client)
        self._evaluator = Evaluator(client)

    @property
    def router(self):
        """内部の Router インスタンスへのアクセス。"""
        return self._router

    @property
    def evaluator(self):
        """内部の Evaluator インスタンスへのアクセス。"""
        return self._evaluator

    @property
    def parallel(self):
        """内部の ParallelCaller インスタンスへのアクセス。"""
        return self._parallel

    def define(self, name, layers):
        """パイプラインを定義する。

        Parameters
        ----------
        name : str
            パイプライン名。
        layers : list[dict]
            レイヤー定義のリスト。各レイヤーは以下の形式:

            "parallel" レイヤー:
                {"type": "parallel", "models": [str, ...],
                 "timeout_per_model": int, "timeout_total": int, "min_success": int}

            "evaluate" レイヤー:
                {"type": "evaluate", "use_llm_judge": bool}

            "single" レイヤー:
                {"type": "single", "model": str}

            "route" レイヤー:
                {"type": "route"}
        """
        self._pipelines[name] = layers

    def remove(self, name):
        """パイプラインを削除する。"""
        self._pipelines.pop(name, None)

    def list_pipelines(self):
        """定義済みパイプライン名のリストを返す。"""
        return list(self._pipelines.keys())

    def get_definition(self, name):
        """パイプライン定義を返す。未定義なら None。"""
        return self._pipelines.get(name)

    def execute(self, pipeline_name, messages, tools=None, params=None):
        """パイプラインを実行する。

        Parameters
        ----------
        pipeline_name : str
            実行するパイプライン名。
        messages : list[dict]
            StandardMessage 形式。
        tools : list[dict] | None
            ツール定義。
        params : dict | None
            パラメータ。

        Returns
        -------
        dict
            最終レイヤーの出力。StandardResponse 形式。

        Raises
        ------
        ValueError
            パイプラインが未定義の場合。
        """
        layers = self._pipelines.get(pipeline_name)
        if layers is None:
            raise ValueError("Pipeline '{}' is not defined".format(pipeline_name))

        tools = tools or []
        params = params or {}

        context = {
            "messages": messages,
            "tools": tools,
            "params": params,
            "last_response": None,
            "parallel_results": None,
        }

        for layer in layers:
            layer_type = layer.get("type", "")

            if layer_type == "parallel":
                context = self._exec_parallel(layer, context)

            elif layer_type == "evaluate":
                context = self._exec_evaluate(layer, context)

            elif layer_type == "single":
                context = self._exec_single(layer, context)

            elif layer_type == "route":
                context = self._exec_route(layer, context)

            else:
                raise ValueError("Unknown layer type: '{}'".format(layer_type))

        if context["last_response"] is not None:
            return context["last_response"]

        return {
            "content": [{"type": "text", "text": ""}],
            "finish_reason": "error",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "raw_extra": {"error": "pipeline produced no output"},
        }

    def stream(self, pipeline_name, messages, tools=None, params=None):
        """パイプラインをストリーミング実行する。

        最終レイヤーが "single" の場合のみ対応。
        それ以外の場合は execute() の結果をストリーム風に返す。

        Parameters
        ----------
        pipeline_name : str
            パイプライン名。
        messages : list[dict]
            StandardMessage 形式。
        tools : list[dict] | None
            ツール定義。
        params : dict | None
            パラメータ。

        Yields
        ------
        dict
            ストリームチャンク。
        """
        layers = self._pipelines.get(pipeline_name)
        if layers is None:
            raise ValueError("Pipeline '{}' is not defined".format(pipeline_name))

        tools = tools or []
        params = params or {}

        if not layers:
            return

        if len(layers) == 1 and layers[0].get("type") == "single":
            model = layers[0].get("model", "stub/default")
            for chunk in self._client.stream(model, messages, tools, params):
                yield chunk
            return

        if layers[-1].get("type") == "single":
            pre_layers = layers[:-1]
            final_layer = layers[-1]

            context = {
                "messages": messages,
                "tools": tools,
                "params": params,
                "last_response": None,
                "parallel_results": None,
            }
            for layer in pre_layers:
                layer_type = layer.get("type", "")
                if layer_type == "parallel":
                    context = self._exec_parallel(layer, context)
                elif layer_type == "evaluate":
                    context = self._exec_evaluate(layer, context)
                elif layer_type == "single":
                    context = self._exec_single(layer, context)
                elif layer_type == "route":
                    context = self._exec_route(layer, context)

            model = final_layer.get("model", "stub/default")
            for chunk in self._client.stream(model, context["messages"], tools, params):
                yield chunk
        else:
            result = self.execute(pipeline_name, messages, tools, params)
            text = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            if text:
                yield {"type": "content_delta", "delta": {"type": "text", "text": text}}
            yield {
                "type": "stream_end",
                "finish_reason": result.get("finish_reason", "stop"),
                "usage": result.get("usage", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
            }

    # ── 内部実行メソッド ─────────────────────────────────────────

    def _exec_parallel(self, layer, context):
        """parallel レイヤーを実行する。"""
        models = layer.get("models", [])
        timeout_per = layer.get("timeout_per_model", 120)
        timeout_total = layer.get("timeout_total", 300)
        min_success = layer.get("min_success", 1)

        results = self._parallel.call(
            models=models,
            messages=context["messages"],
            tools=context["tools"],
            params=context["params"],
            timeout_per_model=timeout_per,
            timeout_total=timeout_total,
            min_success=min_success,
        )

        context["parallel_results"] = results
        return context

    def _exec_evaluate(self, layer, context):
        """evaluate レイヤーを実行する。"""
        results = context.get("parallel_results")
        if not results:
            return context

        use_llm_judge = layer.get("use_llm_judge", False)
        pick = self._evaluator.pick_best(
            results, context["messages"], use_llm_judge=use_llm_judge
        )

        best_response = pick["best_response"]
        context["last_response"] = best_response

        best_text = ""
        for block in best_response.get("content", []):
            if block.get("type") == "text":
                best_text += block.get("text", "")

        if best_text:
            context["messages"] = context["messages"] + [
                {"role": "assistant", "content": best_text}
            ]

        return context

    def _exec_single(self, layer, context):
        """single レイヤーを実行する。"""
        model = layer.get("model", "stub/default")
        result = self._client.complete(
            model, context["messages"], context["tools"], context["params"]
        )
        context["last_response"] = result

        text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        if text:
            context["messages"] = context["messages"] + [
                {"role": "assistant", "content": text}
            ]

        return context

    def _exec_route(self, layer, context):
        """route レイヤーを実行する。"""
        route_result = self._router.route(
            context["messages"], context["tools"], context["params"]
        )
        target = route_result["target"]

        if "/" in target:
            result = self._client.complete(
                target, context["messages"], context["tools"], context["params"]
            )
            context["last_response"] = result

            text = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            if text:
                context["messages"] = context["messages"] + [
                    {"role": "assistant", "content": text}
                ]
        elif target in self._pipelines:
            result = self.execute(target, context["messages"], context["tools"], context["params"])
            context["last_response"] = result
        else:
            context["last_response"] = {
                "content": [{"type": "text", "text": ""}],
                "finish_reason": "error",
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "raw_extra": {"error": "route target '{}' not found".format(target)},
            }

        return context
