#!/usr/bin/env python3
"""Build, approve and execute a foreign Flow overlay through the real CLI."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from issue_1520_cli_roundtrip import (
    APPROVAL,
    DIGEST,
    Terminal,
    approve,
    json_digest,
    open_readonly,
    publish,
    report_conversation_store_failure,
    review_prompt,
)

MESSAGES = "SELECT role, content FROM messages ORDER BY seq"


@dataclass(frozen=True)
class Artifact:
    """A newly authored Pack and its separately selected limits."""

    path: Path
    limits: Path
    pack: str
    digest: str
    permissions: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    """Committed Profile and Pack authority observed across a CLI edit."""

    head: str
    epoch: int
    approvals: dict[tuple[str, str], dict[str, Any]]
    publication: bytes


@dataclass(frozen=True)
class ProfileFixture:
    """A fresh Profile with the preset's original Pack pins and input."""

    profile: Path
    definition: dict[str, Any]
    entry: dict[str, str]
    store_pack: str
    conversation: str
    role: str
    pins: dict[str, str]


def author(
    cli: list[str], directory: Path, pack: str, version: str,
    role: str, marker: str, resources: dict[str, Any], *, audit_append: bool = False,
) -> Artifact:
    """Stage the role-addressed overlay and build a verified .tpk from it."""
    stage = directory / "stage"
    stage.mkdir(parents=True)
    source = "prompt.overlay.json"
    (stage / source).write_text(json.dumps({
        "format": "tobkiri.overlay/1",
        "id": "replace-prompt-text",
        "target": {"role": role},
        "body": [{
            "id": "continue-with-replacement-text",
            "next": {"args": {
                "ctx": "$input.ctx",
                "input": {"conversation": "$input.input.conversation", "text": marker},
            }},
        }],
    }))
    return build_pack(cli, directory, {
        "format": "tobkiri.pack/1", "id": pack, "version": version,
        "runtime": {"kind": "data"}, "shapes": [],
        "ports": {"provides": [], "consumes": [], "untyped": []},
        "flows": [], "overlays": [source], "stores": [], "files": [source],
        "permissions": resources | {"ports": [role], "audit_append": audit_append},
    })


def build_pack(cli: list[str], directory: Path, spec: dict[str, Any]) -> Artifact:
    """Hash explicit staged files and build and verify the public Pack archive."""
    stage = directory / "stage"
    pack, version = spec["id"], spec["version"]
    draft = directory / "draft.json"
    draft.write_text(json.dumps(spec))
    manifest = stage / "pack.json"
    subprocess.run(cli + [
        "pack", "manifest", str(stage), "--template", str(draft),
        "--output", str(manifest),
    ], check=True, timeout=30)
    path = directory / f"{pack}.tpk"
    subprocess.run(
        cli + ["pack", "build", str(stage), "--output", str(path)],
        check=True, timeout=30,
    )
    inspected = subprocess.check_output(
        cli + ["pack", "verify", str(path)], text=True, timeout=30,
    )
    pack_id, verified_version, digest = inspected.split()
    assert (pack_id, verified_version) == (pack, version) and DIGEST.fullmatch(digest)
    document = json.loads(manifest.read_bytes())
    assert digest == json_digest(document)
    permissions = document["permissions"]
    limits = directory / "limits.json"
    limits.write_text(json.dumps(permissions))
    return Artifact(path, limits, pack, digest, permissions)


def snapshot(cli: list[str], profile: Path, output: Path) -> Snapshot:
    """Read the active head, epoch, committed permission bodies and publication."""
    with closing(open_readonly(profile / "approvals.sqlite")) as connection:
        (epoch,) = connection.execute("SELECT epoch FROM security_epoch").fetchone()
        rows = connection.execute("SELECT pack, hash, body FROM permissions")
        approvals = {
            (pack, digest): json.loads(body) for pack, digest, body in rows
        }
    return Snapshot(
        (profile / "head").read_text().strip(), epoch, approvals,
        publish(cli, profile.name, output),
    )


