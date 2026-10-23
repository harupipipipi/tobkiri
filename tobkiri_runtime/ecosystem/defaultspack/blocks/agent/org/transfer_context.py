"""
blocks/agent/org/transfer_context.py — コンテキスト移行ブロック

POST /api/agent/org/{id}/transfer

input_data:
    id               : str (必須) 組織 ID
    source_agent_id  : str (必須) 移行元エージェント ID
    target_agent_id  : str (必須) 移行先エージェント ID
    messages         : list[dict] (任意) 移行対象メッセージ（省略時は空リスト）
    conversation_id  : str (任意) ChatStore の会話IDから自動取得する場合
    model            : str (任意) 要約に使用するモデル
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager
from domain.agent.context_transfer import ContextTransferManager
from domain.agent.role_registry import RoleRegistry


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    org_id = input_data.get("id")
    if not org_id:
        return error("id (org_id) is required")

    source_agent_id = input_data.get("source_agent_id")
    if not source_agent_id:
        return error("source_agent_id is required")

    target_agent_id = input_data.get("target_agent_id")
    if not target_agent_id:
        return error("target_agent_id is required")

    if source_agent_id == target_agent_id:
        return error("source_agent_id and target_agent_id must be different")

    manager = OrgManager()
    org = manager.get_org(org_id)
    if org is None:
        return error("organization not found: " + str(org_id))

    members = org.get("members", {})
    if source_agent_id not in members:
        return error("source agent not found in organization: " + str(source_agent_id))
    if target_agent_id not in members:
        return error("target agent not found in organization: " + str(target_agent_id))

    messages = input_data.get("messages")
    if messages is None:
        conversation_id = input_data.get("conversation_id")
        if conversation_id:
            try:
                from domain.chat.store import ChatStore
                store = ChatStore()
                conv = store.get_conversation(conversation_id)
                if conv and conv.get("messages"):
                    messages = conv["messages"]
                else:
                    messages = []
            except Exception:
                messages = []
        else:
            messages = []

    if not isinstance(messages, list):
        return error("messages must be a list")

    if len(messages) == 0:
        return error("no messages to transfer; provide messages or a valid conversation_id")

    model = input_data.get("model", "default")

    transfer_mgr = ContextTransferManager()
    result = transfer_mgr.transfer_context(
        org_id=org_id,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        messages=messages,
        model=model,
    )

    role_registry = RoleRegistry()
    target_member = members.get(target_agent_id, {})
    target_role_key = target_member.get("role_key", "")
    target_system_prompt = role_registry.build_system_prompt(
        target_role_key,
        extra_context=(
            "You are receiving a context transfer from a previous agent.\n"
            "Here is the summary of their work:\n\n"
            + result.get("summary", "")
            + "\n\nUse transfer_id '" + result.get("transfer_id", "")
            + "' if you need to ask the previous agent questions via back-channel."
        ),
    )
    result["target_system_prompt"] = target_system_prompt

    manager.update_member_status(org_id, source_agent_id, "transferred")
    manager.update_member_status(org_id, target_agent_id, "active")

    return ok(result)
