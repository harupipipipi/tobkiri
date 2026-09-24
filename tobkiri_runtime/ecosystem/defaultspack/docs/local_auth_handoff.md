# Defaultspack local UI authentication

Defaultspack UI windows must never receive the reusable desktop API token in a URL,
fragment, Web Storage entry, route state, clipboard value, or diagnostic payload.

Tobkiri Launcher authenticates a Defaultspack webview through a native command. The
command validates the actual Tauri window label and reserved loopback origin, then uses
the reusable token only in an authenticated loopback request to issue a 20-second code.
The code is bound to the Launcher process, process-local device identity, actual window,
origin, nonce, fixed `defaultspack-local-ui` scope, and authenticated local subject.

The webview redeems that code once over a same-origin, CSRF-protected endpoint. The
resulting bearer is held only in JavaScript memory and expires after eight hours. Every
request repeats the audience binding in headers; the backend rejects a missing or
mismatched origin, process, device, window, nonce, or scope. Exchange codes and sessions
are stored only as hashes in the Defaultspack process and disappear on restart.

Same-origin child windows request their own exchange from the authenticated opener via
target-origin `postMessage`. The opener issues a separately bound one-time code. Neither
the reusable credential nor the exchange code is added to the child URL.

At startup the web client deletes the legacy `rumi-defaultspack-local-auth` value from
both Web Storage types and removes `rumi_local_auth` from old query strings or fragments
without consuming it. Destination helpers accept only fragment-free absolute paths on
the current HTTP(S) origin and fail closed for parsing errors, protocol-relative values,
credentials, custom schemes, malformed escapes, and legacy auth parameters.
