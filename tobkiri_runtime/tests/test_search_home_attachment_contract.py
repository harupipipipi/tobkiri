from __future__ import annotations

import base64
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

pytestmark = pytest.mark.contract


def test_attachment_contract_accepts_text_and_strips_inline_content_from_route_metadata():
    from ecosystem.search_home_pack.domain.attachment_contract import (
        attachment_metadata,
        normalize_attachments,
    )

    attachments = normalize_attachments(
        [{"id": "a1", "name": "notes.md", "size": 5, "type": "text/markdown", "content": "alpha"}]
    )

    assert attachments[0]["content"] == "alpha"
    assert attachment_metadata(attachments) == [
        {"id": "a1", "name": "notes.md", "size": 5, "type": "text/markdown"}
    ]


def test_attachment_contract_normalizes_generic_text_metadata_and_untrusted_names():
    from ecosystem.search_home_pack.domain.attachment_contract import normalize_attachments

    attachment = normalize_attachments(
        [
            {
                "id": "unsafe id\nvalue",
                "name": "../`notes`.txt",
                "size": 5,
                "type": "application/octet-stream",
                "content": "alpha",
            }
        ]
    )[0]

    assert attachment["id"] == "unsafe-id-value"
    assert attachment["name"] == "'notes'.txt"
    assert attachment["type"] == "text/plain"


def test_attachment_contract_accepts_exact_image_payload_and_rejects_mismatches():
    from ecosystem.search_home_pack.domain.attachment_contract import normalize_attachments

    png = b"\x89PNG\r\n\x1a\n"
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    assert (
        normalize_attachments(
            [
                {
                    "id": "image",
                    "name": "pixel.png",
                    "size": len(png),
                    "type": "image/png",
                    "dataUrl": data_url,
                }
            ]
        )[0]["dataUrl"]
        == data_url
    )

    with pytest.raises(ValueError, match="exact size"):
        normalize_attachments(
            [
                {
                    "id": "image",
                    "name": "pixel.png",
                    "size": len(png) - 1,
                    "type": "image/png",
                    "dataUrl": data_url,
                }
            ]
        )
    with pytest.raises(ValueError, match="declared type"):
        normalize_attachments(
            [
                {
                    "id": "image",
                    "name": "pixel.png",
                    "size": len(png),
                    "type": "image/png",
                    "dataUrl": "data:image/jpeg;base64,cG5n",
                }
            ]
        )
    with pytest.raises(ValueError, match="bytes do not match"):
        normalize_attachments(
            [
                {
                    "id": "image",
                    "name": "pixel.png",
                    "size": 3,
                    "type": "image/png",
                    "dataUrl": "data:image/png;base64,cG5n",
                }
            ]
        )
    with pytest.raises(ValueError, match="extension"):
        normalize_attachments(
            [
                {
                    "id": "image",
                    "name": "pixel.jpg",
                    "size": len(png),
                    "type": "image/png",
                    "dataUrl": data_url,
                }
            ]
        )


def test_attachment_contract_rejects_unsupported_or_multiple_files():
    from ecosystem.search_home_pack.domain.attachment_contract import normalize_attachments

    with pytest.raises(ValueError, match="one attachment"):
        normalize_attachments([{}, {}])
    with pytest.raises(ValueError, match="unsupported"):
        normalize_attachments(
            [
                {
                    "id": "zip",
                    "name": "archive.zip",
                    "size": 3,
                    "type": "application/zip",
                    "content": "zip",
                }
            ]
        )
    with pytest.raises(ValueError, match="exact UTF-8 size"):
        normalize_attachments(
            [
                {
                    "id": "text",
                    "name": "notes.txt",
                    "size": 1,
                    "type": "text/plain",
                    "content": "alpha",
                }
            ]
        )
    with pytest.raises(ValueError, match="unsupported"):
        normalize_attachments(
            [
                {
                    "id": "text",
                    "name": "notes.txt",
                    "size": 5,
                    "type": "application/zip",
                    "content": "alpha",
                }
            ]
        )


def test_search_home_http_attachment_reaches_answer_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ecosystem.search_home_pack import desktop_app
    from ecosystem.search_home_pack.domain import defaultspack_bridge

    calls: list[dict[str, object]] = []

    class FakeBridge:
        def answer_query(self, query: str, **options: object) -> dict[str, object]:
            calls.append({"query": query, **options})
            return {"status": "ok", "answer": "The attachment says alpha."}

    monkeypatch.setattr(defaultspack_bridge, "DefaultspackBridge", FakeBridge)
    handler = desktop_app._make_handler(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    attachment = {
        "id": "answer-context",
        "name": "notes.txt",
        "size": 5,
        "type": "text/plain",
        "content": "alpha",
    }
    request_body = json.dumps(
        {
            "input": "What does the attachment say?",
            "model": "demo/model",
            "use_search": True,
            "attachments": [attachment],
        }
    )
    operation = quote("POST /api/answer", safe="")
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request(
            "POST",
            f"/api/contracts/search_home_pack/{operation}",
            body=request_body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    assert response.status == 200
    assert payload["answer"] == "The attachment says alpha."
    assert calls == [
        {
            "query": "What does the attachment say?",
            "model_ref_override": "demo/model",
            "use_search": True,
            "attachments": [attachment],
            "context": {"source": "search_home.answer"},
        }
    ]
