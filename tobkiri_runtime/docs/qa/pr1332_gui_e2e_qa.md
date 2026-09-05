# PR #1332 GUI / Computer Use E2E QA Plan

対象: `agent/add-pack-architecture-implementation-plan` の最終統合物

この文書は、`f15d97c3` の直前に存在した Launcher UI の操作棚卸しと、PR #1332
の統合後に行う Tobkiri Launcher / defaultspack webapp / mobile client / macOS
artifact の受入手順をまとめたものです。ここでは製品コードを変更せず、GUI 操作と
backend の authoritative state を照合します。

## QA の判定ルール

- 実行対象 SHA、`.app` の絶対パス、SHA-256、OS/architecture、viewport、分離した
  user-data/workspace root を Run ごとに記録する。
- UI に表示された `approved`、enabled、profile 名だけを真実とせず、同じ操作の前後で
  backend の v4 Profile/Pack-control/catalog の値を取得して照合する。
- 対象 Profile は `profile_id`、`profile_revision`、`plan_digest`、`catalog_revision`
  を組にして扱う。defaultspack webapp の model/runtime profile ID は別の概念なので、
  v4 activation Profile と混同しない。
- Pack の install/approve/enable、Profile activation、PackVM の provision はこの QA
  の明示スコープ内の操作だが、実際の artifact の出所・署名・対象 workspace が予期せず
  変わった場合は操作を停止して `BLOCKED` とする。token、cookie、bearer secret、
  authority payload は証拠に保存しない。
- 各ステップの直後に Computer Use の app state/AX tree を再取得する。要素 index は
  使い回さず、毎回最新の AX tree から解決する。
- `PASS` は UI と backend の両方が一致した場合だけ。UI だけ通った場合は `FAIL`、
  artifact や依存環境が無い場合は `BLOCKED` とする。

## Canonical Frontend Contract Map gate

PR #1332 の追加 acceptance gate です。Launcher frontend の恒久的な product read/write
operation は、生成済み
`tobkiri_runtime/ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json` を
artifact manifest の digest で pin した `Frontend Contract Map` から解決されます。認証・
setup・Host-owned PackVM lifecycle・health だけは、下記の狭い allowlist を例外として使えます。

```text
/api/contracts/defaultspack/<url-encoded METHOD /api/...>
```

静的・実行時の両方で、次を満たさない run は `FAIL` とします。

- `frontend_contract_map.v4.json` の raw-byte SHA-256 と application manifest の
  `defaultspack/frontend_contract_map.v4.json` artifact digest が一致し、schema/pack_id が
  `io.tobkiri.frontend-contract-map.v4` / `defaultspack` である。
- map の各 route の `method`, target path, `contribution_id`, `contract_id`,
  `operation_id`, `provider_id`, `function_id`, `allowed_payload_keys` を ledger 化し、
  UI 操作で発生した request が ledger の exact route/target に一致する。
- 現在の captured v4 Profile の provider metadata に、map target と同じ
  `contract_id`, `operation_id`, `provider_id`, `function_id`, `profile_id`,
  `plan_digest` が exactly one つ存在し、resolved plan の
  `function_principal`（function implementation / contract revision / operation）と一致する。
- dynamic Pack contribution は、backend が生成した `enabled=true` かつ approved の
  catalog、current Profile plan、live authority grant、verified PackVM の全てが揃った時だけ
  map target に追加される。ブラウザから `approved: true` を送っても authority と catalog
  の代わりにはならない。
- `/api/runtime-recovery/v4`、旧 panel data/API route、別 Pack の Registry/family fallback、
  raw `fetch`/XHR/`sendBeacon` による未登録 operation は 0 件。allowlist に無い direct
  path、method、query、body schema は FAIL とする。
- Profile activation、restored Profile/Advanced/Settings、Packs、Home、dynamic contribution
  の恒久的な read/mutation は allowlist を fallback として使わず、必ず map → Broker →
  Kernel の exact operation trace を持つ。allowlist endpoint が 404/timeout/stale でも、
  UI が旧 API、Registry、推測 URLへ切り替わってはならない。
