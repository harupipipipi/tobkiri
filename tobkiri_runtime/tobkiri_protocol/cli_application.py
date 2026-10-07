"""Public structured CLI frontend over the normal authenticated Contract HTTP port."""

from __future__ import annotations

from http.cookiejar import CookieJar
import json
import re
from typing import Any, Mapping, TextIO
import urllib.error
import urllib.parse
import urllib.request
import uuid

from jsonschema import Draft202012Validator

from .canonical import canonical_json, strict_loads
from .validation import validate_document

_LIMIT = 1024 * 1024
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class CliApplicationError(RuntimeError):
    """A bounded CLI frontend/session failure, never an authority grant."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise CliApplicationError("Host frontend redirect denied")


class PanelContractSession:
    """Use an explicitly supplied Host-issued bootstrap; no ambient credentials.

    The Host must select/approve the Profile and issue the Shell bootstrap using
    its normal ceremony. This client cannot mint approval, select live bindings,
    use debug credentials or create a fallback execution path.
    """

    def __init__(self, endpoint: str, bootstrap_code: str) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or not parsed.port
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise CliApplicationError("CLI requires an explicit loopback Host endpoint")
        if not isinstance(bootstrap_code, str) or not 1 <= len(bootstrap_code) <= 512:
            raise CliApplicationError("Host bootstrap code is required")
        self._base = endpoint.rstrip("/")
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPCookieProcessor(CookieJar()),
        )
        envelope = self._request("/api/panel/auth/exchange", {"code": bootstrap_code}, csrf="")
        data = envelope.get("data")
        if (
            envelope.get("success") is not True
            or not isinstance(data, Mapping)
            or not isinstance(data.get("csrf_token"), str)
            or not data["csrf_token"]
            or not isinstance(data.get("journal_scope"), str)
            or not data["journal_scope"]
        ):
            raise CliApplicationError("Host frontend bootstrap failed")
        self._csrf = data["csrf_token"]

    def _request(self, path: str, payload: Mapping[str, Any], *, csrf: str) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "X-Tobkiri-Request-ID": str(uuid.uuid4())}
        if csrf:
            headers["X-Rumi-CSRF"] = csrf
        body = canonical_json(dict(payload))
        if len(body) > _LIMIT:
            raise CliApplicationError("CLI input exceeds its byte limit")
        request = urllib.request.Request(self._base + path, body, headers, method="POST")
        try:
            with self._opener.open(request, timeout=10) as response:
                raw = response.read(_LIMIT + 1)
        except (OSError, urllib.error.URLError) as error:
            raise CliApplicationError("Host frontend request denied or unavailable") from error
        if len(raw) > _LIMIT:
            raise CliApplicationError("Host frontend response exceeds its byte limit")
        value = strict_loads(raw)
        if not isinstance(value, dict):
            raise CliApplicationError("Host frontend response is invalid")
        return value

    def invoke(self, declaration: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke one declared POST target; Host capture and Broker remain authoritative."""
        namespace, path = declaration["namespace"], declaration["path"]
        if (
            not isinstance(namespace, str)
            or _ID.fullmatch(namespace) is None
            or not isinstance(path, str)
            or not path.startswith("/api/")
            or any(mark in path for mark in ("..", "//", "\\", "?", "#"))
            or path.startswith("/api/contracts/")
        ):
            raise CliApplicationError("CLI declaration has an invalid route")
        schema = declaration["input_schema"]

        def check_refs(value: Any) -> None:
            if isinstance(value, Mapping):
                for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                    reference = value.get(keyword)
                    if reference is not None and (
                        not isinstance(reference, str) or not reference.startswith("#/")
                    ):
                        raise CliApplicationError("CLI schema references must be local")
                for child in value.values():
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)

        check_refs(schema)
        Draft202012Validator(schema).validate(dict(payload))
        route = "/api/contracts/" + namespace + "/" + urllib.parse.quote("POST " + path, safe="")
        envelope = self._request(route, payload, csrf=self._csrf)
        result = envelope.get("data")
        if (
            envelope.get("success") is not True
            or not isinstance(result, Mapping)
            or result.get("status") != "ok"
            or not isinstance(result.get("value"), dict)
        ):
            raise CliApplicationError("CLI Contract did not return a successful result")
        return result["value"]


def run_application_stdio(
    session: PanelContractSession,
    declarations: Mapping[str, Mapping[str, Any]],
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    """Run a real finite frontend; no Function import, shell execution or approval shortcut."""
    while True:
        line = input_stream.readline(_LIMIT + 1)
        if not line:
            return 0
        request_id = "cli:req:invalid000"
        try:
            if len(line.encode("utf-8")) > _LIMIT:
                raise CliApplicationError("CLI frame exceeds its byte limit")
            request = validate_document(line, "cli_io")
            request_id = request["request_id"]
            if request["type"] != "command" or request.get("command") != "application.invoke":
                raise CliApplicationError("CLI Application command is not declared")
            arguments = request["arguments"]
            if set(arguments) != {"command_id", "input"} or not isinstance(
                arguments["input"], dict
            ):
                raise CliApplicationError("CLI Application arguments are invalid")
            declaration = declarations.get(arguments["command_id"])
            if declaration is None:
                raise CliApplicationError("CLI Application command is unselected")
            if request.get("cancel") or request.get("signal"):
                output = {
                    "stdout": "",
                    "stderr": "request cancelled",
                    "exit_status": 130,
                    "stream": "cancelled",
                }
            else:
                output = session.invoke(declaration, arguments["input"])
            if set(output) != {"stdout", "stderr", "exit_status", "stream"}:
                raise CliApplicationError("CLI renderer output fields are invalid")
            if (
                not isinstance(output["stdout"], str)
                or not isinstance(output["stderr"], str)
                or len((output["stdout"] + output["stderr"]).encode("utf-8"))
                > request["output_limit"]
            ):
                raise CliApplicationError("CLI renderer output exceeds its byte limit")
            frame = {
                "protocol": "io.tobkiri.cli.io.v1",
                "type": "result",
                "request_id": request_id,
                **output,
            }
            validate_document(frame, "cli_io")
        except Exception:
            frame = {
                "protocol": "io.tobkiri.cli.io.v1",
                "type": "error",
                "request_id": request_id,
                "error": "CLI Application request denied or unavailable",
                "exit_status": 64,
            }
        output_stream.write(json.dumps(frame, ensure_ascii=False, sort_keys=True) + "\n")
        output_stream.flush()
