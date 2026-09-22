import time
import copy
import uuid
import json
import os
import re
import base64
import tempfile
import threading
import errno
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from domain.ai_client.inline_reasoning import split_inline_reasoning
from domain.chat.icon_matcher import match_icon

DEFAULT_CHAT_MODEL = "stub/default"
_SEARCH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+|[\u3040-\u30ff\u3400-\u9fff]+")
_SEARCH_JA_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _default_conversation_model(settings_path=None):
    try:
        path = settings_path or Path(__file__).resolve().parents[2] / "user_data" / "shared" / "frontend_settings.json"
        settings = json.loads(Path(path).read_text(encoding="utf-8"))
        preferred_model = settings.get("models", {}).get("preferred_model")
        if isinstance(preferred_model, str) and preferred_model.strip():
            return preferred_model.strip()
    except Exception:
        pass
    if settings_path is not None:
        return DEFAULT_CHAT_MODEL
    try:
        from domain.ai_client.profile_loader import ProfileLoader

        profile = ProfileLoader().get("default") or {}
        provider = profile.get("provider")
        model = profile.get("model")
        if provider and model:
            candidate = "{}/{}".format(provider, model)
            if candidate.startswith(("ollama/", "lmstudio/", "vllm/", "llamacpp/", "stub/")):
                return candidate
    except Exception:
        pass
    return DEFAULT_CHAT_MODEL


def _gen_id():
    return str(uuid.uuid4())


def _now_ms():
    return int(time.time() * 1000)


