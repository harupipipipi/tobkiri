from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_cloudflare_sdk_adapter_reports_missing_sdk(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import cloudflare_sdk as sdk_client

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: None)

    status = sdk_client.cloudflare_sdk_status()
    adapter_status = sdk_client.CloudflareSDKAdapter(api_token="secret", account_id="acct").status()

    assert status["available"] is False
    assert status["status"] == "sdk_missing"
    assert adapter_status["status"] == "sdk_missing"
    assert adapter_status["token_configured"] is True
    assert adapter_status["account_configured"] is True


def test_cloudflare_oauth_status_includes_sdk_missing(monkeypatch):
    from core_runtime.cloudflare import sdk_client
    from domain.ai_client.oauth_store import provider_oauth_status

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: None)

    status = provider_oauth_status("cloudflare")

    assert status["cloudflare_sdk"]["status"] == "sdk_missing"
    assert status["provisioning"]["sdk_status"] == "sdk_missing"
    assert status["cloudflare_environment"]["schema"] == "rumi.cloudflare.environment.v1"
    assert status["cloudflare_environment"]["status"] == "needs_check"
    assert status["provisioning"]["runner_deploy_ready"] is False
    assert status["provisioning"]["constraints"]["cloudflare_sandbox_requires_workers_paid"] is True
    assert status["provisioning"]["constraints"]["all_tools_cloudflare_native_supported"] is False
    assert status["provisioning"]["constraints"]["pc_local_tools_require_pc_bridge"] is True
    assert status["provisioning"]["constraints"]["pc_tool_bridge_requires_named_tunnel"] is True
    assert status["provisioning"]["constraints"]["stable_pc_tunnel_requires_cloudflare_managed_zone"] is True
    assert status["provisioning"]["constraints"]["pages_projects_do_not_create_cloudflare_dns_zones"] is True
    assert status["provisioning"]["constraints"]["wrangler_diagnostics_require_explicit_command_or_local_install"] is True
    assert (
        status["provisioning"]["environment"]["deployment"]["sandbox_bridge_scaffold"]
        == "connector://cloudflare/sandbox_bridge"
    )
    assert (
        status["provisioning"]["environment"]["deployment"]["pc_tool_bridge_scaffold"]
        == "connector://cloudflare/pc_tool_bridge"
    )


def test_cloudflare_oauth_status_can_run_active_diagnostics(monkeypatch):
    from core_runtime.cloudflare import diagnostics
    from core_runtime.cloudflare import sdk_client
    from domain.ai_client.oauth_store import provider_oauth_status

    calls: list[bool] = []

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: None)

    def fake_environment_status(
        *,
        active=False,
        command_runner=None,
        api_fetcher=None,
        api_token=None,
        env=None,
        connector_root=None,
    ):
        del command_runner, api_fetcher, api_token, env, connector_root
        calls.append(bool(active))
        return {
            "schema": "rumi.cloudflare.environment.v1",
            "active": bool(active),
            "status": "blocked" if active else "needs_check",
            "runner_deploy_ready": False,
            "sandbox_ready": False,
            "pages_ready": False,
            "zones_ready": False,
            "named_tunnel_ready": False,
            "stable_pc_tunnel_ready": False,
            "pc_tool_bridge_ready": False,
            "blockers": [{"code": "CLOUDFLARE_ACTIVE_TEST", "message": "active diagnostics ran"}] if active else [],
            "constraints": {"cloudflare_sandbox_requires_workers_paid": True},
        }

    monkeypatch.setattr(diagnostics, "cloudflare_environment_status", fake_environment_status)

    status = provider_oauth_status("cloudflare", active_diagnostics=True)

    assert calls == [True]
    assert status["cloudflare_environment"]["active"] is True
    assert status["provisioning"]["blockers"] == [
        {"code": "CLOUDFLARE_ACTIVE_TEST", "message": "active diagnostics ran"}
    ]


def test_cloudflare_oauth_active_diagnostics_passes_imported_token(monkeypatch):
    from core_runtime.cloudflare import diagnostics
    from core_runtime.cloudflare import sdk_client
    from domain.ai_client import oauth_store

    captured: dict[str, object] = {}

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(oauth_store, "get_provider_access_token", lambda provider_id, *, pack_root=None: "cloudflare-secret-token")

    def fake_environment_status(
        *,
        active=False,
        command_runner=None,
        api_fetcher=None,
        api_token=None,
        env=None,
        connector_root=None,
    ):
        del command_runner, api_fetcher, env
        captured["active"] = active
        captured["api_token"] = api_token
        captured["connector_root"] = connector_root
        return {
            "schema": "rumi.cloudflare.environment.v1",
            "active": bool(active),
            "status": "blocked",
            "runner_deploy_ready": False,
            "sandbox_ready": False,
            "pages_ready": False,
            "zones_ready": False,
            "named_tunnel_ready": False,
            "stable_pc_tunnel_ready": False,
            "pc_tool_bridge_ready": False,
            "blockers": [],
            "constraints": {},
        }

    monkeypatch.setattr(diagnostics, "cloudflare_environment_status", fake_environment_status)

    status = oauth_store.provider_oauth_status("cloudflare", active_diagnostics=True)

    assert captured == {
        "active": True,
        "api_token": "cloudflare-secret-token",
        "connector_root": DEFAULTSPACK_ROOT,
    }
    assert "cloudflare-secret-token" not in str(status)


