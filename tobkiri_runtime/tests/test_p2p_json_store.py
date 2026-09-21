from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_json_store_loads_only_objects_and_saves_atomically(tmp_path: Path) -> None:
    from domain.p2p.json_store import load_json_object, save_json_object

    path = tmp_path / "devices.json"
    path.write_text("[]", encoding="utf-8")

    assert load_json_object(path) == {}

    save_json_object(path, {"schema_version": 1, "label": "Rumi"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "label": "Rumi",
    }
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert not list(tmp_path.glob("devices.json.*.tmp"))


def test_json_store_file_lock_reclaims_stale_lock(tmp_path: Path) -> None:
    from domain.p2p.json_store import file_lock

    path = tmp_path / "pairings.json"
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.write_text("old\n", encoding="ascii")
    old_time = time.time() - 60
    os.utime(lock_path, (old_time, old_time))

    with file_lock(path, lock_name="pairing store", timeout_seconds=0.2, stale_seconds=0.001):
        assert lock_path.exists()

    assert not lock_path.exists()
