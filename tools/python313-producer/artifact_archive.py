"""Preserve pinned CI artifact bytes past the 90-day retention without publishing them.

The Issue 1520 consumers restore exact producer and rpds artifacts from older
Actions runs.  Those artifacts expire and their build is not byte-reproducible,
so this tool re-uploads the identical bytes from a repository-private Actions
run (never a Release, package, git object or external store).

``archive``  runs inside the archive workflow: it authenticates the source run
             against ``artifact-archive-pins.json``, verifies the file pins and
             writes a provenance document next to the unchanged bytes.
``fetch``    runs inside every consumer: it authenticates the archive run and
             its provenance against the same pins, then re-verifies the file
             pins before any consumer executes the bytes.
``status``   reports how long the pinned archive artifacts remain available.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from typing import NoReturn

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PINS_PATH = HERE / "artifact-archive-pins.json"
PINS_FORMAT = "tobkiri.artifact-archive-pins/1"
PROVENANCE_FORMAT = "tobkiri.artifact-archive/1"
PROVENANCE_NAME = "archive-provenance.json"
ZIP_LIMIT = 256 * 1024 * 1024
FILE_LIMIT = 128 * 1024 * 1024
SOURCE_FIELDS = (
    "run_id", "run_attempt", "head_sha", "workflow_path",
    "artifact_name", "artifact_id",
)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"artifact archive: {message}")


def gh_bytes(endpoint: str, *, raw: bool = False) -> bytes:
    command = ["gh", "api"] + (["-H", "Accept: application/vnd.github.raw"] if raw else [])
    result = subprocess.run(command + [endpoint], capture_output=True)
    if result.returncode:
        fail(f"gh api {endpoint} failed: {result.stderr.decode(errors='replace')[:400]}")
    return result.stdout


def gh_json(endpoint: str) -> dict:
    return json.loads(gh_bytes(endpoint))


def repository() -> str:
    value = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        fail("GITHUB_REPOSITORY is required")
    return value


def load_pins() -> dict:
    pins = json.loads(PINS_PATH.read_bytes())
    if pins.get("format") != PINS_FORMAT or not isinstance(pins.get("artifacts"), dict):
        fail("pin file format differs")
    return pins


def pin_for(pins: dict, kind: str) -> dict:
    if kind not in pins["artifacts"]:
        fail(f"unknown artifact kind: {kind}")
    return pins["artifacts"][kind]


def archive_names(kind: str) -> tuple[str, str]:
    return f"archive-{kind}", f"archive-{kind}-provenance"


def sha256_file(path: Path) -> dict:
    if not stat.S_ISREG(path.lstat().st_mode):
        fail(f"regular file required: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > FILE_LIMIT:
                fail(f"file size bound exceeded: {path}")
            digest.update(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


def inventory(root: Path) -> dict:
    """Closed inventory of regular files; links and special files are rejected."""
    result = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in list(directories) + files:
            path = Path(current) / name
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                fail(f"link or special file in artifact: {path}")
            result[path.relative_to(root).as_posix()] = sha256_file(path)
    return dict(sorted(result.items()))


def constant(path: Path, name: str) -> str:
    """Read an existing hex pin from its source of truth without importing it."""
    found = re.findall(rf'(?m)^{name}\s*=\s*"([0-9a-f]{{64}})"', path.read_text())
    if len(found) != 1:
        fail(f"pin {name} not found exactly once in {path.name}")
    return found[0]


def get_run(repo: str, run_id: int, attempt: int) -> dict:
    run = gh_json(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
    if (run.get("id") != run_id or run.get("run_attempt") != attempt
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or (run.get("repository") or {}).get("full_name") != repo
            or (run.get("head_repository") or {}).get("full_name") != repo):
        fail(f"run {run_id} attempt {attempt} identity or outcome differs")
    return run


def find_artifact(repo: str, run_id: int, name: str, artifact_id: int | None) -> dict:
    listing = gh_json(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
    if listing.get("total_count", 0) > 100:
        fail("artifact listing exceeds one page")
    matches = [a for a in listing["artifacts"] if a.get("name") == name]
    if len(matches) != 1:
        fail(f"exactly one artifact {name} required in run {run_id}")
    artifact = matches[0]
    if (artifact.get("expired") is not False
            or (artifact.get("workflow_run") or {}).get("id") != run_id
            or (artifact_id is not None and artifact.get("id") != artifact_id)):
        fail(f"artifact {name} of run {run_id} expired or differs from its pin")
    return artifact


def download(repo: str, artifact: dict, destination: Path) -> None:
    if artifact["size_in_bytes"] > ZIP_LIMIT:
        fail("artifact size bound exceeded")
    data = gh_bytes(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
    digest = artifact.get("digest")
    if digest and digest != "sha256:" + hashlib.sha256(data).hexdigest():
        fail("downloaded artifact zip differs from its recorded digest")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total = 0
        for info in archive.infolist():
            parts = info.filename.split("/")
            if (info.filename.startswith("/") or ".." in parts or "\\" in info.filename
                    or ((info.external_attr >> 16) & 0o170000) == stat.S_IFLNK):
                fail(f"unsafe artifact member: {info.filename!r}")
            target = destination.joinpath(*[p for p in parts if p])
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += info.file_size
            if info.file_size > FILE_LIMIT or total > ZIP_LIMIT:
                fail("artifact member size bound exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as sink:
                shutil.copyfileobj(source, sink)
            target.chmod(0o644)


def verify_kind(kind: str, root: Path) -> None:
    """Re-apply the pins the consumers already enforce, before any byte is trusted."""
    files = inventory(root)
    if kind == "rpds":
        expected = {"rpds-build.json", "work/probe.json", "work/stage/outer.wasm",
                    "work/stage/rpds.abi3.so"}
        if set(files) != expected:
            fail("rpds artifact file set differs")
        schema = HERE / "build_schema.py"
        extension = constant(schema, "EXTENSION_SHA256")
        if files["work/stage/rpds.abi3.so"]["sha256"] != extension:
            fail("rpds extension differs from the pinned EXTENSION_SHA256")
        if files["work/stage/outer.wasm"]["sha256"] != constant(schema, "COMPONENT_SHA256"):
            fail("rpds component differs from the pinned COMPONENT_SHA256")
        prior = json.loads((root / "work/probe.json").read_bytes())
        if not (prior["passed"] is True and prior["actual_limited_api"] is True
                and prior["observation"]["post_return"] is True
                and prior["extension_sha256"] == extension):
            fail("rpds probe observation is incomplete")
    elif kind == "producer-cli":
        binary = "work/target/release/componentize-py"
        if set(files) != {"cli-build.json", "cli-source.json", binary}:
            fail("producer CLI artifact file set differs")
        cli = json.loads((root / "cli-build.json").read_bytes())
        source = cli.get("inputs", {}).get("source", {})
        if (cli.get("outcome") != "success"
                or cli.get("format") != "tobkiri.python313-cli-rebuild/1"
                or cli.get("original_recipe_sha256")
                != constant(HERE / "build_cli.py", "RECIPE_SHA256")
                or source.get("sha256") != constant(HERE / "build_native.py", "STAGED_SHA256")
                or cli.get("producer_cli") is not True
                or cli.get("inputs_unchanged") is not True
                or files[binary] != cli.get("binary")):
            fail("producer CLI differs from its build receipt or reviewed recipe pins")
        source_pins = [value for name, value in cli.get("predecessors", {}).items()
                       if Path(name).name == "cli-source.json"]
        if source_pins != [files["cli-source.json"]]:
            fail("producer CLI source receipt differs from its build receipt")
    elif kind == "original-components":
        sys.path.insert(0, str(HERE))
        import verify_original_components

        verify_original_components.verify(root)
    else:
        fail(f"unknown artifact kind: {kind}")


def trusted(repo: str, pins: dict, sha: str) -> None:
    """The archive run's commit must be reachable from a trusted ref of this repository."""
    for ref in pins["trusted_refs"]:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/compare/{sha}...{ref}", "--jq", ".status"],
            capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() in {"identical", "ahead"}:
            return
    fail(f"archive run commit {sha} is not reachable from a trusted ref")