- restored Profile/Advanced/Settings の runtime/profile/topology data と activation の
  authoritative source は canonical map → Broker → Kernel に限定する。Tauri command は
  presentation-only の window/menu/native lifecycle または Shell launch 表示に限り、
  Profile/Pack/catalog/topology の真実を返したり、activation/approval/authority mutationを
  実行したりしてはならない。theme、sidebar、viewport など user-local settings は
  frontend state/local storage の範囲に留める。

### Narrow host-boundary allowlist

実行時 trace では method と path を完全一致させ、query は明示された key/値形式以外を
許可しません。表にない `/api/panel`、`/api/setup`、`/api/v4/packvm`、`/health` は
canonical map 外の未許可 route です。

| Method | Exact path / query | 許可目的 | UI fallback か |
| --- | --- | --- | --- |
| POST | `/api/panel/auth/bootstrap` | Host/bootstrap page が one-time panel code を発行 | いいえ。Launcher app surface の product operation ではない |
| POST | `/api/panel/auth/exchange` | Tauri/Panel session の code→CSRF/session exchange | いいえ。認証確立専用 |
| GET | `/api/setup/packs` | Defaults Profile の review/setup state 読み取り | いいえ。activation 前後の setup boundary 専用 |
| POST | `/api/setup/packs/install` | exact Defaults confirmation による Profile activation | いいえ。恒久 Pack control の代替ではない |
| GET | `/api/v4/packvm/doctor` | Host-owned PackVM attestation/health | いいえ。dynamic operation callable 判定の precondition 専用 |
| GET | `/api/v4/packvm/progress?operation_id=<UUID>` | PackVM lifecycle の同一 operation progress | いいえ。query key は `operation_id` 一つだけ |
| POST | `/api/v4/packvm/prepare` | PackVM provisioning plan の取得 | いいえ。map operation の fallback ではない |
| POST | `/api/v4/packvm/consent` | digest-pinned image download/provision consent | いいえ |
| POST | `/api/v4/packvm/provision` | consent 済み PackVM operation の開始 | いいえ |
| POST | `/api/v4/packvm/cancel` | 同一 PackVM operation の cancel | いいえ |
| POST | `/api/v4/packvm/stop` | typed cleanup/stop boundary | いいえ |
| POST | `/api/v4/packvm/cleanup` | typed PackVM cleanup boundary | いいえ |
| GET | `/health` | local Host liveness/readiness probe | いいえ。business data の代替取得ではない |

`GET /api/setup/status`、`GET /api/setup/migration/status` は Host/bootstrap 側の既存
surface として negative/compatibility test では確認してよいが、Launcher frontend の
product callsite としては許可しない。現在の read-only tree で見つかった
`PANEL_AUTH_EXCHANGE_PATH`、setup、PackVM、health callsite はこの表の目的・method/path
に厳密に分類し、Profile/Advanced/Settings の新しい callsiteからは参照されないことを
実行時 trace で証明する。

### Restored surface routing proof

| Surface | 許可される非-map call | それ以外の data/mutation 経路 |
| --- | --- | --- |
| Setup / activation | `GET /api/setup/packs`（review と active-state revalidation）、`POST /api/setup/packs/install`（exact confirmation の activation のみ） | setup response を Packs/Home/Advanced/Settings の一般データに流用しない。失敗時に旧 panel/Registryへ fallback しない |
| Home / Packs / Pack detail | `/health` GET（liveness/readiness）と canonical map→Broker→Kernel | health/setup/PackVM の payload を Pack catalog・dynamic contribution・Profile config の代替にしない |
| PackVM lifecycle panel | exact PackVM allowlist（doctor/prepare/consent/provision/progress/cancel/stop/cleanup） | Pack operation や Profile read の direct endpoint fallback にしない |
| restored Profile / Advanced | runtime/profile/topology data は generated map の exact route のみ。Tauri は window/menu/native lifecycle と Shell launch の presentation-only action のみ | Tauri payload、setup、PackVM、health、legacy API、Registry discovery を Profile/Advanced data の供給元にしない |
| restored Settings | runtime/profile/topology/activation/approval data は canonical map のみ。theme/sidebar/viewport等の user-local settings は frontend state/local storage | Tauri の authority/approval/debug mutation、old `/settings` route/API、panel route、setup/PackVM を fallback にしない |

