"""
DisclaimerManager — 免責カテゴリの管理、テキスト分類、同意要求/受理/拒否、
同意ログの永続化を担うドメインクラス。

既存の domain/tool/consent.py とは独立して動作する。
ConsentChecker が「テキスト中のセンシティブ判定+同意管理」を行うのに対し、
DisclaimerManager は「免責カテゴリ CRUD」「テキスト分類」「回答ブロック→
同意後リリース」「同意ログ一覧取得」の完全なライフサイクルを管理する。

カテゴリ永続化: user_data/shared/disclaimer_categories/ 配下に
  <name>.json として保存。

同意ログ永続化: user_data/shared/disclaimer_log/ 配下に
  <consent_id>.json として保存。

ブロック中の回答はメモリ上の _pending dict に保持し、
accept/reject で消費される。
"""

import json
import os
import sys
import threading
import time
import uuid
from typing import Any


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from blocks.chat._prompt_helpers import build_content_classifier_prompt


# ======================================================================
# デフォルトカテゴリ定義（初回起動時に永続化される）
# ======================================================================

_DEFAULT_CATEGORIES = {
    "investment": {
        "name": "investment",
        "label": "投資助言",
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
        "builtin": True,
    },
    "tax": {
        "name": "tax",
        "label": "税法アドバイス",
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
        "builtin": True,
    },
    "medical": {
        "name": "medical",
        "label": "医療アドバイス",
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
        "builtin": True,
    },
    "legal": {
        "name": "legal",
        "label": "法務アドバイス",
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
        "builtin": True,
    },
}


# ======================================================================
# AI 分類用プロンプト
# ======================================================================

_AI_CLASSIFY_SYSTEM = build_content_classifier_prompt(
    list(_DEFAULT_CATEGORIES.keys()),
    field_name="detected",
    scope="disclaimer",
)


# ======================================================================
# DisclaimerManager
# ======================================================================

