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

- 状態: **frontend native終了primitiveは実CI成功。元製品のowner接続・移行は未完**。
- 次の実接続は、同AppHandle/選択済みuser_dataから旧Authority contextを開き、実初期化・
  epoch読取・epoch record読取・closeを行う有限native操作（Sol6.1 xhigh実装中）。
  既存Coreの別approval DB/epochやmetadata getterを代用しない。DB不在枝は作成せず、
  完全なctor到達範囲が閉じるまで既存DBのnative採用を開かない。snapshot材料は旧App入力
  の別型であり、Core grant/承認完了へ変換しない。publication recovery/resolveは次区分。
- この移植での不要な拡大を防ぐ: SQLite C heap全体をguest64MiBへinterceptするallocatorは
  既存resource定義でも今回のAstra high再確認でも不要。既存native controlの接続/queue/
  concurrency/statement/pagecache bounds、実row/blob/crypto/diagnostic/返却の容量とcopy/
  保持、deadline/cancel/実connection/lock/Joinを維持する。guest適合とnativecontrol証拠を
  分け、未計測heapを新allowanceで埋めない。新しいaccount/frameworkは追加しない。
- 旧epochはarbitrary positive Python intを保持し、reason_digestはuntagged64hexも許す。
  一方、後続snapshotのprotocol canonicalは2**53-1超を拒否する。材料読取へこの拒否を
  前倒ししたり、独自digest writerで迂回したりしない。元の2readを1readへ畳まない。
