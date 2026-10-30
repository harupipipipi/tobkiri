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

- 状態: **ライブラリ候補は不採用。原本 36 handler と走査の Rust 初稿を実検証中。元の予算での完走・製品置換は未達**。
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
- 同 run はその後、Wasm extension build 段階を完了し、次の probe 起動時に停止。
  sandbox の `/usr/bin/python3` に `tomllib` がなく、probe が使わない lock parser の
  top-level import が原因だった。lock parser 内へ import を移し、実際に必要な環境でだけ
  読み込む。上記の重複 checksum 再書き込みも除去し、元 extractor の出力を全件検証する。
  実 ABI 検査・producer import・Core 呼び出しは未完了。修正後の CI で確認する。
- 次の実 CI `41053629100` / job `124055556831` では extension の build・静的 ABI 検査後、
  元 producer が `Component built successfully` まで進んだ。次の component 境界検査が
  Wasmtime Engine に消費済みの Config を再利用して停止したため、静的検査ごとに
  Config を作る形へ修正。実要求の Store / fuel / deadline は作り直さない。
  Core による実呼び出し・64 ケース・full catalog の成功はまだ証明していない。
- 修正後の `41055703104` / job `124062569122` は、実 Core consumer の実行と
  report 読み取りまで進み、smoke の期待条件の不一致で停止した。
  元の条件は維持し、失敗時の実 report を bounded log と text artifact に保存する。
  失敗した呼び出しを成功や互換性の証拠に数えず、次の実レポートで差分を特定する。
- Astra high の追加設計回答: smoke が実際に通った後、64 ケースで参照・順序などの
  不一致を具体化して現 library candidate の不採用を判断し、巨大な補正層を作らない。
  次候補は元 validator の走査・handler 順序・短絡・branch / evaluated bookkeeping を
  狭い private Rust / PyO3 境界へ直接移す。元 Python error class の軽量な組立て、
  regex / 数値 primitive、registry / resolver は Wasm 内で実 object を保って再利用できる。
  constructor 内で validation を再実行せず、serde clone や path からの参照再構成は使わない。
  lazy error、context の順序・parent と child の同一性、実 cause、dynamic ref の scope、
  bool / int / 1.0 / bigint、unevaluated の branch bookkeeping、cache の所有・破棄も維持する。
  64 件だけでなく supported API から到達する元 handler 全件の同等性が条件。
  これは設計判断で、直接移植の実装・性能・製品採用を証明するものではない。
- [原本 scope inventory](schema-port-scope.md) に固定版の全 36 handler と entry / cache /
  resolver / error graph を記録。selfcheck の class checker と document の generic checker、
  legacy RefResolver と evaluated helper の Registry lookup を共有化で潰さない。
  実 guest の format 登録集合と handler coverage は未観測で、64 ケースだけで全件を
  検証できたとは扱わない。元の helper を再利用し、同じ機能の別実装を増やす前に比較する。

- 実 CI `41058189370` / job `124070924951` は build・import・component 生成後、
  Core の実呼び出しが `referencing::Registry::new` 内の WASI random で unreachable trap。
  実行時間約 0.040 秒、残燃料 1,988,319,617 / 2,000,000,000 で、容量・燃料超過ではない。
  返却値はなく、64 ケースは **未実行**。既知の serde 参照・順序の不一致と合わせて、
  Astra high の追加回答に従い、この候補を不採用とした。固定乱数や追加の Host 権限で救済しない。
- 原本 object の直接移植に着手。現在は実 schema / instance の参照保持と既定 7 型の
  primitive のみで、handler 実装数は **0**。実 error graph・validator API・全 36 handler は未実装。
  PyO3 以外の依存と旧 lock を除き、新しい実 source lock を CI で解決する。
  旧 builder の library smoke は未対応のため、この段階では起動しない。
  新 primitive 自体の build / load / call もまだ未検証で、製品置換や全件互換性に数えない。

- 新 primitive の依存固定は CI `41065264251` / job `124094688838` で成功。
  source `e04117faa` の manifest と実ログ・receipt を照合して source lock のみ回収した
  （15 packages / 3,395 bytes、SHA-256
  `222ef5c4c4b48fbad400c604b0135efe9a56d7ecd1c9cec90ef1021d639b93d6`）。
  builder も `original-object-primitives/1` の狭い検証へ変更。元 object の identity と
  mutation alias、型 21 例だけを対象とし、schema validation 未実行・handler 数 0 を
  receipt に明記する。実 build / load / call の結果はまだなく、64 件や全 validator の
  成功に読み替えない。既存の memory / fuel / deadline / ABI / checksum 条件は維持。