def audit_events(profile: Path) -> list[dict[str, Any]]:
    """Read persisted Core audit events without opening a write connection."""
    with closing(open_readonly(profile / "audit.sqlite")) as connection:
        rows = connection.execute("SELECT body FROM audit ORDER BY seq")
        return [json.loads(body) for (body,) in rows]


def review_edit(
    command: list[str], profile: Path, artifact: Artifact,
    expected_additions: dict[str, Any], *, accept: bool,
) -> str:
    """Answer only the native terminal prompt for this exact permission diff."""
    effect = {"kind": "permissions", "pack": artifact.pack, "hash": artifact.digest}
    with Terminal(command) as terminal:
        identity, prompted_effect, text, answer = review_prompt(terminal)
        assert identity == profile.name and prompted_effect == effect, text
        summary = text.split("\nSummary: ", 1)[1].strip()
        assert json.loads(json.loads(summary)) == expected_additions, text
        terminal.send(answer if accept else "deny")
        output = terminal.finish(success=accept)
    grant = answer.removeprefix("approve ")
    events = [
        event for event in audit_events(profile)
        if isinstance(event["payload"], dict) and event["payload"].get("grant") == grant
    ]
    assert [event["kind"] for event in events] == (
        ["permission-update", "outcome"] if accept else ["outcome"]
    ), events
    for event in events:
        assert event["profile"] == profile.name, event
        assert event["principal"] == {"kind": "user"}, event
        assert event["payload"]["effect_digest"] == json_digest(effect), event
        assert event["payload"]["args_digest"] == json_digest(expected_additions), event
    assert events[-1]["payload"]["outcome"] == {
        "status": "succeeded" if accept else "cancelled",
    }, events
    if not accept:
        assert "user declined" in output, output
        return ""
    revision = output.rsplit(APPROVAL.decode(), 1)[1].strip()
    assert DIGEST.fullmatch(revision), output
    return revision


def turn(
    cli: list[str], profile: Path, fixture: ProfileFixture,
    marker: str, replacement: str | None = None,
) -> None:
    """Observe the rewritten typed prompt, terminal output and actual saved turn."""
    entry, store_pack = fixture.entry, fixture.store_pack
    domain = entry["exact_input_view"].rsplit(".", 2)[0]
    store = profile / "data" / store_pack / "private.sqlite"
    history = []
    if store.is_file():
        with closing(open_readonly(store)) as connection:
            history = connection.execute(MESSAGES).fetchall()
    reply = f"Echo: {replacement or marker}"
    before = len(audit_events(profile))
    try:
        with Terminal(run_turn(cli, profile, entry)) as terminal:
            assert terminal.expect(b"> ") == ""
            terminal.send(marker)
            output = terminal.finish(success=True)
    except AssertionError:
        report_conversation_store_failure(
            profile, store_pack, fixture.pins[store_pack],
            f"{domain}.conversation-store@1.0",
        )
        raise
    lines = output.splitlines()
    assert lines[-2] == f"> {reply}", output
    assert json.loads(lines[-1])["status"] == "succeeded", output
    with closing(open_readonly(store)) as connection:
        assert connection.execute(MESSAGES).fetchall() == history + [
            ("user", marker), ("assistant", reply),
        ]
    effect = json_digest({
        "kind": "port-call", "shape": f"{domain}.prompt-builder@1.0", "op": "build",
    })
    args = json_digest({
        "ctx": {"conversation": fixture.conversation, "messages": [
            {"role": role, "content": content} for role, content in history
        ]},
        "input": {"conversation": fixture.conversation, "text": replacement or marker},
    })
    dispatches = [
        event["payload"]["args_digest"] for event in audit_events(profile)[before:]
        if event["kind"] == "dispatch" and event["payload"]["effect_digest"] == effect
    ]
    assert dispatches == [args], dispatches


