# Rust / Wasm 移行中の整理メモ

作成: 2026-10-27。Python の Host・Core を Rust へ移し、既存 Pack を
Python-on-Wasm へ移す作業で見つけた、古い処理・重複・コード削減案を記録する。
移植の完了表は [acceptance-status.md](acceptance-status.md) を使う。

## 更新ルール

- 調査・実装中に整理できそうな箇所を見つけたら、未対応でもここへ記録する。
- 場所、現在の問題、整理案、維持する動作、着手・削除条件、状態を残す。
- 同じ内容は既存項目を更新する。subagent が見つけたものも取り込む。
- 対応後はコミットと検証結果を追記し、未検証の範囲を明記する。
- 削減行数・性能改善は実測したものだけを書く。コード削減と移植全体の完成を分ける。
- 既存の Setup・専用 GUI 承認・API／モデル設定・会話の流れと機能を維持する。
  未コミットの変更、履歴、復旧データを保護する。
- 異なる信頼境界の検証を、同じ hash を使うという理由だけで省かない。
  旧処理の削除は、置換先の同等性と実製品からの接続を確認してから行う。

## 001: カタログ供給の二重 JSON / Base64 と未使用デコーダ

- 状態: **対応済み（読み込み表現の整理のみ）**。
- 場所: [App adapter](../../../packs/local.harness-bundle-catalog-python/python/bundle_catalog_adapter.py)、
  [source producer](../../../tools/python313-producer/bundle_catalog_resources.py)、
  [Native comparison wrapper](../../../tools/python313-producer/bundle_catalog/native.py)。
- 問題: 固定の原本バイトを JSON 内の Base64 として再度 JSON に包み、呼出しごとに
  復号していた。ordinary Pack に不要な carrier decoder も残っていた。
- 整理: 原本 lock・一意の原本バイト・読み取り索引を compiled resource に格納。
  guest 内の未使用 carrier decoder を削除し、carrier は producer 入力に限定した。
- 維持: 原本 305 件、453 回の読み取り、順序と再読み取り、digest・pin・schema 検証。
  同じバイトを共有しても、必要な読み取りや検証は減らさない。
