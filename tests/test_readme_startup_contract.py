"""Keep development-snapshot setup distinct from the default Git branch."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_each_fresh_clone_selects_documented_snapshot_before_dependencies():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:bash|powershell)\n(.*?)```", text, re.DOTALL)
    recipes = [block for block in blocks if "git clone " in block]
    assert len(recipes) == 2
    for recipe in recipes:
        selected = re.search(r"git fetch origin pull/(\d+)/head", recipe)
        assert selected is not None
        assert f"/pull/{selected.group(1)}" in text
        assert selected.start() < recipe.index("git switch --detach FETCH_HEAD")
        assert recipe.index("git switch --detach FETCH_HEAD") < recipe.index("venv .venv")


def test_python_module_is_not_advertised_as_standalone_defaults_setup():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "not a standalone Defaults setup command" in text
    assert "development build, not a released installer" in text
    assert "npm run tauri -- dev" in text