class DisclaimerManager:
    """免責カテゴリ管理・テキスト分類・同意フロー管理（シングルトン）"""

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
        self._lock = threading.Lock()
        self._pending = {}  # consent_id -> {text, categories, created_at, ...}
        self._categories_dir = self._resolve_dir("disclaimer_categories")
        self._log_dir = self._resolve_dir("disclaimer_log")
        self._ensure_default_categories()

    # ------------------------------------------------------------------
    # directory helpers
    # ------------------------------------------------------------------

    def _resolve_dir(self, subdir):
        """user_data/shared/<subdir>/ のパスを解決し、なければ作成する。"""
        base = os.path.dirname(os.path.abspath(__file__))
        pack_root = os.path.normpath(os.path.join(base, "..", ".."))
        target = os.path.join(pack_root, "user_data", "shared", subdir)
        os.makedirs(target, exist_ok=True)
        return target

    # ------------------------------------------------------------------
    # default category seeding
    # ------------------------------------------------------------------

    def _ensure_default_categories(self):
        """デフォルトカテゴリが未保存ならファイルに書き込む。"""
        for name, cat_def in _DEFAULT_CATEGORIES.items():
            fpath = os.path.join(self._categories_dir, name + ".json")
            if not os.path.exists(fpath):
                self._write_json(fpath, cat_def)

    # ------------------------------------------------------------------
    # JSON I/O
    # ------------------------------------------------------------------

    def _write_json(self, fpath, data):
        """JSON ファイルに書き込む。失敗時は stderr に出力。"""
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(
                "disclaimer_manager._write_json: failed to write "
                + fpath + ": " + str(exc),
                file=sys.stderr,
            )

    def _read_json(self, fpath):
        """JSON ファイルを読み込む。失敗時は None。"""
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------
    # Category CRUD
    # ------------------------------------------------------------------

    def list_categories(self):
        """全カテゴリを返す。戻り値: [category_dict, ...]"""
        result: list[dict[str, Any]] = []
        try:
            entries = os.listdir(self._categories_dir)
        except OSError:
            return result
        for entry in sorted(entries):
            if entry.endswith(".json"):
                data = self._read_json(
                    os.path.join(self._categories_dir, entry)
                )
                if data is not None:
                    result.append(data)
        return result

    def get_category(self, name):
        """名前でカテゴリを取得。見つからなければ None。"""
        fpath = os.path.join(self._categories_dir, name + ".json")
        return self._read_json(fpath)

    def create_category(self, name, label, keywords, disclaimer):
        """
        カスタムカテゴリを作成する。
        戻り値: 作成されたカテゴリ dict。
        既に存在する場合は None を返す。
        """
        if not name or not isinstance(name, str):
            return None
        fpath = os.path.join(self._categories_dir, name + ".json")
        if os.path.exists(fpath):
            return None
        cat = {
            "name": name,
            "label": label or name,
            "keywords": keywords if isinstance(keywords, list) else [],
            "disclaimer": disclaimer or "",
            "builtin": False,
        }
        self._write_json(fpath, cat)
        return cat

    def update_category(self, name, label=None, keywords=None, disclaimer=None):
        """
        カテゴリを更新する。
        戻り値: 更新後のカテゴリ dict。見つからなければ None。
        """
        fpath = os.path.join(self._categories_dir, name + ".json")
        existing = self._read_json(fpath)
        if existing is None:
            return None
        if label is not None:
            existing["label"] = label
        if keywords is not None:
            existing["keywords"] = keywords if isinstance(keywords, list) else existing["keywords"]
        if disclaimer is not None:
            existing["disclaimer"] = disclaimer
        self._write_json(fpath, existing)
        return existing

    def delete_category(self, name):
        """
        カテゴリを削除する。
        戻り値: 削除成功なら True、見つからなければ False。
        builtin カテゴリも削除可能（ユーザー判断に任せる）。
        """
        fpath = os.path.join(self._categories_dir, name + ".json")
        if not os.path.exists(fpath):
            return False
        try:
            os.remove(fpath)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify(self, text, use_ai=False, ai_client=None, model="stub/default"):
        """
        テキストを分析し、該当する免責カテゴリを検出する。

        戻り値: {
            "detected_categories": [str],
            "disclaimers": {category_name: disclaimer_text}
        }
        """
        if not text or not isinstance(text, str):
            return {"detected_categories": [], "disclaimers": {}}

        categories = self.list_categories()
        text_lower = text.lower()

        # キーワードベース判定
        matched = []
        for cat in categories:
            cat_name = cat.get("name", "")
            kw_list = cat.get("keywords", [])
            for kw in kw_list:
                if kw.lower() in text_lower:
                    matched.append(cat_name)
                    break

        # AI ベース判定（オプション）
        if use_ai and ai_client is not None:
            ai_detected = self._classify_ai(text, categories, ai_client, model)
            matched = list(set(matched + ai_detected))

        # disclaimers 構築
        disclaimers = {}
        cat_map = {c["name"]: c for c in categories}
        for cat_name in matched:
            cat_def = cat_map.get(cat_name)
            if cat_def:
                disclaimers[cat_name] = cat_def.get("disclaimer", "")

        return {
            "detected_categories": sorted(matched),
            "disclaimers": disclaimers,
        }

    def _classify_ai(self, text, categories, ai_client, model):
        """AI を使ってテキストを分類する。戻り値: カテゴリ名リスト。"""
        cat_names = [c["name"] for c in categories]
        user_prompt = (
            "Categories: " + json.dumps(cat_names) + "\n\n"
            "Text to classify:\n" + text[:4000]
        )
        messages = [
            {"role": "system", "content": _AI_CLASSIFY_SYSTEM},
            {"role": "user", "content": user_prompt},
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
            detected = parsed.get("detected", [])
            valid = [c for c in detected if c in [cat["name"] for cat in categories]]
            return valid
        except (json.JSONDecodeError, AttributeError):
            return []

    # ------------------------------------------------------------------
    # Consent flow: require → accept / reject
    # ------------------------------------------------------------------

    def require_consent(self, text, detected_categories, disclaimers):
        """
        検出されたカテゴリに基づいて同意要求を生成し、回答をブロック（保持）する。

        戻り値: {
            "consent_id": str,
            "categories": [str],
            "disclaimers": {cat: text},
            "created_at": str,
        }
        """
        consent_id = str(uuid.uuid4())
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        with self._lock:
            self._pending[consent_id] = {
                "consent_id": consent_id,
                "blocked_text": text,
                "categories": list(detected_categories),
                "disclaimers": dict(disclaimers),
                "created_at": created_at,
            }

        return {
            "consent_id": consent_id,
            "categories": list(detected_categories),
            "disclaimers": dict(disclaimers),
            "created_at": created_at,
        }

    def accept(self, consent_id):
        """
        同意を受理する。保持されていた回答テキストを返す。

        戻り値: {
            "consent_id": str,
            "accepted": True,
            "text": str,
            "accepted_at": str,
            "categories": [str],
        }
        見つからなければ None。
        """
        accepted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        with self._lock:
            pending = self._pending.pop(consent_id, None)

        if pending is None:
            return None

        log_record = {
            "consent_id": consent_id,
            "action": "accepted",
            "categories": pending["categories"],
            "created_at": pending["created_at"],
            "resolved_at": accepted_at,
        }
        self._persist_log(log_record)

        return {
            "consent_id": consent_id,
            "accepted": True,
            "text": pending["blocked_text"],
            "accepted_at": accepted_at,
            "categories": pending["categories"],
        }

    def reject(self, consent_id):
        """
        同意を拒否する。保持されていた回答テキストを破棄する。

        戻り値: {
            "consent_id": str,
            "accepted": False,
            "rejected_at": str,
            "categories": [str],
        }
        見つからなければ None。
        """
        rejected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        with self._lock:
            pending = self._pending.pop(consent_id, None)

        if pending is None:
            return None

        log_record = {
            "consent_id": consent_id,
            "action": "rejected",
            "categories": pending["categories"],
            "created_at": pending["created_at"],
            "resolved_at": rejected_at,
        }
        self._persist_log(log_record)

        return {
            "consent_id": consent_id,
            "accepted": False,
            "rejected_at": rejected_at,
            "categories": pending["categories"],
        }

    # ------------------------------------------------------------------
    # Consent log
    # ------------------------------------------------------------------

    def _persist_log(self, record):
        """同意ログを JSON ファイルに永続化する。"""
        fpath = os.path.join(self._log_dir, record["consent_id"] + ".json")
        self._write_json(fpath, record)

    def list_log(self, limit=100, offset=0):
        """
        同意ログの一覧を取得する。新しい順。

        戻り値: {
            "entries": [log_record, ...],
            "total": int,
        }
        """
        entries = []
        try:
            filenames = os.listdir(self._log_dir)
        except OSError:
            return {"entries": [], "total": 0}

        json_files = [f for f in filenames if f.endswith(".json")]

        # ファイルの最終更新時刻で降順ソート
        json_files_with_mtime = []
        for fname in json_files:
            fpath = os.path.join(self._log_dir, fname)
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                mtime = 0.0
            json_files_with_mtime.append((mtime, fname))

        json_files_with_mtime.sort(key=lambda x: x[0], reverse=True)
        total = len(json_files_with_mtime)

        sliced = json_files_with_mtime[offset:offset + limit]
        for _mtime, fname in sliced:
            data = self._read_json(os.path.join(self._log_dir, fname))
            if data is not None:
                entries.append(data)

        return {"entries": entries, "total": total}
