"""bridge.ipc_client — JSON Lines IPC over stdout."""

import sys

import json
import threading

from blocks._common import timestamp


class IpcClient:
    """Send structured messages to the frontend via stdout JSON Lines."""

    _lock = threading.Lock()

    def send(self, message_type, data):
        """Write a single JSON Lines message to stdout."""
        msg = {"type": message_type, "data": data, "ts": timestamp()}
        with self._lock:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def send_stream_start(self, request_id):
        """Signal the start of a streaming response."""
        self.send("stream.start", {"id": request_id})

    def send_stream_delta(self, request_id, content):
        """Send an incremental chunk of a streaming response."""
        self.send("stream.delta", {"id": request_id, "content": content})

    def send_stream_end(self, request_id, output):
        """Signal the end of a streaming response."""
        self.send("stream.end", {"id": request_id, "output": output})