Tauri の実行証拠には command name、caller surface、purpose、redacted args/result、state
mutation の有無を記録する。`get_presentation_catalog` のように Shell selection を表示する
command がある場合も、profile activation、Pack effective set、runtime topology の authoritative
sourceにせず、backend snapshotと照合する。`debug_approval_status`、`arm_debug_approval`、
`revoke_debug_approval` のような authority/approval command は、復元された Settings の
runtime data/mutation経路として Tauri から呼ばれないことを確認する。Tauri command が
Profile/Pack/catalog/activation を返す、または authority state を変更する場合は
`TAURI-01=FAIL` とする。
- map digest mismatch、map schema/target tamper、stale activation/security epoch、old
  `profile_revision`/`plan_hash`/`catalog_hash` は startup または request の段階で fail closed
  し、legacy route/Registry にフォールバックしない。

### Contract evidence ledger

各 run で次の 4 つを同じ revision の JSON として保存する。

1. generated map raw bytes とその SHA-256。
2. application manifest の map artifact entry（path, kind, digest）と packaged resource の
   SHA-256。
3. captured Profile の `resolved.plan.bindings[*].function_principal`、provider metadata、
   `profile_id`, `profile_revision`, `plan_digest`, activation/security epoch。
4. Computer Use/HTTP request trace の method、canonical URL または allowlisted boundary、
   decoded target、selected contribution、owner Pack、request body key set、response code、
   audit event state、callsite purpose。

期待する canonical route ledger は map に存在する全 routeを採取して作り、静的 scan は
少なくとも以下を検査する。

```bash
# Classify hits against the exact allowlist above; only unallowlisted callsites fail.
rg -n --hidden -g '!node_modules' -g '!target' \
  "(/api/runtime-recovery/v4|/api/panel|/api/setup|/api/v4/packvm|/health|fetch\\(|XMLHttpRequest|sendBeacon|Registry)" \
  tobkiri_launcher/frontend/src

python3 - <<'PY'
import hashlib
from pathlib import Path

path = Path("tobkiri_runtime/ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json")
print("contract_map_sha256=sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
PY
```

`Registry` の文字列だけではなく、source call graph と network trace で「未登録 route を
Registry から補う」挙動が無いことを確認する。allowlist に一致するものは目的別に記録し、
それ以外の direct HTTP を fail とする。runtime 側では
`api.ts` の一つの `fetch` transport wrapper 自体は許可するが、pages/components の direct
`fetch`/XHR は許可しない。wrapper の各 caller path は canonical map または上表の exact
allowlist のどちらかに分類する。
`CONTRACT_MAP_STALE`、`CONTRACT_MAP_INVALID`、`CONTRACT_OPERATION_UNKNOWN`、
`CONTRACT_METHOD_MISMATCH`、`legacy_api_retired` を安定した fail-closed evidence として保存する。

## 監査済みの変更境界

現在の read-only 監査対象は `db1574b9` です。最終統合 SHA は実行時に記録します。

- `f15d97c3^`: Launcher に `workspace` と `advanced` の二つの navigation group が
  あり、`/flows`、`/nodes`、`/graphs`、`/profile-graph`、`/ai-input`、`/api-map`、
  `/profile-workspace`、`/settings` を提供していた。
- `f15d97c3`: 上記の retired Launcher panel route/page/hook/catalog を削除した。
- 現行 Launcher: `/`、`/setup`、`/packs`、`/packs/:id` が BrowserRouter の実装済み
  route で、Sidebar の group は `workspace` の Home/Packs のみ。retired URL は
  `nav.unknown` 相当で fail closed になることを確認する。
- defaultspack webapp の `Advanced Settings` / advanced command は別系統の動的 UI で
  あり、retired Launcher の Advanced navigation と同一視しない。

## `f15d97c3^` のユーザー操作棚卸し

この表は「最終版で必ず復活させる」という意味ではありません。retired になった操作は
削除契約を、同じ責務が dynamic contribution 等へ移された場合は移行先の操作と認可を
検証します。

