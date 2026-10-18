"""Black Forest Labs official documentation catalog and async image adapter."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from ..base_provider import BaseProvider
from ..api_key_store import read_provider_api_key


class BlackForestLabsProvider(BaseProvider):
    provider_id = "black-forest-labs"
    BASE_URL = "https://api.bfl.ai"
    DOC_INDEX_URL = "https://docs.bfl.ai/llms.txt"
    _CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}

    def __init__(self, api_key: str | None = None):
        self._key = str(api_key or read_provider_api_key("black-forest-labs", "legacy") or "").strip()
        self._base_url = str(os.environ.get("BFL_BASE_URL") or self.BASE_URL).strip().rstrip("/")
        self._ssl_ctx = ssl.create_default_context()

    def _request(
        self, method: str, url: str, body: Dict[str, Any] | None = None, *, auth: bool = True
    ) -> Dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "RumiAI/1.0"}
        if auth:
            if not self._key:
                raise RuntimeError("black-forest-labs: save BFL_API_KEY")
            headers["x-key"] = self._key
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Black Forest Labs API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Black Forest Labs connection error: {error.reason}") from error
        return payload if isinstance(payload, dict) else {"data": payload}

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._key:
            return []
        cached = self._CACHE.get(self._key)
        if cached and cached[0] > time.monotonic():
            return [dict(item) for item in cached[1]]
        try:
            with urllib.request.urlopen(
                self.DOC_INDEX_URL, context=self._ssl_ctx, timeout=20
            ) as response:
                index = response.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError):
            return []
        pages = []
        for url in re.findall(r"\((https://docs\.bfl\.ml/api-reference/models/[^)]+\.md)\)", index):
            if "report-model-usage" not in url and url not in pages:
                pages.append(url)
        records: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for url in pages:
            try:
                with urllib.request.urlopen(url, context=self._ssl_ctx, timeout=20) as response:
                    document = response.read().decode("utf-8")
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue
            match = re.search(r"\bpost\s+/v1/([A-Za-z0-9._-]+)", document, re.I)
            if not match:
                continue
            model_id = match.group(1)
            if model_id in seen:
                continue
            seen.add(model_id)
            title = re.search(r"^#\s+(.+)$", document, re.M)
            records.append(
                {
                    "id": f"black-forest-labs/{model_id}",
                    "model_id": model_id,
                    "provider_id": self.provider_id,
                    "provider": self.provider_id,
                    "name": title.group(1).strip() if title else model_id,
                    "display_name": title.group(1).strip() if title else model_id,
                    "type": "image_gen",
                    "capabilities": {"image_generation": True, "text_input": True},
                    "metadata": {
                        "source": "bfl_official_openapi_catalog",
                        "capability_source": "official_openapi",
                        "capability_confidence": "official_openapi",
                        "documentation_url": url,
                        "input_schema": "pass exact input as extra_body.bfl_input",
                    },
                }
            )
        if records:
            self._CACHE[self._key] = (time.monotonic() + 86400, [dict(item) for item in records])
        return records

    def image_gen(self, model, prompt, params):
        model_id = str(model or "").removeprefix("black-forest-labs/")
        extra = (
            (params or {}).get("extra_body")
            if isinstance((params or {}).get("extra_body"), dict)
            else {}
        )
        body = (
            dict(extra.get("bfl_input") or {})
            if isinstance(extra.get("bfl_input"), dict)
            else {"prompt": str(prompt or "")}
        )
        submitted = self._request(
            "POST", f"{self._base_url}/v1/{urllib.parse.quote(model_id, safe='-_.~')}", body
        )
        task_id = str(submitted.get("id") or "").strip()
        result = submitted
        for _ in range(120):
            if not task_id:
                break
            result = self._request(
                "GET",
                f"{self._base_url}/v1/get_result?id={urllib.parse.quote(task_id, safe='-_.~')}",
            )
            status = str(result.get("status") or "").lower()
            if status in {"ready", "completed", "succeeded"}:
                break
            if status in {"error", "failed"}:
                raise RuntimeError(
                    f"Black Forest Labs generation failed: {result.get('error') or status}"
                )
            time.sleep(0.5)
        image = str(result.get("sample") or result.get("image") or "")
        return {
            "images": [image] if image else [],
            "raw_extra": {"model": model_id, "task_id": task_id},
        }
