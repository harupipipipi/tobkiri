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
- 次の実接続として、同AppHandle/選択済みuser_dataから旧Authority contextを開き、実初期化・
  epoch読取・epoch record読取・closeを行う有限native操作を追加した（Sol6.1 xhigh実装）。
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
- 原本referenceは`3946f5d5f`の[CI41221394933](https://github.com/harupipipipi/tobkiri/actions/runs/41221394933)
  で両OS40ケースへ拡張して成功。v2/v3の実schema upgrade、既存DBのkey未在・DB/key/guard
  mode、guard directory/symlinkを追加し、実failureのerrno/relative filename/contextも保持。
  sourceと実戻りを調べた上で追加しており、反射仮説や任意APIへのscope拡大ではない。
- 最初のnative old-state実装をCI用snapshotとして固定した（6.1 Sol xhigh実装・独立crypto
  source監査、Root統合）。同じtracked job/source/control/invocationで実Open→2read→close、
  private read carrierと既存Detachmentの実copyをworker内で保持してからDATAだけ返す。
  受信DATA消費までSourceCustodyやNativeAccountを延命する案は採用しない。実未完I/Oだけを
  実close receiptに従い既存control予約付きledgerへ保持し、sendとJoinを混同しない。
  `23f5706fc`の[初回native比較CI41226219937](https://github.com/harupipipipi/tobkiri/actions/runs/41226219937)
  ではmacOSの通常2件＋fresh original fixture必須driver1件が成功。全40件のmaterial/error
  と既存key保持は一致したが、committed state比較は検証ツールのJSON境界で失敗した。
  manifest経由のreferenceはarray、fresh observerのPRAGMA結果はtupleだったためで、
  observerのcolumns/index_infoをarrayへ統一する。厳密な型/field/value比較は維持する。
  修正後のstate一致は未確認。Linuxはnative ownerのOS/filesystem proof拒否で停止した。
  製品ゲートは緩めず、Linuxは元40件と明示unsupported拒否・整数回帰・lock/clippy、
  native実DB操作の40件は対応macOSで検査する。
  同sourceのhandoff CI41226219929はpreparation/domain cleanupとStore lineageが成功し、
  strict Clippyのみ5件失敗（借用/cast/if表現）。6.1 Sol xhighが最小修正し、秘密配列の
  by-value copyやlint抑制で通さない。共有capacity CI41226219893は成功。
  macOSインストーラCI41226665165は同sourceで実行中で、Tauri callerのcompileは未確認。
  修正`f77855520`の[CI41228262432](https://github.com/harupipipipi/tobkiri/actions/runs/41228262432)
  は両OSで完了成功。macOSはnative40件のtyped材料/例外・logical schema/committed rows・
  file mode/存在・既存key保持がすべて一致し、実close/容量解放/owner再openも成功。
  Linuxは原本40件・実unsupported拒否/整数の2件、両OSのstrict Clippyと2lock graphが成功。
  2B/64MiB/絶対30秒や元製品のOS proofは変更していない。artifactはRAMのみで読み、
  selected JSON94,074Bだけを保存。DB/key/ZIP/binaryはローカルへ保存しない。
  これは閉じた40ケースの有限操作の証拠であり、残るctor全契約・実Setup/GUI接続の採用
  ではない。Astra highは次もctor不足を先に閉じ、同じBootstrap invocation/control/owner
  の元到達点でこの実bodyを呼ぶ順を指定。独立jobの結果prefetch/cacheや新accountは作らない。
  次の原本corpusは`c0435f566`で40件を保ったままauthenticatedな不正UTF8/JSON等22件を
  加えた。両OSの初回CI41229630601は全原本操作後、fixture manifestの2MiB制限で停止。
  `69ba709cf`で全field/型と上限を保ちJSONの余分なindentだけ除去し、CI41229894658へ
  送った。nonfiniteな観測値はciphertext SHAへ戻してartifact JSONをfiniteに保つが、
  実原本のdecode/例外/副作用は変更しない。native62件の成功はまだ主張しない。
  Sourceで確認した整理境界: surrogateはJSON読込で拒否せず、実canonical encodeの地点
  まで局所decoded表現へ保持する。duplicateの上書きとobject順序を保持し、通常値には
  既存canonを再利用する。sharedCanonや一般モデルへsurrogate型を拡張しない。
  原本C decoderのthreadstate/可変recursion残量はRust Hostへ複製せず、固定1024を原本
  同等としない。証明できないdeep領域はexplicit unsupportedとして実fixtureを残し、
  不一致を成功へ読み替えない（Astra high回答）。default-off接続の実装は進められるが、
  その未対応を残して旧state操作全体や製品Setupのqualificationを宣言しない。
  `69ba709cf`では両OSの実原本62件が完了し、Linuxの全検証も成功した。macOSは製品操作
  前の検証manifest読込で、未使用reference_state内のlone surrogateをserde_jsonが拒否した。
  full manifestは原本の文字列を保持してPythonによる独立DB比較に使い、Rust driverには
  同じfresh入力/expectedだけをnative-fixtures.jsonで渡す。DB比較を省略したり文字列を
  書き換えたりしない。原本summary99,850BをRAM-only artifactから保存した。
  特にdeep2000は実原本のdecode/canonicalを通り最終audit-chain-invalidまで到達した。
  この既知入力は局所codecで処理する必要があり、unsupportedを同等として先送りしない。
  極端な未観測depthと区別する（Astra high再回答）。surrogate/NaNの先後と非objectも
  追加して総65件へ検証を拡張中。driverは未実装のnative条件を明示outcomeで全件観測し、
  最後のstrict一致assertは維持する。未対応を原本のtyped exceptionへ偽装しない。
  `9fd38e01e`のCI41230909530は両OSの原本65件と通常回帰/strict Clippyを通過し、macOSの
  native65件もすべて観測して実close/容量解放/owner再openを終えた。strict結果一致assert
  は未実装差分で失敗しており、成功へ変更しない。独立state比較は検証ツール自身の
  再帰first_differenceがdeep2000でRecursionErrorとなり未完だった。型/field/valueと
  元DFS順を保つ明示stackへ変更し、入力や比較項目を削らず次CIで検証する。
  artifactからselected JSON135,415Bを保存。製品decoderの深さ対応とは別の修正である。
  併走した[Desktop CI41226665165](https://github.com/harupipipipi/tobkiri/actions/runs/41226665165)
  はsource23fで完了成功。新しいTauri callerを含むlibrary635件/build-script123件/
  runtime-resource18件、9つのfinite test、cold boot、DMG作成とmounted検証が成功。
  282,099,794Bの非公開用CI/E2E配布物はmetadataのみ確認しダウンロードしない。
  既存配布物はまだPython/PackVMを含み、このCIだけでnative Setup/GUI会話採用やVM廃止
  を完了とはしない。
  次の検証は、実historical v1 fixtureから12種のlease入力を追加して総77件へ拡張する。
  nested principalとunknown kwargsの先後、scopeの構造/要素/quotasの先後、数値function ID、
  string/dictのcall_chain、bool/int/float重複とunhashable/長さ短絡を対象にする。
  実原本keyで復号し、原本authority_digestとFernetで1行だけ更新してidentity/hash検査後の
  consumerへ到達させる。期待値の詳細は原本の実ctorで観測し、nativeの値で作らない。
  Source比較ツールの非再帰修正`9ca3a5138`のCI41232275126はstate比較を最後まで実行した。
  65件すべてlogical state/key保持は一致し、typed材料/例外の差は23件として失敗を保持。
  selected JSON149,092Bを保存した。原本77件の追加は`c45407e6e`/CI41233464333で実行中。
  重要Sol6.1 xhighが旧Authority専用のflat arena JSON/lease consumerを実装してfreezeした。
  非再帰parse/canonical/Drop、ordered object/duplicate/surrogate、元の例外発生順と
  scalar型を維持し、sharedCanon全体や依存は変更しない。旧normalize_leaseの重複を除き、
  consumerを小moduleへ分離する。新しい値は以前の64倍係数を流用せず、型/Vec growth/
  reallocの共存、canonical scratch、normalized leaseを同じaccountへ事前予約する。
  Macの通常unitを3件へ、Linuxは2件のままにする。Source/rustfmt確認のみであり、
  77件の実native結果とcompiler/strict lintは次CIで確認する。
  実装`4fe22073b`の[CI41234273534](https://github.com/harupipipipi/tobkiri/actions/runs/41234273534)
  は両OSで完了成功。macOSは原本77件のtyped材料/例外・logical state・既存key保持、
  各操作の実close/容量解放/owner再openがすべて一致。Mac通常3/Linux通常2、両OSの
  strict Clippy/2lock graph、Linuxの原本77件と明示unsupported-owner拒否も成功した。
  selected JSON180,987Bのみ保存。deep2000、surrogate/NaN先後、12のlegacy lease境界も
  この実native比較を通った。極端な未観測depthと既存IO/RNG/clock/close故障の全面資格、
  実製品接続・GUI/1B/API・全20条件は引き続き未完で、材料-only unitを製品採用にしない。
  次接続のSource再確認で重要な順序差を発見: 実profile_captureのsnapshot canonicalは
  AuthorityStoreのwith内、close前に実行される。材料-only77の返却後にcanonicalを行う
  接続は、canonical/close失敗のsurfaceとcontextを変える。Astra highは同じ固定native
  bodyへ元snapshot helperを移し、現在bundle digest→open/init→epoch→record→元protocol
  canonical/digest→closeの順を指定した。DB contextをguestへ跨がせる仕組みは作らない。
  bundle digestは候補DATAでありCore grant/epoch/承認ではない。余分な事前検査/正規化を
  追加せず、不在DBを作らない。同Bootstrap invocation/control/sourceで実bodyを借りる。
  材料-only77は維持し、新しいsnapshot sub-operationの成功/失敗とcloseとの組合せを
  原本helper/実contextで独立比較する。後続Bootstrap cleanupは別階層として扱う。
  新しい独立referenceは材料77件を保持してsnapshot16件（総93）を追加する。原本helperを
  actual with内で呼び、first epochとrecordの別変化、safe-range拒否、genesis/v1と不正stateを
  観測する。canonical成功/失敗とclose成功/実close後のRuntimeError注入の4組合せを含める。
  この注入はcontext/surfaceの制御フロー投影検査であり、OS/SQLiteのphysical-close故障の
  資格証拠とはしない。実close後の状態とkey保持は引き続き独立比較する。
  Sol6.1 xhighのsnapshot固定操作を追加しsource snapshotとしてfreezeした。借用APIの
  run_snapshot/snapshot_resultは同Read/invocation/control/sourceを使い、成功DATAも
  canonical errorも実close後だけ公開する。close errorには先のactual errorをmoveで
  contextへ保つ。bundle文字列の追加検査/正規化、guestへ跨がるDB contextやTauri IPCは
  追加しない。Mac通常unitは4/Linux2。
  実装`2cb833cf8`の[CI41237506970](https://github.com/harupipipipi/tobkiri/actions/runs/41237506970)
  は両OSで完了成功。macOSは材料77＋snapshot16の93件でtyped結果/例外graph・独立観測の
  logical state・既存key保持がすべて一致し、実close/容量解放/owner再openも成功した。
  first epochとsecond recordの別変化、canonical拒否と実close後の故障注入のcontextも
  比較対象に含む。Mac通常4/Linux通常2、両OSstrict Clippy/2lock graphも成功。
  Linuxは原本93件とunsupported-owner拒否を検査し、native DB比較は対応macOSのみ。
  RAM artifactからselected JSON218,195Bだけ保存し、DB/key/ZIP/binaryは保存していない。
  次は同Bootstrap invocation/control/sourceの元到達点への接続。独立jobの材料やdigestを
  prefetch/cacheとして渡すことはしない。既存Setup/recovery/catalog/追加source/Host引継ぎ
  の全体と、未達の実Wasm予算を閉じるまで製品経路はdefault-offを維持する。
  次のsource監査で、元prepare_bootstrap_profile_reviewのcaller自体は現Guest closureに
  含まれず、read/stat wrapperにもcatalog/recoveryのnative bodyがないと確認した。
  Astra highはgenericなreview orchestrationをRustへ移す区間を指定。元Pack-ownedの
  catalog/resolveを純DATAに限定する仮定は撤回し、元IO到達点で許可resourceへ行う汎用Host
  操作を接続する。snapshot前とresolve内のlock再readを畳まず、Pack shape/IDはCoreへ
  持ち込まない。journal recoveryには書込み/DB初期化の別枝があり、不在DBを作らない
  snapshot APIを代用しない。19wrapperの存在や独立snapshot単体を全Setup実装としない。
  Rootは別のclean原本checkout・元packaged fixture上の既存Setup/successor6テストを
  実行する独立CIを用意した。原本lockの版/hash選択は既存reference helperを共有し、
  CPython3.13用rpds行を明示選択する。
  `5151aedd6`の[原本Setup CI41239656333](https://github.com/harupipipipi/tobkiri/actions/runs/41239656333)
  は5成功/1失敗。未変更のtest_review_and_cancel_are_read_onlyはconfirmation内容を通過し、
  最後のuser_data不在assertに失敗した。原本自身の失敗を消したりassertを弱めない。
  Sourceのpin predecessor照会はProfileDefinitionStore constructorでcreate=Trueへ到達
  するが、生成物の内訳/役割は未実測。次CIで原本関数を置換しないread-only metadata
  observerを使い、初回no-journalのbefore/return点/afterを確認する。observerは対象へ
  lstat/scandirだけを行い、ファイル内容・鍵を読まず、生成/修復/削除をしない。
  Astra highは実副作用を確定後、既存testの非書込を限定bugfixとしてnativeへ反映する
  方針を支持。原本完全一致とは区別し、既存storeの排他/競合保証やjournalありのwriteful
  recoveryを落とさない。native review/GUI/完全recoveryや製品採用の証拠ではなく、
  fixture/DB/keyはCIにだけ保持する。
  observer `40086b3e0`/CI41240029648は原本の同じ5成功/1失敗を保持し、実生成物を確定。
  beforeはuser_data不在、ProfileDefinitionStore.__init__復帰でuser_data/profiles各0700、
  _locked復帰でprofiles/.profile-definition.lock(0600、0B)、afterもこの3つだけだった。
  index/Authority/keyは作成されていない。metadata JSON19,194Bを保存し、原本副作用を
  同等移植すべき根拠にはしない。nativeの初回非作成修正は限定bugfixとして区別する。
  次のpublication prelude referenceは元_recover_bootstrap_publicationを実行し、元pointer
  store constructor直前だけ明示test sentinelで停止する。元journal/lock/strict read/key/
  HMAC/predecessorは実処理し、NoJournalは元outer/innerの2呼出しを通す。37のfresh入力を
  準備し、追加/不正key、uppercase/nonASCII HMAC、JSON/順序/symlinkと前倒し検査を比較する。
  `4ea245e1e`の[原本prelude CI41241103672](https://github.com/harupipipipi/tobkiri/actions/runs/41241103672)
  は両OS37件成功。uppercase HMACは認証失敗、nonASCII HMACはraw TypeError、31個の
  Unicode codepoint keyはweak、32個とPython whitespace trim後のkeyは元pointer到達を
  実測。candidate/confirmationの追加検査をこの段階へ前倒ししないことも確認した。
  必要JSON135,214Bのみ保存。重要Sol実装のstrict decoderについて、duplicate hookと
  NaN/huge integer/surrogateの先後、元depth64/65と未完JSONの先後という具体的懸念を
  調べるため、同じ原本処理へ10件追加し総47件とする。独立state/key比較も配線準備した。
  `d5743618a`の[CI41242243436](https://github.com/harupipipipi/tobkiri/actions/runs/41242243436)
  は原本47件が両OS成功。depth64→後続envelope形拒否、depth65→canonical depth拒否、
  未完deep JSON→syntax拒否という先後も実測。必要JSON176,416Bだけ保存した。
  Sol実装の構造空白変換がquoted comma/colon/backslash/Unicodeを変えないことを確認する
  1件を加え総48へ進める。App-denied tagの比較には実原本factoryの型を有限CI bindingと
  して渡すが、実製品factory/GUI接続を証明したとはしない。この観測境界は後半recoveryや
  実review/listing成功ではなく、48件の実結果/native比較は未確認。
  DB/key/fixture/ZIP/binaryはartifactに含めない。
  `f47315637`の[CI41244202116](https://github.com/harupipipipi/tobkiri/actions/runs/41244202116)
  は原本48件が両OS成功。nested Unicode/quoted punctuationのHMACも元pointer境界へ到達し、
  必要JSON179,410Bだけ保存した。重要Sol6.1 xhighは実ReviewListing入口と元journalの
  最初のnative bodyを実装してsource freeze。metadataとは別のreview能力、同Store cancel、
  同AppHandle/source/controlを使い、結果prefetchやnew begin/予算変更は追加しない。
  不確実IOだけを実source付き既存ledgerへ移し、純DATAのためWork/guest graphを保持しない。
  NoJournal/NoIntentは元2呼出し後CatalogNotBound、認証済intentはVerifiedPointerNotBoundと
  して明示拒否する。後半recovery/listing/製品factory/Unicode predecessorのNFC等は未完。
  旧JSON arenaのstrict専用入口とHMAC空白変換は局所化し、legacy overwrite入口は保持する。
  Root統合レビューでowned Review診断がReadのprepaid reservationより長生存する順序を
  発見。Astra high確認後、Solがfunded detachment後・runner.finish前に元owned診断を破棄
  するよう修正。投影失敗にも同じ順を使い、FD/source/cleanupはrunnerに残す。getterの
  CoreBufferは従来のfinish後破棄を保持。実destructor probeの回帰3件を追加しfiniteは
  9→12件。Root fresh driverは独立したOS lock再取得・close receipt・容量0・owner再open、
  元typed結果/state/keyを検査する。Source/rustfmt/ASTのみ確認済みで、実native48比較、
  既存93回帰、strict ClippyとTauri compileは次CIで確認する。
  source固定`e238fcdbb`の初回CIはlibrary compile・既存unit・Store cancellation・domain
  cleanupまで進み、strict Clippyでunused mut/collapsible if/静的fallbackのlazy closureの
  3件を検出。lint抑制を使わず、元評価順と鍵copyを保つ最小表現へ修正する。
  初回publication Mac jobのnative-driver stepはまだ実行中で、stepにcompile時間も含む。
  実body開始は未観測であり、遅延からdeadlockを断定しない。次CIのtest-only progressで
  case開始/実解放を分け、同じ初回handleも終了まで追う。新48比較の成功はまだ主張しない。
  初回e238 Mac driverはcompile7分55秒、実body0.68秒で48件すべてtyped結果/例外・独立state・
  既存key保持が一致した。実close/OS lock再取得/容量0/owner再openも成功し、停止では
  なかった。初回全体の失敗は上記3lintのみ。必要JSON232,970Bを保存した。
  最小修正`54c6446c9`の[CI41247998757](https://github.com/harupipipipi/tobkiri/actions/runs/41247998757)
  は両OSで完了成功。macOS native48一致/実解放、両OSstrict Clippy/2lock graph、Linuxの
  実原本48も成功した。必要JSON232,925Bを保存。e238の既存Authority93回帰もtyped/state/key
  比較は成功（全体は同3lint失敗）し、JSON218,240Bを保存した。ここでのApp-denied型は
  有限CI factory bindingであり、実製品factory・Tauri実caller・全recovery/listingではない。
  同54cの[Desktop CI41248377655](https://github.com/harupipipipi/tobkiri/actions/runs/41248377655)
  は全工程成功。実Launcher library638件（finite12件を個別ログでも確認）/build-script
  123件/resource18件が全成功・skip0。sealed packaging/cold boot/DMG/mounted検証も成功。
  非公開CI用artifact280,138,053Bはmetadataだけ取得し、必要な証拠JSONのみ保存した。
  製品Python/PackVMを含む既存配布経路の検証であり、全native Setup/GUI/1Bの証拠ではない。
  次のSol6.1 xhigh実装は同journal guardのprivate lexical scope内で、2回目のpredecessor
  parse→実current pointer/snapshot読取→全10field比較→eager候補5key→古いintentのclear/
  保持を行う。close後にVerifiedIntentを再開する公開carrierや別lock/accountを増やさない。
  非staleはcandidate['profile_id']の実lookup後、未接続_bootstrap_profile_id()の直前で停止。
  Root比較は元48入力を保持し、29入力を追加した77件のv2 oracleへ拡張中。元pointer store
  を置換せず実snapshot検証・stale clearを観測し、outerとinnerの間に閉じたfixtureを置く
  1件でinner Falseの保持も検査する。数値snapshot IDは有効defaults pointerからの負例。
  `8b8001962`の[CI41252586559](https://github.com/harupipipipi/tobkiri/actions/runs/41252586559)
  は両OSで完了成功。macOSの77件は実typed結果/例外graph・独立file effects/key保持が
  一致し、実close/別descriptorでのOS lock再取得/容量0/owner再openも成功（body2.41秒）。
  Linuxは原本77件のみ、両OSのstrict Clippy/2lock graphも成功。元48入力も新到達点で
  再観測しており、旧48期待値を固定する早期停止flagは追加していない。
  同sourceの[Authority回帰41252586573](https://github.com/harupipipipi/tobkiri/actions/runs/41252586573)
  も両OS成功し、Mac normal4＋実93比較、Linux normal2＋原本93が成功した。
  小artifactはRAM内でsize/SHAを照合し、必要JSON480,011B/225,052Bのみ保存した。
  同8b800のDesktop CI41253253796はcompileを通過し、library638成功/3失敗/skip0。
  既存finite12件はすべて成功、新pointer/snapshot caller3件だけreceiveの30秒timeoutで
  失敗した。失敗したInstaller/cold boot/DMGは成功と数えず、元Native77/93成功とも分ける。
  Solが原因を特定: test Sourceのcheck_original_ownerは常にOkで、diagnostic=Noneの
  新3件はcopy_inputのcheck_liveを通らず、fixture配置前にNoJournalで終わっていた。
  test owner gateを同Sourceのcheck_liveへ委譲する最小修正を追加。製品処理・timeout・
  予算は変更せず、実owner gateで停止→fixture配置→resumeの順に直す。再実行は未確認。
  全recovery/実factory/GUI接続、NFC、bootstrap ID/catalog以降は未完のまま。
  次の削減候補をSourceで確定: 元denied factoryはExceptionを返すだけで、cause/contextは
  Hostのraiseが作る。Host ProtocolErrorをGuestへ往復して作り直す追加bridgeは不要。
  Astra highは同Storeの実factory ref＋有限観測DATAと、Host cause/contextを別由来で
  保持する方針を採用。まず既存import-values/denied/describeの限定接続をSolが実装し、
  原本8case（同じmessageの別callもfresh例外）と独立lifecycle回帰で比較する。
  原本factory8件のreference CI41254654137は両OS成功。実port.is_resolution_deniedを
  追加したv2 CI41255001377も両OS成功し、actualkindとfactory DATAを比較できる形にした。
  Rust/実componentとの比較はまだ未実行。Guest例外にcauseを設定済みと偽らず、一般exception RPCや
  Core内のPack class固定、ownedReview文字列へのCoreBuffer資金混在を追加しない。
  Sol6.1 xhighの第一factory単位をsource freeze。同Storeの既存import-values→denied→
  実返値限定describeをRustへ接続し、Host cause/contextは別由来の有限native graphとして
  保持する。例外の一般fields公開はせず、実fresh factory refだけargs/None/None/Falseを
  builtin descriptorで観測。provenanceは既存Guest64MiB heap上で、release/rollback/retire
  と再利用/raised時に失効する。既存WIT署名/予算/account/Store/製品dispatcherは不変。
  Rootがchild future内だけに返値を保持しないことを再確認し、固定runner pendingへ
  実result/view/error共有を保持。retire/IO wait後に解放し、正常返値/Factory MethodRaisedは
  finish後dropのcarrierへmoveする。Root独立driverも受領直後に保持し、観測失敗で先に
  破棄しない。transport5件、実worker4件（不正describeの契約拒否と実Store退役を含む）を
  追加、独立actual component8比較を既存producer/consumer CIへ配線した。Source/AST/
  8adapter inventory/format/YAMLのみ確認済みで、実Rust/Guest/新19finiteはまだ未実行。
  async journal/getter接続、NFC、完全panic/cancel/IO fault、製品採用とは分ける。
  実factory区分は`178c6341b`のCI41260909785で成功。原本8件と実Wasmが一致し、
  before finishは22,218,403B/3予約、finish後は5,618B/1予約かつSource destructor済、
  DATA破棄後0B/1予約、既存allocator実reap1後0B/0予約・再openまで確認した。
  先の895 CIは最終予約0検査だけで失敗。既存CUSTODYはArc/Weak唯一性の確認まで0B予約を
  保持する契約であり、Work/source/IO循環ではなかった。Nativeを変更せずdriverが元reapを
  完了するよう修正し、assertは維持。必要JSON14,226Bのみ保存、Wasm binaryは未取得。
  owner gate修正bc3eのDesktop CI41256412808も641/123/18・finite15・DMGまで全成功。
  新factory4を含むfinite19も同178のDesktop CI41262201810で全成功。実645/123/18、
  coldboot/DMG/mounted検証まで通過し、artifact280,225,007Bはmetadataのみ確認した。
  追加の削減候補: 新Reviewは同invocationの固定bodyを直接呼ぶ。別jobを作る旧試作の
  read_original_authority_snapshot/OriginalAuthorityJob/Dataは現在のtracked callerがなく、
  将来のsnapshot接続後も独立prefetchとして使わない。直接snapshot接続と実caller回帰を
  確認後、これらの試作wrapperだけを除く。再利用中のRetained/typed detachmentと原本93
  library比較は残す。legacy製品APIや未移植機能を削る許可ではない。
  JSON/surrogate/depth・不正leaseの例外投影、同時key作成/複合I/O/close、Linux process情報
  read内部close receipt等は未閉鎖。全Open同等・既存DB採用・GUI接続の完了とはしない。
- cryptoの余分なowned key配列copyをZeroizing先作成へ整理した。Fernetは既存RustCryptoの
  AES/CBC/HMACと既存canonを使い、独自AES/HMACを増やさない。固定sourceのBase64と
  HMAC→decrypt/padding順は独立監査で差を未発見。明示owned bufferのzeroizing Dropと
  HMAC/SHA内部・全stack/registerの消去保証を分け、全copy消去を主張しない。
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

## 019: ID検証に必要なUnicode処理と生成表の重複

- 状態: **Unicode primitiveのSol6.1 xhigh実装・原本表生成・両OS全域比較が成功、製品未接続**。
- 場所: harness-nativeの`unicode15_1.rs`/`unicode15_1_data.rs`、
  `tools/unicode15_1_reference.py`、既存`packs/ordered-python-compat/src/unicode15_1.json`。
- 問題: Core ProfileIdの別規約やHostのUnicode版を流用すると、元canonical IDの
  codepoint長→NFC→lower/prefix→regexの順序・診断が変わる。既存15.1表はprintable/
  space/decimalだけでNFCを持たず、新たなPython Host往復も不要。
- 整理: CPython3.13.16/Unicode15.1から静的NFD/CCC/composition/lower/printable配列を
  CI生成し、Rustは呼出元scratch・元work meter・funded writerだけを使う。表の実行時
  parse/cache、新account、別allowanceは追加しない。printable表の生成元共有は今後の
  削減候補だが、旧Guest表の廃止は既存全consumerとの一致を確認してから。
- 維持: surrogate、NFC composition blocking/exclusions/Hangul、文脈Sigma、Python repr。
  生成CI41263883959は成功し、公式19,074行の36,482 unique NFC入力、全1,114,112
  codepoint、32contextの原本観測と104,440Bの固定表を生成。実装commit821231531の
  CI41264808102はLinux/macOSともRust11単体・公式36,482 NFC全配列・32context・
  全1,114,112 scalar NFC/lower・128codepoint単位の全域repr digest・strict Clippyに成功。
  再生成表もbyte一致。選択JSON8,095Bだけ保存し、fixtureやZIPはローカルに残さない。
  cfg(test)内のprimitive比較なので、実NativeInvocationのscratch予算・型観測・opaque
  cause・async journal/getter・製品採用の成功には読み替えない。
  原本getter/Host検証は別toolの69入力で観測し、元のport/factory/validatorを保持する。
  同一test Source destructor witnessは実factory/getter driver間で共有し重複を減らす。
- 次のApp adapterもSol6.1 xhighが`app_bootstrap_identity.rs`へ隔離して実装した。
  同じUnicode helperとfunded writerを使い、元のtype/empty→length→NFC→lower/prefix→
  ASCII fullmatchと5種のProtocolError messageを保持する。成功は元slice、処理/資金/
  transport failureは別のouter errorとし、ID不正へ丸めない。実装835ed9818の
  CI41266515519はLinux/macOSとも通常8単体・Rootの原本returned58比較・strict Clippyに
  成功。正確ProtocolError messageと成功slice identity、診断funding解放0を確認し、
  選択JSON150,205Bを保存。raw getter例外11件はこのadapter外と明示する。
  Tauriへ同じ規約を二重実装せず、今はmigration専用App moduleのcfg(test)内で比較する。
  実getter/opaque causeは別のSol実装で有限観測とrunner-held custodyを追加したが、
  Guest6回帰を含む137 Harness testと実component生成は835のCI41266520979で成功。
  実default getter driverも20.78秒で成功し、fuel1,440,919,069→1,436,898,024、元deadline/
  sameScope/extra inspection0を確認。finish後4,218B/1予約、DATA解放後0B/1予約、実reap1後
  0B/0予約、Source destructor・owner再openまで成功した。必要JSON4,033Bのみ保存。
  新caller7を含むfinite26はDesktop CI41267778512で全件成功した。実測652 library /
  123 build-script /18 resourceとpackaging・cold boot・DMG検証も成功し、選択JSON13,235Bを保存。
  canonical adapter・実factory・同journal guardの接続はc6d700124で実装し、実Wasmの
  連続区間CI41275395353は通常default getterのvalid/mismatch計2操作で成功した。
  別FDでfinish前のlock保持・finish後の取得、元deadline/1Store/account/0reset、
  原本とのstate/file/key比較、DATA解放/実reap後0B/0予約・Source destructor/reopenを確認。
  選択JSON5,635Bを保存。後続Desktop CI41279048043は7cdb66411で実661 library /
  123 build-script /18 resource、Sourceにあるfinite35件すべて、packaging/coldboot/DMGが成功。
  artifact13073996617（280,685,998B）はmetadataのみ、選択証拠20,515Bを保存した。
  controlled69/全147native比較、catalog/Authority以降や通常GUI採用は未達のまま。
  次の原本連続区間もcf4acd29f/CI41266973919で両OS147入力が成功した（native比較なし）。
  旧77入力を保持し、raw69とcandidate mismatch1を追加。元helper/factory/比較後の
  workspace Path純構築・immutable runtime取得まで実行し、host_profile_catalog入口で
  明示Boundary停止する。sys.settrace/別catalog実装を増やさず、元の自然な呼出し境界を
  使う。Boundaryはtyped errorではなく到達状態/cleanup用で、catalog/Authority以降は未接続。

## 020: Project所属を一律拒否する保存送信と所属変更の待機漏れ

- 状態: **修正7cdb66411でfrontend59件、Linux/macOS各backend341件成功。capture強化は未実行Source**。
- 場所: `saved_bridge.py`、`rumi_turn_runtime_pack/runtime/saved.py`、
  `tobkiri_protocol/saved_conversation.py`、`saved_project_context.py`、
  defaultspackの`App.tsx`/`historyMembership.ts`/`savedCodingContext.ts`。
- 問題: 保存送信の一律Project拒否は、元のGroup会話・複数フォルダの表示と会話継続を
  失わせる。History所属変更のProject照会/CAS/ACKと、既存モデル変更の保存待機を
  終える前に捕捉した文脈で送信すると、表示した所属と実送信がずれる。
- 整理: 既存の署名済みProject owner readを共有し、canonical membership/aliasと
  owner-persisted DATAを照合する。新Operation、UIからの権限/承認flag、参照snapshotの
  send権限昇格は追加しない。2つのsaved callerにoptional consumesと既存readへの
  requested Profile edgeを宣言するだけで、実grantは既存承認経路を必要とする。
  Sourceはcanonical catalogとProfile intentを先に更新し、manifest/sidecar/index/
  executable/profile lock/bundleは既存generatorで生成する。生成manifestをauthor sourceにしない。
  CI41277008561のgeneration job124761232096で実UI buildと既存全generator/strict integrityが
  成功。変更Packだけをdefaultspack4.0.2、Turn/Orchestration/SideChat/UISettings1.0.1へ
  更新し、公開Contractのversion/signatureは維持した。実artifact13071058988（1,343,797B、
  SHA256 71a221cdd0c1c90521c562265ddf6e5a58a45080845be717329198ef2d806dc3）をRAMで検証し、
  source/patch hashと38パスを照合して37 JSON＋1生成JSだけ取り込んだ。ZIPは保存しない。
  patch4,914,321Bは生成textだけで、source receiptを別保存。frontendは59/59、0skipと
  tsc/既存boundary/security lintが成功した。
- 維持: Historyの所属move/clearは会話のexecution workspace・全binding・順序・primaryを
  変えない。Composerで新Projectを明示選択した場合は、元の操作が保存したworkspaceを
  使用する。Project自身のprimaryと会話のworkspaceを等しいと要求しない。No Projectや
  完全なbinding配列だけのinert chat DATAから、primaryやtool権限を新しく合成しない。
  実filesystem効果は従来の会話execution workspace owner・mount lease・Gateで判定する。
  Linked childは既存のparent linkからその都度解決し、childへ権限や文脈copyを永続化しない。
- 競合: 既存mutation barrierとoperation identityで実ACKを待ち、canonical会話を再読して
  contextを導出する。失敗した試行は停止し、fresh owner DATAで表示を戻す。自動再送や
  恒久的な新UI modeを作らない。各effectの最後のProject readをそのeffectのsnapshotとし、
  次のeffectは再読する。別owner間の原子性・事後の遡及取消は保証せず、既存revocationと
  append CASは保持する。DATA marker/digest自体をcurrentnessやauthorityにしない。
- 削除前提: 追加したPython App policyは既存Wasm Pack内の責務。Rust Hostへの全移植や
  製品Python/PackVM廃止の完了を意味しない。旧拒否経路の廃止にはowner/linked/workspace/
  ACK race回帰、実生成物のdigest整合、通常GUIでの同じ機能を確認する。全受入条件は未完。
  最初のbackend試行はstale manifest、その次は生成patchを当てたdirty checkoutに対する
  既存fixtureの拒否で停止した（両OS各341 setup errors）。製品動作成功には数えない。
  CIはgenerateとverifyを分け、生成物をcommit後にverify modeで同じclean sourceの
  owner回帰を実行する。source/clean検証の無効化やテスト内再生成は行わない。
  5ae2b0265のfresh build/generation CI41277582684は変更パス0/patch0Bで再現性も確認。
  clean snapshot 7cdb66411のCI41279045948はfrontend59/0skip、Linux341/0fail、macOS341/0fail。
  workspace差替えの元PermissionErrorと保存前拒否を維持し、旧テストは変更していない。
  この結果は以下021のcaptured code所有・retire・包括的会計の達成とは別の資格。

## 021: foreign Pack helperのambient importとcaptured classのDATA残留

- 状態: **2782b2d35のreal-loaderを含む両OS440件成功。legacy capture解放/包括的会計は未完**。
- 場所: `captured_provider_modules_v4.py`/`artifact_materialization.py`、production composition、
  conversation-storeの`runtime/saved_project_context.py`、Turn host/saved、Project read factory。
- 問題: 自Pack importはprivate namespaceへ入るが、foreign Pack importはambient
  sys.modules/sys.pathへ抜ける。sidecar digestやpack_dependencies追加だけでは呼出元の
  code captureを固定できない。App dataclassもinstance→class→method globals→loaderで
  fields以外にSourceを保持し得る。
- 整理: resolverとmembership/identifierをconversation-resource ownerの同captureへ置く。
  App compositionが既存selected ConversationRead/ProjectRead factoriesをoperation内で借り、
  captured validatorを固定依存にする。Core/Turnのambient App importを除去し、known saved
  consumerのprivate portだけを結線。新Provider/Function/grant/loader/global registryは不要。
- 維持: signed readと現在のoperation/profile/consumerを検証。NoProjectではProject capture/
  validator/identity getter/bindingを必須化しない。旧ownerのhookなしは従来validatorへ進み、
  plain chat/canonical codingを保持、Group/aliases/bindings/rootのproof不足は明示拒否。
  返却factsはnestedもexact builtins/Host-static DTOだけ、port参照はfinallyで解放する。
- 受入Source: real loaderでpoison、disk差替え、選択Pack更新、Project不在、別scope/consumer、
  wire copy、DATA graph、port weakref、旧ABI互換を確認する。
  clean 2782b2d35のCI41283852793はLinux/macOS各440/0fail、新capture16ケースの実行も
  ログで確認。frontend59/既存lintも成功し、選択証拠7,444Bを保存した。
- 削除前提: accepted captureはglobal _CAPTURES/sys.meta_path/sys.modulesに残り、production
  retireへrelease/refcountがない。raw512MiB上限はcompiled code/globals/copiesの同invocation
  会計ではない。port参照解放をSource最終解放と読まず、汎用Python lease/frameworkを新設しない。
  実Rust Host owner/funding/retireとWasm App policyの同等性を閉じてから撤去する。
  今回はcode closureとDATA分離だけで、64MiB達成/Native採用/製品Python廃止を認定しない。
  実装be5b0727c後の生成ではexplicit same-owner adapter4件のstale pinを検知し、7d18039cfで
  legacy identity/期待値を変えず実implementation bytesのdigestだけ更新した。
  CI41283183002はfrontend59/既存lint/UIbuild/全生成・strict integrityが成功。
  artifact13073823619（15,597B）をRAM検証し、23 JSON/patch135,968BをSource/hash/path照合して
  取り込んだ。実capture16ケースと旧suiteのclean-source回帰も上記CIで成功した。

## 022: catalog前半のIO所有とJSON parseを二重frameworkにしない

- 状態: **原本参照9件は両OS成功、native機構7件成功。実catalog呼出しは表現未対応で失敗**。
- 場所: `bootstrap_catalog.rs`/`original_profile_catalog_io.rs`、既存NativePath/dispatcher、
  `tools/original_catalog_prefix_reference.py`。
- 整理: 同journal guardで元bundle/dev選択→resource bindingの実walk/read/hash→実
  load_catalog→元user-data取得→ProfileDefinitionStore constructor入口までを次unitとする。
  読取資格は同AppHandleの実originを借り、Guest path/digest/env DATAから作らない。
  child専用の元RUMI_ENVIRONMENT/RUMI_APP_DIRは同AppConfig/Kernel条件から借用し、
  native processのambient envへ置き換えない。別occurrence再読取や元close地点を維持する。
- JSON: 新parser/文言推測を増やさず、Hostが当該時点で読んだmanifestを同Storeの固定pure
  parseへ渡す。元UTF8/read_text/newline/json.loadsと実例外/catch/causeを保持する。
  既存decoderの過大なbyte一律予約は、実Vec容量と成長時同居を含むprivate上界へ置換。
  数値は旧単位を保ち、到達したkey比較も同NativeWorkへ課金する。旧journal/Authorityの
  入口・予約量と64MiB/2B/絶対30秒は維持する。Rust7件とGuest6件の新検証Sourceがあり、
  CI41322731594で機構7件、metadata5/preparation5/policy6/実init1/Core owner2は成功。
  実catalog1件はslot7でunsupported-valueを返し未達。実finish/OS lock解放、Source破棄、
  DATA解放/reap後0bytes/0reservations、原本state/key一致は観測したが、catalog成功とはしない。
- 原本: a0971c4b5のCI41282428075は両OS9件成功。verbatim staged Pythonと実root getters/
  catalogを通し、constructor呼出aliasだけsentinelで停止した（本体0）。packaged入力、
  artifact-root不在、CRLF、dev-bundledは到達、manifest/UTF8/JSON/schema/unlisted失敗は
  実typed cause/位置とファイル状態を観測。69,427Bの選択JSONのみ保存した。
- 限界/削除前提: resource fixtureはartifact directoryだけで、実installed executable、
  constructor移行/初期登録、outer Authority、GUI、native実行の成功ではない。
  正常IO Joinと希少Join不確定のcustodyを分け、未完をcompleted flagやmutex取得で完了にしない。
  型/資金/capacity不足は未達を残し、正規移行writeの省略や新cache/grant/allowanceで隠さない。
  pointer/length witnessはAstra highの回答に従い撤去し、同immutable managed AppConfigの
  実fieldを両APIが借り、Work/AppHandleがcleanupまで保持するSource契約を根拠とする。
  非object manifest、非ASCII filename repr、複合IO context、dev resolve競合、深い例外と
  rare Join不確定は未閉鎖であり、既存getter/正常fixtureの資格を流用しない。
  slot7の停止地点を観測するcatalog専用phase/reason診断はAstra high承認の固定tagのみ。
  このexportのdetailsだけを意図的に変え、通常Dispatcher.callと他exportのopaque ABI、
  fatal/MethodRaised分類を維持する。診断は512B以内で権限/成功判定に使わない。
  別のSource欠落として、Native `_ModuleLoader` wrapperが実captured loaderのschema
  resource-owner accessor/identityを維持していない。今回unsupportedの原因とは未断定で、
  単純forwardやPath fallbackで済ませず、実owner/spec.loader/__loader__対応の閉鎖が必要。
  Sol6.1 xhighが実original.exec_moduleを一度だけ実行する修正と、実CapturedFinderを使う
  独立6件を用意した。module-local import viewを先に設置し、本来の__file__/loader/
  成功weakrefを原本ownerに生成させる。shared loaderのguard/APIを緩めず、154件のHarness
  検証にこの独立6件を含めて成功した。
  finder.closeは従来の自分finder除去だけとし、whole-scope/Host retirement拒否と分ける。
  catalog診断は既存outside分類でexact builtin6種の固定tagだけ追加（512B内）。b61ebc7e0の
  CI41330673707で現component構築と既存metadata5/preparation5/policy6/init1/owner2は成功。
  実catalogはoriginal-call/outside-fixed-method-inventory/outside_builtin=unknownで未達。
  実cleanup・state/key・Source破棄・reap0/0は成功したが、修正を今回の停止原因の解消と
  読み替えない。native import側の有限エラーを次に調べ、全closure互換も別に実証する。
  次の診断ではAstra highの回答に従いexact builtin13種と、正常bootstrapで既に取得する
  実codec classだけを固定tagで区別する。subclass/未知型はunknown、例外中のimportや
  名前検索・args/str/repr/traceback・method_errors追加は行わない。Sourceに新2件を加え、
  focused診断は7件。CI driverの既存inspection_attempts前後の2整数もCopyするが、
  成功したIO・owner admission・Joinの証拠にはしない。なおunknownならallowlistを
  増やし続けず、実観測した区間の固定bridge境界を一度だけ細分化する。
  4ef660daeのCI41333994269はHarness156/現component/既存native群が成功し、実catalogは
  exact ModuleNotFoundErrorを返した。inspection前7→後10、Host進行root-symlinkのまま。
  次の固定IO入口前のlazy import経路をSourceで調べる。service/App moduleはcapture済みで、
  transitive secure_persistence→secrets/ctypesとHost-only責務の分離を検討中。ctypesの
  Python部分だけ追加してC拡張や元IO動作まで実装できたとはしない。
  Astra highの回答に従い、catalog期間の実selected serviceにだけ固定2symbolの依存viewを
  実装した。元service/catalog/initはverbatim、whole DataFacadeは未採用のまま。
  SecureDirectoryは状態/Source/handle/callback/成功IOを持たず、使用すれば未接続で停止。
  SecurePersistenceErrorのpass本文は別private classとして翻訳し、元identityやmethod-error/
  cause資格を与えない。成功枝だけでなく失敗枝/例外matching/class使用も0である閉じた
  Source AST群をpinする。元import環境/初期化副作用/実persistenceの再現とはしない。
  最初のowner案は任意responderのduck属性照合で偽装可能と独立Source監査で判明したため、
  未実行のうちに撤去。実entry install返値と同固定Finder/Loader型を、実seal後・初init前に
  既存bootstrapへ一回だけprivate配線する。Frame/WIT/名前/hashからownerを再発見しない。
  同class別instanceと偽属性を拒否し、closeは既存ownfinder除去と追加borrow解放だけを行う。
  13新検証Source（6catalog正常/失敗subcase、実captured owner、偽装/poison、Source drift、
  init/import失敗）を追加し、Root CIで実依存もlock固定する。実component/予算検証は未実行。
  79f4780faのCI41341812508はunittest収集器がtest moduleに直接importされたVIEWへ
  isinstance用の__class__照会を行って停止し、169件は未実行。testだけを通常module alias
  経由へ直し、製品の属性拒否を維持した。__class__拒否も既存test vectorへ追加する。
  修正943a4b102のCI41342226932は169件、frozen16、現component構築と既存native群が成功。
  実catalogは26.33秒でWasm fuel2Bを使い切りouter trapとなった。typed outcomeと
  Catalog成功は未観測で、当該呼出しの最初の不足moduleまで解消したとは認定しない。
  before-finish32,945,579B、実finish後0B/1reservation→reap0/0、Source破棄とOS lock解放は
  観測した。一方cleanup_probeをtyped return後だけ取得するdriverはouter trapで未取得のため、
  cleanup_verifiedはfalse。0/0やlock解放をIO Join receiptへ読み替えない。上限を保って
  実燃料消費箇所とtrap時の既存cleanup ledger保持を次に確認する。
  次のCI専用修正は、outer errorを伝播する前に既存reviewのcleanup probeとcatalog進行を
  取得し、実callが既に採取したfuel/post-return/Store破棄/deadlineをprogram破棄前にCopyする。
  未取得はnullで、finish・probeの実検証・OS lock・reapは別々に記録する。新しいGuest callや
  追加予算は使わず、Copyは既存Workの予約へ含める。既存policy6件のうち1件で、実失敗後の
  Store不可読とCopy保持、journal未開始時probe不在を確認する。Source/rustfmtのみ確認済みで、
  実test/compileとcatalog診断の結果はCI待ち。
  実ba8257d13/CI41346336348ではproducer169件・frozen16・現component構築は成功したが、
  old driver末尾のensureにOption<bool>をboolとして使う取り残しがありE0308でcompile停止。
  このrunではNative catalog/policyは未実行。`verified == Some(true)`へ修正し、未取得を
  成功と扱わない。実Host progressとtrap cleanupの認定は修正後のCIまで保留する。
  原本9ケースのNative比較も追加する。各ケースは新しい独立fixtureと別の元operationを使い、
  そのoperation内ではcompile前から同じ30秒・Store・2B/64MiBを維持する。実factoryの
  kind/argsと実parser例外ref・cause/context同一性・固有fieldを観測する。UTF8のmessageは
  manifest由来かつexact builtinの検証済みfieldだけを固定UnicodeDecodeError.__str__で観測し、
  4096 codepoint超過は拒否、MemoryErrorはfatalを維持する。一般formatterのRust二重実装や
  期待値からの例外再生成はしない。独立comparerが原本graph・filesystem・既存key・実FD lock・
  cleanup・reap0/0・予算を比較する。Sourceのみ確認済みで全9一致は未証明。深い任意例外、
  constructor以降のmigration/Authority、GUI/実モデルの資格へ広げない。
  小さい整理候補として、old1/new9 CI driverに重複するCatalogSourceの実owner借用を、
  environment/app_dirを明示したfixture helperへまとめられる。実source identity/FD借用・
  fixture入力と期待値の分離・operationごとの元予算を維持し、両driverの実比較後に着手する。
  現時点は未対応で、製品controllerや一般例外frameworkの共通化へ広げない。
  Source監査では、typed failure以外にouter anyhowにもRead予約で賄うOriginalAuthorityErrorが
  残り得ると判明。old1/new9の双方で、実型が既知Native診断、またはcleanupが実Copy
  NativeDataIoErrorだけの既存wrapperの場合に限りfinish前に破棄する。未知・Context・Guest
  carrierは元のfinish後まで保持し、診断破棄でも実失敗・未資格を維持する。多重wrapperも
  同じ固定型だけを辿る。文字列再構築や期待値を使う一般例外frameworkは追加しない。
  修正3e1179917のCI41349560999で171件と現component構築、Native compileは通過。
  policyの追加assertは、metadata未許可のroot callがGuest実行前に拒否されることを
  見落としていた。元の拒否とfuel不変・snapshot不在を維持し、同じruntime/accountの許可済み
  READY export内から禁止inspectを行う実Wasm失敗で退役Copyを確認するようtestだけ修正する。
  metadata許可は追加せず、元の6件・init/replay・予算を維持。修正後の実検証は次CI待ち。
  実3eの全9比較は終了し、missing/JSON/UTF8/schema/unlistedの5件が原本の例外graph・
  parser固有field・filesystem・既存key・元予算まで一致した。正常4件はすべて最初の
  pack read後（実loader entries/entry1/walk2/pins0）でfuel0。初回check_schemaかcold42内かは
  まだ区別しない。全9件で実finish/probe・Source破棄・FD解放・owner再open・reap0/0が成功、
  最大peak56,254,129Bで64MiB以内。old1も実probeを取得し、catalog-loadでfuel0/26.15秒、
  deadline未超過・Store破棄・cleanup_verified=trueを観測した。全9一致は5/9のままで未達。
  比較reportのcleanup欄がexecution成功も含めてfalseとなる混同は分離するが、全体matchedは
  引き続きexecution成功も必須にして5成功/4失敗を維持する。32KB ZIPをRAM検証し必要JSON
  68,520Bだけ保存、9,945,120B componentはmetadataのみ。対象はsupported Macの有限fixtureで、
  製品GUI・深い任意例外・constructor以降を検証したものではない。
  将来の整理は未接続HostコードをGuest closureから外して実Rustへ移すことだが、今回の
  依存置換の成功だけでSource削除や後続activation機能の達成を認定しない。

## 024: frozen component checkpointを現在のProfileへ誤照合しない

- 状態: **12fe74c93で修正。原本checkpoint16件と現native component構築がCIで成功**。
- 場所: producerの`component_inputs`、`test_build_components`、
  `original_component_checkpoint.py`。
- 問題: 2026-10-20の固定five-component planは、当時のProfileを含むinventoryに
  結び付く。現在の正規Profile更新後へ同じplanを適用すると、無関係なnative Harnessの
  producerテストまで止まり、古い証拠とcurrent-source検証を混同する。
- 整理: plan/82件の全source hash/過去oracle/観測/生成物は改変せず、既に当該producerが
  通ったc6d700124のgit sourceを固定した一時capsuleで、現producerの同capture処理を実行。
  別processでstager module globalsのcurrent/historical混在を避ける。ローカル確認は
  82件/1,349,035Bのgit blob照合とASTだけで、runtime/componentは実行していない。
- 限界/削除前提: 当時のplan再現だけの検証であり、古いartifactを現在のProfileの証拠に
  しない。現在の全five-component構成へ必要な新plan/build/replayは引き続き未完。
  今回のnative Harnessは別の現source closureから実componentを構築・検証する。

## 023: 保存全履歴とモデルへ渡す文脈の200件上限を分ける

- 状態: **修正c2037a10f、生成物取込02dcbdf25。両OSで新43ケース込み483件成功**。
- 場所: `saved_bridge._saved_read_projection`、protocolの`build_saved_model_messages`、
  Guest saved conversationの`_history`。
- 問題: 全保存履歴へ200件の先行制限を適用すると、user追加後だけ201件になり拒否したり、
  短い選択枝まで別枝の大量履歴で拒否したりする。現Sourceのguard残存は確認したが、
  各Guest経路の実到達・元MessageBuilderの予算規則は別に確認する。
- 実装: 全保存graphの200件guardを除去し、全IDの重複検知と選択枝のモデル入力を分離。
  既存turn ID規則でcurrent/user後/assistant後を各512KiB検査し、fresh owner snapshotの
  CAS直前にも再検査する。予定userはowner証拠や仮revisionを作らず一度だけ組み立て、
  同じ静的生成引数をpreflight/保存直前/AIで3MiB検査する。重複turnは元Guestの
  reconciliationへ進め、readinessやモデル実行を増やさない。
- 維持/削除前提: 198/199/200/201件と別枝大量履歴を実Host/Guest/Storeで確認し、
  次送信・lost ACK・resume・512KiBの各中間形・画像込み3MiB・2048件・revision競合を
  新43ケースで検証した。CI41288040466はmacOS/Linuxとも既存440件込み483件成功、
  frontend59件と型・境界検査も成功。CI41287749591で生成したprotocolのsource hashだけを
  674Bの一致patchとして取り込んだ。旧テスト・Guest ABIは維持し、実モデル/Native Wasm/
  全GUIの成功へ範囲を拡大しない。
- 限界: 全ID投影が512KiBを超える巨大graphは保存前に明示拒否し、owner照会への
  置換は別の未完移行項目。後で確定するassistant本文/tool結果等は到達点の既存制限を
  維持する。全容量失敗の保存前拒否、履歴削除、上限引上げ、新budgetを実装したとはしない。

## 025: registryのdeepcopyを重複したnative保持へ移さない

- 状態: **4e5fb0bb2で容量修正、実Native13件すべて一致。実Wasm/移行本体は未完**。
- 場所: `original_profile_definitions{,_model,_tests}.rs`、既存publication IO/JSON、
  `tools/original_profile_definition_prefix_reference.py`。
- 整理: 元snapshotの厳密JSON/digest/全state・履歴・ID検証とnormal postyield検証を保ち、
  definition lockを実closeした後にlist変換/sort、空の場合だけlegacy fileの実statを行う。
  aliasのないfunded owned arenaからの固定native viewにより、Pythonの物理deepcopyと
  detached dict再作成を重複実装しない。viewは外へ返さず、後段currentnessにも使わない。
- lock: 既存private helperをpublication/definitionの固定二filenameへ限定して再用し、
  元のfullfilename process key・journal→definition順を維持する。各migrationの再lock/readを
  省略しない。同一filenameの一般RLock再入、atomic replace、migration本体は未実装。
- 原本検証: 実ctor/listを13件の制御入力で動かし、最初の2migrationの入口だけを
  sentinelで止める。実definition lock解放とjournal保持、state/key・closeを別に観測する。
  CI41323891367は両OS13件成功。正常4枝/負例9枝と実lock lifetimeを観測し、135,961Bの
  選択JSONのみ保存した。migration本体0であり、製品採用の証拠ではない。
  別のSol6.1 xhighが実装した同13件のNative direct比較はCI41329148126で実行した。
  両OSのnormal機構6件とstrict Clippyは成功。macOSの実Native13件は7一致、実packaged
  Profileを含む6件はAuditのnative-capacityで失敗し、独立比較も失敗した。既存decoderの
  (入力長+1024)×旧単位という一律予約が、約190KBのJSONに87MB以上を要求していた。
  修正では64MiB/2B/絶対30秒を保ち、registry専用の実allocation上界を実装した。
  全raw bytesの計数で未完/invalidの途中Text・frame・各Vec成長と同居を含め、後段の
  bootstrap/tombstone/fallback object・digest文字列とduplicate error scratchも予約する。
  旧decode/decode_strict/catalog予約・厳密解析順は変更しない。新focused4件と既存2件の
  model実容量assert強化はCI41332712410で両OS成功。旧normal6件/原本13件の期待値は保つ。
  strict Clippyも両OS成功。macOSの実Native13件は全一致し、独立比較も13/13成功。
  最大peak48,532,334Bで64MiB内、実close/reap0/0/physical owner再openも全件成功した。
  277,315Bの選択JSONを検証済みRAM ZIPから保存し、archiveは保存しない。
  13件全てで既存1account/1Store、journal保持/definition解放、元state/key、実close、
  DATA解放/reap0/0、physical owner再openは確認した。失敗6件を成功へ数えない。
  実AppHandle/Wasm全catalog/Guest factory/非同期Joinの資格はこの同期mechanics driverへ含めない。
- 未完: outer catch対象のSecurePersistence/ActiveProfileStore/Timeoutの有限class実bindingは
  必須残件。実lock timeoutはActiveProfileStoreLockTimeoutでありbuiltin TimeoutErrorへ
  置換しない。最新Astra high回答に従い、native IO原因は閉じたApp-owned class graphで
  Hostに保持し、同Storeの実denied factory返値のintrinsicsとHost explicit-fromを分ける。
  Guestが原因objectを処理しない区間でclassを再生成するexport/frameworkは増やさない。
  raw Guest例外は実refを保つ。既存typed-request投影を成功へ読み替えず、未知型は未資格、
  registry別RuntimeError familyは元どおりraw。実factory失敗からのfallbackも行わない。
  独立参照tool `original_recovery_error_reference.py` は実原本class10入力をcatalog入口で
  制御注入し、元recover/catch・実factory・journal finallyを変更せず観測する。
  CI41333706776は両OS10件成功。args/MRO/OS fields/同nodeのcause-context/suppressionと
  raw registry2枝を確認した。後続のNative比較用chain入力は任意文言でなく実os.strerrorを使い、
  初回257a45d40の証拠は保持する。Native projection/同policy rebindはSol6.1 xhighが実装し、
  CI41335501380は4ce888ec7の両OSで原本10/portable6/既存graph7＋新4/strict Clippyが成功。
  独立8ケースの実component driverと独立comparerのSourceも追加した。操作入力だけを使い、
  実factory6回とraw registry2枝、同Store/元予算/Host edges/元intrinsics/解放0/0を比較する。
  timeout2入力はnative caller provenanceの対象外として明示除外する。aa017d720の
  CI41336707940は既存factory8、新controlled8、独立比較、既存native群を全て成功した。
  実Store内の6fresh refs、Host cause/context同一、元intrinsics、追加inspection0、
  fuel1,441,118,112→1,400,550,752、元30秒、実finish/DATA解放/reap0/0/owner再openを確認。
  検証済みRAM ZIPから選択JSON72,864Bを保存し、component9,910,225Bはmetadataのみ。
  同aaの両OS再検証CI41336708015も原本10/portable6/graph11/strict Clippyが成功した。
  direct Native13をwhole Storeへ含める際は、特に二重Profile負例のDoc/index/canonicalと
  Guest保持分の同居peakが64MiB内か未資格。必要なら単一duplicate keyのrepr予約を
  全文字列合計から最大1文字列へ絞る余地があるが、現成功資格だけで最適化しない。
  同じJSON文字列/整数の出力処理がcatalog/recoveryに分かれる点は将来の整理候補だが、
  App mappingとprivate policyをCoreへ広げず、同資金・エラー順の一致を示してから共通化する。
  native IO原因・Guest class実生成・実factory failure・GUIの証拠へ範囲を拡大しない。


### 2026-11-03: カタログの計測と後から減らせる重複

- CI `41353160738`（`8be3110a3`）では、権限境界6件と既存のnative検証が成功した。
  実componentのカタログ9入力は負例5件が原本と一致し、正常4件は元の燃料上限で停止した。
  全9件の実cleanup・独立lock解放は成功したが、正常系の機能同等性は未達のまま。
- 次の計測は元の非計測componentと別の明示的なCI生成物に限定する。
  `check_schema` の実開始・正常終了とschema store入口を固定タグで記録し、
  元のvalidator・例外・cache・順序・2B fuel・64MiBを維持する。
  計測を通ったことを製品採用やカタログ正常系の成功へ数えない。
- コード削減候補: 旧1入力driverと9入力driverのSource/cleanup観測に重複がある。
  実失敗と実closeを保持した同等性を確認してから共通化する。先に共通化して
  別々の観測経路を同じ期待値で自己検証する形にはしない。
- 計測DATAの報告は固定16KiBのASCII sidecar一つとhash/サイズのmetadataへ限定する。
  retire後に解除された予算へ再課金せず、実行中に同じaccountから事前確保した
  owned packetで報告終了までDATA寿命を保持する。完了後のreap 0/0も別に検証する。
  計測機構は機能移植後に除去できるCI専用コードで、製品Python廃止の代用にはしない。


### 2026-11-03: 未採用App schema backendの固定名保持

- source: `tools/python313-producer/schema_kernel/src/app_schema/cursor.rs` と
  `src/app_schema.rs`。exact builtin Python文字列の既知36 keywordに対し、
  Rust名の保持・frozen rowの取り出し・Frameへの引き継ぎで同じ固定bytesの
  `String`確保/cloneを繰り返していた。
- source修正: `Cow<'static, str>`を用い、既存のexact builtin判定と同じ36名の
  matchを通過したものだけRustのstatic literalを保持する。実Python keywordと
  valueは元のobjectのまま保持する。foreign keyは元のlive `select_keyword`を
  同じ順序で呼び、同じ`String`抽出を`Cow::Owned`へ包む。未知名やsubclassを
  static経路へ追加せず、結果cache・予算・provider選択・製品defaultを変えない。
- status: Astra highのQ&A確認とSol6.1 mediumのread-only source review済み。
  `5fc0b9a0a`のCI41536252846で実native corpusと元caller比較は成功した。
  実Wasm CI41536252795は凍結producerの参照commit未取得によりSource検査で停止し、
  Rust/Wasm実行は0件。workflowに原本commitの明示取得を追加し、検査を省略せず再検証する。
  allocation失敗や
  fuel停止位置は変わり得るため、速度向上・cold42・全catalog成功は未証明。
  wholeHarnessのgenuine validator計測とは別の未採用backend改善であり、
  採用やPython廃止の根拠にはしない。


- 2026-11-11 作業単位03: 新たな固定属性名intern化候補（`app_schema/references.rs`
  の22呼出し/12種類）と、その`app_schema_reference_checks.py`の追加検査は、両方とも
  作業開始時HEAD `2c0bab21a` の内容へ戻した。既存の過去に検証されたintern化まで
  撤去したものではない。今回の候補は実compile/native/Wasm検証を一度も通していない。
  PyO3 0.30.1の`intern!`はinterpreterキーを持たないstatic `PyOnceLock<Py<PyString>>`
  を使うため、「interpreter lifetime」と同一視しない。実Wasm構成でStoreごとに当該
  module/staticが独立生成される証拠を今回確立できなかった。再開条件は実構成での所有・
  破棄の証明、またはStore/interpreterが所有する有界metadataへの置換とcold費用の計上。
  実descriptor lookup/call順・名前object identity・例外参照を維持し、元native比較と
  非計測cold42/full gateを再検証する。Cは残課題で、性能効果や採用を主張しない。


### 2026-11-03: 実cold schema入口の確認と残る燃料上限

- `0e59ab5c5`の計測専用CI41535218618はproducer185件とCore固定import6件に成功。
  Host4件は3成功・1失敗で、失敗は検証fixtureのinline core instanceが
  `(memory $memory)`を`(core memory $memory)`と記していた構文エラーだった。
  assertion/予算を保って1行だけ修正し、4件の実行成功は次のCIで確認する。
- 実whole componentの計測データは12checkpointを保存した。alias `pack`の実検査は
  正常終了し、cold store本体へ入り、`activation_record_v1`、`activation_record_v2`、
  `base_definition_v4`、`cli_io_v1`の正常終了と`common_v1`開始を記録した後、
  元の2B fuelを使い切った。純checker費用や非計測版の厳密な停止位置は証明しない。
  全体25.92秒、失敗時33,104,603B/18予約、finish後は報告packet分19,472B/2予約、
  報告・DATA解放後0B/1予約、reap後0/0。実cleanup/Source destructor/lock解放も成功。
- 固定名保持を簡素化した別backendの非計測実Wasm CI41536729155は、Source/原本比較と
  実component生成まで進んだが、cold42とfull catalogの両方がfuel0で失敗した。
  文字列確保削減で元予算内に収まったとは主張しない。genuine wholeとは別の実測である。
- 次の優先順は、既存の有限累積診断でApp-owned Rust処理の消費区間を選び、
  cold42/fullの非計測gateを維持して改善する。その後、実selected owner/providerへ
  正規bindingし、初期化・IOを含むwhole同一Store累積予算で検証する。
  Astra high回答に従い、genuine側へ追加計測を増やすことや、予算増加・cache先入れ・
  検査省略で代替しない。旧Python/VM削除とGUI採用は未資格のまま。


### 2026-11-11: 作業単位03・計測診断の修正（未検証）

- source: `bootstrap_catalog_actual_ci.rs` / `bootstrap_runtime.rs` /
  `bootstrap_schema_diagnostic_ci.rs`、producerの`harness_native/schema_checkpoint.py` /
  `schema_diagnostic_stage.py` / `harness_native_ci_inputs.py`と対応検査・workflow。
- 実行前エラーを捨てる`drop(outcome)`を除き、実エラーのcontextを残して診断は常に失敗
  させる。事前parse失敗もunavailableへ記録し、最初の記録前のreject/前払い失敗は
  record DATAを出さないscalar Statusから読める。Traceの説明はTrace直上に置く。
- 69タグ×start/end＋store入口の139記録と、1回限りのsequence140超過通知へ揃える。
  load_schema失敗はcacheされないため、再試行による超過でGuestの本来の例外を
  RuntimeErrorへ置換しない。Hostは検証済み超過通知をACKしoverflow/unavailableを残す。
  Guest sequenceは140で飽和し、以降の観測を増やさない。記録・snapshot・16KiB報告の
  前払いは元account内のまま。fuel/64MiB/絶対deadline/8 snapshot試行は増やさない。
- 無意味な報告bufferテストを実write_schema_traceの全139行・hash・bytes・予約解放検査へ
  置き換えた。prefix `ci_schema_checkpoint_` はsource上7関数で、workflowのexpected-passed
  とworkflow検査も7へ一致。残るWAT中の128はmemory address、parseのwork128は既存課金で、
  記録数128/129の古い期待値ではない。
- 維持/除去条件: genuine validator・cache失敗・正常終了のみの記録・元の全予算と実cleanupを
  維持。診断はqualification=falseのまま、非計測whole/cold/fullや製品採用の証拠に数えない。
  計測の役目が終わるまで保持し、移植parityを示す前にlegacyを削除しない。
- 状態: AST/YAML/rustfmt edition2024/diff静的確認のみ。Rust compile/test・Guest・pytest・
  CIは未実行。git addは共有gitdirのindex.lock作成がOperation not permittedで拒否され、
  **コミット未作成**。この節と次節のcommit欄は、オーケストレーターの通常commit後に補記する。

## 026: legacyの自己申告承認・実行経路を正規の門へ戻す

- source/変更1: `core_runtime/egress_proxy.py`と`python_file_executor.py`。
  HTTPのowner自己申告とloopbackだけの識別を既存IpcAuthManagerのPack tokenへ変更し、
  正規executorの親でtokenを発行してからforkする。CORSのwildcardを除き、grant manager不在と
  transport失敗はallowed=false。UDSは従来のsocket-bound identityを維持する。
- **残課題: permissive hostのforkは親の認証辞書を含むメモリを継承する**。callbackが保持する
  接続情報を狭めてもメモリ隔離にはならない。HTTP認証修正で敵対Pack間の隔離を認定しない。
  解消条件は親のauthority/secretを継承しない実行所有境界（既定Wasm等）の実証と、既存UDS/
  host policy/承認/監査のparity。既存host経路を未証明の代替で削除しない。
- source/変更2: `blocks/chat/_compact_helpers.py` / `auto_compact.py`。
  任意のapproved/tokenを信用せず、既存server contextまたはoperation・実引数hashに結び付く
  一回限りtokenを検証する。会話revisionをserver側で引数へ結び、既存approval requestへ
  実API replay情報を残す。要約・保護範囲・通常の承認UIを維持し、偽のcoding tool IDを作らない。
- source/変更3: `domain/agent/self_improvement_live_loop.py` / `self_improvement_runtime.py`と
  `blocks/agent/self_improvement_run.py`。
  shell=True/direct GitOps/自作承認flagsを除去し、選択済みtrusted workspaceの正規list/getに
  ID/root/mount revisionを束縛する。毎toolで再検査し、terminal cwdはjail内相対pathへ変換して
  通常coding block・approval・host authorityへ渡す。開始時の承認は将来のモデルtoolへ継承しない。
  approval_pendingを永続化して停止し、既存のexact one-shot followupへ委ねる。API再試行は
  pendingを再利用してモデルやmutationを繰り返さず、multiも最初のpendingで止める。
  state pathは既定/指定の両方をresolveしてworkspace外を拒否する。
- 同sourceで発見した重複/不整合: TOOL_DEFINITIONSのトップレベルparametersは既存schema
  adapterが読まず空schemaになっていた。adapterのinput_schema経路へ渡し、正規workspace IDを
  各schemaへ束縛する。providerへの変換は既存domain/tool/schema_adapterへ任せ、独自変換を増やさない。
- source/変更4: `blocks/sandbox/api.py`のdesktop access grant。
  approvedは必須のexact boolとし、欠落/None/数値/文字列等はmanagerを呼ばず400にする。
  正規ownerのTrue/False判断と下流の所有・承認検査を維持する。
- source/CI: `.github/workflows/issue-1520-security-regressions.yml`。
  手動とsoon宛PRの対象path更新で、Linux/macOSの4直接回帰とtool protocol/coding hardening/
  terminal policyの既存回帰を実行する。contents:readのみ、credentials非保持、3 actionを
  commit SHA固定（git ls-remoteのtag照合のみ実施）。secret/contextの注入はなし。
  依存は既存hash付きruntime/dev exportを既存installerからCI上のみ導入する。
- 維持/整理・除去条件: client/modelの自己承認やterminal承認で既存GUIの門を置換しない。
  独立した新executor/approval frameworkを増やさず、通常の承認・jail・監査を通す。
  実回帰・正常呼出し・one-shot/引数変更/拒否・fork/UDSをCIで確認してから修正成功を認定する。
- 状態: `dde2a5669`、`8bb42c832`、`9d4ae0f82`、`3eb31e69d`、`83313bb9f` を統合済み。
  [CI 42929622892](https://github.com/harupipipipi/tobkiri/actions/runs/42929622892)
  （`cc048deec`）はLinux/macOS各216件成功。現行GUI・親メモリの隔離・Rust移植の証拠ではない。
  未使用importの一括整理やlegacy削除はこの検証に含まない。


## 027: rpds predecessor artifact の期限切れと再構築 component の再固定

- 状態: **再固定済み・未検証（CI未実行）。恒久対策は未着手の必須follow-up**。
- 場所: [build_schema.py](../../../tools/python313-producer/build_schema.py) の
  `COMPONENT_SHA256`（`verify_inputs` で厳密比較）。各 workflow の
  `RPDS_RUN=40182231971` / `RPDS_HEAD=53163ed815c62c4853bbed3d8a082c0764e3084f` は不変。
- 経緯: 固定していた artifact `ui-rpds-inactive-component`（run 40182231971 の
  attempt 1、2026-10-25）が保持期間14日で失効した。同じ run を再実行（attempt 2、
  2026-11-11、同一 head SHA）し成功、新 artifact を得た（失効予定 2026-11-25）。
- 問題: componentize-py の出力は同一入力でも byte 再現しない。
  `rpds.abi3.so`（ace6bfa6…）と composition source（5e6a4bca…）は一致したが、
  inner（b2ed5ebc… → d6c58a8e…）と outer component
  （56d76506… → a118322a…）が変わった。旧 pin のままでは
  `rpds predecessor component differs` で native Harness CI が build 前に失敗する。
- 対応: `COMPONENT_SHA256` を attempt 2 の値へ置換しただけで、検査の緩和・削除はしない。
  `probe.json` のうち `build_schema.py` が読む項目（passed / actual_limited_api /
  observation.post_return / extension_sha256）は attempt 1・2 で同一。差分は
  component_sha256・inner_sha256・observation.fuel_consumed（321255 → 320812）・
  cargo transcript hash のみ。他に attempt 1 の digest を pin する箇所は見つからなかった。
- 注意: この変更より前の測定は attempt 1 の component を使っており、
  attempt 2 の component とは厳密に比較できない（燃料の値も僅かに異なる）。
- 再現不能性の確定: [CI run 42938394185](https://github.com/harupipipipi/tobkiri/actions/runs/42938394185)
  は1 job内で同一入力・同一環境（`PYTHONHASHSEED=0`、加えて `SOURCE_DATE_EPOCH=0` の試行）で
  componentize を4回実行し、出力はすべて異なった（サイズ 18.60〜18.63MB）。差分は core module の
  data section（snapshot）と shim の export 順序。環境変数では抑えられない producer 側の非決定性で、
  pin の再取得を繰り返しても恒久策にならない。
- 保持期間: `8c91a0667` で artifact 保持を14日から90日へ延長した。既存の attempt 2 の artifact には
  効かず、失効予定 2026-11-25 のまま。`python313-producer-cli`（run 39956025119）の失効予定は
  2027-01-21。
- follow-up（必須・実装中）: 非公開のアーカイブ連鎖を `claude/issue-1520-artifact-archive` で実装中。
  **完了ではない**（未マージ・未検証）。再現可能な componentize-py build、または耐久的な置き場が
  できるまで、digest pin は artifact の失効ごとに再固定が必要になる。
- 検証: 非再現性は CI run 42938394185 で確認済み。再固定した pin 自体のCI通過は未確認。ローカルでは
  cargo / pytest / build を実行していない。

## 028: `Core::remove_pack` の pack_limits 残存修正と R2.10 の未解決点

- 状態: **修正済み（CI検証済み）**。R2.10 の2点は**未解決**。
- 場所: [core.rs](../../../crates/tobkiri-core/src/core.rs) `Core::remove_pack`、
  `acceptance_update_removal` テスト、store 層の `orphan_pack`。
- 修正: 削除対象 Pack 自身の `pack_limits` 項目が候補構成に残り、`permission-widening` で必ず失敗して
  いた（limits を持つ Pack は一切削除できなかった）。候補から当該項目を落とすようにした。
  維持する挙動: 他 Pack の必須ポートが満たされなくなる削除は失敗し、head とデータは変わらない
  （R2.10 前半）。権限の拡大検査は緩めていない。
- commit: `ab4fd131f`（共有ブランチ `ab4fd131f..ba0027367`）。
- 検証範囲: CI run 42941462516（`issue-1520-acceptance-13-16.yml`、`605397f71`）で
  `acceptance_update_removal` の5件と関連 gate/profile/store テストが `ok`。実 CLI・実 component での
  削除は未実施。
- 未解決1（R2.10 後半）: Core の削除は、削除した Pack の store を「持ち主のいないデータ」として
  印付けしない。`orphan_pack` は store 層にしかなく Core から呼ばれていない。表示・消去・
  同じ形の Pack による引き継ぎ（R7.3）は未接続。
- 未解決2（質問）: Profile の上限が参照する形の最後の提供者を削除すると、必須ポートが壊れなくても
  `policy-view` で失敗する。R2.10 は必須ポートが壊れる場合のみ失敗と読めるため過剰な拒否の
  可能性がある。仕様判断（意図した保守的拒否か、緩和すべきか）が必要。
  判断までは挙動を変えない。
- 削除条件: 上記2点の決定と実装・テストが揃うまで、受け入れ項目13の周辺（R2.10 全体）を閉じたと
  扱わない。

## 029: 実Flow差し替えと独立したチャット検証

- 既存の別PackチャットとUIポートのテストは、全体CIの古い入力fixtureが失敗すると証拠を
  取りにくかった。`5a8f47747` でmacOSの実所有経路を使う独立CIを追加した。
- `crates/tobkiri-e2e/tests/chat.rs` は同じ物理入力を2回使い、間にprivate fixtureの
  Flowだけを変更する。実Packを再検証し、変更後のhashを通常の門でレビューして、
  元のProfile headに対して保存する。異なるplan digestと実出力、会話の継続を確認する。
- [CI 43375008696](https://github.com/harupipipipi/tobkiri/actions/runs/43375008696)
  成功。Flow編集を含むチャット4件、Wasm ProviderとローカルAPI1件、CoreのUIポート2件が通過。
  ローカルはrustfmt/YAML/差分確認のみ。ローカルAPIは既存テストのSSE fixtureで、
  llama.cpp 1B、DMGのGUI、すべての入力経路の移行には別の証拠が必要。
- `fd78392dd` は受け入れ13–16の厳格lintを型aliasで直し、6–8/10のCIを統合ブランチへ
  接続した。テスト条件・件数は変更していない。

## 030: App schemaの検証フレームごとのBox確保

- `a623125ae` は `schema_kernel/src/app_schema.rs` の検証継続を `Vec<Frame>` に置き、
  参照先を解決した直後に既存と同じ子を積む。実lookup、親へ返すエラー、子の破棄と
  親の再開順を維持する。ルートと拡張時の確保失敗は通常の確保エラーなら
  `MemoryError` として扱う。実Guestのメモリ制限は維持する。
- 拡張で旧領域と新領域が一時的に重なり、入れ子の検証中も外側の領域は残る。
  割当回数を減らす変更だが、燃料・ピークメモリ・Wasmスタックの改善は未確認。
  追加差分テストは11段の参照と、兄弟枝のコールバックおよびエラーの順序を比較する。
- 変更前の[明示App backend CI 43374609255](https://github.com/harupipipipi/tobkiri/actions/runs/43374609255)
  は未計測のcold42と全カタログがどちらも元の2B燃料を使い切った。
  変更後の[ネイティブ差分CI 43376752724](https://github.com/harupipipipi/tobkiri/actions/runs/43376752724)は
  追加の継続テストを含め成功。[明示App backend CI 43376758941](https://github.com/harupipipipi/tobkiri/actions/runs/43376758941)は
  cold42と全カタログが変更後も2B燃料を使い切った。予算内完走の改善は確認できず、採用しない。
  次の候補を決めるには、負荷の大きいschemaを構築・参照解決・後処理に分けて共有の費用を
  観測する。計測用の別成果物に限り、元のStoreで燃料を再供給せず、診断を合格扱いしない。
  両方の実Wasmゲートと、同一Store上の初期化・IO・後始末を含む実経路が通るまで採用しない。

## 031: Pi再生用の生成WITをクリーンなCIでも揃える

- [初回CI 43375443695](https://github.com/harupipipipi/tobkiri/actions/runs/43375443695)
  はGit管理外の `wit/deps` がなく、Piラッパーのcomponent化前に停止した。
  `9bfa525bc` は管理下のCore・Pi adapter WITをCIで配置する。固定した上流拡張の
  ソースとhash検証は維持。共通ラッパーの依存準備をまとめる場合も、呼び出し側のWITを
  意図せず置換しないこと。[再実行CI 43376752668](https://github.com/harupipipipi/tobkiri/actions/runs/43376752668)
  では両方のWasmがビルドされ、未改変helloは通過。protected-pathsは最初の警告を返す経路で
  `provider reported unavailable`。テストHostには予算付き通知の受け口がなかったため、
  `afa85d72b` でテストだけに許可済み `notification` の受け口を追加し、実警告の文面と件数も確認する。
  [CI 43377714365](https://github.com/harupipipipi/tobkiri/actions/runs/43377714365)で2件成功。
  helloの実ツール結果、protected-pathsの5判定と3通知を確認した。

## 032: PR #1521の監査再送とLauncherの依存ロック

- PR先端 `8be3110a` の[PR CI](https://github.com/harupipipipi/tobkiri/actions/runs/41353172887/job/124987819046)と
  [push CI](https://github.com/harupipipipi/tobkiri/actions/runs/41353165780/job/124987778099)の
  macOS両ジョブで、指摘対象の実component監査エラー2件が復旧後の `Session.close()` まで成功。
  各ジョブの `provider_proxy` は33件成功。統合ブランチの `82e403756` で対象テストと
  `handle_metadata.rs` のファイル内容は同一だが、そのコミットでの再実行は未確認。
- 独立したLauncherのCargo.lockはPR先端・統合祖先 `7cdb66411`・`82e403756` で同一。
  [PRのInstaller CI](https://github.com/harupipipipi/tobkiri/actions/runs/41353172907/job/124987546127)と
  [統合祖先のInstaller CI](https://github.com/harupipipipi/tobkiri/actions/runs/41279048043/job/124767015757)で
  `--locked` 検証とDMGのマウント検証が成功。いずれも製品経路全体の置換を証明するものではない。

## 033: CLI公開に実行結果と承認の証拠を足す

- `ea75c1d99` はCIの隔離PTYと一時データrootで通常のCLIを起動する。固定Packの
  Profile・hash・権限の種類を端末上で確認してから応答し、作成・実Echo・公開・
  取り込み拒否とhead非公開・別IDへの再承認・実Echo・再公開を追う。両Profileの
  private SQLiteに保存した会話を検査する。GUIの承認や実キーチェーンはこのテストの範囲外。
  [初回CI 43377553320](https://github.com/harupipipipi/tobkiri/actions/runs/43377553320)は
  CLIのProfile作成と6件の承認を通過したが、改行のない承認表示に続くIDをテストが正しく読めなかった。
  `942d5f99d` で読み取りを修正。[CI 43378931498](https://github.com/harupipipipi/tobkiri/actions/runs/43378931498)は
  作成から再公開まで成功し、両Profileの実会話とprivate SQLiteの分離を確認した。

## 034: Profile取り込みで上限だけを変更したときのPack上限

- `Core::import_profile` は公開時の個別Pack上限を維持したままProfile上限を狭めていたため、
  利用者が許可を減らしただけでも、個別上限が新しい上限を超えコンパイルで拒否された。
- 修正では元の公開内容とPack pinを先に検証し、個別上限を新上限との共通部分へ狭める。
  取り込み先は新しいIDを使い、通常の保存・承認経路でもう一度検証する。元から不正な
  宣言や必須ポートの欠落は拒否する。重複するファイルルートや秘密名で解釈が曖昧な上限は、
  上限を変更する取り込みで拒否する。User宣言の保存形式は維持し、実効権限は従来どおり上限と交差する。
- 検証: 個別上限を狭めた取り込みと新規承認、上限拡大・曖昧な上限・不正な公開元の拒否を
  [CI 43378298205](https://github.com/harupipipipi/tobkiri/actions/runs/43378298205)の実Coreテストで確認。

## 035: 固定した1Bモデルの現行ソース再検証

- `8ecb7185d` で既存の実モデルテストをmacOS CIへ接続した。llama.cpp b11146と
  Gemma 3 1Bの配布物はrunner上でSHA-256を照合する。loopbackの認証とモデルaliasを確認し、
  一時Keychainを用いて実CoreからWasm Providerを呼ぶ。テストは会話の永続化、
  キャンセル後のサーバー停止、秘密を露出しない監査、Sessionの終了を確認する。
- [実モデルCI 43378931644](https://github.com/harupipipipi/tobkiri/actions/runs/43378931644)と
  [現行ソースのDMG CI 43379312352](https://github.com/harupipipipi/tobkiri/actions/runs/43379312352)
  を開始した。実モデル初回は認証とモデルaliasを確認し、Harnessが生成へ到達した後、
  30秒以内にストリーム進捗を受信できず終了した。`9b1e4aa58` は同じ認証付きAPIの直接SSE応答時間を
  先に計測し、サーバー待機に戻ってから既存のHarnessテストを実行する。
  [再実行CI 43380059173](https://github.com/harupipipipi/tobkiri/actions/runs/43380059173)
- `869ececce` の[実モデルCI 43382542071](https://github.com/harupipipipi/tobkiri/actions/runs/43382542071)
  は成功。macOS runnerの認証付きloopback API、Gemma 3 1B、実Wasm Flowで37件の
  テキスト差分、会話保存と次のターンへの読み戻し、生成のキャンセル、Secretの
  dispatch 4件と完了4件、Session終了を確認した。承認と入力にはCIのHostを使った。
  製品GUIのSetup、承認、モデル設定から会話までの実機検証が残る。
- [macOS DMG CI 43379312352](https://github.com/harupipipipi/tobkiri/actions/runs/43379312352)
  はLauncherのcold boot、DMGの生成とマウント、現行Python資源の照合を通過。
  PackVM helperとPythonを同梱する既存配布構成での結果。
- [macOS DMG CI 43388499835](https://github.com/harupipipipi/tobkiri/actions/runs/43388499835)
  は `584aed439` でLauncher本体661件、build script 123件、資源18件、
  cold boot、DMG生成とマウントを通過。CI専用成果物14023372359は282,135,307バイト。
  マウント後もPythonを照合する既存パッケージの検証で、移行後のGUI全体は未検証。

## 036: 同時に発生したpublicストアの初回承認

- `tobkiri-gate/src/engine.rs` は書き込み承認の有無をSQLiteへ問い合わせ、UIの応答を
  待ってから承認とdispatchを記録する。同じProfile・writer・owner・storeへの最初の
  二つの要求が記録前に到着すると、両方が承認を求める可能性がある。順次のStore試験は
  実データを書き込んで組ごとの記録を検証するが、同時の初回要求は扱わない。
- Sessionごとに別のGateが作られる。対処時は、同じ物理Profileを開くSession間で
  調停し、待機中の取り消しやepoch変更を扱う必要がある。先行要求の承認記録後も、
  後続要求は自身の引数・権限・監査を通常どおり検証する。既存ワークスペースの
  移行も引き続き確認する。
- `fdbfa5bd2` で同じデータrootの二つのProfileに検証済みWasm Packを配置する試験を追加した。
  所有Packのprivateとread-onlyへの実書き込み、外部Packのアクセス拒否、
  二つの書き手と二つのpublicストアへの順次承認、Profile間の値と承認の分離を確認する。
  同じ組での再書き込みは新たな承認を求めず、各Profileの実セッションが終了した。
  [CI 43381937895](https://github.com/harupipipipi/tobkiri/actions/runs/43381937895)で
  このWasm試験、従来のStore・Core試験、strict Clippyが成功。
- `eb986e424` ではProfileの承認DBが、初回要求の既存のpending予約にwriter・owner・storeの
  組を記録する。短いトランザクションで選んだ先行要求だけが承認を求める。
  待機中の要求は元の期限と取消を使い、自分の予約と監査でdispatchする。
  先行要求が承認を残さずに終わった場合、待機中の要求は承認必須で終わる。
  [同時実行のCI 43387276899](https://github.com/harupipipipi/tobkiri/actions/runs/43387276899)で
  別のDB接続、拒否・取消・epoch変更、旧DBからの移行を含むStore試験9件が成功。
- 専用ウィンドウは承認シート単位に応答を受け取る。CLIの`TerminalHost`は同じ端末を
  共有するため、期限切れの要求に対する入力が次の要求に残り得る。期限切れの承認を
  通さないGate検証とは別に、`86472deaa` でCLIの承認を表示中の予約IDに結び付けた。
  [実CLIのCI 43411791141](https://github.com/harupipipipi/tobkiri/actions/runs/43411791141)は
  実際の旧Pack承認の回答を後続のUser承認に送っても記録されず、表示中の予約への回答でのみ
  進むことを確認した。専用ウィンドウと通常の起動操作に変更はない。
  実public書き込み時の端末期限切れと既存Profileワークスペースの三層移行は別に確認する。

## 037: SSE受信済みの出力を次のHTTPチャンクまで保持する処理

- `packs/local.openai-compat-provider/src/lib.rs` の `pull(max)` は、完全なSSEフレームを
  処理して出力ができた後も、上限数に達するか `[DONE]` を受けるまで通信を待っていた。
  実Flowは一度に最大32件を要求し、受け取った各出力をそのまま発行する。
- `3582d3b4f` では受信済みフレームを使い切ってから、空でないバッチを返す。次の `pull` が残りの通信を
  読む。バッチ上限、EOF、エラー、キャンセル、Gateを通る通信は既存契約に従う。
  最初のSSE出力を送ったあと、実Flowが受け取るまで後続チャンクを保留する回帰試験を
  追加した。本文差分と使用量が同時に来て上限に達する場合は、使用量を次のバッチに繰り越す。
  [回帰CI 43381617628](https://github.com/harupipipipi/tobkiri/actions/runs/43381617628)は
  chat 4件・network 1件・UI 2件とCLIを通過した。
- [CI 43380059173](https://github.com/harupipipipi/tobkiri/actions/runs/43380059173)は、
  同じ認証付きサーバーへの直接SSE要求で初回文字列4.05秒、8トークンの終了12.96秒を観測した。
  続くHarnessの要求は生成ステップで30秒間出力がなく、テストが実行を取り消した。
  直接計測とHarnessは別のプロンプトを使うため、応答完了時間の比較には使わない。
- 修正後の回帰CIでは、後続チャンクの送信前に実Flowが先頭を受信した。
  [実モデルCI 43381617774](https://github.com/harupipipipi/tobkiri/actions/runs/43381617774)
  は30秒間に17件のストリームイベントを観測し、全応答の完了前にテストが取り消した。
  `869ececce` ではCIの観測期限を最初の文字列30秒、応答全体240秒に分けた。
  Coreの実行上限と、後続ターンでのキャンセル・保存・監査の確認は従来どおり。
  [再検証CI 43382542071](https://github.com/harupipipipi/tobkiri/actions/runs/43382542071)
  が成功した。実行範囲と未検証のGUI経路は035に記載。

## 038: App schemaの処理段階ごとの燃料計測

- `0e0327941` でCI専用のWasm成果物を追加した。既存のcold schema-storeを一度呼び、
  実際のschema名ごとにセットアップ、初期参照、評価開始、戻りの残燃料をホストが記録する。
  一つのStoreで元の2B燃料、64MiB、30秒を使う。最大168件で、燃料切れと物理リソースの
  解放を個別に記録する。計測費用も含む値として、次に移植する処理の判断に使う。
- [初回CI 43382206716](https://github.com/harupipipipi/tobkiri/actions/runs/43382206716)は
  同じソースへの二重計測挿入を拒否する試験で止まった。
  `ad87c6916` で二重挿入を拒否し、関連2ワークフローが浅いcheckoutから参照する
  固定の原本コミットも明示的に取得するようにした。
  [計測再実行CI 43382433916](https://github.com/harupipipipi/tobkiri/actions/runs/43382433916)は成功した。
- 一度の実行で164件の計測点と41件の戻りを記録した。最後の `surface_template_v1` の
  戻り時点では燃料が10,256,986残り、その後に燃料切れで停止した。期限内にStoreを
  終了し、IOの後始末と確保量・予約件数が0に戻ることを確認した。ピークは25,205,940バイト。
- 計測点間の合計は、前回の戻りから次のセットアップまで133,020,527、
  セットアップから初期参照まで505,538、初期参照から評価開始まで4,126,769、
  評価開始から戻りまで1,831,592,263。最大は評価開始から戻りまでの
  `defaults_setup_v4` 128,084,701、`profile_v5` 104,120,597、
  `pack_manifest_v4` 100,264,930。再帰的な参照と計測費用も評価区間に含まれる。
  次の候補はこの区間で選ぶ。製品採用には計測を含まないcold schema-storeと全カタログの完走が必要。
  [計測JSONの成果物](https://github.com/harupipipipi/tobkiri/actions/runs/43382433916/artifacts/14017416950)
- 同じソースの無計測の[全体比較CI 43382424984](https://github.com/harupipipipi/tobkiri/actions/runs/43382424984)
  はLinuxとmacOSで埋め込み原本の照合まで成功し、cold schema-storeの本体で燃料が尽きた。
  [一般schema比較CI 43382424888](https://github.com/harupipipipi/tobkiri/actions/runs/43382424888)
  もcoldとfullの両方が元の上限で停止した。
- 原本由来の参照メソッドは頻出だが、属性名のPyStringを広く再利用する案は保留。
  Astra highは、独自getterが渡された名前の同一性と保持を観測でき、名前の事前生成で
  割り当て失敗の順序も変わり得ると指摘した。次は既存の実イテレーターを保ち、
  組み込みの2要素tuple/listの項目取得で汎用Python dispatchを減らせるか確認する。
- `adad87779` でexactな2要素tuple/listだけ型付きの読み出しを使った。不正な長さや
  サブクラスは元のunpackへ渡す。イテレーター本体と保持参照は従来どおり。
  [native比較 43385081054](https://github.com/harupipipipi/tobkiri/actions/runs/43385081054)
  は原本64件と19組の制御比較を通過。新しいtuple/listの8ケースも含む。
- [同じ計測CI 43385264152](https://github.com/harupipipipi/tobkiri/actions/runs/43385264152)
  では41件目の戻りで104,668,660 fuelが残り、前回より94,411,674多かった。同じ41件の
  評価区間は1,737,226,028 fuelで、94,366,235減った。42件目の`ui_view_v1`は評価開始を
  記録してから停止。計測なしの[CI 43385275279](https://github.com/harupipipipi/tobkiri/actions/runs/43385275279)
  ではcoldとfullの両方が燃料切れ。製品への採用判定は未達。
- `d35334cc9` は型を一度読み、組み込み型の照合で再利用する。組み込み以外では
  元のチェッカーを呼び、独自の `__class__` を観測する。追加ケースを含む
  [native比較 43388487147](https://github.com/harupipipipi/tobkiri/actions/runs/43388487147)
  が成功した。[同じ計測CI 43388500074](https://github.com/harupipipipi/tobkiri/actions/runs/43388500074)
  では41件目の戻りに109,209,952 fuelが残った。組み込み2要素ペアの版から4,541,292増加し、
  同じ41件の評価区間は1,732,623,258 fuelで4,602,770減った。42件目の
  `ui_view_v1` は92,825,063 fuelで評価に入り、その途中で燃料切れ。確保量のピークは
  25,205,940バイトで、終了・reap後は0。無計測の
  [cold・全体CI 43388500873](https://github.com/harupipipipi/tobkiri/actions/runs/43388500873)
  も両OSで燃料切れ。元の予算での完走条件は継続する。
- `2c868fdf1` は既存のResource/Anchorの実参照を同じ順序で保持し、所有判定の
  線形走査だけをソート済みアドレスの検索に置換した。外部取得されたオブジェクトの
  昇格やlookup結果のキャッシュは行わず、descriptorと実Registryの呼び出しを保つ。
  [native比較 43408273432](https://github.com/harupipipipi/tobkiri/actions/runs/43408273432)は成功。
  [計測CI 43408308307](https://github.com/harupipipipi/tobkiri/actions/runs/43408308307)では
  41件目の戻りに109,723,594 fuelが残り、前版より513,642増えた。評価区間の合計は
  1,732,192,164 fuel。42件目は93,337,581で評価開始後に停止し、
  [無計測CI 43408311686](https://github.com/harupipipipi/tobkiri/actions/runs/43408311686)
  でもcold・全体ともに燃料切れ。数値は計測区間の比較で、製品採用は引き続き未達。
- Astra highはCursorの `PyIter_NextItem` 呼び出し前の一時参照を検討した。
  CPythonの組み込みdict項目iteratorも前の要素の解放中にユーザーコードを実行し得る。
  そのコードからiteratorの参照数を観測できるため、今回の変更から外した。
  [CPython 3.13.16](https://github.com/python/cpython/blob/v3.13.16/Objects/dictobject.c#L5584-L5627)
  · [PyO3 0.30.1](https://github.com/PyO3/pyo3/blob/v0.30.1/src/types/iterator.rs#L99-L119)
- `cfb4344e8` は元の `TYPE_CHECKER` とintern済みPyStringを保持したまま、`object`、
  `array`、`string`、`number` の4種だけRustの固定名を再利用する。Astra highへの確認と
  原本と照らす18番目の値プローブを追加し、
  [native比較 43412870936](https://github.com/harupipipipi/tobkiri/actions/runs/43412870936)が成功した。
  [計測CI 43412913855](https://github.com/harupipipipi/tobkiri/actions/runs/43412913855)では
  41件目の戻りに111,647,175 fuelが残り、前版より1,923,581増えた。評価区間は合計
  1,730,185,784 fuel（2,006,380減）。42件目は95,257,120で評価を始め、その途中で停止した。
  ピーク25,205,940バイト、終了後0。元の上限を使う
  [無計測CI 43412917884](https://github.com/harupipipipi/tobkiri/actions/runs/43412917884)も
  cold・全体の両方が燃料切れ。計測上の短縮は採用証拠に数えない。
- Astra highの回答に沿い、`11af1dce0` はFrameが同じschemaを保持している間、
  `start` が作ったkeyword cursorの有無を使って再開時の組み込みbool判定を減らした。
  `bool` は継承不能で、他のクラスへの置換が起きても同じFrameのschemaがboolには
  変わらない。実際に非boolのクラスをcallbackで切り替え、その後の実エラー・参照・順序を
  原本と比べるケースを追加した。
  [原本比較 43420777040](https://github.com/harupipipipi/tobkiri/actions/runs/43420777040)は成功。
  [計測 43420799485](https://github.com/harupipipipi/tobkiri/actions/runs/43420799485)の41件目は
  110,504,689 fuelで前版より1,142,486減り、同じ評価区間は1,176,306多く消費した。
  42件目は94,118,156 fuelで開始後に停止し、
  [無計測 43420799288](https://github.com/harupipipipi/tobkiri/actions/runs/43420799288)も
  cold・全体の両方が燃料切れ。`3d03c3a76` で速かった経路へ戻し、追加テストは
  残した。[原本比較 43421951611](https://github.com/harupipipipi/tobkiri/actions/runs/43421951611)が
  成功。次は個別の処理の費用をCI専用計測で絞る。
- `1a66c9cdd` で同じcold操作内のsubresource降下、keyword取得、参照lookup、
  scalar検査に、開始・終了の燃料計測を追加した。schema名から固定の開始点を選び、
  各処理につき257回間隔・最大4組を記録する。完了した評価の処理回数と、実処理のない
  計測1組も保存する。燃料切れで片方だけの点は別に記録する。サンプル外の処理、
  計測コスト、未完了の評価を含めた厳密な関数別内訳には使えない。
  [初回CI 43446184694](https://github.com/harupipipipi/tobkiri/actions/runs/43446184694)
  でWasmを構築できたが、Host試験のSHA-256参照がbuild依存のためコンパイルに失敗した。
  `b145a3b98` で既存のcanonical Digestに揃え、元の2B燃料・64MiB・30秒で
  [CI 43447245150](https://github.com/harupipipipi/tobkiri/actions/runs/43447245150)の
  計測プロトコル試験と実Wasmの計測が成功した。41件を完了し、
  42件目 `ui_view_v1` の評価開始時に69,561,817 fuelが残り、その後使い切った。
  サンプル391組、ピーク25,205,956バイト、終了後の確保と予約は0。
  完了分では参照lookupが15,646回で、そのうちサンプルを得られたschemaに14,876回、
  実計測は61組。各schemaの空マーカー（16,217–18,371 fuel）を引いて回数で外挿すると
  約10.2億fuelで、他の3分類より大きかった。次はlookup内のURI、Registry、
  pointer/anchorの区間を比較する。外挿値は計測対象内の推定であり、
  評価全体の厳密な費用でも製品採用の証拠でもない。
  [計測JSON](https://github.com/harupipipipi/tobkiri/actions/runs/43447245150/artifacts/14047749799)
- `85332e5fa` は同じ枠内の計測点を参照解決の内側へ移す。元のURI処理、Registryの
  `get_or_retrieve`、pointer分岐、anchor分岐に重ならない4区間を置き、
  外部resolverの経路と未計測の処理は集計の範囲外とする。
  [CI 43449073611](https://github.com/harupipipipi/tobkiri/actions/runs/43449073611)が成功。
  189組を記録し、41件完了、42件目の評価開始時に84,461,630 fuel、その後0で停止した。
  終了後の確保・予約は0。完了分について、各schemaの空マーカー費を引いて回数で
  外挿した値はRegistry約2.72億（63組、標本のあるschemaが14,913/15,646回をカバー）、
  URI約2.05億（63組、15,166/15,646回）、pointer約1.03億（10組、735/1,967回）、
  anchor約0.54億（5組、337/1,674回）。後者2区間はカバー率が低く順位を確定できない。
  既に戻したRegistry全体のsnapshotを再導入せず、実Registry呼び出しと例外を保ったまま
  無駄な実処理を減らせる箇所を選ぶ。計測なしの同じ予算で完走を別途確認する。
  [参照解決の計測JSON](https://github.com/harupipipipi/tobkiri/actions/runs/43449073611/artifacts/14049338372)
- Astra highへの設計照会に沿い、`e5cf7067a` は参照先のfragmentが組み込みstrのときだけ、
  PythonのUnicode長と先頭コードポイントで空・pointer・anchorを選ぶ。UTF-8変換を
  介さず、元のfragmentオブジェクトをそのまま後続へ渡す。サブクラスのstartswith、
  戻り値の真偽、fragmentの真偽は元の順と短絡を保つ。Registryと取得結果は変更しない。
  原本との比較に孤立surrogate、サブクラスの短絡・真偽・例外を4件追加した。
  [初回比較 43451376680](https://github.com/harupipipipi/tobkiri/actions/runs/43451376680)
  は個々の比較後の総件数が29のまま停止した。`87bc0d3a3` で33件に揃えて
  [原本比較 43451746013](https://github.com/harupipipipi/tobkiri/actions/runs/43451746013)が成功した。
  [燃料計測 43451408267](https://github.com/harupipipipi/tobkiri/actions/runs/43451408267)では
  41件の評価区間が約3,313万fuel減り、42件目の評価開始時は117,733,356 fuel
  （前版より33,271,726増）だった。完了数は41件で、その後燃料切れ。ピークと解放は
  前版と同じ。計測なしの[coldと全体 43451376683](https://github.com/harupipipipi/tobkiri/actions/runs/43451376683)
  も燃料を使い切った。改善は保持するが、既存の予算で完走する条件は未達。
- `f17c9f3c0` はURI結合で小文字のschemeを借用し、baseと参照のpathを順に走査して
  一時配列を一つにする。[初回比較 43453273374](https://github.com/harupipipipi/tobkiri/actions/runs/43453273374)
  で空のbase pathの先頭セグメントを落とす回帰を検出。`5bee80a6d` で保持し、
  追加6組を含む[原本比較 43453892426](https://github.com/harupipipipi/tobkiri/actions/runs/43453892426)
  はURI全151組で成功した。[燃料計測 43453925199](https://github.com/harupipipipi/tobkiri/actions/runs/43453925199)
  の41件の評価区間は1,690,255,119 fuelで、fragment修正後から16,895,148減った。
  42件目の評価開始は134,611,193 fuel（16,877,837増）。192組の採取後に燃料を使い切り、
  41件で停止した。ピーク25,205,956 bytes、終了後の確保量・予約は0。
  同じ実装の[無計測CI 43453892634](https://github.com/harupipipipi/tobkiri/actions/runs/43453892634)
  でもcoldと全カタログが元の燃料上限で停止した。
- `ee935d758` はURIのparamsを最終バッファに直接書き、空のpathにparamsだけがある
  場合の区切り規則を保つ。追加2組を含む153組で
  [原本比較 43459741509](https://github.com/harupipipipi/tobkiri/actions/runs/43459741509)
  が成功した。[実Wasmの燃料計測 43459824953](https://github.com/harupipipipi/tobkiri/actions/runs/43459824953)
  では完了した41件の評価区間が1,676,097,848 fuel（14,157,271減）になった。
  42件目の評価開始時は148,713,921 fuel（14,102,728増）で、その途中で燃料を使い切った。
  192組を採取し、ピーク25,205,956 bytesと終了後の予約0は前版と同じ。
  [無計測CI 43459741448](https://github.com/harupipipipi/tobkiri/actions/runs/43459741448)
  はcoldと全カタログの両方で燃料切れ。既存の別backendを使う
  [private Wasm CI 43459741563](https://github.com/harupipipipi/tobkiri/actions/runs/43459741563)
  も前版と同じ燃料上限で停止した。

## 039: 初回Setupと既存の公開処理の復旧

- 初回Setupでは、元の `bootstrap_review_catalog` がカタログ読み込み後にactive pointer、
  snapshot、登録済みProfileを確認する。
  [原本CI 43384762882](https://github.com/harupipipipi/tobkiri/actions/runs/43384762882)は
  pointer不在、別Profile、未登録、dangling、snapshot欠落・不一致、登録済みを
  source additionsの有無と組み合わせ、Linux・macOSの各14ケースを通過した。
  登録済みは同じカタログを渡してAuthority DBを開く直前まで実行した。
- Rust側で、2回の実復旧の後に選択済みHost rootを確保し、同じStoreの実カタログと
  bootstrap IDを元の順で使う経路を接続した。復旧は既存のジャーナルガードを保持。
  pointer不在・別Profile・未登録時は元のカタログを保持し、追加ソース要求は既存の
  拒否ファクトリへ渡す。登録済みでは検証済みpointerと登録レコードを保持する。
  Authorityと候補解決の本体、実Wasmカタログを経由する全体比較、Launcherへの採用が残る。
  [定義・復旧CI 43387276864](https://github.com/harupipipipi/tobkiri/actions/runs/43387276864)
  はfocused Rust試験3件をLinux/macOSで通過した。
- `41cc226a8` の[初回Setupの比較CI 43389420830](https://github.com/harupipipipi/tobkiri/actions/runs/43389420830)
  は元の外側呼び出しで再生成した14ケースすべてとmacOS上の実Rust Host選択を照合した。
  2回の復旧、選択結果、例外のcause/context、ファイル状態、物理ロックの解放、終了後の
  確保量と予約0を比較。元のカタログは原本側で実行し、Rust側は同じStore上でHost区間を
  直接試験した。実Wasm loader・拒否factoryと結合したSetupの全体は引き続き未検証。
- 公開の復旧側は、定義ストアの旧Profile移行入口が次の境界。
  [原本CI 43384338921](https://github.com/harupipipipi/tobkiri/actions/runs/43384338921)は
  既存13ケースを維持し、`migrate_legacy_successors` の8ケースを両OSで実行した。
  6ケースは整数0、2ケースは元入力または現行リビジョンのフォールバックを選び
  `_legacy_v4_successor` の入口に達した。実変換と書き込みは次の比較対象。
- `1a0d1ff72` は同じ定義ストアを一覧の後で再ロックし、最新のindexを読み直す。
  格納順、元の `legacy_ids[0]`、現在版へのフォールバック、source pathの独立した
  文字列化を保つ。候補は元の状態の参照として保持し、写しの作成を避ける。
  全行をスキップしたときはロックを再検証する。ヘルパー入口や例外では定義ロックを閉じ、
  外側の公開ジャーナルは既存の終了処理が閉じる。実ヘルパーと書き込みは未接続。
  [検証CI 43390782062](https://github.com/harupipipipi/tobkiri/actions/runs/43390782062)で
  同じストアの再読込と引数選択、従来の原本13件・8件を確認した。LinuxとmacOSで
  新規テストとstrict Clippyが成功し、従来の13件はmacOS上の実比較も通過した。
- [原本のResolver CI 43392317777](https://github.com/harupipipipi/tobkiri/actions/runs/43392317777)
  は元のSetupから9ケースをLinux/macOSで実行した。選択後にユーザーデータとBundleを
  再選択し、パッケージ資源の照合と解決用のロック再読込を別々に行う。内容が同じJSONでも
  物理バイトが変わるとAuthorityのsnapshot digestが変わる。DBあり・なし、壊れたDBと
  ディレクトリ、壊れたmanifest、開発Bundleの選択とロック欠落を確認した。
  Authorityの終了後、2回目のID取得から元のProfileを検索する直前までが対象。
- `17406e959` は同じ実StoreでBundle選択とmanifest照合を再実行し、物理ロックを
  改めて読み込んでAuthorityのsnapshotを計算する。原本のAuthorityを閉じてから
  PackのIDをもう一度取得し、取得済みのカタログと実戻り値をレビュー終了まで保持する。
  従来のローダー資源処理を共用し、最初のロックバイトの再利用を避ける。
  [Rust側のCI 43406607339](https://github.com/harupipipipi/tobkiri/actions/runs/43406607339)
  はLinux/macOSで成功。2回目のユーザーデータ選択、DB不在時のsnapshotと書き込みなし、
  ディレクトリの拒否、開発・配布Bundleの新しい物理バイトを確認した。
  実Wasmカタログを経由する全体の比較と
  選択済みProfileのAuthority/Activation、後続のPack解決・確認画面は残る。
- [登録済みSetupの原本CI 43410805436](https://github.com/harupipipipi/tobkiri/actions/runs/43410805436)
  は、DBなし・壊れたepoch・v1移行・ディレクトリ・dangling・キー欠落と追加ソース有無を
  組み合わせた12ケースを両OSで実行した。実Authorityの `__enter__` を通り、元のactivation
  factory入口までを観測した。`c57f289b4` のRustは、確保済みの候補とrootを保持して同じ
  Authorityを開き、workspace/activationの引数をスコープ内で渡す。DB不在時にキーの
  作成が止まる問題は `c6fa90779` で直し、
  [Rust側のCI 43416966566](https://github.com/harupipipipi/tobkiri/actions/runs/43416966566)で
  両OSのfocused試験とstrict Clippyが成功した。実activation factoryへの接続は残る。
  同時DB作成で実際に `O_EXCL` が `EEXIST` を返す場合、元のPOSIX実装の
  `secure_parent` が先に例外を包むため、DBファイルの再試行へは進まない。
  `447d4fbea` でRustも元の拒否と実OSエラーへのcause/contextを保持する。競合後に
  既存DBを開かず、元の順序で止まる。実ファイルを作る回帰ケースを既存macOS試験に追加。
  [原本CI 43421538226](https://github.com/harupipipipi/tobkiri/actions/runs/43421538226)は
  追加ソースあり・なしを含む14ケースが両OSで成功した。
  [Rust側 43421778809](https://github.com/harupipipipi/tobkiri/actions/runs/43421778809)も
  両OSで成功し、macOSで実競合後の例外、ファイル、物理解放を確認した。
- `f883e1715` の[原本CI 43419146914](https://github.com/harupipipipi/tobkiri/actions/runs/43419146914)
  は以前の9ケースに2ケースを足し、実 `catalog.profiles.get(profile_id)` の戻りが同じ辞書
  オブジェクトであることと、2回目の実IDだけが不在の場合の元の拒否をLinux・macOSで確認した。
  `b4263dfaf` は同じStoreのカタログと2回目のgetterを参照し、実lookup結果を保持する。
  既存の例外ファクトリを使い、一般のDATA操作から専用lookupを選べないようにした。
  [実componentのCI 43419680264](https://github.com/harupipipipi/tobkiri/actions/runs/43419680264)は
  Python側194件と実Wasm生成、macOSの実getterと一般DATA経由の専用操作拒否が成功。
  同CIのカタログ読み出し試験は実行した5件がすべて通過したが、件数照合が4件で停止した。
  2回目の物理ロックを追加で確認する実行済み試験を含め、期待件数を5件に揃えた。
  正常カタログ、requested edgesの解決、Launcher採用は未確認。
- `b981dbd34` の[原本CI 43422442527](https://github.com/harupipipipi/tobkiri/actions/runs/43422442527)
  では、実catalogから得た元の310辺について、既存のSetupが作るauthority referenceと
  挿入順をdynamic edgesの直前で観測し、LinuxとmacOSで成功した。
  `3acb96345` は元のカタログ参照を保ったまま、各辺の4フィールドだけを同じStoreから
  読み、実Authority snapshotを使ってRustが参照のハッシュを計算する。重複キーは最初の
  位置を保持し、一般DATA操作からこの読み出しを選べない。元の枠内で一時領域と計算を
  賄い、結果は確認中の候補に保持する。承認・grantは発行せず、動的辺の手前で止まる。
  [原本310件との比較 43425325740](https://github.com/harupipipipi/tobkiri/actions/runs/43425325740)
  は両OSで元のSetupを実行し、Linuxで310件のRust側の結果と一致した。
  [実componentの検証 43425328249](https://github.com/harupipipipi/tobkiri/actions/runs/43425328249)
  も成功し、macOSの実初期化、static参照の順序とbyteを確認した。
  現行312辺を含む正常カタログでの統合実行は未確認。
- `ba57e9694` は初回の同梱候補で静的な参照を作った後、同じStoreの実カタログと
  2回目のProfile IDで元のPackのdynamic edgesを呼ぶ。元の初回候補に従い追加Pack IDは
  空のtuple。Packがカタログを再参照し、実際の戻りが空の組み込みtupleであることを
  確認してから、保持中の参照をそのまま次の段階へ渡す。参照の破棄やlookupの置換で
  呼び出し資格も失効する。権限、承認、WIT、上限の追加は不要。
  [原本CI 43427416962](https://github.com/harupipipipi/tobkiri/actions/runs/43427416962)
  はLinux・macOSで13ケース成功。Packへの実引数・空の戻り・同じ辞書の保持を観測し、
  Linuxでは動的ループ後の310参照をRustで計算した静的参照の順序とハッシュに照合した。
  [同じ実装の実component CI 43427427053](https://github.com/harupipipipi/tobkiri/actions/runs/43427427053)
  はゲストの回帰7件を含む207件、実Wasm構築、macOSの実初期化とHostでの参照照合に成功。
  追加Packを持つ既存Profileと現行同梱カタログの全実行は別途実装と確認が必要。
- `1b45d23c5` で元の集合内包を実カタログのPack一覧に対して実行し、Pythonの組み込み
  setとその参照を同じStoreに保持する。ここで扱う値は、元のPackが次の解決で使う
  artifact digestの集合。権限承認やShell実行ファイルの検証はこの段階に含まれない。
  [原本CI 43432113300](https://github.com/harupipipipi/tobkiri/actions/runs/43432113300)は
  Linux・macOSで14ケースが成功し、解決Port入口へ渡る153件のdigestと引数の同一性を観測した。
  [実component CI 43432128116](https://github.com/harupipipipi/tobkiri/actions/runs/43432128116)
  で新しい3回帰、通常DATA経由の呼び出し拒否、実Wasm構築と初期化も成功した。
- `3280f65df` の[原本CI 43433277485](https://github.com/harupipipipi/tobkiri/actions/runs/43433277485)
  は、元の解決Portに入った後でlockを変更・symlinkへ置換・削除する3ケースを含む17件を
  両OSで実行した。元のPackが実カタログrootの `bundle.lock.json` を `is_symlink`、
  `is_file`、`read_bytes` の順で調べ、新しいバイトのSHA-256か元の拒否を返した。
- `9184b0ff2` で実Portを呼ぶ経路に、同じカタログrootのlockを一度だけ調べる
  Rust IOを接続した。元のsetとProfile IDを保持し、Rustで作った順序付き権限参照と
  Authorityのsnapshot・epochを渡す。先行するlockの内容を使い回さず、確認済みの
  実ファイルを閉じてから値を返す。Shellのartifactへ進む場合は最初の参照で停止する。
  [初回CI 43436559868](https://github.com/harupipipipi/tobkiri/actions/runs/43436559868)
  は、追加したfixtureが元からある異種カタログ試験に属性を設定しようとして停止した。
  `d74cb238f` で実fixtureの型の場合にだけ属性を設定し、元の拒否試験を維持した。
  [再実行CI 43436878493](https://github.com/harupipipipi/tobkiri/actions/runs/43436878493)
  はportable 211件、実Wasm構築、macOSの物理IO試験6件と実初期化・Hostの所有権確認に
  成功した。正常な全カタログを通る実Wasmの一連の実行は未確認。
- `d68e3a8ad` の[原本CI 43438045179](https://github.com/harupipipipi/tobkiri/actions/runs/43438045179)
  は、既存17ケースと、原本の実ファイルとcaptured入力のShell試験7件をLinux/macOSで実行し、
  Linuxでは従来の310参照も照合した。Shell試験は配布時の実行ファイルではなく、
  検証に使う最小のELF、PE、Mach-OヘッダーとInfo.plistを含む。
- `ff35a5ebf` では同じ原本のpackaging generatorでLinuxのAppImageとmacOSの
  `Tobkiri.app` の検証用fixtureを生成し、元のSetupが選んだvariantと同じrootを
  実Packへ渡した。通常の検証、root欠落の生のFileNotFoundError、rootがsymlinkに
  変わったときの元のProtocolErrorとProfileResolutionDeniedを両OSで観測した。
  macOSではファイル名順のツリー読み込み、entrypointの再読込、Info.plist、headerの順と、
  reader終了後にFDが閉じたことを確認した。テストは合成ヘッダーを使い、確認・承認前に
  停止した。[原本CI 43440932967](https://github.com/harupipipipi/tobkiri/actions/runs/43440932967)
- `ea3b39bdc` は選択されたShellをverifier入口でsymlinkに置換する場合と、
  一時的に欠落させる場合を原本に追加した。正常時はrootを確認した後、
  選択leaf→root→ツリー開始時のleafの順でsymlinkを調べる。symlinkは最初のleafで拒否し、
  欠落はツリーのfile/dir検査で拒否する。両OSで実Packの例外チェーンまで確認し、
  Linuxでは既存310参照の順序とdigestも照合した。
  [原本CI 43453556968](https://github.com/harupipipipi/tobkiri/actions/runs/43453556968)
- `6d96428fe` はloaderが返した実Shell rootと親ディレクトリの同一性を保持し、
  Packによるlock再読込後、元のstrict resolve、元rootのsymlink判定、
  解決済みrootのディレクトリ判定をこの順で実行する。現段階の解決は同じ親の
  通常名と相対symlink一段が対象で、外へのリンク・多段リンクは実行境界で拒否する。
  [実Wasm CI 43444308778](https://github.com/harupipipipi/tobkiri/actions/runs/43444308778)
  でportable回帰、実構築と初期化、macOSの物理IO試験7件が成功した。
  ここでは最初のShellファイル検査で停止する。全カタログを経由した統合実行と
  製品のGUI起動は未確認。
- Shellはrootの解決、ツリーのhash、entrypointのhash、macOSのplist、
  entrypoint headerの順。既存の `fd_protocol.rs` の
  予算付きread/fstatは再利用候補。Shellで使うdescriptorのopen/seek/close、元の失敗と
  実パスの所有権を原本と照合してから、許可されたIOをRustへ移す。loaderが返した
  canonical rootを保持し、解決済みのパス文字列だけから権限を作らない。
  `c9cfbc1f9` はloaderが渡したProfileとShell定義のバイトと2回目の検証済み
  Profile IDからvariantを選び、そのleafと親のsymlink検査を接続した。
  rootの同一性はresolverのディレクトリ判定から検査終了まで保持する。
  `ba1b5db18` でmacOSの `O_SEARCH` とLinuxの `O_PATH` を使い、
  ディレクトリを列挙する権限がない場合も原本と同じleaf検査を行う。
  `e538cdbce` で定義の種類を判定するコードを共通化した。
  [初回の比較CI 43458563648](https://github.com/harupipipipi/tobkiri/actions/runs/43458563648)
  でRustの借用エラーが出たため、`da796f51f` で保持中のleaf参照を共有するよう修正した。
  [再検証 43460009226](https://github.com/harupipipipi/tobkiri/actions/runs/43460009226)で
  LinuxとmacOSの原本比較、Linuxの310参照のRust照合が成功した。
  [物理IO CI 43460078329](https://github.com/harupipipipi/tobkiri/actions/runs/43460078329)は
  両OSで7試験を通過。検索権限だけのroot、選択leafの欠落とsymlink、rootの置換、
  元の定義ファイルの変更後に同じleafを使うこと、各操作後のFD解放を確認した。
  [実Wasm 43460009366](https://github.com/harupipipipi/tobkiri/actions/runs/43460009366)は
  portable 211件と実構築、macOSの実Runtime.init、物理IO 7件、Coreが引き継ぐ
  ownerの検証を通過した。
  現在の同梱variantが使う1階層のファイル名を対象とし、schema上有効な複数階層の
  variantは未移植。複数階層では途中の各親を原本と同じ順で検査する必要がある。
- `9155e4707` は最初のleafと親の検査が終わったあと、元の `artifact_digest` に
  入る時点で選択leafを置換または欠落させる。原本は再度symlinkを確認し、symlinkなら
  ツリー用の例外を返す。欠落ではfile、directoryの両判定を実行してから拒否する。
  [原本CI 43462289407](https://github.com/harupipipipi/tobkiri/actions/runs/43462289407)
  は両OSの原本とLinuxの310参照の照合に成功した。
- `988466564` は元のツリー入口のsymlink、file、directoryの判定を、同じrootの
  同一性と選択leafを使って順に実行する。fileなら元のreadの直前、directoryなら
  列挙直前で停止する。判定の間に新しいsymlinkへ変わった場合は追跡を拒否する。
  [物理IOと原本CI 43463494533](https://github.com/harupipipipi/tobkiri/actions/runs/43463494533)、
  [実Wasm CI 43463522873](https://github.com/harupipipipi/tobkiri/actions/runs/43463522873)
  の物理IOをLinux・macOSで通過。通常のfileとdirectory、欠落、ツリー入口でのsymlink、
  type判定直前のsymlink置換を確認した。実Wasm CIはportable 211件、macOSの実初期化、
  物理IO 7件とCoreによるowner引き継ぎも成功した。両OSの原本と310参照の
  [再比較 43463494534](https://github.com/harupipipipi/tobkiri/actions/runs/43463494534)
  も成功した。ファイルの内容、ツリー列挙、署名、起動の検証は残る。
- `faea02dd7` は選択済みShellディレクトリを同じ物理ディレクトリから列挙し、
  元のPack側で名前順に並べる。既存カタログと列挙・FD解放の処理を共用する。
  最初の子の検査までが次の境界。UTF-8で表せない名前は現段階では拒否する。
  [初回Wasm CI 43469000639](https://github.com/harupipipipi/tobkiri/actions/runs/43469000639)
  でResolver終了後に列挙状態が残った。終了時にクリアする `32dc813b5` を送り、
  [再検証 43469616491](https://github.com/harupipipipi/tobkiri/actions/runs/43469616491)
  のportableと実Wasm構築が成功した。macOSのHost試験は次のRustテストの
  コンパイルエラーで停止した。
  [初回物理IO CI 43468738747](https://github.com/harupipipipi/tobkiri/actions/runs/43468738747)
  は試験で `resolved` の所有権を移してしまいコンパイルで停止した。試験を
  `86c3edbd6` で修正した。
- `ebb161e5b` は選択済みShellが通常ファイルの場合のopen、前後のfstat、
  最大1 MiBずつの読み取り、closeをRustの同じFDへつなぐ。元のPackは読み取り前後の
  メタデータとサイズを比較し、同じdigestを計算する。次のentrypoint解決と
  ディレクトリ内の子は未接続。既存 `fd_stat.rs` の出力処理とcaptured sourceの
  所有者検査を共用した。ABIの異なる既存 `fd_read.rs` と読み取り・base64を
  共用できるかは、物理IO・エラー・予算の比較後に検討する。
  [実Wasm CI 43471331247](https://github.com/harupipipipi/tobkiri/actions/runs/43471331247)
  のportable 213件と実Wasm構築が成功した。原本の関数を使うdigestの試験と、
  元モジュールの限定OS importを含む。Rust側は
  [物理IO・Clippy 43471279499](https://github.com/harupipipipi/tobkiri/actions/runs/43471279499)
  のLinuxでファイルopen時の借用エラーを検出し、`0a58ff9b7` で保持済みの同一性を
  openerに直接渡した。[物理IO・Clippyの再検証 43473201935](https://github.com/harupipipipi/tobkiri/actions/runs/43473201935)
  でLinuxとmacOSの物理IO 7件ずつが通過。open後のファイル置換でも保持した
  FDのバイトと前後のstatが一致し、close後の確保量と予約が0になることを確認した。
  両OSのstrict Clippyも成功。[実Wasmの再検証 43473207079](https://github.com/harupipipipi/tobkiri/actions/runs/43473207079)
  は構築・portable 213件、macOSでのRuntime.initと物理IO 7件、Coreによるownerの
  引き継ぎを通過した。実Wasmの全カタログを経由するShell検証、製品への採用、
  GUIでの会話、VM廃止は未確認。
- `b14a2fd73` は同じディレクトリ列挙とファイルFDを使い、選択済みShellの下を
  原本と同じ名前順・深さ優先で走査する。Wasmには物理列挙順で名前を返し、
  Rustは同じ列挙結果を名前順に保持して次の一件を照合する。祖先の置換を拒否し、
  空ディレクトリと正常に閉じたファイルの後に進む。各FDは当該ファイルのパスと
  照合する。UTF-8の名前、128ディレクトリ段、OSの `PATH_MAX` 内が現在の範囲。
  Packが元のdigestと比較し、次のentrypoint解決で停止する。
  [物理IO・Clippy 43477187457](https://github.com/harupipipipi/tobkiri/actions/runs/43477187457)
  でLinux・macOSの物理IO各7件とstrict Clippyが通過した。
  [実Wasm 43477196433](https://github.com/harupipipipi/tobkiri/actions/runs/43477196433)
  は構築、portable 214件、macOSのRuntime.init・物理IO・Core引き継ぎを通過した。
- `94287c291` は元のdigestが一致した直後にentrypointが欠落・symlink化・内容変更
  する三つのケースを、独立した原本で実行する。通常ファイルではツリー用FDと
  entrypoint用FDを別に開く。内容変更では二度目のdigest比較で元の例外が出る。
  [原本CI 43477617349](https://github.com/harupipipipi/tobkiri/actions/runs/43477617349)
  はLinux・macOSで成功。欠落時はstrict resolveでFileNotFoundError、symlink化では
  解決後の元entrypoint判定、内容変更では再読込後のdigest比較で拒否した。
  plistとヘッダーの読み取りは今後の対象。
  `f47b5bfee` はShell実装のみの変更でも物理IO CIを実行する。
- `be3e437a4` は確保済みのShell定義からentrypointを選び、ツリーの後に
  entrypointとartifactを元の順でstrict resolveする。直下のシンボリックリンクは
  同じ親内の一段をメタデータで解決し、元パスのsymlink判定で拒否する。中間
  ディレクトリを検査して同一性を保持し、次の判定とopenで再確認する。
  ツリーと同じファイルを使うAppImageでもFDを開き直し、前後のstatとdigest比較は
  元のWasm側で実行する。同じFDプロトコルを共用し、ツリー用とentrypoint用を区別する。
  plistとヘッダーの最初の読み取りが次の境界。
  [初回の物理IO・Clippy 43481161858](https://github.com/harupipipipi/tobkiri/actions/runs/43481161858)
  は両OSで物理IO 7件を通過した。
  [初回Wasm CI 43481172131](https://github.com/harupipipipi/tobkiri/actions/runs/43481172131)
  はLinux用の試験fixtureが原本のヘッダー関数を読み込まず停止した。
  `c5392ab6b` と `3b29b4de8` で単体AppImageの経路と原本の関数を揃えた。
  [再検証 43481460283](https://github.com/harupipipipi/tobkiri/actions/runs/43481460283)
  は実Wasm構築、portable 215件、macOSの実Runtime.init・物理IO 7件・Coreへの
  引き継ぎを通過した。strict Clippyの型の複雑さの指摘は `294244199` で親の
  格納先を借りる形に直した。
  [物理IO・Clippyの再検証 43482610397](https://github.com/harupipipipi/tobkiri/actions/runs/43482610397)
  も両OSで物理IO 7件とstrict Clippyを通過した。全カタログを通した製品Setupは未確認。
- `ad560de80` は元のmacOS検証が要求する `Contents/Info.plist` を固定順で
  再検査し、保持済みShell内のFDから読む。内容のplist解析とbundle ID比較は元の
  Pack関数が行う。entrypointヘッダー専用の新しいFDは前後のstat、先頭64バイト、
  PEの場合は実ヘッダーで指定された一つの位置へのseekと24バイトだけを許可する。
  検証関数が対象のShellを実際に返し、RustでヘッダーFDを閉じた後にのみ、次の
  Pack構成の入口に到達する。元のSetupが指定するコード署名フラグはfalse。
  `1962dcca4` でplist・entrypointが同じルート再取得を使い、Mach-O・ELF・PEの
  元関数をfixtureで実行する。既存の予算付きreadを利用し、plistの転送は4 MiBの
  既存フレームとbase64の上限内に収めた。
  [初回Wasm CI 43486476877](https://github.com/harupipipipi/tobkiri/actions/runs/43486476877)
  でfixtureの `plistlib` 未設定を検出し、`1aef1dcb7` で修正した。
  [再検証 43486570483](https://github.com/harupipipipi/tobkiri/actions/runs/43486570483)
  でportable 216件、実Wasm構築、macOSの実初期化・物理IO・Core引き継ぎを通過。
  `1962dcca4` の [実Wasm 43486950528](https://github.com/harupipipipi/tobkiri/actions/runs/43486950528)
  も構築、portable 216件、macOSの実初期化・物理IO 7件・Core引き継ぎを通過した。
  [物理IO・Clippy 43486940947](https://github.com/harupipipipi/tobkiri/actions/runs/43486940947)
  は両OSの物理IO各7件とstrict Clippyが成功。カタログ全体から同じStoreでShellを通る
  確認と製品採用は残る。
- `7f982e39f` は元のShell検証が返した対象を保持し、Pack解決の続きへ渡す。
  元の `ResolvedDefaultProfile` と Profile・Lock・Plan を同じStoreの参照として保持する。
  Rustは完了済みの物理Shell検証、参照のscope、4件の独立性、解放対象を照合する。
  [Wasm CI 43491915570](https://github.com/harupipipipi/tobkiri/actions/runs/43491915570)
  はportableテストとWasm構築、macOSで実Runtime初期化・物理ソースの検証、
  戻り値の受け入れ条件・Coreへの引き継ぎを通過した。
- `5476f2994` の[原本CI 43492718553](https://github.com/harupipipipi/tobkiri/actions/runs/43492718553)
  はLinux/macOSの各10件のShell試験と、配布Profileの解決成功および古いApplication Pack
  digestによる拒否を通過した。成功時は元のPortが同じ戻り値を返し、Profile v5、Plan v2、
  Lock v5を順に検証した実オブジェクトと完全なcanonical digestを記録した。
  両OSでbinding 310件、variant pin 123件、projection 0件。拒否時は同じ例外が外側へ
  伝播し、両ケースともユーザーデータが作成されていない。
- `45b0001ca` はprojectionが空でも実行される `profile_content_projection.py` の
  `Path(__file__).resolve()` を既存のRustメタデータ処理へ接続した。元のShell検証後の
  固定呼び出しで、封印済みローダーが指定した一つのソースだけを解決する。元のパス
  解決、スキーマ検証、digest計算の順序はそのまま。一般のファイル読み取りや追加
  projectionの権限は発行しない。将来の整理では、原本との差分が通った後に既存の
  getterとprojection用のscope・FD・応答生成の共通部分を再点検する。
  [Rust/Wasm CI 43493671067](https://github.com/harupipipipi/tobkiri/actions/runs/43493671067)
  はportableテスト、Wasm構築、macOSの実Runtime初期化・物理検証・Core引き継ぎを通過。
- `8003603fa` の[原本CI 43495702562](https://github.com/harupipipipi/tobkiri/actions/runs/43495702562)
  は、配布Profile解決後のPack pin predecessorに、新規状態と壊れたactive.jsonの2件を
  追加した。macOSで同じ定義ストアが独立したロックで二度snapshotを取得すること、
  定義が未登録でも `verify_snapshot=True` でポインターを読むことを確認した。
  新規状態はその後カタログを引いて `None` を返し、定義ロックファイルだけが残る。
  壊れたポインターはJSON→canonical→active storeの例外を保持し、カタログを引く前に止まる。
- `42ebccf9e` は同じ選択済みuser-data rootのRust処理にこの順序を接続した。既存の定義
  snapshotとActiveProfileStoreの検証を再利用し、定義ストアは一度だけ構築する。
  カタログは同じStore上で改めて引く。定義とポインターが一致する場合だけ元のcanonical
  digestを計算する。新規状態の次は `inactive_bootstrap_predecessor`、一致する登録済み
  状態の次はAuthorityとactivationの読込。追加した読み取りは承認や状態公開を行わない。
  `e5a406d65` で固定原本とRustの例外グラフおよび残存ファイルを照合する試験を追加。
  [原本比較CI 43496444761](https://github.com/harupipipipi/tobkiri/actions/runs/43496444761)、
  [物理読み取りCI 43496130105](https://github.com/harupipipipi/tobkiri/actions/runs/43496130105)、
  [Wasm CI 43496182926](https://github.com/harupipipipi/tobkiri/actions/runs/43496182926)
  を開始した。原本CI 43495702562はLinux/macOSで成功し、原本比較CI 43496444761は
  Linux上で新規・破損時の例外グラフと残存ファイルの実Rust比較も通過した。
- `8be436a83` は元の新規Setupで、activeポインター2件とindexの検査、元のPackが
  確認文書を返すまでを観測する。解決済みオブジェクトとAuthority引数の同一性、
  確認文書のキー順と全体digest、順序付きbindingsの件数とdigestを記録する。
  [原本CI 43498267826](https://github.com/harupipipipi/tobkiri/actions/runs/43498267826)
  で検証中。
- `2adf48f3d` は引き継ぎがない場合のパス確認と、同じStoreの元の `bootstrap_confirmation`
  を接続した。元の `ResolvedDefaultProfile`、ID、Authority引数を再利用し、確認文書と
  その参照を保持する。Rustはscope、識別子、全体digestを照合する。`b6fd06d97` で
  Unixの実stat/lstatの順序と例外伝播を揃えた。元のパス判定をRustで重複実装する箇所は、
  両側の確認が揃った段階で共通化を検討する。
  [Rust/Wasm CI 43498676115](https://github.com/harupipipipi/tobkiri/actions/runs/43498676115)と
  [原本・Rust CI 43498647143](https://github.com/harupipipipi/tobkiri/actions/runs/43498647143)
  を起動した。確認を受け取った後のpreview catalog、登録済みProfileの復旧、認証済み
  handover、全カタログの燃料制限とGUI検証が残る。
- 既存の `tobkiri_launcher/src-tauri/src/defaultspack_authority.rs` の
  `artifact_tree_digest` も同じ木をハッシュする。現状はファイル全体を読み、
  パス名をlossy UTF-8にしている。元のPack検証では順序付きの相対名を厳密にUTF-8化し、
  実FDから分割して読み、前後の同一性とcloseを確認する。まず元の動作を保つ有限の
  ファイル操作を接続する。両経路の呼び出し順・拒否・バイトが比較できたら、
  ファイル読み取りとハッシュの共通化で重複を減らせるか確認する。
- 将来の整理候補: `value_graph.py` の `_bootstrap_identity` とHostの `BootstrapIdentity*` は
  元の固定getterだけでなくcatalog・manifest・lookupも扱う。全固定呼出しの比較が通った後、
  wireのタグ、実参照、同じStoreでの保持を保ったまま名前と共有責務を揃える。

## 040: 公開Profileへ混入しない既存データ

- `crates/tobkiri-e2e/tests/storage_tiers.rs` に、既存の実Wasm経路で3段の値と承認を
  作ったProfileを公開する回帰試験を追加した。`0f79c68af` はユーザー全体のストレージを
  明示のdangerous承認で書き込み、通常の `Core::set_secret` でOSキーチェーンを使うvaultを
  作る。公開形式は従来の構造上の許可リストを使い、取り込みは通常の門で新規承認する。
- 公開した文書と取り込んだProfileを確認し、元の3段の値、別のProfile用のvault、
  承認記録が持ち込まれないことを検証する。明示承認後にアクセスできるユーザー全体の
  ストレージは同一データrootで共有されるため、公開文書の除外と、共有ストアを読む権限を
  別に確認する。元のストアと暗号化済みvaultも照合する。
  [初回CI 43406959874](https://github.com/harupipipipi/tobkiri/actions/runs/43406959874)
  は試験の `ApprovalStore` のimport先が誤っており、実行前のコンパイルで停止した。
  `717ac4ed0` で正規のGate traitをimportし、`457c1973a` で試験用WATの括弧、
  `70290aa6e` で取り込みのUser承認が照合するStrictV1引数のdigestを直した。
  [CI 43412262677](https://github.com/harupipipipi/tobkiri/actions/runs/43412262677)は次に
  Core管理領域の上限で停止。`413237d7e` はclose済みSessionを必要な範囲で解放し、
  失敗した操作名を記録する。`09c379110` はrustfmt、`17ac67b2a` は既存の `anyhow::Error`
  に直接contextを足す修正。上限を維持した
  [CI 43418877488](https://github.com/harupipipipi/tobkiri/actions/runs/43418877488)は
  実Wasmでの公開・取り込み・書き戻しに進み、二つの書き手が同じpublicキーを上書きする
  最終確認で、テストの期待値が誤っていた。`92373039b` で最初の書き込み直後に別Packが
  同じ値を読めることを加え、二番目の書き込み後は両Packで最新値を確認する。
  [再実行CI 43420621105](https://github.com/harupipipipi/tobkiri/actions/runs/43420621105)が
  macOSで成功し、承認項目16を確認済みにした。OSキーチェーンを使うvault、既存承認、
  Profileの3段の値、承認後の共有ストアを同じ試験で照合した。

## 041: 保存済みコンポーネントが参照する旧Profile

- [全体CI 43407299708](https://github.com/harupipipipi/tobkiri/actions/runs/43407299708)の
  foundationsは、旧Python Packの保持用コンポーネントを準備する際に
  `inventory.json` の原本照合で停止した。原本との差は
  `defaults.profile.v5.json` と `defaults.profile.intent.v1.json` の二つ。
  `eb5dabff0` のProject用の権限辺と `5ae2b0265` の生成元の更新によるもので、
  Tool Bridgeが参照する4本の辺、Gatewayのコード、Core WIT、SDKのハッシュは一致した。
- 保存済み実行物は `42dfc6e02` の二つのProfile文書と結び付いている。CIは現在の
  検証ツールと二つの旧文書を別ディレクトリに配置して既存の照合を実行する。
  Rustは旧文書2件の再読込だけを同じ場所に向け、他のソースと実行本体は現在のcheckoutを使う。
  固定のreceipt、バイナリのハッシュ、展開・実行上限は既存の条件で検証する。
  現行Profileと配布物は別途検証し、旧成果物の再生を現行Profileの互換性に数えない。
- `e447c605a` の[全体CI 43408890320](https://github.com/harupipipipi/tobkiri/actions/runs/43408890320)
  で旧Wasmの保持・展開試験8件とGateway試験21件が通過した。次のBootstrap試験は、
  個別起動時に `tobkiri_protocol` がimportできず停止した。`4d5139111` で他の試験と
  同様にcheckout内のruntimeを明示し、
  [全体CI 43409309579](https://github.com/harupipipipi/tobkiri/actions/runs/43409309579)で
  Bootstrap試験は通過した。次のソース試験には既存のcatalogモード2件が期待集合に
  入っていなかった。`c7b55fb30` で2件を追加し、すべてが同じ排他groupに属することを
  引き続き厳密に確認する。
  最終の移行削除ゲートは別途、未移植の棚卸しを検出している。
- `7b291dc0f` では全体CIが実行したSetup Store 16件を個別に確認し、期待件数を16に更新した。
  `6761811cf` はCoreの管理を明示の上限付きcounterへ揃え、
  [Launcher CI 43409912357](https://github.com/harupipipipi/tobkiri/actions/runs/43409912357)で
  実Tauriのコンパイルとportable検証を通過した。
- `98bb10ab6` はProject関連で変わったsource closureを現行の元Pythonで再実行した。
  [再生成CI 43413658285](https://github.com/harupipipipi/tobkiri/actions/runs/43413658285)の
  固定CPython 3.13.9で、Provider Registryの30ケース/85操作と設定の23ケースの入出力が
  前回の原本と一致した。全221ファイルの参照ハッシュとsourceを照合し、変わった時計値と
  参照ハッシュを保存した。同じCIの独立した旧Profile解決は現行の2つのcatalog JSONが
  固定原本と異なり停止しており、この再生成でその比較が通ったとは数えない。
- [全体CI 43417464300](https://github.com/harupipipipi/tobkiri/actions/runs/43417464300)の
  foundationsは、スレッドのスケジューリング次第で回数を使い切れる並行解放試験で失敗した。
  `17ac67b2a` では実リースの解放前、競合中、解放後をbarrierで同期し、上限と
  実リースの処理は変更していない。
  [再実行CI 43419679993](https://github.com/harupipipipi/tobkiri/actions/runs/43419679993)で
  Gate試験は成功。続く全体のstrict Clippyで、Harness試験の型・定数検査・条件式の
  警告を検出した。`86f18a7f2` で検査を保ったまま直し、
  [全体CI 43421823935](https://github.com/harupipipipi/tobkiri/actions/runs/43421823935)で
  Linux foundationsとstrict Clippy、Python比較、macOSの型検査が成功した。
  macOSではRustワークスペース1,458件と実Python Packの順序・Tool Caller・共有モジュールの
  再生も成功した。macOSのstrict Clippyは、テスト内の未使用import1件と
  `.err().expect()` 2件を検出した。未使用importは `873695897` で修正済み。
  `2e785d2c4` で残りを実エラーを検査する `expect_err` に揃えた。
  [再実行CI 43429417116](https://github.com/harupipipipi/tobkiri/actions/runs/43429417116)
  はLinux、macOS、厳格lint、Python Packの再生が成功し、最終ゲートで未移植を検出した。
