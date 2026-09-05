"""
ConsentChecker — 回答テキストにセンシティブな内容が含まれるか判定し、
免責同意を管理するドメインクラス。

判定方式:
  1. キーワードベース判定（高速・常時利用可能）
  2. AI ベース判定（AIClient を利用、use_ai=True 時のみ）

カテゴリ:
  investment — 投資助言
  tax       — 税務アドバイス
  medical   — 医療情報
  legal     — 法律助言

統合ガイド（chat.send への組み込み方）:
  send.py の assistant_msg 生成後、call_handler("defaults.tool.consent_check", ...)
  を呼び出し、requires_consent=True なら emit_widget で免責ポップアップを送信。
  ユーザーが OK を押したら call_handler("defaults.tool.consent_confirm", ...)
  を呼び出して同意を記録し、回答を表示する。
"""

import json
import os
import sys
import threading
import time
import uuid


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from blocks.chat._prompt_helpers import build_content_classifier_prompt


# ======================================================================
# カテゴリ定義
# ======================================================================

CATEGORIES = {
    "investment": {
        "keywords": [
            "投資", "株", "株式", "株価", "銘柄", "ポートフォリオ",
            "資産運用", "利回り", "配当", "投資信託", "ファンド",
            "FX", "為替", "仮想通貨", "暗号資産", "ビットコイン",
            "ETF", "NISA", "iDeCo", "信用取引", "空売り",
            "investment", "stock", "portfolio", "dividend", "fund",
            "forex", "crypto", "bitcoin", "trading",
        ],
        "disclaimer": (
            "【免責事項 — 投資に関する情報】\n"
            "この回答は一般的な情報提供のみを目的としており、"
            "特定の金融商品の購入・売却を推奨するものではありません。\n"
            "投資判断はご自身の責任で行ってください。\n"
            "必要に応じて、資格を持つファイナンシャルアドバイザーにご相談ください。"
        ),
    },
    "tax": {
        "keywords": [
            "税金", "確定申告", "所得税", "住民税", "消費税",
            "法人税", "相続税", "贈与税", "控除", "節税",
            "税務", "年末調整", "源泉徴収", "経費", "減価償却",
            "tax", "deduction", "income tax", "tax return",
        ],
        "disclaimer": (
            "【免責事項 — 税務に関する情報】\n"
            "この回答は一般的な税務情報の提供を目的としており、"
            "個別の税務アドバイスではありません。\n"
            "具体的な税務判断については、税理士等の専門家にご相談ください。"
        ),
    },
    "medical": {
        "keywords": [
            "診断", "治療", "処方", "薬", "服薬", "投薬",
            "症状", "病気", "疾患", "手術", "副作用",
            "医療", "医師", "病院", "クリニック",
            "diagnosis", "treatment", "prescription", "medication",
            "symptom", "disease", "surgery", "side effect",
        ],
        "disclaimer": (
            "【免責事項 — 医療に関する情報】\n"
            "この回答は一般的な医療情報の提供を目的としており、"
            "医学的な診断・治療の代替となるものではありません。\n"
            "健康上の問題については、必ず医師にご相談ください。"
        ),
    },
    "legal": {
        "keywords": [
            "訴訟", "裁判", "弁護士", "法律相談", "契約書",
            "損害賠償", "慰謝料", "示談", "告訴", "起訴",
            "法的", "判例", "法令", "条文", "権利",
            "lawsuit", "attorney", "legal advice", "contract",
            "liability", "damages", "litigation",
        ],
        "disclaimer": (
            "【免責事項 — 法律に関する情報】\n"
            "この回答は一般的な法律情報の提供を目的としており、"
            "個別の法的アドバイスではありません。\n"
            "具体的な法律問題については、弁護士等の専門家にご相談ください。"
        ),
    },
}


# ======================================================================
# AI 判定用プロンプト
# ======================================================================

_AI_JUDGE_SYSTEM = build_content_classifier_prompt(
    list(CATEGORIES.keys()),
    field_name="categories",
    scope="consent",
)


# ======================================================================
# ConsentChecker
# ======================================================================

