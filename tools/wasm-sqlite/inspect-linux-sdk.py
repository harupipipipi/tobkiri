#!/usr/bin/env python3
"""Bounded read-only official SDK stream inspection; never save/extract/execute."""
import gzip
import hashlib
import io
import json
import posixpath
import sys
import urllib.request

URL = "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-34/wasi-sdk-34.0-x86_64-linux.tar.gz"
EXPECTED = "b761e3a0721dbae9c09a0059e5fdb2bf917d1b4a8a7b430fb3b5aafb0984b2c4"
ROOT = "wasi-sdk-34.0-x86_64-linux"
CHUNK = 64 * 1024
MAX_COMPRESSED = 256 * 1024**2
MAX_EXPANDED = 2 * 1024**3
MAX_MEMBERS = 100_000
MAX_SELECTED = 4096
MAX_METADATA = 16 * 1024
MAX_MEMBER_BYTES = 512 * 1024**2
MAX_TOOL_BYTES = 256 * 1024**2

class InspectionError(ValueError): pass

class BoundedHashReader(io.RawIOBase):
    def __init__(self, stream):
        self.stream, self.hash, self.bytes = stream, hashlib.sha256(), 0
    def readable(self): return True
    def read(self, size=-1):
        block = self.stream.read(CHUNK if size < 0 else min(size, CHUNK))
        self.bytes += len(block)
        if self.bytes > MAX_COMPRESSED: raise InspectionError("compressed input bound")
        self.hash.update(block)
        return block

class Expanded:
    def __init__(self, stream): self.stream, self.bytes = stream, 0
    def read(self, size):
        block = self.stream.read(min(size, CHUNK))
        self.bytes += len(block)
        if self.bytes > MAX_EXPANDED: raise InspectionError("expanded input bound")
        return block
    def exact(self, size):
        if size > MAX_METADATA: raise InspectionError("header allocation bound")
        output = bytearray()
        while len(output) < size:
            part = self.read(size - len(output))
            if not part: raise InspectionError("truncated archive")
            output.extend(part)
        return bytes(output)

def cstring(data):
    text = data.split(b"\0", 1)[0].decode("utf-8")
    if len(text.encode()) > 512 or any(ord(char) < 32 for char in text): raise InspectionError("path bound")
    return text

def path(text):
    if text.startswith("/"): raise InspectionError("absolute archive path")
    normalized = posixpath.normpath(text)
    if not (normalized == ROOT or normalized.startswith(ROOT + "/")):
        raise InspectionError("archive path escaped exact SDK root")
    if len(normalized.encode()) > 512 or len(normalized.split("/")) > 32: raise InspectionError("path bound")
    return normalized

def octal(data):
    if data[0] & 128: raise InspectionError("base256 tar numeric header unavailable")
    text = data.rstrip(b"\0 ").lstrip(b" ") or b"0"
    return int(text, 8)

def pax(data):
    fields, offset = {}, 0
    while offset < len(data):
        end = data.find(b" ", offset)
        if end < 0: raise InspectionError("invalid PAX header")
        length = int(data[offset:end])
        if length <= end - offset + 1 or offset + length > len(data): raise InspectionError("PAX length")
        entry = data[end + 1:offset + length]
        if not entry.endswith(b"\n") or b"=" not in entry: raise InspectionError("PAX record")
        key, value = entry[:-1].split(b"=", 1)
        if len(fields) >= 32 or len(key) > 128 or len(value) > 512: raise InspectionError("PAX field bound")
        fields[key.decode()] = value.decode()
        offset += length
    return fields

