import json
import time
from pathlib import Path


from blocks._common import ok


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    audit_dir = Path(str(workspace.get("audit_dir") or ""))
    event = {
        "ts": int(time.time()),
        "profile_id": data.get("profile_id"),
        "event_type": data.get("event_type", "event"),
        "route_model": data.get("route_model"),
        "tools": data.get("tools"),
    }
    if audit_dir:
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return ok(event)
