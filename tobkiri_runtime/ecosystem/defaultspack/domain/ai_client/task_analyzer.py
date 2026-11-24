"""
task_analyzer.py — 入力テキスト分析

ユーザーの入力を分析し、タスクの複雑さ・種類・コンテキスト長を判定する。
fast モード（キーワードベースの高速判定）と heavy モード（詳細分析）を提供する。
"""

import re


# ── タスク種類の定数 ────────────────────────────────────────────
TASK_CODING = "coding"
TASK_CREATIVE = "creative"
TASK_ANALYSIS = "analysis"
TASK_MATH = "math"
TASK_CONVERSATION = "conversation"
TASK_TRANSLATION = "translation"
TASK_SUMMARIZATION = "summarization"
TASK_QA = "qa"
TASK_GENERAL = "general"

# ── 複雑さレベル ────────────────────────────────────────────────
COMPLEXITY_LOW = "low"
COMPLEXITY_MEDIUM = "medium"
COMPLEXITY_HIGH = "high"

# ── キーワード辞書 ──────────────────────────────────────────────
_CODING_KEYWORDS = [
    "code", "python", "javascript", "typescript", "java", "rust", "go",
    "function", "class", "def ", "import ", "return ", "const ", "let ",
    "var ", "async", "await", "api", "endpoint", "database", "sql",
    "html", "css", "react", "vue", "angular", "node", "docker",
    "kubernetes", "deploy", "git", "commit", "branch", "merge",
    "debug", "error", "bug", "fix", "refactor", "test", "unittest",
    "compile", "build", "npm", "pip", "cargo", "gradle", "maven",
    "コード", "プログラム", "実装", "関数", "クラス", "デバッグ",
    "バグ", "修正", "リファクタリング", "テスト", "コンパイル",
    "algorithm", "data structure", "アルゴリズム", "データ構造",
    "regex", "正規表現", "script", "スクリプト", "shell", "bash",
]

_CREATIVE_KEYWORDS = [
    "write a story", "poem", "creative", "imagine", "fiction",
    "character", "plot", "narrative", "dialogue", "screenplay",
    "song", "lyrics", "essay", "blog post", "article",
    "物語", "小説", "詩", "創作", "キャラクター", "シナリオ",
    "歌詞", "エッセイ", "ブログ", "記事を書", "作文",
    "creative writing", "世界観", "設定", "ストーリー",
]

_ANALYSIS_KEYWORDS = [
    "analyze", "analysis", "compare", "evaluate", "assess",
    "pros and cons", "advantages", "disadvantages", "trade-off",
    "strategy", "recommend", "suggest", "advise", "review",
    "分析", "比較", "評価", "メリット", "デメリット",
    "戦略", "推薦", "提案", "アドバイス", "レビュー",
    "investigate", "research", "調査", "研究", "考察",
]

_MATH_KEYWORDS = [
    "calculate", "math", "equation", "formula", "integral",
    "derivative", "probability", "statistics", "matrix",
    "algebra", "geometry", "calculus", "theorem", "proof",
    "計算", "数学", "方程式", "公式", "積分", "微分",
    "確率", "統計", "行列", "代数", "幾何", "定理", "証明",
    "solve", "解", "グラフ", "関数", "log", "sin", "cos",
]

_TRANSLATION_KEYWORDS = [
    "translate", "translation", "翻訳", "訳して", "英訳",
    "和訳", "日本語に", "英語に", "to english", "to japanese",
    "in english", "in japanese", "を翻訳", "に翻訳",
]

_SUMMARIZATION_KEYWORDS = [
    "summarize", "summary", "要約", "まとめ", "要点",
    "tl;dr", "tldr", "in brief", "briefly", "短く",
    "概要", "ダイジェスト", "簡潔に",
]

_COMPLEXITY_HIGH_INDICATORS = [
    "step by step", "detailed", "comprehensive", "in-depth",
    "architecture", "design pattern", "system design",
    "multi-step", "complex", "advanced", "expert",
    "詳細", "包括的", "アーキテクチャ", "設計パターン",
    "システム設計", "複雑", "上級", "専門",
    "explain in detail", "thorough", "全て", "すべて",
    "complete", "full implementation", "完全な",
]

