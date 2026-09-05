from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence
import urllib.error
import urllib.request

from .cloudflare_sdk import cloudflare_sdk_status


CommandRunner = Callable[[Sequence[str], float], "CommandResult"]
CloudflareAPIFetcher = Callable[[str, str, float], dict[str, Any]]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def cloudflare_environment_status(
    *,
    active: bool = False,
    command_runner: CommandRunner | None = None,
    api_fetcher: CloudflareAPIFetcher | None = None,
    api_token: str | None = None,
    env: Mapping[str, str] | None = None,
    connector_root: Path | None = None,
) -> dict[str, Any]:
    """Return a redacted Cloudflare readiness report for a Pack continuation.

    The lightweight default avoids network/process-heavy checks during normal
    settings rendering. Passing active=True probes Wrangler, cloudflared,
    Cloudflare Containers, Pages, and Docker so the user can see real blockers.
    ``connector_root`` is the caller-captured Pack root used only to discover
    that Pack's pinned Wrangler executable; no Pack is selected implicitly.
    """

    environ = env or os.environ
    runner = command_runner or _run_command
    fetcher = api_fetcher or _cloudflare_api_get_json
    wrangler_cmd = _wrangler_command(environ, connector_root=connector_root)
    cloudflared_cmd = _tool_command("cloudflared")
    docker_cmd = _tool_command("docker")

    checks: dict[str, dict[str, Any]] = {
        "python_sdk": cloudflare_sdk_status(),
        "wrangler": _command_presence("wrangler", wrangler_cmd),
        "cloudflared": _command_presence("cloudflared", cloudflared_cmd),
        "docker": _command_presence("docker", docker_cmd),
        "pages": _not_checked(),
        "containers": _not_checked(),
        "zones": _not_checked(),
        "named_tunnel": _not_checked(),
        "pc_tunnel_env": _check_pc_tunnel_env(environ),
        "pc_tool_bridge_env": _check_pc_tool_bridge_env(environ),
    }

    if active:
        checks["wrangler"] = _check_wrangler(wrangler_cmd, runner)
        checks["pages"] = _check_pages(wrangler_cmd, runner)
        checks["containers"] = _check_containers(wrangler_cmd, runner)
        checks["zones"] = _check_zones(_cloudflare_api_token(environ, api_token), fetcher)
        checks["cloudflared"] = _check_cloudflared_version(cloudflared_cmd, runner)
        checks["named_tunnel"] = _check_named_tunnel(cloudflared_cmd, wrangler_cmd, runner)
        checks["docker"] = _check_docker(docker_cmd, runner)

    sandbox_ready = checks["containers"].get("status") == "ready" and checks["docker"].get("status") == "ready"
    pages_ready = checks["pages"].get("status") == "ready"
    zones_ready = checks["zones"].get("status") == "ready"
    named_tunnel_ready = checks["named_tunnel"].get("status") == "ready"
    pc_tunnel_env_ready = checks["pc_tunnel_env"].get("status") == "configured"
    stable_pc_tunnel_ready = named_tunnel_ready and pc_tunnel_env_ready and zones_ready
    pc_tool_bridge_env_ready = checks["pc_tool_bridge_env"].get("status") == "configured"
    pc_tool_bridge_ready = stable_pc_tunnel_ready and pc_tool_bridge_env_ready
    blockers = _blockers(checks, active=active)

    return {
        "schema": "rumi.cloudflare.environment.v1",
        "active": active,
        "status": "ready" if active and sandbox_ready and pages_ready and pc_tool_bridge_ready else ("needs_check" if not active else "blocked"),
        "runner_deploy_ready": bool(active and sandbox_ready),
        "sandbox_ready": bool(active and sandbox_ready),
        "pages_ready": bool(active and pages_ready),
        "zones_ready": bool(active and zones_ready),
        "named_tunnel_ready": bool(active and named_tunnel_ready),
        "stable_pc_tunnel_ready": bool(active and stable_pc_tunnel_ready),
        "pc_tool_bridge_ready": bool(active and pc_tool_bridge_ready),
        "free_plan_supported": False if checks["containers"].get("status") == "paid_plan_required" else None,
        "checks": checks,
        "blockers": blockers,
        "deployment": {
            "sandbox_bridge_scaffold": "connector://cloudflare/sandbox_bridge",
            "sandbox_bridge_url_env": "RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL",
            "sandbox_bridge_api_key_env": "RUMI_CLOUDFLARE_SANDBOX_API_KEY",
            "pc_tunnel_scaffold": "connector://cloudflare/pc_tunnel",
            "pc_tunnel_hostname_env": "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME",
            "pc_tunnel_origin_url_env": "RUMI_CLOUDFLARE_PC_TUNNEL_ORIGIN_URL",
            "pc_tunnel_config_env": "RUMI_CLOUDFLARE_PC_TUNNEL_CONFIG",
            "pc_tunnel_zone_id_env": "RUMI_CLOUDFLARE_ZONE_ID",
            "stable_pc_tunnel": "named_cloudflare_tunnel_with_dns_hostname",
            "pc_tool_bridge_scaffold": "connector://cloudflare/pc_tool_bridge",
            "pc_tool_bridge_url_env": "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL",
            "pc_tool_bridge_token_env": "RUMI_PC_TOOL_BRIDGE_TOKEN",
            "pc_tool_bridge_pc_origin_env": "RUMI_PC_ORIGIN",
            "pc_tool_bridge_pc_bearer_env": "RUMI_PC_RUNTIME_BEARER",
            "pc_tool_bridge_allowed_origin_env": "RUMI_PC_TOOL_BRIDGE_ALLOWED_ORIGIN",
        },
        "constraints": {
            "cloudflare_sandbox_requires_workers_paid": True,
            "sandbox_deploy_requires_docker_running": True,
            "pages_dev_is_not_a_pc_tunnel_hostname": True,
            "pages_dev_urls_are_pages_deployments_not_tunnel_hostnames": True,
            "stable_pc_tunnel_requires_named_tunnel_and_dns_hostname": True,
            "stable_pc_tunnel_requires_cloudflare_managed_zone": True,
            "pages_projects_do_not_create_cloudflare_dns_zones": True,
            "trycloudflare_urls_are_not_stable_pc_tunnel_hostnames": True,
            "quick_tunnels_do_not_support_sse": True,
            "sandbox_preview_urls_require_custom_domain_for_production": True,
            "all_tools_cloudflare_native_supported": False,
            "pc_local_tools_require_pc_bridge": True,
            "pc_tool_bridge_requires_named_tunnel": True,
            "pc_tool_bridge_does_not_upload_pc_local_tools": True,
            "pc_tool_bridge_preserves_pc_approval_authority": True,
            "wrangler_diagnostics_require_explicit_command_or_local_install": True,
        },
    }


