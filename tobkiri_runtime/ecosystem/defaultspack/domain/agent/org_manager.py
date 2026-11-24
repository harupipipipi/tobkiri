"""
domain/agent/org_manager.py — 組織管理ロジック

組織（Organization）の CRUD、メンバー管理、ステータス追跡を行う。
既存の domain/agent/ ファイルは一切変更しない。
"""

import copy
import threading


from blocks._common import gen_id, timestamp


class Organization:
    """エージェント組織を表すデータクラス。"""

    def __init__(self, org_id, name, description="", created_by="system"):
        self.org_id = org_id
        self.name = name
        self.description = description
        self.created_by = created_by
        self.status = "active"
        self.members = {}
        self.created_at = timestamp()
        self.updated_at = timestamp()

    def add_member(self, agent_id, agent_name, role_key, model="default"):
        """メンバーを追加する。既に存在すれば上書き。"""
        self.members[agent_id] = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "role_key": role_key,
            "model": model,
            "status": "idle",
            "joined_at": timestamp(),
            "context_usage": 0,
        }
        self.updated_at = timestamp()
        return copy.deepcopy(self.members[agent_id])

    def remove_member(self, agent_id):
        """メンバーを削除する。見つからなければ False。"""
        if agent_id not in self.members:
            return False
        del self.members[agent_id]
        self.updated_at = timestamp()
        return True

    def get_member(self, agent_id):
        """メンバーを取得する。"""
        member = self.members.get(agent_id)
        if member is None:
            return None
        return copy.deepcopy(member)

    def list_members(self):
        """全メンバーのリストを返す。"""
        return [copy.deepcopy(m) for m in self.members.values()]

    def update_member_status(self, agent_id, status):
        """メンバーのステータスを更新する。"""
        member = self.members.get(agent_id)
        if member is None:
            return False
        member["status"] = status
        self.updated_at = timestamp()
        return True

    def update_member_context_usage(self, agent_id, usage):
        """メンバーのコンテキスト使用量を更新する。"""
        member = self.members.get(agent_id)
        if member is None:
            return False
        member["context_usage"] = usage
        self.updated_at = timestamp()
        return True

    def find_members_by_role(self, role_key):
        """指定ロールのメンバーを全て返す。"""
        return [
            copy.deepcopy(m) for m in self.members.values()
            if m["role_key"] == role_key
        ]

    def to_dict(self):
        return {
            "org_id": self.org_id,
            "name": self.name,
            "description": self.description,
            "created_by": self.created_by,
            "status": self.status,
            "members": {aid: copy.deepcopy(m) for aid, m in self.members.items()},
            "member_count": len(self.members),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OrgManager:
    """組織の CRUD とメンバー管理を行うシングルトン。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._orgs = {}
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._data_lock = threading.Lock()

    def create_org(self, name, description="", created_by="system"):
        """組織を作成する。"""
        org_id = "org_" + gen_id()
        org = Organization(
            org_id=org_id,
            name=name,
            description=description,
            created_by=created_by,
        )
        with self._data_lock:
            self._orgs[org_id] = org
        return org.to_dict()

    def get_org(self, org_id):
        """組織を取得する。見つからなければ None。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return None
            return org.to_dict()

    def list_orgs(self):
        """全組織のリストを返す。"""
        with self._data_lock:
            return [org.to_dict() for org in self._orgs.values()]

    def delete_org(self, org_id):
        """組織を削除する。"""
        with self._data_lock:
            if org_id not in self._orgs:
                return False
            del self._orgs[org_id]
            return True

    def add_member(self, org_id, agent_id, agent_name, role_key, model="default"):
        """組織にメンバーを追加する。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return None
            return org.add_member(agent_id, agent_name, role_key, model)

    def remove_member(self, org_id, agent_id):
        """組織からメンバーを削除する。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return None
            result = org.remove_member(agent_id)
            return result

    def get_member(self, org_id, agent_id):
        """組織のメンバーを取得する。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return None
            return org.get_member(agent_id)

    def list_members(self, org_id):
        """組織の全メンバーを返す。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return None
            return org.list_members()

    def update_member_status(self, org_id, agent_id, status):
        """メンバーのステータスを更新する。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return False
            return org.update_member_status(agent_id, status)

    def update_member_context_usage(self, org_id, agent_id, usage):
        """メンバーのコンテキスト使用量を更新する。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return False
            return org.update_member_context_usage(agent_id, usage)

    def find_members_by_role(self, org_id, role_key):
        """指定ロールのメンバーを検索する。"""
        with self._data_lock:
            org = self._orgs.get(org_id)
            if org is None:
                return []
            return org.find_members_by_role(role_key)

    def get_org_object(self, org_id):
        """内部用: Organization オブジェクト自体を返す。"""
        with self._data_lock:
            return self._orgs.get(org_id)