- 原本比較準備`02313b516`の[CI41216815649](https://github.com/harupipipipi/tobkiri/actions/runs/41216815649)
  はLinux/macOSとも32ケース成功。別checkoutの原本1cf025f1e・CPython3.13.16と元lockの
  hash-pinned依存を使い、実AuthorityStore、実historical v1 fixture、元snapshot helperを
  実行。実v1 leaseがambiguous/revokedへ移行し、2read間の変更とlate canonical拒否も確認。
  Native比較はまだ未実行。artifactはsummary JSONのみ各1,383B、必要JSON15,833B保存。
  Source63fの最初のCIはhistorical guardで失敗。raw main DBだけの複製にはcommitted WALを
  失う不備があり、SQLite backup＋test connection実closeへ直して、assertを保ったまま成功。
  今後もDB/keyはCI一時領域のみ。Core control metadataと旧authority subtreeの観測範囲、
  raw SQLite page配置とlogical schema/commit stateを混同しない。
- 場所: [native_harness_bootstrap.rs](../../../tobkiri_launcher/src-tauri/src/native_harness_bootstrap.rs)、
  [core_host.rs](../../../tobkiri_launcher/src-tauri/src/core_host.rs)、
  [feature 分岐](../../../tobkiri_launcher/src-tauri/src/legacy.rs)。
- 問題: legacy 側の `NativeRuntimeState::adopt_core` と `prepare_original_component` に
  実 callsite がなく、別の core-host 経路に実 Core・Session・UiAttachment がある。
  両者の root も異なる。試作 API の存在だけでは製品接続にならない。
- 整理案（Astra highの2026-10-31回答）: 元run_launcherを製品composition rootに
  保ち、元AppConfig/ShutdownState/選択済みuser-data-dirからNativeRuntimeStateが
  RuntimeOwnerを一度だけ開く。実NativeHostとCoreへ移譲し、その同じArcCoreから
  LauncherCoreを作って一組で公開する。optional core_host::runへApp設定所有権を移したり、
  既定rootの別Coreを混ぜたりしない。Coreができてもpartial catalog/Profileを採用しない。
- 元のfeature分岐は相互排他的なTauriアプリであり、単に別Arcが二つある状態ではない。
  generic core-hostには元のAppConfig/ShutdownStateがなく、legacyの試作helperも実caller
  がない。Arc参照を差し替えるだけで元Setupへ接続したと呼ばない。
- 終了は元ShutdownStateで新規生成を止め、準備/guest/Session/reviewをcancel・drainして
  完了を確認する。NativeComponentPreparationはstage間のsource liveness確認はあるが、
  同期compile中の終了完了や進行中Storeのcancel接続は別に必要。Arc dropだけでは不足。
- 最初の実装: generic LauncherCoreのnative終了primitiveを整理中。UI Stopのcaller guardと
  再起動を保ち、Native Exitでは別の単調shutdown latchで新規受付と有効なSession世代の公開を拒否する。
  既に開始済みの処理が競合しても、その同じSessionは後続drainで終了を待つ。
  実owned Sessionの元retirement/drainを待ち、合成HostCallerでUI経路を呼ばない。
  optional実HostのExit callbackに接続済み。`eff7d8dd7` のCI41153941066でcore-host全73件
  （新規2件含む）/Gate76件/Profile承認12件が成功し、実macOS Hostのコンパイルも成功。
  元run_launcherとのowner一組化・native準備/reviewの全終了はまだ未接続であり、
  このprimitiveだけで製品統合とは呼ばない。
- 次の小区分: native準備からStoreへ同じCoreControlで資金を確保したキャンセル子を
  引き継ぐ。外部account/inert・既キャンセル親は拒否し、Storeの単一claim・元2B/64MiB/
  絶対期限・既存2秒graceは変更しない。同期compileは前後で確認するだけで、途中停止や
  worker完了を偽装しない。準備消費後も子が親を保持し、子のcancelは親へ逆流しない。
  late initialization成功を返す直前のcancelは実finishを待ち、cancelとcleanup故障を両方
  保持する。失敗を後発cancelで上書きしない。実CIの結果は下記。
- この小区分の回帰は実Storeの親生成race/外部親/既cancel拒否/子のみcancel/元graceでの
  guest停止とpayload解放、および実macOS準備のcompile前/引継ぎ前拒否・消費後cancelと
  source寿命。旧fullharnessのpreparation期待件数が2のままだったため実5件へ更新する。
  Store用API重複は既存innerへ集約し、bootstrapも単一prepare_innerを共有する。
  元ShutdownStateの親生成・worker join・Core owner一組化・GUI承認への接続は別途未実装。
- `e2e764869`実CI41156475997: Linux Store全16件成功（実guestは元2秒graceで停止、
  deadline/fuel保持・payload0）。mac準備は4/5成功。DATA結果の新規割当は既存ownerが
  cancel直後から拒否するため、「grace内の呼出し成功」を期待したテストが誤りだった。
  製品の拒否を緩めず、実result-storage/canonical割当拒否と同Store退役を検査する。
  これはepoch graceの変更ではない。
- 修正`a938d9dd0`のCI41157332132は全成功。Linux実Store16件、macOS実RuntimeOwnerの
  準備/引継ぎ5件、harness-native libraryのClippy -D warningsを通過。消費済み準備の
  親cancel/drop後に実DATA呼出しが既存result-storage割当拒否で停止し、同Store退役と
  source保持・physical drop後payload0を確認。証明はlibrary/機構の範囲であり、元製品の
  owner一組化・GUI Setup・全終了や全20条件の達成に広げない。

- 元launcherの終了接続を実装済み（Astra high設計、Sol6.1 xhigh担当）。807と後続finite
  38fの実Launcher Rust工程は成功。元GUI/製品adoptionは未完。
  変更前のstateはprepare/adoptのproduction callerがなくjob/cancel/Joinを保持しなかった。
  実Setupのprepare/adopt呼出しはまだ未接続のまま、既存owner時も
  ShutdownFlagを確認し、owner生成中からjobを登録、同invocation由来のcancelをEngine前に
  登録する。workerがcompile/init/実処理/finish/actualJoinまで所有し、外へはdetached data
  のみ返す。caller dropでも追跡を残す。stopはadmission閉鎖→cancel→lock外でfinish/Join→
  実Sessionがあれば既存close→owner解放/Closed。timeoutはStoppingとowner/Joinを保持し、
  元signal/requestExit/setupFailure/Exitfallbackへ接続し、強制終了を正常完了と記録しない。
  RuntimeOwner.close/Core.closeは存在せず仮定しない。実Setup・qualified artifact・GUI
  採用は別途未完で、このlifecycle sliceを全体完成へ読み替えない。
- この小区分のsource reviewで見つかった整理点: owner slotを空にしただけでは、別の
  stop callerへ解放完了を返せない。実destructorを一度だけ実行し、同じ完了を待つ。
  また元Defaultspack/kernelのstopエラーをログだけで捨てると、新しいClosed表示が
  cleanup成功を誤って表す。元stop API・同時実行・watchdogは保ち、実Joinと全stop結果を
  一つの終了結果へ集約した。Forcedは後発successでClosedへ戻さない。
- 返却境界の修正: `NativeDataError`のDebugは各bufferの長さしか含まず、String化は
  実code/message/detailsを失う。外側errorにも元失敗・cleanup失敗・funded diagnosticが
  ある。単なるログ文字列へ置換せず、実DATAと失敗の関係を保持し、Bootstrap/Storeや
  未追跡のaccount leaseをworker外へ逃がさない返却契約へ修正する。Astra highの回答は、
  domain/initialization/execution/cleanupを分けたdetached構造化failureを選択し、任意の
  anyhow受入れをやめて明示投影できるjob error型へ狭める。実bufferのencodingを保ち、
  初期化のdiagnostic/trace/observationsと元失敗・cleanup失敗の別枝を保持する。worker内で
  detached化→finish→lease保持診断の解放→結果送信→実Joinとsource終了を確認する。
  無制限chain収集・未知errorの自動String化・無期限error保持は追加しない。detached化の
  失敗も明示し、成功や切詰めへ変えない。source実装済み、実Setup採用は引き続き未完。
  現public APIのanyhow context内に隠れた非公開engine error等は、全型投影の証明がない。
  新workerはstage付きUnsupportedSourceとして明示拒否する方針であり、元errorの完全な
  同等性を達成したとは数えない。実Setup採用前には必要なsource生成点で型付き契約を
  接続する必要がある。cleanup失敗時は公開可能な元成功DATAも元failureも別枝で保持する。
  失効した成功DATAはcleanupの有無によらずworker内で捨て、拒否metadataだけを残す。
  `complete`でこの順序を一元化し、ログへraw DATAを新規展開しない。
- 既存libraryのinit domain拒否も、finish失敗時に元DATAを捨てていたため、
  `BootstrapDomainCleanupFailure`で実domain DATAと実cleanup errorを別fieldに保持した。
  Launcher追加18件、library追加2件の回帰sourceを用意した。libraryの2件は実小componentの
  Domainとhelperへ注入したcleanup errorを使う検査であり、実IO故障や元GUI/fullguestの
  成功証明ではない。Source parse/format/TOML/diff確認のみ実施、実行結果はCI待ち。
- source `1604abe09`の[CI41192972944](https://github.com/harupipipipi/tobkiri/actions/runs/41192972944)
  ではLinux実Store16件とmac準備5件は成功。追加domain2件はcomponent実行前に
  `Not a directory (os error 20)`で失敗し、Clippyは未実行だった。既存準備testは
  所有tempdirのrealpathを使う一方、新fixtureはmacOSのsymlink祖先を含み得る一時pathを
  そのままNoFollowへ渡していた。4箇所のfixtureを既存方式へ合わせた。製品のNoFollow・
  権限・assertion・検査件数は変更しない。修正後の実CI結果は別途記録する。
- fixture修正`807437c0a`の[CI41193520451](https://github.com/harupipipipi/tobkiri/actions/runs/41193520451)
  は全成功。Linux実Store16、mac準備5、実小component Domain/helper cleanup2、
  harness-native library Clippy `-D warnings`が通過した。Launcher追加18件と元GUIを
  これで検証したとは数えない。修正済みsourceのDesktop CI41194312852は別途進行する。
- 次の整理（Astra high判断、Sol6.1 xhigh担当）: HRTBでBootstrapのborrowを縛っても、
  callbackがbudgetやfunded結果を外部channelへcloneすることまでは防げない。今回の
  worker自身の保持物・実Joinの証明と、全callerのlease非escapeを区別する。製品入口を
  有限操作dispatcherへ絞り、現boundの3 original methods＋describeだけを同Store内で
  実行し、中間reference/fundingを内部保持する。任意Fは内部/testへ閉じ、未bound操作は
  実行前のintegration failureにする。元source/WITは現在runtime19＋init/transport7で、
  capture・binding・実検証を分けて記録する。19/7全対応やSetup採用は未完のまま。
- 有限dispatcherのsource実装が完了。通常入口は3 getterの単体選択と同Store内の実describe。
  actual getter resultのscope/rootを元CoreValue codecで接続し、descriptionを1回だけ
  元accountで予約してcopyし、String/Valueへmoveする。再import・外部rawref・slot・F入力を
  除去し、固定3列はtest限定にした。F廃止で不要になったWorkerFailure/SourceError/worker
  projection scaffoldを削除し、init Domainはdomain_dataへ直接集約した。
  追加9件/10回の小component実行とunbound22件の事前拒否を検査するsourceを用意した。
  Source parse/format/diff確認に加え、下記38f CIで追加9件が成功。cancelは完了descriptionの
  late publication拒否でありVMのcancel trapではなく、finish faultはSource destructor panic
  であり実IO故障ではない。元Python/GUI/全19＋7の資格確認へ読み替えない。
  テスト用watはroot lockの既知1.252.0を使い、独立lockへ既知checksumの3packageのみ追加、
  旧version/checksumは保持した。実`--locked` resolver確認は次のLauncher CIで行う。
- 依存解決だけを先行確認するため、既存native-data-handoff workflowに明示選択の
  `launcher-lock` modeを追加した。通常push/default dispatchの実Store/preparation/domain/
  Clippyはそのまま実行する。lock modeはmacOS用の実依存graphを`cargo metadata --locked`
  で解決し、両lockが不変であることだけを確認する。build.rs・型・ABI・実行を検査せず、
  Launcher本体やGUIの合格には数えない。実Desktop CIの代わりにはしない。
- `807437c0a`の[Desktop CI41194312852](https://github.com/harupipipipi/tobkiri/actions/runs/41194312852)
  では実Launcher Rust工程が成功。library626件（新lifecycle18件を含む）、build-script
  integration123件、standalone runtime-resource18件が全成功した。その後の配布工程は
  最新finite sourceのCI41197404494へ切り替えて中止したため、installer完成とは数えない。
  finite追加9件はこの807実行に含まれない。
- `38f856e6a`の[lock専用CI41197126669](https://github.com/harupipipipi/tobkiri/actions/runs/41197126669)
  は成功し、実macOS dependency graphの`--locked`解決と両lockの不変を確認した。
  これは有限dispatcherのコンパイル・ABI・実行を検証した結果ではない。
- 同じ`38f856e6a`の[Desktop CI41197404494](https://github.com/harupipipipi/tobkiri/actions/runs/41197404494)
  は全工程成功。実Launcher library635件（finite追加9件を個別ログで確認）、
  build-script123件、standalone runtime-resource18件が全成功・skip0だった。
  既存配布経路のsealed Python、CI/E2Eアプリcold boot（broker/kernel/panel到達）、
  非公開CI用DMG作成・マウント後のpackaged Python検証も成功した。281,595,524Bの
  artifactはmetadataだけ確認し、ローカルへdownloadしていない。この配布経路には
  製品Python/PackVMが残り、有限操作の元Setup採用・全19/7・承認/settings/chat・1B/API・
  全839/20条件の完了証明ではない。
- 次の有限操作は全ABI wrapperの先行追加ではなく、元Setup callerの一単位を選ぶ
  （Astra high回答）。Rootが元`_setup_listing`/`prepare_bootstrap_profile_review`/
  resolution/recovery/candidate selectorを確認し、同じcatalog/resolved/confirmationの
  実object受渡し、追加source/active closure/predecessor、kwargs順を責任mapへ記録した。
  review前のpublication recoveryには実書込みがあり、単なる表示計算としてRustへ
  移してはいけない。元の権限・currentness・native IO境界を閉じるまでは新しい操作列や
  外部description再importで代替しない。実Setup採用と元guest2B内の完走は依然未達。

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

## 012: function identity による body 迂回と同じ function の code 変更

- 状態: **公開定義版は実 native 64 / API18 に成功。一括 fuel は未達。固定 global 名の追加は CI 未確認**。
- 場所: `schema_kernel/src/traversal.rs` の builtin handler identity dispatch と
  `schema_kernel/src/lib.rs` の default TypeChecker method identity guard。
- 原本と同じ function object でも `__code__` は変更可能。現在の keyword dispatch は
  function identity が一致すると実 callable を呼ばず Rust body を選ぶ。type guard も
  checker method identity が変わらない code 変更を検出しない。既存比較は callable
  の置換や descend の code 変更を扱うが、この二つの same-object 変更は未検証。
  64 比較 / API17 合格が得られても、この契約の全体 parity を証明したことにはならない。
- Astra high の回答: 公開 callable の本体を薄い Python wrapper + Rust body に接続し、
  traversal は原本と同じ時点で選ばれた実 callable を必ず呼ぶ。wrapper の code 変更や
  descriptor / custom replacement は通常 dispatch に任せる。code getter を guard に
  追加したり、identity だけで Rust body に直接接続して override を迂回しない。
- 既存参照: module alias や VALIDATORS の値を置換するだけでは、作成済み Validator の
  選択済み handler や保存済み alias は更新されない。各実 alias を実行した結果・副作用を
  比較し、未接続経路を native 化済みと報告しない。元 Python alias は失敗後の retry に
  使わず、その alias の通常実行経路として保持する。
- 必要な比較: 同じ builtin / checker function の code 変更、変更前に保存した alias、
  作成済み Validator、開始済み generator と変更後の新規 call、custom replacement、
  audit event 列。開始済み generator を新しい code に切り替えてはいけない。
- 実験 kernel は製品に接続されていない。現在実行中の vectorcall CI は既存範囲の
  native 比較・元予算測定であり、この未検証契約の修正や製品合格ではない。
  crossing 費用が増えても製品採用前の修正は必要。性能未達を理由に gap を採用しない。
- 参照解決 body 移植でも同じ原則を用い、actual `_validate_reference` → lookup /
  _evolve は通常の実 method 呼出しを保つ。直接 Rust-to-Rust 接続案は採用しない。

- source `fbe042701` は実 native 全 64 比較・API17 に成功（通常 CI `41114239126` /
  job `124264147903`）。一括は元 2B fuel で停止し deadline は保持。累積診断
  `41114246891` / job `124264186051` は同じ contract_v4 まで 1,242,952,199 fuel、
  復帰版の 1,244,463,543 から約 0.12% 減、9 件完了のまま。効果は小さく、新構成の
  成果に引き継がない。API17 成功も今回の code 変更契約を証明するものではない。
- Astra high に設置方法を確認し、C: build 時の既知 source 翻訳を選択。runtime の
  module alias / VALIDATORS 差替えは install 前の code 変更を上書きし得るため不採用。
  function.__code__ の書換えも新 audit / 保存 alias の動作変更を生むので不採用。
- `schema_kernel_translate.py` は元 v4.26.0 `_keywords.py` の SHA-256
  afcfc3aea01f9fa40bc109e65c4820bde89253e51a20bda9da7b8f20d7a57c57 と全 36 定義・
  signature・generator の閉じた形を build 時に確認し、body だけを公開 generator の
  Rust 接続へ変換する。元 14,949 bytes → 7,022 bytes（AST/data 確認のみ）。
  元 package は書き換えず native candidate copy / guest capture を派生させ、完全な
  元 source を独立 oracle module として残す。guest に増えた code / data index も
  元の 64MiB capture bound に含める。原本と派生 source の hash を receipt に分ける。
- traversal の handler identity table / delegate / class ごとの keyword 名 table と、
  lib の fast_is_type / type counter / adapter wrapper を削除。選択済み実 callable を
  常に呼び、Rust body は公開関数が自ら呼ぶ。標準 class / VALIDATORS は通常 import で
  公開定義を参照する。default checker / predicate は元実 callable を実行する。
- source review で、新たな公開 table 引数を任意 tuple にすると C vectorcall の
  kwnames / stack 長を偽造できることを検出。opaque KeywordNames Rust class に変更し、
  Python から constructor / fields / subclass を公開しない。GC に rows を含め、実 tuple
  を clone して borrow を解放してから callback を呼ぶ。API18 で偽 tuple を拒否する。
- 独立 oracle import の ImportError を候補側へ fallback すると自己比較になり得るため、
  translated marker がある場合は oracle import を必須にした。元の単独 oracle 実行だけ
  は元 module を使う。元 17 群を残した上で第 18 群を追加し、64 比較後・API 前の実
  public Rust body entry count も正であることを要求する。
- 現時点は AST / rustfmt / source review のみ。新構成の実 compile・全 64 / API18・
  一括 Wasm・燃料・全 catalog・製品の合格は未確認。元予算や機能を変更しない。

- 次の限定的な allocation 重複は traversal の live global / builtins lookup に残る
  4 個の固定名。`_REMOTE_WARNING_REGISTRY` / `SPECIFICATIONS` / `getattr` /
  `validator_for` を元 LOAD_GLOBAL の有限 code metadata として共有する。辞書の実値や
  constructor / checker / resolver を cache せず、globals → captured builtins の順、
  missing NameError と実 `.name` を保持する。caller / schema 文字列は intern しない。
  既存 API18 の live namespace / builtin fallback / NameError / callback 比較を残し、
  新しい private 反射を必須条件へ追加しない。この変更の compile / 効果は未確認。


## 013: 追加した private 反射仮説を元の製品要件へ拡大しない

- 状態: **出荷 caller / SDK / issue と18群の対応を整理し、実装前に検査を分離中**。
- 根拠: `schema-port-scope.md` の具体的な呼出し元、公式4.26 protocol、現在の原本unit
  inventory。839はunit数でありtest件数ではない。限定検索の未発見だけで除外はしない。
- Astra high はprivate builtin のPython function内部構造を一律MUSTとした前提を修正。
  公開 callable / callback / checker / resolver / errors / generator の機能契約は残す。
- 必須第18群は callback delegation / 保存alias / handler選択 / opaque FFI拒否を検査。
  元の code改変 / metadata / audit観測は3件の独立diagnosticとしてすべて保持し、結果・理由を
  出す。非Python callable を passed と偽らない。復旧失敗・未知BaseExceptionはfatal。
  record欠落を拒否し、同じinvocation内で計測。元64 / 必須18 / 全catalog / 予算は維持。
- source `534b2619a` は実native64/API18成功。一括Wasmは元2Bで停止。累積診断は10件完了、
  同じcontract_v4まで1,142,086,379 fuel（直前から約3.45%減）。native-only profileでは
  _coerce_argsが124,923回。次の候補はその短いbodyのみで、lookup helper抽出は保留。
  実constructor / URI parser / caches / decode helper は使い続け、新候補の性能は未証明。

## 014: 共有 URL 引数 helper の短い Python frame を限定的に置換する

- 状態: **候補却下。637 engineへ復帰し、元Python helperのAPI19回帰検査だけを残す**。
- 場所: `schema_kernel/src/coerce_args.rs`、`schema_stdlib_translate.py`、
  native checker / fresh worker。native profile の実124,923呼出しを根拠とする候補。
  URI parser、decoder、result coercer、標準 urlsplit cache は元実装を使い続ける。
- CPython3.13.16 `urllib/parse.py` 全体の既知 hash と元 helper 形を照合し、最初の
  import 前にだけ source を派生させる。元 helper body と alias は独立比較用に保持。
  引数・戻り値の実 object、globals と function が捕捉した builtins、truth / 比較の順、
  decoder callback 後の encoder lookup を保持する。引数なし / keyword は評価前に
  元 helper へ通常 dispatch し、native error 後の retry は行わない。GC が捕捉参照を辿る。
- 元 Python helper と6個の共有 URL API、bytes / 混合型 / falsey / str subclass /
  callback / exception / cache identity を第19群で比較する。既存64件・18群は残す。
  private function reflection や traceback frame の完全一致は別の未立証範囲であり、
  native callable を Python function と偽装しない。
- native parent の Path / tooling import が先に urllib.parse を読む可能性を避け、
  `-I -S -B` の新規 child で extension 認証・import と sys.modules の実不在を確認。
  guest も kernel 前後で不在を検査し、loader / tooling より先に派生 module を普通に
  import する。既ロード module の reload / 上書きで検査を通さない。
- ソース監査で capture 一覧に urllib source がないことを発見し、認証済み CPython
  archive から `urllib/__init__.py` と `urllib/parse.py` を両方の閉じた一覧へ追加。
  元4MiB stdlib選択 / 64MiB全capture / 2B fuel / 30秒 / 4MiB envelopeは増やさない。
  COPYING.python のみを復元し、却下済み URL parser Rust module は復元しない。
- 簡素化候補: stdlib translator と keyword translator の literal index 読書きが重複。
  実 capture 検証後に閉じた小 helper を共有可能。現段階で orchestration は広く変更しない。
- 実測比較基準は `63750e485`。native64 / 必須18群 / 追加3診断は成功、一括Wasmは
  元2B fuelで失敗。累積診断の同じ contract_v4 は1,146,973,884 fuel、10件完了。
  直前534より約0.43%増えており、古い1,142,086,379を今回の基準に使わない。
  新候補も一括 / 全catalog / 製品gateを通るまで移植完了にはしない。
- 同じ637の全5Pack再実行 `41128486562` は全215件を実行。元Python209一致、
  Rust212一致で既知の差が再現。共有code整合3件、ACK1件、supervisor2件、diagnostics2件、
  別のouter admission1件は成功。outer入力3件の差をguest一致に数えず、製品合格にしない。
- `7bb772eed` の通常CI `41130856110` / job `124321074616` と累積CI
  `41130869823` / job `124321129909` は、実native64/API19・追加診断に成功。
  両方で比較中のnative coerce entryは124,923回。しかしguestの初回import先には派生
  markerがなく検査で停止した。選ばれたoriginは旧ログに未記録。component生成前であり、
  Core実行・燃料値はない。
- Astra high の回答に従い、sys.pathだけの優先化ではなく、認証済みの2moduleを供給する
  import-free bootstrap finderを追加。既ロードの親packageも拒否し、reloadはしない。
  compile / exec / bookkeepingは既存loaderから最小共通helperへ切り出す。変更はprivate
  kernel用の派生copyだけへ適用し、共通baseline loaderや他の実験の形を変更しない。
  通常finderの設置後に同じsource/root/実loader ownerを確認してbootstrap finderを退役。
  新しいfresh-process検査はambient pathと無関係のsource選択・相対import・metadata・
  再import identity・reload/既ロード拒否をCIで確認する。新修正の実行結果は未取得。
- 修正 `934d745e6` の通常CI `41132421203` / job `124326400604` はfresh-process
  source検査・実native64/API19・guest生成を通過し、実Core呼出しに到達した。一括は
  元2B fuelを消費して停止（残0、0.813519294秒、絶対期限保持、deadline/unreachableなし）。
  Nativeの実coerce entryは124,923回。Wasm API19段階にはまだ到達していない。
- 同sourceの累積CI `41132437439` / job `124326464149` は診断記録に成功。完了10件、
  次のexternal_normal_pack_catalog_v4で停止。同じcontract_v4まで1,138,176,725 fuel、
  637の1,146,973,884より約0.77%減。preparedは48,704,484。helper/source選択修正の
  cold metadataも含む小さい差であり、coerce body単独の速度や全catalog上限の証明ではない。
  qualification=falseを保持し、この小幅改善だけで次の推測移植へ進まない。
- Astra high は約0.77%のcold metadata込み総差・完了件数不変では追加コードの保守負担を
  正当化できないと回答。Rust coerce、stdlib派生/専用bootstrap、fresh childを撤去し、
  engine・adapter・source一覧を637へ戻す。不要になったPython noticeも実装と一緒に
  Git履歴へ残す。共通baseline loaderは元から無変更。API19は元標準helperのcoverage
  として残し、native化の証拠とは呼ばない。元637と新19群harnessの違いを明示する。
  削除を完了判定や、未合格の一括/全catalog gateの代わりにしない。
- 復帰 `545c94848` の通常CI `41134448985` / job `124333302161` は実native64/API19と
  追加診断に成功。一括は元2Bで停止（1.469427295秒、絶対期限保持）。累積CI
  `41134467895` / job `124333375439` は10件完了、同じcontract_v4まで1,145,428,872 fuel。
  extension SHAは637と同じ4304343c…2e53。元637をengine性能参照とし、新19群harnessの
  cold metadata差は別に記録する。これをengineの性能改善と数えない。

## 015: 推測移植を増やす前の実Wasm費用分類

- 状態: **同生成物の対照・6件/4件の検査に成功。全12checkpointのfuel一致。stack採取なし**。
- 場所: `crates/tobkiri-wasm/src/native_setup_store.rs` の既存epoch callback。
  Astra high はnative CPUのcallee込み時間だけで次の移植を選ばず、実Wasmで費用分類を
  先に行うよう回答。従来のreturn間fuel snapshotでは内部関数への配賦はできない。
- Solの限定監査: callbackの同一thread / 排他的StoreContextMutで `get_fuel` と
  `WasmBacktrace::capture` を使える。GuestProfiler / call-hook追加featureは不要。
  元check_epochとYield(1)の結果をそのまま返し、既存の絶対Instant期限・fuelを変えない。
- 次の実装候補はcfg(test)限定のbounded host sample。callbackで重い整形/I/Oをせず、
  固定sample数・記録容量と所有fundingを守る。計測なしとの擾乱比較が前提。
  Wasm framesからPython callframeは分からないため、その内訳は未分類とする。
  sample間fuel差分は推定配賦であり厳密な関数別燃料とは呼ばない。
  今回のsource選択修正とは混ぜず、coerce候補の実測を得てから判断する。
- 上記実測後は637 engineへ復帰。次の計測を先行する。公開backtrace APIは表示上限20の
  前に全runtime frameをVecへ集め、関数名/DWARF文字列も複製するため、出力上限128件
  だけでは一時領域を制限できないことを確認。sourceに基づくstack/activation/Vec成長/
  symbol展開量の上限が必要。Astra highは未立証の4MiB定数等で進めず、暫定的に
  backtraceなしのfuel/time固定容量記録でepoch観測費用を確認してもよいと回答。
  その暫定記録は関数分類の代わりにはならない。現時点でfull captureは実装しない。
- 暫定実装: `native_epoch_observation.rs` はactual CoreControlへ固定128件の数値表と
  serialization allowanceを前払いし、最後のhandle破棄まで保持する。actual Storeの
  既存epoch callbackがget_fuel/timeを記録してから元check_epochをそのまま返す。
  fuel補充・新Store・追加guest call・追加import・絶対期限更新は行わない。
  scopeはbindingからStore退役まで（prepareを含む）。通常製品にはコンパイルされない。
- explicit progress診断だけがopt-inでき、普通の一括は無変更。Storeをtrap後に破棄しても
  observer cloneが元fundingを保持し、借用serialize後に返す。overflow・fuel欠落/増加・
  deadline変更・poisonを明示し、閉じた型/件数/単調性検査が不正な記録を拒否する。
  helperの6件とmetadata拒否4件をCIで実行し、同headの観測なし/ありのcheckpointと
  component identityを比較する。関数分類や燃料上限合格へ読み替えない。
- full stackの限定ソース調査: 表示上限20の前のraw frame Vecとname複製は既存Store
  予約に自動で含まれない。単一activationはこの引数なし/string結果/4同期clockのみの
  schema経路に限り説明できる。512KiB設定にはslopがあり、実fiber範囲、Vec倍増時の
  old/new重複、名前上限、DWARF無効化、保持表とserializationの合計を別に証明する必要が
  ある。8MiB等は条件付き案であり未実装・未立証。IO/custom imports/resourcesへ一般化しない。
- `be52c13dc` は実compile・funding/cap/refundの6件・metadata拒否4件に成功。
  通常CI `41135847546` / job `124337969991` はnative64/API19成功後、一括が元2Bで
  停止（2.102424309秒、絶対期限保持）。無観測累積 `41135860522` / job `124338027636`
  と観測累積 `41135864278` / job `124338033712` は両方10件完了。観測は173ticks中
  128件を保持しoverflow=true、記録時のfuel欠落/増加・deadline変更・poisonはfalse。
  これはtick間や未保持の数値列の完全な証明ではなく、関数分類でもない。
- この2実行はconsumer SHAは同じだが、別々に生成したcomponent SHAが異なった。
  contract_v4の1,144,652,688と1,148,013,732の差をobserver costとは扱わない。
  Astra highの回答に従い、観測モードはcomponentを一度だけ生成し、同じconsumer・
  VM設定・入力/順序で独立control/observed legを実行する形へ修正。各leg内はfreshな
  Core/account/Store一つと元予算一回だけで、trap後に続けない。予定67callと実完了数を
  分け、独立receiptを保存する。2legを一回の資格試験や共通予算とは呼ばない。
  同artifactでもhost温存状態・順序等の差は残り、単一ペアの差を因果的費用と断定しない。

- 同生成物pair修正 `6356ef6af` のCI `41137708044` / job `124344148871` は成功。
  component 3daac2ef…c19 / consumer 8e641c0f…26e9 が両legで同一、全12の
  phase/case/fuel tupleが一致。prepared 53,630,028、contract_v4 1,144,124,292、
  完了10件・次のexternal_normal_pack_catalogで元2B停止。観測は210ticks中128件保持、
  overflow=true、fuel欠落/増加・deadline変更・poison=false。保持外の詳細や関数内訳は
  分からない。実時間差は因果的なobserver overhead/高速化と呼ばない。
  通常CI `41137697573` / job `124344108862` もnative64/API19成功後、元2Bで停止
  （1.337424751秒、残0、絶対期限保持）。全catalog/一括の上限合格は未達。

## 016: App schema呼出し契約を先に固定する

- 状態: **追加native differential oracleの実CI一致。置換実装・Wasm/GUI検証は未完**。
- 場所: `tobkiri_protocol/validation.py:128-177`、詳細は
  [App schema caller contract](app-schema-call-contract.md)。
- Astra highはWasm frameだけでPython handler費用が分からないため、追加stack実装より
  actual load_schema→live selected check_schemaの契約を先に観測するよう回答。
  META_SCHEMAのidentityだけで不変扱いするcompiled planやdispatch省略は承認されていない。
- 追加oracleはpinned original/currentを別fresh processで動かし、同じ実file fixture、
  実305entry/453read/45cold call、42resource、strict parse・選択callback・例外cause、
  mutable identity・alias/filename/keyword cache・独立storeを比較する。戻り値の再帰的な
  key順と実parser例外のcause identityも保持する。checkerの戻り値は捨てる元挙動を検査。
- 元64 library vector/一括gateは別のまま。新oracleはnative caller契約だけで、captured
  owner/実Wasm予算/GUI/OS承認の証明ではない。短いApp用checker・META compiled planは
  まだ実装しない。将来重複を減らす場合も元dispatch/constructor/例外/可変objectを守り、
  plan作成費用を元invocation予算に含める。native Python削除の前提は移植後の全機能同等性。

- `54a85d415` の追加CI `41145870815` / job `124370717563` は成功。
  元54e/currentのfresh native laneで全37case・42resource・全305entry/453readが一致。
  実schema cacheは45miss/302hit/45retained。5collectionは153packs/1base/2shells/
  1profile/148executable catalogs。37件には追加のparse故障・失敗後retryを含む。
  artifact13019919970（26,083 bytes）をRAMだけで検査し、bounded JSON receiptのみ保存。
  新checker/compiled plan、captured-ownerや実Wasm上限、GUI合格へは読み替えない。

- 次sliceはAstra highの指示通り、同じ既存637 kernelの第三native laneを追加する。
  先に元64/API19 precheckを通した実extension/source/派生library閉包を照合し、fresh
  processで初回importから元のsource翻訳・adapterを接続する。全catalog/42resource/
  caller caseそれぞれの実keyword entry増加を要求し、黙示的な元library選択を防ぐ。
  新しい最適化は足さず、同じ37case/45miss302hit/305entry453readを元laneと比較する。
  第三laneの実CI・続くWasm実測は未完。compiled META planはまだ承認・実装しない。

- 第三lane `1d4b406ea` のCI `41147858083` / job `124377059142` は実compileと
  native64/API19を通過し、Rust接続後の全37case/42resource/全catalogも元実装と一致。
  実Rust keyword entryはcatalog前0→catalog後158,593→42resource後217,373→
  caller後217,932。source/extension/派生library閉包・実import先を照合し、原版library
  だけの比較でないことを確認。artifact13021598114（42,641bytes）をRAM検査し、
  bounded receiptのみ保存。これはnative上の既存kernel接続証明であり、実Wasmの
  同一予算達成・製品への採用ではない。次は既存bundle catalog App source経路の
  独立Wasm実測設計をAstra highへ照会中。元64 gateや全製品要件は未完のまま。

- Astra highの次設計回答: private64 probeへApp閉包を再構成せず、実bundle-catalog capture
  から明示的に別のRust派生nativeDATA artifactを作る。元42resource/305entry453read/
  実owner/accessor/adapter/entryを維持し、kernel・adapter・必要なkeyword翻訳だけを
  圧縮前に追加する。元SHA/翻訳対象/派生SHAを区別し、初回import前後を検査、reloadや
  原版fallbackで修復しない。preinitはmodule初期化だけで、schema検証・App cache・
  catalog結果を前倒ししない。未変更のfullcatalog処理を一度、同じStoreの元2B/64MiB/
  絶対30秒で呼ぶ。実Wasmのロード元・呼出し中Rust到達・resource実access・意味一致・
  fuel/deadline/Store終了のreceiptが必要。観測費用も同予算に含める。別coldstore/
  negativeは独立実験で、元nativeDATA/ordinaryPack/generic64 gateを維持する。
  ここは未実装の次sliceであり、native第三lane成功をWasmやGUIの証拠にしない。


## 017: 実App catalog由来のRust/Wasm計測

- 状態: **実Rust/Wasm経路へ到達。元2Bで停止、期限・Store退役・解放確認。全catalog未完**。
- 場所: `build_schema_kernel_extension.py`、`bundle_catalog_kernel_inputs.py`、
  `bundle_catalog/kernel_runtime.py`、`bundle_catalog_kernel_ci.rs`。
- 整理方針: 既存bundle catalogのcapture/adapter/entryを再利用し、private64 probeへ
  App依存閉包を複製しない。新しいPack/製品承認操作やHostのdomain処理も追加しない。
  原本Pack/nativeDATA/generic64は独立したまま。
- コンパイラ/SDK/registry/元8file kernelを照合しofflineでextensionだけを生成。
  追加selectorは派生nativeDATA一つだけをbuild。keyword原本/翻訳SHA、元/派生entry・
  export、actualextension・adapter追加のSHAを明示し、既存普通のstageは無変更。
- 初回library import前に実extensionとadapterを接続し、preinitのkeyword実行は拒否。
  request内でcold cacheを要求し、実owner/accessor・元read_catalog一回だけを通す。
  観測は元ResourcePath.read_bytes/OccurrenceReaderを委譲し、finallyで元の所有関係も
  復元する。全45schema/453entry read、45miss302hit、Rust entry差、actual全結果の
  SHA/bytesを観測し、その費用も同じ2B/64MiB/絶対30秒に含める。
- 実NativeSetupStoreの単一呼出しは成功/trap両方で残fuel・期限・Store退役・payload解放を
  記録する。trap後のretry/予算補充はしない。成功時も証明は派生App経路だけで、GUI・
  full20やnative製品Python削除の条件を満たしたとは呼ばない。
- 削減候補は未計測。まずactual App経路の制約を記録し、META snapshot/新checker等の
  推測最適化へ進む前にAstra highへ設計照会する。

- 初回 `9fd1c5b20` CI `41149114797` / job `124381130602` はSDK入力検査で停止。
  新helperがSDK固有manifestをgeneric stage outcome用runtime_inputs.reportへ渡したのが
  原因。既存build_schema_kernelと同じbounded JSON読み取り→実verify_sdkへ修正する。
  SDK archive/展開内容の照合は緩めない。まだRust compile/guest実行には到達していない。

- 修正 `3835e6759` の実CI `41149378352` / job `124381975326` はcompile・派生App生成に
  成功。extensionは元637と同一376,558bytes / SHA4304343c…2e53、component28,414,908
  bytes / SHA811b9884…4b65、importなし。実fullcatalog一回は1.86198259秒で元2Bを
  消費して停止。prepare後1,979,529,745、最終0、deadline保持/deadline未超過、
  unreachable=false、Store退役=true、physical drop後payload=0。trap stackに実Rustの
  OriginalKeywordIterator::step/__next__とPyO3call_methodを確認した。これは実到達の証拠
  だが、関数別fuel配賦や全45/453件完了の証拠ではない。観測費用も同予算に含まれる。
  artifact13022443354（241,882bytes）のmetadataだけをRAM検査しbounded receipt保存。
- 同head前身9fdの既存capability回帰41149114735はbuild/実nativeDATA比較とも成功。
  baselinebundle41149114824は既存3artifact生成・consumer型検査に成功。Linux/macOS
  sourceidentityは同じ残fuel1,223,869,669/payload0で成功し、独立cold schema storeは
  元2Bで停止。fullcatalog/ordinary Flowは未合格。新しい派生成功へ読み替えない。

## 018: keyword呼出し準備の一時辞書を削る

- 現在の状態（2026-11-01 JST）: **内部呼出し/型/値/参照/keyword familyのnative比較は
  下記の各sourceで通過。最新URI b71は全native比較成功、同source実Wasm累積40件へ増加**。
  cold/fullは元2B未達。原本839/製品GUI/全20条件・採用は未完。
  以下は各sliceのsource順の履歴であり、過去の「待機中」は後続の実結果で更新する。
- 場所: `schema_kernel/src/keywords.rs` の$ref/$dynamicRefとevolve呼出し。
- source監査で、descendは既にvectorcallだが、上記2箇所にはまだPyDictのkwargs生成が
  残っていると確認。Astra highの指示に従い、実methodを一度取得してそのcallableへ
  Stable ABI PyObject_Vectorcallで0位置引数・元keyword順の値を渡す。private metadata
  tableに(ref,instance)/(schema)の2行だけ追加。calleeが必要に応じて作るkwargsまで
  消えるとは主張しない。公開generator・algorithm・counter開始・fallbackは変えない。
- 原版/移植双方の実handlerからdescriptor・任意callable・bound callable・functionを
  呼び、get/call故障・同じarg参照・keyword順・一回lookupを既存必須19群に追加する。
  native64/API19/App37、元一括、同checkpoint診断と実App fullcatalogを別に検証する。
- 却下した削減案: public keywordのyield-fromをdirect iterator returnへ変えると、現在は
  Python generator finalizerが担うretained descend childのclose/drop/cycleを失う。
  公開generatorを削って必須gateを弱めない。既存keyword固有send/throw転送はsource上に
  未検証の不足もある（現native sendは値無視、throwなし）。今回の内部呼出し準備とは
  分け、将来の完全なlibrary/Pack互換確認の課題として残す。

- 018の実結果 `50c4fdf75`: nativeCI41151263976は64/API19（追加method binding/fault比較
  を含む）/App37/42resource/全305entry453readが一致。Rust entry数も元のまま。
  generic一括41151263996とApp一括41151263995は元2Bで停止、期限保持・payload0。
  診断41151269323は10件完了・次は同じexternal catalog。contractまで1,137,660,516
  fuel、635の1,144,124,292から約0.565%減だが、変更したcold helper metadataも含む。
  各内部callの配賦や実時間の高速化とは断定しない。全catalogやgeneric64予算は未達。
  変更は小さいcall準備の整理として記録し、解決策と誤記しない。
- App所有Rust backendはAstra highが正当な分離方向と回答。成功だけnativeで処理して
  invalid/unsupportedという検証結果を見た後に原版validatorを再実行する案は不採用。
  明示的なdefault callableの選択・実呼出し、失敗/参照/format callback/診断順まで直接
  守る必要がある。互換経路は検証前のprovider選択として区別する。新backend・旧候補の
  不採用/一般APIの適用範囲を明文化してから実装し、既存gateの結果を消さない。

- App backendの境界と未決事項を[専用設計記録](app-validation-provider-boundary.md)へ整理。
  明示的default callableの選択、実call/None拒否、mutable cache、直接のerror構築、
  reference/format callbackと結果依存fallback禁止を固定する。新backendはまだ未実装。
  Source-onlyな不存在調査だけでcallback契約を消さず、実装前に37ケースの影響を明示する。

- 018 backendの次の具体化（Astra high回答）: 最初はApp所有check_schemaを成功・失敗とも
  Rust frame/contextで完結させる。generic handlerのPython descend/evolve経由を再利用する
  薄いPyClass置換は不採用。子ごとのPython Validatorを作らず、必要なresolver/format/error
  境界だけ元objectへ戻す。document検証は評価前から元経路を選ぶ別operationとして残す。
  入力schemaの$schemaから新しいdialect dispatchを足してはいけない。元check_schemaは
  cls.META_SCHEMAからvalidatorを選択する。実META/資源/first error/SchemaErrorまで実装・
  比較してから元2Bで測定する。新backendのコードはまだ未実装。

- 018 backend初稿: default-off app-schema-backend featureにRustフレーム走査とApp所有
  check_schema入口を追加。子ごとのPython Validator/evolve/descendは使わず、実Py参照・
  resolver・分岐・error graphを保持する。scalar ruleは既存generic実装と共通化し、元の
  method lookup/固定intern文字列identityは保持。原版64/API19は独立したまま、新backend
  64ケースとApp37/42/full305453の別laneを追加。native比較前であり未採用。
  現在のMETAで使われない未実装known ruleは明示拒否する。
  metadata mutation/custom numericの未完了範囲を原版retryで隠さない。まずnative機能比較、
  次に未完了境界と実Wasm費用を検証し、全App/backend完了へ進める。製品UIは未変更。

- 018実結果`7c0f5a265`: nativeCI41159026652は全成功。既存generic64/API19とApp37一致を
  保持し、別featureの新backendも原版64・追加11境界検証・App37/42/full305entry453readが
  一致。新backend実entry数0→45(catalog)→87(42schema)→110(caller)。新backend native
  extension909,944bytes/SHA829273a2…26a6。これはnative機能比較だけでWasm予算・製品採用の
  証明ではない。追加11の未解決META ref試験へ実retrieve URI到達のassertも補強する。
- 続いて、明示的な--app-backend/--app-schema-backendで同じソースをWasmへbuildする
  別artifact経路を追加。実Appのvalidation importだけを新providerへ置き換え、元/派生SHAを
  記録し、元jsonschema keyword/文書経路を改変しない。初期化中のentry/frame0と本呼出しの
  45coldchecks、実source owner・45schema/453entry read・45miss302hit・元結果SHA/bytesを
  同じ2B/64MiB/絶対30秒一回で検査する。generic候補は別のまま、結果依存retryはしない。

- 実Wasm初回Appframe `316874bf7`/CI41159765631は元2Bで停止（1.811789s、prepare後
  1,979,539,652→0）。deadline保持/未超過・unreachable=false・退役・physicalpayload0。
  実feature extension445,530bytes/SHA286acaf7…4a41、component100bf37b…7ec11。
  trapは実Rust Frame.poll→PyObject_VectorcallMethod内。45件完了/文書段階到達は未証明。
  子Validatorは除去できたが、resource作成・scope移動・参照lookup等のPython呼出しが残る。
  次は実native64のcProfileで実関数/呼出し数を収集し、Rust参照状態への移行境界を決める。
  native時間をWasm fuelへ配賦しない。再試行/上限緩和/原版invalid側fallbackはしない。

- 実native64 profile`38dcddde7`/CI41161831597はgeneric64/API19/new64+11/App37/full比較を
  維持して成功。実create_resource25,619回/in_subresource25,204回/lookup18,123回を記録。
  ただしこれは64定義だけのnative呼出し数。full catalogには元のdocument検証も交互に
  含まれるため、trapの現在位置だけで費用の支配要因を断定しない。
- 同じ実artifactで既存cold-original-schema-store（原版42calls/cache/全結果SHA/bytes）を
  別の元2B/64MiB/絶対30秒の一回として計測する。full操作は別Storeで引き続き検査し、
  cold成功や部分結果をfull完走へ合成しない。失敗時も双方のfuel/deadline/dropを保存する。
  native full catalogも実一回をprofileし、定義と元documentの呼出し数を区別する。
- Astra high: 既定Appの参照状態もRust所有へ進める。まず実schemaを毎回読むID変更なしの
  descendを対象とし、任意のPython resolver/specを既定らしいと推測して省略しない。
  不在/None/空文字/相対/絶対/不正型、scope復帰、callback後の再読取り、retrieve順序、
  実schema/error同一性とcustom事前選択を検証する。meta/結果のobject-id cacheは使わない。
  実Wasm成功までwholeRuntime bindingや製品採用には進まない。
- 将来のwholeRuntime接続はAstraの指示で実ModulePathBindings.install後のcoldなvalidation
  moduleへの明示bindingを選ぶ。None/custom/既使用cacheは上書きせず、binder/provider/so/設定を
  plan/provenance/実component admissionへ結ぶ。元indexは元source bytesのみの証明であり、
  派生compositionの動作や権限へ流用しない。ordinaryPackとは独立した別artifact検証が必要。
  cryptographyを含む未提供closureと元GUI/owner統合は未完了のまま。まだ接続コードなし。

- `7d34a0174`実測CI41162682461で、同一component SHA0eea81c5…b06a1のcold42操作と
  full操作を別invocationで検査。両方元2Bで停止した（cold1.084401120s/full0.929129181s、
  prepare後1,979,550,701）。両方deadline保持/未超過・unreachable=false・退役・payload0。
  coldでも同じRust Frame.poll内だったため、文書段階へ進む前に定義側の削減が必要。
  部分結果は合成しない。全Wasm操作は未達のまま。
- 同headnativeCI41162600216は既存比較を保持して成功。full native profileは305の
  validate_documentと45,921の元Validator生成を記録した（305root+45,616evolve）。
  定義と文書の双方に未移植の費用が残る。native時間をWasm fuel配賦へ変換しない。
  次はAstraの既定Rust所有参照状態/no-ID遷移と、ID/anchor/retrieve/mutationの比較を実装する。

- 018参照状態の初稿: Rustがbase URI/実registry/dynamic scopeを保持し、必要なID/ref処理の
  境界だけPython Resolverを組み立てる。通常のexact dictで$id不在またはNoneなら実値を
  毎回読み、そのRust状態を共有する。空文字/不正型や非builtin mappingを省略しない。
  Pythonから渡されたspec/resolverを既定らしいと識別して置換するAPIは廃止し、明示的な
  opaque DefaultReferencePolicyで既定を選ぶ。providerの元closure調査も不要となる。
  policyはmetadataのみでpreinitにResource/Resolver/入力検証を作らず、権限ではない。
  実Registry/Resource callbackとtyped cause/contextは既存の解決境界へ保持し、結果依存
  fallback/ID結果cacheは使わない。native比較と元Wasm予算での測定はまだ未完了。
  元動作の出典: referencing0.37のResource.idとResolver.in_subresource（Noneのみ無変更、
  present IDはrstrip/URI処理を行う）。
  https://github.com/python-jsonschema/referencing/blob/v0.37.0/referencing/_core.py

- `96efa15a0`のnative CI41165076858は成功。既存generic64/API19・新backend64・既存11に
  加え、ID各型/anchor/dynamic anchor/外部retrieve/scope復帰/live ID再読取り/opaque policy/
  custom事前選択/失敗context解放の20比較を通過。App37/42/full305entry453readも一致し、
  実entry数は0→45→87→110を維持した。metadata artifact13029380564（63,470bytes）は
  RAMのみで読み、bounded JSON証跡を保存。製品への採用とWasm予算適合はまだ未証明。
- 同じnative64 profileではcreate_resource25,619→4,630、in_subresource25,204→4,215へ
  減少した一方、Resolver生成は20,119→38,242へ増加した。lookupごとに境界adapterと
  _evolve結果を両方生成するため。lookup18,123/urljoin15,840は変わらず、native計数を
  Wasm fuelの費用配分や性能改善率とは扱わない。次の候補は実Registry/Resource/anchorを
  保ったRust側lookup/_evolve制御で、URIやretrieveの結果cache・型の推測・失敗retryはしない。
  同headの実App Wasm CI41165238057を一度だけ起動し、元予算でcold/fullを測定中。
- CI41165238057の実測はcold/fullとも元2Bで停止。prepare後1,979,543,968、cold2.029334353s/
  full1.779418969s。同一component71abd04d…60ca5、deadline保持/未超過・unreachable=false・
  退役・physical payload0。完了結果や性能改善率は未証明で、予算上限は維持する。
- 次稿はAstra highの回答に従い、lookupの制御とprevious更新をRust側へ移す。
  実URI関数・Registry取得・Resource.pointer・anchor.resolveはそのまま呼び出す。
  内部伝達だけのResolvedとadapter+_evolveの二重生成を除く。実境界が返した正規型の
  resolverは実参照とidentityを保持し、状態不変のdescendでは同じadapterを使う。
  custom/subclass resolverはprivate fieldsを読まずopaqueな実オブジェクトへdispatchする。
  元96の無条件private-field取込みも修正する。失敗時の例外型/cause/contextを維持し、
  pointer escape/index/欠落・anchor欠落・空fragment・取得例外・custom resolverの比較を追加。
  正規Resolver class自体のmethod差替えはこの既定policyの契約へ新規追加しない。
  実native/Wasm検証前で未採用。結果cacheやvalidation再実行は導入しない。
- `cb224cd49`のnative CI41165854018は全成功。新29参照比較（custom返却とNoSuchResourceの
  suppress/cause/contextを含む）・backend64+11・独立generic64/API19・App37/42/full305453を
  保持し、entryは0→45→87→110。実64のResolver生成38,242→6,211、Resource生成4,630。
  native計数だけでWasm fuel改善を主張しない。同ソースの実App Wasm CI41165988012を
  一度だけ起動し、元のcold/full予算を検証中。製品UI/既定設定にはまだ採用しない。
- 未実装の小整理候補: `schema_kernel/src/app_schema.rs`のSelected.nameは各ruleでStringを
  複製するが、保持後の用途はdecorate時の`if`/`$ref`判定だけ。選択時の同じ判定をboolで
  保持すれば文字列複製を省ける。実keyword参照・選択順・実エラーのpath装飾は保持する。
  現在のWasm試験と混ぜず、変更前後のnative比較と実測を前提とする。全体予算の解決策や
  hot spotと断定せず、参照制御移植の結果を先に評価する。
- `cb224cd49`の実App Wasm CI41165988012はcold/fullとも元2Bで停止した。
  同一component1b4700f6…eaadf3、実extension467,348bytes/SHAaf81d66e…cd6d6。
  prepare後1,979,547,224→0、cold1.282093311s/full1.159796787s。両方deadline保持/未超過・
  unreachable=false・退役・payload0。native機能比較は成功したが、全Wasm操作は未達。
  coldの現在stackはPython slice/item処理→実Rust Frame.poll、fullはPy_DecRef→Frame.poll。
  現在位置を全費用の説明にはせず、Astraへ次の実移植範囲と診断要否を照会する。
  同ソースの再実行や上限緩和・wholeRuntime/product採用は行わない。
- Astra highの次の指示は追加のURI/Registry移植ではなく、実Appのschema単位の累積診断。
  実load_schemaの名前・引数形式・順序・owner/resources/parser/provider/既存cacheを使い、
  一つのStore/accountで準備から全checkpointまで元の累積2B/64MiB/絶対30秒を保つ。
  最後に開始したschemaと各完了点の残fuel/cache miss・hitを別診断artifact/receiptへ記録。
  trap後は続行せず、呼出し分割の擾乱を明記する。元cold/full一括gateは独立して維持する。
  関数別fuel比率ではなくschemaごとの費用/進行を得て、次のApp内部Rust接続を一箇所選ぶ。
  診断コードは未実装。小C API移植の反復やnative呼出し数だけでURI移植へ戻らない。
- 累積診断の初稿を実装: 明示的なapp_schema_progress/app_schema_backendでのみ作る
  `harness-app-schema-progress-native`と専用receipt。通常cold/full artifactには混ぜない。
  実captured ownerから元_schema_storeと同じ順序で42名を列挙し、実load_schema(name)を
  positionalで一度ずつ呼ぶ。元のparser/provider/cacheを使用し、結果を事前準備しない。
  Rust側は一回のaccount.begin/Store/programを最大43export callsで保持し、各完了点の
  燃料・cache・backend実entryと呼出し開始/完了数を記録。直前に開始した名前も保持する。
  trapは同Storeを退役して終了し、fuel再取得/続行はしない。観測が正しい予算停止だけを
  診断成功と区別し、sequence_completed/qualification=falseと全物理dropを保存する。
  元のcold/full gateは独立して維持し、別modeの診断成功で置き換えない。CI検証は未実施。
- `0066e3246`実診断CI41167136452は観測とcleanupの検査に成功したが、42件の実行完了では
  ない。同じ2B/64MiB/絶対30秒で19件を完了し、20番profile_lock_v4.schema.jsonの開始後に
  fuel0で停止。開始時残22,620,801、完了cache hit0/miss19/size19、backend entry19/
  frames17,429。deadline保持/未超過・unreachable=false・退役・payload0。
  実componentf13bd8a6…ae6a20、RAM metadataZIP13029905942/244,635bytes、bounded証跡保存。
- metadata列挙は1,070,414fuel。完了したschemaの累積差分を新規frame数で割った値は
  約108,194〜116,869と近く、単一schemaの特別な固定費より評価量と共に増えている。
  Defaults Setup270,603,967fuel/2,416frames、Pack manifest212,489,788/1,936等。
  これは分割呼出しのparser/境界/観測等も含むschema単位の比で、関数別費用ではない。
  original cold/full一括gateは未達のまま。次の実移植箇所をAstraへこの実測で照会する。
- 次はAstraの指示で、取得後のfragment→実contents/次scopeまでをRust frameへ接続する。
  URI処理/Registry全体/META iteratorを混ぜず、実Registry.get_or_retrieve/anchorは保持する。
  最初はproviderの既知source初回ロードで明示所有したbuiltin META Resourceだけを対象にし、
  実Resource参照で所有を記録する。URI名やcontentsの類似で推定せず、外部retrieveが返した
  正規型Resourceもopaqueのまま。これは解決結果cacheではなく適用範囲の記録。
  型/specと、操作時に一度取得した実bound methodのfunction/receiverが既定policyに合う
  場合だけnative処理する。不一致なら取得済みcallableをそのまま呼び、再取得/retryしない。
  内部spec/helperの動的dispatchも原本の時点で保ち、途中のcallback変更も無視しない。
  必須比較はpointer escape/index/不正token/欠落/実参照、途中$id/dynamic scope、anchor競合/
  外部跨ぎ、class patch/format中patch/descriptor/helper変更/所有contents変更/typed cause。
  fragment移植は未実装。実到達・adapter生成数と同じ累積診断/元cold/fullで採否を決める。
- fragment初稿を`schema_kernel/src/app_schema/references.rs`へ分離して実装。最初に自然に
  load/crawl済みの実SPECIFICATIONS._resourcesと既存_anchors（別Resourceを含む）だけを
  保持する。per-call rootは別Resourceなので同じURI/contentsでも所有へ加えない。
  正規methodは最初の既知provider import時にPyOnceLockへ固定し、後のpolicy作成でpatchを
  正規と採り直さない。これはsource metadataのみでResource/Resolver/入力検証を新造しない。
  実contentsのpointer歩行・既定subresource制御・既存anchor走査をRust stateへ接続し、
  内部Resolver adapter/ResolvedとPython制御の往復を除く。実Registry/URI呼出しと、pointer
  各segmentの実spec.create_resource、実Resource.idは維持する。helper変更は取得済み実
  callableへ同じkwargsを渡し、custom resolverをopaqueに保持する。結果/ID/解決先cacheなし。
  実bound methodを一度だけ取得して分岐し、不一致ならそのcallableを直接呼ぶ。
  新10比較は実builtinへの到達・error参照・class/format中patch・descriptor/helper変更・
  外部Resourceのopaque経路・contents再読取りを含む。新到達/adapter計数をnative報告と
  累積診断へ追加。GCは全policy handleを列挙、closed source inventoryは新moduleも含める。
  native/実Wasm検証前で未採用。referencing由来のMIT attributionも同梱noticeへ加える。
- Astraが外部alias条件を明確化: 既に所有している同一Resourceをcallbackが別URIで返す
  場合はnativeを許可する。未所有Resourceを同型/同内容から昇格させない条件は変わらない。
  実使用URI/返却registry/base URI/scopeとcallback変更を維持し、canonical URIへ戻さない。
  origin推測の追加lookupやretrieve wrapperは不要。別URIでの相対参照、返却時patch/変更、
  同contentsの新Resourceがopaqueである比較を追加する。
- `6448b8c2a`native CI41169521983は成功。既存generic64/API19/backend64+11/参照29と
  新13比較・App37/42/full305453を保持。64ケースでnative pointer2,283/dynamic anchor1,932、
  内部adapter構築0を記録し、full catalogではpointer2,452/dynamic2,086/adapter0。
  builtin static anchorの対象inventoryは空のため、native静的anchorの実到達は未証明。
  型/内容が同じ新Resourceの非昇格、所有済みResourceの別URI返却と実scopeは比較済み。
  Source extension988,600bytes/SHA8e429460…2dac7、RAM artifact13030552060/64,074bytes。
  これはnative機能/接続証明のみでWasm予算の改善率ではない。
- 中間$idでpointerのscopeが移る専用比較を追加し、新14比較で再確認する。元29のID比較は
  custom METAのunowned Resource経路なので、owned native区間の証明と混同しない。
- `3510bd970`のnative CI41169716119は新14比較を含め全成功、extensionは6448と同一bytes。
  実Wasmの累積診断CI41169851328は観測/cleanup検査に成功したが、19件完了/20件目停止は
  変わらず。19件後残fuelは40,575,326（旧22,620,801）、frames17,429、cache hit0/miss19。
  Rust pointer928/dynamic857/adapter0の実到達を確認。metadata/observerも変更されており、
  差分を関数の費用割合とは扱わない。新componentd95ce723…de564、RAM artifact13030871218。
- 同head一括CI41169845789は元2Bでcold/fullとも停止。同component09b486f0…58dcb、prepare後
  1,979,547,721→0、cold1.304300670s/full1.161261552s。両方元deadline保持/未超過・
  unreachable=false・退役・payload0。RAM artifact13030344622。元一括gateは未達。
- Astraが次に選んだのは、App内部の参照URI→URI/fragment区間をまとめたRust移植実験。
  `urljoin`制御だけを移してPython parse/unparseへ戻る旧試行は再利用しない。
  exact builtin ASCII str、対応構文、現在の正規URI関数/省略helperと可変scheme設定が
  対応範囲のときだけ実行前に選ぶ。mutable listはidentityだけで固定扱いしない。
  条件外はそのURI操作の最初から実Python経路へ渡し、native失敗後には再試行しない。
  Rustで分岐/parse/join/再構成/defragをまとめ、途中tuple/parse-result/helpersを除去する。
  CPythonそのものの動作と入力文字列を返す場合の参照関係を保ち、一般URI正規化へ変えない。
  空/fragment/query/空query・fragment、path/network/scheme/slash/dot、percent/case/control、
  Unicode/bracket/subclassの事前原版選択、関数/helper/設定の変更後dispatchを比較する。
  Registry/Frameは同時に変更しない。同じ累積診断と未変更cold/fullで前進を測り、効果が
  小さければ戻す。guard自体が多数のPython処理を再現する場合は対応範囲を狭める。
  以下の実測で逆効果と確認したため取り下げた。native profileの回数/時間だけでfuel支配や
  42件完了を予測しない。
- URI実験229dfb9b9→5e3425572を実装・検証した。正規function/default/result-class raw
  MRO slotと可変listの現在の全要素を毎回照合し、対応ASCIIのみRust parse/join/再構成/
  defragを実行。scheme-less baseの入力identity経路は事前Python選択で維持した。
  native CI41173106209はgeneric64/API19/backend64+11/reference29/fragment14と
  URI68操作11群、App37/42/full305-entry453-readの比較に成功。通常64件のRust URI到達は
  13,908回、Python URI境界0。extension1,085,080B/SHA f2943402…99741。
- 同head累積CI41173248896は診断/cleanupだけ成功、sequence=false。元2B/64MiB/30sの
  同一Store/accountで15件完了、16番目pack_manifest_v4でfuel停止。旧3510の19件完了から
  後退した。prepare後1,979,555,353、15件後139,146,644。component d4f7405d…e0e27。
  deadline保持/未超過、unreachable=false、退役、payload0。RAM artifact13032112412。
- 同head元一括CI41173246908もcold/full両方2Bで停止。component0a81f619…1d8b21、
  prepare後1,979,531,183→0、cold1.839863331s/full1.651008346s。元deadline保持/未超過、
  unreachable=false、退役、payload0。RAM artifact13031767489。互換性一致を予算達成と
  扱わない。guard負担は候補仮説であり、停止stackを関数別fuel計測とは呼ばない。
- 逆効果のURI engine/独立helper/追加API・計数・source inventory・不要license noticeを
  一緒に除去し、検証済み3510のApp/URI経路へ戻した。試行の全sourceは5e3425572の履歴に
  保持し、未使用600行を残さない。別修正33464819aの構造化Cargoコンパイルエラーを
  bounded検証記録へ保存する改善は維持する。元42/full予算gate・GUI採用は引き続き未達。
- 戻し後native CI41173790783もgeneric64/API19/backend64+11/reference29/fragment14と
  App37/42/full比較に成功。extension988,600B/SHA8e429460…2dac7は3510/6448と完全一致。
  戻しをsource差分だけで成功扱いせず、実native成果物でも確認した。
- Astra highは、続けて提示した「urllib呼出し閉包の追加移植」を撤回した。
  coercion小幅・単独join後退・今回19→15という経緯から、この系列を優先する実測根拠はない。
  次の性能実装を未確定と明記し、通常経路の閉鎖性を確認する前にRust value snapshotを
  導入しない。失敗を必須比較から除外しない。
- 次の判断に必要な具体的なcallout調査対象を整理した。現4a51/3510では
  `app_schema.rs`のstart(118)が実items view/root listを持ち、decorate(158)が実errorの
  `_set`/pathを更新する。scalar dispatch(566)→`keywords.rs:1169`はequal/ensure_list/
  uniq、re.search、format checker.check(1349)、実数比較とrepr/formatへ戻る。
  `references.rs`のpointer(245)/fragment(325)/lookup(478)/descend(544)は実Registry/
  Resource/Specification/URI/descriptorに戻り、その後のcontents・scopeを読み直す。
  `app_check_schema`(693)もcreate_resource/resolver_with_rootを呼ぶ。参照だけを付けた
  Rust snapshotでは、その途中で変化する入力・設定・例外時点を維持できない。
  閉じた経路の事前選択には、(1)全外部calloutと観測状態、(2)新descriptor/callbackを
  実行しない開始前判定、(3)終了まで前提が崩れないこと、の具体的な証拠が必要。
  strict parser由来/登録箇所未発見という理由だけで許可しない。成立時のみ閉じた経路を
  Rust value modelへ、live callback経路を3510の参照保持engineへ開始前dispatchする案を
  検討する。現時点でその事前判定は未証明で、次の性能コードはまだ追加していない。
- 通常経路のcallout調査で、具体的なbinding相違を発見した。
  `keywords.rs:utility`は補助関数を`jsonschema._utils`から毎回取得していたが、原版
  `_keywords.py`は7種類の補助関数をfrom-importし、自分のglobal bindingから呼ぶ。
  export元の再代入は既存aliasを変えず、import先の再代入は次の実呼出しを変える。
  取得先を実`jsonschema._keywords`に修正し、余分なcache/guard/別engineを作らない。
  これは性能施策ではなく実callback/参照保持の互換修正で、closed operationの事前選択を
  証明するものでもない。全global削除/builtin fallbackや反射契約の完了は主張しない。
- `app_schema_callout_checks.py`に独立した5比較を追加。元META・既定FORMAT_CHECKER・
  checkers dictの同一性を維持し、stock regex callbackから後続maxItems実入力を変更、
  呼出し前/途中のimport先uniq変更、export元変更がaliasに影響しないこと、実例外の
  identityと停止順を比較する。check_schemaにはchecker引数を渡さない。
  原版を実oracleとして結果/順序/実引数参照/error graphを検査し、両leg後に全bindingを
  復元する。原版constructorを再実行してnativeの失敗を補修する経路は追加しない。
  ローカルはsource AST/rustfmtのみ、native実行はCI待ち。
- b7d61c3bcのnative CI41174455898でdefault-callout5群を含む全比較が成功。
  generic64/API19/backend64+11/reference29/fragment14/App37/42/full305453は維持。
  extension988,608B/SHA dfbceec5…70ef6、RAM artifact13031658726。性能/実Wasm予算達成や
  closed operationの採用証拠とは扱わない。
- Solの残存binding監査では、keywordのValidationError/FormatError/Fraction/re aliasの
  取得元と、Fractionを実際には2回lookupする時点の差も見つかった。false-schema errorは
  keywordではなくvalidators側の責任なので一括置換しない。未採用generic翻訳の互換TODOと
  して保持し、App移植を全第三者private globalsの再現へ拡大しない。
- Astra highが、未選択のOriginalへ全metadataをforwardする現在のmetaclassは元Appの
  必須仕様ではなくprototype追加結合の可能性が高いと整理した。選択されたproviderの
  設定・実callback・入力変更は保持し、独立したApp定義/依存stateを所有する方向へ進む。
  原版54e7のload_schema/SDK/Workflowを確認し、実checker/resolver/拡張とprivate globalを
  区別する根拠とケース別の維持条件を`app-validation-provider-boundary.md`へ追記した。
  既存fixtureはまだ削除/retargetしていない。原版runtime/docsのliteral metadata名検索が
  0件というだけで共有契約の不存在を断定しない。実所有META/resourceの参照対応と
  independent-owner isolation検査を伴う次の実装が必要で、独立providerはまだ未実装。
- 次の所有sliceを実装（CI前）。wildcard `_Metadata.__getattr__`を削除し、App providerが
  明示的なMETA/checker/type/registry属性を持つ。新`app_schema_model.py`はartifactに既存の
  lock-pinned jsonschema-specifications2025.9.1から固定8文書を読み直してgraphを構成し、
  OriginalのMETA・registryをdeepcopyしない。class rootだけは原版同様の浅いdict viewで、
  自分のregistry rootの実childと共有し、Originalのcontainerとは共有しない。
  jsonschema4.26の登録定義を明示し、独立checker表に実callback/例外classを保持する。
  callback本体とglobals・明示remote callback・type checkerは引き続き実依存で、完全独立や
  callback-free判定とは呼ばない。source dataは既存lock/inputs/component digestへ結び付く。
- 定義graphだけを通常module初期化時に構成し、App schema/catalog/validation結果を作らず、
  producer warmingを追加しない。Rust `app_reference_policy`はその実crawled Resource/
  anchor Resourceを保持するmetadata factoryで、exact Registry/Resource/DRAFT形状を確認。
  source trustや実行権限は付与せず、後続retrieve由来resourceは昇格しない。
  元のdefault policyは別のcompatibility metadataとして維持する。engine/予算は未変更。
- backend11/reference29/fragment14/default-callout5は、各laneで実際に選択されたproviderの
  META/checker/registry/resourceとerror参照を対象に変更。ケース名・正負計数・descriptor・
  retrieval・scope・例外順/参照を残し、削除しない。observerも選択providerのgraphを索引化。
  新4群で8文書の内容一致/全container非共有、登録表独立と実callable/例外保持、Originalの
  class/graph/checker変更後にsourceから新規再構成できること、実selected META差替えと
  error.schema参照を確認する。source inventoryは12、native/実Wasm packagingにも追加。
  source AST12/rustfmt/diffとSolのreadonly source監査のみ完了、実native/guest検証は未実施。
- 所有sliceの実native検証は71f785246/CI41176050740で成功。generic64/API19/backend64+11/
  reference29/fragment14/default-callout5/ownership4とApp37/42/full305453を維持した。
  初回48ec/CI41175858653は内部比較に成功後、caller observerが旧Originalを索引にして
  error参照locationを失ったためApp比較失敗。実差分1→0を保存し、71fで実selected provider
  を索引に修正した（assertion削除やerrorの捏造なし）。extension996,360B/SHA b952650b…c5513。
- 同71fの実Wasm累積CI41176241136は診断/cleanupのみ成功、sequence=false。
  元1Store/account/2B/64MiB/30sで19件完了、20番目profile_lock_v4がfuel停止。
  prepare後1,979,546,512、19件後36,924,554、frames17,429、cache hit0/miss19。
  実owned graph経路でpointer928/dynamic857/adapter0を確認。component a76c85e0…6e25d。
  元deadline保持/未超過、unreachable=false、退役、payload0。RAM artifact13033296403。
- 同head一括CI41176239148はcold/fullとも元2Bで停止。component07736e8e…976ef6、
  prepare後1,979,531,561→0、cold1.292238855s/full1.154017513s。両方元deadline保持/未超過、
  unreachable=false、退役、payload0。RAM artifact13033506245。両modeの実Wasm extensionは
  511,889B/SHA be7b35be…393f1。source definition初期化とnative参照に実到達したが、
  完全な一括意味結果/予算gateは未達。所有sliceを性能改善と呼ばず、関数別fuelを推測しない。
- Astra highの次指示は、実handleを持つ値viewをRust frameへ接続し、固定App policyの型・
  数値制約から移すこと。dict/listの実iteratorと参照解決は同時変更しない。
  exact scalarはRustで扱い、bool/int・整数float・巨大int・非有限値を維持する。
  非標準object/custom operationはその操作地点で実処理へ渡し、検証全体を再試行しない。
  callbackだけでなくsubscription/比較/iterator/descriptorも再入点。mutable内容borrowを
  残さず読み直すが、原版が既に束縛した子handleを親から再取得して差し替えない。
  errorは元frameの元時点で実schema/instanceから構成する。子取得前後の差替え、同一子
  変更、alias、callback挿入の非標準object、例外順/identityを追加比較する。
  次sliceはまだ未実装で、全value engineや性能合格の予測はしない。
- 次value sliceを実装（CI前）。`app_schema/value.rs`のNodeがFrameの実schema/instance
  handleを所有し、mutable内容や判定結果は保存しない。dict/list iteratorと参照解決は未変更。
  source-default型policyをopaque metadataで明示し、exact組込み型の判定と整数float判定を
  Rustで行う。App TYPE_CHECKERは明示source依存から取得し、実ownerの設定は操作時・error
  decoration時に読み直す。custom checker/非標準object/非exact型名は実is_type境界へ渡す。
  UnknownTypeへの変換も実except内で行い、例外contextと実引数を残す。
- Appのminimum/maximum/exclusiveMinimum/exclusiveMaximumのみ、exact i64同士・float同士・
  整数が正確に表せる±2^53範囲の混合値をRust比較へ移した。巨大整数・危うい混合値・
  subclass/custom比較は比較前に実Py rich-compareを選ぶ。multipleOfやgeneric keyword APIを
  同時変更しない。bool/int・NaN/Inf・負のzeroを区別し、errorは元位置で実handleから作る。
  policyはsource/type実装metadataで、callerのnative許可flagやCore権限を追加しない。
- 独立helperに12群/151対の実原版比較を追加。exact/scalar境界、全4数値制約、巨大/非有限値、
  Decimal/Fraction/Number/subclass、custom TYPEと途中差替え、型名hash、子取得前後の差替え・
  同一子変更・複数path alias・非標準値挿入、比較/descriptor/iterator例外identityと順を含む。
  NaNをJSON digestへ通さず、実node参照とrepr/tagで観測する。実64件のnative型/数値到達も
  必須。Solのsource監査でsurrogate型名のto_str拒否がUnicodeEncodeErrorになる差を発見し、
  実type lookup/UnknownType境界を最初から選ぶよう修正、unknown/surrogate比較を加えた。
  全13source inventoryを更新。source AST7/rustfmt/diffのみ完了、native/guest実行は未実施。
- 6e23f4bc0のnative CI41178194013で12群/151対と従来全比較が成功。generic64/API19/
  backend64+11/reference29/fragment14/default-callout5/ownership4/App37/42/full305453を維持。
  実64件はnative型38,182・native数値335・両Python境界0。extension1,017,720B/
  SHA fa68fec3…50169。RAM artifact13034180617。Wasm到達/改善はこのnative結果で代用しない。
- ユーザーの重要実装をSolへ任せる指示に従い、6.1 Sol xhighがpolicy境界と診断controllerを
  実装。policyはtry_borrowとcallable形状確認を行い、入力を触るResource/Registry callback・
  entry計数より前にmetadata抽出を終える。TYPE_CHECKERの固定名はinternし、Sourceと同じ
  literal名の参照関係を保って反復文字列生成を除く。cleared/forged policyの拒否を確認する
  ために、存在しない公開clear hookは追加しない。
- 既存12群/151対を残し、4群でctor/不正policy5件の入力評価前拒否、実owner getterの
  check/decorate時点・名identity・例外identity、UnknownTypeの実context、再入中の同じ
  policy利用と例外後のweak input解放を追加。診断はcold-zero・正確な4counter形状・
  単調性・schemaごとのnative型増加を要求し、退役後はfuelへ触れず最後の成功したcounter/
  observationを残す。実値到達がないfuel停止を診断成功としない。cold/full gateは未変更。
  3b7190893のnative CI41180279130で全16群と従来全比較が成功。実64件はnative型
  38,182/native数値335、Python境界は両方0。extension1,018,768B/SHA db055d03…ad19。
- 同じ3bの実Wasm cold/full CI41180458589は両方とも元の2B燃料で停止。component
  e8f7df81…e49f、prepare後1,979,506,477→0。cold約1.984秒/full約1.792秒で、
  30秒deadline維持・期限超過なし・unreachableなし・Store退役・physical payload0を確認。
- 累積診断CI41180460640は診断の整合性確認のみ成功。19schema完了、20件目
  profile_lock_v4で燃料0。最後の成功時は32,072,253残、17,429frames、実native型16,671/
  数値76/Python境界0、pointer928/dynamic857/adapter0。cold-zero・単調性・各schemaの
  実到達・最終index19を確認した。元のone-Store/2B/64MiB/30秒とtrap後退役を維持。
  前ownership版の19件後36,924,554とは観測処理も異なるため、個別処理の燃料帰属や
  改善を主張しない。cold/full・全20/839・元GUI・1B/API・VM廃止は未達。

- 実装済み・実CI待ち（Astra high設計、Sol6.1 xhigh担当）: App frameの内部enumerate/range/
  root作業列をRustの実行状態へ移す。実dict/list iterator、foreign protocolと公開error
  contextは維持し、live列をsnapshot化しない。builtin参照cacheやMETA事前計画ではない。
  実原本4.26 validators.py263–277のroot列はlist comprehensionで、prototypeの
  list(pairs)とはforeign length hintと逐次unpack時点が異なる。CPython3.13.16の
  [list実装](https://raw.githubusercontent.com/python/cpython/v3.13.16/Objects/listobject.c)、
  [enumerate実装](https://raw.githubusercontent.com/python/cpython/v3.13.16/Objects/enumobject.c)、
  [zip実装](https://raw.githubusercontent.com/python/cpython/v3.13.16/Python/bltinmodule.c)を
  source確認。短いzipでも左を先に一個消費する順序、子取得時点、長さ固定時点、
  iterator/hint例外と途中解放を12群/26差分対で比較する。Solはcursor.rsとframe変更・新helperを
  実装、Rootがsource-defined foreign pair/key境界、source inventory14件とCIを統合。
  未知keyを先にString変換しない。rootは実逐次unpack/membership/getitem、descendはgetで
  選別する。Root監査でforeign error keyのset membership/hash欠落を見つけ、同じerror
  decoration位置の実key境界で修正した。公開context list、実iterator、参照処理と値policyは
  維持。AST6/rustfmt/diffのみ成功、実native/guest CIは未実施、燃料削減は未測定。

- 初回4a779669b native CI41182075209: コンパイル、generic64/API19、backend64+11/
  reference29/fragment14/default-callout5/ownership4/value16は成功。新controlのforeign
  root iterableで、元は__iter__2回・native1回の差を検出して停止。App backendの最終
  caller/full比較へは未到達。artifact13034843533のJSONだけをRAM経由で保存した。
  テストを緩めず、同じ操作順に直す。Wasmはnative成功まで起動しない。

- Solが原因を確定: pinned CPython3.13.16の内包表記はcompile.cの外側GET_ITER後、
  その実iteratorへもう一度GET_ITERする。rootだけこの2段を再現し、live cursorは1回を
  維持する。2回目__iter__の実供給例外比較も追加、12群/26対。AST/rustfmtのみ成功、
  修正後の実native CI待ち。テストの期待値や既存caseは変更しない。

- 修正1ebc977d1 native CI41182341650は成功。control12群/26差分対と従来generic64/
  API19/backend64+11/reference29/fragment14/default-callout5/ownership4/value16、
  App37/42/full305-entry453-read全比較が一致。extension1,033,744B/
  SHA a194ae77c6433c952248dc7045e1667c1e27bee1d7bcd41f676fe9a714603f00。
  RAM artifact13035258263/65,551B、必要JSONのみ40,390B保存。Wasmはまだ未起動。
- 同じcontrol境界のselected callback例外契約を追加実装（Astra high判断、Sol担当）。
  root constructorのStopIterationは直接伝播、descendant準備/keyword generatorからの
  未捕捉StopIterationだけRuntimeErrorへ変換し、実例外をcause/contextへ残す。実nextの
  通常Stop終端や元のcatchを先に適用し、既変換例外を再変換しない。全体catchによる
  root constructorの誤変換を避ける。18群/48対（従来12群/26対維持）へ追加。constructor
  items/id、descendant items/id、型/format、捕捉済Stop、allOf/not/not内constructor、
  Stop subclassの正常終端を含む。source AST/rustfmt確認のみで、新native CIが必要。
  現native成功からこの追加の同等性は推定しない。

- 例外境界修正25c8aa373 native CI41182636950は成功。control18群/48差分対、value16、
  従来のgeneric64/API19/backend64+11/reference29/fragment14/default-callout5/ownership4、
  App37/42/full305-entry453-read全比較を維持。extension1,035,008B/
  SHA 0581fe1ae3891965914dad5c9e88828219ccb5924eb96d7ac3b6d2a54e904b71。
  RAM artifact13034729384/65,672B、必要JSONのみ40,817B保存。元のconstructor/iterator/
  callback例外の区別をこの比較範囲で確認。全製品・Wasm予算の同等性ではない。
- 同じ25c8でWasm cold/full41182761288と累積診断41182767015を各1回起動。
  元のone-Store/2B/64MiB/絶対30秒を維持し、実行中。同じrunを最後まで追跡する。
  このcontrol sliceの完走件数・燃料改善はまだ未測定。失敗後の予算更新・再試行は行わない。

- 25c8実Wasmは終了。cold/full CI41182761288は両方2B燃料で停止。component
  c166298e…e3896、prepare後1,979,519,111→0、cold約1.329秒/full約1.170秒。
  絶対30秒deadline維持・期限超過なし・unreachableなし・Store退役・physical payload0。
- 累積CI41182767015は診断整合性のみ成功、19件完了/20件目profile_lock_v4で燃料0。
  component601b9800…22bf5、prepare後1,979,545,006、19件後34,803,270残、17,429frames、
  native型16,671/数値76/Python境界0、pointer928/dynamic857/adapter0。実cold-zero/
  各schema到達/単調性/index19/同一Store・account/予算更新0と退役・payload0を確認。
  前3bの19件後32,072,253から数値は変わったが、完了件数は19のまま。個別処理への
  燃料帰属や全体完走・製品採用の証拠ではない。
- 両modeの実Wasm extension540,035B/SHA
  0e14563420cc55fd09e90d2da92078f8646de4ace90ee212b5228c554724db70を確認。
  RAM artifact13035970297/244,810B・13034919667/246,127Bから必要JSONのみ保存。
  cold/full gate・全20/839・元GUI・1B/API・Python/VM廃止は未達。Astraへ次のまとまった
  App-owned evaluator境界を質問中で、次の実装はまだ選択していない。

- 次sliceを実装済み・実CI待ち（Astra high回答、Sol6.1 xhigh重要実装）: Appのtype/const/enum/
  六つの長さ制約と既存四numeric boundを、自身のNode/TypePolicy/error経路へまとめる。
  generic scalar_ruleは他の独立APIとpattern/format等にまだ必要なので削除しない。
  typeのensure_list/import/一時list、exact scalar比較の汎用helper往復を除く。
  実iterator・foreign __class__/比較/len・短絡/変更/実error handleは維持し、container
  走査・pattern/format/reference・uniqueItems等を同時変更しない。
- Rootがmodelの実foreign境界を独立source定義した: equality_boundaryは4.26
  _utils99–143由来のidentity/str/Sequence/Mapping/own unbool sentinel/再帰操作を保持。
  元helperのmutable globalsへは委譲せず、実比較result objectのtruthを一回だけ適用する。
  type_is_singleは実isinstanceでforeign __class__も読み、type_errorはkind reprを先に
  実再走査してからinstance reprを作る。MIT告知を維持、結果/input cacheなし。
  これらはまだPythonのforeign operation境界であり、family全体のRust完結や削除可能性を
  主張しない。Solはvalue.rsのfamily本体とApp dispatch、新12群/193差分対を実装。
  bool/int/float/巨大int/NaN同一・別object/±0/Unicode/surrogate、126サイズ/bound組合せ、
  live enum/type変更、foreign比較結果のtruth/len例外、genexpr実2段GetIter、偽装__class__、
  除去したgeneric helper globalsを変更口にしないtripwireを含む。既存全groupsは維持。
  Root監査でsingle kindのreprがstr subclassを返す場合のjoin正規化漏れを修正し、余分な
  __format__呼出を防いだ。Source AST3/rustfmt/diffのみ成功、実native/guest CIは未実施。
  4counter形状とsource inventory14件を維持。燃料削減と全製品の同等性は未測定。

- scalar family 2c1238151 native CI41184395732は成功。新scalar12群/193対に加え、
  control18/value16/既存全generic・API・backend・reference・fragment・callout・ownership、
  App37/42/full305-entry453-read比較が一致。extension1,042,408B/
  SHA ebb3d632cf6fe77ee27ea44b68f2a1e4067754bc0dacc2d1e32df9f739d47e96。
  RAM artifact13036462026/65,925B、必要JSONのみ41,428B保存。
- 同じ64件限定のnative Python call profileは27行で60行の保存上限未満。共有libraryの
  ensure_list行はなく、equalは908回（旧25cの19,020/6,651から変化）。App-own foreign
  helperを含む全Python呼出数やWasm fuel比率はこのfilterで測っていない。実64件のnative
  型38,182/数値335、Python value境界0は維持。全体の改善は実guest結果で判定する。
- 同じ2c123でWasm cold/full41184568625と累積診断41184572912を各1回起動、実行中。
  元one-Store/2B/64MiB/絶対30秒を維持。最新実guest完走frontierはまだ25cの19件で、
  このsliceによる全体完走・製品採用・Python/VM廃止は未証明。

- scalar2c実Wasm終了。cold/full CI41184568625は両方元2Bで停止。component
  c03889f5…838d25、prepare後1,979,512,600→0、cold約1.975秒/full約1.777秒。
  元絶対30秒を維持し期限超過・unreachableなし、Store退役・physical payload0。
- 累積CI41184572912は診断整合性のみ成功、完了frontierが19→20 schemaへ進んだ。
  profile_lock_v4を完了し、21件目profile_lock_v5で燃料0。component94194394…09448、
  prepare後1,979,536,847、19件後187,482,898、20件後117,706,594残。20件後18,106frames、
  native型17,348/数値77/Python境界0、pointer962/dynamic891/adapter0/cache0hit20miss。
  one-Store/account・予算更新0・64MiB/30秒・cold-zero/単調性/各件到達/index20と
  退役・payload0を確認。これは累積schema件数であり、issueの全20受け入れ条件を
  達成した意味ではない。未完のone-shot42/fullや全製品移植の証明ではない。
- 両modeの実extension548,575B/SHA
  b26116ba91d2209520df4992bd6346d30518b85f9dccd0e7aa5b0fb514bb4c22を確認。
  RAM artifact13036632594/244,952B・13036197024/246,392BのJSONだけを保存。
  共有helper呼出数とaggregate frontierは改善したが、個々の関数へのfuel帰属は行わない。
  次のApp-owned reference/resource境界をAstraへ質問中。追加設計/実装はまだ未選択。

- Registry binding-view案は今回は見送る。Astra highとSol source監査により、modelの
  specifications・combine後registry・root派生registryは別instanceで、exact Resolverや
  外側canonical methodだけでは内部override由来のregistryをownedと証明できない。
  [pinned rpds-py](https://raw.githubusercontent.com/crate-py/rpds/7277eb681f6efd67eca1bbaa32f78d78bdc044a5/src/lib.rs)
  のitems/keys iteratorはremove内で同一stored keyにもPython __eq__を呼ぶため、
  未知mapの走査を無副作用なkey検査に使えない。未使用fastpathやguard群を追加せず、
  将来はApp自身のbinding構築を一次状態にする別設計が必要。source lookup cacheや
  callback返却unknown registryの所有昇格を導入しない。
- 独立のsource正確性修正をSol6.1 xhighが実装: dynamic anchorで実registry.anchorを
  bindしてから実name getterを読み、同じcallableを一回だけ呼ぶ。Descriptor/getter/callは
  元NoSuchAnchor捕捉内、scope iterator.advanceは外のまま。現Rustの引数準備がnameを
  先に評価する差を修正した。実owned anchorだけを一時patchする3群を追加し、既存14群
  を維持、合計17群/現inventory23差分対。取得順・getterによるmethod差替え・供給例外
  identity/causeを比較し、native dynamic到達を必須。AST/rustfmtのみ成功、実CI待ち。
- 次のまとまったsliceはAstra指示のApp object family properties/patternProperties/
  additionalProperties。三ruleを融合せず実順序・child binding/live変更・combined regex
  の意味を保ち、generic findAdditionalPropertiesへの委譲を外す。registry/URI/resource/
  pointer/dynamic算法は変更しない。Solがobject.rs/stateとFrame接続、新object16群/67対を
  実装済み。三ruleの順序を保ち、pattern/subschema/k/vの既bound参照はouter移行でも
  保持する。extrasは実PySetでhash/eq/dedup/orderを保ち、分類Cursorで旧foreign keyを
  次nextまで保持。分類localsはAP TypePolicy前に解放し、false AP errorではextrasを
  error decoration中も保持する。Rootはmessage helper・CI・source inventory15を統合。
  複合regex、foreign items/unpack/hint、regex/truth/供給例外、dict変更、binding・live値、
  AP分類前後・error context/refs・実set順・weakrefを比較。AST6/rustfmt/diffのみ成功、
  実native/guestは未実施。generic API・既存全groupsと4counter形状は維持。

- 独立anchor修正2a265369a native CI41186476354は成功。fragment17群/現23対、
  scalar12/193・control18/48・value16と全既存generic/API/backend/reference29/callout/
  ownership/App37/42/full305-entry453-readを維持。extension1,043,096B/
  SHA d4605b886f330df56d0ddc37269967f16dda437bcccb7f61065ee6b85209e39d。
  RAM artifact13036129128/65,985B、必要JSON41,685B保存。2aだけのWasm再実行は
  行わず、次object sliceのnative成功後、同じ最新sourceで両guest gateを測る。

- object207e6e82e native CI41187452493は成功。object16群/67対に加えfragment17、
  scalar12/193・control18/48・value16・全既存generic/API/backend/reference29/callout/
  ownership/App37/42/full305-entry453-read比較を維持。extension1,050,336B/
  SHA ed1d276e6215dfd99822c74933d649dfa894de948c9feabf022953796446e73a。
  RAM artifact13037232476/66,563B、必要JSON43,181B保存。64件native call profileは
  全26行(<60cap)にfind_additional_propertiesがなく、旧1,687回から変わった。
  全Python workやfuel比率ではない。実native型38,182/数値335/Python境界0は維持。
- 同じ207e6でWasm cold/full41187640127と累積41187645274を各1回起動、実行中。
  元one-Store/2B/64MiB/絶対30秒のまま同一runを追跡する。現時点でこのsliceのguest
  完走/全製品資格は未証明。最新terminal guestは2cの累積20schema、one-shot両方未達。

- object207実Wasm終了。cold/full CI41187640127は元2Bで両方停止。component
  2ec4b2a3…5f3ce9、prepare後1,979,501,139→0、cold約1.999秒/full約1.786秒。
  元30秒deadline維持・期限超過/unreachableなし、Store退役・physical payload0。
- 累積CI41187645274は整合性のみ成功、完了20件のまま21件目profile_lock_v5で燃料0。
  component e13373a9…42f24b、prepare後1,979,540,439、19件後194,460,126、20件後
  125,031,749残。20件後18,106frames/型17,348/数値77/Python0/pointer962/dynamic891/
  adapter0/cache0hit20miss。one-Store/account/予算更新0/64MiB30秒/cold-zero/各件到達/
  単調性/index20/退役/payload0を確認。2cの20件後117,706,594とはaggregateで数値が
  変わったがfrontierは20のまま。関数fuel帰属やone-shot成功の主張には使わない。
- 両mode実extension557,164B/SHA
  af7b9913d1168ac2bc8627dac2252ea9c3d89afb6271328f5086bbe5c6755030を確認。
  RAM artifact13037467805/245,073B・13038101591/246,317Bの必要JSONのみ保存。
  完走・元GUI採用・全839/20受け入れ・1B/API・Python/VM廃止は未達。
- 次のRegistry sliceは、上のpost-hoc binding-view案と分け、Appが生成する実bindingの
  一次状態をRustへ移す（Astra highの回答、Sol6.1 xhigh実装済み、実CI待ち）。元sourceの
  `DefinitionModel`初期insert、crawlの各insert、combine update、root派生を対象にし、
  実Resource/Registry/RPDS mapを保持する。実mapの走査による所有判定、同じmapへの
  二度目のcrawl、callback由来unknown mapの昇格、結果cacheは追加しない。
  実insert地点でnative bindingを更新し、combineは実updateを一回だけ行い、成功時だけ
  追跡済み入力を同じ優先順位でmergeする。内側items/view/iterator等のslotが未確認の
  枝は元処理を続けopaqueにする。native lookupが省略するのはRPDS.getだけで、実
  Retrieved constructor・実field取得・例外順を保つ。Resource/Specification全体の
  guardを増やさず、既知mapと最小の取得経路だけを確認する。
  原本比較にはdefault rootの実native到達と、unknown/custom slot・callback順・供給例外・
  combine優先順位を必須にする。Rootは3producerの閉じたsource inventoryとCI接続を
  担当。source完成後のnative全比較、同じ元2B/64MiB/30秒のcold/full・累積検証で採否を
  決める。source確認のみ成功し、燃料改善・guest完走・製品採用は未証明。
- 同sliceの未commit初稿のRoot reviewで共有参照の整理が必要になった。native `Arc<Rows>`と
  `Arc<Metadata>`を複数GC対象State/Policyが共有したまま、各`__traverse__`から内部の
  同一Py参照を再報告すると、実strong reference数とGC edge数が一致しない。
  純Rust View/static ownerの保持も外部rootとして正しく残す必要がある。単にtraverseを
  削ってcycle leakへ変えず、Astra highの回答に従いRows/Metadata自身をGC-visible共有
  Pyclassへ変更し、各holderは容器への実Py参照だけをvisitする設計へSolが修正した。
  Rowsはflatなimmutable recordsを共有し、実write時だけnative recordsをCOWする。
  旧parent chainは上書き済みResourceを不要に保持するため除去し、actual mapの追加走査
  なしで自身のentriesだけをvisitする。Metadataも独立policyごとに実Pyrefをcloneした
  holderを持ち、canonical staticと動的holderの寿命を混同しない。
  callback中のborrow/lock回避、immutable view、weakref/GC解放を原本比較に
  含める。4つの`__clear__`はBound receiverから短くtakeし、Pyclass borrow/Storage lockの
  両方を解放してからdropする。source修正済みだが、実CIでの保持契約は未検証。
- 同sliceの不要copy整理: 入口のstate生存確認でViewを作って即破棄する処理を
  `require_available`へ変更し、実root後のViewだけを生成する。候補Rustはabi3-py313の
  現PyO3のabi3側で未提供の`PyType_GetDict`へ依存せず、builtin descriptor経由で得る実live namespace proxyを
  保持し、既知slotの現在値だけ読む。proxyもMetadata holderの実GC edgeとして保持する。
  namespaceの変更・metaclass getter回避を比較し、lookup結果のcacheには使わない。
  新helperは21群（15対比較＋fresh default実hit/production Retrieved catch/GC3/Fake拒否）。
  Rootは3inventoryを16sourceへ揃え、既存全比較を保持して新helperを接続した。
  native64前の新registry3counterのcold0と後の実resource hitを必須とし、累積診断でも
  別3fieldのcold0/単調/最初の実成功時positive/最終index整合を確認する。既存value4fieldと
  reference shape・元export回数・予算・cold/full本体は変更しない。追加counter観測自体の
  fuel費用は累積診断へ含まれるので、旧frontierとの差を関数別改善や純性能値としない。
- primary Registry `3038a7483`の[native CI41206680006](https://github.com/harupipipipi/tobkiri/actions/runs/41206680006)
  は全成功。新21群/15対、実default rootのresource/anchor到達、production field/catch、
  GC3群（共有refcount/View rootとdrop/循環解放とcleanup再入）、Fake入力拒否が通過した。
  既存generic64/API19・backend64+11・reference29/fragment17/control18/scalar12/value16/
  object16/default5/ownership4・App37/42と全caller比較も保持した。実64件の新counterは
  cold全0→resource18,123/anchor6,165/opaque0、旧native型38,182/数値335/Python0。
  extension1,217,240B/SHA
  69b4d321d8edb1793ef95ecd02a321c9af539334e8d8f4dbcfa017ef140b47dd。
  artifact13044995382/67,275BをRAMで読み、必要JSON44,275Bだけ保存した。
  同じ3038で[cold/full41206989379](https://github.com/harupipipipi/tobkiri/actions/runs/41206989379)と
  [累積41206992190](https://github.com/harupipipipi/tobkiri/actions/runs/41206992190)を各1回起動した。
  元2B/64MiB/絶対30秒・同一account/Storeのまま実行中で、Wasm完走・燃料改善・製品採用は
  まだ未証明。207の未達結果を3038のnative成功だけで上書きしない。
- `3038a7483`の実Wasmは終了し、cold/full CI41206989379は両方元2Bで燃料0。
  component e2157f67…dc1e8、prepare後1,979,517,836、cold約1.298秒/full約1.164秒。
  元deadline維持、期限超過/unreachableなし、Store退役・physical payload0を確認した。
  累積CI41206992190は整合性のみ成功。18件完了し、19番profile_intent_v1で燃料0。
  component5273d5c0…92e0、prepare後1,979,508,153、18件後130,256,857残。
  実frame16,099/型15,383/数値70/Python0/pointer867/dynamic789/adapter0、
  primary resource7,305/anchor2,546/opaque0、cache0hit18miss。cold0/単調/到達/index18/
  one-Store/account/予算更新0/元64MiB30秒/退役・payload0の検査は通過した。
  旧20件から18件へ減った。新counter観測費用も含むため、個別関数への帰属や純性能値は
  断定しないが、元guest gateを満たさず採用は保留する。小guard調整を反復する前に、
  この最適化全体の撤去範囲と次のまとまった移植対象をAstra highへ質問した。
  独立のRetrieved.value catch修正と比較は切り分けて保持を検討する。
- 両3038 modeの実extension674,377B/SHA
  ff5a58b8b707c4be8cd94c440647c526deebd2beffd4f588e2dd8fb6346e4528。
  RAM artifact13045206074/245,276B・13045186319/246,601Bから必要JSONだけ保存し、
  binary/ZIPは保存していない。全guest/GUI/839/20/1B/API/PythonVM廃止は未達。
- Astra highの実測後の指示に従い、Sol6.1 xhighがRegistry最適化・専用API・2新規fileを
  全撤去した。Rootもsource inventoryを元15fileへ戻し、新registry observerとCI接続を
  外した。約2,100行を削除し、未使用fast pathを残さない。独立のdynamic Retrieved.value
  をNoSuchAnchor try内へ置く修正だけを保持し、実Original/provider比較を既存fragment
  helperへ移した（旧17群を維持し18群）。履歴・失敗結果・保護sourceは保持する。
  撤去後はsource/AST/format確認のみで、実native比較を次に行う。
- 次候補はAstra high選択のApp-owned container equality。`uniqueItems`は今回対象外で、
  既存callout5の実uniq rebindと原本sort→限定例外時live再走査を保つ。Solの一次source監査で
  seqのidentity/分類/len/zip左右取得/compare/truthと、mapのlen/items/membership/getitem/
  compare/truthの順序は確認した。一方、原本Python frame用再帰counterとStable ABIの
  `Py_EnterRecursiveCall`は別counterであり、cycle/deepをRust stack＋固定limitへ置換
  できない。再帰契約が閉じるまで実装を開始せず、Astra highへ次の最小scopeを質問した。
- 撤去/catch保持`ecae315bc`の[native CI41208526034](https://github.com/harupipipipi/tobkiri/actions/runs/41208526034)
  は全成功。fragment18群と全既存比較を維持し、extension1,050,496B/SHA
  cdfaebcf32646aba98ffca5b006c18296e61ecde4e731543e30e96981fee0ad3。
  RAM artifact13045437918/66,578Bから必要JSON43,214Bだけ保存した。
  同じecaeで[cold/full41208732791](https://github.com/harupipipipi/tobkiri/actions/runs/41208732791)と
  [累積41208734734](https://github.com/harupipipipi/tobkiri/actions/runs/41208734734)を各1回開始し、
  撤去後の元予算での実結果を確認する。まだ未完走であり、改善を先に主張しない。
- 再帰counter差の一次sourceを受け、Astra highはcontainer equalityも今回は延期とした。
  元Python再帰frame/genexprを残す内部往復には、まとまった削減がまだ示されていない。
  次はApp-owned pattern/format familyの実generic carrier往復だけを確認する。実regexと
  FormatChecker.check内部はそのまま使い、型確認・選択・呼出し・error変換をApp Frameへ
  接続する。callback/checkerのlive変更・例外/causeと先行したcatch・PEP479境界を維持し、
  既に直接実行済みの箇所は再包装しない。Sol6.1 xhighがsource gateを監査中。
- 監査結果: pattern/formatは既に`keywords::scalar_rule`からRust closure経由で実
  re.search/checker.checkへ直接到達しており、Python keyword-generator/Validator carrierは
  残っていなかった。移設・再包装・新counterを追加して性能改善と数えない。
  代わりに、実format error変換でcause→message→constructorとなっていた順序をSolが
  constructor選択→message→causeへ修正した。変換中はpublic abi3の実handled-exceptionを
  RAIIで設定・復元し、getter/constructorが投げる実例外へ元FormatError contextを保つ。
  checker自身のcatchを先に完了し、未捕捉StopIterationだけを既存PEP479へ渡す。
  既存prescribed constructor ownerはexceptions.ValidationErrorのまま維持する。
  Originalのprivate imported alias単独変更への互換性を追加せず、比較では両laneへ同じ
  constructorを明示設定する。generic側の既存owner差全体を解消したとは主張しない。
  新helper15群/31対で順序、途中差替え、供給例外identity/context、再入と外側handler復元、
  非callableもgetter後に実callすることを比較する。Source確認のみ成功、実CI待ち。
- 撤去後ecaeの実Wasmはcold/full CI41208732791が元2Bで両方停止。component
  07f8af7c…ca179、prepare後1,979,511,589、cold約2.018秒/full約1.788秒、元deadline維持、
  期限超過/unreachableなし、退役・payload0。累積CI41208734734は20件へ戻り、21番
  profile_lock_v5で燃料0。component5ee2d9c1…1b548、prepare後1,979,514,114、20件後
  125,433,519残/18,106frames/型17,348/数値77/Python0、cache0hit20miss。
  cold0/単調/各件到達/index20/1Store-account/予算更新0/64MiB30秒/退役payload0を確認。
  18→20への回復で撤去を確認したが、元42/full guest gateや全製品の完了ではない。
  両mode実extension557,270B/SHA
  74bbd78829ea32f7e267a7fee8a6df2f45d1822280b61ec10380c37aca6b762e。
  RAM artifact13045079620/244,897B・13044987842/246,374Bの必要JSONのみ保存した。
- format修正`cb741adb2`のnative CI41209957896は新比較の外側handler再入で失敗。
  既存全library groupsとmessage/cause/constructor順・outer復元eventsは一致したが、
  最終SchemaError.__context__が元のouter RuntimeErrorではなくNoneだった。backendの
  最終caller比較へは未到達。artifact13045724568/50,321Bの必要JSON44,028Bを保存した。
  PyO3のnormalized from_value→restoreはSetRaisedExceptionだけであり、元Python raiseが
  通るSetObjectのcontext付与をしないことをSolが一次sourceで確認した。
  最終producerはCのactual BaseException判定（偽__class__/custommetaを読まない）後、
  builtin BaseException targetのpublic PyErr_SetObject→fetchで同じ実errorを返す。
  constructor再実行・手動chain再構築をせず、cause/suppressionとcontext-cycle処理を
  CPythonへ委ねる。非exception factory返却は既存経路のまま。元15群31対を保ち、
  作成済error identity/prior-context/outer-overwrite/backlink/self-cycleを追加して17群43対。
  Source/AST/format確認のみ成功で、実native比較を再実行する。
- `8b2eac5c6`の[native CI41211361636](https://github.com/harupipipipi/tobkiri/actions/runs/41211361636)
  は17群43対と従来のgeneric64/API19・backend64+11・reference29/fragment18/control18/
  scalar12/value16/object16/default5/ownership4・App37/42・全caller比較が成功。
  extension1,052,224B/SHA d7a53d7b…1250f。RAM artifact13045869511/66,946Bから
  必要JSON44,640Bだけ保存した。外側context修正のnative同等性の証明であり、性能値ではない。
  同じ8b2の[cold/full41211646611](https://github.com/harupipipipi/tobkiri/actions/runs/41211646611)
  は両方元2Bで燃料0。component06103791…a0f27、prepare後1,979,497,181、
  cold約1.985秒/full約1.758秒、期限維持・unreachableなし・退役/payload0。
  [累積41211649138](https://github.com/harupipipipi/tobkiri/actions/runs/41211649138)は
  20件完了、21番profile_lock_v5で燃料0。componentf791279f…1f2e4、prepare後
  1,979,508,025、20件後124,989,214残。1Store/account・予算更新0・64MiB/絶対30秒・
  cold0/単調/到達/index20・退役/payload0を維持した診断で、全体完了ではない。
  両modeのextension558,321B/SHA247453f9…fc0be、RAM artifact13046349055/245,211Bと
  13046498098/246,273Bから必要JSONだけ保存した。
- 次sliceはAstra highが選択したApp-owned URI DATA区間。Sol6.1 xhighが新`uri.rs`へ
  pinned CPython3.13.16のjoin/defrag制御を限定移植し、Rootが実owned referenceの2callsite、
  model互換境界、17source inventory（URIとCOPYING.python追加）、CIを接続した。
  generic API19・共有urllib、実opaque resolver、Registry/Resource/retrieval/pointer/anchor/
  dynamic scopeの責任は移さない。原本のstartswith/truth/slice、外側defragの先行選択、
  join中のalias変更と次lookupのlive選択、実2項unpack、resource.id→joinの順序を保つ。
  eagerなEnvironmentのURL alias保持2fieldは除去し、必要な元境界でだけ選択する。
- URIのsource reviewで見つかった整理条件: shared urlsplit cacheのpath/fragment実identityは
  Registry/anchor callbackから観測できる。nativeの後でやり直すのではなく、最初にexact
  ASCII strかを確認し、bracket形・nonempty scheme-less baseは元の全sequenceへ渡す。
  nonfragment resolveではbase/refのliteral #と非relative schemeのbaseも先に互換経路へ
  渡す。これによりcacheをRustへ複製せず、非対応caseの結果を新規文字列で偽装しない。
  direct #fragmentだけは元base実handleとslice挙動を維持し、join/defragを実行しない。
  join-onlyは独立に証明したraw early-returnを残す。不要になったnative hashed-defrag構築と
  その専用helperも削除した。実cacheを使う繰返しpath/fragment・base継承fragmentと
  direct sliceのidentity比較を含む17群146対（対称145対＋owned区間isolation1対）を追加。
  共有urllibの変更がowned区間外まで隔離されるとは主張せず、実shared-resolverとfresh
  default対照を残す。既存fixtureは保持する。actual native/guest比較はまだ未実行。
- URI observerは独立4field（native join+defrag/native join-only/互換resolve/互換join）で、
  実route entryだけを数える。fragment-onlyはjoin+defragへ数えず、内部callback回数の
  指標にしない。native64のcold0→実join+defrag到達、累積のcold0/最初の成功時positive/
  単調/index整合を必須とし、旧value/reference shape・export回数・元予算を変えない。
  観測費用は累積燃料に含む。CPythonの一次license全文と変更概要をCOPYING.pythonへ
  保持し、新dependency・config・reflection guard・結果cache・retryを増やさない。
  source/AST/rustfmt/inventoryのみ確認済み。実native通過後に同sourceの元予算Wasmを測り、
  採用可否を決める。製品Python/VM削除・既存GUIフロー変更の許可ではない。
- URI `b71a972f7`の[native CI41213373132](https://github.com/harupipipipi/tobkiri/actions/runs/41213373132)
  は全成功。新17群146対と全従来比較、actual App37/42・全caller比較を維持した。
  実64件のURI counterはcold全0→join+defrag13,908/join-only1,932/互換0、旧型38,182/
  数値335/Python0。実route到達でありfuel改善値ではない。extension1,072,752B/SHA
  73838bc9…03329、RAM artifact13046548253/67,796Bから必要JSON47,596Bだけ保存した。
  独立Sol監査も4counter・旧shape・元予算・単一Store/account・export上限を確認した。
  native通過後、同じb71で[cold/full41213547854](https://github.com/harupipipipi/tobkiri/actions/runs/41213547854)と
  [累積41213557344](https://github.com/harupipipipi/tobkiri/actions/runs/41213557344)を各1回開始。
  元2B/64MiB/絶対30秒の実結果は待機中。native成功だけで製品採用や全体完了にしない。
- URI b71の実Wasmは終了。cold/full CI41213547854は両方元2Bで燃料0。
  component89063178…c61fe、prepare後1,979,510,397、cold約1.685秒/full約1.505秒。
  元deadline維持・期限超過/unreachableなし・Store退役/physical payload0。
  累積CI41213557344は**20→40件**へ増え、41番surface_template_v1で燃料0。
  componentcd1a6681…77dc4、prepare後1,979,510,697、40件後91,359,231残、型31,358/
  数値215/Python0、URI join+defrag11,417/join-only1,591/互換0。
  1Store/account・予算更新0・元64MiB/絶対30秒、counter cold0/実到達/単調/index40と
  cleanup/payload0を確認。追加observer費用を含む実flow測定であり、関数別fuel比や
  全42件・一括・issue全20条件の完了とはしない。効果のある候補として保持し、製品は
  default-offのまま。両modeのextension577,194B/SHA31e1ac1e…89ffa。
  RAM artifact13047053895/245,670Bと13047291954/248,868Bから必要JSON6,629/89,925Bのみ
  保存。ソース・binary/ZIPをダウンロードしてローカルで実行していない。
- 実結果後の次の優先区間はAstra high回答の元Setup review→listing。
  raw-id HashSetによる所有metadataの小整理を主作業にせず、003の実ownerからAuthority
  snapshot/epoch/source能力を得る正規経路を先に閉じる。実recovery write/同じcandidate/
  resolve/catalog/listingの順序・typed error・cancel/finish/Joinを保つ。必要ABIだけ接続し、
  全19/7wrapperの追加を進捗に数えない。元guest予算未達のまま製品経路を有効化しない。
