"""Input-lock expansion tests with no vendor execution."""

import json

import pytest

from scripts import build_pinned_windows_packvm_bundle as builder


def _lock(tmp_path):
    (tmp_path / "asset").write_bytes(b"vendor")
    record = {"path": "asset", "sha256": "sha256:" + "a" * 64, "size_bytes": 6}
    data = {
        "schema": builder.SCHEMA,
        "architecture": "amd64",
        "accelerator": "whpx",
        "files": {slot: dict(record) for slot in builder.VENDOR_SLOTS},
        "qemu_dependencies": [],
        "image_source": "https://example.com/guest.raw",
    }
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(data))
    return lock, data


def _build(lock, tmp_path):
    return builder.build_pinned_bundle(
        lock=lock,
        inputs_root=tmp_path,
        runtime_root=tmp_path,
        config=tmp_path / "config",
        expected_config_sha256="sha256:" + "b" * 64,
        expected_agent_sha256="sha256:" + "c" * 64,
        output=tmp_path / "output",
    )


def test_lock_expands_into_strict_builder(tmp_path, monkeypatch):
    lock, _ = _lock(tmp_path)
    captured = {}

    def stage(**kwargs):
        captured.update(kwargs)
        return tmp_path / "result"

    monkeypatch.setattr(builder, "build_portable_bundle", stage)
    assert _build(lock, tmp_path) == tmp_path / "result"
    assert captured["accelerator"] == "whpx"
    assert captured["inputs"]["qemu"] == (tmp_path / "asset", "sha256:" + "a" * 64)
    assert captured["inputs"]["config"] == (tmp_path / "config", "sha256:" + "b" * 64)
    assert captured["expected_agent_sha256"] == "sha256:" + "c" * 64


@pytest.mark.parametrize(
    "name",
    [
        "../asset",
        "/asset",
        "C:/asset",
        "x\\asset",
        "x/../asset",
        "asset.",
    ],
)
def test_rejects_unsafe_input_paths(tmp_path, name):
    with pytest.raises(ValueError, match="safe relative"):
        builder._input(
            tmp_path,
            {
                "path": name,
                "sha256": "sha256:" + "a" * 64,
                "size_bytes": 1,
            },
        )


def test_rejects_input_size_drift(tmp_path):
    (tmp_path / "asset").write_bytes(b"changed")
    with pytest.raises(ValueError, match="size mismatch"):
        builder._input(
            tmp_path,
            {
                "path": "asset",
                "sha256": "sha256:" + "a" * 64,
                "size_bytes": 1,
            },
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("architecture", "arm64"),
        ("accelerator", "kvm"),
        ("schema", "unknown"),
    ],
)
def test_rejects_wrong_profile(tmp_path, field, value):
    lock, data = _lock(tmp_path)
    data[field] = value
    lock.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unsupported"):
        _build(lock, tmp_path)
