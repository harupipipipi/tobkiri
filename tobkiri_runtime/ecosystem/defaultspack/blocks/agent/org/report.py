"""
blocks/agent/org/report.py — エージェント間報告ブロック

POST /api/agent/org/{id}/report

input_data:
    id                : str (必須) 組織 ID
    sender_agent_id   : str (必須) 報告者エージェント ID
    receiver_agent_id : str (必須) 報告先エージェント ID（通常 PM）
    content           : str (必須) 報告内容
    reference_id      : str (任意) 関連する指示メッセージの ID
    metadata          : dict (任意) 追加メタデータ（成果物パス等）
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager
from domain.agent.inter_agent_comm import InterAgentComm


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

    reference_id = input_data.get("reference_id")
    metadata = input_data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return error("metadata must be a dict")

    comm = InterAgentComm()
    msg = comm.send_report(
        org_id=org_id,
        sender_agent_id=sender_agent_id,
        receiver_agent_id=receiver_agent_id,
        content=content.strip(),
        reference_id=reference_id,
        metadata=metadata,
    )

    manager.update_member_status(org_id, sender_agent_id, "reported")

    return ok(msg)