class ChatStore:
    _instance = None

    def __new__(cls):
        storage_path = cls._default_storage_path()
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._storage_path = storage_path
            cls._instance._lock = threading.RLock()
            cls._instance._conversations = cls._instance._load_conversations()
            cls._instance._loaded_storage_signature = cls._instance._storage_signature()
            if cls._instance._conversations:
                try:
                    cls._instance._save_conversation_files()
                except OSError:
                    pass
        elif cls._instance._storage_path != storage_path:
            cls._instance._storage_path = storage_path
            cls._instance._conversations = cls._instance._load_conversations()
            cls._instance._loaded_storage_signature = cls._instance._storage_signature()
            if cls._instance._conversations:
                try:
                    cls._instance._save_conversation_files()
                except OSError:
                    pass
        return cls._instance

    @staticmethod
    def _default_storage_path():
        override = os.environ.get("RUMI_DEFAULTSPACK_CHAT_STORE_PATH")
        if override:
            return Path(override)
        return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "chat" / "conversations.json"

    def _storage_signature(self):
        try:
            stat = self._storage_path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _refresh_if_storage_changed(self):
        with self._lock:
            current_signature = self._storage_signature()
            if getattr(self, "_loaded_storage_signature", None) == current_signature:
                return
            self._conversations = self._load_conversations()
            self._loaded_storage_signature = current_signature

    def _load_conversations(self):
        with self._lock:
            try:
                data = json.loads(self._storage_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except Exception:
                return {}
            conversations = data.get("conversations") if isinstance(data, dict) else data
            if not isinstance(conversations, dict):
                return {}
            loaded = {}
            for conversation_id, conversation in conversations.items():
                if not isinstance(conversation, dict):
                    continue
                self._normalize_conversation(str(conversation_id), conversation)
                self._sanitize_inline_thought_messages(conversation)
                loaded[str(conversation_id)] = conversation
            return loaded

    def _atomic_write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_atomic_file(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _is_transient_replace_error(exc):
        winerror = getattr(exc, "winerror", None)
        errno_value = getattr(exc, "errno", None)
        if isinstance(exc, PermissionError):
            return True
        if winerror in {5, 32}:
            return True
        if errno_value in {errno.EACCES, errno.EBUSY, errno.EPERM}:
            return True
        message = str(exc).lower()
        return "access is denied" in message or "permission denied" in message

    def _replace_atomic_file(self, tmp_path, path):
        last_error = None
        for attempt in range(8):
            try:
                tmp_path.replace(path)
                return
            except OSError as exc:
                last_error = exc
                if not self._is_transient_replace_error(exc) or attempt >= 7:
                    break
                time.sleep(min(0.05 * (2 ** attempt), 0.5))
        raise last_error

    def _save_conversations(self):
        with self._lock:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            for conversation in self._conversations.values():
                if isinstance(conversation, dict):
                    self._sanitize_inline_thought_messages(conversation)
            payload = {
                "schema_version": 1,
                "updated_at": _now_ms(),
                "conversations": self._conversations,
            }
            self._save_conversation_files()
            self._save_external_conversation_indexes()
            try:
                self._atomic_write_json(self._storage_path, payload)
                self._loaded_storage_signature = self._storage_signature()
            except OSError as exc:
                if not self._is_transient_replace_error(exc):
                    raise

    # ----------------------------------------------------------
    # Conversation CRUD
    # ----------------------------------------------------------
    def create_conversation(
        self,
        model=None,
        system_prompt_id=None,
        agent_id=None,
        tags=None,
        parent_conversation_id=None,
        conversation_kind=None,
        metadata=None,
        group_id=None,
    ):
        with self._lock:
            self._refresh_if_storage_changed()
            cid = _gen_id()
            now = _now_ms()
            parent_id = str(parent_conversation_id) if parent_conversation_id else None
            metadata_dict = dict(metadata) if isinstance(metadata, dict) else {}
            self._set_metadata_icon(metadata_dict, "New Conversation", cid)
            conv = {
                "id": cid,
                "title": "New Conversation",
                "created_at": now,
                "updated_at": now,
                "model": model if model else _default_conversation_model(),
                "system_prompt_id": system_prompt_id,
                "agent_id": agent_id,
                "tags": tags if tags is not None else [],
                "is_starred": False,
                "is_pinned": False,
                "pinned_at": None,
                "pin_scope": "global",
                "is_archived": False,
                "current_node_id": None,
                "parent_conversation_id": parent_id,
                "child_conversation_ids": [],
                "conversation_kind": conversation_kind or ("subagent" if parent_id else "chat"),
                "group_id": group_id,
                "metadata": metadata_dict,
                "messages": [],
            }
            self._conversations[cid] = conv
            if parent_id and parent_id in self._conversations:
                parent = self._conversations[parent_id]
                self._normalize_conversation(parent_id, parent)
                if cid not in parent["child_conversation_ids"]:
                    parent["child_conversation_ids"].append(cid)
                parent["updated_at"] = now
            self._save_conversations()
            return copy.deepcopy(conv)

    def get_conversation(self, conversation_id):
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        return copy.deepcopy(conv)

    def list_conversations(
        self,
        limit=50,
        offset=0,
        tag=None,
        tags=None,
        is_starred=None,
        is_pinned=None,
        is_archived=None,
        company_id=None,
        workspace_id=None,
        conversation_kind=None,
        group_id=None,
        query=None,
        include_messages=False,
    ):
        self._refresh_if_storage_changed()
        filter_tags = self._normalize_filter_tags(tags)
        query_text = str(query or "").strip().casefold()
        results = []
        for conv in self._conversations.values():
            if not isinstance(conv, dict):
                continue
            self._normalize_conversation(str(conv.get("id") or ""), conv)
            metadata = conv.get("metadata") if isinstance(conv.get("metadata"), dict) else {}
            if metadata.get("hidden") is True:
                continue
            if tag is not None and tag not in conv.get("tags", []):
                continue
            if filter_tags and not all(item in conv.get("tags", []) for item in filter_tags):
                continue
            if is_starred is not None and conv.get("is_starred") != is_starred:
                continue
            if is_pinned is not None and conv.get("is_pinned") != is_pinned:
                continue
            if is_archived is not None and conv.get("is_archived") != is_archived:
                continue
            if not self._conversation_field_matches(conv, "company_id", company_id):
                continue
            if not self._conversation_field_matches(conv, "workspace_id", workspace_id):
                continue
            if conversation_kind is not None and str(conv.get("conversation_kind") or "") != str(conversation_kind):
                continue
            if group_id is not None and str(conv.get("group_id") or "") != str(group_id):
                continue
            if query_text and not self._conversation_matches_query(conv, query_text, include_messages=include_messages):
                continue
            if include_messages:
                results.append(copy.deepcopy(conv))
            else:
                results.append(self._conversation_list_summary(conv))
        results.sort(key=self._conversation_list_sort_key, reverse=True)
        total = len(results)
        page = results[offset: offset + limit]
        return page, total

    @staticmethod
    def _conversation_list_summary(conversation):
        messages = conversation.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        message_count = len(messages)
        last_message_preview = ""
        if messages:
            last_message = messages[-1]
            if isinstance(last_message, dict):
                last_message_preview = str(
                    last_message.get("raw_text")
                    or ChatStore._extract_raw_text(last_message.get("content", []))
                ).strip()
            else:
                last_message_preview = str(last_message)
        return {
            **{
                key: copy.deepcopy(value) for key, value in conversation.items()
                if key != "messages"
            },
            "messages": [],
            "message_count": message_count,
            "last_message_preview": last_message_preview,
        }

    def update_conversation(self, conversation_id, updates):
        with self._lock:
            self._refresh_if_storage_changed()
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return None
            protected = {"id", "created_at", "messages"}
            for key, value in updates.items():
                if key not in protected:
                    conv[key] = value

            if "title" in updates or "metadata" in updates:
                if not isinstance(conv.get("metadata"), dict):
                    conv["metadata"] = {}
                self._set_metadata_icon(conv["metadata"], conv.get("title") or "New Conversation", conversation_id)

            conv["updated_at"] = _now_ms()
            self._save_conversation_file(conversation_id, conv)
            self._save_conversation_index()
            return copy.deepcopy(conv)

    def delete_conversation(self, conversation_id):
        with self._lock:
            self._refresh_if_storage_changed()
            if conversation_id in self._conversations:
                conv = self._conversations[conversation_id]
                parent_id = conv.get("parent_conversation_id") if isinstance(conv, dict) else None
                if parent_id in self._conversations:
                    parent = self._conversations[parent_id]
                    child_ids = parent.get("child_conversation_ids", [])
                    if isinstance(child_ids, list):
                        parent["child_conversation_ids"] = [cid for cid in child_ids if cid != conversation_id]
                        parent["updated_at"] = _now_ms()
                for candidate in self._conversations.values():
                    if isinstance(candidate, dict) and candidate.get("parent_conversation_id") == conversation_id:
                        candidate["parent_conversation_id"] = None
                del self._conversations[conversation_id]
                self._save_conversations()
                return True
            return False

    # ----------------------------------------------------------
    # Message CRUD
    # ----------------------------------------------------------
    def add_message(self, conversation_id, message_dict):
        with self._lock:
            self._refresh_if_storage_changed()
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return None
            msg = copy.deepcopy(message_dict)
            if "id" not in msg or msg["id"] is None:
                msg["id"] = _gen_id()
            msg["conversation_id"] = conversation_id
            if "parent_id" not in msg or msg["parent_id"] is None:
                msg["parent_id"] = conv["current_node_id"]
            if "children_ids" not in msg:
                msg["children_ids"] = []
            messages = conv.get("messages")
            if not isinstance(messages, list):
                messages = []
                conv["messages"] = messages
            self._normalize_message_sequence_numbers(messages)
            msg["sequence_number"] = len(messages) + 1
            if "created_at" not in msg or msg["created_at"] is None:
                msg["created_at"] = _now_ms()
            if "raw_text" not in msg or msg["raw_text"] is None:
                msg["raw_text"] = self._extract_raw_text(msg.get("content", []))
            for field in ("finish_reason", "usage", "widget", "metadata", "events", "tool_logs"):
                if field not in msg:
                    msg[field] = None
            parent_id = msg["parent_id"]
            if parent_id is not None:
                for m in conv["messages"]:
                    if m["id"] == parent_id:
                        if msg["id"] not in m["children_ids"]:
                            m["children_ids"].append(msg["id"])
                        break
            conv["messages"].append(msg)
            conv["current_node_id"] = msg["id"]
            conv["updated_at"] = _now_ms()
            self._save_conversations()
            self._persist_message_artifacts(conversation_id, msg)
            return copy.deepcopy(msg)

    def get_message(self, conversation_id, message_id):
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        for msg in conv["messages"]:
            if msg["id"] == message_id:
                return copy.deepcopy(msg)
        return None

    def update_message(self, conversation_id, message_id, updates):
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        protected = {"id", "conversation_id", "created_at"}
        for msg in conv["messages"]:
            if msg["id"] == message_id:
                for key, value in updates.items():
                    if key not in protected:
                        msg[key] = value
                conv["updated_at"] = _now_ms()
                self._save_conversations()
                self._persist_message_artifacts(conversation_id, msg)
                return copy.deepcopy(msg)
        return None

    def delete_message(self, conversation_id, message_id):
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return False
        target = None
        for msg in conv["messages"]:
            if msg["id"] == message_id:
                target = msg
                break
        if target is None:
            return False
        parent_id = target.get("parent_id")
        if parent_id is not None:
            for msg in conv["messages"]:
                if msg["id"] == parent_id:
                    if message_id in msg["children_ids"]:
                        msg["children_ids"].remove(message_id)
                    break
        conv["messages"] = [m for m in conv["messages"] if m["id"] != message_id]
        if conv["current_node_id"] == message_id:
            conv["current_node_id"] = parent_id
        conv["updated_at"] = _now_ms()
        self._save_conversations()
        return True

    # ----------------------------------------------------------
    # Message range / bulk operations (for summarize & trim)
    # ----------------------------------------------------------
    def get_messages_range(self, conversation_id, start_message_id, end_message_id):
        """start_message_id から end_message_id までの範囲のメッセージを返す。

        Returns:
            tuple(list[msg], int) — (範囲内メッセージのリスト, start のインデックス)
            None — start または end が見つからない場合
        """
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        messages = conv["messages"]
        start_idx = None
        end_idx = None
        for i, msg in enumerate(messages):
            if msg["id"] == start_message_id:
                start_idx = i
            if msg["id"] == end_message_id:
                end_idx = i
        if start_idx is None or end_idx is None:
            return None
        if start_idx > end_idx:
            start_idx, end_idx = end_idx, start_idx
        range_msgs = messages[start_idx:end_idx + 1]
        return [copy.deepcopy(m) for m in range_msgs], start_idx

    def delete_messages_bulk(self, conversation_id, message_ids):
        """複数メッセージを一括削除する。parent_id / children_ids の整合性を維持。

        Returns:
            int — 削除されたメッセージ数
        """
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return 0
        delete_set = set(message_ids)
        # 削除対象外メッセージの children_ids から削除対象を除去
        for msg in conv["messages"]:
            if msg["id"] not in delete_set:
                msg["children_ids"] = [
                    cid for cid in msg.get("children_ids", [])
                    if cid not in delete_set
                ]
        # 削除対象外メッセージの parent_id が削除対象を指す場合は None にする
        for msg in conv["messages"]:
            if msg["id"] not in delete_set:
                if msg.get("parent_id") in delete_set:
                    msg["parent_id"] = None
        original_count = len(conv["messages"])
        conv["messages"] = [m for m in conv["messages"] if m["id"] not in delete_set]
        deleted_count = original_count - len(conv["messages"])
        # current_node_id が削除対象の場合、残っているメッセージの最後に移動
        if conv["current_node_id"] in delete_set:
            if conv["messages"]:
                conv["current_node_id"] = conv["messages"][-1]["id"]
            else:
                conv["current_node_id"] = None
        conv["updated_at"] = _now_ms()
        self._save_conversations()
        return deleted_count

    def insert_message_at(self, conversation_id, message_dict, position_index,
                          parent_id=None, children_ids=None):
        """指定位置にメッセージを挿入し、parent_id / children_ids を接続する。

        Args:
            conversation_id: 会話ID
            message_dict: 挿入するメッセージ辞書
            position_index: 挿入位置（messagesリストのインデックス）
            parent_id: このメッセージの parent_id（明示指定）
            children_ids: このメッセージの children_ids（明示指定）

        Returns:
            挿入されたメッセージの deepcopy、または失敗時 None
        """
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        msg = copy.deepcopy(message_dict)
        if "id" not in msg or msg["id"] is None:
            msg["id"] = _gen_id()
        msg["conversation_id"] = conversation_id
        msg["parent_id"] = parent_id
        msg["children_ids"] = children_ids if children_ids is not None else []
        if "sequence_number" not in msg or msg["sequence_number"] is None:
            msg["sequence_number"] = position_index + 1
        if "created_at" not in msg or msg["created_at"] is None:
            msg["created_at"] = _now_ms()
        if "raw_text" not in msg or msg["raw_text"] is None:
            msg["raw_text"] = self._extract_raw_text(msg.get("content", []))
        for field in ("finish_reason", "usage", "widget"):
            if field not in msg:
                msg[field] = None
        # 親メッセージの children_ids にこのメッセージを追加
        if parent_id is not None:
            for m in conv["messages"]:
                if m["id"] == parent_id:
                    if msg["id"] not in m["children_ids"]:
                        m["children_ids"].append(msg["id"])
                    break
        # children のメッセージの parent_id をこのメッセージに更新
        if children_ids:
            for m in conv["messages"]:
                if m["id"] in children_ids:
                    m["parent_id"] = msg["id"]
        # 位置にクランプして挿入
        idx = max(0, min(position_index, len(conv["messages"])))
        conv["messages"].insert(idx, msg)
        # sequence_number を再採番
        for i, m in enumerate(conv["messages"]):
            m["sequence_number"] = i + 1
        conv["current_node_id"] = conv["messages"][-1]["id"]
        conv["updated_at"] = _now_ms()
        self._save_conversations()
        return copy.deepcopy(msg)

    # ----------------------------------------------------------
    # Search
    # ----------------------------------------------------------
    def search(self, query, conversation_id=None):
        self._refresh_if_storage_changed()
        results = []
        q_lower = query.lower()
        targets = {}
        if conversation_id is not None:
            conv = self._conversations.get(conversation_id)
            if conv is not None:
                targets[conversation_id] = conv
        else:
            targets = self._conversations
        for conv in targets.values():
            for msg in conv["messages"]:
                raw = msg.get("raw_text", "") or ""
                if q_lower in raw.lower():
                    results.append(copy.deepcopy(msg))
        return results

    def search_conversations(
        self,
        query,
        *,
        limit=20,
        offset=0,
        conversation_id=None,
        date_filter=None,
        is_starred=None,
        is_archived=None,
        role=None,
    ):
        self._refresh_if_storage_changed()
        text_query = str(query or "").strip()
        if not text_query:
            return [], 0
        query_lower = text_query.lower()
        query_vector = _search_vector(text_query)
        role_filter = str(role or "all").strip().lower()
        min_updated_at = _date_filter_floor_ms(str(date_filter or "all"), _now_ms())
        targets = {}
        if conversation_id is not None:
            conv = self._conversations.get(conversation_id)
            if conv is not None:
                targets[conversation_id] = conv
        else:
            targets = self._conversations

        results = []
        for conv in targets.values():
            if is_starred is not None and conv.get("is_starred") != is_starred:
                continue
            if is_archived is not None and conv.get("is_archived") != is_archived:
                continue
            if min_updated_at and int(conv.get("updated_at") or 0) < min_updated_at:
                continue
            title = str(conv.get("title") or "")
            title_exact = 1.0 if query_lower in title.lower() else 0.0
            title_semantic = _search_cosine(query_vector, _search_vector(title))
            matches = []
            for msg in conv.get("messages", []):
                msg_role = str(msg.get("role") or "").lower()
                if role_filter in {"user", "assistant"} and msg_role != role_filter:
                    continue
                raw = str(msg.get("raw_text") or self._extract_raw_text(msg.get("content", [])) or "")
                if not raw:
                    continue
                exact_score = 1.0 if query_lower in raw.lower() else 0.0
                semantic_score = _search_cosine(query_vector, _search_vector(raw))
                message_score = max(exact_score, semantic_score)
                if message_score < 0.08:
                    continue
                matches.append(
                    {
                        "message_id": msg.get("id"),
                        "role": msg.get("role"),
                        "created_at": msg.get("created_at"),
                        "snippet": _search_snippet(raw, text_query),
                        "exact": exact_score > 0,
                        "score": round(message_score, 4),
                    }
                )
            if not matches and title_exact <= 0 and title_semantic < 0.08:
                continue
            exact_score = max([title_exact, *(1.0 if item["exact"] else 0.0 for item in matches)], default=0.0)
            semantic_score = max([title_semantic, *(float(item["score"]) for item in matches)], default=0.0)
            score = (2.0 * exact_score) + semantic_score + min(len(matches), 4) * 0.05
            results.append(
                {
                    "conversation_id": conv.get("id"),
                    "title": title or "New Conversation",
                    "created_at": conv.get("created_at"),
                    "updated_at": conv.get("updated_at"),
                    "is_starred": bool(conv.get("is_starred")),
                    "is_archived": bool(conv.get("is_archived")),
                    "score": round(score, 4),
                    "exact_score": round(exact_score, 4),
                    "semantic_score": round(semantic_score, 4),
                    "match_count": len(matches),
                    "matches": sorted(matches, key=lambda item: (-float(item["score"]), -(int(item.get("created_at") or 0))))[:3],
                }
            )
        results.sort(key=lambda item: (-float(item["score"]), -(int(item.get("updated_at") or 0))))
        total = len(results)
        return copy.deepcopy(results[offset: offset + limit]), total

    # ----------------------------------------------------------
    # Branch
    # ----------------------------------------------------------
    def branch(self, conversation_id, message_id):
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        chain = self._get_chain(conv, message_id)
        if chain is None:
            return None
        new_conv = self.create_conversation(
            model=conv["model"],
            system_prompt_id=conv.get("system_prompt_id"),
            agent_id=conv.get("agent_id"),
            tags=list(conv.get("tags", [])),
        )
        new_conv_obj = self._conversations[new_conv["id"]]
        new_conv_obj["title"] = conv["title"] + " (branch)"
        old_to_new = {}
        for old_msg in chain:
            new_msg_id = _gen_id()
            old_to_new[old_msg["id"]] = new_msg_id
        for idx, old_msg in enumerate(chain):
            new_msg_id = old_to_new[old_msg["id"]]
            new_parent = None
            if old_msg["parent_id"] is not None and old_msg["parent_id"] in old_to_new:
                new_parent = old_to_new[old_msg["parent_id"]]
            new_msg = {
                "id": new_msg_id,
                "conversation_id": new_conv["id"],
                "parent_id": new_parent,
                "children_ids": [],
                "sequence_number": idx + 1,
                "role": old_msg["role"],
                "content": copy.deepcopy(old_msg.get("content", [])),
                "raw_text": old_msg.get("raw_text", ""),
                "created_at": old_msg.get("created_at", _now_ms()),
                "finish_reason": old_msg.get("finish_reason"),
                "usage": copy.deepcopy(old_msg.get("usage")),
                "widget": copy.deepcopy(old_msg.get("widget")),
            }
            if new_parent is not None:
                for m in new_conv_obj["messages"]:
                    if m["id"] == new_parent:
                        m["children_ids"].append(new_msg_id)
                        break
            new_conv_obj["messages"].append(new_msg)
            new_conv_obj["current_node_id"] = new_msg_id
        new_conv_obj["updated_at"] = _now_ms()
        self._save_conversations()
        return copy.deepcopy(new_conv_obj)

    # ----------------------------------------------------------
    # Export
    # ----------------------------------------------------------
    def export_conversation(self, conversation_id, fmt="markdown"):
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        from domain.chat.exporter import export_markdown, export_json, export_text
        conv_copy = copy.deepcopy(conv)
        normalized_fmt = str(fmt or "markdown").strip().lower()
        if normalized_fmt == "json":
            return export_json(conv_copy)
        if normalized_fmt in {"text", "txt"}:
            return export_text(conv_copy)
        return export_markdown(conv_copy)

    # ----------------------------------------------------------
    # Message chain helper
    # ----------------------------------------------------------
    def get_message_chain(self, conversation_id, up_to_message_id):
        self._refresh_if_storage_changed()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return []
        return self._get_chain(conv, up_to_message_id) or []

    def _get_chain(self, conv, message_id):
        msg_map = {m["id"]: m for m in conv["messages"]}
        if message_id not in msg_map:
            return None
        chain = []
        current_id = message_id
        while current_id is not None:
            msg = msg_map.get(current_id)
            if msg is None:
                break
            chain.append(copy.deepcopy(msg))
            current_id = msg.get("parent_id")
        chain.reverse()
        return chain

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------
    @staticmethod
    def _extract_raw_text(content_blocks):
        parts = []
        if not isinstance(content_blocks, list):
            return str(content_blocks) if content_blocks else ""
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)

    @staticmethod
    def _normalize_conversation(conversation_id, conversation):
        conversation.setdefault("id", conversation_id)
        conversation.setdefault("title", "New Conversation")
        conversation.setdefault("created_at", _now_ms())
        conversation.setdefault("updated_at", conversation.get("created_at", _now_ms()))
        conversation.setdefault("model", _default_conversation_model())
        conversation.setdefault("system_prompt_id", None)
        conversation.setdefault("agent_id", None)
        conversation.setdefault("tags", [])
        conversation.setdefault("is_starred", False)
        conversation.setdefault("is_pinned", False)
        conversation.setdefault("pinned_at", None)
        conversation.setdefault("pin_scope", "global")
        conversation.setdefault("is_archived", False)
        conversation.setdefault("current_node_id", None)
        conversation.setdefault("parent_conversation_id", None)
        conversation.setdefault("child_conversation_ids", [])
        conversation.setdefault("conversation_kind", "subagent" if conversation.get("parent_conversation_id") else "chat")
        conversation.setdefault("group_id", None)
        conversation.setdefault("metadata", {})
        conversation.setdefault("messages", [])
        conversation["is_starred"] = ChatStore._coerce_bool(conversation.get("is_starred"), False)
        conversation["is_pinned"] = ChatStore._coerce_bool(conversation.get("is_pinned"), False)
        conversation["is_archived"] = ChatStore._coerce_bool(conversation.get("is_archived"), False)
        if conversation.get("pinned_at") in ("", 0):
            conversation["pinned_at"] = None
        pin_scope = str(conversation.get("pin_scope") or "global").strip().lower()
        conversation["pin_scope"] = pin_scope if pin_scope in {"global", "group", "company"} else "global"
        if not isinstance(conversation.get("tags"), list):
            conversation["tags"] = []
        if not isinstance(conversation.get("child_conversation_ids"), list):
            conversation["child_conversation_ids"] = []
        if not isinstance(conversation.get("metadata"), dict):
            conversation["metadata"] = {}
        ChatStore._set_metadata_icon(
            conversation["metadata"],
            conversation.get("title") or "New Conversation",
            conversation.get("id") or conversation_id,
        )
        ChatStore._normalize_message_sequence_numbers(conversation.get("messages", []))

    @staticmethod
    def _normalize_message_sequence_numbers(messages: object) -> bool:
        if not isinstance(messages, list):
            return False
        needs_repair = False
        for index, message in enumerate(messages, start=1):
            if not isinstance(message, dict):
                continue
            sequence = message.get("sequence_number")
            if type(sequence) is not int or sequence != index:
                needs_repair = True
                break
        if not needs_repair:
            return False
        for index, message in enumerate(messages, start=1):
            if isinstance(message, dict):
                message["sequence_number"] = index
        return True

    @staticmethod
    def _set_metadata_icon(metadata, title, conversation_id):
        icon_info = match_icon(title or "New Conversation", conversation_id)
        metadata["icon_id"] = icon_info["icon_id"]
        metadata["icon_svg"] = icon_info["icon_svg"]

    @staticmethod
    def _normalize_filter_tags(tags):
        if tags is None:
            return []
        if isinstance(tags, str):
            raw_items = tags.split(",")
        elif isinstance(tags, (list, tuple, set)):
            raw_items = tags
        else:
            raw_items = [tags]
        normalized = []
        for item in raw_items:
            value = str(item or "").strip()
            if value:
                normalized.append(value)
        return normalized

    @staticmethod
    def _coerce_bool(value, default=False):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
        return default

    @staticmethod
    def _conversation_field_matches(conversation, field_name, expected):
        if expected is None:
            return True
        expected_text = str(expected)
        metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        values = [conversation.get(field_name), metadata.get(field_name)]
        return any(str(value) == expected_text for value in values if value is not None)

    def _conversation_matches_query(self, conversation, query_text, include_messages=False):
        metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        fields = [
            conversation.get("title"),
            " ".join(str(tag) for tag in conversation.get("tags", [])),
            metadata.get("workspace_label"),
            metadata.get("company_id"),
            metadata.get("workspace_id"),
        ]
        if include_messages:
            fields.extend(msg.get("raw_text") for msg in conversation.get("messages", []) if isinstance(msg, dict))
        return any(query_text in str(value or "").casefold() for value in fields)

    @staticmethod
    def _conversation_list_sort_key(conversation):
        updated_at = ChatStore._sort_timestamp(conversation.get("updated_at"))
        if conversation.get("is_pinned"):
            pinned_at = ChatStore._sort_timestamp(conversation.get("pinned_at"))
            if pinned_at <= 0:
                pinned_at = updated_at
            return (1, pinned_at, updated_at)
        return (0, updated_at, updated_at)

    @staticmethod
    def _sort_timestamp(value):
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except (TypeError, ValueError):
            return 0

    # ----------------------------------------------------------
    # Per-chat files / workspace artifacts
    # ----------------------------------------------------------
    @staticmethod
    def _normalize_rumi_data_path(value):
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if path.name == "chat" and path.parent.name == ".rumiDP":
            path = path.parent
        elif path.name != ".rumiDP":
            path = path / ".rumiDP"
        return path

    @staticmethod
    def _conversation_rumi_data_path(conversation):
        if not isinstance(conversation, dict):
            return None
        metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        for key in ("rumi_data_path", "rumiDataPath", "rumi_dp_path", "rumiDPPath"):
            data_path = ChatStore._normalize_rumi_data_path(metadata.get(key))
            if data_path is not None:
                return data_path
        return None

    def _conversation_storage_parent(self, conversation):
        data_path = self._conversation_rumi_data_path(conversation)
        if data_path is not None:
            return data_path / "chat"
        return self._storage_path.parent

    def conversation_dir(self, conversation_id):
        conversation = self._conversations.get(str(conversation_id))
        return self._conversation_storage_parent(conversation) / "conversations" / str(conversation_id)

    def conversation_workspace_dir(self, conversation_id):
        return self.conversation_dir(conversation_id) / "workspace"

    def persist_attachments(self, conversation_id, attachments):
        if not isinstance(attachments, list):
            return []
        refs = []
        attachment_dir = self.conversation_workspace_dir(conversation_id) / "attachments"
        attachment_dir.mkdir(parents=True, exist_ok=True)
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            if attachment.get("ephemeral") or attachment.get("do_not_persist") or attachment.get("no_persist"):
                continue
            name = self._safe_filename(str(attachment.get("name") or f"attachment-{index + 1}"))
            path = attachment_dir / name
            suffix = 1
            while path.exists():
                path = attachment_dir / f"{Path(name).stem}-{suffix}{Path(name).suffix}"
                suffix += 1
            written = False
            data_url = attachment.get("dataUrl") or attachment.get("data_url")
            if isinstance(data_url, str) and data_url.startswith("data:") and "," in data_url:
                try:
                    path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
                    written = True
                except Exception:
                    written = False
            if not written and isinstance(attachment.get("content"), str):
                path.write_text(attachment["content"], encoding="utf-8")
                written = True
            if not written:
                path.write_text(json.dumps(self._attachment_manifest(attachment), ensure_ascii=False, indent=2), encoding="utf-8")
            refs.append(
                {
                    "id": attachment.get("id"),
                    "name": attachment.get("name") or name,
                    "size": attachment.get("size"),
                    "type": attachment.get("type"),
                    "source": attachment.get("source"),
                    "sourcePath": attachment.get("sourcePath"),
                    "workspace_path": path.relative_to(self.conversation_dir(conversation_id)).as_posix(),
                }
            )
        try:
            from domain.chat.attachments.store import upsert_attachment_records

            upsert_attachment_records(self.conversation_workspace_dir(conversation_id), attachments, refs)
        except Exception:
            pass
        return refs

    def _save_conversation_files(self):
        with self._lock:
            for conversation_id, conversation in self._conversations.items():
                try:
                    self._save_conversation_file(str(conversation_id), conversation)
                except OSError as exc:
                    if not self._is_transient_replace_error(exc):
                        raise

    def _save_conversation_index(self):
        with self._lock:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            for conversation in self._conversations.values():
                if isinstance(conversation, dict):
                    self._sanitize_inline_thought_messages(conversation)
            payload = {
                "schema_version": 1,
                "updated_at": _now_ms(),
                "conversations": self._conversations,
            }
            self._atomic_write_json(self._storage_path, payload)
            self._loaded_storage_signature = self._storage_signature()

    def _save_conversation_file(self, conversation_id, conversation):
        with self._lock:
            conversation_dir = self.conversation_dir(conversation_id)
            (conversation_dir / "workspace" / "attachments").mkdir(parents=True, exist_ok=True)
            (conversation_dir / "workspace" / "tools").mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "updated_at": _now_ms(),
                "conversation": conversation,
            }
            self._atomic_write_json(conversation_dir / "history.json", payload)

    def _save_external_conversation_indexes(self):
        grouped = {}
        default_parent = self._storage_path.parent
        for conversation_id, conversation in self._conversations.items():
            if not isinstance(conversation, dict):
                continue
            parent = self._conversation_storage_parent(conversation)
            if parent == default_parent:
                continue
            grouped.setdefault(parent, {})[str(conversation_id)] = conversation
        for parent, conversations in grouped.items():
            payload = {
                "schema_version": 1,
                "updated_at": _now_ms(),
                "conversations": conversations,
            }
            self._atomic_write_json(parent / "conversations.json", payload)

    def _persist_message_artifacts(self, conversation_id, msg):
        if not isinstance(msg, dict):
            return
        if msg.get("role") == "assistant" and isinstance(msg.get("tool_logs"), list) and msg["tool_logs"]:
            tool_dir = self.conversation_workspace_dir(conversation_id) / "tools"
            tool_dir.mkdir(parents=True, exist_ok=True)
            path = tool_dir / "{}-tool_logs.json".format(self._safe_filename(str(msg.get("id") or "message")))
            path.write_text(json.dumps(msg["tool_logs"], ensure_ascii=False, indent=2), encoding="utf-8")

    def _sanitize_inline_thought_messages(self, conversation):
        for msg in conversation.get("messages", []) if isinstance(conversation.get("messages"), list) else []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            thoughts = []
            for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = str(block.get("text") or "")

                extracted, cleaned = split_inline_reasoning(text)
                thoughts.extend(extracted)
                if cleaned != text:
                    block["text"] = cleaned
            if thoughts:
                metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
                thinking = metadata.get("thinking") if isinstance(metadata.get("thinking"), dict) else {}
                metadata["thinking"] = {
                    **thinking,
                    "state": "completed",
                    "transcript": "\n\n".join(thoughts),
                    "source": "google_inline_thought",
                }
                msg["metadata"] = metadata
                msg["raw_text"] = self._extract_raw_text(msg.get("content", []))

    @staticmethod
    def _safe_filename(name):
        cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
        return cleaned[:160] or "attachment"

    @staticmethod
    def _attachment_manifest(attachment):
        return {
            key: attachment.get(key)
            for key in ("id", "name", "size", "type", "truncated", "source", "sourcePath")
            if isinstance(attachment, dict) and key in attachment
        }


