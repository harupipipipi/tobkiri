from blocks._common import ok, error, gen_id, timestamp

from domain.chat.session_manager import SessionManager


def run(input_data, context):
    manager = SessionManager()
    name = input_data.get("name")
    metadata = input_data.get("metadata")
    session = manager.create_session(name=name, metadata=metadata)
    return ok(session)