def run_turn(cli: list[str], profile: Path, entry: dict[str, str]) -> list[str]:
    """Use the preset's ordinary input source and configured entry point."""
    return cli + [
        "profile", "run", "--profile", profile.name, "--source", entry["source_pack"],
        "--input-shape", entry["exact_input_view"], "--input-op", entry["operation"],
        "--once",
    ]


def profile_fixture(
    cli: list[str], root: Path, directory: Path, stock: Path,
) -> ProfileFixture:
    """Approve a fresh copy whose ceiling includes the chosen extension ports."""
    directory.mkdir()
    definition = json.loads(stock.read_bytes())
    for pin in definition["packs"]:
        locator = pin["source"].removeprefix("file:")
        source = (stock.parent / locator).resolve(strict=True)
        pin["source"] = f"file:{source}"
    (entry,) = definition["startup"]["inputs"]
    domain = entry["exact_input_view"].rsplit(".", 2)[0]
    store_pack = definition["bindings"][f"{domain}.conversation-store@1"]
    input_config = stock.parent.parent / entry["source_pack"] / "config.json"
    conversation = json.loads(input_config.read_bytes())["conversation"]
    role = f"{domain}.assemble-prompt@1.0"
    # The bundled Pack pins and startup configuration are copied as-is. The
    # user-selected ceiling explicitly permits this already verified open role.
    ports = definition["ceiling"]["ports"]
    if role not in ports:
        ports.append(role)
    derived = directory / "definition.json"
    derived.write_text(json.dumps(definition))
    pins = {pin["id"]: pin["sha256"] for pin in definition["packs"]}
    profile_id = approve(cli + [
        "profile", "create", "--definition", str(derived), "--name", definition["name"],
    ], pins, root)
    return ProfileFixture(
        root / "profiles" / profile_id, definition, entry, store_pack, conversation,
        role, pins,
    )


