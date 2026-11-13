#!/usr/bin/env python3
"""Replay Profile publication and User overlays through the CLI in a CI PTY."""

from __future__ import annotations

from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
from typing import Any


APPROVAL = b"Type approve to approve this request; anything else denies it: "
PROFILE_ID = re.compile(r"prof_[0-9A-Z]{26}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
FLOW_PACK = "local.echo-chat"
STORE_SHAPE = "local.echo-chat.conversation-store@1.0"
OUTPUT_SHAPE = "local.echo-chat.assistant-output@1.0"
OVERLAY_MARKER = "cli-user-overlay-replaced-save"
OVERLAY_ARGS = {"event": {"kind": "delta", "text": f"{OVERLAY_MARKER}\n"}}
PUBLIC_FIELDS = {
    "format", "packs", "publishers", "ceiling", "user_permissions",
    "pack_limits", "bindings", "overlay_order", "user_overlays", "startup",
}


class Terminal:
    """Own one private terminal, its bounded transcript, and its child."""

    def __init__(self, command: list[str]) -> None:
        self.master, slave = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave)
            attrs[3] = (attrs[3] | termios.ICANON) & ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            self.child = subprocess.Popen(
                command, stdin=slave, stderr=slave, stdout=slave,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.deadline = time.monotonic() + 120
        self.output = bytearray()
        self.offset = 0

    def read(self) -> bool:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
            raise AssertionError(f"CLI terminal timed out: {self.output!r}")
        try:
            chunk = os.read(self.master, 8192)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            chunk = b""
        self.output.extend(chunk)
        assert len(self.output) <= 65536, "CLI terminal transcript exceeded its bound"
        return bool(chunk)

    def expect(self, marker: bytes) -> str:
        while (end := self.output.find(marker, self.offset)) == -1:
            assert self.read(), f"CLI exited before {marker!r}: {self.output!r}"
        result = self.output[self.offset:end].decode().replace("\r\n", "\n")
        self.offset = end + len(marker)
        return result

    def send(self, line: str) -> None:
        data = (line + "\n").encode()
        assert os.write(self.master, data) == len(data)

    def finish(self, *, success: bool) -> str:
        while self.read():
            pass
        output = self.output.decode().replace("\r\n", "\n")
        code = self.child.wait(timeout=max(0.1, self.deadline - time.monotonic()))
        assert (code == 0) == success, f"CLI exit {code}: {output}"
        assert self.output.find(APPROVAL, self.offset) == -1, output
        return output

    def __enter__(self) -> Terminal:
        return self

    def __exit__(self, *_error: object) -> None:
        if self.child.poll() is None:
            self.child.kill()
            self.child.wait(timeout=5)
        os.close(self.master)


def review_prompt(terminal: Terminal) -> tuple[str, dict[str, Any], str]:
    """Read one Normal approval request presented to the User."""
    text = terminal.expect(APPROVAL)
    assert "\nTobkiri approval\n" in text and "\nRisk: Normal\n" in text, text
    assert '\nPrincipal: {"kind":"user"}\n' in text, text
    identity = text.split("\nProfile: ", 1)[1].splitlines()[0]
    assert PROFILE_ID.fullmatch(identity), text
    preview = text.split("\nEffect:\n", 1)[1].split("\nSummary: ", 1)[0]
    return identity, json.loads(preview), text


def review_pack(terminal: Terminal, remaining: dict[str, str]) -> str:
    """Require a review for the exact hash of one remaining Pack."""
    identity, effect, text = review_prompt(terminal)
    assert effect == {
        "kind": "permissions", "pack": effect["pack"],
        "hash": remaining.pop(effect["pack"]),
    }, text
    return identity


def approve_packs(terminal: Terminal, pins: dict[str, str]) -> str:
    """Approve each expected Pack once for the same fresh Profile."""
    remaining = pins.copy()
    identities = set()
    while remaining:
        identities.add(review_pack(terminal, remaining))
        terminal.send("approve")
    assert len(identities) == 1, identities
    return identities.pop()


def review_user(
    terminal: Terminal, profile: str, policy: dict[str, Any],
) -> tuple[str, str, int]:
    """Require the exact Profile, revision, epoch and policy in the User review."""
    identity, effect, text = review_prompt(terminal)
    assert identity == profile, text
    assert set(effect) == {"kind", "profile_hash", "revision", "epoch"}, text
    assert effect["kind"] == "user-permissions", text
    profile_hash = effect["profile_hash"]
    revision = effect["revision"]
    epoch = effect["epoch"]
    assert DIGEST.fullmatch(profile_hash) and DIGEST.fullmatch(revision), text
    assert "\nSecurity epoch: 0\n" in text and epoch == 1, text
    summary = text.split("\nSummary: ", 1)[1].strip()
    assert json.loads(json.loads(summary)) == policy, text
    arguments = json_digest([profile_hash, revision, epoch, policy])
    assert f"\nArguments digest: {arguments}\n" in text, text
    return profile_hash, revision, epoch


def json_digest(value: Any) -> str:
    """Hash the compact sorted JSON used for these ASCII fixture values."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing replay database without creating or modifying it."""
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def approve(
    command: list[str], pins: dict[str, str], root: Path,
    *, user_policy: dict[str, Any] | None = None,
) -> str:
    """Complete the ordinary CLI approvals and check any committed User policy."""
    user_approval = None
    with Terminal(command) as terminal:
        identity = approve_packs(terminal, pins)
        if user_policy is not None:
            user_approval = review_user(terminal, identity, user_policy)
            terminal.send("approve")
        output = terminal.finish(success=True)
        # The approval prompt has no trailing newline and the PTY has echo
        # disabled, so the printed ID starts immediately after that prompt.
        assert output.rsplit(APPROVAL.decode(), 1)[1].strip() == identity, output
    if user_approval is not None:
        profile_path = root / "profiles" / identity
        assert (profile_path / "head").read_text().strip() == user_approval[0]
        with closing(open_readonly(profile_path / "approvals.sqlite")) as connection:
            approved_pins = dict(
                connection.execute("SELECT pack, hash FROM permissions"),
            )
            assert approved_pins == pins
            approved = connection.execute(
                "SELECT profile_hash, revision, epoch, body FROM user_approvals",
            ).fetchall()
            assert len(approved) == 1 and approved[0][:3] == user_approval
            assert json.loads(approved[0][3]) == user_policy
    return identity


def turn(cli: list[str], identity: str, marker: str) -> str:
    """Drive a real Echo input and return its terminal transcript."""
    command = cli + [
        "profile", "run", "--profile", identity, "--source", "local.echo-input",
        "--input-shape", "local.echo-chat.user-input@1.0", "--once",
    ]
    with Terminal(command) as terminal:
        assert terminal.expect(b"> ") == "", "unexpected output before the Echo input"
        terminal.send(marker)
        output = terminal.finish(success=True)
        assert f"Echo: {marker}" in output, output
        return output


def messages(
    root: Path, identity: str, *, require_store: bool = False,
) -> list[tuple[str, str]]:
    """Read the physical Echo store after a turn or check a fresh Profile."""
    path = root / "profiles" / identity / "data/local.echo-store/private.sqlite"
    if not require_store and not path.is_file():
        return []
    with closing(open_readonly(path)) as connection:
        return connection.execute(
            "SELECT role, content FROM messages ORDER BY seq",
        ).fetchall()


def publish(cli: list[str], identity: str, output: Path) -> bytes:
    """Export the current Profile through the ordinary CLI."""
    subprocess.run(cli + [
        "profile", "export", "--profile", identity, "--output", str(output),
    ], check=True, timeout=30)
    return output.read_bytes()


def overlay_roundtrip(
    cli: list[str], root: Path, document: dict[str, Any], pins: dict[str, str],
) -> None:
    """Replace the closed Echo save by its exact Flow key and step ID."""
    policy = {
        "network": {"origins": [], "user_origins": None},
        "fs": [], "proc": [], "pty": False, "secrets": [],
        "storage": {"private": False, "public_read": False, "public_write": False},
        "native": False, "cross_profile_storage": False, "audit_read": False,
        "audit_append": False, "host_io": [], "ports": [OUTPUT_SHAPE],
        "mediator": False,
        "wasm": document["ceiling"]["wasm"], "loops": document["ceiling"]["loops"],
    }
    overlay = {
        "format": "tobkiri.overlay/1",
        "id": "replace-save",
        "target": {"flow": f"{FLOW_PACK}/turn", "step": "save"},
        "order": {"before": [], "after": []},
        "body": [
            {
                "id": "announce-replacement",
                "overlay": "closed",
                "call": {
                    "shape": OUTPUT_SHAPE,
                    "op": "emit",
                    "args": OVERLAY_ARGS,
                },
            },
            {
                "id": "replacement-result",
                "overlay": "closed",
                "return": {"ok": {"save": OVERLAY_MARKER}},
            },
        ],
    }
    definition = root.parent / "overlay-definition.json"
    definition.write_text(json.dumps(document | {
        "user_permissions": policy, "user_overlays": [overlay],
    }))
    source_input = "cli-overlay-source-input"
    source = approve(cli + [
        "profile", "create", "--definition", str(definition),
        "--name", "CLI User overlay",
    ], pins, root, user_policy=policy)
    check_overlay_turn(cli, root, source, source_input, pins[FLOW_PACK])

    publication = root.parent / "overlay-publication.json"
    published = publish(cli, source, publication)
    exported = json.loads(published)
    assert exported["user_permissions"] == policy
    assert exported["user_overlays"] == [overlay]
    assert exported["packs"] == document["packs"]
    assert source.encode() not in published
    assert source_input.encode() not in published

    command = cli + [
        "profile", "import", str(publication), "--name", "CLI overlay import",
    ]
    with Terminal(command) as terminal:
        refused = approve_packs(terminal, pins)
        review_user(terminal, refused, policy)
        terminal.send("deny")
        assert "user declined" in terminal.finish(success=False)
    refused_path = root / "profiles" / refused
    assert not (refused_path / "head").exists()
    with closing(open_readonly(refused_path / "approvals.sqlite")) as connection:
        assert connection.execute("SELECT 1 FROM permissions").fetchone() is None
        assert connection.execute("SELECT 1 FROM user_approvals").fetchone() is None

    imported = approve(command, pins, root, user_policy=policy)
    assert len({source, refused, imported}) == 3
    check_overlay_turn(cli, root, imported, "cli-overlay-import-input", pins[FLOW_PACK])
    assert publish(cli, imported, root.parent / "overlay-reexport.json") == published
    print("CLI User overlay replay: replace closed save, deny, import, run, re-export")


def check_overlay_turn(
    cli: list[str], root: Path, identity: str, marker: str, flow_hash: str,
) -> None:
    """Observe the replacement output and verify the original append left no rows."""
    assert messages(root, identity) == []
    output = turn(cli, identity, marker)
    lines = output.splitlines()
    assert lines[-2] == OVERLAY_MARKER, output
    assert json.loads(lines[-1]) == {
        "routes": [{
            "error": None, "index": 0, "result": {"save": OVERLAY_MARKER},
            "status": "succeeded",
        }],
        "status": "succeeded",
    }, output
    assert messages(root, identity, require_store=True) == []

    append = json_digest({"kind": "port-call", "shape": STORE_SHAPE, "op": "append"})
    emit = json_digest({"kind": "port-call", "shape": OUTPUT_SHAPE, "op": "emit"})
    emit_args = json_digest(OVERLAY_ARGS)
    path = root / "profiles" / identity / "audit.sqlite"
    replacement = []
    with closing(open_readonly(path)) as connection:
        for (body,) in connection.execute("SELECT body FROM audit ORDER BY seq"):
            event = json.loads(body)
            if event["kind"] != "dispatch":
                continue
            effect = event["payload"]["effect_digest"]
            assert effect != append, event
            if effect == emit and event["payload"]["args_digest"] == emit_args:
                replacement.append(event["principal"])
    assert replacement == [{"kind": "pack", "pack": FLOW_PACK, "hash": flow_hash}]


def main(binary: str) -> None:
    """Run the publication control and User overlay through the CLI binary."""
    packs = Path(__file__).resolve().parents[2] / "packs"
    definition = packs / "profiles/echo.profile.json"
    pins = {
        pin["id"]: pin["sha256"] for pin in json.loads(definition.read_bytes())["packs"]
    }
    assert len(pins) == 6
    flow = json.loads((packs / FLOW_PACK / "flows/turn.flow.json").read_bytes())
    assert flow["id"] == "turn"
    save = next(step for step in flow["steps"] if step["id"] == "save")
    assert save.get("overlay", "closed") == "closed"
    assert save["call"]["shape"] == STORE_SHAPE and save["call"]["op"] == "append"
    with tempfile.TemporaryDirectory(prefix="tobkiri-cli-roundtrip-") as directory:
        base = Path(directory).resolve()
        root = base / "data"
        root.mkdir(mode=0o700)
        cli = [str(Path(binary).resolve()), "--data-root", str(root)]
        source = approve(cli + [
            "profile", "create", "--definition", str(definition),
            "--name", "CLI source",
        ], pins, root)
        original = "cli-source-only-marker"
        turn(cli, source, original)
        expected = [("user", original), ("assistant", f"Echo: {original}")]
        assert messages(root, source) == expected

        publication = base / "publication.json"
        published = publish(cli, source, publication)
        document = json.loads(published)
        assert set(document) <= PUBLIC_FIELDS
        assert {"format", "packs", "ceiling", "startup"} <= set(document)
        assert {pin["id"]: pin["sha256"] for pin in document["packs"]} == pins
        assert source.encode() not in published and original.encode() not in published

        command = cli + ["profile", "import", str(publication), "--name", "CLI import"]
        with Terminal(command) as terminal:
            refused = review_pack(terminal, pins.copy())
            terminal.send("deny")
            assert "user declined" in terminal.finish(success=False)
        assert refused != source
        assert not (root / "profiles" / refused / "head").exists()

        imported = approve(command, pins, root)
        assert imported not in {source, refused}
        assert messages(root, imported) == []
        marker = "cli-import-only-marker"
        turn(cli, imported, marker)
        assert messages(root, imported) == [
            ("user", marker), ("assistant", f"Echo: {marker}"),
        ]
        assert messages(root, source) == expected

        assert publish(cli, imported, base / "reexport.json") == published
        print(
            "CLI publication replay: create, run, export, deny, fresh import, "
            "run, re-export"
        )
        overlay_roundtrip(cli, root, document, pins)


if __name__ == "__main__":
    main(sys.argv[1])