| 旧 route / 機能 | ユーザー操作 | QA の移行・削除判定 |
| --- | --- | --- |
| `/flows` Flows | flow library を開く、flow を選ぶ/新規作成、Pack filter、step palette から drag/drop または middle-click で node を追加、port を接続、node/edge を移動・削除、undo/redo、YAML/Result を見る、Save/Delete/Execute | 旧 route は表示されず direct URL は unknown/fail closed。置換 contribution がある場合だけ同じ操作・Profile binding・approval を確認 |
| `/nodes` Capability Access / Node Manager | startup Profile を選ぶ、Refresh、Pack を選ぶ、Node を検索・選択、Node または Pack access を Enable/Disable、未承認 Pack の Approve、Node details/domains を確認 | Pack の enable/approve が現行 `/packs` に集約されたか、または dynamic UI に移ったかを実測。旧 route から legacy API を呼ばない |
| `/graphs` Capability Graph Editor | Profile/graph を選ぶ、Readable/JSON 表示を切替、JSON を編集、Validate、Compile/Preview、Save/Create、identity mismatch/stale を確認 | 現行 UI に無い場合は旧 chunk/API の不在と direct URL の拒否を確認。置換編集面がある場合は stale revision を必ず含める |
| `/profile-graph` Profile Wiring | Profile/category を選ぶ、palette を検索、tools/webhooks/API routes/prompts/frontend/flows/nodes を追加・削除、Preview、Apply、Launch | activation Profile の `profile_revision` と contribution の owner を照合し、未承認/disabled Pack の wiring が出ないことを確認 |
| `/ai-input` AI Input Inspector | Profile を選ぶ、Refresh/Show text、preview message を編集、edge を選ぶ、edge の Disable/Enable、field/operator/value を選び condition gate を挿入、Preview、Apply、token heatmap/prompt/tool/policy/diagnostic/trace を読む | 非表示・retired を確認。置換面がある場合は preview が未反映で、Apply 後だけ新しい revision になることを確認 |
| `/api-map` API Map | Profile/focus を query に設定、Apply/Reset、Refresh、search/category filter、runtime entity を選択、inbound/outbound connection、runtime trace の steps/primary/fallback、Profile Runtime/Inspector/Diagnostics を確認 | 旧 direct URL/legacy API の fail closed。置換表示では selected Profile、catalog、Pack owner が同じ snapshot であることを確認 |
| `/profile-workspace` Profile Files | Profile を選ぶ、Refresh、Configuration/Permissions/Flow YAML/Prompts の tab を切替、workspace paths、startup config、resource manifest、flows、rule prompts を read-only で確認 | path traversal/別 Profile の混入がないこと。旧 route を削除する場合も未認証の JSON endpoint が残っていないことを確認 |
| `/settings` Settings | Profile/Version tab、avatar、username、language、job を編集して Save、Reconnect、light/dark、Rounded/Minimal、background status/Send to background、desktop permission Refresh、Developer Debug Approval の ON/OFF/Revoke、update の Refresh/auto-update/Apply | 旧 Launcher settings が削除された場合は unknown。現行 Settings/OS 権限面がある場合は Tobkiri 名称、approval expiry/revoke、secret 非表示を確認 |

## 現行/統合後の操作スコープ

### Launcher の setup / Home / Packs

1. Fresh isolated workspace で `/panel/setup` を開く。
2. Defaults Review に Base Pack、Shell、conversation provider、Pack IDs が表示されることを
   保存し、同じ response の confirmation に含まれる `profile_id`, `catalog_revision`,
   `profile_revision`, `plan_digest` を backend snapshot として保存する。
3. exact confirmation checkbox を選択し、`Activate Defaults Profile` を押す。成功時の
   `profile_id=defaults`、activation receipt、`restart_required=false` を backend と照合する。
4. Presentation Selector で Base Pack/compatible Shell を確認し、verified materialization
   の後に `Save selection`、必要なら `Launch selected Shell` を実行する。Shell の
   `provider_id=shell.tauri.default`、`contract_id=app.shell.v1`、backend identity、
   state owners、artifact digest を記録する。