- 根拠: `58c9588ac`。
  [実 CI](https://github.com/harupipipipi/tobkiri/actions/runs/40502451402) で
  source identity は一致。full catalog は燃料上限に達したため、全体の完成には数えない。

## 002: 同じ resource read の直後の SHA 再計算

- 状態: **対応済み（同じ immutable bytes の実計算 digest を再利用）**。
- 場所: [captured loader](../../../tools/python313-producer/schema_probe/captured_loader.py) の
  `_read_verified` / `_read_verified_bytes` と App adapter の `read_member`。
- 問題: loader が展開・SHA 検証した直後に、adapter が同じ bytes をもう一度 hash していた。
- 整理: 実際に返す bytes と、その read で実計算した digest を一緒に返し、index の期待値と照合する。
- 維持: 毎 read の復元・長さ・EOF・余剰データ・SHA 検査、immutable bytes の所有寿命、
  通常の `__getitem__` / `read_bytes`、原本側の全検証。期待値を実測値として返さず、
  展開結果や検証成功を事前 cache しない。Native witness は保持 bytes を独立に hash する。
- 根拠: `90022de28`。
  [実 CI](https://github.com/harupipipipi/tobkiri/actions/runs/40504211160) で
  source identity は一致。001 と合わせて source 確認の総燃料は約 58% 減少したが、
  full catalog は依然上限超過。コード削減や全体完成率とは別の実測値。

## 003: legacy の試作 owner と実 core-host の経路整理

- 状態: **未対応。実製品の接続・移行が先**。
- 場所: [native_harness_bootstrap.rs](../../../tobkiri_launcher/src-tauri/src/native_harness_bootstrap.rs)、
  [core_host.rs](../../../tobkiri_launcher/src-tauri/src/core_host.rs)、
  [feature 分岐](../../../tobkiri_launcher/src-tauri/src/lib.rs)。
- 問題: legacy 側の `NativeRuntimeState::adopt_core` と `prepare_original_component` に
  実 callsite がなく、別の core-host 経路に実 Core・Session・UiAttachment がある。
  両者の root も異なる。試作 API の存在だけでは製品接続にならない。
- 整理案: 実 core-host の同じ所有者へ正規の Setup・保存・起動・終了経路を接続し、
  役目を終えた試作経路や重複した所有者を段階的に削除する。
- 条件: 旧 Profile・データ・秘密情報・復旧の実移行、実候補と承認の拘束、
  Stop / Quit の終了・失効まで確認する。空の新 root を既存環境の代用にしない。
  最初の Profile を CLI で作らせる暫定画面も、元 Setup に戻すまで未完了とする。
- 詳細: [実 owner と初回 Setup の未接続境界](native-flight-ownership.md#actual-product-owner-and-missing-first-setup-connection-2026-10-27)。

## 004: スキーマ処理の Rust 化後の実装・依存整理

- 状態: **比較実装を準備中。製品の置換・削除は未実施**。
- 場所: [validation.py](../../../tobkiri_runtime/tobkiri_protocol/validation.py) の
  `load_schema` / `_schema_store` / `validate_document` と、Pack 内の schema 依存一式。
- 問題: 元のスキーマ検証が Wasm の燃料上限を超える。
  [実 CI](https://github.com/harupipipipi/tobkiri/actions/runs/40506932105) では
  42 本の cold selfcheck 単独でも上限に達した。
- 整理案: schema・instance・local reference の処理を一つの private Rust/Wasm kernel で共有し、
  置換後に未使用となった Python 実装・依存だけを除く。二つの engine の恒久併存を避ける。
- 条件: 実 producer で build / load / call、原本 64 ケースの error・順序・実 object 参照、
  full catalog の 45 cold calls・cache 寿命・305 件 / 453 reads・拒否例を確認する。
  型や参照を失う JSON clone、bool だけの判定、検証省略で同等性を装わない。
  他の機能から使われる依存は残す。成果物サイズと元の予算も再測定する。
- 注意: 現候補ライブラリの schema / error は `serde_json` の値になる部分があり、
  元 Python object の参照保持は未証明。参照解決や error 情報を復元できない場合は、
  巨大な補正レイヤーを積む前に原本 engine の直接移植へ切り替える。
- 2026-10-30: 比較候補の依存を CI 上だけで解決する
  [source lock workflow](../../../.github/workflows/issue-1520-schema-kernel-lock.yml)
  を追加。実 registry checksum と manifest / lock の SHA を記録し、source lock
  だけを保存する。候補 manifest の追加・更新時に実行し、手動起動も用意する。
  ローカルで依存を取得せず、未生成の lock や未実行の build / load / parity を
  成功扱いしない。これは製品への採用や Python 依存の削除を意味しない。
- 同日、[比較専用 kernel](../../../tools/python313-producer/schema_kernel/README.md)
  のソースを追加。Python instance の直接読み取り API はあるが、error の instance は
  serde clone で、元 schema 参照・順序・parent / cause は保たれない。
  元 root を別途保持しても error node 参照の同等性には数えない。
  現時点では formatter とソース確認のみ。製品の validator は置換していない。
- 依存の実固定は [CI 41049191953](https://github.com/harupipipipi/tobkiri/actions/runs/41049191953)
  で成功。revision `40fa84327` の実ログと receipt に拘束した Cargo.lock を保存した
  （20,394 bytes、87 packages、SHA-256
  `7bd7deb115b32dc7012b44a960efc45d6591ac148fa5a0466c1741a523d9b065`）。
  保存後は自動で再解決せず `--locked` を使う。これは build / load / parity の証拠ではない。
- 実拡張の [CI 接続](../../../.github/workflows/issue-1520-schema-kernel-probe.yml)
  を準備。既存 rpds の固定 toolchain / SDK / producer、Core の実 DATA consumer を使い、
  vendor の自己申告 checksum だけでなく元 `.crate` archive と照合する。
  build script の追加時に起動する。現時点では接続手順の構文確認までで、
  実 build / import / call の成功は未確認。
- 実 CI `41049965671` / 診断 CI `41051424994` は build 前の vendor 照合で停止。
  `ahash-0.8.12` の package checksum は一致し、Cargo vendor が `.gitignore` を
  除外していた。照合を弱めず、CI の `cargo fetch --locked` で得た元 `.crate` を
  既存の bounded extractor で全件展開し、全ファイル・mode・checksum を再照合する。
  使わない Cargo vendor tree / config の生成も除き、表現の違う二重の供給経路を減らす。
  修正後の実 CI はこれからで、kernel の compile / import / call は未証明。
- 修正後の CI `41051810304` / job `124049453850` で、全 archive 展開後の vendor
  照合が実際に成功した。現在は Core consumer の build 中で、kernel の成功とは別。
  追加整理候補: `fetch_registry.unpack` 自体が checksum ファイルを生成するため、
  wrapper の直後の再書き込みは重複している。canonical bytes と mode の維持を確認して
  次の実 build 修正時にまとめて除く候補とする。今の CI の検証対象は変更していない。

## 005: 初回 Profile の生成・承認・保存を一経路にする

- 状態: **提案。既存 Setup 接続時に実装する**。
- 場所: [Core の Profile 管理](../../../crates/tobkiri-core/src/core.rs) の
  `create_profile` / `import_profile` / `save_profile_with_native_review`。
- 問題: Session 前の Profile 作成 API はあるが、元 Setup からの接続がない。
  通常の `create_profile` で保存してから native-review で保存し直すと、
  承認と保存が二重になり、コードも状態管理も増える。
- 整理案: parse・Core による fresh ID 発行を既存の generic 管理処理の内部で共有し、
  実候補に拘束した native-review から一度だけ保存し、その実 ID で Session を開始する。
- 維持: 入力中の古い ID・承認・データを引き継がない。候補・head・epoch・実入力の拘束、
  元 Setup と専用 GUI 承認の順序・操作回数を維持する。commit 不明時は起動成功へ進めない。
  App の意味や新しい WIT・権限を Core に追加しない。
- 根拠: 実 callsite の source 調査と Astra high の設計回答。実装・動作検証は未実施。
- 2026-10-30 の追加確認: 実 `core_host.rs` の `NativeHost` は individual の
  `prompt_approval` だけを実装し、grouped `prompt_approval_review` は未実装。
  [TrustedPrompt の既定実装](../../../crates/tobkiri-gate/src/protocol.rs) は
  `Unavailable` を返す。したがって保存 API の存在だけでは初回 GUI Setup の接続を
  証明できない。今後この経路を接続する前に、Core の完全な候補・全 item と実 native
  入力を同じ live review に結び付ける必要がある。個別 prompt への自動 fallback や
  renderer の承認フラグで代用しない。既定の legacy GUI が失敗したという証拠ではない。
- 元の [専用承認ウィンドウ](../../../tobkiri_runtime/ecosystem/defaultspack/webapp/src/components/AuthorityApprovalWindow.tsx)
  は `typed_confirmation_required` のとき確認文の一致も要求する。
  「全承認は二択だから同じ一括ボタンにできる」と一般化しない。
  各実要求でこの条件が適用されるか確認し、元の必要な同意を維持する。
  Astra の一括 Approve / Deny 案は、選択済みの候補・二択の要求・全件 atomic 保存を
  実ソースで確認できた場合だけの条件付き判断である。
- 条件の供給元も区別する。元 `tobkiri_host/authority_v4.py` は実 request の
  `typed_confirmation_digest` の有無を UI の required に投影し、表示 phrase との
  一致も検査する。別の旧 `core_runtime/authority/service.py` は critical risk・
  `host.process.exec_guarded`・resource / permission 定義から判定する。
  action のモード選択では `SELECT ...`、実行操作では `EXECUTE` を使う箇所もある。
  これらの条件を通常の Profile 初回承認へ一律追加しない。逆に、通常起動が二択でも
  特殊な既存操作の入力要件まで削除しない。実 core-host の現 `trusted_review` は
  Risk による警告表示だけで、上記の入力条件を移植できた証拠にはならない。
- 経路を混同しない。App の `blocks/coding/pack_approval_request.py` は明示的に
  active Launcher debug lease を要求する再承認用で、通常初回 Setup の証拠には使わない。
  Rust の実 Profile 保存は Gate `review_pack_permissions` で、実 revision の最大権限と
  旧承認の差分を実 digest / reservation / summary に拘束する。native または
  cross-profile storage の追加は Dangerous。既存 mediator の狭めた結果も保持する。
  一括 native 表示はこの完全な候補・実 prompt 全件・必要な警告を維持し、
  デバッグ用入力や別操作の確認文を通常 Setup に追加しない。
- 実 `NativeHost` の grouped review 接続を実装。permission / user-permission のみを
  受け付け、完全な候補と各実 prompt を既存 native detail window へ表示する。
  実 native の一回の判断だけを同じ live batch の実 item ID に返し、cancel・失敗・
  未対応 OS・未対応 effect・表示上限超過は拒否する。個別 prompt へ fallback しない。
  危険な権限は警告を維持し、候補だけの確認も表示する。追加の確認文を通常起動へ作らない。
  source review で見つかった prompt accessor の寿命指定を修正した。
  現時点では source / format 確認まで。実 macOS compile・GUI 操作・初回 Setup の
  fresh-ID / 一回の保存 / 同じ Session 起動は未検証・未接続で、項目全体は未完了。
- `b01fce245` の CI `41051559344` / portable job `124048600707` が成功。
  native 表示契約 7 件（追加した grouped の 2 件を含む）と Gate 76 件が通った。
  macOS の実 Host compile と実 GUI 入力は別の証拠が必要で、ここには含めない。
- 初回 Setup の接続では既存 `defaultsSetup.ts` の confirmation / activation 契約も維持する。
  同コードは実 `defaults.activate`・Profile の `defaults`・catalog / profile / plan /
  authority snapshot / confirmation の digest、epoch、activation、fencing、audit receipt の
  対応を検査する。Core の新 Profile ID や structural review JSON をそのまま返して
  同じ成功応答の代わりにしない。互換の表示・App ドメイン値と、実 Core の ID / 保存結果の
  拘束を App 側で明示し、Host で固定成功応答や架空の activation / audit を作らない。

## 新規項目のひな形

### NNN: 整理したい処理

- 状態: 未調査 / 提案 / 実装中 / 対応済み / 保留。
- 場所:
- 現在の問題:
- 整理案・減らせる実装:
- 維持する動作・着手／削除条件:
- 根拠・コミット・検証結果:
