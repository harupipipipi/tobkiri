"""
domain/agent/role_registry.py — ロール定義管理

各エージェントのロール（PM, coder, searcher, reviewer 等）を定義・管理する。
ロールごとにシステムプロンプト、利用可能ツール、コンテキスト上限を保持する。
"""

import copy
import threading


from blocks._common import gen_id, timestamp


# ---------------------------------------------------------------------------
# 組み込みロールテンプレート
# ---------------------------------------------------------------------------

_BUILTIN_ROLES = {
    "pm": {
        "display_name": "Project Manager",
        "system_prompt": (
            "You are a Project Manager (PM). Your responsibilities:\n"
            "- Break down tasks into actionable work items\n"
            "- Assign tasks to appropriate agents (coder, searcher, reviewer)\n"
            "- Track progress and ensure deliverables meet requirements\n"
            "- Coordinate between agents and resolve blockers\n"
            "- Summarize status and report to stakeholders\n"
            "Always think about the big picture and priorities."
        ),
        "allowed_tools": [
            "agent.org.instruct",
            "agent.org.ask",
            "agent.org.report",
            "agent.org.transfer_context",
        ],
        "context_limit": 128000,
    },
    "coder": {
        "display_name": "Coding Agent",
        "system_prompt": (
            "You are a Coding Agent. Your responsibilities:\n"
            "- Write clean, well-structured, production-ready code\n"
            "- Follow existing code patterns and conventions\n"
            "- Include error handling, input validation, and edge case coverage\n"
            "- Write code that is testable and maintainable\n"
            "- Report completion status and any blockers to the PM\n"
            "Always write complete implementations, never leave TODOs or stubs."
        ),
        "allowed_tools": [
            "coding.file_read",
            "coding.file_write",
            "coding.file_create",
            "coding.terminal_exec",
            "coding.git_status",
            "coding.git_diff",
            "agent.org.report",
            "agent.org.ask",
        ],
        "context_limit": 128000,
    },
    "searcher": {
        "display_name": "Search Agent",
        "system_prompt": (
            "You are a Search Agent. Your responsibilities:\n"
            "- Find relevant information from the web, documentation, and codebase\n"
            "- Verify facts and cross-reference multiple sources\n"
            "- Summarize findings clearly and concisely\n"
            "- Provide links and references for all claims\n"
            "- Report findings back to the requesting agent\n"
            "Always prioritize accuracy over speed."
        ),
        "allowed_tools": [
            "net.http.request",
            "file.search",
            "memory.vector.query",
            "agent.org.report",
            "agent.org.ask",
        ],
        "context_limit": 64000,
    },
    "reviewer": {
        "display_name": "Code Reviewer",
        "system_prompt": (
            "You are a Code Reviewer. Your responsibilities:\n"
            "- Review code for correctness, style, and best practices\n"
            "- Identify bugs, security issues, and performance problems\n"
            "- Suggest specific, actionable improvements\n"
            "- Verify that code meets the original requirements\n"
            "- Report review results to the PM\n"
            "Be thorough but constructive. Explain why each issue matters."
        ),
        "allowed_tools": [
            "coding.file_read",
            "coding.git_diff",
            "agent.org.report",
            "agent.org.ask",
        ],
        "context_limit": 128000,
    },
}


# ---------------------------------------------------------------------------
# RoleRegistry — シングルトン
# ---------------------------------------------------------------------------

class RoleRegistry:
    """ロール定義を管理するシングルトンレジストリ。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._roles = {}
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._data_lock = threading.Lock()
        for role_key, role_data in _BUILTIN_ROLES.items():
            self._roles[role_key] = {
                "role_id": "role_builtin_" + role_key,
                "role_key": role_key,
                "display_name": role_data["display_name"],
                "system_prompt": role_data["system_prompt"],
                "allowed_tools": list(role_data["allowed_tools"]),
                "context_limit": role_data["context_limit"],
                "is_builtin": True,
                "created_at": timestamp(),
                "updated_at": timestamp(),
            }

    def define_role(self, role_key, display_name, system_prompt,
                    allowed_tools=None, context_limit=128000):
        """カスタムロールを定義または上書きする。"""
        with self._data_lock:
            existing = self._roles.get(role_key)
            role_id = existing["role_id"] if existing else "role_" + gen_id()
            entry = {
                "role_id": role_id,
                "role_key": role_key,
                "display_name": display_name,
                "system_prompt": system_prompt,
                "allowed_tools": list(allowed_tools) if allowed_tools else [],
                "context_limit": context_limit,
                "is_builtin": False,
                "created_at": existing["created_at"] if existing else timestamp(),
                "updated_at": timestamp(),
            }
            self._roles[role_key] = entry
            return copy.deepcopy(entry)

    def get_role(self, role_key):
        """ロール定義を取得する。見つからなければ None。"""
        with self._data_lock:
            role = self._roles.get(role_key)
            if role is None:
                return None
            return copy.deepcopy(role)

    def list_roles(self):
        """全ロール定義のリストを返す。"""
        with self._data_lock:
            return [copy.deepcopy(r) for r in self._roles.values()]

    def delete_role(self, role_key):
        """カスタムロールを削除する。組み込みロールは削除不可。"""
        with self._data_lock:
            role = self._roles.get(role_key)
            if role is None:
                return False
            if role.get("is_builtin"):
                return False
            del self._roles[role_key]
            return True

    def build_system_prompt(self, role_key, extra_context=None):
        """ロールのシステムプロンプトを構築する。

        extra_context が指定された場合は末尾に追加する。
        """
        role = self.get_role(role_key)
        if role is None:
            return extra_context if extra_context else ""
        parts = [role["system_prompt"]]
        if extra_context:
            parts.append(extra_context)
        return "\n\n".join(parts)

    def get_context_limit(self, role_key):
        """ロールのコンテキスト上限を返す。未定義なら 128000。"""
        role = self.get_role(role_key)
        if role is None:
            return 128000
        return role.get("context_limit", 128000)

    def get_allowed_tools(self, role_key):
        """ロールの利用可能ツールリストを返す。"""
        role = self.get_role(role_key)
        if role is None:
            return []
        return list(role.get("allowed_tools", []))