_COMPLEXITY_LOW_INDICATORS = [
    "what is", "who is", "when", "where", "how old",
    "define", "meaning of", "とは", "って何", "ですか",
    "教えて", "簡単に", "一言で", "yes or no",
    "true or false", "はい か いいえ",
]


def _extract_text_from_messages(messages):
    """メッセージリストからユーザーテキストを抽出する。"""
    parts = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n".join(parts)


def _count_tokens_estimate(text):
    """トークン数の簡易推定（英語: ~4文字/token、日本語: ~1.5文字/token）。"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + non_ascii_chars / 1.5)


def _keyword_score(text, keywords):
    """テキスト内のキーワード一致スコアを返す。"""
    text_lower = text.lower()
    count = 0
    for kw in keywords:
        if kw.lower() in text_lower:
            count += 1
    return count


def _detect_code_blocks(text):
    """コードブロック（```...```）の数を検出する。"""
    return len(re.findall(r"```", text)) // 2


def _detect_task_type_fast(text):
    """キーワードベースの高速タスク種類判定。

    Returns
    -------
    str
        タスク種類定数。
    """
    scores = {
        TASK_CODING: _keyword_score(text, _CODING_KEYWORDS) + _detect_code_blocks(text) * 3,
        TASK_CREATIVE: _keyword_score(text, _CREATIVE_KEYWORDS),
        TASK_ANALYSIS: _keyword_score(text, _ANALYSIS_KEYWORDS),
        TASK_MATH: _keyword_score(text, _MATH_KEYWORDS),
        TASK_TRANSLATION: _keyword_score(text, _TRANSLATION_KEYWORDS),
        TASK_SUMMARIZATION: _keyword_score(text, _SUMMARIZATION_KEYWORDS),
    }
    best_type = TASK_GENERAL
    best_score = 0
    for task_type, score in scores.items():
        if score > best_score:
            best_score = score
            best_type = task_type
    if best_score == 0:
        # QA 判定: 疑問符で終わる or 疑問詞で始まる
        if text.strip().endswith("?") or text.strip().endswith("？"):
            return TASK_QA
        return TASK_GENERAL
    return best_type


def _detect_complexity_fast(text):
    """キーワードベースの高速複雑さ判定。

    Returns
    -------
    str
        COMPLEXITY_LOW, COMPLEXITY_MEDIUM, COMPLEXITY_HIGH のいずれか。
    """
    high_score = _keyword_score(text, _COMPLEXITY_HIGH_INDICATORS)
    low_score = _keyword_score(text, _COMPLEXITY_LOW_INDICATORS)

    token_est = _count_tokens_estimate(text)

    # 長い入力 → 複雑な可能性が高い
    if token_est > 500:
        high_score += 2
    elif token_est > 200:
        high_score += 1

    # コードブロックが多い → 複雑
    code_blocks = _detect_code_blocks(text)
    if code_blocks >= 2:
        high_score += 2
    elif code_blocks == 1:
        high_score += 1

    if high_score >= 2:
        return COMPLEXITY_HIGH
    if low_score >= 2 and high_score == 0:
        return COMPLEXITY_LOW
    return COMPLEXITY_MEDIUM


def analyze_fast(messages):
    """fast モード: キーワードベースの高速分析。

    Parameters
    ----------
    messages : list[dict]
        StandardMessage 形式のメッセージリスト。

    Returns
    -------
    dict
        {
            "task_type": str,
            "complexity": str,
            "context_tokens_estimate": int,
            "has_code": bool,
            "has_images": bool,
            "language_hint": str,  # "ja", "en", "mixed", "unknown"
            "mode": "fast",
        }
    """
    text = _extract_text_from_messages(messages)
    if not text.strip():
        return {
            "task_type": TASK_GENERAL,
            "complexity": COMPLEXITY_LOW,
            "context_tokens_estimate": 0,
            "has_code": False,
            "has_images": False,
            "language_hint": "unknown",
            "mode": "fast",
        }

    task_type = _detect_task_type_fast(text)
    complexity = _detect_complexity_fast(text)
    token_est = _count_tokens_estimate(text)

    # 全メッセージのコンテキスト長を推定
    full_text = ""
    has_images = False
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            full_text += content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        full_text += block.get("text", "")
                    elif block.get("type") in ("image", "image_url"):
                        has_images = True

    context_tokens = _count_tokens_estimate(full_text)
    has_code = _detect_code_blocks(text) > 0 or task_type == TASK_CODING

    # 言語判定
    non_ascii_count = sum(1 for c in text if ord(c) > 127)
    total_len = len(text) if text else 1
    if non_ascii_count / total_len > 0.3:
        language_hint = "ja"
    elif non_ascii_count / total_len > 0.05:
        language_hint = "mixed"
    elif total_len > 0:
        language_hint = "en"
    else:
        language_hint = "unknown"

    return {
        "task_type": task_type,
        "complexity": complexity,
        "context_tokens_estimate": context_tokens,
        "has_code": has_code,
        "has_images": has_images,
        "language_hint": language_hint,
        "mode": "fast",
    }


def analyze_heavy(messages):
    """heavy モード: 詳細分析。

    fast 分析に加えて以下を追加:
    - タスク種類のスコア詳細
    - 複雑さスコア詳細
    - 推奨モデル特性

    Parameters
    ----------
    messages : list[dict]
        StandardMessage 形式のメッセージリスト。

    Returns
    -------
    dict
        fast の結果に加えて:
        {
            "task_scores": dict,
            "complexity_details": dict,
            "recommended_traits": list[str],
            "mode": "heavy",
        }
    """
    fast_result = analyze_fast(messages)
    text = _extract_text_from_messages(messages)

    # タスク種類スコア詳細
    task_scores = {
        TASK_CODING: _keyword_score(text, _CODING_KEYWORDS) + _detect_code_blocks(text) * 3,
        TASK_CREATIVE: _keyword_score(text, _CREATIVE_KEYWORDS),
        TASK_ANALYSIS: _keyword_score(text, _ANALYSIS_KEYWORDS),
        TASK_MATH: _keyword_score(text, _MATH_KEYWORDS),
        TASK_TRANSLATION: _keyword_score(text, _TRANSLATION_KEYWORDS),
        TASK_SUMMARIZATION: _keyword_score(text, _SUMMARIZATION_KEYWORDS),
    }

    # 複雑さ詳細
    high_score = _keyword_score(text, _COMPLEXITY_HIGH_INDICATORS)
    low_score = _keyword_score(text, _COMPLEXITY_LOW_INDICATORS)
    token_est = _count_tokens_estimate(text)
    code_blocks = _detect_code_blocks(text)

    complexity_details = {
        "high_indicators": high_score,
        "low_indicators": low_score,
        "token_estimate": token_est,
        "code_blocks": code_blocks,
        "message_count": len(messages),
    }

    # 推奨モデル特性
    recommended_traits = []
    task_type = fast_result["task_type"]
    complexity = fast_result["complexity"]

    if task_type == TASK_CODING:
        recommended_traits.append("coding_strong")
    if task_type == TASK_CREATIVE:
        recommended_traits.append("creative_strong")
    if task_type == TASK_MATH:
        recommended_traits.append("reasoning_strong")
    if task_type == TASK_ANALYSIS:
        recommended_traits.append("reasoning_strong")
    if task_type == TASK_TRANSLATION:
        recommended_traits.append("multilingual")

    if complexity == COMPLEXITY_HIGH:
        recommended_traits.append("high_quality")
    elif complexity == COMPLEXITY_LOW:
        recommended_traits.append("fast_response")

    context_tokens = fast_result["context_tokens_estimate"]
    if context_tokens > 50000:
        recommended_traits.append("large_context")
    elif context_tokens > 10000:
        recommended_traits.append("medium_context")

    if fast_result["has_images"]:
        recommended_traits.append("vision")

    fast_result.update({
        "task_scores": task_scores,
        "complexity_details": complexity_details,
        "recommended_traits": recommended_traits,
        "mode": "heavy",
    })

    return fast_result
