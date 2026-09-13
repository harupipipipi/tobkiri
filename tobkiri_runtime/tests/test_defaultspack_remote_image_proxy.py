from __future__ import annotations

import io
import struct
import urllib.error
from email.message import Message

import pytest
import ecosystem.defaultspack.domain.media.remote_image_proxy as proxy_module

from ecosystem.defaultspack.domain.media.remote_image_proxy import (
    MAX_ACTIVE_CONSENTS,
    MAX_BYTES,
    RemoteImageError,
    RemoteImageProxy,
    validate_remote_url,
)


def PUBLIC(host):
    return ["93.184.216.34"]


def test_remote_image_proxy_requires_captured_scoped_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/remote-images/consents",
        "tobkiri.remote-image.v1",
        "defaultspack.remote-image.consent",
    )


def test_default_transport_pins_validated_ip_and_preserves_tls_host(monkeypatch):
    captured = {}

    class FakeConnection:
        _address = "93.184.216.34"

        def __init__(self, host, address, timeout):
            captured.update(host=host, address=address, timeout=timeout)

        def request(self, method, path, headers):
            captured.update(method=method, path=path, headers=headers)

        def getresponse(self):
            return FakeHTTPResponse()

        def close(self):
            pass

    class FakeHTTPResponse(io.BytesIO):
        status = 200
        reason = "OK"

        def __init__(self):
            super().__init__(png())
            self.headers = Message()
            self.headers["Content-Type"] = "image/png"

    monkeypatch.setattr(proxy_module, "_PinnedHTTPSConnection", FakeConnection)
    opener = proxy_module._PinnedOpener(lambda host: ["93.184.216.34"])
    request = proxy_module.urllib.request.Request("https://images.example/a.png?q=1")
    with opener.open(request, 7.0) as response:
        assert response.read() == png()
    assert captured == {
        "host": "images.example",
        "address": "93.184.216.34",
        "timeout": 7.0,
        "method": "GET",
        "path": "/a.png?q=1",
        "headers": {"Host": "images.example"},
    }


def png(width=1, height=1):
    return b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", width, height) + b"\0" * 8


class Response(io.BytesIO):
    def __init__(self, body, url="https://images.example/a.png", mime="image/png", length=None, peer_ip="93.184.216.34"):
        super().__init__(body)
        self._url = url
        self.peer_ip = peer_ip
        self.headers = Message()
        self.headers["Content-Type"] = mime
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.parametrize("url", [
    "http://example.com/a.png", "file:///tmp/a.png", "javascript:alert(1)",
    "data:image/png;base64,AA==", "https://u:p@example.com/a.png",
    "https://example.com:444/a.png", "https://localhost/a.png", "https://x.local/a.png",
])
def test_rejects_unsafe_url_forms(url):
    with pytest.raises(RemoteImageError):
        validate_remote_url(url, PUBLIC)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254",
    "0.0.0.0", "::1", "fc00::1", "fe80::1", "224.0.0.1",
])
def test_rejects_private_local_reserved_and_multicast_dns(address):
    with pytest.raises(RemoteImageError, match="Private"):
        validate_remote_url("https://images.example/a.png", lambda host: [address])


def test_mixed_public_private_dns_fails_closed():
    with pytest.raises(RemoteImageError):
        validate_remote_url("https://images.example/a.png", lambda host: ["93.184.216.34", "127.0.0.1"])


def test_consent_fetch_cache_revoke_and_no_sensitive_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    opener = Opener([Response(png())])
    proxy = RemoteImageProxy(resolver=PUBLIC, opener=opener)
    created = proxy.create("https://images.example/a.png#tracking")
    token = created["token"]
    assert proxy.fetch(token) == (png(), "image/png")
    assert proxy.fetch(token) == (png(), "image/png")
    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    assert timeout == 10.0
    lowered = {key.lower() for key in request.headers}
    assert not lowered.intersection({"cookie", "authorization", "referer"})
    proxy.revoke(token)
    with pytest.raises(RemoteImageError, match="revoked"):
        proxy.fetch(token)
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "images.example" not in audit
    assert token not in audit


