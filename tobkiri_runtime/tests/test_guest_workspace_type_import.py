"""Keep Host workspace pin implementation outside the pure Guest closure."""

from pathlib import Path
import os
import subprocess
import sys
import zipfile

from scripts.build_packvm_guest_bundle import build_guest_bundle


ROOT = Path(__file__).resolve().parents[1]


def test_guest_shared_plan_only_refers_to_workspace_pin_in_annotations(
    tmp_path: Path,
) -> None:
    """The actual isolated archive imports without a Host resolver or proof."""
    archive = tmp_path / "guest.pyz"
    archive.write_bytes(build_guest_bundle(ROOT))
    with zipfile.ZipFile(archive) as contents:
        assert "tobkiri_host/saved_workspace_context.py" not in contents.namelist()
    probe = """
import sys
sys.path.insert(0, sys.argv[1])
from tobkiri_host.saved_guest_dispatch import SavedGuestTurns
from tobkiri_host.saved_turn_plan import SavedTurnPlan, SavedToolFrame
assert 'tobkiri_host.saved_workspace_context' not in sys.modules
frame = SavedToolFrame({}, None, b'[]', 'read', None, None)
assert frame.workspace_context_owner is None
assert SavedTurnPlan({'turn_id':'turn', 'conversation_id':'chat',
    'conversation_revision':1, 'content':'message'}).stage == 'read'
try:
    import tobkiri_host.saved_workspace_context
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('Host workspace resolver leaked into Guest closure')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-I", "-S", "-c", probe, str(archive)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
