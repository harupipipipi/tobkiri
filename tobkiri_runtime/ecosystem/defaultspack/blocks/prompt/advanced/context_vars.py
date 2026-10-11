"""blocks.prompt.advanced.context_vars — 拡張コンテキスト変数取得 API

利用可能な全コンテキスト変数とその現在の値を返す。

入力:
    {} (なし、または任意のコンテキスト情報)

出力:
    {
        "status": "ok",
        "data": {
            "builtin_keys":   [...],     # 組み込みコンテキスト変数キー一覧
            "current_values": {...},      # 現在の context から解決できる値
            "extended_keys":  [...],      # 拡張コンテキスト変数キー一覧
            "all_keys":       [...]       # 全キーの統合リスト
        }
    }
"""


from blocks._common import ok
from domain.prompt.template import CONTEXT_VARIABLE_KEYS
from domain.prompt.manager import get_manager


# 拡張コンテキスト変数: ビルダー・バージョニングシステムが使用する追加変数
EXTENDED_CONTEXT_KEYS = (
    "context.model",
    "context.model_provider",
    "context.max_tokens",
    "context.temperature",
    "context.user_id",
    "context.session_id",
    "context.workspace",
    "context.timestamp",
    "context.prompt_name",
    "context.prompt_version",
    "context.active_tools",
    "context.active_agents",
    "context.language",
    "context.platform",
)


def run(input_data: dict, context: dict) -> dict:
    manager = get_manager()

    builtin_keys = list(CONTEXT_VARIABLE_KEYS)
    extended_keys = list(EXTENDED_CONTEXT_KEYS)
    all_keys = builtin_keys + extended_keys

    # 現在の context から値を解決する
    current_values = {}
    if context:
        # 組み込み変数
        builtin_resolved = manager.inject_context_variables({}, context)
        current_values.update(builtin_resolved)

        # 拡張変数: context dict から直接マッピング
        extended_mapping = {
            "context.model": "model",
            "context.model_provider": "model_provider",
            "context.max_tokens": "max_tokens",
            "context.temperature": "temperature",
            "context.user_id": "user_id",
            "context.session_id": "session_id",
            "context.workspace": "workspace",
            "context.timestamp": "timestamp",
            "context.prompt_name": "prompt_name",
            "context.prompt_version": "prompt_version",
            "context.active_tools": "active_tools",
            "context.active_agents": "active_agents",
            "context.language": "language",
            "context.platform": "platform",
        }
        for template_key, ctx_key in extended_mapping.items():
            if ctx_key in context:
                val = context[ctx_key]
                if isinstance(val, (list, dict)):
                    import json
                    val = json.dumps(val, ensure_ascii=False)
                current_values[template_key] = val

    # 各キーの説明情報
    key_descriptions = {
        "context.total_tokens": "現在の会話の総トークン数",
        "context.message_count": "現在の会話のメッセージ数",
        "context.messages": "会話メッセージのJSON文字列",
        "context.system_prompt": "現在のシステムプロンプト",
        "context.conversation_id": "会話ID",
        "context.knowledge": "関連ナレッジ検索結果",
        "context.memory": "関連メモリ検索結果",
        "context.model": "使用中のAIモデル名",
        "context.model_provider": "AIモデルのプロバイダー名",
        "context.max_tokens": "最大トークン数設定",
        "context.temperature": "Temperature設定",
        "context.user_id": "現在のユーザーID",
        "context.session_id": "現在のセッションID",
        "context.workspace": "ワークスペースパス",
        "context.timestamp": "現在のタイムスタンプ",
        "context.prompt_name": "使用中のプロンプト名",
        "context.prompt_version": "使用中のプロンプトバージョン",
        "context.active_tools": "有効なツール一覧",
        "context.active_agents": "有効なエージェント一覧",
        "context.language": "言語設定",
        "context.platform": "プラットフォーム情報",
    }

    detailed_keys = []
    for key in all_keys:
        entry = {
            "key": key,
            "description": key_descriptions.get(key, ""),
            "current_value": current_values.get(key),
            "source": "builtin" if key in builtin_keys else "extended",
        }
        detailed_keys.append(entry)

    return ok({
        "builtin_keys": builtin_keys,
        "extended_keys": extended_keys,
        "all_keys": all_keys,
        "current_values": current_values,
        "detailed": detailed_keys,
    })
