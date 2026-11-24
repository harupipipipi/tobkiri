"""Thread-safe instruction queue for runtime instruction injection."""

import threading


from blocks._common import gen_id, timestamp

_PRIORITY_ORDER = {"urgent": 0, "normal": 1}

# consumed エントリの保持上限。これを超えると古いものから刈り取られる。
_MAX_CONSUMED = 100


class InstructionQueue:
    """Queue that holds user instructions injected during agent execution."""

    def __init__(self):
        self._lock = threading.Lock()
        self._instructions = []

    def add_instruction(self, execution_id, instruction_text, priority="normal"):
        if priority not in _PRIORITY_ORDER:
            priority = "normal"
        entry = {
            "id": gen_id(),
            "execution_id": execution_id,
            "instruction": instruction_text,
            "priority": priority,
            "status": "pending",
            "created_at": timestamp(),
        }
        with self._lock:
            self._instructions.append(entry)
        return entry

    def get_pending(self, execution_id):
        with self._lock:
            pending = [
                i for i in self._instructions
                if i["execution_id"] == execution_id and i["status"] == "pending"
            ]
            pending.sort(key=lambda i: _PRIORITY_ORDER.get(i["priority"], 1))
            for i in pending:
                i["status"] = "consumed"
            self._cleanup_consumed_locked()
            return pending

    def has_pending(self, execution_id):
        with self._lock:
            return any(
                i["execution_id"] == execution_id and i["status"] == "pending"
                for i in self._instructions
            )

    def has_urgent(self, execution_id):
        with self._lock:
            return any(
                i["execution_id"] == execution_id
                and i["status"] == "pending"
                and i["priority"] == "urgent"
                for i in self._instructions
            )

    def clear(self, execution_id):
        with self._lock:
            self._instructions = [
                i for i in self._instructions
                if i["execution_id"] != execution_id
            ]

    def list_all(self, execution_id):
        with self._lock:
            return [
                i.copy() for i in self._instructions
                if i["execution_id"] == execution_id
            ]

    def _cleanup_consumed_locked(self):
        """consumed エントリが _MAX_CONSUMED を超えたら古いものから削除する。

        呼び出し元が既に self._lock を保持していることを前提とする。
        """
        consumed = [i for i in self._instructions if i["status"] == "consumed"]
        if len(consumed) <= _MAX_CONSUMED:
            return
        # created_at の昇順でソートし、超過分を特定する
        consumed.sort(key=lambda i: i["created_at"])
        excess_count = len(consumed) - _MAX_CONSUMED
        ids_to_remove = {consumed[j]["id"] for j in range(excess_count)}
        self._instructions = [
            i for i in self._instructions if i["id"] not in ids_to_remove
        ]