5. Home で `Manage Packs`、`Refresh`、Active Packs、Flows、Kernel、Supervisor Snapshot、
   runtime error の Reload/Copy を確認する。
6. Packs で検索、Installed/Available・Enabled/Disabled・Approved/Needs approval/Revoked
   バッジ、detail 遷移、Install、Approve、Revoke、Switch（enable/disable）、required/core
   表示を確認する。
7. Pack detail で artifact/catalog/profile/plan identity、capabilities、flows、dependencies、
   declared/callable operations、dynamic diagnostics、PackVM lifecycle を確認する。
8. verified callable operation がある場合だけ、synthetic workspace path を使った
   File Inspect を一回実行し、結果が workspace 外へ出ないことを確認する。

### defaultspack webapp と profile の一致

- dynamic catalog の `profile_id`, `profile_revision`, `plan_hash`, `catalog_hash` を
  Launcher Packs/Pack detail と同じ時点の backend snapshot に照合する。
- webapp の `Advanced Settings`、advanced commands、Profiles/Model selector、settings
  sections を表示し、advanced toggle 前後で visibility と keyboard navigation を確認する。
- webapp 内の model/runtime profile candidate は candidate ID と selected/active ID を
  取得する。ただし v4 `profile_id=defaults` と一致させることを期待せず、backend の
  `active_profile_id`/activation record、selected candidate、Pack-derived configuration
  の関係を画面とログで記録する。
- 未承認、disabled、quarantined、stale catalog の Pack contribution は表示も callable
  operation もされないことを確認する。

### mobile client

Tobkiri mobile client の接続先は isolated local runtime とし、既存コードの内部識別子や
互換上の表示ラベルは変更しない。

- Home: Refresh、Authority approvals、Settings、health/connection、migration、Pack
  requests、module list/detail、Enable/Disable/Reload/Rollback、確認 dialog を操作する。
- Settings: Kernel API URL/bearer token は fixture secret を使い、表示・証拠には残さない。
  auto refresh の Save/Cancel と再起動後の read-back を確認する。
- Authority approvals: pending card の bounded resource、risk、reason、requester、expiry、
  `Approve once`、high-impact confirmation、Deny reason/Cancel、Refresh/pull-to-refresh
  を確認する。expired/non-pending は network を発生させず reject されることを backend
  mock と request counter で確認する。

## 受入マトリクス

