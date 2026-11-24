"""Product rename must not silently migrate durable IDs or protocol names."""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'tobkiri_runtime'
BUNDLE = RUNTIME / 'ecosystem/defaultspack/v4'

def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))

def test_harness_display_name_preserves_profile_and_pack_identity() -> None:
    intent = read(BUNDLE / 'defaults.profile.intent.v1.json')
    profile = read(BUNDLE / 'defaults.profile.v5.json')
    assert intent['display_name'] == profile['display_name'] == 'Tobkiri Harness'
    assert intent['profile_id'] == profile['profile_id'] == 'defaults'
    assert intent['base']['pack_id'] == 'defaults-basepack'
    assert any(item['pack_id'] == 'defaultspack' for item in intent['packs'])
    pack = read(RUNTIME / 'ecosystem/defaultspack/pack.v4.json')
    assert pack['pack']['id'] == 'defaultspack'
    assert pack['pack']['display_name'] == 'Tobkiri Harness Providers'

def test_harness_window_title_preserves_native_install_identity() -> None:
    config = read(ROOT / 'tobkiri_launcher/src-tauri/tauri.shell.conf.json')
    assert config['app']['windows'][0]['title'] == 'Tobkiri Harness'
    assert config['identifier'] == 'io.tobkiri.shell.tauri'
    assert config['mainBinaryName'] == 'tobkiri-shell'
    assert config['productName'] == 'Tobkiri'

def test_harness_browser_title_is_built_from_current_source() -> None:
    root = RUNTIME / 'ecosystem/defaultspack'
    for relative in ('webapp/index.html', 'ui/shell.html'):
        assert '<title>Tobkiri Harness</title>' in (root/relative).read_text()

def test_defaults_protocol_version_is_not_a_brand_label() -> None:
    schema = read(RUNTIME / 'tobkiri_protocol/schemas/defaults_setup_v4.schema.json')
    assert schema['properties']['setup_api_version']['const'] == 'io.tobkiri.setup-state.v4'
