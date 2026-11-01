"""
model_profiles.py — モデルプロファイル定義・管理

各AIモデルの特性（得意分野、コスト、速度、コンテキスト長）を定義し、
タスク分析結果に基づいてスコアリングを行う。
"""

import json
import copy


# ── デフォルトプロファイル ───────────────────────────────────────
# traits: モデルの持つ特性タグ
# cost: 相対コスト (1=安い, 10=高い)
# speed: 相対速度 (1=遅い, 10=速い)
# quality: 相対品質 (1=低い, 10=高い)
# context_window: コンテキストウィンドウ (トークン数)
# strengths: 得意なタスク種類のリスト
# provider: プロバイダー名

_DEFAULT_PROFILES = {
    # ── OpenAI ──
    "openai/gpt-4o": {
        "name": "GPT-4o",
        "provider": "openai",
        "model_id": "gpt-4o",
        "traits": ["coding_strong", "creative_strong", "reasoning_strong", "multilingual", "vision", "fast_response"],
        "cost": 5,
        "speed": 8,
        "quality": 9,
        "context_window": 128000,
        "strengths": ["coding", "analysis", "creative", "qa", "general", "translation"],
    },
    "openai/gpt-4o-mini": {
        "name": "GPT-4o Mini",
        "provider": "openai",
        "model_id": "gpt-4o-mini",
        "traits": ["fast_response", "multilingual", "vision"],
        "cost": 2,
        "speed": 10,
        "quality": 6,
        "context_window": 128000,
        "strengths": ["qa", "conversation", "general", "translation", "summarization"],
    },
    "openai/gpt-4-turbo": {
        "name": "GPT-4 Turbo",
        "provider": "openai",
        "model_id": "gpt-4-turbo",
        "traits": ["coding_strong", "reasoning_strong", "vision"],
        "cost": 6,
        "speed": 6,
        "quality": 8,
        "context_window": 128000,
        "strengths": ["coding", "analysis", "math"],
    },
    "openai/o1": {
        "name": "o1",
        "provider": "openai",
        "model_id": "o1",
        "traits": ["reasoning_strong", "high_quality"],
        "cost": 8,
        "speed": 3,
        "quality": 10,
        "context_window": 200000,
        "strengths": ["math", "analysis", "coding"],
    },
    "openai/o3-mini": {
        "name": "o3 Mini",
        "provider": "openai",
        "model_id": "o3-mini",
        "traits": ["reasoning_strong", "fast_response"],
        "cost": 4,
        "speed": 7,
        "quality": 8,
        "context_window": 200000,
        "strengths": ["math", "coding", "analysis"],
    },
    # ── Anthropic ──
    "anthropic/claude-opus-4-0": {
        "name": "Claude Opus 4",
        "provider": "anthropic",
        "model_id": "claude-opus-4-0",
        "traits": ["coding_strong", "creative_strong", "reasoning_strong", "high_quality", "multilingual"],
        "cost": 9,
        "speed": 4,
        "quality": 10,
        "context_window": 200000,
        "strengths": ["coding", "analysis", "creative", "math"],
    },
    "anthropic/claude-sonnet-4-0": {
        "name": "Claude Sonnet 4",
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-0",
        "traits": ["coding_strong", "creative_strong", "reasoning_strong", "multilingual", "fast_response"],
        "cost": 5,
        "speed": 7,
        "quality": 9,
        "context_window": 200000,
        "strengths": ["coding", "analysis", "creative", "general", "translation"],
    },
    "anthropic/claude-3-5-sonnet-20241022": {
        "name": "Claude 3.5 Sonnet",
        "provider": "anthropic",
        "model_id": "claude-3-5-sonnet-20241022",
        "traits": ["coding_strong", "reasoning_strong", "multilingual"],
        "cost": 5,
        "speed": 7,
        "quality": 8,
        "context_window": 200000,
        "strengths": ["coding", "analysis", "general"],
    },
    "anthropic/claude-3-5-haiku-20241022": {
        "name": "Claude 3.5 Haiku",
        "provider": "anthropic",
        "model_id": "claude-3-5-haiku-20241022",
        "traits": ["fast_response", "multilingual"],
        "cost": 1,
        "speed": 10,
        "quality": 6,
        "context_window": 200000,
        "strengths": ["qa", "conversation", "general", "summarization"],
    },
    # ── Google ──
    "google/gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro",
        "provider": "google",
        "model_id": "gemini-2.5-pro",
        "traits": ["coding_strong", "reasoning_strong", "large_context", "multilingual", "vision"],
        "cost": 5,
        "speed": 6,
        "quality": 9,
        "context_window": 1000000,
        "strengths": ["coding", "analysis", "math", "general"],
    },
    "google/gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash",
        "provider": "google",
        "model_id": "gemini-2.5-flash",
        "traits": ["fast_response", "large_context", "multilingual", "vision"],
        "cost": 2,
        "speed": 9,
        "quality": 7,
        "context_window": 1000000,
        "strengths": ["qa", "conversation", "general", "summarization", "translation"],
    },
    "google/gemini-2.0-flash": {
        "name": "Gemini 2.0 Flash",
        "provider": "google",
        "model_id": "gemini-2.0-flash",
        "traits": ["fast_response", "large_context", "multilingual", "vision"],
        "cost": 1,
        "speed": 10,
        "quality": 6,
        "context_window": 1000000,
        "strengths": ["qa", "conversation", "general", "summarization"],
    },
}


