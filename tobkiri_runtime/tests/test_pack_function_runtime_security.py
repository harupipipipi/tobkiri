from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_verified_system_descriptor_allows_in_process_function_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime.pack_function_runtime import is_pack_function_in_process_allowed

    pack_root = tmp_path / "ecosystem" / "system_ui"
    function_dir = pack_root / "functions" / "list_modules"
    function_dir.mkdir(parents=True)
    (pack_root / "ecosystem.json").write_text(
        '{"pack_id":"system_ui","version":"1.0.0"}',
        encoding="utf-8",
    )
    manager = MagicMock()
    manager.is_pack_in_process_allowed.side_effect = (
        lambda pack_id, root: pack_id == "system_ui" and Path(root) == pack_root
    )
    monkeypatch.setattr(
        "core_runtime.pack_function_runtime.get_approval_manager",
        lambda: manager,
    )

    assert is_pack_function_in_process_allowed("system_ui", function_dir)
    manager.is_pack_in_process_allowed.assert_called_once_with("system_ui", pack_root)


def test_invoke_pack_function_rejects_untrusted_pack_before_import(tmp_path: Path) -> None:
    from core_runtime.di_container import get_container, reset_container
    from core_runtime.pack_function_runtime import invoke_pack_function

    reset_container()
    marker = tmp_path / "executed.txt"
    pack_root = tmp_path / "ecosystem" / "evil_pack"
    function_dir = pack_root / "functions" / "pwn"
    function_dir.mkdir(parents=True)
    (pack_root / "ecosystem.json").write_text(
        '{"pack_id":"evil_pack","version":"1.0.0"}',
        encoding="utf-8",
    )
    (function_dir / "main.py").write_text(
        (
            "from pathlib import Path\n"
            "def run(context, args):\n"
            f"    Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
            "    return {'ok': True}\n"
        ),
        encoding="utf-8",
    )
    entry = SimpleNamespace(
        pack_id="evil_pack",
        function_id="pwn",
        qualified_name="evil_pack:pwn",
        function_dir=function_dir,
        entrypoint="main.py:run",
    )
    registry = MagicMock()
    registry.get.return_value = entry

    try:
        get_container().set_instance("function_registry", registry)

        with pytest.raises(PermissionError):
            invoke_pack_function("evil_pack", "pwn")
    finally:
        reset_container()

    assert not marker.exists()
