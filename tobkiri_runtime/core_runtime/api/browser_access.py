"""HTTP presentation of Host-owned local browser admission."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from .api_response import APIResponse
from .auth_gate import PANEL_SESSION_COOKIE
from ..browser_access import BrowserAccessError
from ..browser_access_launcher import open_browser_access_window
from ..panel_auth import PanelAuthBinding, PanelAuthManager

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler as _HTTPHandlerBase
else:
    _HTTPHandlerBase = object

BROWSER_ACCESS_PREFIX = "/api/panel/browser-access"
BROWSER_ACCESS_COOKIE = "rumi_browser_access_pending"
_PUBLIC_ACTIONS = frozenset({"request", "status", "claim"})
_NATIVE_ACTIONS = frozenset({"context", "decision"})


def browser_access_document(target: str, *, request_allowed: bool) -> bytes:
    """Render the finite login ceremony without loading any Pack application."""

    target_literal = json.dumps(target).replace("<", "\\u003c")
    allowed_literal = "true" if request_allowed else "false"
    document = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tobkiri · ブラウザアクセス</title>
<style>
:root{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
background:radial-gradient(ellipse at top,#202328,#111215 65%);color:#f5f5f7;padding:24px}
main{width:min(420px,100%);padding:32px;border:1px solid #ffffff16;border-radius:28px;
background:#242529b8;box-shadow:0 24px 80px #0005;text-align:center}
.icon{width:54px;height:54px;margin:0 auto 20px;background:#ffffff0b;border-radius:17px;
display:grid;place-items:center}.icon svg{width:28px;height:28px}h1{font-size:21px;margin:0 0 12px}
p{font-size:14px;line-height:1.7;color:#b9bac3;margin:0 0 20px}
button{border:0;border-radius:14px;background:#f0f0f5;color:#14151a;font:inherit;
font-weight:600;padding:14px 20px;width:100%;cursor:pointer}button:disabled{opacity:.5;cursor:wait}
button:focus-visible{outline:3px solid #9dafff;outline-offset:4px}
#status{min-height:42px;margin:20px 0 0;font-size:13px}small{display:block;color:#737580;
font-size:11px;margin-top:16px;overflow-wrap:anywhere}[hidden]{display:none!important}
@media(prefers-reduced-motion:no-preference){main{animation:appear .25s ease-out}
@keyframes appear{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}}
</style></head><body><main>
<div class="icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
stroke-width="1.6"><rect x="5" y="10" width="14" height="11" rx="3"/>
<path d="M8 10V7a4 4 0 0 1 8 0v3"/><path d="M12 14v3"/></svg></div>
<h1>Launcherの承認が必要です</h1>
<p>このブラウザからTobkiriを開くには、<br>Tobkiri Launcherでアクセスを許可してください。</p>
<button id="request" type="button" hidden>アクセスをリクエスト</button>
<p id="status" role="status" aria-live="polite">Tobkiri Launcher authentication required</p>
<small id="origin"></small>
</main><script>
document.addEventListener('DOMContentLoaded',()=>{
const target=TARGET_LITERAL;
const requestAllowed=ALLOWED_LITERAL;
const button=document.getElementById('request');
const status=document.getElementById('status');
document.getElementById('origin').textContent=location.origin;
let generation=0;
const messages={rate_limited:'リクエストが続いています。少し待ってから再度お試しください。',
queue_full:'別の承認を待っています。Launcherで確認してください。',
launcher_unavailable:'Launcherに接続できません。起動してから再度お試しください。',
invalid_request:'リクエストの期限が切れました。もう一度リクエストしてください。',
not_approved:'Launcherの承認を確認できませんでした。'};
const post=async(action,body)=>{
const response=await fetch('/api/panel/browser-access/'+action,{method:'POST',
credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const envelope=await response.json();
if(!response.ok||!envelope.success)throw new Error(envelope.data?.code||'invalid_request');
return envelope.data;
};
const enter=(data)=>{
if(!data?.csrf_token||!data?.journal_scope)throw new Error('invalid_request');
sessionStorage.setItem('rumi-panel-csrf',data.csrf_token);
sessionStorage.setItem('tobkiri-panel-journal-scope-v1',data.journal_scope);
if(data.target){location.replace(data.target)}else{location.replace(TARGET_LITERAL)}
};
const fail=(error)=>{
button.disabled=false;button.textContent='もう一度リクエスト';
status.textContent=messages[error.message]||'接続を確認できませんでした。再度お試しください。';
};
const poll=async(requestId,token)=>{
if(token!==generation)return;
try{
const data=await post('status',{request_id:requestId});
if(token!==generation)return;
if(data.status==='approved'){
status.textContent='承認されました。Tobkiriを開いています…';
enter(await post('claim',{request_id:requestId}));return;
}
if(data.status==='denied'){
button.disabled=false;button.textContent='もう一度リクエスト';
status.textContent='アクセスは許可されませんでした。';return;
}
status.textContent='Launcherで承認を待っています…';
setTimeout(()=>poll(requestId,token),1000);
}catch(error){if(token===generation)fail(error);}
};
button.addEventListener('click',async()=>{
const token=++generation;button.disabled=true;status.textContent='Launcherにリクエストしています…';
try{const data=await post('request',{target});await poll(data.request_id,token);}
catch(error){if(token===generation)fail(error);}
});
const params=new URL(location.href).searchParams;
const code=params.get('code');
const requestId=params.get('request_id');
if(code){
status.textContent='Tobkiriに接続しています…';
fetch('/api/panel/auth/exchange',{method:'POST',credentials:'same-origin',
headers:{'Content-Type':'application/json'},body:JSON.stringify(requestId?{code,request_id:requestId}:{code})})
.then(r=>{if(!r.ok)throw new Error('authentication failed');return r.json()})
.then(v=>enter(v.data))
.catch(()=>{status.textContent='Tobkiri Launcher authentication failed';button.hidden=!requestAllowed;});
}else if(requestAllowed){button.hidden=false;status.textContent='許可すると、このブラウザでDefaultsを開けます。';}
});
</script></body></html>"""
    return (
        document.replace("TARGET_LITERAL", target_literal)
        .replace("ALLOWED_LITERAL", allowed_literal)
        .encode("utf-8")
    )