def authenticate_archive(repo: str, pins: dict, pin: dict, kind: str) -> tuple[dict, dict]:
    if not isinstance(pin.get("archive_run"), int) or not isinstance(pin.get("archive_attempt"), int):
        fail(f"no archive run is pinned for {kind}")
    run = get_run(repo, pin["archive_run"], pin["archive_attempt"])
    if (run.get("event") not in pins["archive_events"]
            or run.get("path", "").split("@")[0] != pins["archive_workflow_path"]
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        fail("archive run is not the reviewed archive workflow")
    trusted(repo, pins, run["head_sha"])
    return run, {n: find_artifact(repo, run["id"], n, None) for n in archive_names(kind)}


def fetch(kind: str, destination: Path, *, provenance_out: Path | None = None) -> dict:
    repo, pins = repository(), load_pins()
    pin = pin_for(pins, kind)
    run, artifacts = authenticate_archive(repo, pins, pin, kind)
    data_name, provenance_name = archive_names(kind)
    if destination.exists():
        fail("new destination directory required")
    scratch = destination.parent / (destination.name + ".provenance")
    if scratch.exists():
        fail("new provenance scratch directory required")
    download(repo, artifacts[provenance_name], scratch)
    if set(inventory(scratch)) != {PROVENANCE_NAME}:
        fail("provenance artifact file set differs")
    provenance = json.loads((scratch / PROVENANCE_NAME).read_bytes())
    shutil.rmtree(scratch)
    source = provenance.get("source", {})
    archive = provenance.get("archive", {})
    if (provenance.get("format") != PROVENANCE_FORMAT or provenance.get("kind") != kind
            or provenance.get("repository") != repo
            or any(source.get(k) != pin["source"][k] for k in SOURCE_FIELDS)
            or archive.get("run_id") != run["id"]
            or archive.get("run_attempt") != run["run_attempt"]
            or archive.get("sha") != run["head_sha"]
            or archive.get("workflow_path") != pins["archive_workflow_path"]):
        fail("provenance does not name the pinned source run, head, artifact and archive run")
    download(repo, artifacts[data_name], destination)
    if inventory(destination) != provenance.get("files"):
        fail("restored bytes differ from the provenance manifest")
    verify_kind(kind, destination)
    if provenance_out is not None:
        provenance_out.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return provenance


def archive(kind: str, origin: str, destination: Path) -> None:
    repo, pins = repository(), load_pins()
    pin = pin_for(pins, kind)
    source = pin["source"]
    data = destination / "data"
    chain: list = []
    if origin == "original":
        run = get_run(repo, source["run_id"], source["run_attempt"])
        if (run.get("head_sha") != source["head_sha"]
                or run.get("path", "").split("@")[0] != source["workflow_path"]):
            fail("source run head or workflow differs from its pin")
        artifact = find_artifact(repo, source["run_id"], source["artifact_name"],
                                 source["artifact_id"])
        download(repo, artifact, data)
        verify_kind(kind, data)
        source_record = dict(source, status=run["status"], conclusion=run["conclusion"],
                             artifact_created_at=artifact.get("created_at"),
                             artifact_digest=artifact.get("digest"))
    else:
        previous = fetch(kind, data, provenance_out=None)
        chain = list(previous.get("chain", [])) + [previous["archive"]]
        source_record = previous["source"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    provenance = {
        "format": PROVENANCE_FORMAT,
        "kind": kind,
        "repository": repo,
        "origin": origin,
        "source": source_record,
        "archive": {
            "run_id": run_id,
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "sha": os.environ["GITHUB_SHA"],
            "ref": os.environ["GITHUB_REF"],
            "workflow_path": pins["archive_workflow_path"],
            "archived_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "chain": chain,
        "files": inventory(data),
    }
    (destination / "provenance").mkdir()
    (destination / "provenance" / PROVENANCE_NAME).write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"kind": kind, "origin": origin, "files": provenance["files"]}, indent=2))


