#!/usr/bin/env python3
"""Install an independently authored Wasm tool and call it in a real CLI turn."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from issue_1520_cli_pack_overlay import (
    Artifact,
    MESSAGES,
    audit_events,
    build_pack,
    profile_fixture,
    review_edit,
    run_turn,
    snapshot,
    turn,
)
from issue_1520_cli_roundtrip import Terminal, json_digest, open_readonly

TOOL = "local.acceptance-typed-tool"
PORT = f"{TOOL}.transform"
VIEW = f"{PORT}@1.0"
CALLER = "local.acceptance-typed-tool-overlay"
DECLARATION = {"shape": PORT, "version": "1.0", "optional": False}


def author_tool(
    cli: list[str], directory: Path, component: Path, wit: Path,
    resources: dict[str, Any],
) -> Artifact:
    """Build a Pack from the new component and its source WIT contract."""
    stage = directory / "stage"
    (stage / "wit").mkdir(parents=True)
    shutil.copyfile(component, stage / "component.wasm")
    shutil.copyfile(wit, stage / "wit/typed-tool.wit")
    (stage / "wit/transform.shape.json").write_text(json.dumps({
        "cardinality": "one", "conformance": None, "callbacks": {}, "streams": {},
    }))
    return build_pack(cli, directory, {
        "format": "tobkiri.pack/1", "id": TOOL, "version": "1.0.0",
        "runtime": {
            "kind": "wasm", "component": "component.wasm",
            "toolchain": "rust/1.94.0;wit-bindgen/0.62.0",
        },
        "shapes": [{
            "name": PORT, "version": "1.0", "kind": "port",
            "wit": "wit/typed-tool.wit",
        }],
        "ports": {"provides": [DECLARATION], "consumes": [], "untyped": []},
        "flows": [], "overlays": [], "stores": [],
        "files": ["component.wasm", "wit/typed-tool.wit", "wit/transform.shape.json"],
        "permissions": resources | {"audit_append": True},
    })


def author_caller(
    cli: list[str], directory: Path, role: str, resources: dict[str, Any],
) -> Artifact:
    """Route a typed tool result back into the existing prompt builder."""
    stage = directory / "stage"
    stage.mkdir(parents=True)
    path = "tool.overlay.json"
    (stage / path).write_text(json.dumps({
        "format": "tobkiri.overlay/1", "id": "run-typed-tool", "target": {"role": role},
        "body": [
            {
                "id": "call-tool", "out": "rewritten",
                "call": {"shape": VIEW, "op": "rewrite", "args": {"text": "$input.input.text"}},
            },
            {
                "id": "continue-with-tool-result",
                "next": {"args": {
                    "ctx": "$input.ctx",
                    "input": {"conversation": "$input.input.conversation", "text": "$rewritten"},
                }},
            },
        ],
    }))
    return build_pack(cli, directory, {
        "format": "tobkiri.pack/1", "id": CALLER, "version": "1.0.0",
        "runtime": {"kind": "data"}, "shapes": [],
        "ports": {"provides": [], "consumes": [DECLARATION], "untyped": []},
        "flows": [], "overlays": [path], "stores": [], "files": [path],
        "permissions": resources | {"ports": [role, VIEW]},
    })


def edit(cli: list[str], action: str, profile: Path, artifact: Artifact, limits: Path) -> list[str]:
    return cli + [
        "profile", action, "--profile", profile.name, "--pack", str(artifact.path),
        "--limits", str(limits),
    ]


def author_flow_upgrade(cli: list[str], directory: Path, source: Path) -> Artifact:
    """Keep the existing Flow code and declare the new tool on its owner."""
    document = json.loads((source / "pack.json").read_bytes())
    stage = directory / "stage"
    files = [entry["path"] for entry in document["files"]]
    for path in files:
        target = stage / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / path, target)
    release, patch = document["version"].rsplit(".", 1)
    document["version"] = f"{release}.{int(patch) + 1}"
    document["files"] = files
    document["ports"]["consumes"].append(DECLARATION)
    document["permissions"]["ports"].append(VIEW)
    return build_pack(cli, directory, document)


def permission_delta(permissions: dict[str, Any], **new: Any) -> dict[str, Any]:
    zeros = {key: dict.fromkeys(permissions[key], 0) for key in ("wasm", "loops")}
    return permissions | zeros | new


def run_denied(cli: list[str], profile: Path, entry: dict[str, str], text: str) -> None:
    with Terminal(run_turn(cli, profile, entry)) as terminal:
        assert terminal.expect(b"> ") == ""
        terminal.send(text)
        failed = terminal.finish(success=False)
    assert "step call-tool: denied" in failed, failed
    assert "Echo:" not in failed, failed


def check_tool_call(
    rows: list[dict[str, Any]], flow: Artifact, tool: Artifact, text: str, *, passed: bool,
) -> None:
    effect = json_digest({"kind": "port-call", "shape": VIEW, "op": "rewrite"})
    (dispatch,) = [
        event for event in rows
        if event["kind"] == "dispatch" and event["payload"]["effect_digest"] == effect
    ]
    assert dispatch["principal"] == {"kind": "pack", "pack": flow.pack, "hash": flow.digest}
    assert dispatch["payload"]["args_digest"] == json_digest({"text": text})
    guest = [
        event for event in rows
        if event["kind"] == "guest-event" and event["payload"]["kind"] == "typed-tool-rewrite"
    ]
    if passed:
        (event,) = guest
        assert event["principal"] == {"kind": "pack", "pack": tool.pack, "hash": tool.digest}
        assert event["payload"] == {"kind": "typed-tool-rewrite", "payload": {}}
    else:
        assert guest == []
        assert any(
            row["kind"] == "denied"
            and row["principal"] == {"kind": "pack", "pack": tool.pack, "hash": tool.digest}
            and row["payload"][:2] == ["audit-append", "denied"]
            for row in rows
        )


def replay(
    cli: list[str], root: Path, directory: Path, stock: Path, component: Path, wit: Path,
) -> None:
    fixture = profile_fixture(cli, root, directory, stock)
    profile, entry = fixture.profile, fixture.entry
    resources = {key: fixture.definition["ceiling"][key] for key in ("wasm", "loops")}
    tool = author_tool(cli, directory / "tool", component, wit, resources)
    caller = author_caller(cli, directory / "caller", fixture.role, resources)

    restricted = directory / "tool-read-only.json"
    narrow = tool.permissions | {"audit_append": False}
    restricted.write_text(json.dumps(narrow))
    original = snapshot(cli, profile, directory / "original.json")
    assert VIEW not in json.loads(original.publication)["ceiling"]["ports"]
    add = edit(cli, "add", profile, tool, restricted)
    with Terminal(add + ["--allow-provided-port", fixture.role]) as terminal:
        assert "New Pack must define and provide the exact port" in terminal.finish(success=False)
    assert snapshot(cli, profile, directory / "unchanged.json") == original
    add += ["--allow-provided-port", VIEW]
    review_edit(add, profile, tool, narrow, accept=False)
    assert snapshot(cli, profile, directory / "declined-add.json") == original
    review_edit(add, profile, tool, narrow, accept=True)
    review_edit(
        edit(cli, "add", profile, caller, caller.limits), profile, caller,
        caller.permissions, accept=True,
    )

    flow_pack = fixture.role.removesuffix(".assemble-prompt@1.0")
    base_source = stock.parent.parent / flow_pack
    base_limits = fixture.definition["pack_limits"][flow_pack]
    base_limits_path = directory / "base-limits.json"
    base_limits_path.write_text(json.dumps(base_limits))
    base = Artifact(base_source, base_limits_path, flow_pack, fixture.pins[flow_pack], base_limits)
    before = len(audit_events(profile))
    run_denied(cli, profile, entry, "w")
    rows = audit_events(profile)[before:]
    assert any(
        row["kind"] == "denied"
        and row["principal"] == {"kind": "pack", "pack": base.pack, "hash": base.digest}
        and row["payload"][:2] == ["port-call", "denied"]
        for row in rows
    )

    flow = author_flow_upgrade(cli, directory / "flow", base_source)
    flow.limits.write_text(json.dumps(base_limits | {"ports": [*base_limits["ports"], VIEW]}))
    review_edit(
        edit(cli, "update", profile, flow, flow.limits), profile, flow,
        permission_delta(flow.permissions, ports=[VIEW], audit_append=False), accept=True,
    )

    before = len(audit_events(profile))
    run_denied(cli, profile, entry, "x")
    check_tool_call(audit_events(profile)[before:], flow, tool, "x", passed=False)
    with closing(open_readonly(profile / "data" / fixture.store_pack / "private.sqlite")) as db:
        assert db.execute(MESSAGES).fetchall() == []

    review_edit(
        edit(cli, "update", profile, tool, tool.limits), profile, tool,
        permission_delta(tool.permissions), accept=True,
    )
    before = len(audit_events(profile))
    turn(cli, profile, entry, fixture.store_pack, fixture.conversation, "y", "tool-y")
    check_tool_call(audit_events(profile)[before:], flow, tool, "y", passed=True)

    active = snapshot(cli, profile, directory / "active.json")
    assert VIEW in json.loads(active.publication)["ceiling"]["ports"]
    remove = cli + ["profile", "remove", "--profile", profile.name, "--pack"]
    with Terminal(remove + [tool.pack]) as terminal:
        assert "Profile has conflicts" in terminal.finish(success=False)
    assert snapshot(cli, profile, directory / "in-use.json") == active
    for command in (
        remove + [caller.pack],
        edit(cli, "update", profile, base, base.limits),
        remove + [tool.pack],
    ):
        with Terminal(command) as terminal:
            terminal.finish(success=True)
    restored = snapshot(cli, profile, directory / "restored.json")
    assert restored.publication == original.publication
    turn(cli, profile, entry, fixture.store_pack, fixture.conversation, "z")
    print(f"{stock.stem}: typed tool capability denial, approval, execution, and safe removal")


def main(binary: str, component: str) -> None:
    repo = Path(__file__).resolve().parents[2]
    wit = repo / "crates/test-guests/typed-tool/wit/typed-tool.wit"
    with tempfile.TemporaryDirectory(prefix="tobkiri-cli-typed-tool-") as directory:
        base = Path(directory).resolve()
        root = base / "data"
        root.mkdir(mode=0o700)
        cli = [str(Path(binary).resolve()), "--data-root", str(root)]
        for profile in ("echo", "harness-mock"):
            stock = repo / "packs/profiles" / f"{profile}.profile.json"
            replay(cli, root, base / profile, stock, Path(component).resolve(), wit)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
