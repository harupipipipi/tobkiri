"""
domain.tool.container_manager — Docker コンテナ管理。
Docker SDK が利用可能ならそれを使い、なければ subprocess フォールバック。
Docker 自体が利用不可なら fail-closed で停止する。
"""
import subprocess
import shutil
import time
import uuid
from typing import Any

# Docker SDK の有無を検出
_docker_available = False
_docker_client: Any = None
try:
    import docker
    _docker_client = docker.from_env()
    _docker_client.ping()
    _docker_available = True
except Exception:
    _docker_available = False

# Docker CLI の有無を検出
_docker_cli_available = shutil.which("docker") is not None


def _has_docker():
    """Docker が利用可能かどうかを返す"""
    return _docker_available or _docker_cli_available


def _run_cmd(args, timeout=30):
    """subprocess でコマンドを実行し stdout/stderr を返す"""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "command timed out"}
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "command not found: {}".format(args[0])}


# ---------------------------------------------------------------------------
# インメモリ コンテナ管理
# ---------------------------------------------------------------------------
_containers: dict[str, "ContainerInfo"] = {}


class DockerUnavailableError(RuntimeError):
    """Raised when Docker-backed container operations cannot be performed."""


def _docker_unavailable_message():
    return (
        "Docker is not available; refusing to create or use a local host "
        "execution fallback. Start Docker or route the work through an "
        "explicit approval-aware host execution policy."
    )


def _raise_if_local_fallback(info):
    if info.status in ("local-only", "running-local", "stopped-local"):
        raise DockerUnavailableError(
            "Refusing to use legacy local host execution fallback for container "
            "{}. {}".format(info.container_id, _docker_unavailable_message())
        )


