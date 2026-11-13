#!/usr/bin/env python3
"""Replay Profile publication through the actual CLI in an isolated CI PTY."""

from __future__ import annotations

from contextlib import closing
import errno
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


APPROVAL = b"Type approve to approve this request; anything else denies it: "
PROFILE_ID = re.compile(r"prof_[0-9A-Z]{26}")
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


def review(terminal: Terminal, remaining: dict[str, str]) -> str:
    """Accept only the expected Normal review of a pinned fixture Pack."""
    text = terminal.expect(APPROVAL)
    assert "\nTobkiri approval\n" in text and "\nRisk: Normal\n" in text, text
    assert '\nPrincipal: {"kind":"user"}\n' in text, text
    identity = text.split("\nProfile: ", 1)[1].splitlines()[0]
    assert PROFILE_ID.fullmatch(identity), text
    preview = text.split("\nEffect:\n", 1)[1].split("\nSummary: ", 1)[0]
    effect = json.loads(preview)
    assert effect == {
        "kind": "permissions", "pack": effect["pack"],
        "hash": remaining.pop(effect["pack"]),
    }, text
    return identity


def approve(command: list[str], pins: dict[str, str]) -> str:
    remaining = pins.copy()
    with Terminal(command) as terminal:
        identities = set()
        while remaining:
            identities.add(review(terminal, remaining))
            terminal.send("approve")
        output = terminal.finish(success=True)
    identity = output.splitlines()[-1]
    assert identities == {identity}, output
    return identity


def turn(cli: list[str], identity: str, marker: str) -> None:
    command = cli + [
        "profile", "run", "--profile", identity, "--source", "local.echo-input",
        "--input-shape", "local.echo-chat.user-input@1.0", "--once",
    ]
    with Terminal(command) as terminal:
        assert terminal.expect(b"> ") == "", "unexpected output before the Echo input"
        terminal.send(marker)
        assert f"Echo: {marker}" in terminal.finish(success=True)


def messages(root: Path, identity: str) -> list[tuple[str, str]]:
    path = root / "profiles" / identity / "data/local.echo-store/private.sqlite"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return connection.execute("SELECT role, content FROM messages ORDER BY seq").fetchall()


def main(binary: str) -> None:
    definition = Path(__file__).resolve().parents[2] / "packs/profiles/echo.profile.json"
    pins = {pin["id"]: pin["sha256"] for pin in json.loads(definition.read_bytes())["packs"]}
    assert len(pins) == 6
    with tempfile.TemporaryDirectory(prefix="tobkiri-cli-roundtrip-") as directory:
        base = Path(directory).resolve()
        root = base / "data"
        root.mkdir(mode=0o700)
        cli = [str(Path(binary).resolve()), "--data-root", str(root)]
        source = approve(cli + [
            "profile", "create", "--definition", str(definition), "--name", "CLI source",
        ], pins)
        original = "cli-source-only-marker"
        turn(cli, source, original)
        expected = [("user", original), ("assistant", f"Echo: {original}")]
        assert messages(root, source) == expected

        publication = base / "publication.json"
        subprocess.run(cli + [
            "profile", "export", "--profile", source, "--output", str(publication),
        ], check=True, timeout=30)
        published = publication.read_bytes()
        document = json.loads(published)
        assert set(document) <= PUBLIC_FIELDS
        assert {"format", "packs", "ceiling", "startup"} <= set(document)
        assert {pin["id"]: pin["sha256"] for pin in document["packs"]} == pins
        assert source.encode() not in published and original.encode() not in published

        command = cli + ["profile", "import", str(publication), "--name", "CLI import"]
        with Terminal(command) as terminal:
            refused = review(terminal, pins.copy())
            terminal.send("deny")
            assert "user declined" in terminal.finish(success=False)
        assert refused != source
        assert not (root / "profiles" / refused / "head").exists()

        imported = approve(command, pins)
        assert imported not in {source, refused}
        assert messages(root, imported) == []
        marker = "cli-import-only-marker"
        turn(cli, imported, marker)
        assert messages(root, imported) == [("user", marker), ("assistant", f"Echo: {marker}")]
        assert messages(root, source) == expected

        reexport = base / "reexport.json"
        subprocess.run(cli + [
            "profile", "export", "--profile", imported, "--output", str(reexport),
        ], check=True, timeout=30)
        assert reexport.read_bytes() == published
        print("CLI publication replay: create, run, export, deny, fresh import, run, re-export")


if __name__ == "__main__":
    main(sys.argv[1])