def replay(cli: list[str], root: Path, directory: Path, stock: Path) -> None:
    """Previously admitted configurations survive refused additions and conflicts."""
    fixture = profile_fixture(cli, root, directory, stock)
    profile = fixture.profile
    definition = fixture.definition
    entry = fixture.entry
    store_pack = fixture.store_pack
    role, pins = fixture.role, fixture.pins
    profile_id = profile.name
    domain = entry["exact_input_view"].rsplit(".", 2)[0]
    turn(cli, profile, fixture, "baseline")
    before = snapshot(cli, profile, directory / "before.json")
    resources = {key: definition["ceiling"][key] for key in ("wasm", "loops")}
    marker = "overlay v1"
    overlay = author(
        cli, directory / "approved", "local.acceptance-prompt-overlay", "1.0.0",
        role, marker, resources,
    )
    add = cli + ["profile", "add", "--profile", profile_id, "--pack", str(overlay.path)]

    with Terminal(add) as terminal:
        assert "overlay-permission" in terminal.finish(success=False)
    assert snapshot(cli, profile, directory / "no-limits.json") == before

    too_wide = directory / "over-limit.json"
    too_wide.write_text(json.dumps(overlay.permissions | {"audit_read": True}))
    with Terminal(add + ["--limits", str(too_wide)]) as terminal:
        assert "Pack limits must narrow" in terminal.finish(success=False)
    assert snapshot(cli, profile, directory / "over-limit-export.json") == before

    add += ["--limits", str(overlay.limits)]
    review_edit(add, profile, overlay, overlay.permissions, accept=False)
    assert snapshot(cli, profile, directory / "declined.json") == before
    head = review_edit(add, profile, overlay, overlay.permissions, accept=True)
    active = snapshot(cli, profile, directory / "active.json")
    assert active.head == head != before.head and active.epoch == before.epoch + 1
    assert active.approvals == before.approvals | {
        (overlay.pack, overlay.digest): overlay.permissions,
    }
    installed = json.loads(active.publication)
    assert {pin["id"]: pin["sha256"] for pin in installed["packs"]} == pins | {
        overlay.pack: overlay.digest,
    }
    assert installed["pack_limits"][overlay.pack] == overlay.permissions
    turn(cli, profile, fixture, "after install", marker)

    conflict = author(
        cli, directory / "conflicting", "local.acceptance-conflicting-overlay", "1.0.0",
        role, "conflict", resources,
    )
    with Terminal(cli + [
        "profile", "add", "--profile", profile_id, "--pack", str(conflict.path),
        "--limits", str(conflict.limits),
    ]) as terminal:
        assert "overlay-order" in terminal.finish(success=False)
    assert snapshot(cli, profile, directory / "after-conflict.json") == active
    turn(cli, profile, fixture, "after conflict", marker)
    print(f"{stock.stem}: decline, add, run, reject conflict and rerun", flush=True)

    updated_marker = "overlay v2"
    assert definition["ceiling"]["audit_append"]
    update = author(
        cli, directory / "update", overlay.pack, "1.1.0", role, updated_marker,
        resources, audit_append=True,
    )
    update_command = cli + [
        "profile", "update", "--profile", profile_id, "--pack", str(update.path),
        "--limits", str(update.limits),
    ]
    delta = update.permissions | {"ports": []} | {
        key: dict.fromkeys(update.permissions[key], 0) for key in ("wasm", "loops")
    }
    review_edit(update_command, profile, update, delta, accept=False)
    assert snapshot(cli, profile, directory / "declined-update.json") == active
    updated_head = review_edit(update_command, profile, update, delta, accept=True)
    publication_path = directory / "published.json"
    final = snapshot(cli, profile, publication_path)
    assert final.head == updated_head != active.head and final.epoch == active.epoch + 1
    assert final.approvals == active.approvals | {
        (update.pack, update.digest): update.permissions,
    }
    publication = json.loads(final.publication)
    final_pins = pins | {update.pack: update.digest}
    assert {pin["id"]: pin["sha256"] for pin in publication["packs"]} == final_pins
    assert publication["pack_limits"][update.pack] == update.permissions

    required = definition["bindings"][f"{domain}.prompt-builder@1"]
    with Terminal(cli + [
        "profile", "remove", "--profile", profile_id, "--pack", required,
    ]) as terminal:
        assert "required-port" in terminal.finish(success=False)
    assert snapshot(cli, profile, directory / "after-removal.json") == final
    turn(cli, profile, fixture, "after update", updated_marker)

    imported_id = approve(cli + [
        "profile", "import", str(publication_path), "--name", "Imported overlay",
    ], final_pins, root)
    assert imported_id != profile_id
    imported = root / "profiles" / imported_id
    checked = snapshot(cli, imported, directory / "reexported.json")
    assert checked.publication == final.publication
    assert checked.approvals == {
        (pack, digest): final.approvals[pack, digest]
        for pack, digest in final_pins.items()
    }
    assert not (imported / "data" / store_pack / "private.sqlite").is_file()
    turn(cli, imported, fixture, "after import", updated_marker)
    print(f"{stock.stem}: update, reject removal, fresh import and run", flush=True)


def main(binary: str) -> None:
    """Exercise a fresh Echo Profile and a Profile derived from bundled Harness."""
    profiles = Path(__file__).resolve().parents[2] / "packs/profiles"
    with tempfile.TemporaryDirectory(prefix="tobkiri-cli-pack-overlay-") as directory:
        base = Path(directory).resolve()
        root = base / "data"
        root.mkdir(mode=0o700)
        cli = [str(Path(binary).resolve()), "--data-root", str(root)]
        for profile in ("echo", "harness-mock"):
            replay(cli, root, base / profile, profiles / f"{profile}.profile.json")


if __name__ == "__main__":
    main(sys.argv[1])
