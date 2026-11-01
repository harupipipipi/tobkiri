"""
domain/agent/inter_agent_comm.py — エージェント間通信

質問/回答、指示、報告の非同期メッセージングを提供する。
"""

import copy
import threading


from blocks._common import gen_id, timestamp


# ---------------------------------------------------------------------------
# メッセージ種別
# ---------------------------------------------------------------------------

MESSAGE_TYPES = ("question", "answer", "instruction", "report")
MESSAGE_PRIORITIES = ("low", "normal", "high", "urgent")
MESSAGE_STATUSES = ("pending", "delivered", "read", "answered", "expired")


# ---------------------------------------------------------------------------
# CommMessage
# ---------------------------------------------------------------------------

class CommMessage:
    """エージェント間通信メッセージ。"""

    def __init__(self, msg_id, org_id, sender_agent_id, receiver_agent_id,
                 message_type, content, priority="normal",
                 reference_id=None, metadata=None):
        self.msg_id = msg_id
        self.org_id = org_id
        self.sender_agent_id = sender_agent_id
        self.receiver_agent_id = receiver_agent_id
        self.message_type = message_type
        self.content = content
        self.priority = priority
        self.status = "pending"
        self.reference_id = reference_id
        self.metadata = metadata if metadata else {}
        self.response = None
        self.created_at = timestamp()
        self.updated_at = timestamp()

    def to_dict(self):
        return {
            "msg_id": self.msg_id,
            "org_id": self.org_id,
            "sender_agent_id": self.sender_agent_id,
            "receiver_agent_id": self.receiver_agent_id,
            "message_type": self.message_type,
            "content": self.content,
            "priority": self.priority,
            "status": self.status,
            "reference_id": self.reference_id,
            "metadata": copy.deepcopy(self.metadata),
            "response": self.response,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# InterAgentComm — シングルトン
# ---------------------------------------------------------------------------

class InterAgentComm:
    """エージェント間通信を管理するシングルトン。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._messages = {}
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._data_lock = threading.Lock()

    def send_question(self, org_id, sender_agent_id, receiver_agent_id,
                      content, priority="normal", metadata=None):
        """質問メッセージを送信する。"""
        msg_id = "comm_q_" + gen_id()
        msg = CommMessage(
            msg_id=msg_id,
            org_id=org_id,
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            message_type="question",
            content=content,
            priority=priority,
            metadata=metadata,
        )
        with self._data_lock:
            self._messages[msg_id] = msg
        return msg.to_dict()

    def send_answer(self, org_id, sender_agent_id, receiver_agent_id,
                    content, reference_id=None, metadata=None):
        """回答メッセージを送信する。

        reference_id に元の質問メッセージの ID を設定すると紐付けられる。
        """
        msg_id = "comm_a_" + gen_id()
        msg = CommMessage(
            msg_id=msg_id,
            org_id=org_id,
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            message_type="answer",
            content=content,
            reference_id=reference_id,
            metadata=metadata,
        )
        with self._data_lock:
            self._messages[msg_id] = msg
            if reference_id and reference_id in self._messages:
                question_msg = self._messages[reference_id]
                question_msg.status = "answered"
                question_msg.response = content
                question_msg.updated_at = timestamp()
        return msg.to_dict()

    def send_instruction(self, org_id, sender_agent_id, receiver_agent_id,
                         content, priority="normal", metadata=None):
        """指示メッセージを送信する（通常 PM → coder のような流れ）。"""
        msg_id = "comm_i_" + gen_id()
        msg = CommMessage(
            msg_id=msg_id,
            org_id=org_id,
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            message_type="instruction",
            content=content,
            priority=priority,
            metadata=metadata,
        )
        with self._data_lock:
            self._messages[msg_id] = msg
        return msg.to_dict()

    def send_report(self, org_id, sender_agent_id, receiver_agent_id,
                    content, reference_id=None, metadata=None):
        """報告メッセージを送信する（通常 coder → PM のような流れ）。"""
        msg_id = "comm_r_" + gen_id()
        msg = CommMessage(
            msg_id=msg_id,
            org_id=org_id,
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            message_type="report",
            content=content,
            reference_id=reference_id,
            metadata=metadata,
        )
        with self._data_lock:
            self._messages[msg_id] = msg
        return msg.to_dict()

    def get_message(self, msg_id):
        """メッセージを取得する。"""
        with self._data_lock:
            msg = self._messages.get(msg_id)
            if msg is None:
                return None
            return msg.to_dict()

    def get_inbox(self, org_id, agent_id, message_type=None, status=None):
        """エージェントの受信メッセージを取得する。"""
        with self._data_lock:
            results = []
            for msg in self._messages.values():
                if msg.org_id != org_id:
                    continue
                if msg.receiver_agent_id != agent_id:
                    continue
                if message_type and msg.message_type != message_type:
                    continue
                if status and msg.status != status:
                    continue
                results.append(msg.to_dict())
            results.sort(key=lambda m: m["created_at"], reverse=True)
            return results

    def get_outbox(self, org_id, agent_id, message_type=None):
        """エージェントの送信メッセージを取得する。"""
        with self._data_lock:
            results = []
            for msg in self._messages.values():
                if msg.org_id != org_id:
                    continue
                if msg.sender_agent_id != agent_id:
                    continue
                if message_type and msg.message_type != message_type:
                    continue
                results.append(msg.to_dict())
            results.sort(key=lambda m: m["created_at"], reverse=True)
            return results

    def get_org_messages(self, org_id, message_type=None, limit=100):
        """組織の全メッセージを取得する。"""
        with self._data_lock:
            results = []
            for msg in self._messages.values():
                if msg.org_id != org_id:
                    continue
                if message_type and msg.message_type != message_type:
                    continue
                results.append(msg.to_dict())
            results.sort(key=lambda m: m["created_at"], reverse=True)
            return results[:limit]

    def mark_delivered(self, msg_id):
        """メッセージを配信済みにする。"""
        with self._data_lock:
            msg = self._messages.get(msg_id)
            if msg is None:
                return False
            if msg.status == "pending":
                msg.status = "delivered"
                msg.updated_at = timestamp()
            return True

    def mark_read(self, msg_id):
        """メッセージを既読にする。"""
        with self._data_lock:
            msg = self._messages.get(msg_id)
            if msg is None:
                return False
            if msg.status in ("pending", "delivered"):
                msg.status = "read"
                msg.updated_at = timestamp()
            return True