class BrowserAccessHTTPMixin(_HTTPHandlerBase):
    """Expose request/claim to the browser and decisions only to the Launcher."""

    _panel_auth_manager: PanelAuthManager | None
    _runtime_port: int
    _raw_body_bytes: bytes
    _browser_access_mac_context: tuple[str, str, str] | None = None

    if TYPE_CHECKING:

        def _current_panel_auth_binding(self) -> PanelAuthBinding | None: ...
        def _get_cors_origin(self, origin: str) -> str: ...
        def _is_loopback_client(self, address: object) -> bool: ...
        def _native_pack_launcher_authenticated(self) -> bool: ...
        def _parse_object_body(self) -> dict[str, object] | None: ...
        def _discard_request_body(self) -> None: ...
        def _parse_cookie_header(self) -> dict[str, str]: ...
        def _build_set_cookie(self, name: str, value: str, **kwargs: object) -> str: ...
        def _send_response(
            self,
            response: APIResponse,
            status: int = 200,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None: ...

    @staticmethod
    def _browser_access_target(target: object, binding: PanelAuthBinding) -> str | None:
        """Limit browser admission to the active Profile's chat entry point."""

        root = "/p/" + quote(binding.profile_id, safe="-._~")
        if isinstance(target, str) and target in {root, root + "/", root + "/chat"}:
            return root + "/chat"
        return None

    def _browser_request_origin(self) -> str:
        """Require exact local Origin and Host, including the runtime port."""

        origin = self.headers.get("Origin", "")
        if (
            not self._is_loopback_client(self.client_address)
            or not self._get_cors_origin(origin)
            or self.headers.get("Host", "") != urlsplit(origin).netloc
            or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
        ):
            return ""
        return origin

    def _browser_access_error(self, code: str, status: int) -> None:
        self._browser_access_response(
            APIResponse(False, data={"code": code}, error="Browser access denied"),
            status,
        )

    def _browser_access_response(
        self,
        response: APIResponse,
        status: int = 200,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        """Authenticate native replies against the exact serialized envelope."""

        headers = [("Cache-Control", "no-store"), *(extra_headers or [])]
        context = self._browser_access_mac_context
        if context is not None and self._panel_auth_manager is not None:
            timestamp, nonce, path = context
            proof = self._panel_auth_manager.browser_access_native_auth.response_proof(
                timestamp, nonce, path, status, response.to_json().encode("utf-8")
            )
            headers.append(("X-Tobkiri-Browser-Access-Response-Proof", proof))
        self._send_response(response, status, extra_headers=headers)

    def _handle_browser_access(self, path: str) -> bool:
        """Handle only the five exact routes, preserving all other auth gates."""

        action = path.removeprefix(BROWSER_ACCESS_PREFIX + "/")
        if (
            not path.startswith(BROWSER_ACCESS_PREFIX + "/")
            or action not in _PUBLIC_ACTIONS | _NATIVE_ACTIONS
        ):
            return False
        self._browser_access_mac_context = None
        native = action in _NATIVE_ACTIONS
        origin = "" if native else self._browser_request_origin()
        if (
            native
            and (
                not self._is_loopback_client(self.client_address)
                or self._panel_auth_manager is None
                or not self.headers.get("X-Tobkiri-Browser-Access-Proof", "")
            )
        ) or (not native and not origin):
            self.close_connection = True
            self._browser_access_error("unauthorized", 401 if native else 403)
            return True
        # These endpoints accept tiny finite objects; generic Pack payload limits
        # are intentionally not used as their admission budget.
        content_length = self.headers.get("Content-Length", "")
        if (
            not content_length.isascii()
            or not content_length.isdecimal()
            or not 0 < int(content_length) <= 4096
            or self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            != "application/json"
        ):
            self.close_connection = True
            self._browser_access_error("invalid_request", 400)
            return True
        body = self._parse_object_body()
        if body is None:
            return True
        if native:
            timestamp = self.headers.get("X-Tobkiri-Browser-Access-Timestamp", "")
            nonce = self.headers.get("X-Tobkiri-Browser-Access-Nonce", "")
            proof_header = self.headers.get("X-Tobkiri-Browser-Access-Proof", "")
            assert self._panel_auth_manager is not None
            if not self._panel_auth_manager.browser_access_native_auth.verify_request(
                timestamp, nonce, proof_header, path, self._raw_body_bytes
            ):
                self._browser_access_error("unauthorized", 401)
                return True
            self._browser_access_mac_context = (timestamp, nonce, path)
        expected_keys = (
            {"target"}
            if action == "request"
            else {"request_id", "decision"} if action == "decision" else {"request_id"}
        )
        if set(body) != expected_keys:
            self._browser_access_error("invalid_request", 400)
            return True
        binding = self._current_panel_auth_binding()
        panel_auth = self._panel_auth_manager
        if binding is None or panel_auth is None:
            self._browser_access_error("invalid_request", 409)
            return True
        manager = panel_auth.browser_access
        proof = self._parse_cookie_header().get(BROWSER_ACCESS_COOKIE, "")
        response_headers: list[tuple[str, str]] = []
        try:
            if action == "request":
                target = self._browser_access_target(body["target"], binding)
                if target is None:
                    raise BrowserAccessError("invalid_request")
                result = manager.create(binding, origin, target, existing_proof=proof)
                request_id = str(result["request_id"])
                if result.get("created", True):
                    try:
                        open_browser_access_window(
                            binding, request_id, self._runtime_port
                        )
                    except RuntimeError:
                        manager.decide(request_id, binding, "denied")
                        self._browser_access_error("launcher_unavailable", 503)
                        return True
                response_headers.append(
                    (
                        "Set-Cookie",
                        self._build_set_cookie(
                            BROWSER_ACCESS_COOKIE,
                            str(result["proof"]),
                            path=BROWSER_ACCESS_PREFIX,
                            max_age=int(result["expires_in"]),
                            http_only=True,
                        ),
                    )
                )
                data = {
                    key: value
                    for key, value in result.items()
                    if key not in {"proof", "created"}
                }
            else:
                request_id = body["request_id"]
                if not isinstance(request_id, str) or len(request_id) > 64:
                    raise BrowserAccessError("invalid_request")
                if action == "context":
                    data = manager.context(request_id, binding)
                elif action == "decision":
                    decision = body["decision"]
                    if not isinstance(decision, str):
                        raise BrowserAccessError("invalid_request")
                    data = manager.decide(request_id, binding, decision)
                elif action == "status":
                    data = manager.status(request_id, proof, binding, origin)
                else:
                    result = manager.claim(request_id, proof, binding, origin)
                    response_headers.extend(
                        [
                            (
                                "Set-Cookie",
                                self._build_set_cookie(
                                    PANEL_SESSION_COOKIE,
                                    str(result["session_id"]),
                                    path="/",
                                    max_age=int(result["expires_in"]),
                                    http_only=True,
                                ),
                            ),
                            (
                                "Set-Cookie",
                                self._build_set_cookie(
                                    BROWSER_ACCESS_COOKIE,
                                    "",
                                    path=BROWSER_ACCESS_PREFIX,
                                    max_age=0,
                                    http_only=True,
                                ),
                            ),
                        ]
                    )
                    data = {
                        key: result[key]
                        for key in (
                            "csrf_token",
                            "expires_in",
                            "journal_scope",
                            "target",
                        )
                    }
        except BrowserAccessError as error:
            status = 429 if error.code in {"rate_limited", "queue_full"} else 409
            self._browser_access_error(error.code, status)
            return True
        self._browser_access_response(
            APIResponse(True, data=data), extra_headers=response_headers
        )
        return True
