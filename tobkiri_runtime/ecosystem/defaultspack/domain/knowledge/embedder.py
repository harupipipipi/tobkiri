"""Embedder — AIClient 経由の embedding 取得と cosine similarity 計算.

embedding API が利用できない環境では文字列マッチにフォールバックする。
"""

import math
import os



def _default_model():
    """embedding に使うモデル文字列を返す."""
    return os.environ.get("KNOWLEDGE_EMBED_MODEL", "openai/text-embedding-3-small")


def get_embedding(text):
    """テキストを embedding ベクトル (list[float]) に変換する.

    AIClient.embed() を呼び出す。失敗した場合は None を返す。
    """
    if not text or not isinstance(text, str):
        return None
    try:
        from domain.ai_client.client import AIClient
        client = AIClient()
        model = _default_model()
        result = client.embed(model, text)
        embeddings = result.get("embeddings", [])
        if embeddings and len(embeddings) > 0:
            vec = embeddings[0]
            if isinstance(vec, list) and len(vec) > 0:
                return vec
        return None
    except Exception:
        return None


def cosine_similarity(vec_a, vec_b):
    """2 つのベクトル間の cosine similarity を計算する.

    戻り値は -1.0 〜 1.0。引数が不正な場合は 0.0 を返す。
    """
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) != len(vec_b):
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def text_similarity(query, content):
    """embedding が使えない場合のフォールバック: 文字列ベースの類似度計算.

    3 段階マッチング:
      1. 完全一致        -> 1.0
      2. 部分文字列一致  -> len(query) / len(content)
      3. 単語レベル一致  -> 一致単語数 / クエリ単語数
    """
    q = query.lower().strip()
    c = content.lower().strip()

    if not q or not c:
        return 0.0

    if q == c:
        return 1.0

    if q in c:
        return round(len(q) / max(len(c), 1), 4)

    q_words = set(q.split())
    c_words = set(c.split())
    if not q_words:
        return 0.0
    matched = q_words & c_words
    if not matched:
        return 0.0
    return round(len(matched) / len(q_words), 4)
