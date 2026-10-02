"""
blocks/agent/org/ask.py — エージェント間質問ブロック

POST /api/agent/org/{id}/ask

input_data:
    id                : str (必須) 組織 ID
    sender_agent_id   : str (必須) 質問者エージェント ID
    receiver_agent_id : str (必須) 回答者エージェント ID
    content           : str (必須) 質問内容
    priority          : str (任意) "low" | "normal" | "high" | "urgent"
    transfer_id       : str (任意) バックチャンネル質問の場合の移行記録 ID
    model             : str (任意) バックチャンネル質問で AI 回答生成に使うモデル
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager
from domain.agent.inter_agent_comm import InterAgentComm
from domain.agent.context_transfer import ContextTransferManager


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    org_id = input_data.get("id")
    if not org_id:
        return error("id (org_id) is required")

    sender_agent_id = input_data.get("sender_agent_id")
    if not sender_agent_id:
        return error("sender_agent_id is required")

    receiver_agent_id = input_data.get("receiver_agent_id")
    if not receiver_agent_id:
        return error("receiver_agent_id is required")

    content = input_data.get("content")
    if not content or not isinstance(content, str) or not content.strip():
        return error("content is required and must be a non-empty string")

    manager = OrgManager()
    org = manager.get_org(org_id)
    if org is None:
        return error("organization not found: " + str(org_id))

    members = org.get("members", {})
    if sender_agent_id not in members:
        return error("sender agent not found in organization: " + str(sender_agent_id))
    if receiver_agent_id not in members:
        return error("receiver agent not found in organization: " + str(receiver_agent_id))

    transfer_id = input_data.get("transfer_id")
    if transfer_id:
        model = input_data.get("model", "default")
        transfer_mgr = ContextTransferManager()
        back_channel_result = transfer_mgr.ask_source_agent(
            transfer_id=transfer_id,
            question=content.strip(),
            model=model,
        )
        if back_channel_result is None:
            return error("transfer record not found: " + str(transfer_id))
        return ok({
            "type": "back_channel_answer",
            "transfer_id": transfer_id,
            "question": content.strip(),
            "answer": back_channel_result.get("response", ""),
            "back_channel_message": back_channel_result,
        })

    priority = input_data.get("priority", "normal")
    if priority not in ("low", "normal", "high", "urgent"):
        priority = "normal"

    comm = InterAgentComm()
    msg = comm.send_question(
        org_id=org_id,
        sender_agent_id=sender_agent_id,
        receiver_agent_id=receiver_agent_id,
        content=content.strip(),
        priority=priority,
    )

    return ok(msg)