def test_expired_token_never_fetches():
    now = [10.0]
    opener = Opener([Response(png())])
    proxy = RemoteImageProxy(resolver=PUBLIC, opener=opener, clock=lambda: now[0])
    token = proxy.create("https://images.example/a.png")["token"]
    now[0] += 301
    with pytest.raises(RemoteImageError, match="expired"):
        proxy.fetch(token)
    assert opener.requests == []


def test_consent_store_is_bounded_and_prunes_expired_or_revoked_entries():
    now = [10.0]
    proxy = RemoteImageProxy(resolver=PUBLIC, clock=lambda: now[0])
    tokens = [
        proxy.create(f"https://images.example/{index}.png")["token"]
        for index in range(MAX_ACTIVE_CONSENTS)
    ]
    with pytest.raises(RemoteImageError) as caught:
        proxy.create("https://images.example/overflow.png")
    assert caught.value.code == "CONSENT_LIMIT"

    proxy.revoke(tokens[0])
    assert proxy.create("https://images.example/revoked-slot.png")["token"]

    now[0] += 301
    assert proxy.create("https://images.example/expired-slots.png")["token"]
    assert len(proxy._consents) == 1


@pytest.mark.parametrize("body,mime,code", [
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "MIME_BLOCKED"),
    (b"<script>alert(1)</script>", "image/png", "UNSAFE_IMAGE_TYPE"),
    (png(), "image/jpeg", "MIME_MISMATCH"),
    (png(10000, 10000), "image/png", "PIXEL_LIMIT"),
])
def test_blocks_svg_active_content_mime_confusion_and_pixel_bombs(body, mime, code):
    proxy = RemoteImageProxy(resolver=PUBLIC, opener=Opener([Response(body, mime=mime)]))
    token = proxy.create("https://images.example/a.png")["token"]
    with pytest.raises(RemoteImageError) as caught:
        proxy.fetch(token)
    assert caught.value.code == code


def test_blocks_declared_and_streamed_oversize():
    for response in [Response(png(), length=MAX_BYTES + 1), Response(b"x" * (MAX_BYTES + 1))]:
        proxy = RemoteImageProxy(resolver=PUBLIC, opener=Opener([response]))
        token = proxy.create("https://images.example/a.png")["token"]
        with pytest.raises(RemoteImageError) as caught:
            proxy.fetch(token)
        assert caught.value.code == "IMAGE_TOO_LARGE"


def test_redirect_target_is_revalidated_and_private_target_blocked():
    headers = Message()
    headers["Location"] = "https://internal.example/a.png"
    redirect = urllib.error.HTTPError("https://images.example/a.png", 302, "", headers, None)
    def resolver(host):
        return ["127.0.0.1"] if host == "internal.example" else ["93.184.216.34"]
    proxy = RemoteImageProxy(resolver=resolver, opener=Opener([redirect]))
    token = proxy.create("https://images.example/a.png")["token"]
    with pytest.raises(RemoteImageError) as caught:
        proxy.fetch(token)
    assert caught.value.code == "PRIVATE_NETWORK_BLOCKED"


def test_rebinding_on_second_resolution_fails_closed_before_request():
    calls = [0]
    def resolver(host):
        calls[0] += 1
        return ["93.184.216.34"] if calls[0] == 1 else ["127.0.0.1"]
    opener = Opener([Response(png())])
    proxy = RemoteImageProxy(resolver=resolver, opener=opener)
    token = proxy.create("https://images.example/a.png")["token"]
    with pytest.raises(RemoteImageError):
        proxy.fetch(token)
    assert opener.requests == []


def test_rebinding_after_validation_is_blocked_by_connected_peer_check():
    opener = Opener([Response(png(), peer_ip="127.0.0.1")])
    proxy = RemoteImageProxy(resolver=PUBLIC, opener=opener)
    token = proxy.create("https://images.example/a.png")["token"]
    with pytest.raises(RemoteImageError) as caught:
        proxy.fetch(token)
    assert caught.value.code == "PRIVATE_NETWORK_BLOCKED"