class ContainerInfo:
    """管理用コンテナ情報"""

    def __init__(self, container_id, name, image, status, config):
        self.container_id = container_id
        self.name = name
        self.image = image
        self.status = status
        self.config = config
        self._docker_id: str | None = None
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_dict(self):
        return {
            "container_id": self.container_id,
            "name": self.name,
            "image": self.image,
            "status": self.status,
            "config": self.config,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Docker SDK パス
# ---------------------------------------------------------------------------

def _sdk_create(name, image, config):
    """Docker SDK でコンテナを作成する"""
    environment = config.get("environment", {})
    ports = config.get("ports", {})
    volumes = config.get("volumes", {})
    command = config.get("command")

    kwargs = {
        "image": image,
        "name": name,
        "detach": True,
        "environment": environment,
        "ports": ports,
        "volumes": volumes,
        "stdin_open": True,
        "tty": True,
    }
    if command:
        kwargs["command"] = command

    container = _docker_client.containers.create(**kwargs)
    return container.id


def _sdk_start(docker_id):
    container = _docker_client.containers.get(docker_id)
    container.start()


def _sdk_stop(docker_id):
    container = _docker_client.containers.get(docker_id)
    container.stop(timeout=10)


def _sdk_remove(docker_id):
    container = _docker_client.containers.get(docker_id)
    container.remove(force=True)


def _sdk_exec(docker_id, command):
    container = _docker_client.containers.get(docker_id)
    exit_code, output = container.exec_run(command, demux=False)
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
    return {"exit_code": exit_code, "output": text}


# ---------------------------------------------------------------------------
# CLI フォールバック パス
# ---------------------------------------------------------------------------

def _cli_create(name, image, config):
    """docker CLI でコンテナを作成する"""
    args = ["docker", "create", "--name", name]
    environment = config.get("environment", {})
    for k, v in environment.items():
        args.extend(["-e", "{}={}".format(k, v)])
    ports = config.get("ports", {})
    for host_port, container_port in ports.items():
        args.extend(["-p", "{}:{}".format(host_port, container_port)])
    args.extend(["-it", image])
    command = config.get("command")
    if command:
        if isinstance(command, list):
            args.extend(command)
        else:
            args.append(command)
    result = _run_cmd(args, timeout=60)
    if result["returncode"] != 0:
        raise RuntimeError("docker create failed: {}".format(result["stderr"]))
    return result["stdout"].strip()


def _cli_start(docker_id):
    result = _run_cmd(["docker", "start", docker_id])
    if result["returncode"] != 0:
        raise RuntimeError("docker start failed: {}".format(result["stderr"]))


def _cli_stop(docker_id):
    result = _run_cmd(["docker", "stop", docker_id], timeout=30)
    if result["returncode"] != 0:
        raise RuntimeError("docker stop failed: {}".format(result["stderr"]))


def _cli_remove(docker_id):
    result = _run_cmd(["docker", "rm", "-f", docker_id])
    if result["returncode"] != 0:
        raise RuntimeError("docker rm failed: {}".format(result["stderr"]))


def _cli_exec(docker_id, command):
    if isinstance(command, str):
        args = ["docker", "exec", docker_id, "sh", "-c", command]
    else:
        args = ["docker", "exec", docker_id] + list(command)
    result = _run_cmd(args, timeout=120)
    return {"exit_code": result["returncode"], "output": result["stdout"] + result["stderr"]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_container(name, image, config):
    """
    コンテナを作成する。
    Docker が利用可能ならば Docker で作成、不可なら fail-closed。
    戻り値: ContainerInfo の dict
    """
    container_id = str(uuid.uuid4())

    if not name:
        name = "rumi-container-{}".format(container_id[:8])

    if not image:
        image = "ubuntu:22.04"

    if not config:
        config = {}

    docker_id = None
    status = "created"

    if _docker_available:
        try:
            docker_id = _sdk_create(name, image, config)
        except Exception as exc:
            raise RuntimeError("Docker SDK create failed: {}".format(exc))
    elif _docker_cli_available:
        try:
            docker_id = _cli_create(name, image, config)
        except Exception as exc:
            raise RuntimeError("Docker CLI create failed: {}".format(exc))
    else:
        raise DockerUnavailableError(_docker_unavailable_message())

    info = ContainerInfo(
        container_id=container_id,
        name=name,
        image=image,
        status=status,
        config=config,
    )
    info._docker_id = docker_id
    _containers[container_id] = info
    return info.to_dict()


def start_container(container_id):
    """コンテナを起動する"""
    info = _containers.get(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))
    _raise_if_local_fallback(info)
    docker_id = getattr(info, "_docker_id", container_id)
    if _docker_available:
        _sdk_start(docker_id)
    elif _docker_cli_available:
        _cli_start(docker_id)
    else:
        raise RuntimeError("Docker is not available")
    info.status = "running"
    return info.to_dict()


def stop_container(container_id):
    """コンテナを停止する"""
    info = _containers.get(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))
    _raise_if_local_fallback(info)
    docker_id = getattr(info, "_docker_id", container_id)
    if _docker_available:
        _sdk_stop(docker_id)
    elif _docker_cli_available:
        _cli_stop(docker_id)
    else:
        raise RuntimeError("Docker is not available")
    info.status = "stopped"
    return info.to_dict()


def delete_container(container_id):
    """コンテナを削除する"""
    info = _containers.get(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))
    if info.status not in ("local-only", "running-local", "stopped-local"):
        docker_id = getattr(info, "_docker_id", container_id)
        if _docker_available:
            _sdk_remove(docker_id)
        elif _docker_cli_available:
            _cli_remove(docker_id)
    del _containers[container_id]
    return {"container_id": container_id, "deleted": True}


def exec_in_container(container_id, command):
    """コンテナ内でコマンドを実行する"""
    info = _containers.get(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))
    _raise_if_local_fallback(info)

    docker_id = getattr(info, "_docker_id", container_id)
    if _docker_available:
        return _sdk_exec(docker_id, command)
    elif _docker_cli_available:
        return _cli_exec(docker_id, command)
    else:
        raise RuntimeError("Docker is not available and container is not in local mode")


def get_container(container_id):
    """コンテナ情報を取得する"""
    info = _containers.get(container_id)
    if info is None:
        return None
    return info.to_dict()


def list_containers():
    """全コンテナ情報を返す"""
    return [info.to_dict() for info in _containers.values()]


def is_docker_available():
    """Docker が利用可能かどうかを返す"""
    return _has_docker()