| ID | シナリオ | 期待結果 / 必須証拠 |
| --- | --- | --- |
| ADV-01 | Sidebar の Advanced と旧8項目を列挙 | 最終仕様に応じて「意図的に無い」または replacement contribution のみ。旧 URL は unknown/guarded、旧 chunk/legacy API request なし |
| ADV-02 | 旧8画面の direct URL、Back/forward、reload | 旧 route が残らず、blank/error loop、stale screen、chunk 404 がない。replacement の場合は各 route の AX label と owner Pack を記録 |
| PROFILE-01 | setup activation 前後 | backend `profile_id`, `profile_revision`, `plan_digest`, `catalog_revision`, activation receipt と Setup/Home/Packs の表示が一致 |
| PROFILE-02 | backend enabled Profile と webapp candidate | selected/active candidate、effective Pack set、dynamic catalog が同じ activation snapshot。別 Profile への silent fallback なし |
| PACK-01 | Pack add: install → approval candidate → approve → enable | Packs list/detail、Home count、v4 Profile candidate/config、plan/catalog revision、dynamic contribution が反映。未承認/disabled Pack の UI/operation は無い |
| PACK-02 | Pack detail operation / File Inspect | verified PackVM doctor と current catalog の callable operation だけ実行可能。workspace-relative fixture の結果、owner/contribution/contract が一致 |
| REFRESH-01 | activation、enable、disable、revoke 後に全画面 refresh | Setup/Home/Packs/detail/webapp/mobile の各画面で新 revision を読み、古い badge/count/contribution/operation が消える。前後 screenshot と backend snapshot を保存 |
| PERSIST-01 | Launcher/runtime restart | `profile_id`, selected presentation、Pack installed/enabled/approval state、catalog revision、webapp contribution が一度だけ復元。child runtime の重複なし |
| APPROVAL-01 | approval/PackVM consent を拒否 | dialog close/deny 後に state mutation がなく、enabled/approved にならず、retry が可能。audit に deny のみが残る |
| APPROVAL-02 | approval/PackVM request timeout | foreground/mutation の timeout 後に spinner が止まり、safe error と retry が表示され、backend は pending/unchanged の契約を守る。token/secret がエラーに無い |
| CONSIST-01 | stale catalog/profile revision | 旧 `catalog_revision` または `profile_revision` の request は stable stale error で拒否。UI は古い verified catalog を callable として使わず、Refresh を促す |
| CONSIST-02 | tampered revision/digest/artifact | isolated fixture の catalog/plan/artifact/activation envelope の改ざんは fail closed/quarantine。route/contribution/operation を公開せず、アプリは Home に復帰可能 |
| CONTRACT-01 | generated map digest と packaged map | source/generated map、application manifest、packaged resource の SHA-256 と schema/pack_id が一致。map target/principal ledger と runtime provider metadata が一致 |
| CONTRACT-02 | Launcher frontend request trace | 恒久的な UI business request は canonical `/api/contracts/defaultspack/...` のみ。allowlist boundary は目的・method/pathが一致し、runtime-recovery、legacy panel、未登録 route、Registry fallback は 0 件 |
| CONTRACT-03 | declared Contract/Operation/Function principal | map target → provider metadata → resolved plan `function_principal` → current authority grant が contract/operation/provider/function/artifact/profile/plan revision まで一致 |
| CONTRACT-04 | map digest tamper/stale | map 1 byte tamper、artifact manifest digest mismatch、old security epoch/Profile capture は socket bind または request 前に拒否。legacy/Registry fallback なし |
| CONTRACT-05 | client-trusted approval probe | forged `approved` request field、DOM/local store の approval flag 改ざん、unapproved/disabled Pack の callable probe は backend authoritative catalog/authority により拒否。UI state は mutation しない |
| CONTRACT-06 | boundary allowlist method/path | auth exchange、setup activation、PackVM lifecycle、health だけが exact allowlist。Profile/Advanced/Settings/activationの復元面がこれらをproduct read/mutation fallbackに使わない |
| TAURI-01 | Tauri command boundary | restored runtime/profile/topology/activation data は Tauri から取得しない。許可は presentation-only window/menu/native lifecycle/Shell launch と local frontend settings。authority/approval mutation は Tauri 0 件 |
| WIDTH-01 | Launcher 800x600、1024x768、1280x800 | horizontal overflow、clipped dialog/button、unreachable AX control がない。Pack detail/approval/lifecycle panel を含む |
| WIDTH-02 | defaultspack webapp 320/375/390 幅、1024/1280 幅 | Advanced Settings、Profile selector、Pack contribution の text/button wrapping と focus order に破綻なし |
| WIDTH-03 | mobile 320x568、375x667、390x844、800 threshold、landscape | compact list/detail と wide split の切替、NavigationBar、approval card、module action に overflow/重複操作なし |
| ART-01 | packaged `Tobkiri Launcher.app` を絶対パスから起動 | dev server 不使用、bundle product name/identifier が Tobkiri Launcher、署名・catalog・IPC・HTML content が検証済み。Setup→Home を実操作 |
| ART-02 | DMG artifact（提供時）を別場所から展開して起動 | `hdiutil verify`、path relocation 後の app launch、Resources 内の release binding と catalog/artifact digest が一致 |
| MOBILE-01 | high-impact approve / deny / expired | explicit confirmation、Deny reason、Cancel、expired pre-network rejection、secret-like resource redaction、submit once |

## fixture / harness

`fixtures/pr1332_gui_e2e_scenarios.json` は実データではなく、GUI 実行前に isolated
backend fixture を作るための synthetic scenario manifest です。digest と secret は
placeholder であり、実行時には実際の値を証拠ファイルへ解決します。

### backend fixture の組み立て