def test_cloudflare_environment_uses_caller_captured_local_wrangler(tmp_path, monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    local_bin = (
        tmp_path
        / "cloudflare"
        / "pc_tool_bridge"
        / "node_modules"
        / ".bin"
        / "wrangler"
    )
    local_bin.parent.mkdir(parents=True)
    local_bin.write_text("#!/bin/sh\n")
    local_bin.chmod(0o755)

    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/usr/local/bin/npx" if name == "npx" else None)

    assert diagnostics._wrangler_command({}, connector_root=tmp_path) == [str(local_bin)]


def test_cloudflare_environment_does_not_fall_back_to_downloadable_npx_wrangler(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/usr/local/bin/npx" if name == "npx" else None)
    assert diagnostics._wrangler_command({}) == []

    status = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda argv, _timeout: (_ for _ in ()).throw(AssertionError(f"unexpected command: {argv}")),
        env={},
    )

    assert status["checks"]["wrangler"]["status"] == "missing"
    assert "RUMI_WRANGLER_COMMAND" in status["checks"]["wrangler"]["detail"]
    assert "node_modules/.bin/wrangler" in status["checks"]["wrangler"]["detail"]
    assert "auto-download" in status["checks"]["wrangler"]["detail"]
    assert "CLOUDFLARE_WRANGLER_MISSING" in {item["code"] for item in status["blockers"]}


def test_cloudflare_environment_active_diagnostics_reports_paid_plan_and_tunnel_blockers(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: f"/usr/local/bin/{name}" if name in {"cloudflared", "docker"} else None,
    )

    def runner(argv, _timeout):
        args = tuple(argv)
        if args == ("/usr/local/bin/npx", "wrangler", "--version"):
            return diagnostics.CommandResult(0, "4.106.0\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "whoami"):
            return diagnostics.CommandResult(0, "You are logged in with an OAuth Token.\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "pages", "project", "list"):
            return diagnostics.CommandResult(0, "rumi-line-webhook-relay\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "containers", "list"):
            return diagnostics.CommandResult(
                1,
                "",
                "Unauthorized: You do not have access to Cloudflare Containers. Deploying containers requires the Workers Paid plan.",
            )
        if args == ("/usr/local/bin/cloudflared", "--version"):
            return diagnostics.CommandResult(0, "cloudflared version 2026.3.0\n", "")
        if args == ("/usr/local/bin/cloudflared", "tunnel", "list"):
            return diagnostics.CommandResult(1, "", "No file cert.pem; client didn't specify origincert path")
        if args == ("/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"):
            return diagnostics.CommandResult(1, "", "Cannot connect to the Docker daemon")
        return diagnostics.CommandResult(127, "", f"unexpected command: {args}")

    status = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=runner,
        env={"RUMI_WRANGLER_COMMAND": "/usr/local/bin/npx wrangler"},
    )

    assert status["status"] == "blocked"
    assert status["pages_ready"] is True
    assert status["sandbox_ready"] is False
    assert status["runner_deploy_ready"] is False
    assert status["named_tunnel_ready"] is False
    assert status["stable_pc_tunnel_ready"] is False
    assert status["pc_tool_bridge_ready"] is False
    assert status["free_plan_supported"] is False
    assert status["checks"]["containers"]["status"] == "paid_plan_required"
    assert status["checks"]["zones"]["status"] == "not_checked"
    assert status["checks"]["named_tunnel"]["status"] == "origin_cert_missing"
    assert status["checks"]["pc_tunnel_env"]["status"] == "not_configured"
    assert status["checks"]["pc_tool_bridge_env"]["status"] == "not_configured"
    assert status["checks"]["docker"]["status"] == "daemon_unavailable"
    assert status["deployment"]["sandbox_bridge_url_env"] == "RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL"
    assert status["deployment"]["pc_tunnel_scaffold"] == "connector://cloudflare/pc_tunnel"
    assert status["deployment"]["pc_tool_bridge_scaffold"] == "connector://cloudflare/pc_tool_bridge"
    assert status["constraints"]["quick_tunnels_do_not_support_sse"] is True
    assert status["constraints"]["trycloudflare_urls_are_not_stable_pc_tunnel_hostnames"] is True
    assert status["constraints"]["all_tools_cloudflare_native_supported"] is False
    assert status["constraints"]["pc_local_tools_require_pc_bridge"] is True
    assert status["constraints"]["pc_tool_bridge_does_not_upload_pc_local_tools"] is True
    assert status["constraints"]["pc_tool_bridge_preserves_pc_approval_authority"] is True
    assert {item["code"] for item in status["blockers"]} >= {
        "CLOUDFLARE_CONTAINERS_PAID_PLAN_REQUIRED",
        "CLOUDFLARE_ZONES_NOT_CHECKED",
        "CLOUDFLARE_NAMED_TUNNEL_ORIGIN_CERT_MISSING",
        "CLOUDFLARE_PC_TUNNEL_ENV_NOT_CONFIGURED",
        "CLOUDFLARE_PC_TOOL_BRIDGE_ENV_NOT_CONFIGURED",
        "CLOUDFLARE_DOCKER_DAEMON_UNAVAILABLE",
    }


def test_cloudflare_environment_reports_inactive_wrangler_managed_named_tunnel(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: f"/usr/local/bin/{name}" if name in {"cloudflared", "docker"} else None,
    )

    def runner(argv, _timeout):
        args = tuple(argv)
        if args == ("/usr/local/bin/npx", "wrangler", "--version"):
            return diagnostics.CommandResult(0, "4.106.0\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "whoami"):
            return diagnostics.CommandResult(0, "You are logged in with an OAuth Token.\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "pages", "project", "list"):
            return diagnostics.CommandResult(0, "rumi-line-webhook-relay\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "containers", "list"):
            return diagnostics.CommandResult(
                1,
                "",
                "Unauthorized: Deploying containers requires the Workers Paid plan.",
            )
        if args == ("/usr/local/bin/npx", "wrangler", "tunnel", "list"):
            return diagnostics.CommandResult(
                0,
                "09fe4401-091d-45b2-ba3a-126dcea4be0c rumi-pc inactive cfd_tunnel\n",
                "",
            )
        if args == ("/usr/local/bin/cloudflared", "--version"):
            return diagnostics.CommandResult(0, "cloudflared version 2026.3.0\n", "")
        if args == ("/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"):
            return diagnostics.CommandResult(0, '"29.1.3"\n', "")
        return diagnostics.CommandResult(127, "", f"unexpected command: {args}")

    status = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=runner,
        env={"RUMI_WRANGLER_COMMAND": "/usr/local/bin/npx wrangler"},
    )

    assert status["named_tunnel_ready"] is False
    assert status["stable_pc_tunnel_ready"] is False
    assert status["checks"]["named_tunnel"]["status"] == "not_running"
    assert status["checks"]["named_tunnel"]["manager"] == "wrangler"
    assert status["checks"]["named_tunnel"]["tunnel_count"] == 1
    assert status["checks"]["named_tunnel"]["active_count"] == 0
    assert status["checks"]["named_tunnel"]["inactive_count"] == 1
    assert status["checks"]["zones"]["status"] == "not_checked"
    assert "CLOUDFLARE_NAMED_TUNNEL_ORIGIN_CERT_MISSING" not in {
        item["code"] for item in status["blockers"]
    }
    assert {item["code"] for item in status["blockers"]} >= {
        "CLOUDFLARE_CONTAINERS_PAID_PLAN_REQUIRED",
        "CLOUDFLARE_ZONES_NOT_CHECKED",
        "CLOUDFLARE_NAMED_TUNNEL_NOT_RUNNING",
        "CLOUDFLARE_PC_TUNNEL_ENV_NOT_CONFIGURED",
        "CLOUDFLARE_PC_TOOL_BRIDGE_ENV_NOT_CONFIGURED",
    }


def test_cloudflare_environment_reports_missing_cloudflare_zone(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: None)

    status = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        api_fetcher=lambda _path, _token, _timeout: {
            "success": True,
            "result": [],
            "result_info": {"count": 0, "total_count": 0},
        },
        api_token="cloudflare-secret-token",
        env={},
    )

    assert status["zones_ready"] is False
    assert status["checks"]["zones"]["status"] == "missing_cloudflare_zone"
    assert status["checks"]["zones"]["zone_count"] == 0
    assert "cloudflare-secret-token" not in str(status)
    assert "CLOUDFLARE_ZONES_MISSING_CLOUDFLARE_ZONE" in {
        item["code"] for item in status["blockers"]
    }


def test_cloudflare_environment_rejects_pages_dev_as_stable_pc_tunnel(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: None)

    status = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        env={
            "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME": "rumi.pages.dev",
            "RUMI_CLOUDFLARE_PC_TUNNEL_ORIGIN_URL": "http://127.0.0.1:8765",
        },
    )

    assert status["checks"]["pc_tunnel_env"]["status"] == "pages_dev_not_supported"
    assert status["checks"]["pc_tunnel_env"]["hostname"] == "rumi.pages.dev"
    assert status["constraints"]["pages_dev_is_not_a_pc_tunnel_hostname"] is True
    assert "CLOUDFLARE_PC_TUNNEL_ENV_PAGES_DEV_NOT_SUPPORTED" in {
        item["code"] for item in status["blockers"]
    }


def test_cloudflare_environment_rejects_quick_tunnel_and_private_hosts(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: None)

    quick = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        env={
            "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME": "random.trycloudflare.com",
            "RUMI_CLOUDFLARE_PC_TUNNEL_ORIGIN_URL": "http://127.0.0.1:8765",
        },
    )
    private = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        env={
            "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME": "192.168.1.20",
            "RUMI_CLOUDFLARE_PC_TUNNEL_ORIGIN_URL": "http://127.0.0.1:8765",
        },
    )
    url = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        env={
            "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME": "https://rumi-pc.example.com/path",
            "RUMI_CLOUDFLARE_PC_TUNNEL_ORIGIN_URL": "http://127.0.0.1:8765",
        },
    )

    assert quick["checks"]["pc_tunnel_env"]["status"] == "trycloudflare_not_stable"
    assert private["checks"]["pc_tunnel_env"]["status"] == "not_public_hostname"
    assert url["checks"]["pc_tunnel_env"]["status"] == "invalid_hostname"


