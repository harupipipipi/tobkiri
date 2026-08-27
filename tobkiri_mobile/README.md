# Rumi Mobile

Rumi Mobile is the Flutter client for Rumi. It ships a ChatGPT-style chat UI
that runs **on-device** against any OpenAI-compatible endpoint, plus QR-based
PC pairing.

The app lives under the defaultspack ecosystem folder so it sits next to the
canonical control panel at `rumi_ai_1_10/ecosystem/defaultspack/`.

## Features

- Rich ChatGPT-like chat UI: new chat, conversation list, pin/rename/delete,
  markdown rendering, streaming responses, typing indicator.
- Smartphone-local: conversations and API config are stored on-device
  (`shared_preferences` for history, `flutter_secure_storage` for keys).
  No server is required to chat.
  See [Mobile conversation persistence](CONVERSATION_PERSISTENCE.md) for the
  versioned snapshot, rollback, recovery, and diagnostics contract.
- OpenAI-compatible streaming client (`/chat/completions` SSE).
- QR import:
  - **スマホをペアリング**: scan a `rumi_mobile_pair_v1` QR to claim a PC
    pairing session and pick up scoped split mobile tokens after PC approval.
- PC pairing from the defaultspack control panel: open **Settings → アプリ** on
  the Mac to start **スマホをペアリング** or display a Cloudflare Pages URL QR.

## PC chat controls

When the phone is connected to a paired PC space, the chat header shows the
active PC model next to the selected space. The composer has a left-side **+**
menu for PC controls:

- choose any selectable PC model/profile reported by the PC capabilities
  endpoint;
- toggle PC turn options such as DeepThink/thinking levels when the selected
  model supports them;
- run PC slash commands from the menu instead of typing them manually.

The PC command list is not hard-coded in the mobile app. Mobile reads the
paired PC's capabilities/catalog response, including command manifest entries,
and posts selected commands to `/api/mobile/v1/commands/execute`. New public PC
slash commands should therefore appear in the **+** menu after the PC exposes
them through its command manifest and capabilities response.

Typing slash commands still works. For discoverability, `/` commands should
also be reachable from the **+** menu. Local on-device chat has its own model
selection from the same menu, backed by the stored API/model config.

## QR payload format

The PC side (defaultspack webapp **Settings → アプリ**) emits JSON QR codes:

```jsonc
// Recommended PC pairing QR (kind=rumi_mobile_pair_v1)
{
  "kind": "rumi_mobile_pair_v1",
  "version": 1,
  "pairingId": "pair-...",
  "code": "ABCD-2345",
  "pickupSecret": "pup_...",
  "baseUrls": ["https://rumi.example.com"],
  "manifestUrl": "https://rumi.example.com/api/mobile/v1/manifest",
  "roles": ["mobile_client", "mobile_approver"],
  "serverPublicKey": "",
  "expiresAt": 1781830000000
}

Rumi Remote Mobile is the Flutter client for managing a PC-hosted Rumi
`defaultspack` from iOS and Android devices on a trusted network.

The app targets the Kernel Pack API on port `8765`, not the standalone
defaultspack chat transport on port `8766`. The Kernel API requires a bearer
token and is the safer surface for LAN access.

## PC Setup

Start Rumi with the Kernel API bound to the trusted LAN:

```powershell
$env:RUMI_API_BIND_ADDRESS="0.0.0.0"
python -m rumi_ai
```

Read the active API token from `tobkiri_runtime/user_data/hmac_keys.json` or run:

```powershell
cd tobkiri_runtime
python -c "from core_runtime.hmac_key_manager import HMACKeyManager; print(HMACKeyManager().get_active_key())"
```

The mobile parser falls back to `QrUrl` for plain `http(s)://` links (e.g.
Cloudflare Pages URLs).

## PC pairing developer/tester checklist

1. Start Rumi with the Kernel API bound to a trusted LAN address. Use this only
   on a private network that the test phone can reach:

   ```bash
   cd rumi_ai_1_10
   RUMI_API_BIND_ADDRESS=0.0.0.0 python app.py
   ```