- 正常 Pack は `tobkiri_runtime/tests/test_external_pack_catalog_v4.py` の signed external
  Pack helper/fixture を優先して再利用する。外部ネットワークから取得しない。
- dynamic frontend は `tobkiri_runtime/tests/test_frontend_host.py` の profile-scoped
  contribution fixture を使い、`pack-a` 相当の owner/contribution/operation を synthetic
  ID に置き換える。改ざんケースは `tmp_path` 相当の isolated copy だけを変更する。
- backend の read-only probe は `/health`、setup state、presentation catalog、
  `catalog.read`、`pack.status`、Profile capture/activation record、dynamic UI catalog、
  audit/log snapshot を使う。contract map の artifact entry、provider metadata、resolved
  plan `function_principal`、authority grant digest も同じ snapshot に含める。mutation は
  最終 GUI run では UI からのみ行う。
- fixture が公開する必須 binding は `profile_id`, `workspace_id`, `profile_revision`,
  `plan_digest`, `catalog_revision`, `catalog_hash`, `pack_id`, `artifact_digest`,
  `approval_status`, `enabled`, `owner_pack_id`, `contribution_id`, `operation_id`、
  `contract_id`, `provider_id`, `function_id`, `function_principal`, `contract_map_sha256`。

### Computer Use harness 契約

実装を追加せず、手動 run でも次の一行を scenario ごとに埋める。

```text
capture(run_id, scenario_id, surface, page, action, pre_state, post_state,
        backend_snapshot, ax_text, screenshot_paths, log_paths, result)
```

`pre_state`/`post_state` は app state の URL、window size、visible text、AX role/name、
request/console error の redacted snapshot。`backend_snapshot` は上記 binding と effective
Pack set の JSON。各 failure injection は fixture を launch 前に用意し、実行中に本番
workspace の catalog を破壊しない。

## Computer Use 実行手順

1. `git rev-parse HEAD`、OS/arch、Node/Python/Rust/Flutter version、実行者、Run ID を記録する。
2. clean な isolated user-data/workspace root と空の evidence directory を `/private/tmp`
   以下に作る。既存の Tobkiri データを削除・上書きしない。
3. packaged release を使う場合は、CI artifact の `.app` を exact path に展開し、先に次を実行する。

   ```bash
   shasum -a 256 "Tobkiri Launcher.app/Contents/Resources/app/bundled/presentation_catalog.json"
   codesign --verify --deep --strict --verbose=2 "Tobkiri Launcher.app"
   python3 tobkiri_launcher/scripts/verify_presentation_release.py --app "Tobkiri Launcher.app"
   ```

   署名済みの `.app` を検証できない場合は GUI の PASS を付けない。
4. Computer Use の `get_app_state` に `.app` の絶対パスを渡して起動し、HTML content、
   window title、`Tobkiri Launcher`、Setup/Home の AX tree が出るまで待つ。dev server URL や
   bare binary での代用は artifact launch の証拠にしない。
5. 各クリック・キー入力・resize の後に `get_app_state` を呼び、fresh AX tree から次の
   target を選ぶ。ページ遷移は URL と visible heading の双方で確認する。
6. `PROFILE-01` → `PACK-01` → `REFRESH-01` → `PERSIST-01` → failure cases の順で実行する。
   途中の mutation ごとに pre/post backend snapshot、screenshot、redacted log を保存する。
7. mobile は simulator/test harness の実 device size を記録し、`flutter test` の既存
   `authority_approval_screen_test.dart` の overflow/approval assertions と GUI 観測を対応付ける。
8. 終了時に app/runtime を正常終了し、child process、pending operation、temporary fixture
   の cleanup を確認する。証拠には secret/token を含めない。

## 証拠様式

各 scenario で `PASS` / `FAIL` / `BLOCKED` と、UI/backend の差分を一つの JSON/Markdown
record にする。

