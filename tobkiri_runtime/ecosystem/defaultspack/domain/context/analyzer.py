"""domain.context.analyzer — コンテキスト分析ロジック。

ChatStore, Inspector, PromptManager, ToolRegistry, KnowledgeStore, MemoryStore
を読み取り専用で使用し、会話別・システム全体のコンテキスト情報を算出する。
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.chat.store import ChatStore
from domain.dev.inspector import Inspector


# ---------------------------------------------------------------------------
# モデルごとのコンテキスト上限トークン数テーブル
# ---------------------------------------------------------------------------
MODEL_CONTEXT_LIMITS = {
    # OpenAI
    "openai/gpt-4o": 128000,
    "openai/gpt-4o-mini": 128000,
    "openai/gpt-4-turbo": 128000,
    "openai/gpt-4": 8192,
    "openai/gpt-3.5-turbo": 16385,
    "openai/o1": 200000,
    "openai/o1-mini": 128000,
    "openai/o1-pro": 200000,
    "openai/o3-mini": 200000,
    # Anthropic
    "anthropic/claude-sonnet-4-20250514": 200000,
    "anthropic/claude-opus-4-20250514": 200000,
    "anthropic/claude-3-7-sonnet-20250219": 200000,
    "anthropic/claude-3-5-sonnet-20241022": 200000,
    "anthropic/claude-3-5-haiku-20241022": 200000,
    "anthropic/claude-3-opus-20240229": 200000,
    "anthropic/claude-3-sonnet-20240229": 200000,
    "anthropic/claude-3-haiku-20240307": 200000,
    # Google
    "google/gemini-2.0-flash": 1048576,
    "google/gemini-1.5-pro": 2097152,
    "google/gemini-1.5-flash": 1048576,
    # Rumi (カーネル内蔵ルーティング)
    "rumi/auto": 200000,
    "rumi/fast": 128000,
    "rumi/balanced": 200000,
    "rumi/max": 200000,
    # Stub
    "stub/default": 4096,
    "stub/fast": 4096,
    "stub/large": 4096,
}

# プロバイダーレベルのデフォルト上限 (個別モデルが見つからない場合)
_PROVIDER_DEFAULTS = {
    "openai": 128000,
    "anthropic": 200000,
    "google": 1048576,
    "rumi": 200000,
    "stub": 4096,
}

_GLOBAL_DEFAULT_LIMIT = 128000


# ---------------------------------------------------------------------------
# トークン推定
# ---------------------------------------------------------------------------
def estimate_tokens(text):
    """テキストの推定トークン数を返す。

    ヒューリスティクス:
      - ASCII 文字 (英語等): 1 トークン ≈ 4 文字
      - 非ASCII 文字 (日本語等): 1 トークン ≈ 1.5 文字

    Args:
        text: 推定対象のテキスト文字列

    Returns:
        推定トークン数 (int, 最低 0)
    """
    if not text or not isinstance(text, str):
        return 0

    ascii_chars = 0
    non_ascii_chars = 0

    for ch in text:
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1

    tokens_from_ascii = ascii_chars / 4.0
    tokens_from_non_ascii = non_ascii_chars / 1.5

    return max(0, int(tokens_from_ascii + tokens_from_non_ascii))


def estimate_tokens_for_messages(messages):
    """メッセージリスト全体の推定トークン数を返す。

    各メッセージの role (約 1 トークン) + content のトークン数を合算する。
    メッセージ区切りに約 3 トークンのオーバーヘッドを加算する。

    Args:
        messages: メッセージ dict のリスト。各 dict は "role" と
                  "content" (str) または "raw_text" (str) を持つ。

    Returns:
        推定トークン数 (int)
    """
    if not messages:
        return 0

    total = 0
    for msg in messages:
        # メッセージ区切りのオーバーヘッド (role トークン + 構造トークン)
        total += 4

        content = msg.get("raw_text") or ""
        if not content:
            raw_content = msg.get("content", "")
            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, list):
                parts = []
                for block in raw_content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                content = " ".join(parts)

        total += estimate_tokens(content)

    # 会話全体の終端オーバーヘッド
    total += 3
    return total


# ---------------------------------------------------------------------------
# モデル上限解決
# ---------------------------------------------------------------------------
def get_model_context_limit(model_str):
    """モデル文字列からコンテキスト上限トークン数を返す。

    解決順:
      1. MODEL_CONTEXT_LIMITS に完全一致
      2. プロバイダー名部分で _PROVIDER_DEFAULTS を検索
      3. _GLOBAL_DEFAULT_LIMIT にフォールバック

    Args:
        model_str: "provider/model" 形式のモデル文字列

    Returns:
        コンテキスト上限トークン数 (int)
    """
    if not model_str or not isinstance(model_str, str):
        return _GLOBAL_DEFAULT_LIMIT

    # 完全一致
    if model_str in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[model_str]

    # プレフィックス一致 (最長マッチ)
    best_match = None
    best_length = 0
    for key in MODEL_CONTEXT_LIMITS:
        if model_str.startswith(key) and len(key) > best_length:
            best_match = key
            best_length = len(key)
    if best_match is not None:
        return MODEL_CONTEXT_LIMITS[best_match]

    # プロバイダーデフォルト
    if "/" in model_str:
        provider = model_str.split("/", 1)[0]
        if provider in _PROVIDER_DEFAULTS:
            return _PROVIDER_DEFAULTS[provider]

    return _GLOBAL_DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# 会話別コンテキスト分析
# ---------------------------------------------------------------------------
def analyze_conversation(conversation_id):
    """指定された会話のコンテキスト情報を分析して返す。

    Args:
        conversation_id: 会話 ID

    Returns:
        分析結果 dict。会話が見つからない場合は None。

        dict keys:
            conversation_id    : str
            title              : str
            model              : str
            model_context_limit: int
            total_messages     : int
            estimated_tokens   : int
            context_usage_ratio: float (0.0 ~ 1.0+)
            role_distribution  : {"user": int, "assistant": int, "system": int, "other": int}
            role_percentages   : {"user": float, "assistant": float, "system": float, "other": float}
            injected_knowledge_count: int
            injected_memory_count   : int
            created_at         : int (ms)
            updated_at         : int (ms)
            is_archived        : bool
    """
    store = ChatStore()
    conv = store.get_conversation(conversation_id)
    if conv is None:
        return None

    messages = conv.get("messages", [])
    model = conv.get("model", "stub/default")
    context_limit = get_model_context_limit(model)

    # トークン推定
    estimated_tokens = estimate_tokens_for_messages(messages)

    # ロール分布
    role_counts = {"user": 0, "assistant": 0, "system": 0, "other": 0}
    for msg in messages:
        role = msg.get("role", "other")
        if role in role_counts:
            role_counts[role] += 1
        else:
            role_counts["other"] += 1

    total_messages = len(messages)

    # ロール割合
    role_percentages = {"user": 0.0, "assistant": 0.0, "system": 0.0, "other": 0.0}
    if total_messages > 0:
        for role_key in role_percentages:
            role_percentages[role_key] = round(
                role_counts[role_key] / total_messages * 100.0, 2
            )

    # コンテキスト使用率
    context_usage_ratio = 0.0
    if context_limit > 0:
        context_usage_ratio = round(estimated_tokens / context_limit, 6)

    # 注入されたナレッジ・メモリ数を Inspector ログから取得
    injected_knowledge_count = 0
    injected_memory_count = 0
    try:
        inspector = Inspector()
        logs = inspector.find_by_conversation(conversation_id, limit=1)
        if logs:
            latest_log = logs[0]
            ctx_info = latest_log.get("context_info", {})
            knowledge_results = ctx_info.get("knowledge_results", [])
            memory_results = ctx_info.get("memory_results", [])
            injected_knowledge_count = len(knowledge_results) if isinstance(knowledge_results, list) else 0
            injected_memory_count = len(memory_results) if isinstance(memory_results, list) else 0
    except Exception:
        # Inspector アクセス失敗時は 0 のまま
        injected_knowledge_count = 0
        injected_memory_count = 0

    return {
        "conversation_id": conversation_id,
        "title": conv.get("title", ""),
        "model": model,
        "model_context_limit": context_limit,
        "total_messages": total_messages,
        "estimated_tokens": estimated_tokens,
        "context_usage_ratio": context_usage_ratio,
        "role_distribution": role_counts,
        "role_percentages": role_percentages,
        "injected_knowledge_count": injected_knowledge_count,
        "injected_memory_count": injected_memory_count,
        "created_at": conv.get("created_at", 0),
        "updated_at": conv.get("updated_at", 0),
        "is_archived": conv.get("is_archived", False),
    }


# ---------------------------------------------------------------------------
# システム全体コンテキスト分析
# ---------------------------------------------------------------------------
def analyze_system():
    """システム全体のコンテキスト情報を分析して返す。

    Returns:
        dict keys:
            active_conversations      : int
            total_conversations       : int
            total_messages_all        : int
            estimated_total_tokens    : int
            estimated_memory_bytes    : int  (概算)
            registered_prompts_count  : int
            registered_tools_count    : int
            knowledge_entries_count   : int
            memory_entries_count      : int
            models_in_use             : list[str] (ユニークモデル一覧)
            inspector_log_count       : int
            timestamp                 : str (ISO 8601)
    """
    store = ChatStore()
    all_convs, total_conv_count = store.list_conversations(limit=999999, offset=0)

    active_count = 0
    total_messages_all = 0
    estimated_total_tokens = 0
    models_in_use_set = set()

    for conv in all_convs:
        if not conv.get("is_archived", False):
            active_count += 1

        messages = conv.get("messages", [])
        msg_count = len(messages)
        total_messages_all += msg_count

        estimated_total_tokens += estimate_tokens_for_messages(messages)

        model = conv.get("model", "stub/default")
        models_in_use_set.add(model)

    # メモリ使用量の粗い概算: 1メッセージ ≈ 500 bytes のメタデータ + テキスト
    # テキスト量は estimated_total_tokens * 3 文字 * 3 bytes/char (UTF-8) で概算
    estimated_memory_bytes = (total_messages_all * 500) + (estimated_total_tokens * 9)

    # プロンプト数
    registered_prompts_count = 0
    try:
        from domain.prompt.manager import get_manager
        manager = get_manager()
        prompts = manager.list_prompts()
        registered_prompts_count = len(prompts)
    except Exception:
        registered_prompts_count = 0

    # ツール数
    registered_tools_count = 0
    try:
        from domain.tool.catalog_contract_client import (
            ContractToolCatalog as ToolRegistry,
        )
        tool_registry = ToolRegistry()
        tools = tool_registry.list_tools()
        registered_tools_count = len(tools)
    except Exception:
        registered_tools_count = 0

    # ナレッジ数
    knowledge_entries_count = 0
    try:
        from domain.knowledge.store import KnowledgeStore
        ks = KnowledgeStore()
        result = ks.list_entries(limit=0, offset=0)
        knowledge_entries_count = result.get("total", 0)
    except Exception:
        knowledge_entries_count = 0

    # メモリ数
    memory_entries_count = 0
    try:
        from domain.memory.store import MemoryStore
        ms = MemoryStore()
        memory_entries_count = len(ms.long_term) + len(ms.vector_store)
    except Exception:
        memory_entries_count = 0

    # Inspector ログ数
    inspector_log_count = 0
    try:
        inspector = Inspector()
        all_logs = inspector.list_logs(limit=9999)
        inspector_log_count = len(all_logs)
    except Exception:
        inspector_log_count = 0

    return {
        "active_conversations": active_count,
        "total_conversations": total_conv_count,
        "total_messages_all": total_messages_all,
        "estimated_total_tokens": estimated_total_tokens,
        "estimated_memory_bytes": estimated_memory_bytes,
        "registered_prompts_count": registered_prompts_count,
        "registered_tools_count": registered_tools_count,
        "knowledge_entries_count": knowledge_entries_count,
        "memory_entries_count": memory_entries_count,
        "models_in_use": sorted(models_in_use_set),
        "inspector_log_count": inspector_log_count,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
