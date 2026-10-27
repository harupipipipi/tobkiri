"""Cloudflare connector SDK adapter owned by the provider registry Pack."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import importlib
import importlib.util
import json
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


_CLOUDFLARE_PAGES_MAX_PAGE_SIZE = 10


@dataclass(frozen=True)
class CloudflareSDKStatus:
    available: bool
    status: str
    package: str = "cloudflare"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "status": self.status,
            "package": self.package,
            "detail": self.detail,
        }


class CloudflareSDKOperationError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "cloudflare_sdk_operation_failed",
            "message": str(self),
            "error_type": self.error_type,
            "status_code": self.status_code,
        }


def cloudflare_sdk_status() -> dict[str, Any]:
    if importlib.util.find_spec("cloudflare") is None:
        return CloudflareSDKStatus(
            available=False,
            status="sdk_missing",
            detail="Install the official Cloudflare Python SDK to enable provisioning.",
        ).to_dict()
    return CloudflareSDKStatus(
        available=True,
        status="ready",
        detail="Cloudflare Python SDK is importable.",
    ).to_dict()


class CloudflareSDKAdapter:
    def __init__(
        self,
        *,
        api_token: str | None = None,
        account_id: str | None = None,
        rest_fetcher: Callable[[str, str, dict[str, Any] | None, dict[str, str]], Any] | None = None,
    ) -> None:
        self._api_token = str(api_token or "").strip()
        self._account_id = str(account_id or "").strip()
        self._rest_fetcher = rest_fetcher

    def status(self) -> dict[str, Any]:
        status = cloudflare_sdk_status()
        return {
            **status,
            "account_configured": bool(self._account_id),
            "token_configured": bool(self._api_token),
        }

    def client(self) -> Any:
        status = cloudflare_sdk_status()
        if not status.get("available"):
            raise RuntimeError(str(status.get("status") or "sdk_missing"))
        module = importlib.import_module("cloudflare")
        client_factory = getattr(module, "Cloudflare", None)
        if not callable(client_factory):
            raise RuntimeError("sdk_invalid")
        kwargs: dict[str, str] = {}
        if self._api_token:
            kwargs["api_token"] = self._api_token
        return client_factory(**kwargs)

    def list_accounts(self, *, per_page: int = 50) -> list[dict[str, Any]]:
        return self._call(lambda client: _serialize_collection(client.accounts.list(per_page=per_page)))

    def get_account(self, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(lambda client: _serialize_resource(client.accounts.get(account_id=account_id)))

    def verify_token(self) -> dict[str, Any]:
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.user.tokens.verify()),
            "GET",
            "/user/tokens/verify",
            collection=False,
        )

    def list_zones(self, *, per_page: int = 50) -> list[dict[str, Any]]:
        return self._call(lambda client: _serialize_collection(client.zones.list(per_page=per_page)))

    def list_pages_projects(self, *, account_id: str | None = None, per_page: int = 10) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_collection(
                client.pages.projects.list(
                    account_id=account_id,
                    per_page=_bounded_pages_page_size(per_page),
                )
            )
        )

    def get_pages_project(self, project_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_resource(
                client.pages.projects.get(project_name, account_id=account_id)
            )
        )

    def create_pages_project(
        self,
        *,
        name: str,
        production_branch: str = "main",
        account_id: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_resource(
                client.pages.projects.create(
                    account_id=account_id,
                    name=name,
                    production_branch=production_branch,
                    **params,
                )
            )
        )

    def update_pages_project(
        self,
        project_name: str,
        *,
        account_id: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_resource(
                client.pages.projects.edit(project_name, account_id=account_id, **params)
            )
        )

    def delete_pages_project(self, project_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_resource(
                client.pages.projects.delete(project_name, account_id=account_id)
            )
        )

    def create_pages_deployment(
        self,
        project_name: str,
        *,
        account_id: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_resource(
                client.pages.projects.deployments.create(project_name, account_id=account_id, **params)
            )
        )

    def list_pages_deployments(
        self,
        project_name: str,
        *,
        account_id: str | None = None,
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_collection(
                client.pages.projects.deployments.list(
                    project_name,
                    account_id=account_id,
                    per_page=_bounded_pages_page_size(per_page),
                )
            )
        )

    def get_pages_deployment(
        self,
        project_name: str,
        deployment_id: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_resource(
                client.pages.projects.deployments.get(
                    deployment_id,
                    account_id=account_id,
                    project_name=project_name,
                )
            )
        )

    def delete_pages_deployment(
        self,
        project_name: str,
        deployment_id: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call(
            lambda client: _serialize_resource(
                client.pages.projects.deployments.delete(
                    deployment_id,
                    account_id=account_id,
                    project_name=project_name,
                )
            )
        )

    def list_workers(self, *, account_id: str | None = None, per_page: int = 50) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_collection(client.workers.scripts.list(account_id=account_id, per_page=per_page)),
            "GET",
            f"/accounts/{account_id}/workers/scripts",
            query={"per_page": per_page},
        )

    def get_worker(self, script_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.workers.scripts.get(script_name, account_id=account_id)),
            "GET",
            f"/accounts/{account_id}/workers/scripts/{script_name}",
            collection=False,
        )

    def upload_worker_module(
        self,
        script_name: str,
        *,
        main_module: str,
        modules: list[dict[str, Any]] | None = None,
        bindings: dict[str, Any] | list[dict[str, Any]] | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {
            "main_module": main_module,
            "modules": list(modules or []),
            "bindings": bindings or {},
        }
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.workers.scripts.update(script_name, account_id=account_id, **payload)
            ),
            "PUT",
            f"/accounts/{account_id}/workers/scripts/{script_name}",
            payload,
            collection=False,
        )

    def patch_worker_settings(
        self,
        script_name: str,
        *,
        settings: dict[str, Any],
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.workers.scripts.settings.edit(script_name, account_id=account_id, **dict(settings or {}))
            ),
            "PATCH",
            f"/accounts/{account_id}/workers/scripts/{script_name}/settings",
            dict(settings or {}),
            collection=False,
        )

    def list_worker_deployments(
        self,
        script_name: str,
        *,
        account_id: str | None = None,
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_collection(
                client.workers.scripts.deployments.list(script_name, account_id=account_id, per_page=per_page)
            ),
            "GET",
            f"/accounts/{account_id}/workers/scripts/{script_name}/deployments",
            query={"per_page": per_page},
        )

    def create_worker_deployment(
        self,
        script_name: str,
        *,
        version_id: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {"version_id": version_id} if version_id else {}
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.workers.scripts.deployments.create(script_name, account_id=account_id, **payload)
            ),
            "POST",
            f"/accounts/{account_id}/workers/scripts/{script_name}/deployments",
            payload,
            collection=False,
        )

    def delete_worker(self, script_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.workers.scripts.delete(script_name, account_id=account_id)),
            "DELETE",
            f"/accounts/{account_id}/workers/scripts/{script_name}",
            collection=False,
        )

    def put_worker_secret(
        self,
        script_name: str,
        name: str,
        value: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {"name": name, "text": value, "type": "secret_text"}
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.workers.scripts.secrets.update(script_name, account_id=account_id, **payload)
            ),
            "PUT",
            f"/accounts/{account_id}/workers/scripts/{script_name}/secrets",
            payload,
            collection=False,
        )

    def patch_worker_secrets(
        self,
        script_name: str,
        secrets: Mapping[str, str],
        *,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self.put_worker_secret(script_name, str(name), str(value), account_id=account_id)
            for name, value in dict(secrets or {}).items()
        ]

    def list_d1_databases(self, *, account_id: str | None = None, per_page: int = 50) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_collection(client.d1.database.list(account_id=account_id, per_page=per_page)),
            "GET",
            f"/accounts/{account_id}/d1/database",
            query={"per_page": per_page},
        )

    def create_d1_database(self, name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.d1.database.create(account_id=account_id, name=name)),
            "POST",
            f"/accounts/{account_id}/d1/database",
            {"name": name},
            collection=False,
        )

    def get_d1_database(self, database_id: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.d1.database.get(database_id, account_id=account_id)),
            "GET",
            f"/accounts/{account_id}/d1/database/{database_id}",
            collection=False,
        )

    def delete_d1_database(self, database_id: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.d1.database.delete(database_id, account_id=account_id)),
            "DELETE",
            f"/accounts/{account_id}/d1/database/{database_id}",
            collection=False,
        )

    def query_d1_database(
        self,
        database_id: str,
        sql: str,
        params: list[Any] | None = None,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {"sql": sql, "params": list(params or [])}
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.d1.database.query(database_id, account_id=account_id, **payload)
            ),
            "POST",
            f"/accounts/{account_id}/d1/database/{database_id}/query",
            payload,
            collection=False,
        )

    def list_r2_buckets(self, *, account_id: str | None = None, per_page: int = 50) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_collection(client.r2.buckets.list(account_id=account_id, per_page=per_page)),
            "GET",
            f"/accounts/{account_id}/r2/buckets",
            query={"per_page": per_page},
        )

    def create_r2_bucket(
        self,
        name: str,
        *,
        account_id: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {"name": name}
        if location:
            payload["location"] = location
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.r2.buckets.create(account_id=account_id, **payload)),
            "POST",
            f"/accounts/{account_id}/r2/buckets",
            payload,
            collection=False,
        )

    def get_r2_bucket(self, name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.r2.buckets.get(name, account_id=account_id)),
            "GET",
            f"/accounts/{account_id}/r2/buckets/{name}",
            collection=False,
        )

    def delete_r2_bucket(self, name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.r2.buckets.delete(name, account_id=account_id)),
            "DELETE",
            f"/accounts/{account_id}/r2/buckets/{name}",
            collection=False,
        )

    def upload_r2_object(
        self,
        bucket_name: str,
        key: str,
        value: str | bytes,
        *,
        account_id: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {
            "key": key,
            "value": value.decode("utf-8", "replace") if isinstance(value, bytes) else value,
        }
        if content_type:
            payload["content_type"] = content_type
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.r2.buckets.objects.put(bucket_name, account_id=account_id, **payload)
            ),
            "PUT",
            f"/accounts/{account_id}/r2/buckets/{bucket_name}/objects/{key}",
            payload,
            collection=False,
        )

    def list_queues(self, *, account_id: str | None = None, per_page: int = 50) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_collection(client.queues.list(account_id=account_id, per_page=per_page)),
            "GET",
            f"/accounts/{account_id}/queues",
            query={"per_page": per_page},
        )

    def create_queue(self, name: str, *, account_id: str | None = None, **params: Any) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {"queue_name": name, **dict(params or {})}
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.queues.create(account_id=account_id, **payload)),
            "POST",
            f"/accounts/{account_id}/queues",
            payload,
            collection=False,
        )

    def get_queue(self, queue_id_or_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.queues.get(queue_id_or_name, account_id=account_id)),
            "GET",
            f"/accounts/{account_id}/queues/{queue_id_or_name}",
            collection=False,
        )

    def delete_queue(self, queue_id_or_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.queues.delete(queue_id_or_name, account_id=account_id)),
            "DELETE",
            f"/accounts/{account_id}/queues/{queue_id_or_name}",
            collection=False,
        )

    def create_queue_consumer(
        self,
        queue_id_or_name: str,
        *,
        script_name: str,
        account_id: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {"script_name": script_name, **dict(params or {})}
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.queues.consumers.create(queue_id_or_name, account_id=account_id, **payload)
            ),
            "POST",
            f"/accounts/{account_id}/queues/{queue_id_or_name}/consumers",
            payload,
            collection=False,
        )

    def list_workflows(self, *, account_id: str | None = None, per_page: int = 50) -> list[dict[str, Any]]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_collection(client.workflows.list(account_id=account_id, per_page=per_page)),
            "GET",
            f"/accounts/{account_id}/workflows",
            query={"per_page": per_page},
        )

    def get_workflow(self, workflow_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.workflows.get(workflow_name, account_id=account_id)),
            "GET",
            f"/accounts/{account_id}/workflows/{workflow_name}",
            collection=False,
        )

    def put_workflow(
        self,
        workflow_name: str,
        *,
        script_name: str,
        class_name: str,
        bindings: dict[str, Any] | list[dict[str, Any]] | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        payload = {"script_name": script_name, "class_name": class_name, "bindings": bindings or {}}
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.workflows.update(workflow_name, account_id=account_id, **payload)
            ),
            "PUT",
            f"/accounts/{account_id}/workflows/{workflow_name}",
            payload,
            collection=False,
        )

    def delete_workflow(self, workflow_name: str, *, account_id: str | None = None) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(client.workflows.delete(workflow_name, account_id=account_id)),
            "DELETE",
            f"/accounts/{account_id}/workflows/{workflow_name}",
            collection=False,
        )

    def create_workflow_instance(
        self,
        workflow_name: str,
        payload: dict[str, Any],
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account_id = self._require_account_id(account_id)
        return self._call_sdk_or_rest(
            lambda client: _serialize_resource(
                client.workflows.instances.create(workflow_name, account_id=account_id, payload=payload)
            ),
            "POST",
            f"/accounts/{account_id}/workflows/{workflow_name}/instances",
            dict(payload or {}),
            collection=False,
        )

    def _require_account_id(self, account_id: str | None) -> str:
        resolved = str(account_id or self._account_id or "").strip()
        if not resolved:
            raise ValueError("cloudflare account_id is required")
        return resolved

    def _call(self, operation: Callable[[Any], Any]) -> Any:
        try:
            return operation(self.client())
        except Exception as exc:
            raise CloudflareSDKOperationError(
                _scrub_secret(str(exc), self._api_token),
                error_type=exc.__class__.__name__,
                status_code=getattr(exc, "status_code", None),
            ) from None

    def _call_sdk_or_rest(
        self,
        sdk_operation: Callable[[Any], Any],
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
        collection: bool = True,
    ) -> Any:
        try:
            return self._call(sdk_operation)
        except CloudflareSDKOperationError as exc:
            sdk_unavailable = exc.error_type == "RuntimeError" and str(exc) in {"sdk_missing", "sdk_invalid"}
            if exc.error_type not in {"AttributeError"} and not sdk_unavailable:
                raise
        return self._rest(method, path, payload, query=query, collection=collection)

    def _rest(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
        collection: bool = True,
    ) -> Any:
        if not self._api_token:
            raise CloudflareSDKOperationError("cloudflare api token is required", error_type="MissingToken")
        headers = {"Authorization": f"Bearer {self._api_token}", "Content-Type": "application/json"}
        try:
            response = (
                self._rest_fetcher(method, _url_with_query(path, query), payload, headers)
                if self._rest_fetcher is not None
                else _default_rest_fetch(method, _url_with_query(path, query), payload, headers)
            )
            result = _unwrap_cloudflare_response(response)
            return _serialize_collection(result) if collection else _serialize_resource(result)
        except CloudflareSDKOperationError as exc:
            raise CloudflareSDKOperationError(
                _scrub_secret(str(exc), self._api_token),
                error_type=exc.error_type,
                status_code=exc.status_code,
            ) from None
        except Exception as exc:
            raise CloudflareSDKOperationError(
                _scrub_secret(str(exc), self._api_token),
                error_type=exc.__class__.__name__,
                status_code=getattr(exc, "code", None) or getattr(exc, "status_code", None),
            ) from None


def _serialize_collection(value: Iterable[Any], *, max_items: int = 100) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in value:
        items.append(_serialize_resource(item))
        if len(items) >= max_items:
            break
    return items


def _bounded_pages_page_size(per_page: int) -> int:
    # Pages list endpoints reject larger page sizes even though the SDK accepts them.
    return max(1, min(int(per_page), _CLOUDFLARE_PAGES_MAX_PAGE_SIZE))


def _serialize_resource(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", exclude_none=True)
        if isinstance(dumped, Mapping):
            return {str(key): _serialize_value(item) for key, item in dumped.items()}
    if value is None:
        return {}
    return {"value": _serialize_value(value)}


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _serialize_resource(value)
    return value


def _scrub_secret(message: str, secret: str) -> str:
    if not secret:
        return message
    return message.replace(secret, "[redacted]")


def _url_with_query(path: str, query: dict[str, Any] | None = None) -> str:
    path = str(path or "")
    if not query:
        return path
    encoded = urllib.parse.urlencode({str(k): str(v) for k, v in query.items() if v is not None})
    if not encoded:
        return path
    return f"{path}?{encoded}"


def _default_rest_fetch(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
) -> Any:
    url = "https://api.cloudflare.com/client/v4" + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise CloudflareSDKOperationError(raw or str(exc), error_type="HTTPError", status_code=exc.code) from None
    return json.loads(raw) if raw else {}


def _unwrap_cloudflare_response(response: Any) -> Any:
    if not isinstance(response, Mapping):
        return response
    if "success" not in response and "result" not in response:
        return response
    if response.get("success") is False:
        raise CloudflareSDKOperationError(
            str(response.get("errors") or "Cloudflare API request failed"),
            error_type="CloudflareAPIError",
        )
    return response.get("result", {})