```yaml
run_id: "PR1332-YYYYMMDD-HHMM"
scenario_id: "PACK-01"
target_sha: "<integrated commit>"
artifact:
  app_path: "/absolute/path/Tobkiri Launcher.app"
  app_sha256: "<sha256>"
  catalog_sha256: "<sha256>"
  contract_map_sha256: "<sha256>"
  contract_map_manifest_digest: "<sha256>"
  release_binding_verified: true
environment:
  os: "macOS <version>"
  architecture: "arm64|x86_64"
  surface: "launcher|defaultspack-webapp|mobile"
  viewport: "1280x800"
  isolated_user_data: "/private/tmp/<run-id>/user-data"
  backend_endpoint: "<redacted/local endpoint>"
identity:
  profile_id: "defaults"
  workspace_id: "<synthetic workspace>"
  profile_revision: "sha256:<64 hex>"
  plan_digest: "sha256:<64 hex>"
  catalog_revision: "sha256:<64 hex>"
  catalog_hash: "sha256:<64 hex>"
  activation_id: "<redacted-or-synthetic>"
  security_epoch: 1
  contract_bindings: [
    {
      "contribution_id": "defaults.pack.catalog",
      "contract_id": "tobkiri.host.pack-control.v4",
      "operation_id": "catalog.read",
      "provider_id": "tobkiri.host.pack-control",
      "function_id": "tobkiri.host.pack-control",
      "function_principal_digest": "sha256:<64 hex>"
    }
  ]
  selected_pack_ids: ["defaults-basepack"]
  effective_pack_ids: ["defaults-basepack"]
transport:
  canonical_request_count: 0
  allowlisted_boundary_request_count: 0
  allowlisted_boundary_requests: []
  unallowlisted_direct_request_count: 0
  unallowlisted_boundary_requests: []
  forbidden_path_matches: []
  registry_fallback_count: 0
  client_approval_assertion_count: 0
tauri:
  presentation_only_commands: []
  runtime_profile_data_commands: []
  authority_mutation_commands: []
  local_frontend_settings_only: true
steps:
  - step: 1
    at: "<ISO-8601>"
    action: "<AX action>"
    observed: "<visible text/role/name>"
    expected: "<assertion>"
    result: "PASS"
evidence:
  ax_snapshots: ["/private/tmp/<run-id>/ax/001.json"]
  screenshots: ["/private/tmp/<run-id>/screens/001.png"]
  backend_snapshots: ["/private/tmp/<run-id>/backend/001.json"]
  logs: ["/private/tmp/<run-id>/logs/redacted.log"]
result: "PASS"
defect_or_blocker: null
```

必須項目は `target_sha`、artifact/catalog hash、identity 4 値、viewport、isolated root、
各 mutation 前後の snapshot、結果です。ログを共有する前に URL query、Authorization、
cookie、request body の署名/secret、local path の個人情報を redaction する。

## 静的テストとの対応

GUI 実行前後に、変更された surface に応じて次を focused check として再実行する。

```bash
(cd tobkiri_runtime && python -m pytest \
  tests/test_external_pack_catalog_v4.py \
  tests/test_frontend_host.py \
  tests/test_frontend_contract_routes.py \
  tests/test_production_frontend_contract_http.py \
  tests/test_defaultspack_runtime_v4.py \
  tests/test_phase_a_setup_api.py \
  tests/test_phase_a_health.py \
  tests/test_pack_api_server.py \
  tests/test_minimal_profile_vertical_slice.py \
  tests/test_file_inspect_profile_activation.py -q)

rg -n --hidden -g '!node_modules' -g '!target' \
  "(/api/runtime-recovery/v4|/api/panel|/api/setup|/api/v4/packvm|/health|fetch\\(|XMLHttpRequest|sendBeacon|Registry)" \
  tobkiri_launcher/frontend/src

npm --prefix tobkiri_launcher/frontend run lint
npm --prefix tobkiri_launcher/frontend test
(cd tobkiri_runtime/ecosystem/defaultspack/webapp && npm test)

(cd tobkiri_mobile && flutter test \
  test/authority_approval_screen_test.dart \
  test/mobile_authority_test.dart)
```

macOS artifact では `tobkiri_launcher/scripts/verify_launcher_release.sh --app <app>` を
優先し、bundle build/署名/measurement/log analysis の結果を evidence directory に保存する。
この QA 文書の追加自体は製品実装ではないため、push や branch 更新は行わない。