- 次の直接移植境界について Astra high に追加質問し、既存 Python Validator の公開 API /
  実 class / instance を状態の所有者として残し、`iter_errors` / `descend` の内側へ
  Rust の遅延 iterator と全 handler を移す設計回答を得た。`VALIDATORS` の差し替えだけで
  Python traversal を残す方法や、別の validator / resolver / cache の二重実装は避ける。
  `evolve` の結果も元 class の実 instance とし、元のクラス選択・引数継承・scope / cache
  の共有と寿命を維持する。全 error の先行生成や元 traversal への自動 fallback はしない。
  iterator 作成 / 最初の next / 途中終了、複数 iterator の交互実行と direct API 呼び出しの
  時点・例外・短絡も観測対象。これは今後の実装境界の設計回答で、primitive の成功や
  完成済み validator の証明ではない。

- 新 primitive の実 [CI 41065687808](https://github.com/harupipipipi/tobkiri/actions/runs/41065687808) /
  job `124096340005` が source `e436a6d5f` で成功。実 extension build / limited-ABI 検査 /
  原本 producer import / component 生成と、実 Core NativeData の参照・mutation alias・
  21 型判定チェックまで通った。これにより上記の primitive build / load / call 未検証は解消。
  64MiB / 2B fuel / 元の絶対 30 秒を維持。根拠は当該 source の strict builder gate と
  成功した実 CI step で、raw child JSON は CI のみに保存しローカルへ取得していない。
  実残燃料値・OS owner / GUI・schema validation・全 36 handler・64 比較・full catalog の
  証明には数えず、製品にはまだ接続しない。

- 原本 traversal と全 36 handler の Rust 初稿、実 class を保つ thin adapter を追加。
  既存の 64 selfcheck reference は helper を共有する形で observer 本体を移し、
  比較内容の AST 同等性を確認。期待結果や期待 registry は guest に入れず、元 schema 入力
  だけを渡し、実 object 参照・parent / cause・順序・render hash を照合する CI 接続を追加。
  全 64 件を同じ実 Core DATA 呼び出しの 64MiB / 2B / 元の絶対 30 秒で処理し、
  callback の lazy / close / interleave / PEP479 など 7 API probe も加える。
  現時点では構文・formatter・source review のみで、実 compile / 64 比較成功は未確認。
  元 validator の製品経路や旧 Python 依存は削除・切替していない。

- 実 CI `41070883433` / job `124114339579`（source `638bf7489`）で原本 64 reference
  （187,923 bytes）の生成、Rust 拡張 build・ABI 検査は成功。producer の初期化で
  test App が sysconfig の config module 読み込み前に loader を seal し停止した。
  原本 probe と同じく設定 module の genuine import 後に seal する順へ修正。
  まだ実 Core call / 64 比較には到達していない。
- 接続漏れ・追加 cache の整理: 以降の公開 create / extend factory の実 class にも
  同じ traversal を install し、登録済み class の追加 strong set を WeakSet へ変更。
  実 class の弱参照解放を 8 件目の API probe で確認する。Astra high は、実 identity で
  事前選択する custom / 旧 draft の明示 dispatch を native 失敗後の fallback と区別して
  R11.2 へ適合と回答。ただし既取得 factory 参照・残り API 経路や他 closure からの
  strong retain は未検証で、全 draft native 化や全 API の完了を主張しない。

- 次の実 CI `41071710332` / job `124117409348`（source `70c926adb`）は component
  生成と実 Core call に到達し、正の remaining fuel の lifetime 条件で停止。生の
  fuel 値・型・trap がログに出ていないため、現時点で数値や原因は断定しない。
  条件を変えず、validate_report が raise する前にも bounded 実 report を残す。
- generator 後始末の具体差を整理: Rust keyword iterator の通常 dealloc は、
  外部で retain された yield-from child を閉じない。各 builtin iterator を薄い本物
  Python generator で包み、原本の finalization / GeneratorExit を再利用する。
  handler 本体は Rust のまま同じ予算で動き、元検証へ戻す fallback や独自 tp_finalize は
  加えない。Astra high がこの境界を支持し、明示 close だけでなく最後の outer 参照を
  解放する実 dealloc テスト・未開始・通常終了・循環 GC の区別を要求した。
  これらを含む 12 API 観測を原本 native と移植 guest の両方で実行する接続を追加。
  現時点でこの修正版の実行成功は未確認。

- 実 CI `41074411036` / job `124126643885`（source `f4f95793b`）で原本 native の
  12 API 観測は実際に成功。移植 guest は残燃料 **0**、全 **2,000,000,000 fuel** を
  消費し、約 **1.311426821 秒**で停止した。元 deadline は維持され、期限超過・
  unreachable trap ではない。64 比較や guest API の完了は証明できていない。
- 次の整理は既定型判定の入口: exact 元 Draft202012 class + 同一 default checker +
  素の str の既定 7 名だけを既存 Rust primitive へ移す。integer は exact int / bool /
  float に限り、float subclass の任意返却 object・例外を元 method に保持する。
  custom / redefine / 他 draft / 未知名・非標準引数は実行前に元 method へ明示 dispatch し、
  native 失敗を再試行しない。実 numbers.Number の ABC 登録・cache は元 object のまま。
  Astra high はこの条件付き境界を支持し、まず生 report を確認して同じ入力で前後比較を
  要求した。上記 raw 0 fuel を baseline とし、追加の非有限 float・bigint・subclass・
  実例外・ABC 変更・checker 変更・method override の比較と実行回数を準備。
  新しい型判定結果 cache は加えず、今は実 compile / parity / fuel 改善は未確認。

- Source review で、非既定 checker を guard と元 method で二重に読むと属性 getter の
  副作用・例外が変わることを発見。guard は exact class の raw class dictionary と
  元 accessor / checker method metadata を検査する形にし、descriptor や独自 getter は
  元 method へ送って実属性を一回だけ読む。TYPE_CHECKER descriptor の実呼出し数・
  同じ例外 object、checker method 上書きも 13 件目の比較群へ追加。実行結果はまだない。

- 比較入力の事前 materialize を除去: 旧実験では schema_inputs module が 64 schema を
  parsed Python literal として snapshot に保持していた。新実験は入力の raw JSON bytes
  だけを capture し、同じ実 Core 呼び出しの中で parse してから検証する。期待結果は
  引き続き外側に置く。元 input 値・順序・全 64 比較を維持し、予算外での入力解析を
  認めない。旧 0 fuel は不完走の記録として残し、cold-path 合格とは扱わない。

- 実 CI `41080438535` / job `124147774991`（source `ea0e6160a`）は原本 native の
  追加 API 比較で停止し、最適化候補の build / 燃料測定には未到達。元 rpds が
  non-str 型名 key の比較で BaseException 系 PanicException を出した。文字列型の
  API 比較と、None / list / bytes という契約外引数の実例外・panic 比較を分離し、
  どの入力も削除しない。公開 method の non-plain-str は追加 Rust 境界へ入る前に
  元 Python method に渡す。独立した original / candidate Wasm の異常終了比較は未証明。
- 機能差を先に発見する CI-only native precheck を追加。既存 source / wheel admission を
  再利用して frozen vendor から実拡張を build し、genuine 元 class / registry / errors で
  全 64 件・13 API 群・契約外引数を照合する。Wasm budget / OS owner / 製品の証明には
  数えず、後段の同じ 64MiB / 2B / 元 deadline の実 Wasm gate は必須のまま。
  strict JSON 差分 helper は共有して bool / int / float の等値混同を防ぎ、同じ observer
  と source admission の二重実装を減らした。新経路の実行結果はまだ未確認。

- 実 CI `41086108540` / job `124167563976`（source `2f853413f`）で native Rust
  拡張の全 64 比較・13 API 群・別枠の契約外引数が成功。実 type dispatch は
  guarded default 38,308 回 / explicit original 32 回。契約外 None は UnknownType、
  list は TypeError、bytes は元と同じ PanicException を実際に観測した。
  しかし Wasm 実 Core call は残燃料 0 / 2B、約 2.028240556 秒で停止。期限超過・
  unreachable はなく、Wasm 比較・燃料改善は未証明。
- 次は元 evolve の引継ぎ loop を Rust 化。対応する元 code object の一致を確認し、
  actual closure list / globals / Function.__builtins__ を保持する。実 schema を常に評価し、
  現在の validator_for を選択、missing alias のみ元順で属性取得、実 constructor を呼ぶ。
  中途 list 変更を snapshot 化せず、global / builtin の差替え・欠落 NameError.name・
  明示 None・属性取得順・class 遷移・constructor 例外・override を 14 件目の群で比較。
  Astra high の回答に従い元 class / resolver / registry の owner を増殖させず、GC 対象の
  実参照を追加。source `34994f41b` の CI `41090025133` / job `124181172734` は
  元 reference / API と実 native Rust 比較（全 64 件・14 API 群）の段階に成功。
  同じ job の実 Core consumer / component build も成功したが、Wasm 実行は
  残燃料 0 / 2B、約 2.049135565 秒で停止。元 deadline は保持、期限超過や
  unreachable はなし。run 全体は失敗、Wasm parity・燃料改善は未証明。
- 次の整理対象は元 `__attrs_post_init__` の body。元 generated attrs constructor・
  class・field initialization は保持し、元 code object が一致する body のみ Rust へ移す。
  実 registry / resource / resolver と closure / namespaces を引き継ぎ、boolean 属性取得の
  short-circuit・live keyword 選択・list 完成後の一度の保存・legacy scope callback の
  順序と失敗を 15 件目の API 群で比較する。
- 実 CI `41092195794` / job `124188678135`（source `23400a4a7`）は実 native
  compile・全 64 比較・15 API 群（繰り返し・途中失敗の状態も含む）に成功した。
  Core / component build 後の Wasm は残燃料 0 / 2B、約 2.059816844 秒で停止し、
  期限超過や unreachable はなし。Wasm 比較・燃料改善は未証明。停止 stack だけを
  全コストの根拠にせず、残存処理を計測して次の移植対象を絞る。細かな一時 object の
  finalization・全 API coverage は未証明。

## 005: 初回 Profile の生成・承認・保存を一経路にする

- 状態: **Core / native 承認部分を一部実装・CI 検証済み。既存 Setup 接続は未対応**。
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
- 同 run の macOS job `124048600442` は実コンパイルで停止した。
  `for ... batch.items().len()` の一時 iterator が borrow を保持するため、件数を
  別 statement で取得してから各実 ID へ回答する形へ修正。Tauri の実 frontendDist
  も未生成だったため、CI に既存 `build:core-host` を追加した。
  placeholder の画面で compile を通さず、正規の画面を生成する。修正後の検証はこれから。
- 修正 `c2d9feef2` の CI `41052938514` は両 job が成功。
  macOS job `124053223264` で正規 core-host frontend の build と実 Tauri / native Host の
  `cargo check --locked --features core-host` が通った。portable job `124053222710` も成功。
  実 native 入力・キャンセルの GUI 操作や初回 Setup 全体はまだ証明していない。
- 初回 Setup の接続では既存 `defaultsSetup.ts` の confirmation / activation 契約も維持する。
  同コードは実 `defaults.activate`・Profile の `defaults`・catalog / profile / plan /
  authority snapshot / confirmation の digest、epoch、activation、fencing、audit receipt の
  対応を検査する。Core の新 Profile ID や structural review JSON をそのまま返して
  同じ成功応答の代わりにしない。互換の表示・App ドメイン値と、実 Core の ID / 保存結果の
  拘束を App 側で明示し、Host で固定成功応答や架空の activation / audit を作らない。
- Core に `create_profile_with_native_review` を追加。既存 create と fresh-ID / parse を
  一つの private helper で共有し、元の構造定義・公開定義の受理順序を維持する。
  新入口は実 fresh ID / head / epoch から一度だけ native review と保存を行い、
  通常保存を先に呼ばない。既存 head または非ゼロ epoch の target は拒否する。
  元 Profile の保存状態を維持したまま別 ID を一回の承認で作るケースと、
  拒否時に新 revision / 権限を残さないケースを実 Core テストへ追加。
  ローカルでは format / source 確認のみ。CI と Setup からの接続は未確認。
- `bc1b9957e` の CI `41053399646` は両 job が成功。
  portable job `124054783704` で native 表示契約 7 件、Gate 76 件、実 Core の
  native review 12 件（上記 fresh creation の成功・拒否を含む）が通った。
  macOS job `124054783273` で実 frontend build と Tauri / core-host compile も通った。
  mock input を使う Core テストは native GUI 入力の証拠にはしない。
  初回 Setup の候補生成・実 GUI 操作・Session 起動・既存データ移行は未完了。

## 006: 初回 Setup のための並列 Core / 不要な snapshot 機構を増やさない

- 状態: **source 調査済み。製品接続は未対応**。
- 場所: Core の `begin_native_setup`、Gate の `NativeSetupInvocation::control`、
  [component preparation](../../../tobkiri_launcher/harness-native/src/preparation.rs)、
  [既存 bootstrap composition](../../../tobkiri_launcher/src-tauri/src/native_harness_bootstrap.rs)。
- 問題: legacy 側の別 Core や仮想的な Root controller を新設すると、実製品の root・
  account・deadline と分岐し、所有・終了処理が二重になる。
- 整理案: 実 core-host の同じ `Arc<Core>.begin_native_setup()` と、その invocation の
  実 `CoreControl` を既存 preparation へ渡す。カタログは原本 305 件を compiled resource に
  持つ自己完結 component なので、この呼び出しのための外部 file snapshot / private root
  lease bridge は不要。初期化・catalog 呼び出し・破棄まで元 invocation の予算を保持する。
- 維持・条件: `compile(File, expected Digest)` は bounded read / 実 SHA / 長さを検査するが、
  File の取得・path の許可・expected digest の信頼元を証明しない。
  `OriginalBootstrapInputs` は実 AppHandle / AppConfig / ShutdownState の生存確認で、
  artifact admission の代用ではない。metadata root を bytes 読み取り権限へ変えない。
  実 artifact の正規取得と digest provenance、実 Host の owner state、同じ engine / invocation /
  source owner を保持する consuming generic data-program handoff が未接続。
  App の catalog メソッドを Core の固定 NativeBootstrap binding 表へ追加しない。
- 根拠: Sol の限定 source 調査。既存 bootstrap は File を開かず caller から受け取る。
  実機・GUI・この製品 callsite の実行証拠は未取得。
- consuming `prepare_data_program` を既存 preparation へ追加。初期化専用の metadata root
  trait から owner の生存確認だけを抽出し、既存 bootstrap owner はそのまま利用する。
  新しい pure DATA handoff では root provider を要求せず、同じ engine / component /
  invocation から IO import のない一つの Store を作る。source owner を破棄する前に
  Store を retire し、呼び出し前後の失効は出力を返さず Store を恒久 retire する。
  fuel・deadline・capacity の維持と失効後の再利用拒否を、実 WAT component の 2 ケースへ追加。
  Sol の限定 source review は blocking issue なし。実 CI はこれからで、製品での
  artifact admission・App owner・Setup callsite を証明するものではない。
- `ceb86ea2d` の実 macOS CI `41057779834` / job `124069547358` が成功。
  preparation 4 件（追加した pure DATA の 2 件を含む）と library の strict Clippy が通った。
  実 WAT component と controlled source custody の検証であり、製品 artifact の
  read admission / digest の信頼元 / GUI owner が接続できた証拠にはしない。

## 007: CI の native compiler 依存を毎回 build し直す重複

- 状態: **GitHub 上の cache 手順を追加。cache hit と短縮量は未測定**。
- 場所: [private kernel probe workflow](../../../.github/workflows/issue-1520-schema-kernel-probe.yml)。
- 問題: Guest の差分調査ごとに同じ Core consumer と依存を最初から compile していた。
- 整理: Rust 1.94・OS・architecture・lock / manifest に拘束した native build cache を
  GitHub 内で復元し、成功した native build の直後に保存する。
  後続の Guest 検証が失敗しても、成功済みの compiler 出力を再利用できる。
- 維持: 毎回 `cargo test --locked --no-run` の実 artifact を選択し、実 binary の SHA と
  現 source head を記録する。Guest の instantiation / schema / 出力・実行結果は保存対象にせず、
  元の cold 実行・fuel・deadline・memory・smoke 条件を維持する。
  ローカルに target / registry cache を増やさない。
- 根拠: [公式 restore](https://github.com/actions/cache/blob/main/restore/README.md) /
  [save](https://github.com/actions/cache/blob/main/save/README.md) の別 step を使用。
  workflow の source 検査のみ。実 cache 再利用や性能改善の成功とは数えない。

## 新規項目のひな形

### NNN: 整理したい処理

- 状態: 未調査 / 提案 / 実装中 / 対応済み / 保留。
- 場所:
- 現在の問題:
- 整理案・減らせる実装:
- 維持する動作・着手／削除条件:
- 根拠・コミット・検証結果:

## 008: schema の停止 stack だけで次の移植対象を推測する重複

- 状態: **累積診断の実 CI 記録に成功。検証中のコストが主な残課題**。
- 場所: private schema builder と CI-only native DATA observer。
- 問題: 2f / 349 / 234 の機能比較は成功しても、Wasm は同じ 2B 上限で停止した。
  停止時の stack はその時点の位置であり、全体コストや改善幅の証明にならない。
- 整理: 明示 dispatch の診断 component のみ、同じ Store / account / component を
  保って準備・各 case・API・完了の最大 67 return 直後を観測する。元の累積 fuel・
  64MiB・最初の絶対 30 秒を更新しない。実 funded output は記録後、次 call 前に解放し、
  trap / 明示 failure 後は再開しない。cleanup の charged bytes 0 も必要。
- 原本の一括 App source は変更しない。通常 CI の完全 64 比較と API gate は必須。
  診断は別 component SHA / `qualification=false` で、最後に完了した case と次に実行中
  だった case を区別する。snapshot 時に schema / result を用意しない。
- 注意: 分割境界で参照寿命・GC・cache が変わるため、一括実行の上限値とも合格とも
  扱わない。Astra high の質問回答でこの境界と制約を確認した。実装対象の特定後は
  この一時診断が必要か再評価し、製品の起動フローや権限 API へ流用しない。

- source `2418b8edb` の通常 CI `41094612644` / job `124197028502` は native
  全 64 比較・15 API 群・Core / component build 後、Wasm の残燃料 0 / 2B で失敗。
  実 dispatch 診断 CI `41094625731` / job `124197075079` は記録・cleanup に成功し、
  準備 53,122,419 fuel、原本と一致した 7 件の後に残 400,836,291、次の
  `defaults_setup_v4.schema.json` の途中で累積 0 を観測した。API 群には未到達。
  期限超過はなく、合計約 1.411181439 秒。診断 component は別 SHA である。
- 次は native の呼出し profiling を補助測定として行い、検証中のどの Python 経路が
  残っているか絞る。native 時間は Wasm fuel の代用ではない。診断完了や一括合格を
  主張せず、変更後は同じ全 64 件・API と本来の一括 Wasm gate で再検証する。
- 補助測定は明示 dispatch `native_profile_only=true` で選択する。通常 push / 手動 default
  は元の全 gate を実行する。補助 mode は Core / Wasm を省略した事実を step に残し、
  native 全 64 比較の check_schema 呼出しだけを cProfile で計測する。
  観測 helper・metadata 準備・後続 API は計測外。元の全比較・15 API 群は必須のまま。
  native 呼出し列の測定は Python / C で計測 overhead が異なるため benchmark・Wasm fuel・
  一括合格とは扱わない。progress 診断との同時指定は拒否する。
- 補助 CI `41096568179` / job `124203753335`（source `486190619`）は native
  全 64 比較・15 API 群に成功し、Core / Wasm step を実際に skipped とした。
  64 check_schema の profile は 2,062,841 calls、参照 lookup 18,123 回・evolve
  39,112 回・type 38,182 回を観測した。計測下の合計 2.117 秒と入れ子の時間を
  足し合わせたり、Wasm 削減・計測なし benchmark として扱わない。
  同じ source の通常 CI `41096555198` は profile を無効に保ち、native 比較後に
  元の一括 Wasm 2B fuel で停止した。

## 009: Rust 移植時の固定属性名・引数名の反復文字列 allocation

- 状態: **固定属性名版の native 比較に成功。累積診断の同じ地点まで約 20.2% 減**。
- 場所: private schema kernel の lib / traversal / keywords。
- 原本 Python の LOAD_ATTR / keyword 名は code metadata として共有される一方、
  Rust から毎回 &str を渡すと PyO3 は一時 Python Unicode を生成する。native profile
  で多数回呼ばれる evolve / type / post-init と traversal の補助処理にこの重複がある。
- 整理: 110 箇所の固定 identifier 名に PyO3 の intern! を使い、実 lookup / callable /
  class / error / 順序は変更しない。schema・URI・caller 由来の文字列や判定結果は
  対象外。metadata は guest Store の memory 内に置き、Host 側で guest 参照を共有せず、
  初期化費用も元の冷間 fuel / memory / deadline に含める。
- identity は観測可能なため、元 post-init の co_names と push_scope literal、
  evolve の kwargs keys の実 object identity を既存 API 群に追加する。
  Astra high の回答に従い、単に同値だから完全互換とは扱わない。
- 削除・完了条件: 全 64 比較・15 API 群・元の一括 Wasm gate と分離した累積診断で
  実 compile / parity / fuel を測定する。改善がない場合は結果を記録して対象を見直す。

- source `45b1c4147` の通常 CI `41098227240` / job `124209443526` は実 compile・
  全 64 比較・15 API 群（文字列 identity 追加分を含む）に成功し、一括 Wasm は依然
  2B fuel で停止した。実累積診断 `41098272613` / job `124209608899` は 8 件完了、
  `distribution_v1` の途中で停止。同じ `contract_v4` return までの累積消費が
  1,599,163,709 → 1,276,375,494（約 20.2% 減）。一括合格・全体の進捗率ではない。
- 次の同型重複は type 比較 loop の 7 固定名と guard の 5 固定 key、private keyword
  helper の 4 固定 kind。比較順と未知・surrogate・custom dispatch を保ち、caller が
  schema に入れた文字列 object は共有対象から除く。元 keyword の実 kind literal と
  custom callback / schema error の caller object identity を追加で確認する。
  この追加分の実 CI と燃料効果は未確認。

- source `0e2dfed1a` は実 native 全 64 比較・15 API 群に成功。通常 CI
  `41100329636` / job `124216396038` は一括 2B fuel で失敗したが、累積診断
  `41100353558` / job `124216453220` は 9 件完了、次の executable_catalog_v4
  で停止した。同じ contract_v4 まで 1,243,994,756 fuel（元の診断から約 22.2% 減）。
  一括・全 catalog・製品の合格ではない。

## 010: 残存する URI 結合の Python 制御フロー

- 状態: **実 native 比較は成功したが、累積燃料が約 2.93% 増えたため不採用。速かった 0e へ復帰中**。
- 以下の移植内容・10 file / native counter / licensing 条件は不採用 prototype の履歴。
  現行 source の条件は末尾の復帰記録（8 file・元 Python URI 経路）に従う。
- native profile の 18,123 lookup / 15,840 urljoin が次候補の根拠。Astra high の
  質問回答に従い、まず実 CPython 3.13.16 urljoin の body のみを移し、型変換・
  parse / unparse・本来の cache・Resolver / Resource / Registry は元の実処理を使う。
- 元 function の globals / cached builtins / literal object を保持し、実 referencing
  alias のみ薄く包む。stdlib 関数は比較原本のまま残す。通常の positional str/bytes
  を native に渡し、custom 入力・引数 binding・元 code の変更は Rust 前に元 dispatch。
  元 defaults 変更を反映し、native 失敗からの再試行は追加しない。
- original slice の None field と二つの slice object、unpack の timing、例外 target と
  context、callback が書き換えた次の global helper、rich comparison を保つ。
  PyO3 の便宜的 eq/ne の identity 短絡を避け、元の比較式には rich_compare を使う。
- 16 件目の API 群で URI 境界、実 callback、source 定数の identity、変更される helper /
  config、実 cache と Registry の関連 object を比較する。64 件の直後の native
  urljoin attempt count も正である必要がある。全 stdlib API・細かな frame/refcount /
  finalization / peak memory の完全同値は未証明。元の一括 Wasm gate は必須。
- licensing: CPython v3.13.16 の実 LICENSE 13,809 byte を COPYING.python に保持。
  SHA-256 78b12c3a81360b357002334f0e70ea0e92eebf7a9b358805c03c48484945f3bb。
  法的 notice のみ追加し、依存 source tree・ZIP・binary はローカルに保存しない。
  source admission はこの notice と新 Rust module を含む閉じた 10 file に更新する。

- 実 CI `41106604032` / job `124237968178`（source `97eb93207`）は compile・
  全 64 比較・16 API 群に成功した。API 前の実 native urljoin は 15,840 回。
  Core / component build 後の一括 Wasm は残燃料 0 / 2B、約 1.419555938 秒で停止。
  元 deadline は保持され、期限超過・unreachable はない。燃料改善は未測定。
- 元 '/' constant object と実 slice field を callback で直接比較する追加観測を、
  97 の run 後に加えた。この追加分と累積診断の実 CI はまだ未確認。

- source `72fa58ca0` の通常 CI `41108293467` / job `124243776309` は追加の
  slash identity / slice field 観測を含む実 native 全 64 比較・16 API 群に成功。
  一括 Wasm は元の 2B fuel で停止。累積診断 `41108401129` / job `124244170322`
  は同じ contract_v4 まで 1,280,400,271 fuel を消費し、0e の 1,243,994,756
  より約 2.93% 増加、完了件数も 9 → 8。診断の実測に基づき単独 URI 移植は不採用。
- Astra high の回答に従い、active engine / adapter を `0e2dfed1a` に戻し、
  未使用 URI module / wrapper / native counter 条件と対応 notice を active source
  から削除する。prototype と license は Git history に一緒に残す。閉じた source
  admission は 8 file に戻す。16 件目の API 群は元 Python URI 経路の比較として保持。
  既存の 64 比較・API 群・元一括 gate は弱めず、復帰 source の新 CI で再確認する。

## 011: descend 呼び出しの一時 kwargs dictionary

- 状態: **source review で不採用。実 compile / 実行前に候補を撤去**。
- 場所: `tools/python313-producer/schema_kernel/src/keywords.rs` の descend helper。
- 整理案は、実 bound method を一度取得し、exact MethodType / FunctionType / 既知 code /
  同じ self と連続した引数 prefix の時だけ実 generator を positional で呼ぶものだった。
  body / live defaults / globals / closure は変更しない案でも、guard 自体に問題がある。
- Sol の source review により、exact function の `__code__` 取得も CPython の
  `object.__getattr__` audit event を発行することを確認。監査 hook は例外や状態変更を
  起こせるため、元呼出しにない event を追加できない。function identity のみでは
  変更可能な code を guard できず、PyFunction_GetCode は必要な Limited API の外。
- root は未コミット候補の metadata / counter / API17 integration / 未完 helper を撤去。
  active engine は復帰版 `811cb8bca` のまま。実行前の棄却であり、燃料改善や実 parity の
  成果は主張しない。既存 API16 と元 budget / ABI / 監査観測の条件を保持する。
- 次候補は `_validate_reference` → actual Resolver.lookup / _evolve の連続した body
  移植。actual Resource / Registry / Resolver / Resolved、取得 callback / scope /
  constructor / errors / lazy descend を保持し、urljoin は元 Python のままにする。
  既知 method の direct dispatch にも今回と同じ監査 event 問題がないか先に検討する。
  この構造的候補でも大幅削減が出なければ、現在の境界で元予算に到達する経路は未立証。

- 別案として、引数を positional に変更せず keyword 名・元の順序を保ったまま
  Stable ABI（3.12 以降）の PyObject_Vectorcall で実 method を一回呼ぶ候補を実装。
  PyO3 の公開 PyCallArgs は sealed で keyword 名 tuple を受け取れないため、最小の
  FFI 境界を使う。code guard / audit event / retry / 新 generator は追加しない。
  8 個の固定 keyword tuple は guest context の metadata のみで GC に含め、入力の
  4 pointer は同期呼出し中の実 owner から借用する。offset flag は使用しない。
- source review では pointer owner / reentrancy / 全呼出し順に blocking finding はない。
  専用 API17 は実元 handler との kwargs 順・arity・identity・descriptor・custom callable /
  code 変更・generator lifecycle・追加 code audit event がないことを比較する。
  prefixItems の元順は instance / schema / schema_path / path。元 v4.26.0 の実 source
  を RAM で確認し、候補とテスト期待値を揃えた。依存 source は filesystem 保存しない。
- 復帰版 `811cb8bca` 通常 CI `41112798825` / job `124259214054` は実 native
  全 64 比較・API16 に成功。一括 Wasm は元 2B fuel で停止し、deadline は保持。
  累積 CI `41112803950` / job `124259239999` は 9 件完了、次 executable_catalog_v4
  で停止。同じ contract_v4 まで 1,244,463,543 fuel。この新しい復帰版実測を比較基準に
  使う。URI API helper の metadata も冷間費用に含む。元一括・製品 gate は未合格。