def test_cloudflare_environment_accepts_configured_pc_tool_bridge_env(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: f"/usr/local/bin/{name}" if name in {"cloudflared", "docker"} else None,
    )

    def runner(argv, _timeout):
        args = tuple(argv)
        if args == ("/usr/local/bin/npx", "wrangler", "--version"):
            return diagnostics.CommandResult(0, "4.106.0\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "whoami"):
            return diagnostics.CommandResult(0, "You are logged in with an OAuth Token.\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "pages", "project", "list"):
            return diagnostics.CommandResult(0, "rumi-pages\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "containers", "list"):
            return diagnostics.CommandResult(0, "container-id\n", "")
        if args == ("/usr/local/bin/npx", "wrangler", "tunnel", "list"):
            return diagnostics.CommandResult(
                0,
                "09fe4401-091d-45b2-ba3a-126dcea4be0c rumi-pc active cfd_tunnel\n",
                "",
            )
        if args == ("/usr/local/bin/cloudflared", "--version"):
            return diagnostics.CommandResult(0, "cloudflared version 2026.3.0\n", "")
        if args == ("/usr/local/bin/cloudflared", "tunnel", "list"):
            return diagnostics.CommandResult(0, "rumi-pc\n", "")
        if args == ("/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"):
            return diagnostics.CommandResult(0, '"29.0.0"\n', "")
        return diagnostics.CommandResult(127, "", f"unexpected command: {args}")

    status = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=runner,
        api_fetcher=lambda _path, _token, _timeout: {
            "success": True,
            "result": [{"id": "zone-id"}],
            "result_info": {"count": 1, "total_count": 1},
        },
        api_token="cloudflare-secret-token",
        env={
            "RUMI_WRANGLER_COMMAND": "/usr/local/bin/npx wrangler",
            "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME": "rumi-pc.example.com",
            "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL": "https://rumi-cloudflare-pc-tool-bridge.example.workers.dev",
            "RUMI_PC_TOOL_BRIDGE_TOKEN": "client-secret",
            "RUMI_PC_RUNTIME_BEARER": "pc-runtime-secret",
            "RUMI_PC_ORIGIN": "https://rumi-pc.example.com",
            "RUMI_PC_TOOL_BRIDGE_ALLOWED_ORIGIN": "https://app.example.com",
        },
    )

    assert status["status"] == "ready"
    assert status["zones_ready"] is True
    assert status["pc_tool_bridge_ready"] is True
    assert status["stable_pc_tunnel_ready"] is True
    assert status["checks"]["named_tunnel"]["status"] == "ready"
    assert status["checks"]["named_tunnel"]["active_count"] == 1
    assert status["checks"]["named_tunnel"]["inactive_count"] == 0
    assert status["checks"]["pc_tool_bridge_env"]["status"] == "configured"
    assert status["checks"]["pc_tool_bridge_env"]["bridge_token_configured"] is True
    assert status["checks"]["pc_tool_bridge_env"]["pc_runtime_bearer_configured"] is True
    assert status["checks"]["pc_tool_bridge_env"]["allowed_origin"] == "https://app.example.com"
    assert status["checks"]["pc_tool_bridge_env"]["pc_origin"] == "https://rumi-pc.example.com"
    assert status["blockers"] == []


def test_cloudflare_environment_rejects_invalid_pc_tool_bridge_env(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import (
        cloudflare_diagnostics as diagnostics,
    )

    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: None)

    pages = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        env={
            "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL": "https://rumi.pages.dev",
            "RUMI_PC_TOOL_BRIDGE_TOKEN": "client-secret",
            "RUMI_PC_RUNTIME_BEARER": "pc-runtime-secret",
            "RUMI_PC_ORIGIN": "https://rumi-pc.example.com",
        },
    )
    private_pc_origin = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        env={
            "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL": "https://rumi-tool.example.workers.dev",
            "RUMI_PC_TOOL_BRIDGE_TOKEN": "client-secret",
            "RUMI_PC_RUNTIME_BEARER": "pc-runtime-secret",
            "RUMI_PC_ORIGIN": "http://192.168.1.20:8765",
        },
    )
    missing_secret = diagnostics.cloudflare_environment_status(
        active=True,
        command_runner=lambda _argv, _timeout: diagnostics.CommandResult(127, "", "missing"),
        env={
            "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL": "https://rumi-tool.example.workers.dev",
            "RUMI_PC_RUNTIME_BEARER": "pc-runtime-secret",
            "RUMI_PC_ORIGIN": "https://rumi-pc.example.com",
        },
    )

    assert pages["checks"]["pc_tool_bridge_env"]["status"] == "pages_dev_not_supported"
    assert private_pc_origin["checks"]["pc_tool_bridge_env"]["status"] == "invalid_pc_origin"
    assert missing_secret["checks"]["pc_tool_bridge_env"]["status"] == "bridge_token_missing"