class ModelProfileManager:
    """モデルプロファイルの管理・スコアリング。

    シングルトンではない。ModelRouter が単一インスタンスを保持する想定。
    """

    def __init__(self):
        self._profiles = copy.deepcopy(_DEFAULT_PROFILES)
        self._custom_profiles = {}

    def get_profile(self, model_key):
        """model_key (e.g. "openai/gpt-4o") のプロファイルを返す。

        カスタムプロファイルが優先される。
        見つからなければ None。

        Parameters
        ----------
        model_key : str

        Returns
        -------
        dict | None
        """
        if model_key in self._custom_profiles:
            return copy.deepcopy(self._custom_profiles[model_key])
        if model_key in self._profiles:
            return copy.deepcopy(self._profiles[model_key])
        return None

    def list_profiles(self):
        """全プロファイルのリストを返す。

        Returns
        -------
        list[dict]
            各要素に "key" フィールドを含む。
        """
        result = []
        seen = set()
        for key, profile in self._custom_profiles.items():
            entry = copy.deepcopy(profile)
            entry["key"] = key
            entry["custom"] = True
            result.append(entry)
            seen.add(key)
        for key, profile in self._profiles.items():
            if key not in seen:
                entry = copy.deepcopy(profile)
                entry["key"] = key
                entry["custom"] = False
                result.append(entry)
        return result

    def add_custom_profile(self, key, profile_data):
        """カスタムプロファイルを追加/更新する。

        Parameters
        ----------
        key : str
            モデルキー (e.g. "openai/gpt-4o" or "custom/my-model")
        profile_data : dict
            プロファイル情報。
        """
        self._custom_profiles[key] = copy.deepcopy(profile_data)

    def remove_custom_profile(self, key):
        """カスタムプロファイルを削除する。

        Parameters
        ----------
        key : str
        """
        self._custom_profiles.pop(key, None)

    def update_custom_profile(self, key, updates):
        """カスタムプロファイルを部分更新する。

        Parameters
        ----------
        key : str
        updates : dict
            更新するフィールド。
        """
        if key in self._custom_profiles:
            self._custom_profiles[key].update(updates)
        elif key in self._profiles:
            # デフォルトを元にカスタム化
            self._custom_profiles[key] = copy.deepcopy(self._profiles[key])
            self._custom_profiles[key].update(updates)

    def score_model(self, model_key, analysis_result, speed_preference="balanced"):
        """分析結果に基づいてモデルをスコアリングする。

        Parameters
        ----------
        model_key : str
        analysis_result : dict
            task_analyzer の出力。
        speed_preference : str
            "fast", "balanced", "heavy" のいずれか。

        Returns
        -------
        float
            0.0 ~ 100.0 のスコア。
        """
        profile = self.get_profile(model_key)
        if profile is None:
            return 0.0

        score = 0.0

        # 1. タスク種類マッチ (最大 30 点)
        task_type = analysis_result.get("task_type", "general")
        strengths = profile.get("strengths", [])
        if task_type in strengths:
            score += 30.0
        elif "general" in strengths:
            score += 15.0

        # 2. 特性マッチ (最大 25 点)
        recommended_traits = analysis_result.get("recommended_traits", [])
        model_traits = profile.get("traits", [])
        if recommended_traits:
            matched = sum(1 for t in recommended_traits if t in model_traits)
            trait_score = (matched / len(recommended_traits)) * 25.0
            score += trait_score

        # 3. 品質スコア (最大 20 点)
        quality = profile.get("quality", 5)
        score += (quality / 10.0) * 20.0

        # 4. 速度/コスト (最大 15 点)
        speed = profile.get("speed", 5)
        cost = profile.get("cost", 5)
        if speed_preference == "fast":
            score += (speed / 10.0) * 12.0
            score += ((10 - cost) / 10.0) * 3.0
        elif speed_preference == "heavy":
            score += (speed / 10.0) * 3.0
            score += (quality / 10.0) * 12.0
        else:
            score += (speed / 10.0) * 7.5
            score += ((10 - cost) / 10.0) * 7.5

        # 5. コンテキスト適合 (最大 10 点)
        context_tokens = analysis_result.get("context_tokens_estimate", 0)
        context_window = profile.get("context_window", 128000)
        if context_tokens > 0:
            if context_tokens > context_window:
                score -= 50.0  # コンテキスト超過ペナルティ
            elif context_tokens > context_window * 0.8:
                score += 3.0
            else:
                score += 10.0
        else:
            score += 10.0

        # 6. 画像対応ボーナス
        if analysis_result.get("has_images") and "vision" in model_traits:
            score += 10.0
        elif analysis_result.get("has_images") and "vision" not in model_traits:
            score -= 30.0  # vision 非対応ペナルティ

        return max(0.0, min(100.0, score))

    def get_available_models(self, client):
        """AIClient に登録されているプロバイダーに対応するプロファイルのキーリストを返す。

        Parameters
        ----------
        client : AIClient

        Returns
        -------
        list[str]
        """
        available = []
        provider_names = set(client._providers.keys()) - {"stub", "rumi"}
        all_profiles = {}
        all_profiles.update(self._profiles)
        all_profiles.update(self._custom_profiles)
        for key, profile in all_profiles.items():
            provider = profile.get("provider", "")
            if provider in provider_names:
                available.append(key)
        return available

    def export_profiles(self):
        """全プロファイルを JSON シリアライズ可能な dict で返す。"""
        return {
            "defaults": copy.deepcopy(self._profiles),
            "custom": copy.deepcopy(self._custom_profiles),
        }

    def import_custom_profiles(self, data):
        """カスタムプロファイルを一括インポートする。

        Parameters
        ----------
        data : dict
            {key: profile_data} 形式。
        """
        for key, profile_data in data.items():
            self._custom_profiles[key] = copy.deepcopy(profile_data)
