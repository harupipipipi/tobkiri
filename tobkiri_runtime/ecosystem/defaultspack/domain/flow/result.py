"""FlowResult — フロー実行結果を表すデータクラス"""

from blocks._common import ok, error, gen_id, timestamp


class FlowResult:
    """フロー実行の結果を保持するクラス"""

    def __init__(self, status="completed", output=None, messages=None, metadata=None):
        self.status = status
        self.output = output or {}
        self.messages = messages or []
        self.metadata = metadata or {}

    def to_dict(self):
        """結果を辞書形式で返す"""
        return {
            "status": self.status,
            "output": self.output,
            "messages": self.messages,
            "metadata": self.metadata,
        }

    def is_success(self):
        """実行が成功したかどうか"""
        return self.status == "completed"

    def is_error(self):
        """実行がエラーかどうか"""
        return self.status == "error"

    def __repr__(self):
        output_desc = list(self.output.keys()) if isinstance(self.output, dict) else "?"
        return "FlowResult(status={!r}, output_keys={})".format(self.status, output_desc)