def test_cloudflare_sdk_adapter_routes_pages_operations_through_sdk(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import cloudflare_sdk as sdk_client

    calls: list[tuple[str, dict[str, object]]] = []

    class Resource:
        def __init__(self, **payload: object) -> None:
            self._payload = payload

        def model_dump(self, *, mode: str = "json", exclude_none: bool = True) -> dict[str, object]:
            assert mode == "json"
            assert exclude_none is True
            return dict(self._payload)

    class Deployments:
        def create(self, project_name: str, **kwargs: object) -> Resource:
            calls.append(("pages.projects.deployments.create", {"project_name": project_name, **kwargs}))
            return Resource(id="deployment-id", project_name=project_name)

        def list(self, project_name: str, **kwargs: object) -> list[Resource]:
            calls.append(("pages.projects.deployments.list", {"project_name": project_name, **kwargs}))
            return [Resource(id="deployment-id", project_name=project_name)]

        def delete(self, deployment_id: str, **kwargs: object) -> dict[str, object]:
            calls.append(("pages.projects.deployments.delete", {"deployment_id": deployment_id, **kwargs}))
            return {"id": deployment_id, "deleted": True}

    class Projects:
        def __init__(self) -> None:
            self.deployments = Deployments()

        def create(self, **kwargs: object) -> Resource:
            calls.append(("pages.projects.create", dict(kwargs)))
            return Resource(name=str(kwargs["name"]), production_branch=str(kwargs["production_branch"]))

        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("pages.projects.list", dict(kwargs)))
            return [Resource(name="rumi-pr440-smoke-pages-test")]

        def edit(self, project_name: str, **kwargs: object) -> Resource:
            calls.append(("pages.projects.edit", {"project_name": project_name, **kwargs}))
            return Resource(name=project_name, updated=True)

        def delete(self, project_name: str, **kwargs: object) -> dict[str, object]:
            calls.append(("pages.projects.delete", {"project_name": project_name, **kwargs}))
            return {"name": project_name, "deleted": True}

    class Accounts:
        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("accounts.list", dict(kwargs)))
            return [Resource(id="account-id", name="Test Account")]

    class Zones:
        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("zones.list", dict(kwargs)))
            return [Resource(id="zone-id", name="example.com")]

    class FakeCloudflare:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("Cloudflare", dict(kwargs)))
            self.accounts = Accounts()
            self.zones = Zones()
            self.pages = SimpleNamespace(projects=Projects())

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(sdk_client.importlib, "import_module", lambda _name: SimpleNamespace(Cloudflare=FakeCloudflare))

    adapter = sdk_client.CloudflareSDKAdapter(api_token="cloudflare-secret-token", account_id="account-id")
    accounts = adapter.list_accounts(per_page=1)
    zones = adapter.list_zones(per_page=50)
    project = adapter.create_pages_project(name="rumi-pr440-smoke-pages-test")
    projects = adapter.list_pages_projects(per_page=50)
    updated = adapter.update_pages_project("rumi-pr440-smoke-pages-test", production_branch="main")
    deployment = adapter.create_pages_deployment("rumi-pr440-smoke-pages-test", branch="main")
    deployments = adapter.list_pages_deployments("rumi-pr440-smoke-pages-test", per_page=50)
    deleted_deployment = adapter.delete_pages_deployment("rumi-pr440-smoke-pages-test", "deployment-id")
    deleted_project = adapter.delete_pages_project("rumi-pr440-smoke-pages-test")

    assert accounts == [{"id": "account-id", "name": "Test Account"}]
    assert zones == [{"id": "zone-id", "name": "example.com"}]
    assert project["name"] == "rumi-pr440-smoke-pages-test"
    assert projects == [{"name": "rumi-pr440-smoke-pages-test"}]
    assert updated["updated"] is True
    assert deployment["id"] == "deployment-id"
    assert deployments == [{"id": "deployment-id", "project_name": "rumi-pr440-smoke-pages-test"}]
    assert deleted_deployment["deleted"] is True
    assert deleted_project["deleted"] is True
    assert [name for name, _payload in calls] == [
        "Cloudflare",
        "accounts.list",
        "Cloudflare",
        "zones.list",
        "Cloudflare",
        "pages.projects.create",
        "Cloudflare",
        "pages.projects.list",
        "Cloudflare",
        "pages.projects.edit",
        "Cloudflare",
        "pages.projects.deployments.create",
        "Cloudflare",
        "pages.projects.deployments.list",
        "Cloudflare",
        "pages.projects.deployments.delete",
        "Cloudflare",
        "pages.projects.delete",
    ]
    assert calls[0][1] == {"api_token": "cloudflare-secret-token"}
    assert calls[3][1]["per_page"] == 50
    assert calls[7][1]["per_page"] == 10
    assert calls[13][1]["per_page"] == 10
    assert "cloudflare-secret-token" not in str(
        [accounts, project, projects, updated, deployment, deployments, deleted_deployment, deleted_project]
    )