class ConsentChecker:
    """同意チェッカー（シングルトン）"""
    _instance = None
    _initialized: bool

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._consents = {}
        self._lock = threading.Lock()
        self._log_dir = self._resolve_log_dir()

    # ------------------------------------------------------------------
    # directory resolution
    # ------------------------------------------------------------------

    def _resolve_log_dir(self):
        """user_data/shared/consent_log/ のパスを解決し、なければ作成する"""
        base = os.path.dirname(os.path.abspath(__file__))
        pack_root = os.path.normpath(os.path.join(base, "..", ".."))
        log_dir = os.path.join(pack_root, "user_data", "shared", "consent_log")
        os.makedirs(log_dir, exist_ok=True)
        return log_dir

    # ------------------------------------------------------------------
    # keyword-based check
    # ------------------------------------------------------------------

    def check_keywords(self, text):
        """
        キーワードベースでテキストを判定する。
        戻り値: マッチしたカテゴリ名のリスト
        """
        if not text:
            return []
        text_lower = text.lower()
        matched = []
        for category_name, category_def in CATEGORIES.items():
            for kw in category_def["keywords"]:
                if kw.lower() in text_lower:
                    matched.append(category_name)
                    break
        return matched

    # ------------------------------------------------------------------
    # AI-based check
    # ------------------------------------------------------------------

    def check_ai(self, text, ai_client=None, model="stub/default"):
        """
        AIClient を使ってテキストを判定する。
        戻り値: マッチしたカテゴリ名のリスト
        """
        if ai_client is None:
            return []
        messages = [
            {"role": "system", "content": _AI_JUDGE_SYSTEM},
            {"role": "user", "content": text[:4000]},
        ]
        try:
            response = ai_client.complete(model, messages)
        except Exception:
            return []
        ai_text = ""
        if isinstance(response, dict):
            content = response.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        ai_text += block.get("text", "")
            elif isinstance(content, str):
                ai_text = content
        if not ai_text:
            return []
        try:
            parsed = json.loads(ai_text)
            categories = parsed.get("categories", [])
            valid = [c for c in categories if c in CATEGORIES]
            return valid
        except (json.JSONDecodeError, AttributeError):
            return []

    # ------------------------------------------------------------------
    # combined check
    # ------------------------------------------------------------------

    def check(self, text, use_ai=False, ai_client=None, model="stub/default"):
        """
        テキストを判定し、結果を返す。
        戻り値: {
            "requires_consent": bool,
            "categories": [str],
            "consent_id": str or None,
            "disclaimers": {category: disclaimer_text}
        }
        """
        categories = list(set(self.check_keywords(text)))
        if use_ai and ai_client is not None:
            ai_categories = self.check_ai(text, ai_client=ai_client, model=model)
            categories = list(set(categories + ai_categories))
        if not categories:
            return {
                "requires_consent": False,
                "categories": [],
                "consent_id": None,
                "disclaimers": {},
            }
        consent_id = str(uuid.uuid4())
        disclaimers = {}
        for cat in categories:
            if cat in CATEGORIES:
                disclaimers[cat] = CATEGORIES[cat]["disclaimer"]
        with self._lock:
            self._consents[consent_id] = {
                "consent_id": consent_id,
                "categories": categories,
                "accepted": False,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "accepted_at": None,
            }
        return {
            "requires_consent": True,
            "categories": categories,
            "consent_id": consent_id,
            "disclaimers": disclaimers,
        }

    # ------------------------------------------------------------------
    # consent confirmation
    # ------------------------------------------------------------------

    def confirm(self, consent_id, accepted):
        """
        同意を記録する。
        戻り値: {
            "consent_id": str,
            "accepted": bool,
            "accepted_at": str or None,
        }
        None を返す場合は consent_id が見つからない。
        """
        with self._lock:
            record = self._consents.get(consent_id)
            if record is None:
                return None
            record["accepted"] = accepted
            if accepted:
                record["accepted_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
            else:
                record["accepted_at"] = None
        self._persist(record)
        return {
            "consent_id": record["consent_id"],
            "accepted": record["accepted"],
            "accepted_at": record["accepted_at"],
        }

    def get(self, consent_id):
        """同意レコードを取得する。見つからなければ None。"""
        with self._lock:
            return self._consents.get(consent_id)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _persist(self, record):
        """同意記録を JSON ファイルに永続化する"""
        fpath = os.path.join(
            self._log_dir, record["consent_id"] + ".json"
        )
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(
                "consent._persist: failed to write "
                + fpath + ": " + str(exc),
                file=sys.stderr,
            )
