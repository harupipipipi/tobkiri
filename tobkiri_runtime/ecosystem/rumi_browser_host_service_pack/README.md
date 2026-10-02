# Tobkiri Browser Host Service Pack

Defaults includes a **Browser** workspace for a model-owned Chromium browser.
It keeps its own profiles, cookies and uploaded extensions under
`RUMI_USER_DATA/browser_host/managed`. Start the browser from this workspace,
choose a profile, then open or select a tab. The browser opens in its own window;
the workspace supplies controls and DevTools inspection. Headless mode is also
available.

## Requirements

Install Chrome, Edge or Chromium. The CDP transport dependency is included in
the runtime and installed by Launcher. The Host discovers common installed
locations. For a custom installation, configure
`RUMI_BROWSER_EXECUTABLE` in the Host environment. Browser startup is explicit;
Defaults can still start offline without Chromium. Existing manual runtime
installations should update their dependencies; the `browser-cdp` extra remains
available for compatibility.

The Host launches a dedicated user data directory with a randomly assigned
loopback CDP port. Callers cannot choose an executable, debugging endpoint or
filesystem output path. Stopping the browser validates its profile and debugging
identity before sending `Browser.close`.

## Extensions

Select an unpacked **Manifest V3** extension folder in the Browser workspace.
Review its name, permissions and site access, then install it. AI-created
extensions can also be installed through `browser_managed` by supplying a
`files` map containing `manifest.json` and the extension's source files:

```json
{
  "action": "browser.extensions.install",
  "payload": {
    "files": {
      "manifest.json": "{\"manifest_version\":3,\"name\":\"Page helper\",\"version\":\"1.0\",\"content_scripts\":[{\"matches\":[\"https://example.com/*\"],\"js\":[\"helper.js\"]}]}",
      "helper.js": "document.documentElement.dataset.tobkiriHelper = 'ready';"
    }
  }
}
```

Packages accept UTF-8 text files, up to 256 files, 1 MiB per file and 4 MiB in
total. Paths must remain inside the package; links and Windows reparse points
are rejected. Uploaded packages are retained for the selected profile and
reloaded at its next startup. Binary assets and Chrome Web Store installation
are outside this upload format.

Installation uses Chromium's experimental `Extensions` CDP API, enabled only
for this private browser. Builds lacking that API report an unsupported result;
use a compatible Chrome for Testing or Chromium executable. The workspace can
list and remove uploaded extensions.

## Cookie import

Export cookies from another browser as JSON or a Netscape cookie file, select
that file in the workspace and optionally restrict the imported domains.
Supported JSON shapes include an array of cookies, `{ "cookies": [...] }` and
Playwright storage state. Import maps supported `sameSite`, `httpOnly`, `secure`
and expiration fields to Chromium cookies and applies them to the selected
profile. This does not read encrypted native browser databases or import local
storage. Cookie values are hidden in inspection results.

## DevTools and model tools

The `browser_managed` tool is registered through the canonical local-operation
Contract and dispatches through the same browser observe/control providers as
the workspace. Its actions cover runtime status/start/stop, profiles, live tabs,
navigation, extensions, cookie import, DOM inspection, viewport screenshots,
JavaScript evaluation and network capture. For example:

```json
{
  "action": "browser.network.capture",
  "payload": { "duration_ms": 2000, "max_entries": 200, "reload": true }
}
```

Network capture observes the selected tab for 100–10,000 ms over one persistent
CDP connection. Reload is opt-in. Results contain request URLs, methods, status,
resource types, MIME types, timing/size metadata and console event metadata.
Headers, request/response bodies, console argument values, URL credentials and
query values are omitted or redacted. There is no continuing background capture.
DOM inspection excludes form values; explicit JavaScript evaluation is a control
operation and can read or change page state. The Host can capture a PNG
viewport, but the current canonical model tool transport is text-only:
`browser.capture.page` reports `image_forwarding_supported: false` and returns
metadata without forwarding image bytes to the model.

## Authority and integration

Observation and control remain separate Contracts:
`tobkiri.resource.browser.host.v1` and `tobkiri.action.browser.host.v1`.
Defaults exposes them through authenticated `/api/browser/observe` and
`/api/browser/control` routes. Input has the shape
`{ "operation": "browser.runtime.status", "arguments": {} }`.

The digest-bound Host Provider Factory validates the captured Profile, Plan,
principal, domain, activation, epoch, deadline and cancellation before executing.
Model calls need both the local-executor edge and the nested browser edge.
Client-supplied approval flags or tokens cannot authorize an action. Removing
this Pack removes the browser providers without granting other desktop access.

Legacy browser metadata entrypoints remain available for compatibility, while
the Defaults Browser workspace and `browser_managed` use these canonical paths.
