"""Pack 独自ルート ハンドラ Mixin"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from .api_response import APIResponse

logger = logging.getLogger(__name__)


def _is_safe_path_param(value: str) -> bool:
    """パスパラメータが安全かどうかを検証する。

    null バイトやパストラバーサルシーケンスを含む値を拒否する。
    """
    if "\x00" in value:
        return False
    if ".." in value:
        return False
    return True


# --------------------------------------------------------------------------
# テンプレートパス → 正規表現コンパイル
# --------------------------------------------------------------------------

# {param} プレースホルダの検出パターン
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _compile_template_path(path: str) -> tuple[re.Pattern, list[str]] | None:
    """テンプレートパスを正規表現にコンパイルする。

    パス中に ``{param}`` プレースホルダが含まれない場合は None を返す。

    変換例::

        /api/packs/{pack_id}/items/{item_id}
        → ^/api/packs/(?P<pack_id>[^/]+)/items/(?P<item_id>[^/]+)$

    Returns:
        (compiled_regex, param_names) or None
    """
    param_names: list[str] = _TEMPLATE_PLACEHOLDER_RE.findall(path)
    if not param_names:
        return None

    # セグメント単位で正規表現を構築
    regex_str = "^"
    for segment in path.strip("/").split("/"):
        regex_str += "/"
        m = _TEMPLATE_PLACEHOLDER_RE.fullmatch(segment)
        if m:
            regex_str += f"(?P<{m.group(1)}>[^/]+)"
        else:
            regex_str += re.escape(segment)
    regex_str += "$"

    return (re.compile(regex_str), param_names)


class RouteHandlersMixin:
    """Pack 独自ルート (load / match / handle / reload) のハンドラ

    ルーティングは 2 段階で実行される:

    1. **完全一致** — ``_exact_routes`` dict で O(1) lookup
    2. **テンプレート** — ``_template_routes`` のプリコンパイル済み正規表現を走査

    ``_pack_routes`` は後方互換のため維持し、一覧 API で使用する。
    """

    _pack_routes: dict = {}          # {(method, path): route_info} — 後方互換・一覧用
    _exact_routes: dict = {}         # {(method, path): route_info} — 完全一致 O(1)
    _template_routes: list = []      # [(method, compiled_re, param_names, route_info), ...]

    if TYPE_CHECKING:
        def _run_flow(
            self,
            flow_id: str,
            inputs: dict[str, object],
            timeout: float,
        ) -> dict[str, object]: ...

        def _send_response(
            self,
            response: APIResponse,
            status: int = 200,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None: ...

    @classmethod
    def load_pack_routes(cls, registry) -> int:
        """Reject legacy route authority; v4 routes live in OperationCatalog."""
        del registry
        cls._pack_routes = {}
        cls._exact_routes = {}
        cls._template_routes = []
        logger.warning(
            "Legacy Pack routes are disabled; use a captured v4 dispatch session"
        )
        return 0

    def _match_pack_route(self, path: str, method: str):
        """パスとメソッドがPack独自ルートにマッチするか判定。

        マッチした場合は (route_info, path_params) のタプルを返す。
        マッチしない場合は None を返す。

        1. 完全一致ルートを dict O(1) lookup
        2. テンプレートルートをプリコンパイル済み正規表現で走査
        """
        del path, method
        return None

    def _handle_pack_route_request(self, path: str, body: dict, method: str, match) -> None:
        """Pack独自ルートへのリクエストをFlow実行に委譲する"""
        from ..pack_api_server import APIResponse

        route_info, path_params = match
        pack_id = route_info["pack_id"]
        flow_id = route_info["flow_id"]

        # Flow スコープ検証: flow_id は当該 Pack のスコープ内でなければならない
        if not (flow_id.startswith(pack_id + ".") or flow_id == pack_id):
            logger.warning(
                "Flow scope violation: pack_id=%s, flow_id=%s", pack_id, flow_id,
            )
            self._send_response(
                APIResponse(False, error="Flow scope violation"), 403,
            )
            return

        input_mapping = route_info.get("input_mapping", {})

        # 入力を構築
        inputs = {}
        if body and isinstance(body, dict):
            inputs.update(body)
        if path_params:
            inputs["_path_params"] = path_params
        inputs["_method"] = method
        inputs["_path"] = path

        # input_mapping を適用
        if input_mapping and isinstance(input_mapping, dict):
            for target_key, source_expr in input_mapping.items():
                if isinstance(source_expr, str) and source_expr.startswith("path."):
                    param_name = source_expr[5:]
                    if param_name in path_params:
                        inputs[target_key] = path_params[param_name]

        result = self._run_flow(flow_id, inputs, timeout=300)
        if result.get("success"):
            self._send_response(APIResponse(True, result))
        else:
            status_value = result.get("status_code")
            status_code = status_value if isinstance(status_value, int) else 500
            error_value = result.get("error")
            error = error_value if isinstance(error_value, str) else None
            self._send_response(
                APIResponse(False, error=error), status_code,
            )

    def _get_registered_routes(self) -> dict:
        """GET /api/routes — 登録済みPack独自ルート一覧を返す"""
        routes = []
        for (method, path), info in self._pack_routes.items():
            routes.append({
                "method": method,
                "path": path,
                "pack_id": info["pack_id"],
                "flow_id": info["flow_id"],
                "description": info.get("description", ""),
            })
        return {"routes": routes, "count": len(routes)}

    def _reload_pack_routes(self) -> dict:
        """Reject mutable route reload outside a captured v4 activation."""
        return {"reloaded": False, "error": "Legacy Pack route reload is disabled"}
