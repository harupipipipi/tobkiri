"""
domain/agent/context_transfer.py — コンテキスト移行ロジック

あるエージェントのコンテキストが溢れた場合に要約して新エージェントに引き継ぐ。
旧エージェントへの質問チャンネル（back-channel）も提供する。
"""

import copy
import threading


from blocks._common import gen_id, timestamp
from blocks.chat._prompt_helpers import build_summarizer_system_prompt
from domain.ai_client.client import AIClient


_SUMMARIZE_SYSTEM_PROMPT = (
    build_summarizer_system_prompt(persona="context")
    + "\n\nPreserve:\n"
    "1. Key decisions and their rationale\n"
    "2. Current state of all work items\n"
    "3. Open questions and blockers\n"
    "4. Important context that a successor agent needs\n"
    "5. Any commitments or promises made\n\n"
    "Be thorough but concise. The successor agent will rely "
    "entirely on this summary to continue the work."
)


class TransferRecord:
    """コンテキスト移行の記録。"""

    def __init__(self, transfer_id, org_id, source_agent_id,
                 target_agent_id, summary, original_message_count):
        self.transfer_id = transfer_id
        self.org_id = org_id
        self.source_agent_id = source_agent_id
        self.target_agent_id = target_agent_id
        self.summary = summary
        self.original_message_count = original_message_count
        self.back_channel_messages = []
        self.status = "completed"
        self.created_at = timestamp()

    def add_back_channel_message(self, sender_agent_id, content, response=None):
        """バックチャンネルメッセージを追加する。"""
        msg = {
            "id": "bcm_" + gen_id(),
            "sender_agent_id": sender_agent_id,
            "content": content,
            "response": response,
            "created_at": timestamp(),
        }
        self.back_channel_messages.append(msg)
        return copy.deepcopy(msg)

    def to_dict(self):
        return {
            "transfer_id": self.transfer_id,
            "org_id": self.org_id,
            "source_agent_id": self.source_agent_id,
            "target_agent_id": self.target_agent_id,
            "summary": self.summary,
            "original_message_count": self.original_message_count,
            "back_channel_messages": [
                copy.deepcopy(m) for m in self.back_channel_messages
            ],
            "status": self.status,
            "created_at": self.created_at,
        }


class ContextTransferManager:
    """コンテキスト移行を管理するシングルトン。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._transfers = {}
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._data_lock = threading.Lock()
        self._client = AIClient()

    def _summarize_messages(self, messages, model="default"):
        """メッセージ群を要約する。"""
        summary_messages = [
            {"role": "system", "content": _SUMMARIZE_SYSTEM_PROMPT},
        ]
        conversation_text_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = " ".join(text_parts)
            conversation_text_parts.append(role + ": " + str(content))
        conversation_text = "\n".join(conversation_text_parts)
        summary_messages.append({
            "role": "user",
            "content": (
                "Please summarize the following conversation:\n\n"
                + conversation_text
            ),
        })
        try:
            result = self._client.complete(model, summary_messages)
            if isinstance(result, dict):
                return result.get("content", result.get("text", str(result)))
            return str(result)
        except Exception as exc:
            return (
                "Summary generation failed (" + str(exc) + "). "
                "Original conversation had " + str(len(messages)) + " messages."
            )

    def transfer_context(self, org_id, source_agent_id, target_agent_id,
                         messages, model="default"):
        """コンテキストを移行する。

        Parameters
        ----------
        org_id : str
            組織 ID。
        source_agent_id : str
            移行元エージェント ID。
        target_agent_id : str
            移行先エージェント ID。
        messages : list[dict]
            移行元エージェントの会話メッセージ。
        model : str
            要約に使用するモデル。

        Returns
        -------
        dict
            移行結果。
        """
        transfer_id = "transfer_" + gen_id()
        summary = self._summarize_messages(messages, model)
        record = TransferRecord(
            transfer_id=transfer_id,
            org_id=org_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            summary=summary,
            original_message_count=len(messages),
        )
        with self._data_lock:
            self._transfers[transfer_id] = record
        return record.to_dict()

    def get_transfer(self, transfer_id):
        """移行記録を取得する。"""
        with self._data_lock:
            record = self._transfers.get(transfer_id)
            if record is None:
                return None
            return record.to_dict()

    def list_transfers_for_org(self, org_id):
        """組織の全移行記録を返す。"""
        with self._data_lock:
            results = []
            for record in self._transfers.values():
                if record.org_id == org_id:
                    results.append(record.to_dict())
            return results

    def ask_source_agent(self, transfer_id, question, model="default"):
        """バックチャンネルで旧エージェントに質問する。

        移行時の要約に含まれていなかった情報を取得するために使う。
        旧エージェントのコンテキスト（要約）を基に AI が回答を生成する。

        Parameters
        ----------
        transfer_id : str
            移行記録 ID。
        question : str
            質問内容。
        model : str
            回答生成に使用するモデル。

        Returns
        -------
        dict
            回答を含むバックチャンネルメッセージ。
        """
        with self._data_lock:
            record = self._transfers.get(transfer_id)
            if record is None:
                return None

        answer_messages = [
            {
                "role": "system",
                "content": (
                    "You are the previous agent that handled a task. "
                    "A new agent has taken over and is asking you a question "
                    "about context that may not have been in the handoff summary.\n\n"
                    "Here is the summary of your work:\n"
                    + record.summary
                    + "\n\nAnswer the question as helpfully as possible based on "
                    "your knowledge of the work you did."
                ),
            },
            {"role": "user", "content": question},
        ]
        try:
            result = self._client.complete(model, answer_messages)
            if isinstance(result, dict):
                answer = result.get("content", result.get("text", str(result)))
            else:
                answer = str(result)
        except Exception as exc:
            answer = "Failed to generate answer: " + str(exc)

        with self._data_lock:
            msg = record.add_back_channel_message(
                sender_agent_id=record.target_agent_id,
                content=question,
                response=answer,
            )
        return msg