def _wrangler_command(
    env: Mapping[str, str],
    *,
    connector_root: Path | None = None,
) -> list[str]:
    """Return an explicit or caller-captured local Wrangler command."""

    explicit = str(env.get("RUMI_WRANGLER_COMMAND") or "").strip()
    if explicit:
        return shlex.split(explicit)
    wrangler = shutil.which("wrangler")
    if wrangler:
        return [wrangler]
    local_wrangler = _local_wrangler_command(connector_root)
    if local_wrangler:
        return local_wrangler
    return []


def _tool_command(name: str) -> list[str]:
    path = shutil.which(name)
    return [path] if path else []


def _local_wrangler_command(connector_root: Path | None) -> list[str]:
    """Locate Wrangler only inside the caller's explicitly captured Pack."""

    if connector_root is None:
        return []

    candidates = [
        connector_root
        / "cloudflare"
        / "pc_tool_bridge"
        / "node_modules"
        / ".bin"
        / "wrangler",
        connector_root
        / "cloudflare"
        / "sandbox_bridge"
        / "node_modules"
        / ".bin"
        / "wrangler",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    return []


def _command_presence(name: str, command: Sequence[str]) -> dict[str, Any]:
    if name == "wrangler" and not command:
        detail = (
            "Wrangler command was not found. Set RUMI_WRANGLER_COMMAND or run npm install in a Cloudflare scaffold "
            "so its pinned node_modules/.bin/wrangler is available; diagnostics will not auto-download Wrangler."
        )
    else:
        detail = f"{name} command is available." if command else f"{name} command was not found on PATH."
    return {
        "available": bool(command),
        "status": "available" if command else "missing",
        "command": _public_command(command),
        "detail": detail,
    }


def _not_checked() -> dict[str, Any]:
    return {
        "available": None,
        "status": "not_checked",
        "detail": "Run active Cloudflare diagnostics to verify this requirement.",
    }


def _check_wrangler(command: Sequence[str], runner: CommandRunner) -> dict[str, Any]:
    if not command:
        return _command_presence("wrangler", command)
    version = runner([*command, "--version"], 10)
    whoami = runner([*command, "whoami"], 45)
    stdout = f"{version.stdout}\n{whoami.stdout}"
    stderr = f"{version.stderr}\n{whoami.stderr}"
    authenticated = whoami.returncode == 0 and "logged in" in stdout.lower()
    return {
        "available": version.returncode == 0,
        "status": "ready" if authenticated else "auth_required",
        "command": _public_command(command),
        "authenticated": authenticated,
        "detail": "Wrangler is logged in." if authenticated else _summarize_output(stdout, stderr, "Wrangler login is required."),
        "version": _first_nonempty_line(version.stdout),
    }


def _check_pages(command: Sequence[str], runner: CommandRunner) -> dict[str, Any]:
    if not command:
        return _command_presence("wrangler", command)
    result = runner([*command, "pages", "project", "list"], 30)
    return {
        "available": result.returncode == 0,
        "status": "ready" if result.returncode == 0 else "unavailable",
        "detail": "Cloudflare Pages projects are listable." if result.returncode == 0 else _summarize_output(result.stdout, result.stderr, "Pages project list failed."),
    }


def _check_containers(command: Sequence[str], runner: CommandRunner) -> dict[str, Any]:
    if not command:
        return _command_presence("wrangler", command)
    result = runner([*command, "containers", "list"], 30)
    output = f"{result.stdout}\n{result.stderr}".lower()
    if "workers paid plan" in output or "do not have access to cloudflare containers" in output:
        return {
            "available": False,
            "status": "paid_plan_required",
            "detail": "Cloudflare Containers/Sandbox access requires the Workers Paid plan for this account.",
        }
    return {
        "available": result.returncode == 0,
        "status": "ready" if result.returncode == 0 else "unavailable",
        "detail": "Cloudflare Containers are accessible." if result.returncode == 0 else _summarize_output(result.stdout, result.stderr, "Cloudflare Containers check failed."),
    }


def _cloudflare_api_token(env: Mapping[str, str], explicit: str | None) -> str:
    for value in (
        explicit,
        env.get("CLOUDFLARE_API_TOKEN"),
        env.get("CF_API_TOKEN"),
        env.get("RUMI_CLOUDFLARE_API_TOKEN"),
    ):
        token = str(value or "").strip()
        if token:
            return token
    return ""


def _check_zones(api_token: str, fetcher: CloudflareAPIFetcher) -> dict[str, Any]:
    if not api_token:
        return {
            "available": None,
            "status": "not_checked",
            "zone_count": None,
            "detail": "Import a Cloudflare token or set CLOUDFLARE_API_TOKEN to verify that the account has a Cloudflare-managed DNS zone for the permanent PC Tunnel hostname.",
        }
    try:
        payload = fetcher("/zones?per_page=1", api_token, 20)
    except Exception as exc:
        return {
            "available": False,
            "status": "unavailable",
            "zone_count": None,
            "detail": _scrub_cloudflare_error(str(exc), api_token) or "Cloudflare zones check failed.",
        }
    if not bool(payload.get("success", False)):
        return {
            "available": False,
            "status": "unavailable",
            "zone_count": None,
            "detail": _cloudflare_errors_summary(payload) or "Cloudflare zones check failed.",
        }
    result = payload.get("result")
    result_info = payload.get("result_info")
    zone_count = 0
    if isinstance(result_info, Mapping):
        try:
            zone_count = int(result_info.get("total_count") or result_info.get("count") or 0)
        except (TypeError, ValueError):
            zone_count = 0
    if zone_count <= 0 and isinstance(result, list):
        zone_count = len(result)
    if zone_count <= 0:
        return {
            "available": False,
            "status": "missing_cloudflare_zone",
            "zone_count": 0,
            "detail": "No Cloudflare-managed DNS zones are available on this account. Add or transfer a domain to Cloudflare before creating a permanent named Tunnel hostname.",
        }
    return {
        "available": True,
        "status": "ready",
        "zone_count": zone_count,
        "detail": "Cloudflare-managed DNS zones are available for a named Tunnel public hostname.",
    }


def _check_cloudflared_version(command: Sequence[str], runner: CommandRunner) -> dict[str, Any]:
    if not command:
        return _command_presence("cloudflared", command)
    result = runner([*command, "--version"], 10)
    return {
        "available": result.returncode == 0,
        "status": "available" if result.returncode == 0 else "unavailable",
        "command": _public_command(command),
        "version": _first_nonempty_line(result.stdout),
        "detail": "cloudflared is installed." if result.returncode == 0 else _summarize_output(result.stdout, result.stderr, "cloudflared version check failed."),
    }


def _check_named_tunnel(
    cloudflared_command: Sequence[str],
    wrangler_command: Sequence[str],
    runner: CommandRunner,
) -> dict[str, Any]:
    if wrangler_command:
        wrangler = runner([*wrangler_command, "tunnel", "list"], 30)
        if wrangler.returncode == 0:
            tunnel_counts = _tunnel_status_counts(wrangler.stdout)
            tunnel_count = tunnel_counts["tunnel_count"]
            if tunnel_count > 0:
                if tunnel_counts["active_count"] <= 0:
                    return {
                        "available": True,
                        "status": "not_running",
                        "manager": "wrangler",
                        **tunnel_counts,
                        "detail": "Named Cloudflare Tunnel resources exist, but none are active. Run the PC tunnel before treating the stable hostname as reachable.",
                    }
                return {
                    "available": True,
                    "status": "ready",
                    "manager": "wrangler",
                    **tunnel_counts,
                    "detail": "At least one named Cloudflare Tunnel is active through Wrangler OAuth.",
                }
            return {
                "available": True,
                "status": "not_created",
                "manager": "wrangler",
                "tunnel_count": 0,
                "detail": "Wrangler can manage Cloudflare Tunnels, but no named tunnel exists yet.",
            }

    if not cloudflared_command:
        return _command_presence("cloudflared", cloudflared_command)
    result = runner([*cloudflared_command, "tunnel", "list"], 15)
    output = f"{result.stdout}\n{result.stderr}".lower()
    if "origincert" in output or "origin certificate" in output or "cert.pem" in output:
        return {
            "available": False,
            "status": "origin_cert_missing",
            "detail": "cloudflared is installed, but a tunnel origin certificate is missing. Run cloudflared tunnel login before creating named tunnel DNS routes.",
        }
    if result.returncode != 0:
        return {
            "available": False,
            "status": "unavailable",
            "manager": "cloudflared",
            "detail": _summarize_output(result.stdout, result.stderr, "Named tunnel check failed."),
        }
    tunnel_counts = _tunnel_status_counts(result.stdout)
    if tunnel_counts["tunnel_count"] > 0 and tunnel_counts["active_count"] <= 0:
        return {
            "available": True,
            "status": "not_running",
            "manager": "cloudflared",
            **tunnel_counts,
            "detail": "Named tunnel credentials are available, but no tunnel is currently active.",
        }
    return {
        "available": result.returncode == 0,
        "status": "ready" if result.returncode == 0 else "unavailable",
        "manager": "cloudflared",
        **tunnel_counts,
        "detail": "Named tunnel credentials are available and at least one tunnel is active."
        if tunnel_counts["active_count"] > 0
        else "Named tunnel credentials are available.",
    }


def _tunnel_status_counts(output: str) -> dict[str, int]:
    tunnel_ids: set[str] = set()
    active_count = 0
    inactive_count = 0
    unknown_count = 0
    uuid_re = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
    for line in str(output or "").splitlines():
        match = uuid_re.search(line)
        if match is None:
            continue
        tunnel_ids.add(match.group(0).lower())
        status = _tunnel_status_from_line(line)
        if status == "active":
            active_count += 1
        elif status == "inactive":
            inactive_count += 1
        else:
            unknown_count += 1
    return {
        "tunnel_count": len(tunnel_ids),
        "active_count": active_count,
        "inactive_count": inactive_count,
        "unknown_status_count": unknown_count,
    }


def _tunnel_status_from_line(line: str) -> str:
    parts = [part.strip().lower() for part in line.split("│")]
    if len(parts) >= 4:
        candidate = parts[3]
        if candidate in {"active", "inactive", "degraded", "healthy", "down"}:
            return candidate
    uuid_re = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    match = re.search(rf"\b{uuid_re}\b\s+\S+\s+(\S+)", line, re.IGNORECASE)
    if match:
        return match.group(1).strip().lower()
    return ""


def _check_pc_tunnel_env(env: Mapping[str, str]) -> dict[str, Any]:
    hostname = str(env.get("RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME") or "").strip()
    origin_url = str(env.get("RUMI_CLOUDFLARE_PC_TUNNEL_ORIGIN_URL") or "http://127.0.0.1:8765").strip()
    config_path = str(env.get("RUMI_CLOUDFLARE_PC_TUNNEL_CONFIG") or "").strip()
    parsed_hostname = _hostname_from_public_hostname(hostname)
    if hostname and parsed_hostname is None:
        return {
            "available": False,
            "status": "invalid_hostname",
            "detail": "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME must be a hostname only, not a URL or path.",
            "hostname": _redact_value(hostname),
            "origin_url": origin_url,
            "config_path": config_path,
        }
    if parsed_hostname and parsed_hostname.endswith(".pages.dev"):
        return {
            "available": False,
            "status": "pages_dev_not_supported",
            "detail": "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME must be a Cloudflare Tunnel public hostname on your domain, not a pages.dev Pages deployment URL.",
            "hostname": parsed_hostname,
            "origin_url": origin_url,
            "config_path": config_path,
        }
    if parsed_hostname and parsed_hostname.endswith(".trycloudflare.com"):
        return {
            "available": False,
            "status": "trycloudflare_not_stable",
            "detail": "trycloudflare.com hostnames are random quick tunnels. Use a named Cloudflare Tunnel hostname on your domain for stable phone-to-PC access.",
            "hostname": parsed_hostname,
            "origin_url": origin_url,
            "config_path": config_path,
        }
    if parsed_hostname and _is_private_or_loopback_hostname(parsed_hostname):
        return {
            "available": False,
            "status": "not_public_hostname",
            "detail": "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME must be a public Cloudflare Tunnel hostname, not localhost or a private LAN address.",
            "hostname": parsed_hostname,
            "origin_url": origin_url,
            "config_path": config_path,
        }
    if not hostname:
        return {
            "available": False,
            "status": "not_configured",
            "detail": "Set RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME to a named Cloudflare Tunnel hostname for stable phone-to-PC access.",
            "origin_url": origin_url,
            "config_path": config_path,
        }
    if not origin_url.startswith(("http://", "https://")):
        return {
            "available": False,
            "status": "invalid_origin_url",
            "detail": "RUMI_CLOUDFLARE_PC_TUNNEL_ORIGIN_URL must be an http(s) local service URL.",
            "hostname": hostname,
            "origin_url": origin_url,
            "config_path": config_path,
        }
    return {
        "available": True,
        "status": "configured",
        "detail": "Stable PC tunnel environment is configured. cloudflared must still be running and routed to this hostname.",
        "hostname": parsed_hostname or hostname,
        "origin_url": origin_url,
        "config_path": config_path,
    }


def _check_pc_tool_bridge_env(env: Mapping[str, str]) -> dict[str, Any]:
    bridge_url = str(env.get("RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL") or "").strip()
    bridge_token_configured = bool(str(env.get("RUMI_PC_TOOL_BRIDGE_TOKEN") or "").strip())
    pc_runtime_bearer_configured = bool(str(env.get("RUMI_PC_RUNTIME_BEARER") or "").strip())
    allowed_origin = str(env.get("RUMI_PC_TOOL_BRIDGE_ALLOWED_ORIGIN") or "").strip()
    pc_origin = str(env.get("RUMI_PC_ORIGIN") or "").strip()
    tunnel_hostname = str(env.get("RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME") or "").strip()

    bridge = _parse_https_origin_url(bridge_url)
    if not bridge_url:
        return _pc_tool_bridge_env_result(
            "not_configured",
            "Set RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL to the deployed PC Tool Bridge Worker URL.",
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )
    if bridge is None:
        return _pc_tool_bridge_env_result(
            "invalid_bridge_url",
            "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL must be an HTTPS Worker URL without path, query, or fragment.",
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )
    if bridge.hostname.endswith(".pages.dev"):
        return _pc_tool_bridge_env_result(
            "pages_dev_not_supported",
            "The PC Tool Bridge scaffold is a Worker. Do not use a pages.dev Pages deployment URL for this bridge.",
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )
    if _is_private_or_loopback_hostname(bridge.hostname):
        return _pc_tool_bridge_env_result(
            "bridge_url_not_public",
            "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL must be a public Worker/custom-domain HTTPS URL.",
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )
    if not bridge_token_configured:
        return _pc_tool_bridge_env_result(
            "bridge_token_missing",
            "Set RUMI_PC_TOOL_BRIDGE_TOKEN as a Worker secret and matching client secret.",
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )
    if not pc_runtime_bearer_configured:
        return _pc_tool_bridge_env_result(
            "pc_runtime_bearer_missing",
            "Set RUMI_PC_RUNTIME_BEARER as a Worker secret for upstream PC API authentication.",
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )

    origin_check = _pc_tool_bridge_pc_origin_check(pc_origin, tunnel_hostname)
    if origin_check["status"] != "configured":
        return _pc_tool_bridge_env_result(
            str(origin_check["status"]),
            str(origin_check["detail"]),
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )
    if allowed_origin and _parse_https_origin_url(allowed_origin) is None:
        return _pc_tool_bridge_env_result(
            "invalid_allowed_origin",
            "RUMI_PC_TOOL_BRIDGE_ALLOWED_ORIGIN must be an HTTPS origin without path, query, or fragment.",
            bridge_url=bridge_url,
            bridge_token_configured=bridge_token_configured,
            pc_runtime_bearer_configured=pc_runtime_bearer_configured,
            allowed_origin=allowed_origin,
            pc_origin=pc_origin,
            tunnel_hostname=tunnel_hostname,
        )
    return _pc_tool_bridge_env_result(
        "configured",
        "PC Tool Bridge environment is configured. Deploy the Worker and run the named PC tunnel before using it.",
        bridge_url=bridge_url,
        bridge_token_configured=bridge_token_configured,
        pc_runtime_bearer_configured=pc_runtime_bearer_configured,
        allowed_origin=allowed_origin,
        pc_origin=pc_origin,
        tunnel_hostname=tunnel_hostname,
    )


def _pc_tool_bridge_pc_origin_check(pc_origin: str, tunnel_hostname: str) -> dict[str, str]:
    raw = pc_origin or tunnel_hostname
    if not raw:
        return {
            "status": "pc_origin_not_configured",
            "detail": "Set RUMI_PC_ORIGIN or RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME to the named Tunnel HTTPS origin.",
        }
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = _parse_https_origin_url(candidate)
    if parsed is None:
        return {
            "status": "invalid_pc_origin",
            "detail": "RUMI_PC_ORIGIN must be an HTTPS origin without path, query, or fragment.",
        }
    hostname = parsed.hostname
    if hostname.endswith(".pages.dev"):
        return {
            "status": "pages_dev_not_supported",
            "detail": "RUMI_PC_ORIGIN must point to a named Cloudflare Tunnel hostname, not a pages.dev Pages deployment URL.",
        }
    if hostname.endswith(".trycloudflare.com"):
        return {
            "status": "trycloudflare_not_stable",
            "detail": "RUMI_PC_ORIGIN must be stable. Random trycloudflare.com quick tunnel URLs are not supported for production.",
        }
    if _is_private_or_loopback_hostname(hostname):
        return {
            "status": "pc_origin_not_public",
            "detail": "RUMI_PC_ORIGIN must be a public named Tunnel hostname, not localhost or a private LAN address.",
        }
    return {"status": "configured", "detail": "PC origin is a stable public HTTPS origin."}


def _pc_tool_bridge_env_result(
    status: str,
    detail: str,
    *,
    bridge_url: str,
    bridge_token_configured: bool,
    pc_runtime_bearer_configured: bool,
    allowed_origin: str,
    pc_origin: str,
    tunnel_hostname: str,
) -> dict[str, Any]:
    return {
        "available": status == "configured",
        "status": status,
        "detail": detail,
        "bridge_url": _redact_url(bridge_url),
        "bridge_token_configured": bridge_token_configured,
        "pc_runtime_bearer_configured": pc_runtime_bearer_configured,
        "allowed_origin": _redact_url(allowed_origin),
        "pc_origin": _redact_url(pc_origin),
        "tunnel_hostname": _hostname_from_public_hostname(tunnel_hostname) or _redact_value(tunnel_hostname),
    }


def _check_docker(command: Sequence[str], runner: CommandRunner) -> dict[str, Any]:
    if not command:
        return _command_presence("docker", command)
    result = runner([*command, "info", "--format", "{{json .ServerVersion}}"], 10)
    output = f"{result.stdout}\n{result.stderr}".lower()
    if "cannot connect to the docker daemon" in output:
        return {
            "available": True,
            "status": "daemon_unavailable",
            "detail": "Docker CLI is installed, but the Docker daemon is not running.",
        }
    return {
        "available": result.returncode == 0,
        "status": "ready" if result.returncode == 0 else "unavailable",
        "detail": "Docker is running." if result.returncode == 0 else _summarize_output(result.stdout, result.stderr, "Docker check failed."),
        "version": _first_nonempty_line(result.stdout).strip('"'),
    }


def _blockers(checks: Mapping[str, Mapping[str, Any]], *, active: bool) -> list[dict[str, str]]:
    if not active:
        return [
            {
                "code": "CLOUDFLARE_ACTIVE_DIAGNOSTICS_NOT_RUN",
                "message": "Run active Cloudflare diagnostics before deploying a cloud sandbox runner.",
            }
        ]
    blockers: list[dict[str, str]] = []
    for key in ("wrangler", "pages", "containers", "zones", "cloudflared", "named_tunnel", "pc_tunnel_env", "pc_tool_bridge_env", "docker"):
        status = str(checks.get(key, {}).get("status") or "")
        if status in {"ready", "available", "configured"}:
            continue
        blockers.append(
            {
                "code": f"CLOUDFLARE_{key.upper()}_{status.upper() or 'BLOCKED'}",
                "message": str(checks.get(key, {}).get("detail") or f"{key} is not ready."),
            }
        )
    return blockers


def _run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            close_fds=True,
        )
    except FileNotFoundError:
        return CommandResult(127, "", "command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "command timed out")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _cloudflare_api_get_json(path: str, api_token: str, timeout: float) -> dict[str, Any]:
    if not path.startswith("/"):
        path = f"/{path}"
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"success": False, "errors": [{"message": exc.reason, "code": exc.code}]}
        if isinstance(payload, dict):
            return payload
        return {"success": False, "errors": [{"message": exc.reason, "code": exc.code}]}
    payload = json.loads(raw)
    if isinstance(payload, dict):
        return payload
    return {"success": False, "errors": [{"message": "Cloudflare API returned a non-object payload."}]}


