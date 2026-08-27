# Rumi Browser Companion

`Rumi Browser Companion` is a Manifest V3 Chromium extension that lets Rumi drive the user's real browser session through a local bridge. It is designed to complement the existing `browser_use` and `computer_use` tools:

- `computer_use` / `browser_computer`: visible-window, computer-use style control
- `browser_companion`: DOM-aware browser control inside the user's signed-in browser profile

This gives Rumi a "computer use + browser use" path where the model can inspect DOM state, select between connected browsers, and operate with the user's live cookies and sessions.

## Files

- `manifest.json`: extension manifest
- `background.js`: bridge polling, browser metadata, tab operations, capture orchestration
- `content_script.js`: DOM snapshots and element-level actions
- `options.html`, `options.css`, `options.js`: local bridge configuration UI

## Motion accessibility

The extension options UI does not use motion to communicate state. Its CSS
also honors `prefers-reduced-motion: reduce`, making any future animation,
transition, or smooth scrolling instant while preserving the same visible
labels, status text, focus treatment, and actions.

## Install

1. Open a Chromium-based browser such as Chrome, Edge, Brave, or Vivaldi.
2. Open the browser's extensions page and enable developer mode.
3. Choose "Load unpacked" and select this folder:

   `<repo>/tobkiri_runtime/ecosystem/defaultspack/browser_extensions/rumi_browser_companion`

4. In Rumi, call `browser_companion` with `action: "bridge.pairing"` to get the pairing token and candidate server URLs.
5. Open the extension options page and paste:

   - `Server URL` such as `http://127.0.0.1:8766`
   - `Pairing Token`
   - Optional `Profile Label`, for example `Work` or `Personal`

6. Click `Poll Bridge Now` to confirm the extension can connect.

## Bridge API

The extension talks to these local endpoints:

- `POST {serverUrl}/api/tools/browser-companion/bridge/poll`
- `POST {serverUrl}/api/tools/browser-companion/bridge/result`

`poll` request body:

```json
{
  "pairing_token": "example-token",
  "client": {
    "client_id": "uuid",
    "label": "My Edge Companion",
    "client_label": "My Edge Companion",
    "browser_profile_id": "profile-uuid",
    "profile_label": "Work",
    "installation_id": "install-uuid",
    "client_profile": {
      "browser_profile_id": "profile-uuid",
      "profile_label": "Work",
      "installation_id": "install-uuid",
      "extension_id": "extension-id",
      "browser_name": "Microsoft Edge",
      "browser_version": "136.0.0.0"
    },
    "browser_name": "Microsoft Edge",
    "browser_version": "136.0.0.0",
    "extension_version": "0.1.0",
    "platform": "Win32",
    "user_agent": "...",
    "active_tab_id": 123,
    "capabilities": {
      "multi_browser": true,
      "user_session_cookies": true,
      "semantic_dom": true,
      "semantic_targeting": [
        "element_id",
        "selector",
        "text",
        "text_query",
        "accessible_name",
        "role",
        "semantic_id",
        "nearby_text"
      ]
    },
    "tabs": [
      {
        "id": 123,
        "windowId": 1,
        "active": true,
        "title": "Example",
        "url": "https://example.com",
        "status": "complete"
      }
    ]
  }
}
```

`poll` response body:

```json
{
  "status": "ok",
  "data": {
    "accepted": true,
    "client_id": "uuid",
    "command": {
      "command_id": "cmd_123",
      "action": "page.snapshot",
      "payload": {
        "tab_id": 123,
        "include_capture": true,
        "limit": 200
      }
    }
  }
}
```

`result` request body:

```json
{
  "pairing_token": "example-token",
  "client_id": "uuid",
  "results": [
    {
      "command_id": "cmd_123",
      "ok": true,
      "browser_profile_id": "profile-uuid",
      "profile_label": "Work",
      "installation_id": "install-uuid",
      "client_profile": {
        "browser_profile_id": "profile-uuid",
        "profile_label": "Work",
        "installation_id": "install-uuid",
        "extension_id": "extension-id",
        "browser_name": "Microsoft Edge",
        "browser_version": "136.0.0.0"
      },
      "elements": [
        {
          "element_id": "rumi-el-...",
          "semantic_id": "button:button:submit",
          "accessible_name": "Submit"
        }
      ],
      "result": {
        "snapshot": {
          "schema_id": "rumi.browser.semantic_dom_v2",
          "schema_version": "semantic_dom_v2",
          "url": "https://example.com",
          "title": "Example",
          "snapshot_metadata": {
            "source": "rumi_browser_companion",
            "browser_profile_id": "profile-uuid",
            "profile_label": "Work",
            "installation_id": "install-uuid"
          },
          "client_profile": {
            "browser_profile_id": "profile-uuid",
            "profile_label": "Work",
            "installation_id": "install-uuid"
          },
          "browser_profile_id": "profile-uuid",
          "profile_label": "Work",
          "installation_id": "install-uuid",
          "elements": [
            {
              "element_id": "rumi-el-...",
              "semantic_id": "button:button:submit",
              "accessible_name": "Submit"
            }
          ],
          "nodes": [
            {
              "element_id": "rumi-el-...",
              "semantic_id": "button:button:submit",
              "accessible_name": "Submit",
              "labels": ["Submit"],
              "nearby_text": "Contact form",
              "viewport_center": {"x": 540, "y": 320},
              "page_rect": {"x": 500, "y": 300, "width": 80, "height": 40},
              "page_center": {"x": 540, "y": 320},
              "action_hints": ["extract", "click", "press"],
              "selector_hint": "button.primary",
              "selector_hints": ["#submit", "button.primary"],
              "xpath_hint": "/html[1]/body[1]/button[1]"
            }
          ]
        }
      }
    }
  ]
}
```

## Supported Actions

- `browser.tabs`
- `browser.select_tab`
- `page.navigate`
- `page.snapshot`
- `page.capture`
- `page.click`
- `page.type`
- `page.press`
- `page.scroll`
- `page.extract`
- `page.highlight`
- `page.clear_highlight`

Element actions can target a tab by `tab_id` and an element by `element_id`, `selector`, or `selectors`. For semantic targeting, the extension also accepts:

- `text` or `text_query`
- `accessible_name`
- `role`
- `semantic_id`
- `nearby_text`

For `page.extract` and `page.highlight`, text and accessibility matching prefers direct, smaller semantic elements over broad containers such as `html`, `body`, or large parent `div` elements.

## Safety Notes

- This extension can inspect and act on pages in the user's real browser profile.
- Only pair it with a local Rumi server you control.
- Do not share the pairing token.
- The pairing token is stored in browser-local extension storage and is not synced between browser profiles.
- Capture and tab selection may foreground the browser tab.
- DOM actions are best-effort and may not work on all pages.

## Notes

- The extension uses the user's real browser profile, so authenticated pages work with the user's existing cookies and sessions.
- DOM snapshots and element actions can target tabs that already have the content script loaded.
- Visible tab capture still depends on the browser's active visible tab, so capture requests may activate the target tab.