def test_cloudflare_sdk_adapter_redacts_token_from_errors(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import cloudflare_sdk as sdk_client

    class Projects:
        def list(self, **_kwargs: object) -> list[object]:
            raise RuntimeError("permission denied for cloudflare-secret-token")

    class FakeCloudflare:
        def __init__(self, **_kwargs: object) -> None:
            self.pages = SimpleNamespace(projects=Projects())

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(sdk_client.importlib, "import_module", lambda _name: SimpleNamespace(Cloudflare=FakeCloudflare))

    adapter = sdk_client.CloudflareSDKAdapter(api_token="cloudflare-secret-token", account_id="account-id")
    try:
        adapter.list_pages_projects()
    except sdk_client.CloudflareSDKOperationError as exc:
        error = exc.to_dict()
    else:
        raise AssertionError("Cloudflare SDK errors should be wrapped")

    assert "cloudflare-secret-token" not in str(error)
    assert error["message"] == "permission denied for [redacted]"


def test_cloudflare_sdk_adapter_redacts_token_from_rest_error(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import cloudflare_sdk as sdk_client

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: None)

    def rest_fetcher(_method: str, _path: str, _payload: dict[str, object] | None, _headers: dict[str, str]):
        raise sdk_client.CloudflareSDKOperationError(
            "permission denied for cloudflare-secret-token",
            error_type="CloudflareAPIError",
            status_code=403,
        )

    adapter = sdk_client.CloudflareSDKAdapter(
        api_token="cloudflare-secret-token",
        account_id="account-id",
        rest_fetcher=rest_fetcher,
    )
    try:
        adapter.list_workers()
    except sdk_client.CloudflareSDKOperationError as exc:
        error = exc.to_dict()
    else:
        raise AssertionError("Cloudflare REST errors should be wrapped")

    assert "cloudflare-secret-token" not in str(error)
    assert error["message"] == "permission denied for [redacted]"
    assert error["error_type"] == "CloudflareAPIError"
    assert error["status_code"] == 403


def test_cloudflare_sdk_adapter_uses_rest_fallback_when_sdk_missing(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import cloudflare_sdk as sdk_client

    calls: list[tuple[str, str, dict[str, object] | None, dict[str, str]]] = []
    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: None)

    def rest_fetcher(method: str, path: str, payload: dict[str, object] | None, headers: dict[str, str]):
        calls.append((method, path, payload, headers))
        return {"success": True, "result": [{"id": "worker-id"}]}

    adapter = sdk_client.CloudflareSDKAdapter(
        api_token="cloudflare-secret-token",
        account_id="account-id",
        rest_fetcher=rest_fetcher,
    )

    workers = adapter.list_workers(per_page=2)

    assert workers == [{"id": "worker-id"}]
    assert calls[0][0] == "GET"
    assert calls[0][1] == "/accounts/account-id/workers/scripts?per_page=2"
    assert calls[0][2] is None
    assert calls[0][3]["Authorization"] == "Bearer cloudflare-secret-token"


def test_cloudflare_sdk_adapter_routes_runner_resources_through_sdk(monkeypatch):
    from ecosystem.rumi_provider_registry_pack.runtime import cloudflare_sdk as sdk_client

    calls: list[tuple[str, dict[str, object]]] = []

    class Resource:
        def __init__(self, **payload: object) -> None:
            self._payload = payload

        def model_dump(self, *, mode: str = "json", exclude_none: bool = True) -> dict[str, object]:
            assert mode == "json"
            assert exclude_none is True
            return dict(self._payload)

    class WorkerSecrets:
        def update(self, script_name: str, **kwargs: object) -> Resource:
            calls.append(("workers.scripts.secrets.update", {"script_name": script_name, **kwargs}))
            return Resource(name=str(kwargs["name"]))

    class WorkerDeployments:
        def list(self, script_name: str, **kwargs: object) -> list[Resource]:
            calls.append(("workers.scripts.deployments.list", {"script_name": script_name, **kwargs}))
            return [Resource(id="worker-deployment")]

        def create(self, script_name: str, **kwargs: object) -> Resource:
            calls.append(("workers.scripts.deployments.create", {"script_name": script_name, **kwargs}))
            return Resource(id="worker-deployment")

    class WorkerSettings:
        def edit(self, script_name: str, **kwargs: object) -> Resource:
            calls.append(("workers.scripts.settings.edit", {"script_name": script_name, **kwargs}))
            return Resource(updated=True)

    class WorkerScripts:
        def __init__(self) -> None:
            self.deployments = WorkerDeployments()
            self.settings = WorkerSettings()
            self.secrets = WorkerSecrets()

        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("workers.scripts.list", dict(kwargs)))
            return [Resource(id="rumi-worker")]

        def get(self, script_name: str, **kwargs: object) -> Resource:
            calls.append(("workers.scripts.get", {"script_name": script_name, **kwargs}))
            return Resource(id=script_name)

        def update(self, script_name: str, **kwargs: object) -> Resource:
            calls.append(("workers.scripts.update", {"script_name": script_name, **kwargs}))
            return Resource(id=script_name, updated=True)

        def delete(self, script_name: str, **kwargs: object) -> dict[str, object]:
            calls.append(("workers.scripts.delete", {"script_name": script_name, **kwargs}))
            return {"id": script_name, "deleted": True}

    class D1Database:
        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("d1.database.list", dict(kwargs)))
            return [Resource(name="rumi-d1", uuid="d1-id")]

        def create(self, **kwargs: object) -> Resource:
            calls.append(("d1.database.create", dict(kwargs)))
            return Resource(name=str(kwargs["name"]), uuid="d1-id")

        def get(self, database_id: str, **kwargs: object) -> Resource:
            calls.append(("d1.database.get", {"database_id": database_id, **kwargs}))
            return Resource(uuid=database_id)

        def delete(self, database_id: str, **kwargs: object) -> dict[str, object]:
            calls.append(("d1.database.delete", {"database_id": database_id, **kwargs}))
            return {"uuid": database_id, "deleted": True}

        def query(self, database_id: str, **kwargs: object) -> Resource:
            calls.append(("d1.database.query", {"database_id": database_id, **kwargs}))
            return Resource(rows=[])

    class R2Objects:
        def put(self, bucket_name: str, **kwargs: object) -> Resource:
            calls.append(("r2.buckets.objects.put", {"bucket_name": bucket_name, **kwargs}))
            return Resource(key=str(kwargs["key"]))

    class R2Buckets:
        def __init__(self) -> None:
            self.objects = R2Objects()

        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("r2.buckets.list", dict(kwargs)))
            return [Resource(name="rumi-bucket")]

        def create(self, **kwargs: object) -> Resource:
            calls.append(("r2.buckets.create", dict(kwargs)))
            return Resource(name=str(kwargs["name"]))

        def get(self, name: str, **kwargs: object) -> Resource:
            calls.append(("r2.buckets.get", {"name": name, **kwargs}))
            return Resource(name=name)

        def delete(self, name: str, **kwargs: object) -> dict[str, object]:
            calls.append(("r2.buckets.delete", {"name": name, **kwargs}))
            return {"name": name, "deleted": True}

    class QueueConsumers:
        def create(self, queue_name: str, **kwargs: object) -> Resource:
            calls.append(("queues.consumers.create", {"queue_name": queue_name, **kwargs}))
            return Resource(id="consumer-id")

    class Queues:
        def __init__(self) -> None:
            self.consumers = QueueConsumers()

        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("queues.list", dict(kwargs)))
            return [Resource(queue_name="rumi-queue")]

        def create(self, **kwargs: object) -> Resource:
            calls.append(("queues.create", dict(kwargs)))
            return Resource(queue_name=str(kwargs["queue_name"]))

        def get(self, queue_name: str, **kwargs: object) -> Resource:
            calls.append(("queues.get", {"queue_name": queue_name, **kwargs}))
            return Resource(queue_name=queue_name)

        def delete(self, queue_name: str, **kwargs: object) -> dict[str, object]:
            calls.append(("queues.delete", {"queue_name": queue_name, **kwargs}))
            return {"queue_name": queue_name, "deleted": True}

    class WorkflowInstances:
        def create(self, workflow_name: str, **kwargs: object) -> Resource:
            calls.append(("workflows.instances.create", {"workflow_name": workflow_name, **kwargs}))
            return Resource(id="instance-id")

    class Workflows:
        def __init__(self) -> None:
            self.instances = WorkflowInstances()

        def list(self, **kwargs: object) -> list[Resource]:
            calls.append(("workflows.list", dict(kwargs)))
            return [Resource(name="rumi-workflow")]

        def get(self, workflow_name: str, **kwargs: object) -> Resource:
            calls.append(("workflows.get", {"workflow_name": workflow_name, **kwargs}))
            return Resource(name=workflow_name)

        def update(self, workflow_name: str, **kwargs: object) -> Resource:
            calls.append(("workflows.update", {"workflow_name": workflow_name, **kwargs}))
            return Resource(name=workflow_name, updated=True)

        def delete(self, workflow_name: str, **kwargs: object) -> dict[str, object]:
            calls.append(("workflows.delete", {"workflow_name": workflow_name, **kwargs}))
            return {"name": workflow_name, "deleted": True}

    class FakeCloudflare:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("Cloudflare", dict(kwargs)))
            self.workers = SimpleNamespace(scripts=WorkerScripts())
            self.d1 = SimpleNamespace(database=D1Database())
            self.r2 = SimpleNamespace(buckets=R2Buckets())
            self.queues = Queues()
            self.workflows = Workflows()

    monkeypatch.setattr(sdk_client.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(sdk_client.importlib, "import_module", lambda _name: SimpleNamespace(Cloudflare=FakeCloudflare))

    adapter = sdk_client.CloudflareSDKAdapter(api_token="cloudflare-secret-token", account_id="account-id")

    adapter.list_workers(per_page=2)
    adapter.get_worker("rumi-worker")
    adapter.upload_worker_module("rumi-worker", main_module="worker.js", modules=[], bindings={})
    adapter.patch_worker_settings("rumi-worker", settings={"logpush": True})
    adapter.list_worker_deployments("rumi-worker")
    adapter.create_worker_deployment("rumi-worker", version_id="version-id")
    adapter.put_worker_secret("rumi-worker", "RUMI_SECRET", "secret-value")
    adapter.delete_worker("rumi-worker")
    adapter.list_d1_databases()
    adapter.create_d1_database("rumi-d1")
    adapter.get_d1_database("d1-id")
    adapter.query_d1_database("d1-id", "select 1")
    adapter.delete_d1_database("d1-id")
    adapter.list_r2_buckets()
    adapter.create_r2_bucket("rumi-bucket")
    adapter.get_r2_bucket("rumi-bucket")
    adapter.upload_r2_object("rumi-bucket", "key.txt", "value")
    adapter.delete_r2_bucket("rumi-bucket")
    adapter.list_queues()
    adapter.create_queue("rumi-queue")
    adapter.get_queue("rumi-queue")
    adapter.create_queue_consumer("rumi-queue", script_name="rumi-worker")
    adapter.delete_queue("rumi-queue")
    adapter.list_workflows()
    adapter.get_workflow("rumi-workflow")
    adapter.put_workflow("rumi-workflow", script_name="rumi-worker", class_name="RumiWorkflow")
    adapter.create_workflow_instance("rumi-workflow", {"hello": "world"})
    adapter.delete_workflow("rumi-workflow")

    call_names = [name for name, _payload in calls if name != "Cloudflare"]
    assert call_names == [
        "workers.scripts.list",
        "workers.scripts.get",
        "workers.scripts.update",
        "workers.scripts.settings.edit",
        "workers.scripts.deployments.list",
        "workers.scripts.deployments.create",
        "workers.scripts.secrets.update",
        "workers.scripts.delete",
        "d1.database.list",
        "d1.database.create",
        "d1.database.get",
        "d1.database.query",
        "d1.database.delete",
        "r2.buckets.list",
        "r2.buckets.create",
        "r2.buckets.get",
        "r2.buckets.objects.put",
        "r2.buckets.delete",
        "queues.list",
        "queues.create",
        "queues.get",
        "queues.consumers.create",
        "queues.delete",
        "workflows.list",
        "workflows.get",
        "workflows.update",
        "workflows.instances.create",
        "workflows.delete",
    ]
    assert any(name == "workers.scripts.secrets.update" for name in call_names)


def test_cloudflare_runner_provisioner_plan_and_dry_run_are_side_effect_free():
    from domain.cloudflare.provisioning import CloudflareRunnerProvisioner, CloudflareRunnerSpec

    class Client:
        calls: list[str] = []

    provisioner = CloudflareRunnerProvisioner(Client(), capabilities=["cloudflare.runner.deploy"])
    spec = CloudflareRunnerSpec(account_id="acct", prefix="Rumi Test!")

    plan = provisioner.plan(spec)
    dry_run = provisioner.deploy(spec, dry_run=True)

    assert plan["status"] == "ready"
    assert dry_run["dry_run"] is True
    assert plan["resources"] == {
        "worker": "rumi-test-worker",
        "d1": "rumi-test-d1",
        "r2": "rumi-test-artifacts",
        "queue": "rumi-test-queue",
        "workflow": "rumi-test-workflow",
    }
    assert Client.calls == []


def test_cloudflare_runner_provisioner_blocks_deploy_without_capability():
    from domain.cloudflare.provisioning import CloudflareRunnerProvisioner, CloudflareRunnerSpec

    class Client:
        def __getattr__(self, name: str):
            raise AssertionError(f"unexpected write call: {name}")

    result = CloudflareRunnerProvisioner(Client()).deploy(
        CloudflareRunnerSpec(account_id="acct", prefix="rumi-test"),
        dry_run=False,
    )

    assert result["status"] == "blocked"
    assert result["blockers"][0]["code"] == "insufficient_capabilities"


def test_cloudflare_runner_provisioner_deploy_order_is_stable():
    from domain.cloudflare.provisioning import CloudflareRunnerProvisioner, CloudflareRunnerSpec

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def list_d1_databases(self, **_kwargs: object) -> list[dict[str, object]]:
            self.calls.append("list_d1")
            return []

        def create_d1_database(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("create_d1")
            return {"uuid": "d1-id"}

        def get_r2_bucket(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("get_r2")
            raise RuntimeError("missing")

        def create_r2_bucket(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("create_r2")
            return {"name": "bucket"}

        def get_queue(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("get_queue")
            raise RuntimeError("missing")

        def create_queue(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("create_queue")
            return {"queue_name": "queue"}

        def get_workflow(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("get_workflow")
            raise RuntimeError("missing")

        def put_workflow(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("put_workflow")
            return {"name": "workflow"}

        def upload_worker_module(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls.append("upload_worker")
            return {"id": "worker"}

        def patch_worker_secrets(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            self.calls.append("patch_secrets")
            return []

    client = Client()
    result = CloudflareRunnerProvisioner(client, capabilities=["cloudflare.runner.deploy"]).deploy(
        CloudflareRunnerSpec(account_id="acct", prefix="rumi-test"),
        dry_run=False,
    )

    assert result["status"] == "deployed"
    assert client.calls == [
        "list_d1",
        "create_d1",
        "get_r2",
        "create_r2",
        "get_queue",
        "create_queue",
        "get_workflow",
        "put_workflow",
        "upload_worker",
        "patch_secrets",
    ]