def _cloudflare_errors_summary(payload: Mapping[str, Any]) -> str:
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return ""
    messages: list[str] = []
    for item in errors[:3]:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        message = str(item.get("message") or "").strip()
        if code and message:
            messages.append(f"{code}: {message}")
        elif message:
            messages.append(message)
    return "; ".join(messages)[:240]


def _scrub_cloudflare_error(message: str, api_token: str) -> str:
    text = str(message or "")
    token = str(api_token or "").strip()
    if token:
        text = text.replace(token, "[redacted]")
    return text[:240]


def _public_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command[:2])


def _first_nonempty_line(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return ""


def _hostname_from_public_hostname(value: str) -> str | None:
    clean = str(value or "").strip().lower().rstrip(".")
    if not clean:
        return ""
    if "://" in clean or "/" in clean or ":" in clean:
        return None
    if clean.startswith(".") or ".." in clean:
        return None
    return clean


def _parse_https_origin_url(value: str) -> Any | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return parsed


def _is_private_or_loopback_hostname(hostname: str) -> bool:
    clean = str(hostname or "").strip().lower()
    if clean in {"localhost", "localhost.localdomain"} or clean.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(clean)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _redact_value(value: str) -> str:
    text = str(value or "")
    if len(text) <= 160:
        return text
    return f"{text[:120]}...{text[-16:]}"


def _redact_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 200:
        return text
    return _redact_value(text)


def _summarize_output(stdout: str, stderr: str, fallback: str) -> str:
    text = _first_nonempty_line(stderr) or _first_nonempty_line(stdout)
    return text or fallback