def main():
    inventory, pending, global_pax, long_name, long_link = {}, {}, {}, None, None
    seen_paths = set()
    whole_hash, sysroot_hash, sysroot_members = hashlib.sha256(), hashlib.sha256(), 0
    def record(name, entry):
        nonlocal sysroot_members
        encoded = json.dumps([name,entry],sort_keys=True,separators=(",",":")).encode()
        framed = len(encoded).to_bytes(4,"big") + encoded
        whole_hash.update(framed)
        if name.startswith(ROOT + "/share/wasi-sysroot/"):
            sysroot_hash.update(framed); sysroot_members += 1
        if name.startswith(ROOT + "/bin/") or name.endswith("/libc.a") or ".so" in posixpath.basename(name):
            if len(inventory) >= MAX_SELECTED: raise InspectionError("selected metadata bound")
            inventory[name] = entry
    with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent":"tobkiri-pinned-sdk-inspection"}), timeout=45) as response:
        raw = BoundedHashReader(response)
        expanded = Expanded(gzip.GzipFile(fileobj=raw, mode="rb"))
        members = 0
        while True:
            header = expanded.exact(512)
            if not any(header):
                if any(expanded.exact(512)): raise InspectionError("invalid tar end")
                break
            checksum = octal(header[148:156])
            if sum(header[:148]) + 8 * 32 + sum(header[156:]) != checksum: raise InspectionError("tar checksum")
            if header[257:262] != b"ustar": raise InspectionError("unsupported tar header")
            size, kind = octal(header[124:136]), header[156:157]
            if size > MAX_MEMBER_BYTES: raise InspectionError("member byte bound")
            name = cstring(header[:100]); prefix = cstring(header[345:500])
            if prefix: name = prefix + "/" + name
            link = cstring(header[157:257])
            members += 1
            if members > MAX_MEMBERS: raise InspectionError("member count bound")
            if kind in (b"x", b"g", b"L", b"K"):
                data = expanded.exact(size)
                expanded.exact((-size) % 512)
                if kind == b"x": pending = pax(data)
                elif kind == b"g": global_pax = pax(data)
                elif kind == b"L": long_name = cstring(data)
                else: long_link = cstring(data)
                continue
            fields = dict(global_pax); fields.update(pending); pending = {}
            name = path(fields.get("path", long_name or name)); long_name = None
            link = fields.get("linkpath", long_link or link); long_link = None
            size = int(fields.get("size", size))
            if not 0 <= size <= MAX_MEMBER_BYTES: raise InspectionError("member byte bound")
            name_hash = hashlib.sha256(name.encode()).digest()
            if name_hash in seen_paths: raise InspectionError("duplicate archive member")
            seen_paths.add(name_hash)
            if kind in (b"0", b"\0"):
                digest, remaining, first = hashlib.sha256(), size, b""
                while remaining:
                    block = expanded.read(min(remaining, CHUNK))
                    if not block: raise InspectionError("truncated member")
                    if not first: first = block[:64]
                    digest.update(block); remaining -= len(block)
                entry = {"kind":"file", "bytes":size, "sha256":digest.hexdigest()}
                if first[:4] == b"\x7fELF":
                    entry["elf"] = {"class":first[4],"endianness":first[5],"machine":int.from_bytes(first[18:20],"little" if first[5] == 1 else "big")}
                record(name, entry)
            elif kind in (b"1", b"2"):
                if size: raise InspectionError("link with payload")
                target = path(link if kind == b"1" else posixpath.join(posixpath.dirname(name), link))
                record(name, {"kind":"hardlink" if kind == b"1" else "symlink", "target":target})
            elif kind == b"5":
                if size: raise InspectionError("directory with payload")
                record(name, {"kind":"directory"})
            else: raise InspectionError("unsupported archive member kind")
            expanded.exact((-size) % 512)
        # Read to decompressor EOF (including CRC checks), then raw EOF. Nothing
        # is extracted or stored; the full compressed stream digest is mandatory.
        while block := expanded.read(CHUNK):
            if any(block): raise InspectionError("unexpected nonzero tar trailer")
        while raw.read(CHUNK): pass
        if raw.hash.hexdigest() != EXPECTED: raise InspectionError("whole archive digest mismatch")
    def resolve(name):
        chain, seen = [], set()
        while True:
            if name in seen or len(chain) >= 32: raise InspectionError("archive link cycle/bound")
            seen.add(name); chain.append(name)
            entry = inventory.get(name)
            if entry is None: raise InspectionError("archive link target missing")
            if entry["kind"] == "file":
                if entry["bytes"] > MAX_TOOL_BYTES: raise InspectionError("selected tool byte bound")
                return {"requested":chain[0], "resolved":name, "chain":chain,
                        "bytes":entry["bytes"], "sha256":entry["sha256"],
                        "elf":entry.get("elf")}
            if entry["kind"] not in ("hardlink","symlink"): raise InspectionError("tool target not file")
            name = entry["target"]
    tools = {key:resolve(ROOT + "/" + value) for key,value in {
        "clang":"bin/clang", "wasm_ld":"bin/wasm-ld",
        "wasip1_libc":"share/wasi-sysroot/lib/wasm32-wasip1/libc.a"}.items()}
    # Closed whole-SDK and sysroot manifests include file bytes and link targets.
    print(json.dumps({"status":"stream hashes verified; SDK not saved/extracted/executed; Linux module not compiled",
        "url":URL,"archive_sha256":EXPECTED,"compressed_bytes":raw.bytes,"expanded_bytes":expanded.bytes,
        "member_count":members,"sdk_root":ROOT,"tools":tools,
        "inventory_encoding":"archive-order length-prefixed canonical JSON [path,entry]",
        "whole_inventory_sha256":whole_hash.hexdigest(),
        "sysroot_inventory_sha256":sysroot_hash.hexdigest(),
        "sysroot_member_count":sysroot_members,"bounds":{"compressed":MAX_COMPRESSED,"expanded":MAX_EXPANDED,
        "members":MAX_MEMBERS,"selected_metadata_records":MAX_SELECTED,"metadata":MAX_METADATA,"member_bytes":MAX_MEMBER_BYTES,"tool_bytes":MAX_TOOL_BYTES,
        "chunk":CHUNK},"inventory":inventory},indent=2))

if __name__ == "__main__":
    try: main()
    except Exception as error:
        # Never print redirected signed CDN URLs or transient response tokens.
        message = ": " + str(error) if isinstance(error,InspectionError) else ""
        print("SDK stream inspection failed: " + type(error).__name__ + message,file=sys.stderr)
        sys.exit(1)