def _search_vector(text):
    normalized = str(text or "").casefold()
    vector = Counter()
    for token in _SEARCH_TOKEN_RE.findall(normalized):
        token = token.strip(" \t\r\n.,!?()[]{}")
        if not token:
            continue
        vector[token] += 2
        if "_" in token:
            for part in token.split("_"):
                if part:
                    vector[part] += 1
        if _SEARCH_JA_RE.search(token):
            if len(token) <= 2:
                vector[token] += 1
            else:
                for index in range(len(token) - 1):
                    vector[token[index:index + 2]] += 1
    return vector


def _search_cosine(left, right):
    if not left or not right:
        return 0.0
    overlap = set(left) & set(right)
    numerator = sum(left[key] * right[key] for key in overlap)
    if numerator <= 0:
        return 0.0
    left_norm = sum(value * value for value in left.values()) ** 0.5
    right_norm = sum(value * value for value in right.values()) ** 0.5
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _search_snippet(text, query, radius=72):
    raw = str(text or "").replace("\n", " ").strip()
    if len(raw) <= radius * 2:
        return raw
    q_lower = str(query or "").lower()
    index = raw.lower().find(q_lower) if q_lower else -1
    if index < 0:
        return raw[: radius * 2].rstrip() + "..."
    start = max(0, index - radius)
    end = min(len(raw), index + len(q_lower) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(raw) else ""
    return prefix + raw[start:end].strip() + suffix


def _date_filter_floor_ms(date_filter, now_ms):
    normalized = str(date_filter or "all").strip().lower()
    if normalized in {"all", "", "any"}:
        return 0
    day_ms = 86_400_000
    if normalized in {"today", "24h", "day"}:
        return int(now_ms) - day_ms
    if normalized in {"7d", "week", "recent"}:
        return int(now_ms) - day_ms * 7
    if normalized in {"30d", "month"}:
        return int(now_ms) - day_ms * 30
    return 0
