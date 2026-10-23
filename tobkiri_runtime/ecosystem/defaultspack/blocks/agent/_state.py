
from domain.agent.instruction_queue import InstructionQueue

_engines = {}
_instruction_queue = InstructionQueue()

# ---------------------------------------------------------------------------
# マルチエージェントセッション管理
# ---------------------------------------------------------------------------

_multi_sessions = {}


def get_multi_session(session_id):
    """セッションIDからマルチエージェントセッションを取得する。見つからなければ None。"""
    return _multi_sessions.get(session_id)


def set_multi_session(session_id, session):
    """マルチエージェントセッションを登録する。"""
    _multi_sessions[session_id] = session


def remove_multi_session(session_id):
    """マルチエージェントセッションを削除する。"""
    _multi_sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# エージェントエンジン管理
# ---------------------------------------------------------------------------

def get_engine(execution_id):
    engine = _engines.get(execution_id)
    if engine is not None:
        return engine
    try:
        from domain.agent_runtime.run_store import AgentRunStore
        from domain.agent.engine import AgentEngine

        if AgentRunStore().get_run(execution_id) is None:
            return None
        engine = AgentEngine()
        _engines[execution_id] = engine
        return engine
    except Exception:
        return None


def set_engine(execution_id, engine):
    _engines[execution_id] = engine


def remove_engine(execution_id):
    _engines.pop(execution_id, None)
    _instruction_queue.clear(execution_id)


def list_engines():
    result = {}
    for eid, engine in _engines.items():
        result[eid] = engine.status(eid)
    try:
        from domain.agent_runtime.run_store import AgentRunStore

        for run in AgentRunStore().list_runs(limit=100):
            run_id = run.get("run_id")
            if run_id and run_id not in result:
                result[run_id] = {
                    "execution_id": run_id,
                    "status": run.get("status"),
                    "current_step": (run.get("execution_json") or {}).get("current_step", 0),
                }
    except Exception:
        pass
    return result


def get_instruction_queue():
    return _instruction_queue