def status(minimum_days: int) -> None:
    repo, pins = repository(), load_pins()
    now = datetime.datetime.now(datetime.timezone.utc)
    problems = []
    for kind, pin in pins["artifacts"].items():
        if not isinstance(pin.get("archive_run"), int):
            problems.append(f"{kind}: no archive run pinned")
            continue
        for name in archive_names(kind):
            artifact = find_artifact(repo, pin["archive_run"], name, None)
            expires = datetime.datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00"))
            days = (expires - now).days
            print(f"{kind} {name}: expires {artifact['expires_at']} ({days} days)")
            if days < minimum_days:
                problems.append(f"{kind} {name}: only {days} days left")
    if problems:
        fail("re-archive required: " + "; ".join(problems))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    kinds = sorted(load_pins()["artifacts"])
    one = sub.add_parser("archive")
    one.add_argument("--kind", choices=kinds, required=True)
    one.add_argument("--origin", choices=["original", "previous-archive"], default="original")
    one.add_argument("--destination", type=Path, required=True)
    two = sub.add_parser("fetch")
    two.add_argument("--kind", choices=kinds, required=True)
    two.add_argument("--destination", type=Path, required=True)
    two.add_argument("--provenance-out", type=Path)
    three = sub.add_parser("status")
    three.add_argument("--minimum-days", type=int, default=14)
    args = parser.parse_args()
    if args.command == "status":
        status(args.minimum_days)
        return
    destination = args.destination.resolve()
    if args.command == "archive":
        destination.mkdir(parents=True, exist_ok=False)
        archive(args.kind, args.origin, destination)
    else:
        provenance = fetch(args.kind, destination, provenance_out=args.provenance_out)
        print(f"restored {args.kind} from archive run {provenance['archive']['run_id']} "
              f"(source run {provenance['source']['run_id']} attempt "
              f"{provenance['source']['run_attempt']}, head {provenance['source']['head_sha']})")


if __name__ == "__main__":
    main()