2. On the Mac/PC control panel, open **Settings → アプリ** and use
   **スマホをペアリング**. Release Android builds require HTTPS for PC pairing,
   so enter a Cloudflare Tunnel/reverse-proxy origin such as
   `https://rumi.example.com`. Debug/profile builds can opt into LAN HTTP with
   `RUMI_MOBILE_ALLOW_CLEARTEXT_QR=1` for local testing.
3. Scan the `rumi_mobile_pair_v1` QR from the mobile app. The app claims
   `/api/mobile/v1/pairings/{id}/claim` with the QR code, the mobile
   `device_id`, label, public key, and requested scopes.
4. Approve the claimed device in the control panel. Mobile polls
   `GET /api/mobile/v1/pairings/{id}/status` without secrets, then retrieves
   the encrypted token envelope with `POST /api/mobile/v1/pairings/{id}/token/pickup`
   using `pickup_secret` and `device_id` in the JSON body.
5. Confirm the paired phone can load PC chat state, create/send a test message,
   and still cannot call non-mobile API routes with the `dtk_...` token.

The legacy **PC接続QR** flow is still useful for compatibility checks, but it
places a full bearer token in the QR payload. Prefer `rumi_mobile_pair_v1` for
normal pairing tests.

## Security contracts from PR #364

- `dtk_...` client tokens are mobile-only. The backend accepts them only on
  `/api/mobile/v1/...` routes, and only when the route declares a matching
  device scope such as `chat.read`, `chat.write`, or `tools.observe`.
- `dtk_...` approver tokens are separate and accepted only on
  `/api/authority/*` request list/read/challenge/approve/deny routes.
- Pairing status is observable by the PC, but token pickup is a separate POST
  body request that requires the QR-only pickup secret and the claimed
  `device_id`. Status responses must not include token material. Token delivery is an encrypted
  X25519/AES-GCM envelope, and pickup is consumed only after mobile decrypts,
  stores, and acknowledges the delivery.
- Credential transfer is behind `RUMI_MOBILE_CREDENTIAL_TRANSFER=1` until
  encrypted device-bound delivery is complete. Plaintext keys, `api_key`
  fallback fields, and `plaintext`/`base64-wrapper` algorithms are rejected.
- Do not paste real provider keys, bearer tokens, or device tokens into docs,
  screenshots, issues, or test fixtures. Use redacted placeholders.

## Real-device install and LAN HTTP caveats

- TestFlight and App Store builds are not published yet. For a real iPhone,
  install from macOS with Xcode/Flutter and a valid signing team. The
  `flutter build ios --no-codesign` command is for unsigned build verification;
  use Xcode signing to run on a physical device.
- iPhone and PC must be on the same reachable network path. Disable guest Wi-Fi
  isolation/VPNs for LAN debug tests, allow the iOS Local Network prompt, and
  allow the Rumi port through the host firewall when using direct LAN access.
- iOS declares `NSLocalNetworkUsageDescription` and `NSAllowsLocalNetworking`
  for private-network debug paths. Prefer HTTPS for release-like pairing.
- Android declares internet/network/camera permissions. Debug/profile builds
  permit cleartext HTTP for LAN testing; release builds use the platform HTTPS
  default and will not pair over `http://192.168.x.x:8765`.
- For off-LAN smoke tests, expose the local Kernel API with Cloudflare Tunnel:

  ```bash
  cloudflared tunnel --protocol http2 --url http://127.0.0.1:8765
  ```

  Use the printed `https://...trycloudflare.com` origin in the `rumi_mobile_pair_v1`
  QR. Cloudflare Pages can host docs/install pages, but it does not expose a
  local Mac/PC Kernel API by itself.

## Distribution status

TestFlight and App Store builds are **coming soon**. The **Settings → アプリ**
panel in the defaultspack control panel reflects this state.

The Kernel also defines authenticated `/api/mobile/v1/conversations` routes
for future chat UI work. A mobile chat surface must treat their durable success
response as the save acknowledgement and must not restore the retired local
`ChatStore` keys.

## Development

```powershell
cd tobkiri_mobile
flutter pub get
flutter analyze
flutter test
```

Android debug builds require a Flutter/Android SDK environment:

```bash
flutter build apk --debug
```

iOS builds require macOS and Xcode:

```bash
flutter build ios --no-codesign
```

Camera permission is requested for QR scanning (`NSCameraUsageDescription` on
iOS, `android.permission.CAMERA` on Android).
