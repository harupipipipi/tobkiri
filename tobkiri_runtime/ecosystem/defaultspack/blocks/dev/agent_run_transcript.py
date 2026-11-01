

from blocks._common import error, ok
from domain.agent_runtime.run_store import AgentRunStore
from domain.agent_runtime.transcript import TranscriptStore


def run(input_data, context=None):
    run_id = input_data.get("run_id") or input_data.get("id")
    if not run_id:
        return error("run_id is required", "INVALID_INPUT")
    run_data = AgentRunStore().get_run(run_id)
    if not run_data:
        return error("run not found", "NOT_FOUND")
    transcript_id = run_data.get("current_transcript_id")
    return ok({"transcript_id": transcript_id, "entries": TranscriptStore().read_tail(transcript_id, 1000)})
