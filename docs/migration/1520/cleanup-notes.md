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
- `3e66f6bbb` はASCIIのbase URIを受け付けた時点で検証済みのscheme境界を保持し、
  後続の相対URI判定と結合に再利用する。baseのscheme中には除去対象の制御文字がない。
  参照側は従来通り制御文字の除去後にschemeを調べ、元の引数をそのまま返す経路も保つ。
  制御文字と大文字を含む2組を追加し、[原本比較43548960540](https://github.com/harupipipipi/tobkiri/actions/runs/43548960540)で
  155組を確認した。[実Wasm計測43548996895](https://github.com/harupipipipi/tobkiri/actions/runs/43548996895)の
  41件の評価区間は1,665,145,906 fuel（10,951,942減）。42件目の評価開始時は
  159,637,695 fuelで、その途中で燃料切れ。ピーク25,205,956 bytes、終了後の確保・予約0。
  [無計測CI43549005667](https://github.com/harupipipipi/tobkiri/actions/runs/43549005667)も
  coldと全カタログが既存の燃料上限で停止した。
- `b7e56d0a8` は参照解決で毎回作成していた固定の属性名を再利用する。Registryの実際の
  descriptor取得とコールバック、結果の参照、例外順序は維持する。
  [原本比較43551375081](https://github.com/harupipipipi/tobkiri/actions/runs/43551375081)と
  [燃料計測43551374165](https://github.com/harupipipipi/tobkiri/actions/runs/43551374165)、
  [無計測CI43551376678](https://github.com/harupipipipi/tobkiri/actions/runs/43551376678)で検証する。
- `b7e56d0a8` の原本比較が通過し、無計測の実Wasmでは42件すべてのcold schema-storeが
  元と同じ134,817バイトを返した。燃料残り8,308,664、終了後の確保量0。カタログ全体は
  同じ成果物の別の実行で燃料切れだった。計測版は42件目の評価完了後に26,266,880 fuelを
  記録し、後続の処理で停止した。`25268e5c7` はPythonの実iteratorを保持したまま進め、
  反復ごとの同じ参照の追加・解放を省く。[原本比較43552218512](https://github.com/harupipipipi/tobkiri/actions/runs/43552218512)は通過し、
  [実Wasm43552218468](https://github.com/harupipipipi/tobkiri/actions/runs/43552218468)も同じ42件と出力を確認した。
  燃料残りは34,280,686（25,972,022増）。全カタログは燃料切れで、終了後の確保量は0だった。
- `edc636278` はCI専用の計測成果物で、実カタログの読み込み件数の節目、スキーマの読み込み件数と
  最後のファイル名を記録する。[実Wasm計測43552917269](https://github.com/harupipipipi/tobkiri/actions/runs/43552917269)で
  上限を保ったまま、最初のカタログ項目の処理中に停止したことを確認した。32回目のスキーマ読込
  `resolved_plan_v2.schema.json` で57,050,015 fuelが残り、次の読込は観測されなかった。
  計測付きの独立初期化は42件を完了し、19,199,366 fuelが残った。両方で終了後の確保・予約は0。
  `76ef62280` は処理のたびに作成していた固定の属性名を再利用し、実際の属性取得、descriptor、
  `create_resource` の呼び出しは維持する。[実Wasm43553887697](https://github.com/harupipipipi/tobkiri/actions/runs/43553887697)で
  独立初期化の出力が一致し、105,513,931 fuelが残った。全カタログは燃料切れで停止した。
- 全カタログは最初のPackを検証する前に、埋め込みblob 305個とlockの計2,408,795バイトを展開・検証する。
  最初のPackのスキーマも、別々の名前のキャッシュキーで検証される。
  `065fd46d2` で実ソース所有者の構築前後を計測し、ローダーの処理から切り分ける。
  [計測CI43554257071](https://github.com/harupipipipi/tobkiri/actions/runs/43554257071)
- `2f5a685c9` は既存の埋め込みindexを型と配列順を保って比較し、不一致時は従来のJSON比較を使う。
  blobの重複判定は実計算済みSHA-256で候補を絞り、同じdigest内で実バイトを比較する。
  `446676b18` はApp計測・比較成果物に限り、埋め込みカタログのソースを展開不要な既存codecで保持する。
  [JSON比較後43554376094](https://github.com/harupipipipi/tobkiri/actions/runs/43554376094)は42件を完了し、全カタログは燃料切れ。
  [展開省略後43554528659](https://github.com/harupipipipi/tobkiri/actions/runs/43554528659)は内側Wasmが
  37,765,552バイトとなり、既存32MiB上限で停止した。`dec0146e2` で無圧縮化を取り消した。
- [実測43554257071](https://github.com/harupipipipi/tobkiri/actions/runs/43554257071)の
  データ所有者の構築前後は1,966,154,985→1,413,862,289 fuel。全カタログは最初の項目の処理中、
  33回目のスキーマ読込後に停止した。`b7a60f5f3` はindex、blobの展開・検証、読込対応表、
  最終照合に分けて計測する。[計測CI43555145202](https://github.com/harupipipipi/tobkiri/actions/runs/43555145202)
- 上記の計測で、準備全体は524,161,698 fuel。内訳はindex 57,556,739、blobの展開と検証
  443,743,216、読込対応表16,678,229、最終照合6,183,514。終了後の確保量は0だった。
  `1a5de1e3b` はApp比較用の複製ローダーだけ、展開後の実バイトを同じ上限でRustのSHA-256に渡す。
  圧縮形式、実バイトの照合、照合順を維持する。[燃料比較43555818184](https://github.com/harupipipipi/tobkiri/actions/runs/43555818184)と
  [無計測43555830118](https://github.com/harupipipipi/tobkiri/actions/runs/43555830118)で確認する。
  `5c31e575c` はソースlockのCIを既存の共通検証に揃え、全featureを有効にして依存を解決する。
  [依存解決43555901004](https://github.com/harupipipipi/tobkiri/actions/runs/43555901004)が通過した。
- 展開後のSHA-256の移植では、blob区間が443,743,216から447,586,160 fuelに増えた。
  独立スキーマ初期化は同じ出力で完了し、全カタログは引き続き最初の項目で停止した。
  `2ac5e9478` で展開後の検証を元の実装に戻した。`f84bc1f1f` ではApp比較用の
  カタログ本体が、元の初回読込と再読込それぞれの実バイトをRustでハッシュする。
  対象は4MiB以内の組み込み `bytes`。元のcallback、入力順、比較結果、対象外のハッシュ動作を維持する。
  [変更後の計測43556473713](https://github.com/harupipipipi/tobkiri/actions/runs/43556473713)では
  診断用の独立初期化42件が91,387,365 fuelを残して元と同じ出力を返した。全カタログは最初の項目で
  33回目のスキーマを読み、60,500,716 fuelから停止した。終了後の確保量は0。
  [通常の比較43556451217](https://github.com/harupipipipi/tobkiri/actions/runs/43556451217)は別構成で、
  LinuxとmacOSの両方で独立初期化が燃料切れになった。通常の比較の通過には含めない。

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
- `590a7d8a6` は元のレビューが返す表示用catalogと確認文書を観測する。配布fixtureの
  Profileは1件。catalogの他のフィールドが同じ実オブジェクトを指し、確認の浅いコピーが
  元の値の参照を保つことを検査する。`52acfc911` で元のHostのSetup一覧まで実行し、
  表示順、確認digestと一覧全体のdigestを記録する。
  [表示用catalogの原本CI 43500116695](https://github.com/harupipipipi/tobkiri/actions/runs/43500116695)で
  Linuxの原本観測が成功した。[一覧の原本CI 43500923792](https://github.com/harupipipipi/tobkiri/actions/runs/43500923792)も
  Linuxで原本観測を通過し、Rust側の検証を継続中。
- `b93226a1f` は元の `catalog_with_profiles` と `setup_listing` を同じStoreで呼び、確認文書を
  参照のまま渡す。元のSetupスキーマを通った一覧をRustで受け、確認の全内容と表示状態を
  照合する。既存のLauncher workerは元の64 MiB枠で一覧のUTF-8を確保し、Storeの終了と
  shutdown状態の確認後に返せる。製品のGUI経路への採用と実表示は未検証。
  前回のLinux確認試験はOS固有の永続ストレージを開けず失敗した。文書の受入検査には
  テスト用の割当を使い、実ストレージの試験は既存のmacOS経路で継続する。
  [Rust検証 43500681279](https://github.com/harupipipipi/tobkiri/actions/runs/43500681279)と
  [Wasm CI 43500714040](https://github.com/harupipipipi/tobkiri/actions/runs/43500714040)を開始した。
- 一覧の原本CI 43500923792 は原本観測後にRustの型不一致で停止した。
  `5a1c61171` で一覧の二重Resultとテスト用割当の型を修正した。
  `988eb416c` は非アクティブProfileの定義を選択済みrootから別のロックで読む。
  元の同一Storeで登録文書と元の配布Profileを辞書比較するため、boolと整数のような
  Pythonの比較規則も維持する。行なし、同一定義、tombstone、差分ありの各分岐を
  固定原本から採取し、登録と引き継ぎファイルを保持する。
- `37bb337ae` はジャーナルと32バイト鍵の読み取りに既存のSecureDirectoryを再利用する。
  読み取り前と途中のサイズ上限、ファイルの同一性、バッファの消去を保持する。
  canonical HMACを確認し、完了状態とレジストリ世代・snapshot digest・bootstrap状態を
  照合する。UTF-8の書き込み形式を対象とし、UTF-16/32の入力は境界で停止する。
  移行元定義のinert判定、activeの再検査、承認文書への結合と登録直前のCASは次段階。
  [固定原本との比較 43504460895](https://github.com/harupipipipi/tobkiri/actions/runs/43504460895) と
  [定義ストア・Clippy 43504460839](https://github.com/harupipipipi/tobkiri/actions/runs/43504460839)を起動した。
  Linuxの固定原本観測は通過。実Rust比較と
  [Wasm CI 43504693992](https://github.com/harupipipipi/tobkiri/actions/runs/43504693992)、
  [同一invocationのCI 43504764640](https://github.com/harupipipipi/tobkiri/actions/runs/43504764640)は検証中。
  今後は同一定義を比較するためのcanonical出力と再パースを減らせるか検討する。
  同じ元オブジェクトに対する辞書比較、元の短絡順序、容量計上と例外を保つことが条件。
- `89d72b2b3` は引き継ぎ元の定義が未解決かつAuthorityを持たないことを検証し、active
  ポインター2箇所を再検査してから、引き継ぎ元の13項目を元の承認文書に渡す。
  既存のPython文字列表現とcanonical出力を再利用し、Pack順序とShellの値を保持する。
  Rustが承認文書の引き継ぎ項目と全体digestを検証し、同じ文書がSetup一覧へ渡る。
  再digestしたPack順序改変、認証済みでもinert条件を満たさない定義、途中で現れた
  activeポインターを拒否するテストを追加した。
- `9a1f9e56e` は固定原本のSetup一覧まで実行して移行時の承認文書を採取する。
  `4ef8e2df6` は元の登録関数で、正常な後継revision追加と、定義更新・Host側／workspace側
  のactiveポインター出現時の拒否を採取する。
  [原本比較CI 43506425229](https://github.com/harupipipipi/tobkiri/actions/runs/43506425229)
  はLinuxの原本観測まで通過。[登録比較用CI 43506665301](https://github.com/harupipipipi/tobkiri/actions/runs/43506665301)
  と[Wasm CI 43506174622](https://github.com/harupipipipi/tobkiri/actions/runs/43506174622)で検証中。
  登録直前のworkspace lock → Host選択lock → 定義CASと、製品GUIでの承認・登録は未接続。
- `9d1c3291d` と `f0c6276b0` はCIの隔離したuser-dataで、workspace lock、Host選択lock、
  registry snapshotと独立した再取得によるCASを実行する。後継だけを履歴へ追加し、
  再署名したcanonical JSONを同一ディレクトリ内でアトミックに置換する。
  固定原本とindex.jsonをバイト単位で比較し、途中の編集、Host/workspaceのactiveポインター、
  承認時のdigest不一致を拒否する。書き込みはCIのfixture用で、製品の登録には未接続。
  `f0c6276b0` は前段階で発見したClippyの条件式も修正した。
  [承認文書と原本の登録観測 43506665301](https://github.com/harupipipipi/tobkiri/actions/runs/43506665301)
  はLinux/macOSで成功。新しい[登録CAS比較 43508549743](https://github.com/harupipipipi/tobkiri/actions/runs/43508549743)
  は実行中。製品への接続後、registry読み込みとcanonical出力後の再パースを一度の所有グラフで
  置き換えられるか調べる。確定済みの内容、履歴順序、物理ロック、保存形式の比較は維持する。
- `26542c5e0` と `70abd5ff9` は比較済みの定義グラフとJSON解析の一時予約を、次の
  認証済みジャーナル読み取りより前に解放する。重複キーの診断枠は最長トークンから求める。
  64 MiBの上限、ロック順序、保存形式を維持し、`30917702f` でworkspace lockのimportを
  補った。[登録比較CI 43547877973](https://github.com/harupipipipi/tobkiri/actions/runs/43547877973)
  はLinuxとmacOSで成功。LinuxのRust比較は登録成功時のファイル全バイトと、並行編集・
  Host側／workspace側のactive pointerによる拒否を照合した。
- `d3f560d14` と `501667fdc` は、元のPackによる事前判定と新しく生成した一覧との照合を
  同じStoreに接続した。承認と一致した候補は、元の確認文書、source Profile、bundle lockと
  PlanのdigestをHost側でも照合する。[実Wasm CI 43510084337](https://github.com/harupipipipi/tobkiri/actions/runs/43510084337)
  は `confirmed: 1` を409で拒否し、データrootを開かないことをmacOSで確認した。
  この経路は確認データを返すまで。専用GUI承認、公開直前の再読み取りと実登録は未接続。
- `437e4a433` は、照合済みの一覧の確認内容をJSONに再変換せず、同じ予算で保持した
  グラフから使う。元の固定Packの比較には、認証済み取り込みの実一覧での一致・古い一覧の
  拒否・整数の拒否を追加した。[比較CI 43548658443](https://github.com/harupipipipi/tobkiri/actions/runs/43548658443)
  で検証する。Launcherへ一覧を返す既存のJSON形式はそのまま。
- `761e0e4ee` の[固定原本比較43549477713](https://github.com/harupipipipi/tobkiri/actions/runs/43549477713)は
  公開ロックの下で元のcaptureを実行した。一致時はHostカタログ準備、source Profileと
  bundle lockの再取得後に最初の公開intentの入口へ達した。古い確認はカタログ準備前、
  sourceとbundle lockの変更はintent作成前に拒否した。既存ファイルとロック解放も確認した。
- `2d2415e90` は元のresolverが返したPlanとLockのdigestをその時点で記録し、後から返る
  Setup確認とsourceを照合する。sourceとそのdigestを同時に置き換えるケースを追加した。
  [最初のWasm CI43549752201](https://github.com/harupipipipi/tobkiri/actions/runs/43549752201)は
  テスト用PlanとLockに追加のdigestがないため3件で停止。`164ad2517` でfixtureを補い、
  [Wasm CI43550240528](https://github.com/harupipipipi/tobkiri/actions/runs/43550240528)で再検証する。
- `23b258989` は引き継ぎジャーナルが存在しない場合の検査と認証処理を共通化した。
  確認済みcaptureの検証経路では、不完全な引き継ぎをロック作成前に止め、intentがない
  場合も元の公開ロックを保持してHostの先行pointerを読み取る。復旧の入れ子は同じロックを
  借りる。ここで検証するのは書き込み前の範囲。実保存にはGUI承認、ロック下での再解決、
  物理的な再取得、Authorityとworkspaceの永続化が残る。
- [ロック比較43550162901](https://github.com/harupipipipi/tobkiri/actions/runs/43550162901)と
  [復旧比較43550162634](https://github.com/harupipipipi/tobkiri/actions/runs/43550162634)が通過。
  `001362994` の[比較43550579150](https://github.com/harupipipipi/tobkiri/actions/runs/43550579150)も通過し、
  承認後の復旧では古い先行pointerを拒否して署名済みintentを残すことを確認した。
- `72fcad6dc` は、元のwriterが生成した公開intentとRustで生成したintentの全バイトを
  比較する。隔離したテスト用の鍵を使い、確認文書全体のdigest、候補のactivation ID、
  署名・再読込、既存intentの保護を検査する。定義登録のアトミック書き込みを再利用し、
  intentが既にあるときは拒否する。鍵の作成と実GUIからの書き込みは別途必要。
  [初回43551150533](https://github.com/harupipipipi/tobkiri/actions/runs/43551150533)はLinuxの
  既存Clippyと競合。`7d4c272a1` が比較専用のRust 1.94.0を隔離し、
  [再実行43551451311](https://github.com/harupipipipi/tobkiri/actions/runs/43551451311)で検証する。
- 隔離した再実行と、書き込み直前に別のintentが出現しても置換しない `13cf074d0` の
  [比較43551670298](https://github.com/harupipipipi/tobkiri/actions/runs/43551670298)が通過。
  Linuxで元writerとの全バイト一致とRustでの認証、macOSで固定原本の観測が成功した。
- `ae227bb0c` は確認後の候補解決、Hostカタログ、配布カタログをそれぞれ読み、最後に
  ロックを独立して開く順序を固定原本で観測する。`e74e27f75` は既存の物理読込テストで
  選択時のパスを保持し、ローダー完了後にロックを一度だけ読み直す。前の読み込み後に
  バイトを入れ替えて違いを検出する。[原本比較43552133355](https://github.com/harupipipipi/tobkiri/actions/runs/43552133355)は両OSで成功。
  [物理読込43552300422](https://github.com/harupipipipi/tobkiri/actions/runs/43552300422)は先行ステップで
  11件が成功したが、CIが10件を期待して停止した。`279669889` は実件数に揃え、
  [再実行43553458280](https://github.com/harupipipipi/tobkiri/actions/runs/43553458280)で物理読込へ進める。
- `ecc8ca3b8` は固定原本の署名付きintent比較で、鍵がない場合の生成、同期、再読込を追加した。
  元と同じ64桁の形式と所有者専用の権限で保存し、既存鍵とintentを置換しない。危険なリンクは拒否する。
  [原本比較43553784493](https://github.com/harupipipipi/tobkiri/actions/runs/43553784493)は両OSで通過し、
  Linuxでは原本と同じ署名付きintent、初回鍵、危険なリンクの拒否をRustで確認した。
  製品の書込には、実際のクリックで得た確認と再解決、ジャーナル下の登録・有効化が必要。
- [再実行43553458280](https://github.com/harupipipipi/tobkiri/actions/runs/43553458280)で、ローダーが読んだ後の
  raw lockの再読込をLinux・macOSで確認した。`4a3a3e172` は元の最後のカタログ読込で得た
  Profileと別読込のraw lockを、CI用の署名付きintent書き込みの直前に候補と照合する。
  `e1c01c38a` は保持中の公開ロックが指すディレクトリを使い、置換が起きた場合は書き込み前に止める。
  [固定原本とRustの比較43555264904](https://github.com/harupipipipi/tobkiri/actions/runs/43555264904)で検証する。
  初回比較ではfixtureのパスを読み込み時にmoveしていたためRustのコンパイルが停止した。
  `b5ad4ae59` で同じパスを借用し、[再実行43555771881](https://github.com/harupipipipi/tobkiri/actions/runs/43555771881)で検証する。
  再実行は両OSで成功し、LinuxのRust writerでProfile、raw lock、公開先ディレクトリの変更を拒否した。
  実際のクリック後に新しいWasmローダーを動かす接続は残る。
- Setupの確認欄を移す際は `DefaultsReview.tsx` と `DefaultsConfirmationDetails.tsx` の表示を
  再利用する。現在の `legacy.rs` が表示するSetupと `core_host.rs` の既存承認シートは
  異なる入口。既存のUI操作を維持するため、元のSetup内にCoreが所有する独立の確認領域を
  配置し、Coreが表示データと入力を保持する。Tauriの親window権限を子まで持ち込まない
  構成と実機確認が前提。macOSでは既存 `main` window内のAppKitビューで独立した
  `WKWebView` を管理できる。Tauriの子webviewは親windowラベルでも権限が適用されるため、
  隔離にはTauri IPCを持たない構成を使う。既存の確認欄と実クリックを同じ表示データに結び付ける。
  製品切替時には旧 `/api/setup/packs/install` の直接送信も同じ承認経路に移し、既存の再検証UIを使う。
  Launch後の個別承認は既存の専用ウィンドウで行う。
- `042bd8258` は既存のv4検証を通信から分離し、既存の確認欄を読み取り専用で再利用する。
  [表示回帰43557093919](https://github.com/harupipipipi/tobkiri/actions/runs/43557093919)のmacOSステップが通過。
  `41f1e0162` は既存パネルとは別のビルドにし、長さを制限したbase64のUTF-8 JSONだけを表示する。
  アクション、クリップボード書き込み、承認IPCを持たず、表示に失敗した場合はエラーを返す。
  [資産ビルド43557247571](https://github.com/harupipipipi/tobkiri/actions/runs/43557247571)では
  macOSの表示回帰と独立資産のビルドが通過した。
  `b7c25251e` はビルド済みのJSとCSSを個別のCSPハッシュ付きで一枚のHTMLに含め、
  メモリからWebKitに渡せる形にした。ファイル配布用のschemeとTauri資産ルートの
  追加が不要になった。[独立ビルド43559363670](https://github.com/harupipipipi/tobkiri/actions/runs/43559363670)が通過。
- `c92779404` はAppKitの表示枠に読み取り専用の `WKWebView` を載せる。
  独立の非永続データストアを使い、初回のメモリ文書だけを許可する。
  [初回43559771589](https://github.com/harupipipipi/tobkiri/actions/runs/43559771589)は
  WebKitの既定機能が既存ロックに含まれない依存を追加するため停止した。
  `6dd360e41` は利用するWebKitバインディングだけを明示し、元の確認欄が使うTailwind
  クラスを資産に含める。[再実行43559922441](https://github.com/harupipipipi/tobkiri/actions/runs/43559922441)で
  実表示、非永続設定、ナビゲーション拒否、表示枠のサイズ追従、取り外しを確認した。
- 実際の `main` windowはsplashから認証済み `/panel/` に進み、ReactがSetupを選ぶ。
  `Setup.tsx` の確認欄は引き継ぎUIの後にあり、外側がスクロールする。
  Core所有の表示を載せる際はスクロール・レイアウト・フォーカスを追従し、候補の再取得、
  Launchへの遷移、親の非表示化で取り外す。既存main-thread dispatchは待機期限で
  キューの処理を取り消さないため、表示前に候補と親がまだ有効かを再確認する。
  製品用の挿入、承認の結び付け、DMGへの組み込みは残る。
- `4e86bfd47` は表示範囲と文書全体の枠を分け、既存のWebKit座標からAppKitのポイントへ
  変換する。画面外の部分はクリップして、文書の幅と高さを保つ。
  [AppKit比較43561063548](https://github.com/harupipipipi/tobkiri/actions/runs/43561063548)で検証する。
- `92a554c06` は元のSetupに測定用の属性を付け、Rustが決まった式を親WebKitで実行する。
  DOMから取得するのは表示座標だけ。クリップ祖先、モーダル、非表示、拡大中を処理する。
  JavaScriptの2件は通過。[WebKit測定43561318088](https://github.com/harupipipipi/tobkiri/actions/runs/43561318088)で実表示を検証する。
  `f562ba942` は表示を候補単位で保有し、親のポート・ルート・非表示状態を再確認し、
  取り消し済みの測定結果を捨てる。[寿命の検証43561542672](https://github.com/harupipipipi/tobkiri/actions/runs/43561542672)を実行中。
  最初の2回はAppKitの非保持 `superview()` 取得でコンパイルが停止した。
  `ca1a62f50` は保持中のコンテナを呼び出し元から渡し、`subviews()` で実際の親子関係を検査する。
  [修正後43561750355](https://github.com/harupipipipi/tobkiri/actions/runs/43561750355)が通過。
  実WebKitで元のルートと同じパスの測定、文書全体のサイズを保ったクリップ、候補の取り消し、
  ウィンドウ非表示後の取り外しを確認した。
  製品の親は引き継ぎ状態に応じて上下に伸びる。Core所有の確認欄に切り替える時は、
  ビューの `body/#root` の画面高ではなく、確認欄のsectionの実寸を親に返して外側の
  スクロールを維持する。親のモーダルとnative sheetでの撤去、拡大表示、子のスクロール
  とフォーカスも既存経路で確認する。
- `9757b2495` と `2b7baf526` は既存LauncherのAppHandleで登録されたワーカーの
  完了結果を、Tauriの `main` に対応する実AppKitコンテナへ渡す。
  Storeの終了処理で失敗した結果や公開前に取り消された結果は表示に進めない。
  閉じる、非表示、セッション再認証、終了で世代を無効化し、遅れた結果を拒否する。
  独立Core Hostのビルドは既存LauncherのRustモジュールを含まないため、
  [Desktop Installers](https://github.com/harupipipipi/tobkiri/actions/workflows/desktop-installers.yml)
  に両機能を有効にした元Launcher側の回帰を追加した。
  実際の画面からの開始には、既存の権限検証を通った通常Wasmとモジュール索引の
  Launcher内での選択が残る。CI専用のパスや診断用Wasmを配布物に入れない。
- `e56f4d852` と `b61a83816` は同じ実行で成功したWasm生成レシート、全ソース一覧、
  planと現行ソースのprovenanceを照合する。Launcher用のデータレシートは通常版だけが生成する。
  [初回43563002676](https://github.com/harupipipipi/tobkiri/actions/runs/43563002676)は既存の
  テスト用入力に生成レシートがなかったため停止した。フィクスチャ修正後はローカル6件が通過し、
  [通常版43563363525](https://github.com/harupipipipi/tobkiri/actions/runs/43563363525)と
  [診断版43563365702](https://github.com/harupipipipi/tobkiri/actions/runs/43563365702)は
  生成に成功したが、macOS側が生成元のmode付き記録とサイズ・ハッシュだけの結果を
  比較して停止した。`ed4cae946` はアーカイブ転送をまたぐ2ファイルをサイズと
  SHA-256で照合する。生成元の検証は引き続きmodeも含む。実アーカイブ2件を使う
  binder単体の再現で双方が通り、通常版だけがLauncher用レシートを出力した。
  [通常版の再実行43564055821](https://github.com/harupipipipi/tobkiri/actions/runs/43564055821)でも
  macOS側の入力照合が通過した。
- `415322a55` は別途固定したLauncher用レシートを受け取り、照合したWasmを配布資産内の
  `native-setup/component.wasm` に配置する。Wasmハッシュとモジュール索引を同時にRustへ固定し、
  元ワーカーが同じFDから読み直してコンパイル前に照合する。開発版もビルドで封印した配置先を使う。
  [ビルドと配置の回帰43563344564](https://github.com/harupipipipi/tobkiri/actions/runs/43563344564)では
  AppKitの試験と、フィクスチャの変更・リンク・不正索引の拒否が通過した。
  `bab923a47` は通常版を作った同じCIで、binderが記録したレシートのハッシュを
  元Launcherのデバッグビルドへ渡す。ステージの封印とTauri側のコピーを検証する
  [配置CI 43564606334](https://github.com/harupipipipi/tobkiri/actions/runs/43564606334)は成功した。
  同じ通常版の実Wasmで初期化、元のLauncherでのステージ封印、Tauri側へのコピーを照合した。
  既定DMGへの固定レシート供給と、既存Setupルートからの開始、確認操作の接続は残る。
- `/panel/setup` は認証後のSPA内遷移なので、全ページのロード通知をSetup開始の
  根拠にはできない。現状の表示枠は確認チェックと有効化ボタンも含む。読み取り専用表示を
  載せる前に、親画面の候補切替・同じURLへの再マウントを判別し、確認に使う候補を
  同じ世代と実表示へ結び付ける。元の復旧は古いジャーナルを削除できるため、
  新規状態で事前実行するならRust側の所有・ロックの境界で新規性を確認する。
- `32ff72ccd` は既存Setupの確認候補を、WebKitのメインフレームで取得した要素単位の識別子と
  確認ダイジェストに結び付ける。識別子はWebKitの分離されたスクリプト環境が保持する。
  ワーカーの開始前に識別し、結果表示前と表示範囲の更新時に照合する。同じURL・ダイジェストの
  再マウント、候補再取得、ルート移動後に旧結果を表示しない。既存Setupの
  チェックボックスと有効化ボタンは元のままで、画面からワーカーを呼び出す接続は残る。
  ローカルのJavaScript 5件は通過。[初回実WebKit CI 43565562062](https://github.com/harupipipipi/tobkiri/actions/runs/43565562062)は
  テストHTMLのCSS括弧を `format!` 内でエスケープしておらず、実行前のコンパイルで停止した。
- `2b6575304` はテストのCSS括弧を修正し、隔離された確認欄に元のチェックボックスと
  有効化ボタンを再利用する。専用WebKitコンテントワールドは、実入力で確認したチェックボックスと
  同じ要素への確認操作を照合し、固定のメッセージを一度だけRustへ渡す。表示候補のdigestは
  Rustが保持し、メッセージには含めない。元のSetupの操作経路はまだこの入力に切り替えていない。
  ローカルのJavaScript 7件が通過した。[実WebKit再実行43566438224](https://github.com/harupipipipi/tobkiri/actions/runs/43566438224)
  はコンパイルと画面描画を通過したが、テストのマウス入力が確認チェックボックスに届かず停止した。
- `d328b93e2` はRustが保持する候補と親画面を確定操作時に再照合し、ワーカーが返した
  確認オブジェクト全体を次の実Wasm Storeへ渡す。元の判定はそのStoreで候補を再生成し、
  元の確認と比較する。検証済み判定はワーカー終了後にDATAとして返し、有効化の書き込みは
  今後接続する。テスト入力はAppKitのキーボード操作へ切り替えた。
  `bb161016d` は同じダイジェストの親候補を入れ替えた後に旧画面からの入力を拒否する回帰を追加した。
  [修正後のCI 43572029432](https://github.com/harupipipipi/tobkiri/actions/runs/43572029432)では、
  固定の元listingから確認全体を取り出すRust試験、実WebKitの操作、元Launcherのワーカーを検証する。
- 既存Hostの起動は `pack_control_v4.py` のAuthority DBと `profile_definition_store_v4.py` の
  ProfileレジストリをSetup表示前に初期化する。どちらも不在という条件では既存の初回Setupを
  接続できない。現ワーカーの旧復旧経路はジャーナル削除、ディレクトリとロックの作成・chmod、
  SQLiteのWALオープンを含む。既存Hostと並行で候補を生成するなら、開始前の不在確認だけでなく、
  既存の初回レジストリとAuthorityを扱う読み取り専用の入口が必要。利用者の確定操作後は元の順序で
  publication、workspace activation、Host active、definitionのロックを取り再検証する。
  開発Host引き継ぎの排他ファイルはflockと独立した作成・削除型なので、同じファイル名へのflockでは代替できない。
- [通常版43564055821](https://github.com/harupipipipi/tobkiri/actions/runs/43564055821)は
  両ジョブが通過し、macOSの実Wasm初期化、Profile IDとroot getter、Coreへの所有者引き継ぎを確認した。
  `5e4015e29` は [macOSの厳格lint](https://github.com/harupipipipi/tobkiri/actions/runs/43563472645)
  が指摘した決定値のサイズ差3件に限定したClippyの例外を付けた。既存の予算外に
  Boxを確保しないため、インライン表現を維持する。
  [再実行43565562034](https://github.com/harupipipipi/tobkiri/actions/runs/43565562034)はLinux・macOSの両方で通過した。
- `d58a6f638` はWebKit試験内の繰り返し評価を別スコープにし、同じダイジェストを
  持つ親候補への入れ替えを実AppKitのキー操作で確認する。
  [実WebKit CI 43572515915](https://github.com/harupipipipi/tobkiri/actions/runs/43572515915)では
  この確認が通過した。続く元Launcherのワーカー試験は単回入力の `clone` でコンパイルが
  停止し、`81a146759` で試験のWasm作成時に入力を借り、ワーカーへの受付時に一度だけ消費する。
- `6a480c2f5` は承認後の新規Setup候補を、元の公開ロックを保持した状態で再解決する。
  引き継ぎをロック前に確認し、ロック下で復旧状態と候補選択・resolverの保存先を照合する。
  一致時はロックとディレクトリの同一性を確認し、元のワーカー終了処理で実FDを閉じる。
  ロック以外のFD・接続の残留やclose失敗は従来のcleanup検査で扱う。
  [元Launcher CI 43573161607](https://github.com/harupipipipi/tobkiri/actions/runs/43573161607)は通過した。
  [ロック比較 CI 43573161659](https://github.com/harupipipipi/tobkiri/actions/runs/43573161659)の
  Ubuntuは拒否理由が診断の `cause` に入ることを試験が見落としていた。`a7b2aa4ef` で修正し、
  [レジストリ CI 43574843849](https://github.com/harupipipipi/tobkiri/actions/runs/43574843849)で
  新規候補の未変更・差し替えの回帰がLinux・macOSで通過した。
  次は同じStoreでHostカタログを準備し、配布カタログのProfileを取り直した後に物理lockを再読込する。
  `CatalogIo` のloaderを再利用し、参照の受付を各実呼び出しに結び付ける。各読込の順序と
  保存直前の同一性を比較できてから、試験用の別入口を整理する。製品Setupからの開始、
  active Profileの再確認と、元のジャーナルへの書き込みは残る。
- `6b43986ce` と `6dfcc2bed` は元の定義ロックを取り直し、保存済みの多言語名から
  対象locale、言語、日英、旧nameの順で選び、元の言語マップと既存版を保持した後継版を追加する。
  全件を確認してから時計を一度だけ読み、ハッシュを更新したindexを一回の書き換えで保存する。
  [レジストリ CI 43574843849](https://github.com/harupipipipi/tobkiri/actions/runs/43574843849)は
  両OSで履歴、再オープン時の重複生成なし、後の行が不正な場合の全体拒否を確認した。
  `ddd598e52` の[原本比較CI 43592124848](https://github.com/harupipipipi/tobkiri/actions/runs/43592124848)
  では、UbuntuとmacOSで固定Pythonと保存後の全バイトが一致し、再実行でも追加書き込みがなかった。
- `62e997141` は廃止済み4種類のPackを既存Profileの中立projectionへ移す。元の定義ロック内で
  runtime rootから実ファイルを読み、元の順序を保ってハッシュを照合する。各Profileの
  immutableな履歴、既存descriptor、移行receipt、元のbootstrap metadataを保持して一回だけ
  書き換える。後半の不正で全体を中断し、再実行では時刻の読取りも書込みも発生しない。
  UTF-8で表せない相対名は未対応。読取り中の交換に対してはdirectory descriptorの一致も
  確認する。[CI 43593582219](https://github.com/harupipipipi/tobkiri/actions/runs/43593582219)は
  UbuntuとmacOSで固定Pythonとの全バイト一致と再実行時の無変更を確認した。
  `7b2e4f01e` は元の順に別の定義ロックで状態を読み直し、既存Profileのある場合は一覧の
  手前、空のときはテンプレート検索の手前まで進む。
  [CI 43594182859](https://github.com/harupipipipi/tobkiri/actions/runs/43594182859)は両OSで成功。
  `3656f0aab` は同じRuntimeのgetter再呼出しと実catalogの添字参照、tombstone込みの照会、
  別ロックでの初版保存を接続した。取得したPackの検証済みテンプレートだけを既存の
  StrictV1上限付きDATAにし、取得先のKeyErrorを元の例外のまま拒否に結び付ける。
  実競合時だけ再読取りし、ロック・時計・保存の順番を比較する。変更したPython transport
  89件が成功。[レジストリ比較CI 43596397948](https://github.com/harupipipipi/tobkiri/actions/runs/43596397948)は
  Ubuntu・macOSで初回登録と再実行の原本比較に通過。
  [Wasm本体CI 43596417935](https://github.com/harupipipipi/tobkiri/actions/runs/43596417935)も
  ビルド、元Runtime初期化、getter、通常DATA入口からの専用lookup拒否に成功した。
  `e5b1f6523` は同じregistryを読み直し、元の並び順、tombstone除外、10キーの有無だけの
  判定で解決候補を作る。[CI 43596750559](https://github.com/harupipipipi/tobkiri/actions/runs/43596750559)で
  UbuntuとmacOSの両方で通過した。旧定義importと旧successor builderが残る。
  `5b455dff3`、`fe3c27c50`、`6d1cad954` は既存の復旧ロックが選んだ同じ
  ユーザーデータで、公式Pack一覧の読込み、外部レジストリのスキーマ、既存のHost HMAC、
  install journalの連鎖、最初の対象ルートのdevice/inodeまでを検査する。独立に署名・
  admissionしたPackとHMAC変更の原本比較は[CI 43597966368](https://github.com/harupipipipi/tobkiri/actions/runs/43597966368)。
  UbuntuとmacOSの両方で通過した。UTF-16/32のJSONと一部の複合値のPython文字列化は未対応。
  `3b35f3676` は最初のCASルートをdescriptorで再確認し、元の順序で実行カタログ、
  Packマニフェスト、Host Extensionと実行種別の識別を扱う。コンパイルとdigest照合の
  手前で止まる。署名付きファイル一覧と実ファイルの照合は、コンパイル後に接続するための
  比較テストに組み込んだ。[実署名付きPack比較CI 43599247113](https://github.com/harupipipipi/tobkiri/actions/runs/43599247113)でUbuntuは通過し、macOSで実行中。
  元の全スキーマ診断と一部の複合値は未移植。残るCAS照合をつないでからclosureを
  組み立て、同じRuntimeでProfile一覧を差し替える。
  `26079f919`、`576fb6a9e` は元のコンパイラの4文書を実ファイルから順に再読込し、Pack ID、
  source identity、canonicalなartifactと実行カタログのdigest、sidecarの生バイトのdigest、
  indexのsealまで照合する。4文書を共有したままWIT等の検証に必要な以後の項目を扱う。
  `433bb0e4f` は実装ファイルをdescriptorから分割読込みし、Function、Contract と
  Operationのスキーマを照合した後、署名済み一覧と内容digestを既存のHost受領証と照合する。
  [CI 43601961985](https://github.com/harupipipipi/tobkiri/actions/runs/43601961985) のUbuntu比較は通過。
  `4d69b6226` と `cd887369d` は通常Packの保存済み版を順に照合し、元の一覧revisionを計算し、
  未指定時の同梱ID衝突を拒否する。[CI 43602338697](https://github.com/harupipipipi/tobkiri/actions/runs/43602338697) の
  Ubuntuで同一鍵から別々に署名した2版との比較が通過。旧Host Extensionの互換性再投影は残る。
  `093012fee` は外部Pack追加が不要な復旧で、同じStoreの実catalogに保存済みのProfile一覧を渡し、
  AuthorityStoreの生成直前まで接続した。実App/Wasmとの照合を
  [CI 43603201335](https://github.com/harupipipipi/tobkiri/actions/runs/43603201335)で検証中。
  外部Packを使うProfileの解決とAuthorityによる復旧は残る。Operationをハッシュするときは
  親文書全体への予算を都度確保せず、同じcanonical writerが実際に走査する部分木を見積もる。
- 既存の `tobkiri_launcher/src-tauri/src/defaultspack_authority.rs` の
  `artifact_tree_digest` も同じ木をハッシュする。現状はファイル全体を読み、
  パス名をlossy UTF-8にしている。元のPack検証では順序付きの相対名を厳密にUTF-8化し、
  実FDから分割して読み、前後の同一性とcloseを確認する。まず元の動作を保つ有限の
  ファイル操作を接続する。両経路の呼び出し順・拒否・バイトが比較できたら、
  ファイル読み取りとハッシュの共通化で重複を減らせるか確認する。
- 将来の整理候補: `value_graph.py` の `_bootstrap_identity` とHostの `BootstrapIdentity*` は
  元の固定getterだけでなくcatalog・manifest・lookupも扱う。全固定呼出しの比較が通った後、
  wireのタグ、実参照、同じStoreでの保持を保ったまま名前と共有責務を揃える。

## PR #1521 のマージ前チェックリスト

- [#1525](https://github.com/harupipipipi/tobkiri/issues/1525) はレビュー時点の
  `4f17a0200` を対象にしている。#1521 の現在のheadは `8be3110a3` で、修正はその
  子孫の統合ブランチに積んでいる。契約・SDK・UIを別々にレビューできる単位で保持する。
  E-1は `soon` 向けの [Launcher表示 #1526](https://github.com/harupipipipi/tobkiri/pull/1526) と
  [Pack UI SDK #1527](https://github.com/harupipipipi/tobkiri/pull/1527) を切り出した。
  SDKのみで11件が通過。[WIT契約 #1530](https://github.com/harupipipipi/tobkiri/pull/1530) は
  基準WITを含むソースのドラフト。契約テストには現在 `tobkiri-{canon,gate,wit,manifest}` が必要。
  E-2は [Wasmtimeソース #1528](https://github.com/harupipipipi/tobkiri/pull/1528) と
  [旧検証記録 #1529](https://github.com/harupipipipi/tobkiri/pull/1529) を別のドラフトにした。
  Wasmtimeは公開された2アーカイブのハッシュ・ライセンス・Git treeと差分を確認し、
  旧検証記録は圧縮前後のハッシュが一致した。アプリ配布に使うCargo接続と、既存fixtureの分割は残る。
- B-2 と B-3 は構造化チャット2.0、ツール・コマンド一覧1.1を別の変更で定義した。
  既存のプロンプト、OpenAI互換プロバイダー、会話保存、Flow、UIは1.0のテキスト契約を使う。
  ツール呼出しと結果のID、reasoning、終了理由、キャッシュ使用量を同じ実ターンで照合してから、
  重複する変換を整理する。既存の保存済み会話の読込と承認・権限の照合を維持する。
  [契約 CI 43574843792](https://github.com/harupipipipi/tobkiri/actions/runs/43574843792)で
  4件が通過した。Pi では既存の `load()` がコマンド名だけ返すため、説明文を表示するには
  wrapperとadapterの両方で一覧操作を受け渡し、対応するWITをPackに登録する必要がある。
  `d27bc058c` と `7a4eb2ac1` はopt-inのPi wrapperとWasm adapterにツールおよび
  コマンド一覧を接続した。コマンド説明を登録から取り出すNode試験4件は成功。
  [従来のPi再生CI 43593497033](https://github.com/harupipipipi/tobkiri/actions/runs/43593497033)は
  ピン留めされた2拡張で成功した。新しい実Wasmの
  [CI 43593582212](https://github.com/harupipipipi/tobkiri/actions/runs/43593582212)はmacOSで
  同じhello拡張のツールとスキーマ、空のコマンド一覧、従来呼出しの照合に通過した。
  通常のHarnessターンとの接続は残る。
- B-4 / C-11 は、`de1f3a75e` でProfileにproviderの集合を明示できるようにし、
  `4f9fb7557` でFlowの `call.provider` を追加した。初回admissionと最終dispatchで
  Pack ID、ハッシュ、提供ビューを照合する。[契約 CI 43592612365](https://github.com/harupipipipi/tobkiri/actions/runs/43592612365)は
  非バインドの拒否と、明示時に最新の互換minorを選ぶコンパイラ試験に通過した。
  [実Wasm CI 43592612356](https://github.com/harupipipipi/tobkiri/actions/runs/43592612356)は
  2つのproviderと、未選択時にどちらも実行しないことを確認した。
  `089f5187f` は2つのPackの応答値を区別し、監査行の対象操作と実行回数も検証する。
  [追加検証CI 43593496962](https://github.com/harupipipipi/tobkiri/actions/runs/43593496962)も成功した。
  ゲストからの動的呼び出し先選択は残る。最高の互換minorの検索はCore、Wasm Host、明示Flowの
  コンパイラにあり、共通化前に省略時の固定済みwitnessと既存の選択結果を比較する。
  レビュー対象 `4f17a0200` の `harness-live.profile.json` はすべて単一Pack名の文字列を
  `bindings` に入れていた。Piアダプタも同時点では `pi-extension@1.0` の単一import。
  現在の集合とFlowの明示選択は確認済みだが、通常のHarnessで複数のPi拡張を選ぶ導線は必要。
- B-5 の `cebfae6de` はWasmのProviderとpromptから512トークンの固定上限を外した。
  Providerのtemperatureは既定0を保ち、0..2の値をAPIへ渡す。StrictV1のPack設定に小数は
  置けないため、`"0.4"` のような文字列も読み、APIには数値で送る。既存configは変更しない。
  base URLはoriginだけなら `/v1` を補い、明示されたAPI prefixを保持する。
  URLのcredentials/query/fragmentは受け付けず、Coreのorigin承認を引き続き使う。
  HTTPエラーは最大4096 bytesを読み、APIのmessage/type/code/paramを制限付きで表示する。
  認証エラーの本文は表示せず、既知の資格情報表現も伏せる。任意の秘密形式の検出は保証しない。
  Coreの `public_provider_error` とstream facadeはguestのエラー本文を固定文へ投影するため、
  詳細は既存chat-eventの `kind:error` で一度通知し、次のpullで失敗する。Coreと監査には
  本文を追加しない。UIはtextContentで詳細を表示し、失敗した会話を保存せず下書きを残す。
  元の成功時のdelta・usage・保存順序を保ち、HTTP 400/401と途中delta後のSSEエラーを
  実Wasm→承認済みテスト用Core→Flow→ローカルHTTPで検証した。network 2件、chat 4件、
  UI port 2件、実SDKのMessageChannelを通すUI 11件、guest 7件、strict Clippy、workflow
  22件が成功。component・UIの実ファイルとmanifest hash、既存権限、更新したProfileのpinも
  照合した。通常LauncherのGUI承認・会話検証や、tool/reasoning/finish/retryは未完了。
  旧 `domain/ai_client/providers/openai_provider.py` のHTTPError処理は57/167/206/664行付近で
  本文読取り・decode・整形を重複する。元のstatus/SSL/timeout/secret方針とGUI表示の
  一致を確認した後、共通のエラー整形へ移せる。既存Providerの削除は置換の製品検証後に行う。
  [実チャットCI44130220413](https://github.com/harupipipipi/tobkiri/actions/runs/44130220413)も成功し、
  同じrunでビルドしたPackのchat 4件・network 2件・UI port 2件・manifest 2件と、
  通常CLIによるProfile作成・実行・公開・拒否・再取込み・User overlayの再生を通過した。
  [実1B CI44130220058](https://github.com/harupipipipi/tobkiri/actions/runs/44130220058)は、固定した
  llama.cpp b11146とGemma 3 1B Q4_K_Mを使い、認証付きloopback APIへの実Wasm入力・Flow・
  stream・保存・cancelの1件を73.35秒で通過した。元Launcher GUI全体の合格を表すものではない。
- B-8 の `262108ccc` はHarness prompt Packだけで履歴のUTF-8サイズを制限する。既定2,048
  バイトを超えた分は古いターン単位で省き、システム文と新しい入力、モデルと出力上限は
  そのままProviderへ渡す。保存済み会話や画面の履歴を削除しない。既存設定の項目省略も同じ
  既定値で動く。Packと5つの開発Profileを実Wasmのdigestへ更新した。Packテスト5件、実Wasmの
  Flowとloopback APIの2件がローカルで通過。保存された2行が残った上で長い前ターンだけが
  次回リクエストから消えることを確認した。ストアの最大1,000行の既存読取りは今回の範囲外。
  [実1B 44259160901](https://github.com/harupipipipi/tobkiri/actions/runs/44259160901) は
  更新後のPackで70.69秒の一連の推論と保存を通過。
  [実Flow・UI 44259160857](https://github.com/harupipipipi/tobkiri/actions/runs/44259160857) の
  Flow・ネットワーク・UI・マニフェストも通過。最後の別Pack構成のEcho CLIは最初の保存が
  `unavailable` で停止した。同じEchoのCLI作成・実行・公開・拒否・再取込み・User overlayは
  ローカルで通過。原因は未確定。`d36f7ef3f` はCIで再発時に既存の監査DBから保存に関係する
  許可・実行結果だけを読み、状態コードを出力する。限定出力はローカルの生成fixtureで確認し、
  [診断付きの再試験 44261610869](https://github.com/harupipipipi/tobkiri/actions/runs/44261610869)
  はEchoの保存・公開・再取込み・User overlayまで通過。最初の失敗は再現せず、原因は未特定。
  [44368388797](https://github.com/harupipipipi/tobkiri/actions/runs/44368388797) では通常Profileの
  実行・公開・再取込みは通過し、User overlayの初回ターンで元の `load` が `unavailable` で停止した。
  保存時の既存監査レポートを全ターンに共用し、`load` と `append` を同じ上限付き抽出の対象に追加した。
  stderrもCIの実行記録に保存する。ローカルのSQLite fixtureで両方の呼び出しが残り、
  対象外イベントや任意のエラー文が出ないことを確認。既存の試験条件で再計測する。
  [44371423977](https://github.com/harupipipipi/tobkiri/actions/runs/44371423977) のimport後の
  overlayターンでは、初期化中のDDLが成功し、`load` が同じDDLを再実行した時だけ
  `storage-sql-failed` が記録された。先行のread grantのcancelはDDLの書込み分類によるもの。
  EchoとHarnessの会話ストアでテーブル作成をライフサイクルの `init` に集約。
  ホストは `init` 成功後にのみハンドラーを実行する。以降の `load` は既存のSELECTのみ、
  `append` は2行をまとめる既存のINSERTのみを行う。2つのWasmと関連Profile 14件を再生成。
  実Wasmの通常Flow、Core再起動後の履歴、承認したFlow変更を含むローカル4件が通過。
  ローカル実CLIもProfile作成・公開・拒否・新規取込みと、User overlayの取込み・実行・再公開まで通過。
  [実CLI再試験44375207375](https://github.com/harupipipipi/tobkiri/actions/runs/44375207375) と
  [実1B再試験44375207327](https://github.com/harupipipipi/tobkiri/actions/runs/44375207327) を実行中。
  SQL書込み全体の競合原因はこの監査コードだけでは特定できず、承認・commit fenceと再試行の
  条件は維持する。
  [44618237213](https://github.com/harupipipipi/tobkiri/actions/runs/44618237213) では
  通常の公開再生とUser overlayの公開元のターンが通過し、承認して取り込んだProfileでの
  最初のターンがストアの初期化中に失敗した。初回の `load` はハンドラーの前に
  Wasmの `init` を呼ぶ。監査は初期化DDLの書込み許可が出た後の失敗を示し、SQLiteの
  実行エラーとProfileの承認DBのcommit fenceによる拒否は同じコードに集約されていた。
  次回は、SQL本体・引数・エラー本文を含まない分類と、そのPackの `init` の結果も採取する。
  Flow overlayと型付きツールは独立したProfileで検証するため、公開再生の失敗時もCLIの
  ビルドが成功していれば実行する。各試験の失敗は引き続きCIの失敗になる。
  [44621810345](https://github.com/harupipipipi/tobkiri/actions/runs/44621810345) は
  通常の公開とUser overlay、型付きWasmツールが通過。独立したFlow overlayでは更新・公開と
  取込みまで進み、取込み直後の最初の `load` が `internal` で止まった。この試験にも
  取込み先の会話ストアのpinとポートに合わせて、同じ限定監査を出すようにした。
  [再試験44625908711](https://github.com/harupipipipi/tobkiri/actions/runs/44625908711) は
  通常の公開とUser overlay、独立したFlow overlayの再取込み後のターンまで通過した。
  型付きツール側で共通の `turn` に旧引数を渡していた2か所を、監査で使うPackのpinを含む
  `ProfileFixture` を渡すように更新した。ストア初期化の前回の失敗はこの実行では再現しなかった。
  [修正後の実CLI 44629343416](https://github.com/harupipipipi/tobkiri/actions/runs/44629343416) は
  `52588aba6` で成功。Profile公開・再取込み、Flow Overlay、型付きWasmツールが通過した。
- A-1 / A-2 は、直接呼び出しのチャットが `ChatRunEngine`、別のエージェントAPIと承認後の再開が
  `AgentEngine`、通常の保存済みデスクトップ会話が `conversation.saved-turn.v1/saved_complete`
  を使うことを確認した。旧 `blocks/chat/send.py` の旧ループは比較・テスト用。
  A-2 では `domain/agent/loop_engine.py` を削除した。静的参照、公開エクスポート、
  ハンドラー登録、Packマニフェスト、固定ソース目録に対象への参照はなかった。デスクトップの
  リソース目録とsealed環境は、配布時にソースから生成される。旧ループだけが使っていた
  `domain/agent/context_builder.py` は別の削除候補。配布前のファイル目録と、固定入力からの
  目録検証が通過した。共通fixtureを省いたローカル試験79件と単独のストリーム試験3件が
  通過した。プロセス終了後の復旧を扱う2件はCIでの再確認が必要。
- A-6 は、現行のモデルへの使用量変換がキャッシュトークンを落とすことを確認した。
  毎回のツール選択でプロンプトの早い位置が変わり得る。キャッシュヒット率への影響は未計測。
  A-7 は、元の `tobkiri_protocol/saved_messages.py` の分岐、ツール結果IDと画像の上限を
  Rust/Wasmで比較した後、保存履歴から切り離したモデル入力に圧縮を適用する。
  承認を要する保存済み履歴の圧縮と、送信する入力に結び付いたdigestは維持する。
- D-2 は `78c549e62` で、Launcher更新欄と日英の説明の表示をHarnessに揃えた。
  更新APIに送る既存の `defaultspack` キーは互換性のため引き続き使う。
- D-3 のShell起動画面を `13bb521e6` で日英対応にした。通常の起動操作を
  [Launcher CI 43598692796](https://github.com/harupipipipi/tobkiri/actions/runs/43598692796)で確認し、
  関連49件と実AppKit/WebKitが通過した。登録済みモデル一覧の表示は `7c2b4a962` で日英対応。
  共通ヘルパーと他画面の直書き文言は残る。
- C-13 は `a5c6c9eca` で設定ラベルのワイルドカードを、登録済みセクションと項目の
  完全一致に変更した。既存の日本語訳と別セクションの同名フィールドを含む26件が成功。
- C-7 は、イベントだけが呼び出し元の `has-ui` を受け、ツールとコマンドには
  実画面の状態が届かない。`5bcc4f786` に仕様と試験を記録し、
  [実Pi CI 43574843811](https://github.com/harupipipipi/tobkiri/actions/runs/43574843811)で
  現状の動作を確認した。実画面の状態を渡す接続は未実装。
- C-2 は `99cfd5f65` で、承認されたUI assetと可変なユーザーデータの境界、
  廃止された動的Pythonツールのエラーと移行先をドキュメントに反映した。
  `3c758088b` では、同READMEの旧フォルダ一覧からも配布物の配置先としての説明を修正した。
- C-1 は `e53bfb384` で `packs/README.md` に新Pack、Pi、MCP、プロンプト、旧v4の開始点を整理した。
- A-11 は `cfb010379` で両プロンプトの名乗りをTobkiriにした。既存の本文の残りは維持する。
- C-4 は `844ec6f08` で `tobkiri pack manifest` を追加した。明示した成果物だけを読み、
  ハッシュとサイズ、既存の型に基づく権限一覧を生成する。macOS/Linuxの既存ディレクトリ
  読取りを使う。実際のCLIで生成、ビルド、検証する[CI 43599247031](https://github.com/harupipipipi/tobkiri/actions/runs/43599247031)を確認中。
- C-15、C-16、D-6の一部は `77125f304`。Pack UIは親からの接続待ちを10秒で
  終え、接続失敗を表示する。最初の応答を待つpullは最大300秒とし、Coreが元の
  deadlineとキャンセルを適用する。停止・失敗時は未保存の行を外して下書きを維持する。
  SDK、Harness UI、native source、Pi inventoryのローカル試験37件が成功。
  C-14の同じSDKコピーは、汎用SDK、Harness Pack、native試験Packの3か所にある。
  同じmanifestの自己完結と、既存のnative検証に使うbytes一致を保てる生成範囲を
  決めてから追跡済みコピーを削る。固定会話IDの置換は保存済み履歴の互換と実1B
  検証の会話IDを一緒に扱う。
- D-4のSetup確認は `271334a63` と `15181e0d1` で、確認手順と権限の説明を
  画面上の利用者向けの文言へ変更した。検証済みの手順順序、各digestと権限状態の値、
  確認要素と操作は維持する。
  [Launcher CI 43594705258](https://github.com/harupipipipi/tobkiri/actions/runs/43594705258)で
  既存のSetup表示と実WebKitが成功。
- C-10は `5048a0a8f`。自作のPiソースを `--local-source` で受け付け、入力バイトの
  ハッシュとローカル来歴を保存する。従来のGitHub固定入力も利用できる。
  componentize-jsを限定環境の別プロセスで実行し、ライブラリ呼出し元の環境を保持する。
  [CI 43596134776](https://github.com/harupipipipi/tobkiri/actions/runs/43596134776)のオフライン
  CLI・ライブラリ実コンパイルが成功。従来2拡張と1.1在庫の同じ実行をCIで再確認中。

- D-5 は `69fe1c9dd` で、Launcherの登録とHarnessの「普段使うモデル」の
  適用範囲を日英の表示に追加した。[表示 CI 43604721250](https://github.com/harupipipipi/tobkiri/actions/runs/43604721250)が、画面試験とmacOSのAppKit/WebKit試験で成功。
  B-6 の `71e7f56d1` は、Harness promptの既定設定からモデル名の複製を外した。Wasmの
  OpenAI互換Providerは、同じFlowで選ばれたProvider自身の承認済みモデルをリクエストに使う。
  旧Profileが明示したモデルは引き続きProviderで照合する。空文字、null、数値の指定は拒否する。
  Promptの6件と、既定値と異なるProviderモデルで実Wasmからloopback APIまで通す試験を含む
  networkの2件がローカルで通過。実Wasmと5つのProfileのpinを照合した。Launcher側の既存の
  モデル登録はそのWasm設定への接続が残る。
  [更新後の実Flow・UI・CLI 44266173501](https://github.com/harupipipipi/tobkiri/actions/runs/44266173501)
  は通過。[実1B 44266173496](https://github.com/harupipipipi/tobkiri/actions/runs/44266173496)
  の初回は推論と保存、次ターンのCoreキャンセルと保存抑止まで進んだが、llama.cppの
  `/slots` が3秒以内に待機状態を示さなかった。サーバー、モデル、リクエストのモデル名、
  キャンセル実装、試験の上限は前回の成功後も同じ。
  [同じSHAでの2回目](https://github.com/harupipipipi/tobkiri/actions/runs/44266173496/attempts/2)
  は71.04秒で停止と保存の試験を通過し、前回の失敗は再現しなかった。原因は未特定。
  接続先・Secret・モデルを獲得する箇所を一つの実Wasmターンに接続し、既存保存値と照合する。
  `frontend/src/lib/launcherConnections.ts::saveLauncherModelProfile` の既存入力は
  model_profile_id/model_id/provider_instance_idとRegistry revisionを送る。
  `local.provider-registry-rust` は、同じRegistryをWasmからCoreのPack専用ストアへつないだ。
  Coreが実行中のProfileを決め、SQLiteで保存済み文書のハッシュを照合してから更新する。
  応答がCoreのJSON形式で表現できることを検査した上で書き込む。保存時のSQLテキスト引数は
  ハッシュ込みで1MiBまでで、実際の配送には呼び出し側の出力上限も適用される。ソースから
  ビルドした実Wasmを使い、別Profileへの書き込み拒否、同じ接続IDの分離、古いrevisionの
  拒否、UI向け読み取りでのendpointとcredential handleの除外、Core再起動後の復元を確認した。
  Python原本との比較に使う4ファイルがLauncher変更で進んだため、採取時の `98bb10ab6` から
  同じバイト列を取得し、受領済みのサイズとSHA-256を照合する。両比較の固定観測は更新していない。
  Rustの単体52件、採取済みPython観測との比較81件、実WasmのCore統合試験、strict Clippyが通過。
  このPackには旧Registryからの取込みとCredential Broker接続が残る。また、モデル登録と既存の
  「普段使うモデル」を同じ会話ターンから読む経路が必要。設定を接続する際は従来の承認を使う。
  モデル登録の読み取りは `local.model-registry-rust` に移植した。元のModel Registryの
  一覧・完全一致・一段のalias解決と、既存の `/api/ai/profiles` が使う表示変換を共用する。
  旧文書の浮動小数点・大きな整数はPack専用DATAの内側に保持し、表示用に絞った結果を
  Core JSONへ変換する。既存の出力上限と登録revisionを照合し、無効なモデルと非公開項目は
  元の表示と同じように除外する。原本の16ファイルを固定し、25ケースで原本と比較した。
  実Wasmでは、採取した文書をテストのCore専用ストアへ格納して、一覧の一致、空のProfile、
  別Profileの文書の拒否を確認した。旧文書の自動取込み、既存HTTPの接続、保存と会話への
  接続が残る。旧版が作れた文字列以外の辞書キーとCore JSONで表せない表示revisionは
  現時点の表現上限に当たる。ソース単体10件、Coreの実Wasm試験2件、strict Clippyが通過。
  同じ読み取りと表示変換を、旧 `model_registry.store.json` の実ファイルからも呼び出せるようにした。
  Coreは指定Profileの固定パスをdescriptorで読み取る。欠損時は空一覧を返し、読み取り時に
  ディレクトリやデータを作らない。既存の保存先を読むため、旧Hostの原子的な保存も次の読取りに
  反映できる。Wasmには最大1 MiBの文書と固定Profileを渡し、ストレージ・ネットワーク・別ポートは
  与えない。ProviderとModelの固定WITの照合、実行予算、Wasm Storeの終了を共用する。
  物理読取りは既存の `NativeDataIo` スロットに載せ、期限切れや取消時の応答から、元のworkerの
  終了とソースの解放を独立して確認する。
  通常の一覧入口は署名付き `GET /api/ai/profiles` の `defaults.models.profiles.list`。同じownerを
  呼ぶモデル検索やSettings内部の呼出しは非公開のメタデータを含む別の応答を使う。通常GETの
  接続では既存Shell、Profile grantとlease、Launcherの起動世代を照合し、表示変換を一度だけ行う。
  現行のordered-finite codecでは重複キー、非有限数、単独のサロゲートを含む旧文書は扱えない。
  通常GETへ接続する前に、既存Brokerで受理される範囲と、読み取り失敗時の互換動作を照合する。
  Store 148件、Model Pack 28件、strict Clippy、ビルドして検証済み `.tpk` を使うModel E2E 3件と
  Provider E2Eが通過。旧ファイルの有無・Profileの不一致・上限と原子的な更新を含めて照合した。
  既存のProvider実通信の13条件も通過。通常画面の実経路は署名付きHTTPとの接続後に検証する。
  `9560234d9` に続き、通常のモデル一覧を同じProfileの旧ファイルを読むWasmへ接続した。
  Launcherは元のShellの直接の `profile_grant` をProfile・Plan・Lockと署名済みBundleで照合する。
  ProviderとModelの許可とreceiptを別々に保持し、両方が使える場合も同梱3アーカイブの照合は一回。
  認証済みソケットでは同じ起動世代が持つ該当操作の証拠だけを受け付ける。元のHTTPセッション、
  Brokerのlease・予約・期限を確認し、workerとStoreの終了後に同じ画面へ一覧を返す。
  Rust開始後は、書式・サイズ上限で互換読取りが必要な場合に限り、元のleaseを再確認して旧ownerに戻る。
  無効な文書、Profileや証拠の不一致と取消を、互換読取りに切り替える理由にしない。
  `model_settings_presenter_input` はWasmの公開行だけを元の署名済み表示処理が受け取れる形式に戻す。
  通常表示の契約とHTTPセッションをNative側が直接持てる段階で、この変換と同期ソケット借用を
  削除できる。モデル検索と内部Settingsの生データ参照、保存、会話の解決はそれぞれ移行を残す。
  署名済みHTTPとソケット試験39件、Model Pack 29件、receipt 10件、Harnessの選択5件と
  原本Setupの発行試験1件、strict Clippyが通過。macOSの分離実通信はProviderとModelの20条件。
  Tauri登録とresolverはfixtureを使い、署名済みHTTP試験ではソケットをfixtureへ置き換えた。
  実アプリの両方の起動証拠と、インストール後の通し検証は同じDMGで確認する。
  モデル登録の保存では、現在のProfileで実行中のModel Registryが、既存のProvider Registry
  の読み取りポートを呼ぶ。ProviderとModelのrevisionを照合し、無効な接続への登録を拒否する。
  SQLiteは読み取った文書のハッシュを条件にして保存する。既存ルートの再確認は書き込まず、
  出力上限だけの変更では旧パラメーターと非公開のハンドルを維持する。競合する更新は一件だけ成功する。
  Providerと共通だったUTC変換と整数リテラルの上限判定を共有した。
  Python原本の保存7ケースと32ケースの読み書き比較、Rust 21件と実CoreのWasm試験3件が通過。
  元の画面とHTTPへの接続、選択したモデルの会話への接続は作業中。
  既存Pythonは書込み後に表示へ変換するが、Rustは表現できない応答で書込みが確定しないよう
  先に表示を検査する。現状では上限外のJSON数値を含む異常な旧文書にこの差が出る。
  最初の保存移植は `b6b2d2bb8`。現在は元のModel Registryファイルをその場で更新するRust＋Wasmの
  保存処理もある。元の入れ子のProvider読取りに由来するスナップショットでrevisionと有効な接続を
  照合してから、Pythonと同じ `model-registry.lock` で元のModel文書を取得する。既存ルートの
  再確認や上限の変更がない場合はファイルも時刻も更新しない。Wasmは旧形式の保存候補、公開する
  画面の結果、既存操作ジャーナル用の結果を組み立てる。ジャーナル用の非公開項目とJSON数値は
  文字列のままRustに渡し、画面に返す結果には含めない。Rustは同じロックの下で元のバイト列と
  直前の権限を確認し、Wasmの終了後に原子的に置き換える。Providerと共通の置換処理を使う。
  通常の保存HTTPに接続するには、署名済みModel ManageとそのProvider読取りの起動証拠、
  元のHTTP操作ジャーナル、置換確定後の切断時に再実行させない処理を結び付ける。
  Model Pack 34件、実Core＋WasmのModel 3件とProvider 1件、Store 150件、JSON互換19件が通過。
  元のPython保存データ7件の再生成も固定証拠と一致した。実Core試験はProvider応答とBrokerの
  権限チェックをfixtureで供給し、実際に旧パスへ作成・更新してファイルと記録の内容を確認した。
  起動済みのv5 Profileから、Shell→Model ManageとModel Manage→Provider Registry読取りを
  一組として選定する処理も追加した。両方の権限、スコープ、実行バリアントが揃った場合だけ
  対応するpinを返す。Model一覧とShell自身のProvider読取りに与えられた別の権限を流用しない。
  既存のProvider準備と選定処理を共有した。設定経路の選定8件と、旧Setupの原本から
  発行されたProfileの両方のスコープを照合する1件が通過。
  Launcherは起動時に保存の両経路を元のBundleのPackと実行カタログへ照合し、同梱Model
  Packのreceipt、必須ポート、Wasm宣言を確認して起動世代に結び付ける。Model一覧の証拠を
  別に保持する。同じModelアーカイブについて一覧と保存の両方を選んだ場合も、同梱アーカイブの
  読み取りとPack検証は一回。旧BundleのModelとProviderのカタログは、各権限の照合ごとに
  読み直す。計約63 KiBの追加読み取りで、今後まとめる場合も個々のpinの照合と停止点を保つ。
  元のBundleを使った分離試験10件とWasmを実行するモデル試験3件が通過した。分離試験で
  現在の同梱Packは両方のポート、旧版の同梱Packは一覧のポートだけを持つことも確認した。
  通常の署名済み `POST /api/ai/profiles` を保存処理へ接続した。Brokerが許可したModel
  Manageから、元と同じ資格情報なしのProvider読取りを実行する。応答を接続ID・有効状態・
  revisionに絞り、重複を検出できるよう対象の有効な行を最大2件渡す。両方の署名済み経路の
  effect classは旧カタログどおり `privileged` として照合する。
  LauncherはWasm終了後、ファイル置換直前に同じ接続で一度だけ最終許可を受ける。許可を
  送り得る状態になった後の切断・期限切れは、Broker監査と既存ジャーナルの不確定結果へ
  結び付ける。変更のない保存には最終許可が不要。同じHTTP要求IDの再送は元の履歴を返す。
  結果のJSONは旧ownerの数値表現を保つため現在は通信層で別に検証する。ネイティブ側との
  結果契約を統一する際は有限の浮動小数点、整数の範囲、旧ジャーナルとの互換を確認する。
  通信50件、Broker等の回帰110件、署名済みHTTPの4件が通過。HTTP試験のLauncher応答は
  fixtureで供給する。分離試験では実際のLauncher worker、Wasm、旧Registryファイルを使い、
  許可拒否、新規保存、変更なしを確認した。DMGでの通し検証が残る。
  キー不要のローカル会話では、既存のOpenAI SSE処理から、選択された
  保存モデルのIDとProvider接続を実行中のProfileで解決する。選択先のloopback URLと
  モデル名を取り出し、保存した出力上限を適用する。送信先の許可は既存のCore Gateが判定する。
  既存 `messages_generate.py` と Gateway は同じモデル参照を二度解決する経路がある。
  この経路では一度の解決にまとめ、旧モデルの非公開メタデータをDATA側に保持する。
  同じ承認済みUIセッションから保存済み2件を選び、2つのローカルAPIへの実リクエストと
  SSE応答を照合した。既存の会話画面のモデル選択がこの経路を使うための接続と、Hosted認証と
  モデル要件の移植が残る。キー不要モードで扱えない設定は送信前に拒否する。
  `20ed649b4` の経路に会話の保存モデルをつないだ。原本 `ConversationStore.create` と
  `update` を固定時計で実行して得た会話文書を試験用のPack専用DATAに格納する。会話IDと
  revisionを渡すと、実行中のProfileに属する文書から元のモデル参照を返す。古いrevisionと
  リクエスト内のモデル上書きを拒否し、2件の登録で別々のloopback APIへの送信とSSEを確認した。
  この専用DATA経路は1MiBに収まる値を読む。元の会話ファイルを使う通常の読み取りは
  下記の経路につなぎ、親コンテキストの解決、保存ターンの状態管理とレシートは元の処理を使う。
  通常の `GET /api/chat/conversation` と、元のSaved Turn、Saved Orchestrationからの
  会話 `get` は、同じBroker認可を使って、元のProfileの `conversations.json` をRustから
  読み込む。起動時と呼出時に3つの署名済み権限を個別に照合する。Wasmが返す完全な会話行には
  モデル参照、メッセージ、親参照とコンテキスト項目を保持する。旧ownerと同じ順序番号と
  メタデータの整形を適用し、既存の親コンテキスト検証とターンの状態管理につなぐ。
  読取ではディレクトリもロックも作成しない。元のファイル、結果、Coreのメモリ消費を
  それぞれ既存の上限で制限する。旧JSONの特殊な表現や1MiBを超える文書には、
  現在の権限と期限が確認できる場合に元の読取り経路を使う。
  旧 `ConversationStore._read` は各回で全会話をコピーし、対象の行をさらにコピーする。
  Native GETでは文書の所有者を検証した上で対象の行を整形する。旧経路を除去する際は、
  深いメタデータ、旧Unicode変換、子と親の読取れ方、一覧とレシートの現行結果を確認する。
  `saved_receipt` でも全会話をコピーしている。保存ターンと照合の両方で元の同じファイルから
  レシートだけを取得できる。各callerの権限と親リース、欠落時のnull、完全なレシート、
  Turn側の検証と確定処理を維持してから従来の読取を除去する。
  Rustは同じ物理読取を使い、専用の `legacy-receipt.get` を実Wasmで実行する。元のPython応答と
  UTF-8バイト単位で照合し、桁の大きな整数、負のゼロ、追加項目を維持した。ファイルの欠落と
  対象外の行はnull。空mapは空mapを返し、壊れたレシート一覧では元のエラー順序を保つため
  互換経路を要求する。実ファイルとロックの非変更を含むCore試験5件、Pack 15件、strict Clippyが
  通過した。署名済みの送信再試行と照合は、それぞれ元のShell親リースと呼出し元の権限を
  照合して専用ポートを使う。既存のGETと独立した起動証明を持ち、同じ署名済みPackと
  カタログの物理読取とアーカイブ検証を共用する。Turn側の検証と確定処理は元のownerで行う。
  分離した実macOSソケットの試験52件で、旧ファイルを読む実Wasm、権限の流用拒否、
  null・空map・不正な一覧の処理とファイル非変更を確認した。Tauri管理状態はfixtureで供給し、
  元のSetupの発行レコードと一致するBundleを別の試験で照合した。別世代のBundleは拒否した。
  署名済み選択14件、原本Setup 1件、CPython通信128件も通過した。
  署名済みHTTPの4件では元のBrokerと実際のSQLite・会話ファイルに、Launcher応答のfixtureを
  組み合わせた。会話保存直後の応答喪失と同時停止を発生させ、古いrevisionの確定を拒否し、
  送信再試行で会話を重複保存せず復旧することを確認した。確定済みの結果への停止、
  互換読取りと予算不足時の読取りも通過した。別の実行で既存の通常GETも通過した。
  Wasmの不正な文書とIDは共通のinvalidとして扱う。通常のエラー経路でレシートを補わず、
  確定も行わない。古いPythonの個別エラー文言まで必要になれば固定のエラー契約を分ける。
  `DurableTurnRuntime.settle_saved_from_receipt` は同じSQLite行を `_record` と
  `_restore` で二度検証してから保存する。一行の観測と遷移を共用する際も、
  `BEGIN IMMEDIATE` 内の再照合、完了済み同一レシートの無書込再送、照合側の期待revision、
  停止要求と停止確定の扱い、guidanceとイベント順序を保持する。会話側の確認は別の書込境界。
  既存ファイルの更新はRustの移行ブリッジに追加した。ターン固有の判定はPack側に置き、
  既存の順序付きPython JSON互換処理を共用した。CoreのStoreは既存SQLiteを開く汎用処理を
  提供する。元の`turns.sqlite3`の一行だけを対象にし、新しいテーブルを作らない。
  最終権限照合はSQLiteが確定用のロックを得た後に行う。拒否時は変更をロールバックし、
  確定失敗の結果が不明な場合は元の行を再確認する。行・一時作業・返却値は別々に予算を確保する。
  macOSの物理SQLite試験9件で既存スキーマ、無変更の再送、独立した停止書込み、リーダーが
  確定を遅らせた間の権限失効、64MiB予算と、確定前後のSIGKILLからの復旧を確認した。
  実Python版とはrunning、waiting、未確定の停止とguidanceの3例で保存行をバイト照合した。
  Packの判定試験4件が通過。実Wasmをビルドし、WITとPackアーカイブを検証した。
  分離したWasmtimeでは48MiBのゲスト上限の下、更新案、時刻を取得しない再送、revision競合、
  別Profile拒否を確認した。Profileと時刻をテストで供給し、SQLite書込試験とは別に実施した。
  通常送信への接続には元のShell→Turnの権限と予約を使う。Conversationレシート読取の子リースと
  独立した確定用の境界、明示Reconcileの観測revision、結果喪失時の再観測が必要。
  Launcherの専用経路は両方のShell→Turnを個別に照合し、元の署名済みBundle内の
  privileged操作とWasmのWITを確認する。v2のCIレシートは4本すべてを照合する。
  インストール済みのv1は既存3本を検証し、保存ターン確定の権限は発行しない。
  汎用のWasm実行は既存の呼出しに一度だけ束縛する。CoreのProfile参照と上限3回の時刻取得を
  使い、WASIの標準ライブラリ用CLI・I/Oには呼出しを拒否する実装だけをつなぐ。
  読み取ったSQLite行の更新案を同じトランザクションで評価し、実ロック取得後の確定許可前に
  書込みを公表しない。正確な再送では確定許可を求めない。許可が届き得た後の応答異常は未確認
  として扱う。Python IPC 153件、Rustの権限選択16件、分離Launcher試験56件、
  DMG検証71件、実WasmとSQLiteの同一トランザクション試験が通過した。
  分離Launcherでは実PythonプロセスをUnixソケットにつなぎ、権限のない役割、古いrevision、
  許可中断、確定、無変更の再送を確認した。Tauri管理状態はfixtureで供給し、元の承認発行記録と
  元のBundleで両方のprivileged権限を別の試験で照合した。
  現行Turn Pack 1.0.3を生成し、通常送信・保存済みターンの再試行・明示Reconcileの
  確定箇所から専用経路を呼ぶ。元の保存結果検証が完了した後だけ、同じ送信の権限、期限、
  容量予約を引き継ぐ。呼出しは署名済みリクエストのターンIDに限る。確定許可を出す
  直前に元のenvelopeを記録し、応答が失われた場合は再照合に戻す。Conversationの確認は
  確定後に従来の別リースで行う。旧Turnの実体は保持版から読み込み、
  `42dfc6e0` と凍結済みの旧実体は現行のカタログと分けて検証した。
  配布用ソースの生成と照合5件、Defaults整合性検査、CPython 3.13.16のターン回帰88件が
  通過した。実署名付きHTTPの5ケースで、通常送信、再試行、明示Reconcile、
  互換確定、確定応答未確認を検証した。実SQLite・会話ストアを使い、外部Providerと
  Launcher応答をfixtureで供給した。実Rust＋Wasmの確定検証は上記の分離Launcherで行った。
  既存の署名付きレシート復旧とモデル設定の回帰2件、およびHostの確定応答・監査境界11件も通過した。
  `ConversationStore.confirm_saved_completion` の判定を同じConversation Packの専用Wasmポートに
  移植した。Turnの永続確定と分け、元の `locks/conversations.lock` 取得後の観測だけを
  同じCore invocationのWasmに渡す。確定済みの再送と失効した候補は無書込。
  保存時は旧ownerと同じ全行のメタデータと順序番号の整形、元のUTF-8 JSONの区切りとキー順、
  追記時刻と会話revisionを保持する。成功時に増えるのはstore revisionのみ。
  現在の互換読取りと書込みで共用する整形とJSONコードを二重に移植しない。
  互換経路を除去する前に、旧サロゲートや有限でない数値を含む文書、1MiBを超える保存値の
  扱いと、再試行における永続結果の再観測を検証する。非失効の旧NamedLockが残る異常終了は
  SQLiteの強制終了確認とは別の問題。既存ロックを強制解除しない。
  CPython 3.13.16の実会話・SQLite Turn ownerから生成したfixtureと、保存バイトまで照合した。
  Pack・共有codecの38件、元の物理Storeの4件、Wasm応答契約の1件、実Pack・Coreと実ファイルの
  2件が通過した。実ファイルの試験では公開権限失効時のinode維持、別invocationの拒否、
  承認後の置換と無書込の再送を照合し、Registry Pack CIにも追加した。
  現行Conversation Store Pack 1.0.2では、既存Completion Hostが資格情報を渡さないTurn Resource
  のGETから得た証跡を専用Wasmに渡す。ShellからSavedまたはReconcile、独立したCompletion、
  そのCompletionからTurn Resourceの元の署名済み権限を照合する。実際のCompletion子リース、
  期限と予約を使い、元のロック内で最終許可を得る。確定結果が不明な場合は既存の再照合へ戻る。
  古いWasmに専用ポートがない場合や対応外の保存形式は、公開前の互換経路を使う。
  Calendarは既存の追加認証とowner経路を保持し、専用ポートを呼ばないことを試験した。
  配布用生成物5件と厳格なDefaults整合性検査、元のSetup承認記録からの権限照合1件が通過した。
  CPython 3.13.16の署名付きHTTP 4件は、実SQLiteと会話ファイルにLauncherと外部Providerの
  fixtureを組み合わせ、通常送信、互換経路、書込み前後の応答喪失と復旧を検証した。
  IPC、既存完了確認、Calendar、Brokerの公開結果を含む回帰299件も通過した。
  分離macOS Launcherの66件では実Pythonプロセス、Unixソケット、実Wasm、元の会話ファイルを
  使い、異なるcallerと不一致レシートの拒否、最終許可中断、置換と無書込の再送を確認した。
  分離試験のTauri外側状態はfixture。DMGのインストールからSetup、承認、会話までの検証は後続。
  既存会話の追記・候補レシートを同じ物理ロックを使うRust＋Wasmに移植した。
  一回の書込案にメッセージ、親の子ID、順序、revision、追記時刻、レシートと候補を含める。
  親会話のrevisionと内容、継承するツール・実行設定は同じロックのsnapshotで再照合する。
  保存済み入力のID、画像・参照・ツール履歴、元の正規化メッセージのdigestを検証する。
  完了確認と追記は元のJSONエンコードと完了状態判定を共用する。順序付き集合を使い、
  このWasmにWASI乱数権限を貸さない。時刻は追記呼出しにだけ上限3回で供給する。
  共通のnative clockを整理する場合も、追記だけが呼べる状態と上限、現行の64MiB計上を保持する。
  CPython 3.13.16の実ownerから通常と親つきのユーザー・アシスタント4追記を採取し、
  返却値、レシート、保存バイトを照合した。画像・参照・ツールを含む別のreceipt証跡も一致。
  Pack・共有codec 56件、Wasm応答2件、Core＋同梱Wasmと元のファイル3件が通過した。
  物理試験では最終許可が拒否された書込み、古いrevisionの再送、編集済みの親を使う追記が
  元のファイルを変更しないことを確認した。macOS上のPackビルド、厳格Clippyも通過。
  Conversation Store Pack 1.0.3では通常送信のMessage Manage Hostをこの追記へ接続した。
  ShellからSaved Turn、Saved Orchestration、Message Manageまで元の3つの署名済み権限と
  現行の呼出しを照合する。メッセージ追記の子リース・予約・期限を使い、保存済み入力は
  元のOrchestrationの入力と比較する。未指定のメッセージ作成時刻は旧Hostと同じく
  物理ロックを得る前に用意し、ライフサイクルと会話更新時刻はロック内で取得する。
  Launcherは追記専用の1MiBフレームを受ける。以前のLauncherは新しいフレームを選択しない。
  バージョン通知は通信形式の選択に使い、既存のLaunch証明、稼働Hostと公開許可を毎回確認する。
  公開許可後の応答喪失は同じ子の未確認効果とし、再照合が保存済みのレシートを観測する。
  昔の形式や容量に起因する互換経路は公開許可前にのみ使う。
  分離macOS Launcherの74件、実PythonプロセスとUnixソケットを使う元ファイル試験、
  24KiBの追記、公開許可中断、古いrevisionと権限なしの拒否を確認した。
  Rust Harness 300件と元のSetup承認記録1件、CPython 3.13.16の関連回帰352件、
  配布生成物5件・厳格なDefaults整合性検査が通過した。
  署名付きHTTP 5件は実SQLiteと会話ファイルを使い、ProviderとLauncher応答はfixture。
  通常送信、公開前の互換経路、許可直後の応答中断、ユーザー・アシスタント追記後の応答喪失を
  照合した。再送と再照合では新しい追記や推論を発生させない。
  追記対象外の公開メッセージ操作とCalendar、通常Saved Orchestrationの実行本体は旧経路。
  既存のPackVMを廃止する際は同じ続行データ、Provider呼出順序、承認、永続結果の再観測を維持する。
  会話の完了確認が使う `DurableTurnRuntime.get` と保存ターンの確定処理は、同じ
  `turns.sqlite3` のインデックス照合と行検証を共用できる。読み取り用のRust＋Wasmを追加し、
  元のJSONバイトを再シリアライズせず返す。存在しないファイルと行、無効な行、旧codecが
  必要な行、期限切れを判別する。読み取り用のVFSは既存ディレクトリを保持し、書込みと
  サイドカーのopenを拒否する。WALとホットジャーナルの復旧が必要な場合は読み取りを中断する。
  Turn Pack 0.2.0では読み取り用WITも固定し、元の0.1.0の確定用WITは従来の形で受理する。
  このポートを会話の完了確認に接続する際は、同じProfile・ターンに束縛された元の3段の
  署名済み権限とTurn Resourceの独立した子リース・予約を使う。別のGETと一覧には広げない。
  `17cee8be5` で、実SQLiteの排他待ちではキャッシュ設定もSELECTと同じ期限と権限チェックを
  使うよう修正した。実macOSでHarness 304件、Store 261件、Pack 6件、WIT固定1件、
  ワークフローの契約16件が通過した。StoreはmacOSの既定`/var`がシンボリックリンクのため、
  実ディレクトリ配下に`TMPDIR`を指定して実行した。新旧アーカイブでの実Wasm確定と新しい
  読み取りも通過し、元の行バイト、無書込み、計上量の解放を確認した。同じCIで作った
  アーカイブを使う実Wasmの2件を、DMGのPackアーカイブ生成ジョブに追加した。
  通常送信と明示Reconcileの `Shell → Saved Turn/Reconcile → Conversation Completion →
  Turn Resource GET` に読み取りを接続した。元の3つの署名済み権限と新しいTurn Packの
  固定WITを独立して検証し、読み取りに同じターンの子リース、期限、容量予約を使用する。
  親のHostがRust書込みに未移行の場合も、子の予約は独立して確認できる。同じTurnの公開GET、
  Events、Calendar等の別契約からは選択しない。旧形式は生きている子リースで確認してから
  従来の読取りに戻る。ストレージの破損、権限失効、未確認の通信はそのまま失敗として扱う。
  LaunchではConversationの確定とTurnの読取りに同じ署名付き三段の照合があり、両方を
  同梱する場合は確認を共用できる。Packのダイジェスト、機能、役割は個別に照合し続ける。
  配布パックの所有者試験は [CI 44466303817](https://github.com/harupipipipi/tobkiri/actions/runs/44466303817/job/135368446132)
  でLinuxの所有権証明が未対応のため停止した。証明を要求できるarm64・Darwin 24.6.0・APFS
  で同じ成果物と試験を実行するよう変更した。workspaceと実パスに解決した一時領域を検証する。
  `2a82008a0` の署名付きHTTPでは通常送信・再試行・明示Reconcileを通し、読み取り用の
  独立した子リースと、書込みが旧実装のままでも接続できることを確認した。公開GETは同じ
  応答を返し、専用IPCの回数に加わらない。旧版・旧codec用の互換応答を含む追加4件が通過。
  HTTP試験のProviderとLauncher応答はfixture。別のmacOS分離Launcher試験74件では、
  実CPython、Unixソケット、同梱Wasm、実SQLiteで元の行バイトと欠損を照合した。
  未許可の役割、破損、旧codecの応答も確認した。ここではTauri管理状態と稼働証明をfixtureで
  供給した。固定CPythonのIPC 269件、同梱アーカイブを使う実Conversationストア3件、
  ワークフロー契約16件も通過した。
  `7da875c403` は通常保存の `begin_saved` と `claim_saved` の判定を
  Turn Pack 0.3.0の `legacy-start` へ移した。元の request ID、入力 digest、
  メッセージ ID、JSON の順とイベント時刻を用い、既存のSQLiteと二つの書込みロックを保つ。
  `DurableTurnRuntime.begin` の再送では候補を生成せず、会話の競合判定では
  `delivery._rows` の再デコードと全行の保持を減らす。既存と同じ順で全行を検証する。
  停止要求を持つ行や取得済みの行は元のバイトを返す。新しい行は対象と時刻を照合し、
  SQLiteの確定用ロック内で公開許可を確認する。旧codecや未初期化のDBでは公開前に戻る。
  Pack 14件、実オーナー23件、strict Clippy、ワークフロー契約16件が通過した。
  実Wasmの3件では開始・取得・許可拒否時のロールバックと、既存読取り・確定を検証した。
  実アーカイブの0.1.0、0.2.0、0.3.0はLauncherのポート定義照合を通過した。
  `8f78fafb7` で通常LauncherのShell→Saved Turn→Lifecycleを専用IPCにつないだ。
  既存の親子リース・子の容量予約・期限を使い、元のBundleと両方の署名済み権限を照合する。
  起動後も同じBundleとPackを読み直す。SQLiteの両方のロック内で `saved_inputs` の
  親入力と受理済み入力のdigestを照合する。コンテキスト等の投影を受理しても、
  元の会話・ターン・本文とその他の要求内容は保持する。
  公開許可後に応答が失われた場合は従来の未確認効果として扱う。再送で取得済みの行を
  観測したときは新たな推論を開始しない。旧版や旧codecへの切替は公開許可前の応答に限る。
  元のSetupの2系統の物理Bundleを含む選択・再照合27件、Python IPC 324件、分離Rust
  Launcher 112件が通過した。実CPython→Unixソケット→ワーカー→実Wasm→SQLiteの
  9ケースで、digest不一致、許可拒否、開始、取得、無書込みの再送を確認した。
  Tauriの管理状態はfixture。署名付きHTTPでは元のSQLiteを使い、Launcher応答はfixture。
  受理済みコンテキスト、旧経路に戻る開始、取得確定後の応答喪失、重複推論と保存の防止を
  確認した。クリーンなコピーでPython IPCと署名付きHTTPの327件、元のWasmとSQLiteを
  使うRust 24件が通過した。[接続後のDMG検証](https://github.com/harupipipipi/tobkiri/actions/runs/44484324397)を開始した。
  実DMGのSetup・承認から実モデルでの会話までの検証は後続。
  `bfaf5bf92` で、元の `saved_complete` の開始と再開を純粋なRust＋Wasm Packに移植した。
  既存のJSON続行フレームと最大4 MiB、ユーザー入力3 MiBを維持する。会話の読み取り、追記、
  ストリーミングとStrategyの選択、ツール呼出順序と定義のpin、保存が不明な時の再照合を
  同じ状態遷移で判定する。結果のintentはデータであり、実行は元のBrokerの承認とリースを
  必要とする。サイズ検査だけの箇所は直列化結果のバッファを作らない。
  元のguest用の `_history` はこの入口から呼ばれず、会話履歴は現在のHostの投影が作る。
  署名済み旧guestを廃止する際に未使用処理も除去する。既存の追記receipt検証とは、
  IDや画像の構文検査を共用できる。Strategyのコスト項目と未解決参照の受理条件が違うので、
  入力validator全体の統合はそれぞれの受理範囲の維持が条件。
  Rust 18件とstrict Clippy、固定CPython 3.13.16との50ケースのバイト照合が通過した。
  実Core＋Wasmで同じ50ケースと、最大1 MiBの画像2枚を含む開始、読み取り後の追記、
  保存応答を照合した。画像の読取り後の段階では約9.40億fuelを使ったため、ゲストの上限を
  14億とした。Coreの64 MiBとゲストの48 MiBの枠内で実行し、各回の計上が0へ戻ることを確認。
  Pack作成と検証を通常CLIで行い、元のguestと比較するCIをインストーラー用アーカイブと
  独立した領域に追加した。ワークフロー契約16件が通過した。
  元のShell→Saved Turn→Saved Orchestrationの2段だけを物理確認する処理を既存追記と共用した。
  追記は引き続きMessage Manageの3段目も必須。権限欠損・不正、Calendar呼出元の隔離を含む
  選択試験23件と、分離環境でのLauncher関連ソースの型検査が通過した。
  当時のBrokerは同じSaved principalを
  通常送信とCalendarで使い、物理backendの証跡をprincipal単位で保持する。
  元の論理経路と承認を保ったまま、VMの割当て前に実際のWasm実行境界を選び、各続行で
  元のHostのpreflightと子dispatch、完了ダイジェスト、取消を保持する必要がある。
  `4c77ee5a6` で、5本目の `local.saved-orchestration` をレシートv3でmacOS CI/E2E版に
  同梱する。v1とv2も引き続き読み取れる。起動時は元の通常送信の2段の署名・Bundleと、
  リソースのdigest、版、権限なしの固定WITを照合する。各要求でも稼働中のProfileと
  同じアーカイブを再確認する。
  IPCは稼働中の相手プロセス、runtime instance、epoch、起動証明を確認してから
  最大4 MiBの本体を受け、既存のLIVEと取消を継承する。互換応答を使えるのは本体受信前。
  認可を示すものは稼働証明であり、プロトコル対応を示す環境変数は存在の目印として使う。
  新しいポートの上限は成功値のUTF-8で計上し、既存JSONをWITの文字列へ再直列化しない。
  WITに宣言された結果型、funded allocation、既存の期限と取消を維持する。そのJSONは
  Hostが確認したのち、元の続行とBrokerの承認に渡すデータ。
  元のSetup記録の2段のpinと元の物理Bundleを確認した上で、固定CPythonから実Unix socket、
  Launcherワーカー、v3、Wasmまでを分離環境で検証した。通常・続行、36 KiB入力、不正入力、
  証明なし、稼働登録なし、epochまたはruntime instance不一致の8ケースが通過。
  Tauri管理状態はfixture。分離macOS試験85件、Python IPC 292件が通過した。
  配布・DMG試験112件とサブテスト12件、およびレシートと物理パスの分離Rust試験16件が
  通過し、実アーカイブのv3を読み取り専用のDMGにマウントして照合した。
  v3で既存Conversationの実ストア3件も通過。実Core＋Wasmは前回の50ケースと最大画像、
  さらにWIT文字列として二重にエスケープすると4 MiBを超える正当なツール履歴も受理した。
  `9a87a1be2` の通常の保存送信では、Launcher が Defaultspack の起動・自動再起動の直前に証明を取り直し、
  有効な接続先と Wasm が揃う場合に、検証したマニフェスト digest を子へ渡す。
  Shell 登録時の証明と子プロセス起動時の証明は用途とタイミングが異なるため、
  Supervisor の定期確認は従来の実行 ID 照合に留める。
  Broker は元の署名済み Function・承認・Grant・要求 digest を使い、PackVM の割当て前に
  通常の Saved Turn が呼ぶ純粋な処理だけを登録済み Wasm ドメインに結び付ける。
  マニフェスト digest、対象ドメイン、現在の Launcher 登録、元の親リースと予約を
  各ステップで照合する。元の Provider が取り消された場合は、Wasm 側の実行も拒否する。
  ステップの整形と続行、認可済みの子呼出しと最終照合は既存の Host と共用する。
  Calendar など別の署名済み呼出元は個別の経路を選ぶ。
  署名済み Profile を読み直す HTTP 試験で、元の Grant と Wasm ドメインの組み合わせ、
  PackVM の割当てなし、保存結果、再試行時の重複防止、元 Provider の取消後の停止を確認した。
  この HTTP 試験は純粋なステップを fixture で実行する。Launcher IPC と実 Wasm は分離試験で確認した。
  `df3fad73f` で、macOS の HTTP 試験が公開するテスト用の起動インスタンスを、
  本物の Launcher ソケットから分離する共通 fixture を使うようにした。
  クリーンなコピーで対象208件を実行し、200件が初回に通過した。上記 fixture の修正後、
  失敗した8件と会話の回帰6件を再実行し、14件が通過した。
  Launcher の起動引数と環境変数の分離 Rust 試験24件と `rustfmt` も通過した。
  [統合後の DMG 検証](https://github.com/harupipipipi/tobkiri/actions/runs/44478189713)を開始した。
  他の PackVM 経路と Python Host を置き換えた時点で、互換起動と旧 Backend を除去できる。
  Pack 11件、旧保存領域18件、実Core＋Wasm 3件、元のSetupでの権限照合1件が通過。
  IPC 66件、実WasmとLauncherワーカーの分離検証35件、元の署名済みHTTPの会話読み取り・
  保存ターンとモデル設定の回帰が通過。HTTPのLauncher応答と、分離検証のTauriサービスは
  それぞれfixtureで供給した。
  実ファイルの会話から選んだモデルで2つの独立したローカルAPIとSSE応答を照合した。
  1BのCIでも同じ実ファイルを読み、その会話のモデル参照で推論する。通常経路における
  モデルの別名・無効状態・Provider要件の解決と、一覧・会話の変更は移行を続ける。
  `805541a56` の[実モデルCI 44449094777](https://github.com/harupipipipi/tobkiri/actions/runs/44449094777)
  は、Wasm RegistryでのローカルProviderの準備と承認済み保存、元のモデル解決、
  認証付きloopbackと、保存済み設定を使う実1B推論まで通過した。
  同じソースの[macOS arm64 CI 44449128148](https://github.com/harupipipipi/tobkiri/actions/runs/44449128148)
  は、Launcherのコールド起動、DMG作成、マウント後のリソース照合に通過し、配布対象外の
  CI用DMGを保存した。DMGを使ったGUIのSetup、承認、設定から会話までの連続操作は後続。
  元の `model_registry.store.json` から、通常会話向けのモデル解決を行う固定Rust＋Wasm
  ポートを追加した。別名は一段だけ辿る。無効モデルも状態付きの元の結果を返し、呼び出し元の
  Gatewayが元どおり拒否できるようにする。結果は資格情報ハンドル、ポリシー、数値、項目順を
  含む元のJSONを、公開用のモデル一覧と分けて保持する。実ファイルを使うCore＋Wasmと
  モデル設定の回帰4件、Pack 38件、strict Clippyが通過。quoteの2経路・通常生成・streamの
  4つはそれぞれ現在のProfile・Plan・LockからModel Registryへの署名済み権限を選ぶ。
  選定12件で同じ操作を呼ぶ別のcaller間でも権限を流用できないことを確認し、
  元のSetupから発行されたProfileを使う統合試験1件で4つの権限とscopeを照合した。
  通常の署名済み送信は、Shellから保存ターン、保存会話の完了、Gatewayを経由した場合に
  この解決を呼ぶ。元のBrokerは4つの子呼出しのそれぞれで、保持中の親リクエスト、別個の
  リースと予約、期限、起動世代を照合する。Launcherは元のBundleで子操作がreadである
  ことと、各ロールのpinを確認する。Model Packのアーカイブ照合を一覧・保存で共用する。
  1MiBの応答は認証したIPCを通してGatewayに返る。予約上限、対象外の起動証明、対応外の
  旧JSONでは現行の権限を再確認して元の読取を使う。切断や権限失効では読取をやり直さない。
  CPythonのIPC 98件とLauncher分離試験44件が通過。分離試験は実macOSソケットで
  物理RegistryとWasmを使い、別ロールの拒否、別名・無効状態・非公開メタデータ、未登録、
  旧JSONの切替を確認した。Tauriサービスはfixtureで、元のBundleとの4つの権限照合は
  元のSetupの生成レコードを使って別に実行した。
  署名済みHTTP試験では、既存の保存ターンとGatewayがバッファ応答とSSEを使い分け、
  再送で生成しないことと、親の終了後に同じ子操作でも元の読取へ戻ることを確認した。
  API鍵を含むProvider送信とLauncher応答はfixture。狭いHost回帰21件も通過した。
  `saved_bridge.py` の追記前の必須quote、任意のstream準備quote、実生成は、それぞれの
  権限で最新のRegistryを読む。共用するのは物理読取とWasmの解決処理までとし、各回で
  Registryの状態変更を検出する。旧Python分岐を除去する際はConversationと共通する
  1MiBの結果枠と旧JSON判定を見直せる。入力と文書の検証順序、無効モデルの扱いと
  Gateway側のProvider・資格情報の制御を維持する。GatewayとBrokerの所有者をRustへ
  移した際は、Pythonの起動世代引き渡しとソケットを除去し、同じ親リースと物理終了証明を
  Coreの直接呼び出しで照合する。
  Gemma 3 1BのCIは既存の認証付き通常Flowと、保存会話から選んだキー不要の経路を別々に実行する。
  [20ed649b4 の認証付き実1B 44364433650](https://github.com/harupipipipi/tobkiri/actions/runs/44364433650)
  では中断後のllama.cppスロット確認が再度失敗した。保存会話からの実1Bは独立して測る。
  `717883632` 後、同じNative Inputと実Wasmを使い、最初のSSEテキストで中断して、未完了の
  HTTP/1.1を開いたままのサーバーが3秒以内にEOFまたはリセットを検出することを確認した。
  元のFlowは中断で確定し、次の正常ターンは中断した履歴を読まなかった。ローカルnetwork試験3件が通過。
  実1BではCoreの中断時刻と有効な`/slots`応答を記録する。直接クライアントも同じ上限で独立して
  スロット解放を観測する。直前の処理が落ち着いた後に、履歴を付けない非キャッシュの試験入力で
  最初のテキストを受け取って切断する。サーバー側を比較する補助計測で、元の3秒の合否を使う。
  [44367151278](https://github.com/harupipipipi/tobkiri/actions/runs/44367151278) の認証付き試験は
  実1Bの完了と中断を通過した。別のキー不要の試験は保存と再起動後、Wasmストリームの読取りで失敗。
  同じランナーの直接8トークン要求は13.33秒で完了した。キー不要の経路では20秒がHTTP応答全体の
  期限なので、CPU専用fixtureの保存上限を6トークンに、質問を短くした。再起動後のモデル選択、
  ストリーム終端、表示テキストの照合と実行期限は引き続き検証する。失敗時はキー不要のテスト用
  サーバーログ末尾を残し、タイムアウトと別原因を区別できるようにした。修正後の実1B再計測はCIで行う。
  [実1B再計測44370741318](https://github.com/harupipipipi/tobkiri/actions/runs/44370741318) で
  認証付きFlowの会話・保存・中断と、キー不要の保存モデル経路の両方が通過。再起動後の
  Registry revision 1 で `saved-gemma` を解決し、実 Gemma 3 1B の表示デルタ6件と終端を照合した。
  [0db5f7134 の実1B CI 44443876778](https://github.com/harupipipipi/tobkiri/actions/runs/44443876778)
  は、元のRegistryと会話文書を使うWasm読取、認証付きFlow、保存モデルからの実1B推論まで通過した。
  インストール済みDMG上のGUIからの確認は別に行う。
- `454a84311` で、元のキー不要なProviderのローカルPOSTをRust Hostへ移した。
  実行Profileの既存Registryから有効な接続ID、レコードrevision、保存済みloopbackを照合する。
  元の1 MiBのリクエストをそのまま送り、従来どおりHTTP 200から最大4 MiBのJSONを読み取る。
  IPv4とIPv6の数値loopback、固定パスとヘッダー、30秒以内の元の期限、取消を維持する。
  実通信は元の呼出リースを再確認し、送信許可を一度だけ取得する。応答を保持する間も
  Coreの容量に計上する。実TCPの7件で上限ちょうどの応答、1バイト超過、リダイレクト拒否、
  切断と容量解放、別Profile・古いrevision・無効接続での送信拒否を確認した。
  元のPythonから採取したRegistryを使うProviderの58件と比較試験1件、既存ネットワークの
  回帰2件も通過。
  通常チャットの署名付きSaved Turn→Gateway→Adapterの呼出から、元のRegistryを読む
  権限と実行リースを引き継いでこの処理へ接続する作業が残る。最大4 MiBの応答は、
  専用IPCの既存長さ付き結果フレームを再利用し、元のAdapterがJSONを解釈できるよう
  生バイトで渡す。長い推論中も他の設定操作が進められるよう共有リスナーの直列処理を確認する。
  通常のDMG LauncherからこのRust経路での実モデル会話は未検証。
  `cd72a0ca3` で通常のShell→Saved Turn→Saved Orchestrationから、Gateway、Adapter、
  Provider Registryに至る既存の5つの署名対象を、GenerateとStreamごとに選択する。
  元のProfileと投影済みLock・Planの各grantを照合する。後続のLauncher検証は選択された
  全pinを物理Bundleと照合してからIPCを許可する。片方の経路にgrantがない場合や、別の
  ロールのスコープに差し替えた場合は該当経路を選ばない。選択の回帰28件が通過。
  `03bda4fcc` では、既存IPCの受信を固定長ヘッダーと上限検証後に一度確保する本文に分けた。
  受信中のチャンクとスライスの複製をなくし、完成時に一度だけ不変のbytesへ変換する。
  認可の確認が途中で失敗しても読み取った位置を保持し、取消と終了確認でフレームを読み続ける。
  既存のフレーム種別と上限、期限・認可確認は維持する。固定CPython 3.13.16の分離実ソケット
  回帰326件が通過。
  `22846faa9` で、通常Launchが両ロールの5つの署名対象をインストール済みBundleの
  Pack文書・実行カタログと照合する。各Packは一度読み、共通の文書で両ロールの操作、
  契約、実装、effect classを個別に確認する。起動中のProfile識別子・epochの証明に、
  元のApplication、Bundle、lockのdigestとロールごとのpinを保持する。
  既存v4 Bundleと実際に解決されたProfile・Lock・Planを使う分離試験では、両ロールと
  それぞれ単独のgrantを確認し、照合対象の10文書の差し替えと、別Applicationを拒否した。
  分離試験はLauncherの実ファイル読取りを使い、署名付きresolverの戻り値はfixtureで供給した。
  分離試験と権限選択の合計30件、macOSの実Launcherで `native-first-setup` のライブラリと
  テストの型チェックが通過。
  `0a6927322` で、元の管理対象HostのUnixソケットに保存済みローカルProvider専用の
  操作を接続した。プロセス、Profile、起動世代、epochとロール固有の起動証明を確認する。
  本文は1 MiB以内で保持し、実POSTの直前にBundle lockと10文書を読み直して、
  元の要求の専用送信許可を一回だけ取得する。最大4 MiBの生応答を実ワーカーの終了後に返す。
  HTTPを待つ接続は元のBrokerの同時処理数を上限に別スレッドで追跡し、既存の短い操作は
  元の入口で処理する。停止時は両方のソケットを閉じてjoinする。既存のネイティブ容量台帳を
  使用し、元のmacOSの`/tmp`・`/var`の扱いを既存Registryと共用した。
  分離したCPython→実Launcherソケットサービス→実ワーカー→loopback HTTPの5ケースで、
  Generate、Stream、推論中の既存Wasm Model一覧、送信拒否、古いrevision、未許可ロールを
  確認した。管理アプリ・Profile選択はfixture。分離したRustの63件、Registryの21件、
  固定CPython 3.13.16のIPC 348件とmacOS Launcherのテスト型チェックが通過。
  本文フレームから容量計上済みCoreBufferへのコピーは、受信前に同じ容量を確保できるように
  なった段階で削減できる。
  `736486698` と `c77b6aebd` で、通常チャットのAdapterが完了した同じロールの
  署名付きRegistry GETから接続ID・レコードrevision・endpointを保持し、同じBrokerの
  lease、容量予約、起動世代を再確認してからRustへ送る。保存ターンからGatewayまでの
  呼び出し元も照合する。旧レコードでrevisionがなく、容量不足や本文前の対象外応答の
  場合は元のローカルHTTPを使う。他の失敗は元の期限内で再送せずに確定する。
  本文のコンパイルと結果のJSON解釈はAdapter側に残る。書き出し済みバイトを使うため、
  現段階では順序・Unicode・オプション・応答エラーの互換性を維持できる。
  `763f3d7da` で有限JSONのStream応答に元のテキスト・ツール意図・終了理由を含めた。
  Gatewayはイベントで表示を作り、終端フィールドで保存済み返信を確定する。イベントだけを
  返す旧ローカル経路では本文を保存できないことが署名付きHTTP試験で分かった。
  Adapterの同梱版を1.0.3に更新し、v4目録・Profile Lockと互換projectionを再生成した。
  クリーンな`ac17cbfbf`のCPython 3.13.16で376件が通過し、署名付きHTTPからのGenerateと
  Stream両方で返信保存と結果再送、拒否と応答喪失に対する二重送信の防止を確認した。
  HTTP試験はProvider・Model Registryと署名付きBrokerを通し、LauncherのIPC呼出と
  PackVMのプロセス搬送はfixtureで再現した。ソース・Pack・実行目録・Bundle等の生成器6件の
  再照合とmacOS Launcherの型チェックも通過。最新DMGからの実モデル会話は後続。
  クリーンな `dbe79b053` では、macOS上で同じ署名付きHTTP回帰を実Launcherの
  UnixソケットサービスとRustワーカーに接続した。元のCPython 3.13.16クライアントを
  Rustの直接の子プロセスにし、実際に有効化したProfile・Lock・Plan、Providerレコード、
  起動世代を両側で共用した。GenerateとStreamで、疑似Providerが受け取った本文と
  認証ヘッダーの不在、Rustプロセスが所有するTCP接続、返信の保存、同じターンの再送時に
  POSTが増えないことを確認した。結合試験1件が通過した。
  この試験のApplication管理、配布物のresolver入口、PackVMのプロセス搬送にはfixtureを
  使った。CIでは同じ世代のWasmとRegistry Packを同梱する署名済みLauncherで、GUIのSetupと
  Shell handoffを経たHostに、このHTTP応答と再送の検査を接続する余地がある。
  `1dbc10644` は受信済みのProvider応答を変換する副作用なしの
  `local.provider-adapter` Wasm Packを追加した。Registryレシートv4に、同じビルドの当該
  Packと正確なWITを固定する。v1〜v3も引き続き検証する。Rust単体6件、通常CLIで検証した
  Wasm PackをCore内で実行する5ケース、DMG検証121件が通過した。実WasmではGenerateと
  Streamのフィールド・UTF-8の保持、未対応のJSONと4 MiBを超える投影の識別、各呼出し後の
  容量台帳の解放を確認した。
  `d592284f9` は通常の保存ターンでLauncherが読み込むこのPackへ応答変換を接続した。
  POST前に起動時のRegistryレシートとPackを照合し、元の承認で送った後、同じCoreの
  read-only portで変換する。変換済みの結果は要求済みで送信許可がある接続の専用フレーム
  に返す。扱えないJSONや上限外の変換では、受信済み本文を元のAdapterで処理する。
  同梱Adapterを1.0.4に更新し、CPython 3.13.16のIPC・ローカルHTTP・署名付き回帰382件、
  macOS Launcherのテスト型チェック、生成物の照合6件が通過した。
  クリーンな `d592284f9` の結合試験では、署名付きのGenerateとStreamがRust所有の
  TCP接続で一度ずつPOSTし、v4レシートで検証したWasm結果をIPCから受け取った。
  両方の返信を保存し、再送でProviderの受信数が増えないことを確認した。Application管理、
  配布物のresolver入口、PackVMの搬送はfixture。最新DMGの起動からの実モデル会話は後続。
  共用JSON codecは重複キーを一度再エンコードしてから有限値へ変換する。このコピーの削減は
  初出順・最終値・非有限値や孤立サロゲートの扱いを共用codecで検証してから行う。
  旧Pythonの応答解釈は、残る入力表現と例外の互換性を証明するまで必要。
  `e8fd1dce6` は、同一の検証済みPackに宣言された二つの読取り専用ポートを一つの
  Wasmインスタンスに接続する。先行呼出しのABI後処理・上限検査の後に後続を一度だけ実行でき、
  元の期限・fuel・64 MiBの容量台帳を共有する。
  `0d17af5ab` は `local.provider-adapter` 0.2.0へローカルリクエスト生成を追加した。
  元のキー順と上書き、Unicode、有限数、1 MiB上限を維持する。Registryレシートv4は
  0.1.0と0.2.0を版別のWITで検証する。Rust単体11件、実Wasmの単独・二段呼出し、
  旧版も含めたDMG検証147件が通過した。
  `692635268` は、既存の署名済みローカルAdapterが版3を使うときに、モデル等5項目の
  データを渡す。Wasmが本文を生成し、元のBundleを読み直した後に従来の許可で一度だけ送信、
  同じインスタンスで応答を変換する。入力を扱えない場合は送信許可前にだけ旧codecへ戻る。
  同梱Adapter 1.0.5、生成物照合6件、macOS Launcherのテスト型チェック、CPython 3.13.16の
  IPC・ローカルHTTP回帰400件、Rustの保存ローカルIPC・起動証明9件が通過した。
  `692635268` のmacOS結合試験では、署名済みの5項目を版3の実IPCで受け取り、
  v4レシートのPackで従来と同じ本文を生成した。Rust所有の各TCP接続で一度ずつ送信し、
  Generate・Stream両方でWasm結果を受け取って返信保存と再送を確認した。
  PackVMの搬送、Application管理、配布物resolverの入口にはfixtureを使った。
  `2bedf7641` は変換辞退時に旧形式の本文をまず同じLauncherへ渡す。RegistryとBundleを
  再照合する経路を維持し、送信が可能になった後の失敗は再送しない。最終ソースで
  macOSの実IPC・実Wasmの結合試験と、辞退コードを含むRust IPC・起動証明9件を再実行した。
  クリーンなCPython 3.13.16で403件が通過し、署名付きの経路でも変換辞退後の
  Launcher再照合が拒否された場合に、ProviderのTCP接続を開かないことを確認した。
  元のHostがIPCに送る5項目のJSONとWasmがProviderへ送る本文は用途が異なる。
  前者の変換を外す条件は、モデルアクセスを含む上流処理を同じCore経路で置き換えること。
  既存の `local.provider-model-access-rust` は `_apply_model_access` が使うパラメータ生成の
  候補を持つ。現行経路へ接続する際は、署名付きBrokerとLauncherが照合する同じRegistry
  レコードを入力にし、現在の拒否理由とオプション順を保つ。
  `948fe27fa` はIPC版4とAdapter Pack 0.3.0を接続する。Launcherが物理Registryから選択中の
  保存済み接続だけを読み、そのモデル許可リストをWasmの本文生成に使う。接続とポリシーを
  送信許可前にもう一度照合する。同じレコード版でも選択済みポリシーの変化を拒否する。
  既存のモデル拒否、無効なモデルID、ルート上書きのエラーを維持する。旧IPC版と変換を
  辞退した入力では互換codecで判定し、旧形式の本文も同じ送信権限で照合する。
  同梱Adapter 1.0.6の生成物照合6件、Python IPC・ローカルHTTP回帰433件、Rust Adapter単体14件、
  Registry選択7件、Launcher IPC 38件、ポリシー変更時の送信拒否1件、実Wasm結合1件、
  DMG検証器172件が通過した。実DMGを入れたGUI経路の検証は後続。
  クリーンな `948fe27fa` のmacOS結合試験では、明示許可リストを保存した同じProfileで、
  署名付きGenerateとStreamがIPC版4の実Wasmを通り、Rust所有のローカルTCPに一度ずつ送信した。
  本文、認証ヘッダーの不在、返信保存、同じターンの再送でPOSTが増えないことを確認した。
  Application管理、配布物resolverの入口、PackVMの搬送とHTTP応答にfixtureを使った。
  署名付きHTTP回帰では、モデルを拒否した場合に送信許可を取得せず、ユーザー発言だけを保存した。
  署名付きHTTPの旧本文、IPC版3、IPC版4の各1件も通過した。版3の最初の実行で発生した
  SQLiteのディスクI/Oエラーは、生成済みキャッシュの整理後の同じソースで再現しなかった。
  Pythonの `_apply_model_access` を縮小する条件は、旧IPCと既存Profileの入力表現、
  モデル・認証情報・接続先・期限のエラー順を同じ経路で再現できること。
  Registryの `validate_document` と `resolve_legacy_local_chat_policy` は選択レコードの
  ポリシーをそれぞれ正規化する。全レコードの検証と選択条件の順序を保ち、選択済みレコードの値だけを
  検証結果から再利用できれば、送信前に2回行う物理照合それぞれの変換を一回減らせる。
  `2173e2ea6` では、同じWasmのポート参照の不要な借用を取り除いた。旧HeadのDATA、
  Authority、容量CIがそれぞれRust 1.94の同じClippyで停止した。修正後の容量対象3クレートの
  strict Clippyがローカルで通過した。GitHub上の再検証は
  [DATA 44509752378](https://github.com/harupipipipi/tobkiri/actions/runs/44509752378)、
  [Authority 44509752703](https://github.com/harupipipipi/tobkiri/actions/runs/44509752703)、
  [容量 44509796534](https://github.com/harupipipipi/tobkiri/actions/runs/44509796534)。
  そのAuthority CIのUbuntuでは、追加したポリシー照合の入れ子の条件をClippyが指摘した。
  `c9cd27c52` で同じ条件を一つのガードにし、Harness Nativeのstrict Clippyとrustfmtが通過した。
  `request.rs` と `projection.rs` の事前走査は文字列内のエスケープと深さ判定を共用できる。
  応答だけが必要とする重複キーを含めた構造数の上限と、各経路の旧codecへの戻り方を保つ。
  [717face1f の実Gemma 3 1B CI](https://github.com/harupipipipi/tobkiri/actions/runs/44487465311)
  は既存のHarness実行経路で通過した。
  [d3113e9b2 の再計測](https://github.com/harupipipipi/tobkiri/actions/runs/44490251513)では
  キー不要の保存モデル経路が通過し、認証付きFlowは32 msで取消が確定した後、3秒以内に
  llama.cppの空きslotを観測できなかった。独立した直接クライアントでは2.450秒だったが、
  会話履歴・キャッシュ・接続の扱いが異なる。次回の失敗では、直接プローブ前に採取した
  サーバーログ末尾を認証情報を除いて保存し、3秒の合否基準を維持する。
  [ae72bf306 の再実行](https://github.com/harupipipipi/tobkiri/actions/runs/44493529574)は
  認証付きFlow・キー不要の保存モデルの両方で通過した。
  [0031aea76 の実1B再計測](https://github.com/harupipipipi/tobkiri/actions/runs/44506573236)は
  キー不要の保存モデル経路を通過した。認証付きFlowは42 msで取消を確定し、サーバーログにも
  対象タスクの中止が残った。57回目までの照会で3秒以内の空きslotを観測できず、後続の直接プローブ
  開始時点では既に空いていた。サーバーが中止を検出してからslotが空くまでの時間は特定できていない。
  同じ3秒の上限で [948fe27fa の実1B CI](https://github.com/harupipipipi/tobkiri/actions/runs/44509752472)
  は認証付きFlowとキー不要の保存モデル経路の両方で通過した。
- `1df9a002d` は既存Gatewayの `tobkiri.service.ai.route.v1` の候補評価を
  `local.ai-routing` 0.1.0に移した。二つの事前照会とGenerate・Streamの4ロールについて、
  Shell→保存ターン→保存済みOrchestration→Gateway→署名済みRouterの元のgrantと文書を
  選択する。Launcherは実行前に配布物とアーカイブを照合する。Packは渡された記述子、要件、
  稼働情報、評価時刻を使って、元の候補順・除外理由・曖昧なProviderの扱いを決める。
  Registry取得、資格情報、送信許可の所有者は元の経路にある。v5レシートは7本を順序付きで
  検証し、v1〜v4も引き続き読む。
  `8244d3f5a` で、同じ署名済みRouter呼び出しのリース、親の連鎖、期限、予約量を
  確認してからNativeへ渡す。入出力は1 MiBの範囲で、ネイティブ実行対象外または明示的な
  変換辞退の場合は元の処理へ戻る前に同じリースを確認する。署名済みPython Routerの配布元は
  既存のものを参照する。
  実WasmアーカイブをCoreから呼ぶ試験1件、4ロール選択と無関係なgrant拒否2件、
  Rustゲスト単体6件とstrict Clippy、Python IPC39件、レシートRust15件が通過。
  macOSのLauncherテスト型チェック、Harness Nativeのstrict Clippy、分離Rust IPC40件、
  固定CPython 3.13.16の署名付きHTTP回帰1件と生成物照合6件も通過した。
  クリーンな `8244d3f5a` のmacOS結合試験では、署名付き保存チャットから4ロールを
  Launcherの実IPCで呼び、v5レシートから検証した実Wasmの結果を元のPython Routerと照合した。
  バッファ応答とストリームで返信を保存し、同じターンの再送で候補評価や生成が増えないことを確認。
  Application管理、配布物resolver入口、PackVMの搬送とProvider応答はfixtureで供給した。
  CPython原本との3000件のソース比較では、出力2560件がバイト一致し、変換対象外440件で
  元の処理に戻ることを確認した。既存v4を含むDMG検証器とワークフロー検査214件も通過。
  [現在の実1B CI](https://github.com/harupipipipi/tobkiri/actions/runs/44513762760)を開始した。
  元のPythonでは候補ごとにProvider一覧を走査する。Wasmでは要求ごとの索引を使い、
  完全一致の重複やワイルドカードの曖昧性を維持する。署名済みGatewayとBrokerをCoreへ
  移した段階で、ルート別のIPCとPython側の予約額設定を除去できる。元の承認・取消・同時処理と、
  各ロールが別々の現在値を読むことを確認してから行う。
  [8244d3f5a の実1B CI](https://github.com/harupipipipi/tobkiri/actions/runs/44513762760) は
  認証付きFlowとキー不要の保存モデル経路の両方で通過した。
- `a102d49d2` は通常の署名済みGateway Streamのイベント正規化を
  `local.ai-routing` 0.2.0の独立した効果なしポートへ接続した。既存のスタンドアロンWasmと
  Rustの正規化処理を共用する。旧0.1.0のルーティング宣言とレシートは引き続き検証できる。
  LauncherはStream専用のgrant、配布元文書、Bundle Lock、実アーカイブを照合する。
  `52667689c` の物理ファイルの回帰で、Stream用の正規化カタログとBundle Lockの変更を拒否し、
  別のRoutingカタログの変更ではこの経路を拒否しないことを確認した。
  `4f9a96ab1` は、その呼出自身の署名付きリース、親、期限、予約量を既存DATA IPCに接続した。
  テキスト差分、ツール情報、使用量、終了イベントの順序と値を維持する。
  終了後の追加イベントなど、Wasmが変換を明示的に辞退した場合は同じリースを確認して
  Pythonの当時の検証エラーへ戻す。実行中に権限が失効した場合は処理を続けない。
  Rustゲスト単体18件、WasmのCore実行1件、分離Rust IPC41件、権限選択と物理再照合各1件、
  レシートRust15件、DMG検証器とワークフロー検査238件、macOS Launcherテストの型検査が通過。
  スタンドアロンのWIT経路はCPython 3.13.14原本との53件で結果とエラーが一致した。
  固定CPython 3.13.16ではIPC419件、生成物照合6件、`9adb6d5a9` の署名付きHTTP回帰1件が通過した。
  この回帰では、正規化の予約が祖先から独立することを確認する。祖先同士は既存の予約を共用できる。
  同じソースのmacOS結合試験で、Stream専用の署名付き証明からLauncherの実IPCを通し、
  v5レシートで検証した0.2.0実Wasmの返り値をPython原本と照合した。返信の保存と再送で
  Providerを呼び直さないことを確認した。Application管理、配布物resolver入口、PackVM搬送、
  ProviderのSSE応答はfixtureで供給した。
  元のProviderの応答変換とGatewayのイベント正規化では同じイベントが行き来する。
  Gatewayの所有者をCoreへ移す際にIPCのコーデックを省ける。要求ID・試行番号の付与、
  プロバイダーが返したイベントの検証、元のツール・使用量の呼出順序を保つ。
- `f25a017b1` は既存Generate・Streamの使用量・コスト計算を
  `local.ai-routing` 0.3.0の独立したポートへ接続した。既存の単体Wasmと計算を共用し、
  0.1.0・0.2.0のWITとv5レシートを引き続き検証する。Launcherは元の使用量操作の
  署名済み権限を対象のGatewayロールごとに照合し、他ロールのカタログから権限を借りない。
  `76f6b392b` はBrokerが取得した同じ親の連鎖、子のリース・期限・予約量を実行に渡す。
  Providerの報告値、Gatewayが渡した単価、桁の扱い、明示的なnullとトークン別名の優先順を保つ。
  料金情報がない場合の金額は不明のままとする。元のエラーや範囲外の結果は、リースを
  確認して元の計算へ戻す。権限の失効や実行不能を旧処理の許可として扱わない。
  Coreから実Wasmを呼び、既知の料金、料金不明、有限JSONの範囲外を確認した。
  Rustゲスト13件、CPython 3.13.16原本との単体版41件、Python IPC439件、分離Rust IPC42件、
  権限選択と物理再照合各1件、レシートRust15件、DMG検証器とワークフロー検査263件が通過した。
  macOS Launcherのテスト型検査、Harness Nativeおよび両ゲストのstrict Clippyも通過した。
  現行の宣言検証で、実際にステージされた0.1.0、0.2.0、0.3.0の各アーカイブを照合した。
  クリーンな `76f6b392b` で固定CPython 3.13.16の署名付きHTTP回帰と生成物照合6件が通過した。
  同じソースのmacOS結合試験では、使用量計算だけの署名付き証明を使って、GenerateとStreamの
  両方をLauncherの実IPCから実Wasmへ通した。Python原本との結果の一致、返信保存、再送で
  Providerを呼び直さないことを確認した。Application管理、配布物resolver入口、PackVM搬送、
  Providerのバッファ・SSE応答はfixtureで供給した。
  Routing・Stream・使用量・ツール呼び出し情報に共通するPython IPCは、モードごとの契約・
  入力項目・結果検証を共通の型で表現できる。既存のワイヤ形式、各モード別の有効化、元の
  権限境界、互換処理の条件を維持する。署名済みGatewayをRust所有に移した段階で、同じDATAを
  IPC用に変換する処理を除ける。
- `37d264686` は既存Generate・Streamのツール呼び出し情報を
  `local.ai-routing` 0.4.0の独立したポートで正規化する。各ロールで元の署名済み四段の
  呼出しを個別に検証し、以前の3版のPack宣言とv5レシートも受理する。既存の単体Tool Bridgeと
  共通のRust処理でツール名、要求ID、呼出し順、引数JSONを保つ。すべての結果で承認と
  実行権限は未付与。1 MiBの入力・出力境界内で計算し、ゲストが扱えない値や元のエラーは、
  既存のリースを再確認して元のTool Bridgeで扱う。権限失効や実行不能は終端エラーとして扱う。
  固定CPython 3.13.16から独立生成した単体版の81件は全件一致した。
  Rust Routing 18件、Tool Bridge core 4件（旧v1の33ケースを含む）、共通codec 19件、
  権限選択33件、Python IPC459件、Rust IPC43件、Packレシート15件、DMG検証とCI構成288件が通過した。
  macOS Launcherのテスト型検査、Harness Nativeとゲストのstrict Clippy、生成物照合6件も通過した。
  物理検証で0.1.0〜0.4.0の実Packを照合した。Core経由の実Wasm試験では正規化と範囲外の
  戻り値を検証し、各呼出し後に確保量が0へ戻ることを確認した。
  クリーンな `37d264686` で、GenerateとStreamを元の署名済みHTTP入口からLauncherの
  実IPCと検証済みWasmへ通してPython原本と照合した。Providerが返したツール呼出し情報も
  実行権限なしのデータとしてのみ返した。Application管理、配布物resolver入口、PackVM搬送、
  Providerのバッファ・SSE応答はfixtureで供給した。
  同じコミットの署名付きHTTP回帰では、両ロール、旧Launcherとゲスト辞退の互換経路、
  権限失効、拒否、実行不能、Providerが返した未要求のツール呼出し情報、親のない呼出しを確認した。
  [0.2.0のGemma 3 1B実モデルCI 44516968539](https://github.com/harupipipipi/tobkiri/actions/runs/44516968539)
  は、認証付きloopbackのWasm Flowと、保存済みRegistryから選ぶキーレス会話が通過した。
  0.4.0を含む `badeb906f` の同じ実モデル試験も通過した。
  [Gemma 3 1B実モデルCI 44520961942](https://github.com/harupipipipi/tobkiri/actions/runs/44520961942)
  Tool BridgeとRoutingで共通化した正規化処理は、単体版のABIが不要になるまで保持する。
  元の署名宣言、固定CPythonとの差分試験、通常Gatewayの呼出し結果の一致を除去の前提とする。
- `63a7d0ccf` はリクエスト整形と再試行の可否判定を `local.ai-routing` 0.5.0の
  二つのポートへ接続した。整形はGenerate・Streamと個別のルート見積もり、再試行判定は
  バッファ付きGenerateで元の署名済み呼出しに結び付く。両者はDATAを返す。Providerの呼出しや
  ストリーム失敗の終端条件は元のGatewayが担う。レシートv5で0.1.0〜0.5.0を検証する。
  `8d7ddf5f4` はPythonの値をJSONが暗黙変換する場合と、署名済みProviderで扱うべき
  Credential Handle候補を互換経路へ戻す。同じリースを再確認して元のエラーを保持する。
  1 MiBの範囲に収まる有限値を使い、期限と要求IDの扱い、パラメーター・要件の順序、
  再試行の6条件と最初に不成立だった条件を保つ。
  固定CPython 3.13.16の原本処理を使う2,000ケースで、1,246件の順序付きJSONと754件の
  互換経路選択が一致した。Rust Routing 25件、権限選択35件、Python IPC474件、Rust IPC44件、
  レシート単体15件、DMG検証とワークフロー構成313件、生成物照合6件が通過した。
  macOS Launcherのテスト型検査、Harness Nativeとゲストのstrict Clippyが通過した。実Packを
  5版とも照合し、検証済み0.5.0の全ポートをCore経由で呼んだ。各呼出し後の確保量は0へ戻った。
  クリーンな `20cd3f781` のmacOS結合試験では、署名済みHTTP入口から全4役割の整形と
  Generateの再試行判定を、実IPCと検証済みWasmへ通してPython原本と照合した。
  配布物resolver入口、Application管理、PackVM搬送、Provider応答はfixtureで供給した。
  元のGatewayに届く型付きProviderエラーをfixtureで再現し、Generateで判定が1回呼ばれ、
  Streamは1回のProvider試行で終了することを照合した。通常のBrokerが型を包むHTTP 503は
  独立して検証し、再試行判定へ進まない既存の挙動を保持した。
  同じクリーンなソースの署名付きHTTP回帰で、通常とエラーの10ケース、旧Launcherと
  ゲスト辞退、リース失効・実行拒否・実行不能、親のない呼出しが通過した。
  Launchで同じPipeline Packと目録を役割ごとに読み直す。起動計測で重複を確認できたら、
  読み取りを共用しつつ、各役割の署名済み操作と呼出しごとの物理再照合を維持できる。
- `1250fe3e8` は通常の保存済みチャットで、Provider応答からGenerateの基本結果を作る処理と
  Streamの最終結果を組み立てる処理を `local.ai-routing` 0.6.0へ移した。既存のRust Finalizer
  と純粋な処理を共用し、Provider呼出し、Stream Normalizer、Tool Bridge、Usage Costは元の
  署名済みGatewayから同じ順で呼ぶ。GenerateでProviderが返したエラーや、JSONの1 MiB境界を
  超えるなどゲストが扱えない値は、元の結果整形に戻して同じ挙動を維持する。Streamの場合、
  既に終えた子呼出しを繰り返さず手元のDATAだけで組み立てる。
  Shell、Saved Turn、Saved Orchestration、Gatewayの3段の署名済み辺を照合する。
  `58d9b6fe4` は現行Bundleと収録ソースを再生成し、親が予約の証明を持たないときも既存の
  親リースを照合する。`f988bda4b` は元のGatewayに割り当てられた予約をHost側の実行証明に
  渡す。古いWasmアーカイブ5版は既存のWITとv5レシートで引き続き照合する。
  固定CPython 3.13.16の原本との2,300ケースで、1,329件の順序付き結果と971件の互換経路選択が
  一致した。Rustゲスト31件とdoctest 1件、権限選択36件、Python IPC481件、Rust IPC45件、
  Gateway回帰71件、レシート15件、DMG検証とワークフロー構成338件が通過した。
  macOS Launcherのテスト型検査、ゲストのstrict Clippy、アーキテクチャ検証、生成物照合6件、
  実Pack6版の宣言検証とCore経由の全ポート実行が通過した。
  クリーンな `89aa81414` のmacOS結合試験で、GenerateとStreamが同じ署名済みGateway
  から実IPCと0.6.0の実Wasmを呼び、Python原本と同じ結果を保存した。Provider応答、配布物
  resolver入口、Application管理とPackVM搬送はfixtureで供給した。
  同じソースの署名済みHTTP回帰では、両ロール、旧Launcherの非適用、元の整形への辞退、
  リース失効・実行拒否・実行不能の7ケースを確認した。各ケースでProvider呼出しは1回だった。
  `46701e9c1` は過去の独立比較を物理的に分け、当時のコードとProfileを固定Gitオブジェクト
  から取得し、過去のレシートと生成ソースを照合する。固定CPythonによる旧Provider 94件と
  Tool Bridge 81件の観測再生成、履歴側のソース照合と現在側CI契約の抜粋11件が通過した。
  ローカルでの過去の全ケースと実コンポーネント比較は、空き容量が尽きたため未完了。
  現行GatewayとBrokerをCore所有に移したら、結果整形のIPCと、PackVM用の結果エンコードを
  除去できる。保存済み応答、型付きエラー、署名済み子呼出しの順序と非重複を前提とする。
- `2865b0709` は保存モデルの要件のマージ、パラメーターの統合、出力トークン上限と
  ルート要件の正規化を `local.ai-routing` 0.7.0へ接続した。元のModel Registryの解決を
  使い、無効モデルの扱い、Provider Registryの照合、接続先と認証情報の所有者は既存の
  Gatewayが確認する。保存上限と要求側の上限のうち小さい値を採用する。複数生成や不正な
  上限値は、同じリースで元の型付きエラーを返す。
  Generate、Stream、両見積もりでShellからGatewayまでの3段を再照合する。見積もりの
  二つの配信方式は同じ物理grantを使い、実行中の元の入力から方式を識別する。0.7.0では
  0.6.0と共通するGatewayの物理証明を再利用し、policyとresultのWasmポートを個別に確認する。
  レシートv5で過去6版の宣言とWITの固定digestを維持する。
  0.5.0のPipelineと順序付きJSON、Pythonの数値変換、dictと整数比較を共用した。1 MiBと
  ネスト128段の境界内で処理し、対応外の入力は元のPythonで扱う。固定CPython 3.13.16の
  3,000ケースで、順序付き結果975件と互換経路選択2,025件が一致した。
  Rust Routing36件、権限選択36件、Python IPC495件、Rust IPC46件、レシート15件、
  DMG検証とワークフロー構成363件、Gateway関連94件と保存ルート28件が通過した。
  macOS Launcherのテスト型検査、strict Clippy、アーキテクチャ検証と生成物照合6件が通過。
  Core経由で0.7.0の全ポートを実行し、出力上限と各呼出し後の確保量の回収を確認した。
  クリーンな `2865b0709` のmacOS結合試験で、通常会話と見積もりの全4役割が実IPCと
  実Wasmを使った。モデル別名を解決し、別々のターンで最新の保存モデルと上限を使って原本と
  同じ返信を保存した。Provider応答、配布物resolver入口、Application管理、PackVM搬送は
  fixtureで供給した。
  同じソースの署名済みHTTP回帰で、両配信方式、旧Launcher、ゲスト辞退、リース失効、
  実行拒否の6ケースが通過した。既存の署名済み見積もり回帰2件でも両方式を検証した。
  0.6.0を含む `350517a72` のGemma 3 1B試験は、認証付きloopbackと、保存モデルから選ぶ
  推論が通過した。[実モデルCI 44527611997](https://github.com/harupipipipi/tobkiri/actions/runs/44527611997)
- [容量CI 44487465360](https://github.com/harupipipipi/tobkiri/actions/runs/44487465360)は
  実容量の計測前にルート全体のrustfmtで止まった。`ecda8e89c` からLauncherの依存を
  経由して17ファイルが対象に入り、同じ差分が繰り返し報告されていた。`6407eea34` で
  該当ファイルをRust 1.94.0で整形し、ルート全体の `cargo fmt --all -- --check` が通過した。
- [Launcher staging 44484324397](https://github.com/harupipipipi/tobkiri/actions/runs/44484324397)
  は、元のLauncherがHost原本のinventoryを検証する段階で停止した。
  `972baeb39` で `native_saved_orchestration.py` と `saved_rejection.py` を正規一覧に追加し、
  元のパッケージ検証器で97ファイルの一致を確認した。DMGは修正後のheadで再検証する。
- [Launcher staging 44493089798](https://github.com/harupipipipi/tobkiri/actions/runs/44493089798)
  はTurnの根拠を検証する3件すべてに合格した後、CI側の期待件数2で停止した。
  `d7315fe5f` で件数を3に更新し、元のテストフィルターと判定を維持した。
  [修正済みのDMG CI 44502822238](https://github.com/harupipipipi/tobkiri/actions/runs/44502822238)
  には応答変換版までを含めた。リクエスト生成版の配布物は別途実機で検証する。
- A-6 の調査では、現行の `ContractLLMGateway` とv4ストリームは受け取った使用量を保持し、
  旧Providerの一部はキャッシュ内訳やOpenAIストリーム末尾の使用量を捨てることを確認した。
  現行OpenAIアダプターは明示の指定がなければ使用量を要求しない。
  通常のチャットもターンごとの時刻とヒントをシステム入力の先頭付近に挿入する。
  実測のヒット率は未確認。実装では対応する公式ストリームだけで使用量を要求し、
  明示設定と汎用互換・llama.cpp経路を維持する。対象 `rumi_provider_adapters_pack/runtime/adapter.py`
  は現行と履歴の署名済みPack目録に固定されているため、既存の成果物を保存して新しい版で検証する。

- C-14 は `74450120b` で、native acceptanceのSDKコピーと生成マニフェストを
  正規SDKからのステージングに置き換えた。Node 21件とRustによる生成Packの検証1件が成功し、
  UIマニフェストのハッシュが同じことを確認した。`eec96c310` ではHarness UIも
  同じSDKから別ディレクトリに組み立て、既存Profileのハッシュと一致することを含む
  関連Node 24件が成功。チェックイン済みHarness Packを直接参照するProfileの移行が残る。

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
  現行GUIのProvider設定は、署名済みルートを通じて `interactive_effect.manage` が
  `ProviderRegistryHostFactoryV4` のprepare/executeを呼ぶ。Rust側の設定prepareを
  この固定バインディングと既存Profile Registryの読み取りへ接続するのが次の置換点。
  Host側の世代予算と専用承認、実行時の同じCredential Brokerを維持する。
  `component_store.rs` の段階書込みは現行の結果検証用で、実際のCASが後から実行されるため、
  設定の保存確認・不確定応答・資格情報回収にはそのまま使えない。承認済み経路で一つの
  Registryへ確定し、実際のCAS結果を設定処理に返すまで、製品設定の切替は未実施。
  Rust RegistryのWasmに独立した `configuration.prepare-local` を追加。型付き入力には
  接続名とendpoint、任意の表示名・カタログID・capabilitiesだけを持たせる。実行中のProfileと
  Pack専用ストアのrevisionを用い、キー不要の旧制約、送信時のURL表記のdigest、正規化先を
  既存の `prepare_configuration` で計算する。保存やCredential Brokerは呼ばない。
  Coreに個別に承認されたProfileで、固定Python原本のIPv4・末尾スラッシュ・IPv6の計画が一致。
  外部endpoint、キーとProfileの入力、ポート未承認を拒否し、事前検証前後のRegistryが一致した。
  通常の試験用保存が進んだ後のrevisionも同じStoreから読んだ。ローカル56件、実Wasm/Core 4件、
  component有効のstrict Clippyが通過。実1B CIに同じ検証を追加。
  通常GUIは旧Registryの正本と世代予算を使っており、この読み取り専用Wasmを接続するには
  その正本の二度読み・revision競合の扱いを保つHost側の結線が要る。
  キー不要の保存は別の `configuration-commit.execute-local` に分けた。試験はCoreの
  信頼済みUser入口を使い、読取りだけのProfileには書込みを付与しない。事前検証内容を
  正規化JSONのdigestで固定し、実行中Profileの現行revisionで再検証する。応答がCoreの
  JSONで表せることを確認し、条件付きSQLite書込みが1行を確定した後だけ成功を返す。
  不確定な書込みは繰り返さず、状態の一致だけから今回の承認が確定したと扱わない。
  既存のWITの一般エラーではCore内部の不確定書込み理由が外側に残らず、通常GUIの
  approved operation statusへの接続時に実行固有の確定・不確定を保持する必要がある。
  2 Profileの同revisionで他方の計画、元のURL表記の変更、承認拒否、旧計画の再利用を拒否し、
  再起動後の記録を確認した。ローカルSSE試験は事前検証→承認済み保存→モデル保存→会話で使用。
  Wasm/Core 4件、component単体56件、strict Clippyが通過。実Gemmaの再起動経路も同じ保存に切替。
  [実モデルCI 44378633570](https://github.com/harupipipipi/tobkiri/actions/runs/44378633570)
  は準備、承認済み保存、モデル選択、Core再起動後のllama.cpp Gemma 3 1B、認証付き
  loopbackのSSEが通過した。
  RustのUser呼出しは、明示指定時に `core_imports.rs` の実Storeエラーを元のProvider
  実行スコープに記録する。WITへ変換する前の型に限り、ゲストが返す文言は参照しない。
  `dispatch.rs` では同じ承認済み呼出しの終端を処理し、不確定書込みとDATA移動の修復要求を
  Coreの固定StepErrorコードへ返す。Wasm/Coreの4件が通過。隔離したProfileの試験で、
  SQLiteの保存後に監査結果の記録だけを失敗させ、Registryのrevisionが1だけ進んだ状態で
  保存不確定が返ることを確認した。破損した移動ジャーナルでは書込みが行われず、従来の
  User呼出しには既存のWITエラーが返った。別の実行スコープや同文の一般エラーは検出対象外。
  Wasmの全単体試験は物理一時ディレクトリ `/private/tmp` で282件通過、既存ignore 5件。
  変更対象のstrict Clippyが通過。macOSの既定一時パスではAPFS試験がパス解決で失敗した。
  製品GUIとの接続では、`launcherConnectionsConfigure.ts` の承認済み要求IDと
  `PendingEffectController` の永続claim/dispatchを引き継ぐ必要がある。初回のCore呼出しは
  新規IDを採番するため、タイムアウト後にそれを呼び直さない。Gate終端の確認は要求の終了を
  示すので、再起動後にRegistry CASの確定を照合するには書込み固有の永続記録が別途必要。
  現在の実行内マーカーは、成功したStore戻り値を受け取った後で応答全体を失った場合や、
  native Store応答前に呼出し側が終了した場合までは照合できない。
  製品の最初の置換点は、署名済み `/api/ai/provider-key` の
  `ProviderRegistryHostFactoryV4.capture` 内のキー不要prepare。現在の設定・モデル読取りは
  Profileの `provider_registry.store.json` に依存し、Wasmのprivate SQLiteとは別のため、
  同じ正本を参照して二度読みとrevision競合を維持する接続が先に要る。保存を切り替える前に
  そのCASの結果を同じ承認済み要求へ結び付け、認証ありの設定では既存Credential Brokerの
  選択済みconsumerと世代予算を継承する。今回のCoreの保存試験はこの製品接続を含めていない。
  同じ既存Profileの二度読みを使う、独立した `legacy-configuration.prepare-local` を
  Wasm Packに追加。最初の観測で得たrevisionを二度目の保存前検査へ渡し、現行Core Profileと
  各文書のID、記録内容を既存Registry処理で検証する。保存計画に他の接続やハンドルは含めない。
  設定計画で使うのはrevisionだけなので、`Registry::snapshot` の全接続コピーを外した。
  `secure.rs` の既存ファイル専用読取りは、データrootの作成やリンク追跡をせず、上限内の
  同一ユーザー所有ファイルだけを読む。不在と、読取り中に変化した場合を別に扱う。
  Core統合のfixtureでは従来の相対pathから実ファイルを二度取得し、間の置換、別Profileの
  文書、未承認呼出し、両方の保存先への書込みがないことを検証する。製品へ配線する際は、
  署名済みLauncherが確認したroot、同じactivation、現行Profile、元の予算を二度の観測に束縛する。
  この入口の計画は承認の証明を含まず、旧Registryの承認済み保存先はそのまま使う。
  既存ファイル3件を含むStore単体134件、Registry単体56件、実Wasm/Core 4件、対象の
  strict Clippyが通過。通常の設定に
  適用する前に、観測の作業領域を元の世代予算に計上し、現行activationとProfileをread前後で
  検証する製品側の接続が必要。通常Launcherの `NativeRuntimeState::adopt_core` にはまだ
  製品用Sessionがないため、別の `core-host` アプリを有効にして代用しない。
- Coreに保存済みProfileを起動する際、`ProfileStartBinding` で実行するhead、検証済みPackから
  作ったplan digest、権限epochを固定した。既存のGateと起動処理を共用し、準備後にheadまたは
  epochが変わった場合はUIへ渡す前にSessionを終了する。終了にも失敗した場合は両方のエラーを返す。
  `LauncherCore::start_bound` は既存のUI generation発行・破棄に接続し、起動側のactivationが
  開始前または公開時に失効していれば新しいSessionを公開しない。通常のIPC入口は従来のoriginを確認する。
  登録済みProviderとモデルの試験で、同じ `RuntimeOwner` をCoreへ渡し、開始と再起動に固定値を使用。
  同じProfile IDでのhead変更、異なるplan、headを保った権限epoch更新、期限切れの起動を検査。
  Core単体304件とLauncherアダプター単体74件が通過。Wasm/Coreの4件はローカルで成功。
  製品Launcherのv5 Profile/Lock/PlanとCoreの各digestは別契約。実製品で使うには既存
  `presentation::launch_verified_target_once` の実行IDと、署名済み置換Pack群で確認したCore
  の固定値を結ぶ。実行中の管理対象子プロセスの失効は既存の終了管理へ渡す。DMGの現行手順と
  照合した範囲で、試験用Harness ProfileのIDを変更して使う方法では元のPackと権限を復元できない。
- Provider Registry、Model Registry、Saved ConversationのManifestとshapeを各Packのソースに移し、
  E2E内の仮Manifest生成を削除した。`stage_registry_packs.py` が標準の `tobkiri pack
  manifest/build/verify` で実 `.tpk` を作り、アーカイブの長さ・SHA-256とCoreで使うManifest
  digestを記録する。統合試験はファイル形式のPack pinを通して同じアーカイブをCoreへ渡す。
  CoreのローカルPack規則と既存のDATA所有者に合わせて `local.*` を保持した。
  非配布用macOS CI/E2Eでは、同じrunの作成ジョブから受け取るreceiptのSHA-256を検証して
  アプリのruntimeリソース目録へ格納する。DMGマウント後も同じreceiptと3ファイルのサイズ・
  digestを照合する。通常の配布ビルドにはこのステージを有効にしない。既存LauncherのProfileと
  Native Sessionを関連付け、このPack群を設定・会話から使う製品の接続は引き続き未完了。
  ローカルのPack CLI 2件、実Wasm/Core 4件、strict Clippy、ワークフロー38件が通過。
  同じコンポーネントを2回パッケージしたバイト列とreceiptは一致し、変更したWasmを含む
  アーカイブを標準verifierが拒否した。
  Launcherのビルド処理を使った3件で、格納した4ファイルがruntime目録と一致し、改変・
  リンク差し替えを拒否した。Launcherスクリプト163件（実DMGのfixtureを含む）が通過。
  実 `.tpk` 3個を入れたfixture DMGも読み取り専用でマウントし、receiptと各Packを検証した。
  旧フロントエンド試験のStart欄を、同ボタンが購読準備までdisabledである現行実装に揃えた。
  非配布用DMGのRegistry Pack receiptに使ったSHA-256をLauncherのビルドに固定する。
  既存Shellの明示的なLaunchで、署名済みApplication resolverが選んだ同梱ルートから
  receiptと3つのアーカイブを照合する。既存のジョブ・準備期限・Core制御枠を用い、読取りの直後と
  Shell引き渡し前に有効なv5実行IDを再確認する。
  同じビルド・起動時の検証器を通す6件が成功し、生成済みの実 `.tpk` 3個も照合した。
  次の置換候補は `rumi_provider_registry_pack.provider-configure.prepare` のキー不要分。
  旧 `profile_grant` で到達する準備と `interactive_only` の保存は別操作として維持する。
  `OriginalActivationInspection` は、既存のHost選択とワークスペースが指す同じv5
  エンベロープを確認する。既存ロックの下で、Authorityの監査チェーン、アクティブ予約と
  SecurityEpochを一つの読み取りトランザクションで照合し、現行Profile・Lock・Planを返す。
  監査チェーンは行ごとに検証する。既存Setupの復旧では元のキャッシュと読取り順序を保持する。
  読み取り対象はチェックポイント済みのDB。保留中のジャーナル、未完了予約、WALがある場合は
  既存の復旧経路が必要になる。元のSetupが行う復旧と書込みは、元の入口で扱う。
  native Harness 271件とstrict Clippyが通過した。既存の確認済みSetupのfixtureでも
  発行直後のProfile・Lock・Planを読み、保留ジャーナルおよびWALの拒否、保存済みファイルの
  バイト列と物理close結果を照合した。
  非配布用DMGの明示的なShell起動からこの検査を呼ぶ。既存のLauncherジョブに結び付いた
  AppConfigを保持し、同梱Setupのschemaと、宣言済み成果物の読み取りに使う同じWasm Storeの
  予算を使う。照合結果は署名済みShellの実行IDとカタログrevisionに照らし、ジョブの期限と実IOの
  close結果を確認する。照合できない状態では、従来のHostが復旧を扱う。
  Core 305件、native Harness 271件、strict Clippyが通過した。Storeの単一取得、キャンセル後の
  予算の停止、元のSetupが作成した実データを期限切れのLauncher検査が変更せずに拒否することを確認した。
  `OriginalProviderPrepareSelection` は有効なProfile・Lock・Planから、選択済みShellと
  coordinatorを経由する旧Provider設定の `profile_grant`、Authority参照、スコープと
  FunctionPrincipal、variantを照合する。対応する旧Packと実行カタログは元のBundleで
  検証されたパスとdigestを使って、同じ起動ジョブの予算で確かめる。
  同梱 `.tpk` の検証はレシートで照合する同じファイルの読取りをCoreの標準verifierへ渡す。
  manifestのID・版・digest、Wasm runtimeと、`local.provider-registry.legacy-configuration@1.0`
  の唯一の `prepare-local` 操作を確認する。レシート照合はverifierが途中で読取りを終えても
  最後のバイトまで行う。旧版で有効な別variantや別スコープは元の設定経路で扱う。
  Core 306件、native Harness 275件、Pack manifest 23件、strict Clippyが通過した。
  確認済みSetupが作るProfileから旧準備操作を選んだ。生成済み `.tpk` の読取りと改変・取消の
  3件、実WasmのProvider・Model Registry 4件も成功した。
  宣言済み成果物を読む同じ `NativeArtifactInspection` のStoreで、レシート照合済み
  `prepare-local` を一度実行できる。ホストがProfileと旧Registryの二つの観測値を渡し、
  `current-profile` だけを提供する。観測値の上限は各1 MiB。実 `.tpk` の試験で、元の
  設定計画と同じ応答、revision競合の型付き拒否、Registryのバイト列が維持されることを確認した。
  元の署名済みShell → coordinator → Providerの `profile_grant` を経る固定経路に限り、
  Python Host側で元のPOSTのlease、親scope、期限、キャンセル、受付済み予算を借りる接続点を追加した。
  旧Registryはその接続点からディスクリプタを使って最大2回読み、当初の不在と確認後の消失を区別する。
  承認前の計画作成と承認後の保存を分け、既存のGenerationBudgetを適用する。
  署名付きHTTP試験で、隔離予算ポリシーがない構成の承認待ち・承認後の一度だけの保存と、
  OpenRouter専用ポリシーによるローカル設定の拒否を確認した。実Wasm E2Eは4件、
  macOSの `SecureDirectory` は29件が通過した。元のコミットでも、既存のProvider設定試験2件は
  この隔離ポリシーによる503を返す。
  `legacy-configuration-live.prepare-local` は既存の `flow.invoke-callback` を、同じStoreで
  発行した固定の読取りhandleに束縛する。Wasmの設定計画が旧Registryの読取りに達した時だけ
  ホストを呼ぶため、要求や初回の文書が不正な場合は以降の読取りを行わない。欠落ファイルと
  既存ファイルの `null` は区別する。Launcher同梱Packの照合では従来の操作とこの追加操作を確認する。
  Native Storeはライブ読取りに一本化した。同じWasmの旧exportにある二つのスナップショットの
  引数は、既存利用先を移してから削除できる。実 `.tpk` の E2E 4件で、初回なし・有効なRegistryで
  各2回、不正な要求で0回、壊れたJSONと `null` で各1回の読取り、実ファイルのrevision差による
  競合、Registryの内容を変更しないことを確かめた。
  `native_launch_metadata.rs` は3つの同梱アーカイブと旧Providerの選択を照合し、元の
  AuthorityとStoreのcloseを確認してから、Profile・epochとreceiptのdigestを返す。
  `dock_registration.rs` がこの値を監視用メタデータから取り出し、元の認証済みlistenerに結び付ける。
  `presentation.rs` はShellの `BindingAdmitted` を受けた後だけ登録する。managerは同じ
  listenerのPID・起動時刻・port・Application・Profileを確認し、停止・再起動・listener交代で
  登録を破棄する。確認値はこのプロセスのメモリに保持される。元の選択試験4件と、
  macOSのプロセス観測を代替した分離テスト2件が通過した。
  [DMG CI 44392237715](https://github.com/harupipipipi/tobkiri/actions/runs/44392237715)の
  `actual_resolved_source_reaches_core_worker_and_preserves_desktop_metadata` だけが失敗した。
  Coreがパスの各要素をsymlink追従なしで開く際、macOSのテスト用パスに含まれる `/var` などを
  拒んだ。fixtureの実体パスに設定と同じ相対配置を適用した。macOSのCoreで元の失敗を再現し、
  修正後のrootで `RuntimeOwner::open` と `begin_native_setup` の成功を確認した。
  `native_provider_prepare_job.rs` が登録を借り、元のRuntimeOwnerの個別ジョブで署名済み
  Applicationと同梱Packを再照合し、読み取り専用のWasmを動かす。登録が同じdigestで更新されても
  古いリクエストが再利用できないよう、同じ登録オブジェクトをStoreと結果返却時に確かめる。
  `native_runtime_jobs.rs` の個別joinは取消を当該ジョブに伝え、スレッドとsourceが実際に終了した
  時だけ完了を返す。不明な終了はRuntimeOwnerを保持する。3件をmacOSの実Coreで確認した。
  Launcher管理状態を代替する分離検証では、生成済み `.tpk` の照合、同じCore上のWasm実行、
  型付き拒否、取消後の結果破棄、読み取りの前後のguardを実行した。
  [DMG CI 44407663438](https://github.com/harupipipipi/tobkiri/actions/runs/44407663438)で
  Launchの成果物検査10件が通過した。CIの期待件数が8件のままだったため後続へ進めず、
  同じフィルターを実行するジョブの指定を10件に揃えた。修正後のCIラッパーを使うmacOS実行も通過した。
  次に通常Launcherと元のHostのリクエストを接続する。既存Host Brokerの接続認証は起動中の
  Profileまでを証明する。接続時に実listenerのPID・起動時刻とHTTPサーバの世代を照合し、
  元のleaseで逐次読み取りと取消を確認する。相互の物理終了と、1 MiB超の旧Registryとの
  互換動作も製品経路で確かめる。
  分離したmacOSプロセス同士でUnix接続のUID・PIDを両方向から観測し、同じ環境で
  Pythonの `monotonic_ns()` と `CLOCK_UPTIME_RAW` の時刻基準が一致することを確認した。
  `063b395b5` は `native_provider_prepare_service.rs` と `native_provider_prepare_ipc.py` を元のLauncherと
  署名済みHTTP HostのPREPAREへ接続した。管理対象の実プロセス、Profileの4識別子と
  security epoch、HTTPサーバの世代を照合し、元のHTTP captureとleaseが有効な間だけ
  Registryを読む。結果は実Wasmジョブが終了してから返し、承認と保存は既存の経路で行う。
  1 MiB超の既存Registryは、制限の判定と実ジョブの終了を確認してから、同じguardで元の
  読み取りを実行する。追加メモリを確保できない場合は、作業を始める前に既存の予約で
  入場を試みる。署名済みの予約、承認、Generation Budgetはいずれも既存の経路を通す。
  分離したmacOSの実プロセスで、実Pythonの読み取りと実Rustのソケット、固定レシートの
  Wasmを使い、Registryなし・既存revision・サーバ世代の不一致・1 MiB超と取消を確認した。
  Launcherの管理状態とroot解決は分離検証用fixture。Rustはこの実通信と回帰の9件、
  Pythonは署名済みHTTP 5件を含む43件が通過した。Windows固有の3件はmacOSでは対象外。
  Launcherの通常ターゲットでもジョブと通信の回帰8件を実行するようCIを更新した。
  通常のリリースworkflowは検証済み
  SetupとRegistryの入力を取り込む段階が残る。署名済みCI検証Appで配布時の流れを調べる。
  Registryと元の呼出しをRustで担える段階で、Pythonからの同期借用、ソケット、稼働世代の
  引き渡しを削除し、現在のlease確認と物理終了証明をNative側の同じ所有者に集約できる。
  `58cbfdc56` で承認後の検証用に `legacy-configuration-commit.validate-local` をProvider Packへ追加。
  元の計画を再計算する2回の観測を終え、承認時の計画と比較してから3回目の読み取りで
  既存のprepare-saveを行う。Rustの既存executeも同じ検証を使う。検証用Storeは元の
  Registryの読み取りだけを受け付ける。Coreは固定したWITと実Wasmの型を確認し、回数を計上する。
  新しくビルドしたPackのCore試験で、準備済みレコード、計画の改変、3回目直前の競合、
  通常のPackポート経由のアクセス拒否、検証だけで原本が変化しないことを確認した。
  Providerの57件、固定Pythonの比較1件、元のPythonでの同じレコードの実行比較、
  Clippyが通過した。製品の承認済み実行は別のPendingEffectワーカーで動く。接続時は
  そのワーカーのdispatched lease、確定済みpayload、稼働中Hostとの結び付きを確認する。
  元のNamedLock・リビジョン照合・不確定な保存応答の
  確認まで検証した段階で、従来のPython側の保存実装を整理できる。
  [macOSのSetupレビュー CI 44416408861](https://github.com/harupipipipi/tobkiri/actions/runs/44416408861)
  は `native-first-setup` のテスト用メタデータ3件で初期化漏れを検出した。通常フィールドの
  fixtureを維持して、機能有効時の任意Provider情報を初期化した。分離コンパイルで修正前の
  3件を再現し、修正後は機能の有効・無効の両方で通過した。同じファイルの変更をSetup
  レビューCIの起動対象に含めた。
  `58cbfdc56` の[再検証 44418244271](https://github.com/harupipipipi/tobkiri/actions/runs/44418244271)
  でmacOSのLauncherとAppKit・WebKitが通過した。
  [実モデル CI 44418244216](https://github.com/harupipipipi/tobkiri/actions/runs/44418244216)も
  llama.cppのGemma 3 1Bを実行し、保存したRegistryのモデル選択からのWasm Flowが通過した。
  元の署名済み `interactive_only` の実行へWasm検証を接続した。保存用ワーカーが引き継いだ
  画面の所有者、一回限りの承認、確定済みリクエスト、実行中のlease、稼働中のHost公開状態を
  照合する。既存のNamedLock内で、署名済みRegistry実装が書き込みに進む直前にも再照合する。
  既存の署名済みProviderのソースとレジストリ形式を使う。Nativeは結果を返す前に終了し、
  そこから元のCAS、保存と不確定な応答の確認に進む。固定IPCの入力上限は16 KiBで、承認済み
  計画がある場合の結果上限は保持するモデル選択ポリシーに合わせて1 MiBとした。
  macOSの分離実通信11件で、署名済みApplicationに同梱するレシートの実Wasmを使った。元の
  Registry読取り、3回の観測、計画改変の拒否、16 KiBを超えるモデル制限を保存後も保持する
  ことを確認した。分離検証のLauncher管理状態と元のPack選択はfixtureを使用した。
  署名済みHTTPと周辺回帰66件が通過した。Runtimeの公開切替とNamedLock内の承認失効では
  Registryの内容が維持され、再resumeによる保存もなかった。通常の認証情報付き設定を扱う
  通常起動では、試験用OpenRouter予算を新規適用しない。隔離試験は信頼済みHostからレビュー済みの
  テンプレートを明示する。既存の照合済み記録は新規適用の有無にかかわらず当時のテンプレートで
  復元する。以前の暗黙起動と明示起動が同じrequired記録を保存したため、由来を識別できない
  既存記録も維持する。Authorityとwitnessの片側だけ、requiredなしの残存active、
  同じProfileの鍵に制限マーカーだけがある場合は拒否し、欠けたwitnessを新規作成しない。
  区別された別Profileの通常登録には影響しない。Native予算と実際の送信上限の回帰127件、
  通常起動と明示の隔離起動の20件が通過した。署名済みGateway更新後の通常起動でも、
  登録済み予算の復元を確認した。周辺のパッケージ統合62件は変更前の `3c134cc6d` と変更後で
  同じ28件が通過し、同じ34件が失敗した。既存の失敗は効果復旧の例外期待18件、
  Host Extension試験の古いresolver引数15件、GatewayがローカルHTTPへ到達しない1件。
  Host Extensionの外部Pack fixtureに現行のorigin引数を追加し、外部Packに同梱originを
  渡さないことを確認した。関連する署名済みカタログ試験16件がクリーンなfixtureで通過した。
  署名済みHTTPの15件も通過。予算セレクターの差し替えを除いた通常起動で、既存承認から
  設定を保存し、base URL・API鍵・モデルを使う会話のSSEとバッファ応答、再送と拒否を確認した。
  HTTPS応答と、バッファ応答のテキストブロック試験でのstream準備状態はfixtureで供給した。
  元の保存処理を移す際は、元の排他ロック、リビジョン照合、文書形式、結果の不確定時の
  確認まで同じ永続化境界で検証する。その段階でHost側の保存直前guardのためのRegistry
  派生クラスを除去できる。
  承認済みのキーレスローカルProvider設定では、Wasmで凍結した計画を再照合し、元のRegistryに
  保存する文書を作る。最初の3回は既存Hostから読み取り、4回目はRustが元のNamedLockを取得して
  同じProfileのファイルを読み取る。保存するJSONは元のキー順・改行・UTC秒の日時形式を維持する。
  RustはWasmのStoreを終了させてから、同じロックの中でリビジョンと現在の承認を再確認し、
  同じディレクトリで原子的に置き換える。応答が不確定な場合は、当該ファイルのinodeと保存内容を
  照合する。上限に達した場合に旧保存へ進めるのは、置き換え前に終了したと確認できたときだけ。
  Pythonの保存直前guard専用のRegistry派生クラスを除去した。元のHostによる最初の3回の読み取りと
  承認照合は引き続き使う。認証情報付きProvider設定の保存は残る。
  Store 142件、Providerコンポーネント60件、JSON互換18件、実Wasmから旧Registryへ保存するCore試験と
  strict Clippyが通過。macOSの分離検証11件では、実Python・RustのUnixソケットと固定レシートの
  Wasmを使い、既存モデル制限の保持、置き換え直前の承認失効、出力上限での保存前の終了を確認した。
  固定CPython 3.13.16で署名済みHTTP、IPC、Provider設定の回帰67件も通過した。
  Launcherの管理状態と署名済みrootの解決は分離検証用fixtureを使用。インストール済みDMGでの
  Setup・専用承認ウィンドウ・設定・実モデル会話の通し検証は残る。
  `69e34d882` に続き、LauncherがキーレスProviderの準備と承認後の保存に必要なRegistryをすべて
  読み取るようにした。準備は2回、保存は3回の排他なしの観測とNamedLock取得後の4回目を行う。
  各回の前後で元のHostにleaseを照会する。ファイルとディレクトリは同じ記録先をdescriptorで
  検査し、初回の準備ではファイルがなければディレクトリやロックを作らない。macOSの標準の
  `/var` と `/tmp` はroot所有の標準リンクを確認して扱う。
  PythonのRegistry読取りと文書を渡すIPCを削除した。呼び出し元のない検証専用IPCも削除し、
  Coreの原本比較で検証用Wasmを使う。元の承認、リクエスト形式、Brokerの予約量は維持する。
  読み取り開始後の旧経路への復帰は、検証済みファイルやステージ済み出力の上限を検出し、稼働中のleaseと
  作業終了を確認できた場合に限る。確保失敗はサイズ超過と区別する。
  Store 145件、実Wasmで原本の計画と読み書きを比較するCore試験、strict Clippyが通過。
  macOSの実ソケットは13条件で、準備・承認後のファイル上限、承認失効、標準パス別名、
  不正リンクと不正UTF-8を確認した。Launcher管理状態はfixtureを使用。署名済みHTTPを含むPython
  回帰76件と、上限の通知前にもlease照合を要求する最終版IPC 21件が通過した。
  次に通常Hostの既存承認・Registry互換処理を移す。移行後にこの同期IPCと旧Registryへの
  フォールバックを削除できる。承認の失効、確定結果の照合、失敗時の再実行防止は残す。
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

## 042: 復旧時の外部PackとAuthority

- `81e9046df` は保持したWasm catalogでの既存Pack照会、署名済みNormal Packのcatalogと
  CASの独立した再読、それぞれの依存順序の観測を追加した。外部のexecutable catalog再読と
  全Packを含める `catalog_with_packs` の反映、既存Host Extensionの互換比較が残る。
  [固定原本比較 CI 43604577547](https://github.com/harupipipipi/tobkiri/actions/runs/43604577547)で
  原本が署名して登録した2 Packと独立した正規Host Extension更新を準備する。dispatcherはローカル81件が成功。
- 同じコミットで、保存済みProfileを入れたcatalogを確認してからpublication lockの内側で
  Authority DBを開閉し、元のworkspace有無まで進めた。実Wasmの検証では独立した原本を
  Authorityの初期化から呼出しまで実行して比較する。Workspaceがない場合、原本の
  `active_activation_reservation().fetchall()` が返す行は、この呼出し元では存在確認にのみ使われる。
  後続の移植では候補ID、active状態、複数行のエラー、SQLiteの後半読み取りエラーを保持しながら、
  行データの領域を呼出し元で確保せずに済む。activationの復旧と通常DMGの通し試験は未完了。
- 前段の固定原本比較はLinuxで成功。署名済み2 Packの再読、Host Extensionの
  元Pythonでの再構成、Authority初期化の2試験が通過した。macOSの比較が残る。
- 次のスライスは、ゲストに返さない固定SQLで、workspaceがない場合の予約の有無を照合する。
  該当なしはジャーナルの削除直前、1件は既存の拒否生成、複数件は元のAuthorityエラーへ進む。
  同時に、closureの後続検証に使う外部専用のdigest指定サブ処理を試験構成に追加した。
  旧版と署名済み後継版の両方を個別に選択し、元の読み取り順・生成catalogへのフォールバックを検証する。
  本番で外部executable catalogを使うには別途、Packと実行定義の再読および互換レコード全体の再構成が必要。
- [実Wasm CI 43602884999](https://github.com/harupipipipi/tobkiri/actions/runs/43602884999)は
  Profile置換の試験で、先行するcatalog-loadでWasmの燃料を消費し切った。
  元の読込位置はカタログ項目2回目、計測時点で経過時間上限は未到達、解放後の確保0。
  現在の復旧試験を同じ上限で再実行し、既存のスキーマ照合にかかる実行量を詰める。
- `e6f235be9` はApp Rust schema backendのpointer参照で、チルダを含まない実Unicodeの
  tokenに対する2回のreplaceを省く。escape順、非組み込み型に対する実メソッド呼出し、
  Unicode、コールバック、同一の予算を維持する。
  新しい3ケースに合わせた件数を `373b47175` で修正し、36件の参照を含む
  [原本比較 CI 43606290093](https://github.com/harupipipipi/tobkiri/actions/runs/43606290093)が通過した。
  [App Wasm CI 43606128602](https://github.com/harupipipipi/tobkiri/actions/runs/43606128602)はcold schema 42件に成功し、全catalogの呼出しは2B fuelを使い切った。
  既存の別枝に明示選択の実装があるが、実カタログの
  [CI 42980447600](https://github.com/harupipipipi/tobkiri/actions/runs/42980447600)は燃料切れ。
  aliasとファイル名は別呼出しであり、同一ハッシュによるcheck_schemaのスキップに置き換えない。
  通常のLauncherへの採用は、現行の予算で原本比較と実カタログ通過を確認した後に扱う。

- `998919dee` は一致する予約がないときに、元のAuthorityとpublication lockの内側で
  ジャーナルを消し、元の次の起動処理へ進む。Authorityの試験はLinux・macOSで成功。
  activationディレクトリの3試験はTokioを起動せずに失敗し、`5183407cb` で修正した。
  同コミットはプロファイルごとのactivation lockを取得してpendingの有無を確認する。
  [並列 CI 44068201150](https://github.com/harupipipipi/tobkiri/actions/runs/44068201150)で確認中。
- `5183407cb` では、Normal Packの実行定義を指定digestで読み直し、互換レコードの
  再構成とCASの独立した再確認を行う。元の `catalog_with_packs` が元の順序で利用し、
  その後に保存済みProfileを反映する。Wasmへの受け渡しに使う実行定義は確認した最初の値で、
  Nativeへの追加権限を持たない。ローカルでは元のメソッド本体を使った読み込みと
  dispatcher 83件が成功した。適合Host Extensionの互換再構成、実Wasmの復旧通過は残る。
  [実Wasm CI 44068320748](https://github.com/harupipipipi/tobkiri/actions/runs/44068320748)
- 既存workspaceのpendingを元のロック内で読み、厳密JSONとworkspace digestを照合する。
  pendingがないときはfencing token順で元のSQLite結果を読み、予約なしの場合は
  錠を閉じてからactive予約を照合し、該当なしで未公開の場合はpublicationを復旧する。
  SQLiteの未使用列も元と同様に変換を確認し、後で読むIDとstateだけを確保する。
  pendingの予約照合、未完了予約のabort・監査、active snapshotの照合が残る。
- `eb23f183e` は、検証済み実行定義の比較式とPackの所有を保ち、先行比較で指摘された
  Clippy 2件を修正した。 `d0ff57263` は、組み込み文字列の `#` 参照で元のbaseと
  CPythonの `ref[1:]` を直接使い、先行していたURI解析を省く。Unicode、単一文字の
  同一性、本来調べないbaseを含む比較を155件から160件に増やした。実Wasmでの効果は未計測。
- 保留記録の形を確認した後、同じactivation lockの内側で実Authorityの元の
  `SELECT * WHERE reservation_id=?` と照合する。未完了一覧と同じ列変換を使い、
  `fetchone` のタイミングを保ったまま後続処理で使う列だけを確保する。既に中止済みなら
  元の順序で保留ファイルと対応するenvelopeだけを削除し、通常の復旧へ戻る。
  activeの予約は、既存の安全なディレクトリ読み取りと厳密JSONで対応するenvelopeを
  開き、canonical digest、activationのstate、planとprofileの参照を元の順序で確認する。
  元Pythonの復旧本体では中止済み・別Profile・古いtoken・別ファイルを含む8件の順序を確認。
  さらにenvelope検証後に予約を別のSQLite接続で読み直し、plan、Profile authority、
  fencing token、activationの状態と照合する。すべて一致するときだけ現在のSecurityEpochを
  読む。原本の再照合を7ケースで確認し、別planではepochを読まないこと、古いepochを
  拒否することを実Authorityの試験に追加した。
- 未完了の予約はworkspace lockを保持し、fencing token順で個別のAuthority
  トランザクションを実行する。保留記録がある場合はその予約に絞り、状態の照合、
  元のepoch行の読み取り、状態更新、暗号化された監査イベントを同一トランザクションに
  入れる。既存の監査処理を共有し、監査書き込みの失敗で状態と時刻を戻すことを確認する。
  元Pythonの実AuthorityStoreで3種類の中止と監査エラーでの巻き戻しを確認した。
  workspace lockを解放した区間でのパッケージ検証と再公開、active snapshotが残る。
- `e85058491` は、中止の4試験がUbuntuで本番用ファイルシステム割り当て証明を要求し、
  Authorityに入る前に停止した点を修正した。既存のテスト専用割り当てを使い、実SQLiteと
  暗号化監査の照合および領域解放を確認する。本番の割り当て方法は別の試験で扱う。
- コミット済みのProfileは元のworkspace lockを閉じた後、同じAuthorityと公開ロックの中で
  確認する。現在の置換カタログに入れた登録Profile、実ローダーで読んだShell定義とPack pinを
  照合し、親ディレクトリの記録を使って実ファイルの同一性とツリーのdigestを確認する。
  元Pythonの実ファイルで有効例と変更例5件を照合した。エントリポイントのdigest・実行形式、
  macOSのInfo.plist、再ロックとポインタ公開、active snapshotは後続に残る。
  実ローダーのPack pinはIDとdigestだけを保持する。全経路が通ったら、既存ローダーの
  検証済み結果から同じpinを取り出せるか検討し、マニフェストの追加パースを減らせる。
  `91717d7c9` のローカルHarness試験29件とstrict Clippyは成功。LauncherのDashboard
  試験17件も成功した。`e85058491` の中止4件は
  [CI 44076940752](https://github.com/harupipipipi/tobkiri/actions/runs/44076940752)で
  Linux・macOSとも成功した。
- [原本比較 CI 44071483174](https://github.com/harupipipipi/tobkiri/actions/runs/44071483174)
  はURI 160件を含む比較に成功。全体の [macOS CI 44068320748](https://github.com/harupipipipi/tobkiri/actions/runs/44068320748)
  では、変更前の実componentがAuthorityの照合まで到達せず、実測30.24秒で元の期限に達した。
  [実App Wasm CI 44071515666](https://github.com/harupipipipi/tobkiri/actions/runs/44071515666)は
  変更後の初回スキーマ42件に成功（147,351,964 fuel残り）。全catalogは2Bを使い切った。
  [計測 CI 44072390698](https://github.com/harupipipipi/tobkiri/actions/runs/44072390698)では
  初期データ所有に523,930,522 fuel、最初のPackの34番目のスキーマファイルで約4M残り、
  読み込みを返す前に停止した。計測用の埋め込み入力と異なり、実Launcherの経路では
  既存のCatalogIoがファイルを供給する。次は同経路に原本比較済みRustのschema
  providerを導入した候補を、現行の上限で比較する。
- [whole Harness CI 44075223346](https://github.com/harupipipipi/tobkiri/actions/runs/44075223346)
  は25,736,236 Bの実Wasmを生成してmacOSで照合した。実カタログの通常呼出しと復旧呼出しは
  両方とも先頭のPackの処理中に2B fuelを使い切った。Rustの参照処理まで到達し、30秒の
  時間切れとは別の失敗で、後処理時に使用領域と予約は0だった。エラー比較9件のうち先に
  終わる5件は原本と一致し、カタログを読む4件ではWasmの呼出しが完了しなかった。
- コミット済みのShellについて、ツリー照合に続いて実行ファイルを独立に開き直し、
  個別digestとバイナリ形式を確認する。macOSではXML・バイナリのInfo.plistから
  `CFBundleIdentifier`を照合する。元と同じ5秒の検証区間を共有する。
  Info.plistは読み取り済みのバイト列から検査し、上限付きで構造を読み取る。
  Python生成の標準XMLとバイナリは各60例で一致した。一部の非標準XMLや深さ128を
  超えるplistは拒否する。新しい汎用パーサーが同じ読み取り上限、型の区別、重複キーの
  扱いを保てる場合は `original_recovery_bundle_metadata.rs` の専用処理を削減できる。
  復旧の再ロック、ポインタ再公開、active snapshotは後続に残る。
  ローカルHarnessのAuthority関連90件は成功し、原本fixtureを必要とする4件は未実行。
- 実macOS CIでは、1つのStoreについて、インスタンス化、inventory、7つの初期化
  チェックポイント、`Runtime.init` 完了、先頭のcatalog exportの前後を計測する。
  既存の残燃料と経過時間を記録し、2B fuel、30秒、64MiBの予算を共有する。
  ローカルでは `bootstrap-ci` のコンパイルとワークフロー検査13件が成功した。
- `d6f2b035f` ではコミット済みのShellを検証した後、workspace lockの外と再取得後でそれぞれ
  配布物のメタデータを取り直す。同じ保留記録、envelope、Authority予約、
  SecurityEpochの場合に、ワークスペースの `active.json` を元のcanonical JSONで
  原子的に書き換え、その後に `pending.json` を消す。既存の配布記録は保持する。
  保留記録が変わった場合は復旧ループへ戻る。途中でenvelopeやepochが変わる例では
  元のポインタを保持する。再公開後にも残る未完了予約を中止してから、同じ状態ディレクトリで
  active pointerとenvelopeを照合する。現行レコードの検証、旧版migration、
  再読による一致確認とHostポインタ公開は残る。パスにNULを含む場合も、親ディレクトリの
  確認を先に行う元のエラー順を復元した。
  Authority関連のローカルRust試験93件とstrict Clippyが通過し、4件は原本fixture待ち。
- `30fb1876b` の `schema_kernel/src/app_schema/references.rs` では、元の `pointer[1:]` と
  同じ `PySlice_New` でスライスを作る。`builtins.slice` の検索と呼出しを外し、先に取得した
  `unquote` と実際の `__getitem__` を使う。macOSのローカル拡張で原本比較19グループが通過。
  [固定依存関係によるApp原本比較 CI 44086016083](https://github.com/harupipipipi/tobkiri/actions/runs/44086016083)も成功。
  同一予算でのWasm燃料差は未計測。Registryと他の名前解決は元と同じ順序を個別確認してから扱う。
- `29fcc1f94` は、組み込み `str` に限って `str.split` と同じ `PyUnicode_Split` を呼ぶ。
  `unquote` がサブクラスや別オブジェクトを返した場合は元の属性検索と呼出しを使う。
  実Rust拡張の原本比較20グループは通過。Unicode・空要素と実ディスクリプタを追加した。
- 再公開したアクティベーションで、workspace lockを保持したまま先頭Profileの厳密JSONを読み、
  v4/v5に対応するAppのスキーマを選ぶ。元の順序で、旧版の生オブジェクト条件と現行の
  文字列レコードを扱う。オブジェクト以外・未対応バージョン・重複キーなどを元の
  SchemaValidationErrorの診断と原因付きで扱う。JSON構文エラーの深い原因の再構成、
  Appスキーマ読込・検証以降、および旧版の更新は残る。後続レコードの先読みは行わない。
  同一の `SELECT *` に元のSQLiteカーソル処理を共用した。既存の公開意図の確認は値のコピーを
  行わず、次のスナップショット照合用の5列はテストで実DBから取得した。
  重複の分類前に最後の行まで読み、後続のSQLiteエラーを優先する。Harness全179件と
  strict Clippyが成功し、外部fixture前提の9件は未実行。先頭Profileの失敗4例は元Pythonの
  `validate_document` でも比較した。
- [実Wasm CI 44083568336](https://github.com/harupipipipi/tobkiri/actions/runs/44083568336)は
  通常カタログで `Runtime.init` 後に1,392,234,787、先頭export前に1,263,894,246 fuel残り、
  25.708秒で0になった。復旧は先頭export前に1,263,598,822、29.368秒で0になった。
  どちらも元の最初の項目の途中で、Storeとworkspace lockは解放され、最終の確保・予約は0。
  比較9件中5件は原本と一致。別の1件 `source-without-artifact-root` は30.012秒で期限に達し、
  754,712,042 fuelが残った。backtraceだけでは `Resolver.lookup` と
  `Registry.get_or_retrieve` の生存区間を区別できない。次の計測では既存のphasesを実loaderの
  `load_schema` に接続し、元の呼出しでどちらの区間が長いかを記録する。
- 最初のProfileスキーマ2点は、選んだWasmと同じsource manifestの圧縮DATAから取り出す。
  Producerは元のスキーマと実入力の長さ・SHA-256を照合し、Launcherは別途pinしたv2レシートと
  各スキーマのdigest・ID・バージョンを確認する。v1レシートの従来経路も使える。
  同じコンポーネントのdigestを確認した組だけを保持し、workspace lock内で選択済みスキーマを
  厳密JSONとして読む。Appのキャッシュ済みオブジェクト、メタスキーマ、参照先、レコード検証は後続。
  Rust Harness 180件、strict Clippy、Producer 2件、ワークフロー13件、Launcherステージングの
  独立テストを実行。実macOS CI向けのHarnessテストもローカルでコンパイル確認した。
- Profile v4/v5に加えて、元の共通スキーマとProfileコンテンツ投影スキーマを同じWasmの
  source manifestで検証する。v1・参照を含まないv2レシートは従来の読み取りを維持する。
  完全な組がある復旧経路はworkspace lock内でProfileの構造、ID・digest・重複、禁止された
  権限フィールドと参照の整合を確認して、次のLock検証へ進む。
  約26KiBの元のv4 Profileは、読み取り専用JSONを既存の確保量計算で解析し、パーサーの
  作業領域を解放することで従来の64MiB上限内で通った。元のPythonではv4・v5の成功例と
  3つの拒否例を照合し、10種のパターンは17,720例で元 `re.search` と一致した。
  構造・意味のいずれかが不一致の場合は復旧を止める。元の全診断文の再構成は後続。
  Lock v4/v5・Plan v1/v2・Activation v1/v2とFunctionPrincipal v1を、同じWasmの
  source manifestから既存レシートに追加した。共通スキーマとコンテンツ投影は1回読む。
  Planの固定ルート参照とActivation IDパターンを扱い、Lockのvariant pinとPlanのbindingの
  重複判定は既存の検証処理を共用した。Plan内のPrincipal digestを単体用の検査で再計算する
  二重処理は省いた。固定された元Pythonの依存関係では `date-time` の検査器が未登録である
  ことを確認し、元の文字列型検査を再現する。レガシーの混在判定は全レコード検証後に行う。
  元のPythonパッケージ用fixture builderから生成した現行v5/v5/v2/v2の全レコードも照合し、
  312 bindingsを含む約750KiBのデータをRust側の64MiB Store予算内で検査した。
  組み込みの旧v4/v4/v1/v1 fixtureでは生オブジェクトと文字列を試した。
  レコード間のdigest・投影・Authorityの照合と、元の全診断文は後続。
- [実Wasm CI 44086657107](https://github.com/harupipipipi/tobkiri/actions/runs/44086657107)
  の通常カタログは先頭export前に1,267,026,502 fuel、25.590秒で0。復旧は1,266,762,026、
  27.107秒で0。比較9件のうち5件が一致し、残る4件は燃料切れで、全9件が後処理を完了した。
  この実行モードはスキーマ詳細計測を飛ばした。別の
  [cold exportの計測](https://github.com/harupipipipi/tobkiri/actions/runs/44072390698/artifacts/14322583595)
  では42スキーマを最後まで読み、標本からRegistry約2.85億、URI約1.93億 fuelと推定した。
  同一Storeの実loaderでは既存Rust samplerとApp backendを一緒に検証する明示的なモードが
  必要。前回の `launcher-staging` は、fixtureが圧縮DATAを含む前の `22f6f0c74` で停止した。
- 復旧の現行レコードはProfile・Plan・Lockの実digestから照合し、元の順序で内容投影、成果物の
  選択集合、Authority bindings、Activation ID・予約・fencing token・SecurityEpochを確認する。
  bindingごとにPlan全体を走査する元の処理を、重複を保持する予算付きの借用索引に置き換えた。
  closure digestは2つの既存subtreeを順番にhashへ渡し、複製したJSON成果物を保持しない。
  中立投影は退役時のdigest walkerを共用し、保持したディレクトリのハンドルから辿る。
  旧版も元のdigest、実SQLiteの予約、epochとAuthority参照を照合する。後続は旧版の
  successor生成・発行と、現行のロック解放後の成果物再検証・Host公開。
  現行の再検証後にはロックを取り直して4レコードとAuthorityの読み直しが必要。
  既存の `RecoveryCatalogInputs` の成果物検査を再利用し、元の再承認例外と期限を保つ。
  Rust本体190件とPython生成の現行fixture 1件を実行し、後者は実SQLite予約とロックも通過。
  Pythonとの比較では3種のdigest不一致と、中立投影の内容変更・別名・symlinkの診断も一致した。
- 現行の復旧ではworkspace lock内で検証した4レコードのdigest、成果物検査に必要なShell、
  公開候補の7項目を保持する。Profile全体の二重コピーは不要。既存のdescriptorベースの
  梱包成果物検査をロック外で行い、取り直したロック内でもう一度Authorityと4レコードを照合する。
  変化があれば中断し、同じ場合だけ公開候補と置換catalog内の登録済み定義を照合する。
  現行成果物と検証済み後継を区別する。後継の再承認をUIに渡す処理とHost公開は後続。
  実ファイルfixtureで現行・後継、欠損、差し替え、候補定義の欠損・不一致を確認した。
  Python生成の現行fixtureでは、成果物検証の呼出し時にworkspace lockが解放済みであること、
  その間にレコードが変わった場合と確認済み公開候補が異なる場合に先へ進まないことを確認した。
- 検証済みの復旧候補をHostの `active.json` へ発行する。元の規則から選んだエンベロープを読み、
  保持中の公開ロックの下で署名付き意図と現在のポインタを読み直す。Host選択ロック内で
  先行ポインタ・ディスク上のdigest・5つのID・catalog revisionを確認し、既存の原子的置換を
  共用する。公開成功後に意図を消す。同じ公開の再試行は世代を進めず、進んだ別世代は拒否する。
  既存エンベロープは読み取り専用JSON解析を共用し、750KiBの入力も同じ64MiB枠で扱った。
  Rust193件と元Python生成の現行fixture 1件を実行し、元Pythonでも初回・再試行・世代違いの
  挙動を比較した。Launcher CI 44096178241は型付きSchemaValidationの網羅漏れで停止した。
  同じStore内で元のエラーに戻せない場合は、保持中のグラフをワーカー外へ渡さず型付きの
  detachment失敗を返す分岐を追加した。
- `catalog-app-frame-diagnostic` を追加し、通常のApp schema拡張と実カタログのStoreで、
  `load_schema` の前後を既存の固定checkpointsから記録できるようにした。2B fuel、
  30秒、64MiB、139件の上限は共用する。ProducerとBinderはApp拡張と両方のsource変換に
  加え実ローダーが使う圧縮内容を検証し、結果にはAppの利用を明示する。この経路から
  Launcher用のレシートは発行しない。[実Wasm CI 44103840520](https://github.com/harupipipipi/tobkiri/actions/runs/44103840520)
  では計測用のCoreテスト6件を通過し、実カタログは30秒の期限に達した。
- Pack由来の復旧投影は選択集合が固定した版を使い、生成済みカタログと外部の署名付き登録を
  検証する。選択元のNormal Packを再コンパイルし、マニフェストに記載された配下のファイルと
  descriptorから計算した一覧を照合する。退役・中立投影と同じ走査、レコード生成、digest処理を
  共用する。外部Host Extensionの互換性検証に達すると従来どおり停止する。
  Rust 198件、strict Clippy、ワークフロー22件に加え、固定元ソースをローカルPython 3.13.9
  で実行して得た署名付き投影と保持済み2版を比較した。現行の全Profileレコードによる追加検証も
  通った。CIでは独立した署名付き登録の一致を固定Python 3.13.16で比較する。
  投影と保留中のActivationレコードが個別に持つOSError整形は、対象パスと明示された例外の
  causeを元Pythonと照合してから共用できる。
- Harness Pack UIでは、停止・失敗・停止結果の未確認を別々に表示する。停止と完了が競合する
  場合、受け取った完了イベントと保存後の読込を照合する。保存を確認できない行は消し、
  入力欄の既存の下書きや送信中の編集を保持する。実SDKのMessageChannelを使った回帰と
  Pack生成を含む42件が成功した。会話の識別は現状の `e2e` を使う。既存の会話所有者
  が作るIDと、同一内容が並列書き込みされた場合にも区別できる保存結果をUIへ渡せたら、
  現在の内容・件数での照合を削除できる。実Launcherとプロバイダーを通す検証は残る。
- Appスキーマの単純なHTTPS相対参照では、元のパス結合と一致する条件で既存URIの
  ディレクトリ部と相対パスを最終バッファへ直接書く。参照解決の17,985回中13,790回が
  この形だった。Registryには毎回新しいPython文字列を渡し、ドットや空のセグメント、
  query、params、percent escapeは従来の処理を維持する。実拡張で原本と173組を比較し、
  同じ参照の連続取得で文字列を使い回さないことも確認した。ローカルのAppスキーマ42件の
  処理時間は、6回ずつの中央値で44.77→42.97ms。
  [実Wasm CI 44108246295](https://github.com/harupipipipi/tobkiri/actions/runs/44108246295) は
  独立したcold exportで42件の出力を原本と照合した。処理前の残燃料は1,979,505,186、
  42件の処理後は248,375,194で、この区間で1,731,129,992 fuelを消費した。
  同じ成果物の全カタログは従来の上限で燃料切れ、Storeを退役した。
- `78fd660ee` はAppのRust frameで `required` と `dependentRequired` を処理する。
  元のmembership呼び出し、項目と依存先の順序、重複、型検査、例外contextを保持する。
  空の依存先を挟む場合も最後のloop変数を保持し、終わったiteratorは次のmembershipより
  先に解放する。iteratorのfinalizerが次のmembership結果を変える差分を再現して修正した。
  エラー変換中は同じExecutionを保持し、SchemaError.create_fromの呼び出しが
  suspended iteratorの値を参照できるようにした。
  ローカルPython 3.13.9・元と同じライブラリ版で実拡張をビルドし、64件の本体比較、
  boundary 11群、reference 36群、object 22群と既存の追加比較が通過した。
  schema-phasesを含むビルド確認と、入力・候補の補助テスト15件も成功した。
  [App callerの原本比較CI 44123561928](https://github.com/harupipipipi/tobkiri/actions/runs/44123561928)
  は固定Python 3.13.16のLinuxでも通過した。
  元の `keywords.rs` のRequired／DependenciesとApp frameには走査とメッセージ整形の
  重複がある。両経路のiterator寿命とエラーcontextの一致を確認したら、小さい共有処理へ
  移せる。document validator全体の移植と、実Wasmの燃料・期限の検証は後続。
  候補commit `46c2f84cc` のRegistry hit処理は、Retrievedのconstructor／descriptorや
  同じ関数のcodeを変更した場合に元の呼び出しを飛ばす可能性がある。
  これらの動的な変更と、miss時のコールバック回数を比較してから採用を判断する。
  [App frameの実Wasm診断44120217693](https://github.com/harupipipipi/tobkiri/actions/runs/44120217693)
  は `3a5b95fe7` 世代（required移植前）を記録した。40 checkpointsを取得し、
  `profile_intent_v1` の検査開始後、30.009538秒の期限で終了した。残燃料は53,915,988、
  退役後の予約は0。完了済みcheck区間の差分では、defaults_setup/alias pack/inventory/
  pack_manifest/external_normal_pack_catalogの5件が706,394,875 fuelの55.4%を占める。
  checkpoint間にはimport構築も含み、keyword単独の消費や支配性を証明する計測ではない。
  `uniqueItems` はlive `_keywords.uniq` を呼び、既存比較で開始前・途中の差し替えも検査する。
  exact list/strだけで呼び出しを省く案は、この挙動を消し、改善量も未測定のため保留した。
  次は `78fd660ee` 後の同条件で期限と燃料を比較する。
  同じ観測のinstantiation前は22.05秒で、fuelはまだ2B。
  `preparation.rs` は毎回Engineを作り、`Component::new` を同じ期限内で実行する。
  Engine・読取り・hash・Store準備も含むため、この全時間をcompile単独には帰属できない。
  現在のEngine設定にはWasmtimeのcompilation cacheがなく、`core_modules::CodeCache` は
  同じEngine/catalog内のcore Module用で、NativeSetupのComponentには使えない。
  既存の共有キャッシュの利用漏れとして接続できる箇所は確認できなかった。
  [required移植後の実Wasm診断44130339328](https://github.com/harupipipipi/tobkiri/actions/runs/44130339328)
  も期限で終了した。instantiation前24.353227秒、catalog開始27.725348秒、
  終了30.022431秒で、残燃料753,733,103、退役後の予約は0。schema traceは8件で、
  tag11/27/28を終えtag29を開始した。共通の3 schema区間では旧計測から性能改善を
  確認できなかった。期限とcold準備時間が異なるため、完了schema数だけの優劣比較はしない。
  requiredの原本比較が成功したことと、全体が実行上限に収まることは別々に確認する。
  Appの定義自己検証を純JSON evaluatorへ限定移植する案も検討した。exact JSON入力と
  singletonのidentityだけでは、META/Registry内容やFormatCheckerのmutable登録表は固定されない。
  既存比較には、callbackが入力やhelperを変更する場合がある。現Reference/Value policyだけで
  既定META全体を選別する狭い境界はまだない。invalid時だけ元Frameでエラーを作る案でも、
  先行検査の副作用・残予算・期限を維持し、例外や予算切れ後に再実行しないことが必要。
- 旧Activationのsuccessor解決は、元のレコードを照合したworkspace lock内で、同じ
  Authority Storeと置換catalogを使う。既存Setup resolverの入力は一部の旧authority-ref
  を拒否する。旧レコードのepoch 0は実Authorityとの照合を通過しない。既存のartifact検証を
  再利用できるのは、successorを解決・照合した後、workspace lockを一旦離す発行準備段階。
  承認済みの先行状態と署名付き公開意図を別々に扱い、元の処理どおりworkspaceの移行後に
  Hostの公開候補を比較する。
  Rustの復旧経路を、元のワーカーで保持するAuthority Store・workspace lock・検証済み
  レコードを貸し出したまま待機できる形にした。SQLite接続は従来どおり各操作で閉じる。
  カタログのmutexは同期のHost検証中だけ保持する。中断時も既存のdescriptorとロックの
  解放処理を共用する。Rust199件、strict Clippy、元Python生成の現行fixture 1件、
  固定旧ソースのレガシー移行・ドリフト拒否6件をローカルで実行した。
  `6cbf902f2` で復旧専用のPack呼び出しを接続した。検証済みの置換catalogからShell入力を選び、
  保存済みのauthority-refと元のedge順、snapshotとepochを同じStoreのresolverに渡す。
  後継候補のclosure digest、BaseとShell、principalのbinding、権限scopeをworkspace
  lock内で比較し、元の再承認エラーを保持する。Rust 203件とPython 243件が通過した。
  固定元ソース `1cf025f1e` が生成した46 Pack・176 bindingのレガシーfixtureでも、
  同じAuthority Storeを読むRustテストが後継比較を通過した。
  macOS・Linuxの定義CIでも、固定元ソースで旧Activationを実際に移行し、生成した後継を
  Rustの照合テストに渡す。後継artifactの検証はworkspace lockを離し、同じStoreと
  公開guardを維持して行う。`4e8ddd79e` は再取得した後継の完全なplan digestと一致する検証証跡を
  次のworkspace移行に使う。SQLiteとlifecycleのハンドルは各操作で閉じる。
  `original_activation_authority.rs` は元の予約・CAS・退役・監査を、
  `original_legacy_activation.rs` は4段階のenvelope保存とpending更新、2回のartifact
  identity検査、失敗時のabortと削除を実行する。committing段階でready時のpending
  digestを残す順序も元の処理と揃える。移行後の再読み取りとHostの候補照合まで接続した。
  ローカルの原本生成fixtureでは、Rustが保存したProfile・Lock・Plan・Activationが
  元Pythonと一致した。権限世代の変更、保存前後のartifact変更と、保存後に残ったpending
  からの復旧も検証した。Rust 220件、Python 243件、CI feature付きlibraryのstrict Clippy、
  workflow・CI補助52件が通過した。`eeec29fd8` は
  `original_external_catalog_io.rs` のtests moduleを末尾へ移し、tests込みのstrict Clippyと
  関連20件を通過した。移動したテスト本体と製品コードの一致も比較した。
  CI44130220241はsource staging 4件の後、全workspaceのfmt checkで別の改行差を検出した。
  `4d7593c32` で `original_recovery_pending_reservation.rs` の `read_state_with` 呼出を
  formatterの形に揃え、ローカルの `cargo fmt --all -- --check` に通過した。処理は変更しない。
  定義CIのcatalogテスト数指定も実行済み8件に合わせた。
  実Wasmを通す復旧全体と、元GUIからの会話を含む製品検証は残る。
  現在のresolver結果は `bootstrap_catalog.rs` でscopeと形式を確認し、
  `original_recovery_legacy_successor.rs` で既存schema検証用に再エンコードする。
  scopeの検証、確保量の追跡と元のschemaエラーを維持できれば、同じ解析結果を
  schema検証へ渡して中間エンコードを省ける。
  successorのprofile/lock/planはimmutableな共有リンクで保存側へ渡し、Python側のdict
  複製を置き換えた。再検証では元の3つの比較fieldを同じ候補から投影し、既存の
  committed reservation照合を共用する。Directoryの固定ファイル書き込みも共通化し、
  activation保存で不足する親を作成する。公開・定義保存では親の存在を既存のとおり要求する。
  実際の大きな復旧envelopeで従来の汎用decodeの予約量が容量に達したため、読み取り専用の
  正確な領域計測を使う既存decoderへ置き換え、同じ64MiB内でpending復旧まで検証した。
  `original_recovery_abort_transaction.rs` と新しいgeneral transitionはabortのCASと監査を一部重複して
  実装している。原本の不正row診断とrollbackを両方の経路で照合したら共用できる。
- 初回Setupの保存は `profile_capture.py::_capture_bootstrap_profile` が発行lock内で原本を
  再読込みし、journal、定義、workspace、activation、Host pointerの順に進める。
  `974a31bbd` で4段階activation journalのID決定を呼出側へ移した。従来の移行callerは
  同じmigration IDを生成し、初回IDの場合も同じjournal・pending・失敗時処理を使える。
  成功と5つの中断箇所を両IDで検査した。初回の定義登録、workspace作成、発行は未接続。
  `31ab3c755` は元のSetup decisionで一致したときに、同じresolverのProfile・Lock・Planを
  Native workerへ返す。確認済みrevision、Authority snapshot、epoch、束のdigestを照合し、
  Profile全体、LockとPlanの自己digestを除いた本文を既存の予算内で再hashする。
  既存の確認lockとcleanupを維持する。Tauriの表示には従来の結果だけを返し、保存はまだ行わない。
  Rust 220件、strict Clippy、Python 243件、workflow補助14件が通過した。
  原本CIのjournal比較に、実Pythonが発行直前に使った3レコードとRustが受け取るcanonical bytesの
  比較を追加した。[原本比較CI 44233355545](https://github.com/harupipipipi/tobkiri/actions/runs/44233355545)
  はLinuxとmacOSで成功し、Linuxで確認document、3レコードと署名済みintentのbytes、
  activation候補の照合を通過した。
  `af567159e` は中断した発行の復旧を、同じ発行lockとworkerで確認済みSetupに接続する。
  未確定の作業を片付けた場合は、そのlockを保持して元の候補確認を続ける。保存済みの候補は
  snapshotと登録定義の照合後に元の確認digestを確認し、一致した場合だけHost pointerを発行する。
  発行済みの結果はSetupの再実行を起こさず返す。失敗時は既存の終了処理でlockを解放する。
  Rust 221件とstrict Clippy、Python dispatcher 87件、workflow補助14件が通過した。
  既存Launcherからの採用、初回の発行全体と64MiB内での実Wasm確認は後続。
  `df0674b38` は `bootstrap_catalog.rs` のHost catalog構築をAuthority復旧から独立させた。
  定義、Pack依存、同じRuntimeで置換されたcatalogの検証を共用でき、既存復旧は同じ参照と
  呼出元のままAuthorityへ進む。Rust 221件とstrict Clippyが通過した。
  初回発行への接続では `host_profile_catalog` の完了後に元Bundleを別にロードし、元Pythonの
  `profiles[profile_id]` と解決前パスからのLock生バイト読み取りを再現する。既存resolverの
  `.get` と読み取り順序が異なるため、同じ関数への統合は両者の例外と順序を確認してから行う。
  `c31ededfa` は確認後のHost catalog構築、別のBundle load、元Profileの添字取得と
  生Lockの再読、確認済み定義とBundleの両digest照合を同じ発行lock内へ接続した。
  保存先の再取得がlockと異なる場合は定義操作前に止め、復旧時の例外変換は従来の復旧に適用する。
  一回限りの読み直しには現在の原本で同値のresolver getter IDを使う。capture先頭の独立した
  getter呼出は後続。journal作成以降の保存と実Wasm上の連続実行は未検証。
  Rust 221件とstrict Clippy、PythonのHarness Native 248件が通過した。解決後に親のaliasが
  変わる場合、同じ要求の再実行、古い参照、参照解放、元のKeyErrorを確認した。
  `bfa524be0` は同じ発行lockの内側で照合済みの候補と最初の承認を比較し、保持したHost
  pointerを含めて初回journalのpayloadを組み立てる。別Profileを選択済みの場合も、
  ロック取得時のpointerを参照する。元Pythonを固定コミットから実行して得た署名済み
  journalと、前任なし・別Profileありの両方でハッシュとpayloadが一致した。
  現在の本番経路はpayload構築までで、鍵・journalの書き込みはテスト経路で検証した。
  `b6f049a63` は通常の定義登録をテスト経路に移し、既存の初回作成・世代CAS・比較・
  履歴追記を共用した。同一内容の再実行、作成競合後の再読、未変更テンプレート以外の
  更新拒否を確認した。初回のjournal、通常・inactive定義登録、workspaceを既存の
  activation処理とつなぐ段階が残る。Rust 225件、原本比較1件、strict Clippy、
  workflow補助22件が通過した。ローカル原本比較にはPython 3.13.9を使用した。
  初回保存も既存の4相writerとHost pointer発行を再利用できる。保存前にProfile→Lock→Planの
  schema検証を行い、既存のrecord graph検査からActivationに依存しない部分を共有できる。
  仮のActivationを作らず、原本どおりrecover後、artifactの検証と予約の前で3レコードを検証する。
  `43d953f28` は初回発行の元Bundle再読込でも、同じloaderが実際に取得したPack pinを保持する。
  原本は最後に独立ロードした同梱catalogのProfileとShellを参照する。発行のartifact照合は
  このpinと再読込済みProfileを使う必要があり、Host catalogへ追加された外部Packは含めない。
  `4ec69ca5f` は既存の復旧と旧版activation直前のpending処理を共用する。ロック内で取得した
  journalを使い、既存の予約状態に応じて中断を整理するか、ロックを解放して再発行へ進む。
  `3f22710d3` はProfile・Lock・Planのschemaとgraph検証から、保存済みActivationへの依存を
  外した。元Pythonの実resolverが返した3レコードで、実Authorityの予約、4相writer、
  ワークスペースからの照合済みの再読込に成功した。実Packのハッシュはこのテストでは
  専用の検証関数に置き換えてあり、実Launcherと発行処理への接続が残る。
  Rust 226件、原本データを含む追加の検証2件、strict Clippy、workflow補助22件が通過した。
  `c09838e77` はShellの実ファイルの同一性取得、Packの指定と内容の照合、同一性の再検査を
  既存復旧と初回activation用で共用した。初回の照合には、同じ再読込loaderが保持した
  Pack pinと同梱Profileを渡す。先に同一性を記録し、別の5秒枠でPackを検証してから
  再検査する。全段階で元の呼出期限も適用する。
  実ファイルのテスト用Shellで、同梱Profileの指定が変わった場合と、内容が同じ別inode
  へ照合後に置き換わった場合を拒否した。Rust 226件、Shell照合のCI相当5件、strict
  Clippyが通過した。初回の入口はjournal・定義・workspace・最初のrecoverの後に
  この検証を呼ぶ段階で接続する。検証済みの同一性は予約commitとpointer書込の前に再確認する。
  `f3e0a5b7b` は承認済みの初回Setupを、同じ発行lock内で署名journal、通常・非アクティブ
  定義登録、workspace、Authority、Shell照合、4相activation、Host pointer CASまでつないだ。
  初回recoverと予約直前のrecoverを既存処理と共用し、確定済みpendingはworkspace lockを
  解放した後に照合する。Host pointerの切り替えは照合済みsnapshotの再読と候補一致の後。
  原本が解決したProfile・Lock・Plan、再読したShell定義・Pack pin、実ディスク上のShellを
  使い、非アクティブ定義更新、前任pointer付きの通常登録、Shell欠落、既存定義競合を検証した。
  通常登録のfixtureは、前任Profileは有効で、対象の定義がまだ登録されていない状態を作る。
  エラー時は署名journalを保持し、前任のpointerと予約状態を保つ。Rust 226件、原本比較3件、
  strict Clippy、workflow補助22件が通過。通常のLauncher画面からの呼び出し、実Wasmでの
  全体実行、既存active/ソース更新、並行して旧版snapshotに切り替わった場合の原本同等の復旧は残る。
  `bb9b623f5` はネイティブの初回発行と復旧発行の完了を、Launcher既存の監督付きKernel
  再起動へ接続した。ワーカーの後処理を通過した発行結果を使い、レビュー開始時の管理対象
  世代が一致する場合だけ引き継ぐ。監督処理が従来のHost契約再取得とパネル再認証を行う。
  後処理失敗や未確定の結果からは再起動を要求しない。発行後の再起動失敗は状態の再読込が必要。
  この入口はまだ通常のSetup画面に公開していない。実Wasmの実行上限内での確認と、同じ
  管理対象起動で旧HTTP確定を制限してRendererからの別経路を閉じる作業が残る。
  切替の判断は `KernelManager::start` の管理対象世代にひも付ける。初回と既存Profileの
  再確認は同じbootstrap契約を使う場合があるので、選択は検証済みの永続Profileの有無で行う。
  `host_contract.json` は同じProfileで値を引き継ぐため、起動ごとの選択は保持させない。
  `sealed_python` のKernel専用起動引数と既存の起動証明を使い、Python側と選択を一致させる。
  世代0の再利用Kernelと、承認中に管理対象を失った世代はネイティブ確定の対象外。
  確認済み処理中の失効は、KernelManagerのlockをまたぐ形で判定せず、既存の
  `OriginalBootstrapInputs` の生存・所有者検査に世代の失効を伝える形を検討する。
  元のSetupと隔離確認のフロントエンド25件、整形と差分の検査が通過した。
  クリーンな一時チェックアウトのPython 3.13.9で旧Setup APIの47件が通過した。
  [macOSの再起動・完了結果テスト 44242425667](https://github.com/harupipipipi/tobkiri/actions/runs/44242425667)
  は実WebKitでの隔離レビューと、実子プロセスの再起動引き継ぎを含む元Launcherの4件、
  portable契約の各ジョブで通過した。
  [原本のSetup resolverと初回発行 44241344745](https://github.com/harupipipipi/tobkiri/actions/runs/44241344745)
  はLinuxとmacOSで通過した。
  `195d99fc0` は封印済みKernelの起動証明で初回Setup経路を照合する。macOSの明示的な
  `native-first-setup` ビルドで完全な同梱レシートがあり、検証済みactive Profileが存在しない
  場合だけ選ぶ。起動時のnonceと実子プロセスの監督を再利用し、パネル用コードの取得前と
  確認開始前にポートを保持するPIDを照合する。子の停止や失効はワーカーの既存所有者検査へ届く。
  Python側は同じ起動で選択を保持し、旧確定要求には既存cookie・Origin・CSRFを検証した上で
  `activation_denied` と空のwrite setを返す。GETは従来どおり。Host契約への値追加は不要になった。
  実HTTPの旧Setup拒否とhandler更新、旧Setup API、封印ランチャーの組合せをクリーンな
  チェックアウトで50件確認。通常配布では、GUIと実Wasmの一連の検証が終わるまで機能を選ばない。
  [macOSの起動・境界回帰 44245333980](https://github.com/harupipipipi/tobkiri/actions/runs/44245333980)
  は元Launcherの9件と実WebKit、portable契約が通過した。
  `3fcf6c5ac` は既存Setup画面で元の選択を終えた後に隔離レビューを起動する。親画面は
  確認アンカーを保持し、旧確定操作はLauncherが明示した旧経路だけで有効にする。
  IPC応答喪失と発行済み結果の両方を従来の読み取り専用復旧へ渡し、失敗時の再レビューは
  新しいアンカーを使う。隔離ビューは待機中に表示位置を更新し、結果を得たレビューだけを
  終了する。確認が済むまでの操作回数は元のまま。画面・設定・Launch・接続のNode 99件と
  型検査が通過した。[統合のmacOS回帰 44246338408](https://github.com/harupipipipi/tobkiri/actions/runs/44246338408)
  も通過した。`40a26c614` はTauriのIPCがパスのないOriginで照合する通信経路に対応した。
  許可はmainのloopbackに限定し、正確な `/panel/setup` とポートを実WebViewで検証する。
  [境界修正のmacOS回帰 44246956670](https://github.com/harupipipipi/tobkiri/actions/runs/44246956670)
  も通過した。実DMGでSetupから会話まで通す検証は残る。
  実コンポーネント [44236773225](https://github.com/harupipipipi/tobkiri/actions/runs/44236773225)
  では普通版Wasmの準備が20.82秒、`Runtime.init` 終了が23.38秒、残fuelが約14.37億だった。
  原本の承認後再読を保持するため、レビュー時と確定時でワーカーはそれぞれ初期化する。
  同一呼び出しで初期化を繰り返してはいない。guestのカタログ処理の燃料と、Hostコンパイルの
  所要時間は別の制約なので、`217860f53` で同一予算内のsource読み取り・照合と
  `Component::new` の区間を測る。ローカルの準備テスト5件が通過し、
  実測の初回はworkflow内の呼出順チェックが引数追加前の文字列を要求して停止した。
  `51fcde445` で実際の世代検査を順序チェックに含め、workflow・archiveの19件が通過。
  [通常版Wasmの再計測 44245976443](https://github.com/harupipipipi/tobkiri/actions/runs/44245976443)
  は取得・照合0.159秒、コンパイル20.343秒、`Runtime.init` 終了22.767秒だった。
  原本Profile IDとroot getterを取得して正しく後処理し、カタログはこの試験では実行しない。
  25,123,780バイトの照合済み成果物は、core data約17.53MB、core code約6.76MB、
  名前のcustom section約0.47MB。読取・照合は準備時間の1%未満。名前の除去による
  コンパイラ区間の効果は未計測。
  `c09245233` はホスト側 `wasmtime-internal-cranelift` のテストビルドだけを最適化し、
  同じrunner、封印済みWasm、ゲスト `OptLevel::None`、30秒・2B fuel・64MiBで比較する。
  [同一成果物でのコンパイル比較 44256380358](https://github.com/harupipipipi/tobkiri/actions/runs/44256380358)
  では `Component::new` が20.825秒から12.428秒へ40.3%短縮。初期化終了は24.260秒から
  14.585秒、両方で残fuelは1,436,785,285、準備のピーク計上は25,127,094バイトだった。
  原本getterと後処理も通過。`1d28af0fd` は計測済みの設定を通常テストとLauncher側の
  開発・配布ビルドへ適用し、比較用の一時workflow分岐を削除した。
  [最適化後のLauncher・WebKit回帰 44259277046](https://github.com/harupipipipi/tobkiri/actions/runs/44259277046)
  はmacOSとportableで通過。配布用ビルドの実測は残る。
  [通常カタログの再計測 44259276687](https://github.com/harupipipipi/tobkiri/actions/runs/44259276687)
  は、単独ケースで初期化12.971秒、先頭export前に1,307,611,853 fuel、18.278秒で燃料切れ。
  9件中、早期拒否の5件は原本と一致し、カタログを読む4件は17.141〜26.198秒で同じ
  `catalog-load` の燃料切れだった。全件で後処理を確認、最大計上量は56,383,736 / 67,108,864 B。
  停止区間は `packs/defaults-basepack.pack.v4.json` の読取り後から次のファイルを読む前まで。
  この間に `bundle_catalog.py` が元のdigestを照合し、`validation.py` が文書スキーマと全参照用の
  `_schema_store()` を初期化する。区間内の各処理の燃料は未計測。
  `original_external_catalog_compiler.rs` のRust構造・意味検証を同梱カタログに再利用する候補がある。
  外部CAS専用の承認と例外は同梱処理へ持ち込まず、同梱用のエラー詳細と順序を原本と比較する。
  読取順、ダイジェスト、pinの独立再読、既存上限と後処理を維持して実カタログを再検証する。
  `3cc640b09` は既存のRust構造検証を共用し、同梱5種類の元スキーマが使う暦日、相対パス、
  `maxItems` を追加した。同じ予算下の読み取り専用decoderで、同梱ロックの305件すべてを
  1件ずつ検査した。日付・実行パス・Shellの3変異も、CPython 3.13.16の固定原本と同様に拒否。
  Profile v4の `requested_edges` は入れ子のauthority参照も既存の許可リストに照合する。
  `7b332e9e7` は`catalog-app-frame`の実Wasm比較経路に限定し、CatalogIoが実際に得た
  ダイジェスト照合済みの初回読取りに、Rustの構造検証結果を結び付ける。対象は元の9スキーマを
  使う5種類。元のslot・invocation・lock順、種類・digestを同じ読取りで照合し、結果を1回だけ
  返す。別の文書と後続pinの独立再読には転用できない。スキーマの確保も同じ予算に計上する。
  対応する構造は元の意味検証を引き続き実行し、対象外の構造は元の検証で例外詳細を得る。
  固定CPython上の比較は全305文書と453回の読取り、構造・意味・digest・pinの各失敗で一致。
  Rustの実ファイル試験は読取り後の差し替え、別序数・種類・digest、結果の再利用を拒否した。
  [実Wasmの同一上限での比較 44304950274](https://github.com/harupipipipi/tobkiri/actions/runs/44304950274)
  ではコンポーネントの生成に成功し、同梱・復旧両方の入口は 149 inspections、文書序数27まで進んだ。
  9件のうち先行拒否5件は元の結果と一致し、読取りを進めた4件は序数27または32で燃料2Bを使い切った。
  9件とも後処理を確認し、最大計上量は約58.62MBで64MiB以内だった。製品入口は実Wasmの完走と
  文書エラーの照合を待つ。物理カタログ読取りのテストは9件すべて成功していたが、CIが旧件数8を
  要求していたため失敗した。`7520ed17c` で期待件数を9に更新し、同じ検証ラッパーで通過した。
  同梱入口の最後の読取りは序数26の `rumi_ai_strategy_runtime_pack.pack.v4.json` で、305文書
  のうち27件、213,799バイトだった。独立したpin再読には未到達。次に汎用isinstanceを繰り返す
  意味検査の走査と、物理応答の復号・digest計算を区別するため、既存のread、shape、次のresolveの
  インポート入口で燃料を比較する。機能や上限の変更はこの失敗の解消条件に含めない。
  `029093528` は登録済みのactive ProfileをSetupレビューで読み、既存のAuthorityと
  workspace復旧を共用する。同じ初回ロードで取得した同梱のProfile、Shell、Pack pinを使う。
  ロック外でShellを検証し、変更なしの場合はスナップショットを再読する。Authorityを閉じた
  後に、保持していたHost pointerと4つの識別子を比較する。Rust 227件が通過。元ソースから
  生成したmacOS fixtureで、実Shellの正常な再読と、別activationの真正なHost pointerの
  拒否を確認した。両ポインタに書込みは発生しなかった。アクティブ定義の比較方針には
  認証済みのProfile全体とPlanが必要で、カタログ更新、旧形式移行、確定処理は後続。
  [原本と現行スナップショット 44261201051](https://github.com/harupipipipi/tobkiri/actions/runs/44261201051)
  はmacOS・Linuxで通過。`6ca80213d` は、登録済み定義、認証済みの完全なProfile、定義digestを
  次の判断に渡す。元のShellを確認できたときだけ、2回目の読取りの照合後に最初の完全なPlanを
  渡し、Shellの再承認が必要ならPlanは渡さない。別経路と再読の記録は従来どおり小さい投影を使う。
  ローカルでRust 227件、strict Clippy、固定原本とCPython 3.13.16によるmacOS fixtureが通過。
  完全なProfile・PlanとShell再承認、異なるHost pointerの拒否、64MiBの共通上限に計上した予約の
  解放を確認した。Pack pinと追加Packの照合を含むカタログ判断と、製品画面の切替は後続。
  `261fd6a93` は同じ読取済みcatalogのPack pinから、実際の登録済みSetupの選択を判定する。
  検証済みのBase・Shell・選択Packが不足するか、同梱IDに異なる明示pinがあれば入場判定に進む。
  同梱集合を使う場合は稼働中Planのbindingをすべて照合し、Shellの再承認時だけ旧Planを使わない。
  既存の外部closureは同梱IDの置換を認めず、exact版の読み取りは単独の実行定義だけを扱う。
  登録済みの全pinと依存をまとめて検証する接続、定義の更新方針、任意Packの過去の承認照合は後続。
  macOSのRust 231件とstrict Clippy、固定原本の物理Shell／Authority fixtureが通過した。
  原本SetupのLinux CIにも、同梱文書の構造検証と登録済みカタログ選択の各2件を加えた。
  `b7edfdb4e` は同梱catalogの登録済み定義を、認証済みactive側の定義digestと照合する。
  不一致時だけレジストリを再読し、固定した同梱Profileを使って元の2種類の更新提案を再現する。
  直接の親と、その親の実データと更新結果のdigestがすべて一致するときに限り中断更新と認識。
  数値と真偽値を含むPythonのdict比較を共用する。正確なPack pin、任意Packの承認、最終候補への
  適用は各後続経路。Rust 235件、strict Clippy、物理Shell fixture、原本の再承認19件が成功。
  `330b442c8` は登録済みSetupで、照合済みの現行定義または中断更新の親から候補を組み立てる。
  元のShell変更とscope更新の順序を保ち、明示的な追加要求だけを合成する。同一内容でbinding更新も
  ない追加要求は元の拒否factoryへ送り、衝突時は元のクラスとcauseをHost内で保持する。
  初回に読んだ同梱manifestから依存IDだけを確保し、検証済みactive選択から候補の宣言Packと
  activeのBase・Shell・選択Packが要求する依存を除く。任意Packは候補の必須Packに昇格させない。
  欠落した依存目録を空として扱わず、同梱catalogの完了、所有者、invocationと取消経路を照合する。
  候補と任意Pack IDを同じreview内で保持する。候補のWasm受け渡しには、既存の復旧・previewと
  異なる、最初のProfile getterを使った照合が必要。任意Packがある場合はその承認・成果物照合が先。
  登録済みexact closureの次の実装では、全pinによる署名済みレコード選択、単一pinでのroot再読、
  変更manifestの実行目録再読を元の順序で保つ。既存の復旧用closureを流用できるのは個々の検証のみ。
  ローカルでRust 240件、登録済みSetupのCIコマンド6件、strict Clippy、物理Shell fixtureが通過。
  [原本との比較CI 44307082668](https://github.com/harupipipipi/tobkiri/actions/runs/44307082668)
  はLinuxとmacOSで通過した。
  `5a3b98a94` は任意Packがない登録済み候補を、最初の実getterと元の発行済みcatalogに結び付けて
  同じStore内の `catalog_with_profiles` に渡す。別のProfileとPack目録は同じデータを保持する。
  新しいcatalogの発行・所有を確認してから、元の順序でユーザーデータと物理バンドルロック、
  Authorityを再読し、2回目の実getterで新しいcatalogを検索する。新規Setupと重複していた
  この順序は共用した。Shellの物理入力は新規Setupの同梱定義から選ぶ経路が残るため、登録済み
  候補のShell選択とPack pin predecessorの照合を加えるまで、解決後の確認と保存へは接続しない。
  Rust 242件、strict Clippy、固定原本の物理Shell fixture、CPython 3.13.16の255件が通過。
  153 Packを持つ固定原本のcatalogを使ったCPython比較で、上書きと2回目getterの検索も一致した。
  新しい登録済み候補の経路を含む実Wasmと、Setupから承認ウィンドウまでの実機検証は後続。
  `416b6cc07` はこのcatalogを、静的・動的edge、成果物集合、解決、後継判定の再取得、previewへ
  一貫して渡す。同じProfile IDなら登録済みのShell指定を使い、2回目のgetterが別IDなら、その
  IDの同梱定義を使う。物理バンドルとShell定義は元の読取りを共用し、同じIDでShell指定がない
  場合は同梱Shellへ戻さない。定義とpointerが一致してPack pinの再読が必要な場合は引き続き
  `BootstrapPredecessorActivationNotBound` で止まる。ここではresolver側のrootでAuthorityを
  改めて開き、確定済みの4識別子と最終候補のShellを照合する必要がある。候補準備時の記録の
  再利用は避け、復旧と更新の読取り処理を共用できる箇所を検討する。任意Packの承認照合、外部
  Pack closure、登録済み置換の確定は後続。実Shell経路の代替候補拒否と一回限りの証明を含む
  Rust 242件、strict Clippy、Python 255件、初回確定の物理fixtureが通過した。固定原本で153
  Packと物理macOS Shellを読み、登録済み候補から確認文書とpreviewを得る原本側の試験も通過。
  [実Wasmの燃料計測 44310299404](https://github.com/harupipipipi/tobkiri/actions/runs/44310299404)
  は `994b070e9` を計測した。先行拒否5件が一致し、読取りに進む4件は燃料を使い切った。
  9件とも退役後の確保は0、最大58,631,132 B。同梱正常系の単独計測では305文書の28件目を
  読み終え、pin再読へは進まなかった。catalog export直前の残燃料は1,262,410,526。
  完了した27文書でread→shapeは385,870,206、shape→次のresolveは332,895,039を消費し、
  合計はexport内の約57%。これはインポート入口間の差分で、ゲスト側の応答復号、digest計算、
  構造確認後のstrict parse・意味検証、次の要求を含む。次は同じ物理read・序数・digestを検証する
  Rust側で構造検証時の文書を意味検証と投影に共用し、base64の往復と二重パースを減らす。
  root→最初のis-symlinkは238,292,598、ロックread→最初のresolveは112,678,538。懸念される
  遅延importの内訳は切り分けて測る。上限は2B・30秒・64MiBのまま。
  `802b7dc6e` は比較経路の初回物理readにdigestとRustの構造検証をまとめた。成功時は同じ
  UTF-8原文を文字列で渡し、Pythonの厳密検証、base64、digest再計算と別のshape呼び出しを省く。
  オブジェクトの順序と元の意味検証は維持する。検証対象外は同じreadのbytesを返して
  元の例外と診断を出す。応答の4MiB上限と旧byte応答で許された入力サイズを維持し、
  executable pinは毎回ファイルを独立して読み直す。Rust 247件、strict Clippy、
  Python 262件とproducer依存テスト6件が通過。固定CPython 3.13.16で応答を模擬した比較では
  305文書と453回の読取り順が一致し、digest・スキーマ・重複キーの失敗診断も一致した。
  [実Wasmの再計測 44322944319](https://github.com/harupipipipi/tobkiri/actions/runs/44322944319)
  は同じ2B・30秒・64MiBで、同梱正常系の初回read完了が28件から37件に進んだ。
  先頭27件の同じインポート区間で895,281,935から733,349,666 fuelに減少。9ケースのうち
  先行拒否5件が一致し、読取りに進んだ4件は37・46・37・47件で燃料を使い切った。
  すべてpin再読の前で止まり、期限超過はなく、最大計上量58,500,598 B、退役後の確保は全て0。
  意味検証では文書全体の2回の走査をRustで共用できる。
  外部Catalogの意味検証を使う際はShellの禁止キーを補い、Pythonが文字列化する入れ子bindingsは
  一致を確証できる型だけを処理する。Profile v4の有効な要求scopeにリスト型の識別要素を
  含む実例があるため、確証できなければ原本の意味検証へ渡す。148回の物理pin再読では
  同じ読み取りからdigestを返すことで、約1.08MBのbyte応答と再ハッシュを省ける。
  `73a6ecac6` は構造検証済みの同じ文書で、同梱5種類の意味検証をRustに移した。Packと
  Profileの禁止フィールドは既存の識別子・重複検査と同じ走査で確認し、Shellは元の
  大小文字とハイフンの正規化に基づく禁止部分文字列も確認する。非ASCIIのShell keyや
  表現できないbindingの文字列化は元のbytesとPython検証へ戻し、例外の順序を保つ。
  永続化済みProfileでは従来どおりの検証順にする。Rustで同梱305件と変異を検証し、
  固定CPython 3.13.16でID・承認項目・重複operationの拒否と、整数やobjectのbindingを持つ
  有効な入力を照合した。Rust 248件、strict Clippy、Python 262件、producer 344件が通過。
  [意味検証を含む実Wasm CI 44324536644](https://github.com/harupipipipi/tobkiri/actions/runs/44324536644)。
  同梱正常系と復旧の単独計測は58件（315,462 B）まで進んだ。最初の27件の同一区間では
  733,349,666から525,785,909 fuelへ減少し、9ケース中の読取り4件は58・62・57・63件で
  上限に達した。pin再読前、期限超過なし、最大58,238,454 B、全9件で退役後の確保は0。
  先行拒否5件は原本と一致した。計測対象は`f88735b2e`。
  `d1d9dd599` は同梱比較経路で、ロックの生パスと序数を照合してから初回のパス解決、
  root外拒否、symlink検査、open、文書検証を一度の呼び出しで行う。固定CPythonに合わせて
  区切りや末尾slashを正規化し、`..` を残す。中間パス欠落、ENOTDIR、ループ、NUL到達時の
  errno・filename・優先順を実ファイルで比較した。ピンは独立したFDで読んだdigestを返し、
  ファイル差し替え後にそのdigestが変わることを確認した。旧8,192バイトのパス上限と
  旧byte応答の上限を検証し、試行した初回readとpin数を別々に燃料計測へ出力する。
  305文書と148ピンの順序は模擬応答で固定原本と一致。Rust 249件、strict Clippy、Python
  262件、producer 344件、CIスクリプト14件が通過した。
  [物理I/Oをまとめた実Wasm CI 44328024810](https://github.com/harupipipipi/tobkiri/actions/runs/44328024810)。
  `94a26fbf8` はCatalogIoの専用OSエラーで、固定CPythonのUTF-8ファイル名reprを生成側と
  照合側で共用する。コードポイント用の配列は作らず、既存予算で走査と出力を計上する。
  libcのエラー文字列も既存の有限コピーを共用する。実際の日本語・絵文字・NBSPを含む
  欠落パスと、引用符・制御文字を含む例外の型、表示、ファイル名、ポリシー、確保の返却を確認。
  非UTF-8のUnix名は別途surrogateescapeに対応する伝送が必要。305文書すべての原文、
  独立した148件のpinとartifact rootを、同じ物理fixtureの呼び出し期限内で検証し、確保量は0に戻った。
  Rust 251件、strict Clippy、固定CPythonテスト263件、CIスクリプト14件が通過した。
  `9f359850b` はダイジェスト・構造・意味を照合した同じ文書で、Base/Shellのrevisionを
  指定フィールドを除いたcanonical bytesから照合する。検証済み応答ではゲストの再計算を
  省き、古いrevisionや一時領域不足では元の文書を返して同じ制限下の原本診断へつなぐ。
  実行目録の事前検査をRustに実装し、Packの存在、source identity、canonical digest、
  manifestのpin件数を原本順に確認。末尾LFを含めpinの生表記を保持し、物理再読のdigestと
  Lock・manifestを照合した。現行の実行目録側は物理試験で検証し、製品経路への接続は後続。
  同梱3 revision・148実行目録、失敗順、古いBaseの同一バイト応答を確認した。
  Rust 253件、strict Clippy、固定CPython 263件、producer 344件、CIスクリプト14件が通過。
  `a67b2ba45` は検証済みの種類別IDを物理ロックの順で保持する。ロック件数に基づき
  同じ予算で領域を確保し、別種類で同じIDを許容する。生バイト応答が出ると索引を破棄し、
  後続の型付き文書も元の重複検索に通す。フレーム確定に失敗した場合は追加済みIDを戻し、
  同じ序数を再読できる。実ファイル305件でID順とartifact root後の索引を照合し、重複、
  確証できない有効なPack、応答末尾の上限超過を検証。Rust 254件、物理カタログ14件の
  CIラッパー、strict Clippy、固定CPython 263件、producer 344件、CIスクリプト14件が通過。
  [revision・ID索引込みの実Wasm CI 44334991562](https://github.com/harupipipipi/tobkiri/actions/runs/44334991562)。
  `996aba9c6` は選択済みsourceからRustでロックを読み、305文書と独立した148 pin再読を
  行ってからartifact rootを解決し、順序付きの原文目録を所有する。読取済みPackは保持した
  原文から照合し、pinだけ元の字句パスからファイルを開き直す。ロック、文書、索引、パスは
  同じinvocationに計上し、目録を落とすと読取り前のbytes・予約数に戻る。固定原本と照合した
  欠落ロック、長いパス上の型・digestエラー、重複Base、Pack不在を物理試験で確認した。
  Rust 255件、カタログCIラッパー15件、strict Clippy、CIスクリプト14件が通過。
  この直接読取りはテスト経路で検証した。未表現のschema診断と、Launcherからの接続は後続。
  製品側でPythonのcatalog loaderを外す位置は`bootstrap_catalog.rs::load_bundle`のsource ownerと
  実manifest確認の後。Rustが全5種類の順序付き目録を所有し、文書の識別子重複、Base/Shellの
  canonical revision、実行目録のsource identity・canonical digest・pinを確認してから渡す必要がある。
  復旧はnative目録合成後にAuthorityを開き、新規Setupのresolverは従来のAuthority snapshotに依存する。
  接続時は既存Runtimeの遅延import・永続化ガードの後、固定catalog入口から同じbindingと
  invocationでRustの読取りを開始する。検証済み原文を序数付きで渡し、正規の
  `BundledCatalog` を構築して全行の消費を確認する。resolverに必要なShell・Pack pin・
  選択ルートの情報を同じ検証から保持し、照合用にファイルを余分に開かない。
  [grouped版の実Wasm計測 44328024810](https://github.com/harupipipipi/tobkiri/actions/runs/44328024810)
  は先行拒否5件が原本と一致した。読取りに進んだ4件は97・104・97・104文書で燃料を
  使い切り、pin再読前で止まった。単独の正常系は99文書、復旧系は97文書に到達。正常系の
  先頭27文書では同じ読取り区間が525,785,909から343,533,608 fuelに減った。9件とも期限内、
  最大計上量59,498,497 Bで、退役後は0 B・0予約。正常系の終端ではCPythonのJSON復号に
  燃料を使っていた。
  [revision・索引追加後の実Wasm 44334991562](https://github.com/harupipipipi/tobkiri/actions/runs/44334991562)
  は同じ先行拒否5件が一致した。読取りに進む4件は前回と同じ97・104・97・104文書で
  燃料を使い切った。単独正常系98、復旧系100文書で、どの経路もpin再読前に停止し、
  期限超過はなかった。9件の最大計上量59,542,449 B、退役後は0 B・0予約。
  `e79363f72` は `catalog-app-frame` のSetup検証で、選択した目録を同一呼び出しのRustから
  読み、既存の `load_catalog` 内で順に渡す。ゲストは入手した原文から同じ種類別辞書を組み立て、
  全行の受領後に完了を返す。この時点で同じロックと原文からresolver用の入力を保持する。
  公開時の独立したロック再読と呼び出しの上限を維持した。物理fixtureでは305文書と148 pin、
  読取り後に文書を取り除いても保持済み原文で完了すること、順序違反と未完了を拒否することを
  確認した。固定原本との模擬応答比較では順序と辞書内容が一致。Rust 256件、カタログCI
  ラッパー16件、strict Clippy、固定CPython 265件、producer 345件、CIスクリプト14件が通過。
  OSエラーと単独CanonicalizationErrorは原本のcause/contextの同一性を維持する。
  JSON構文・UTF-8の失敗時は判定済みのロック原文を一度渡し、固定原本のparserで外側の診断と
  入れ子の例外を再現する。ファイル再読なしで例外の型・引数・同一性・抑制フラグを照合した。
  不正な応答や再試行を拒否し、失敗応答後に確保が元へ戻ることをRust 257件、カタログCI
  ラッパー17件、producer 345件、CIスクリプト14件で検証した。
  [新しい目録配送を含むSetup実Wasm CI 44340854329](https://github.com/harupipipipi/tobkiri/actions/runs/44340854329)
  は `438c14db7` で先行拒否5件が原本に一致した。読取りに進んだ4件はRustで305文書と
  148 pinを物理確認し、ゲスト側で7・8・7・8ページ目までに燃料を使い切った。正常系と
  復旧系もCPythonの正規表現処理で同じ燃料上限に達した。期限超過はなく、9件の最大計上量は
  41,347,355 B、退役後は0 B・0予約。入れ子のロック診断修正はこの計測より後。
  同じ検証済みWasmと固定原本fixtureを `32e210626` のHostで実macOS再生すると、
  Rustの305文書・148 pin確認後、
  132文書を含む7ページの送信時に燃料が0になった。最初のページの前に残っていた燃料は
  999,263,700。原本のユーザーデータ・期限・確保量は同じ呼び出し内で管理し、回収を確認した。
  `014843728` は新しいApp版の受け渡しにBase64を使い、派生App版だけで文字列の連続区間を
  まとめて走査する。従来のサイズ・ノード制限、UTF-8、順序、文書IDと最終確認を引き継ぐ。
  Rustは先行のテキストページ要求にも応答する。
  固定CPython 3.13.16で実物305件のdigestと完成辞書を照合し、反復11回の中央値は
  70.202 msから20.297 msになった。実Wasm燃料の比較はこの変更を含むCIで行う。
  Rust 266件、strict Clippy、producer 346件、固定CPythonアダプター269件、
  CIスクリプト15件が通過。旧Tool Bridgeの事前目録は、既存Profile2ファイルの記録差分で
  検査1件が失敗する。
  [Launcherステージング 44349700285](https://github.com/harupipipipi/tobkiri/actions/runs/44349700285)
  は準備試験6件に成功した後、ラッパーの期待件数が5件のため止まった。`cc4cafcb4` で6件に合わせ、
  [Launcherステージング 44355528195](https://github.com/harupipipipi/tobkiri/actions/runs/44355528195) と
  [Base64版の実Wasm 44355527562](https://github.com/harupipipipi/tobkiri/actions/runs/44355527562) を開始した。
  Launcher CIは準備6件、実コンポーネントの選択、封印したリソースとTauriへのコピーの照合を通過した。
  DMGのインストールから既存GUIで会話する受け入れ試験が残る。
  Base64版では実カタログの305文書と148 pinを読み、コンストラクター入口までの9ケースが
  原本と一致した。Profile復旧の続きではロード直後に燃料37,430,611を残し、同じStoreで
  Profileの入替えとAuthority初期化を行う途中で使い切った。期限超過はなく、確保は0に戻った。
  App向けのデコード後にBase64文字列を再エンコードしていた処理を、復号した値の検証に置換。
  文字列・キーのUTF-8検証、ノード・サイズ上限、重複キー・数値の検査と例外を維持する。
  固定CPython 3.13.16で実物305文書・24ページの結果が一致し、11回の中央値は
  13.730 msから7.465 ms。producer 349件が通過。Wasm上の復旧は同じ実行上限で再計測する。
  [3fd83202b のmacOS実Wasm 44364569859](https://github.com/harupipipipi/tobkiri/actions/runs/44364569859)
  は、固定原本の結果を照合しながら、同じStoreで復旧時のProfile表の入替えとAuthority DBの初期化を
  通過した。次の確認は、この派生Appコンポーネントを封印済みLauncherへ組み込む段階。
  既存 `desktop-installers.yml` のCI用DMGは `.app` をビルド後に署名・検証して包む。
  現行の冷起動試験はKernelとパネルまで。実DMGのSetup確認・公開・Launchには別の検証が要る。
  [DMG CI 44418243523](https://github.com/harupipipipi/tobkiri/actions/runs/44418243523) は
  Launcher 670件の後、ソース検証3件で失敗した。先行するVite buildが検証対象の
  `ui/shell-defaultspack-app.js` をcheckout内で2,689,881バイトから約2,690,660バイトへ変更した。
  CIの対象コミットからwebappとuiを一時領域へ展開し、`npm ci`、Vite、生成物照合をそこで実行する。
  本来のcheckoutにある成果物を署名・パッケージングし、元の検証を通す。同じコマンド列を
  macOSのNode 25.5.0で実行し、状態照合と一時領域の削除が成功した。元の667件はサイズ・モード・
  SHA-256まで一致した。CIのNode 22と実DMGの再確認は新しいrunで行う。
  [先行DMG CI 44429185996](https://github.com/harupipipipi/tobkiri/actions/runs/44429185996) も
  修正前の同じ上書きで128件が通過した後の3件で失敗した。失敗時と再実行時のGit blob 667件は
  同じmanifestに一致する。修正 `314c939c4` を含む
  [再実行44439801329](https://github.com/harupipipipi/tobkiri/actions/runs/44439801329) で確認を続ける。
  そのrunではLauncher stagingのIPCが8件すべて通過した後、旧期待件数6で停止した。
  当時の実際の試験数はIPC 8・ワーカー7。現行の実行件数12・12への更新を含む
  [3c134cc6d の検証用DMG CI 44446050664](https://github.com/harupipipipi/tobkiri/actions/runs/44446050664)
  はLauncherのテストビルド中に45分のジョブ上限で終了した。DMGを作る場合だけ75分に延長した。
  `launcher-snapshot-staging` の任意のDMG検証を、既存のCI用インストーラーワークフローへ接続。
  同じrunで生成したApp用Wasmを再照合し、先行ステージングで検証したv3レシートのSHA-256を
  Launcherビルドに渡す。検証専用のRust bindingはビルド環境へ渡さない。
  既存の読み取り専用マウントを再利用して、同梱WasmのサイズとSHA-256、既存の署名とPythonを確認する。
  検証したDMGのパスをそのままCIアーティファクトに使う。配布版の経路は対象外。
  ローカルではDMG実マウントを含む22件、Launcherワークフロー16件、パッケージ契約25件が通過。
  CI実DMGのビルドと、インストール後の人によるSetup・承認・設定・会話は受け入れ未確認。
  CI用のWasmが束縛されたLauncherでは、既存Launchとguardianが共用するメタデータ読取りを、
  Setupと同じ `NativeRuntimeState` のワーカーに接続した。元の署名付きresolverと組立処理を使い、
  宣言されたアーティファクトの照合を同じ物理ルートと制御・計上のWasm DATA Storeで行う。
  親の期限・キャンセルで発行を止め、物理後処理を待つ。完了後は現行active Profileと照合してから
  既存のHost契約発行へ進む。別のCoreや承認用Hostは生成しない。Coreの物理試験8件が通過。
  元Launcher fixtureに新旧メタデータの比較を追加。ローカル容量のためTauriのビルドはCIで行う。
  既存の起動前後と自動再起動の照合も同じ読取りへ接続し、元の一回の準備期限を共有する。
  active Profileとの再照合を管理ワーカーの期限内に移し、Profile交代は読取り失敗の上限に加算しない。
  自動再起動中に前景で採用された新しいProfileを古い結果で上書きしない回帰試験をmacOS CIに追加。
  終了時は読取りを待つ起動元を解除し、ワーカーの物理後処理は既存の管理下に残す。
  Pythonプロセスの起動と通常画面のRegistry・会話の接続は後続。
  [直前のLauncherステージング44368885897](https://github.com/harupipipipi/tobkiri/actions/runs/44368885897)
  は成功。監視を接続した `dc4e5c4d8` はパッケージ契約25件とワークフロー16件が通過し、
  [新しいDMG CI 44374630677](https://github.com/harupipipipi/tobkiri/actions/runs/44374630677) を実行中。
  `2535f6dbd` は `adopt_owned_bundle` のPack再パースを外し、構造と意味を検証した同じ文書からpin情報を抽出
  する。必要な候補だけPack ID・ハッシュ・種類・依存IDを保持し、全行受領後にロック順で採用。
  旧readerとの直接比較で、非ASCIIを含む依存ID順、設定なしと空の違い、欠損時の結果が一致した。
  独立した148件のファイル再読は従来の段階で実施する。
  `05ec1bebf` はエスケープ後に4MiBを超える文書を、既存のbytes応答で一件だけ送る。元の生バイト上限を
  共用し、ゲストは検証済みの版フィールドとIDから種類別辞書の同じ位置へ格納する。固定原本
  が受理した702,348 BのBase原文はテキスト応答4,202,970 B、旧bytes応答936,580 Bだった。
  Rustの物理fixtureとproducerで、前後の順序、不正応答、再試行と早い完了の拒否を確認。
  実Wasmの観測では物理文書数に加え、送信済みの序数を分けて記録する。
  Rust 260件、カタログCIラッパー18件、strict Clippy、固定CPythonのproducer 345件、
  CIスクリプト14件が通過。
  `b101cbb9a` は同梱目録、保存済み外部目録、CASのPack・実行目録でbindingsの比較を共用した。
  文字列と空の値は借用し、入れ子の値だけ既存のUnicode15.1対応rendererで
  `str(value or "")` にする。固定原本の実スキーマと比較し、辞書キーの順序、
  `[true]` と `[1]`、欠落と空配列による重複、空のprincipalがprovider IDに優先する場合を確認。
  CASの重複した禁止キー走査を削除し、Packの禁止キー判定は共通の検証に残した。
  定義をゲストに渡す従来の経路では、追加の証明領域が不足した場合も同じバイト応答を試みる。
  Rust 263件、カタログCIラッパー18件、strict Clippy、CIスクリプト14件が通過した。
  `8eb7969de` は同梱文書の証明に失敗した場合、種類・元の相対パスを先に返し、一回限りの
  診断要求で同じ物理読取りのバイト列を渡す。固定された元のvalidatorが失敗したときだけ
  元の `BundleIntegrityError` を組み立てる。厳密JSON、UTF-8、スキーマ、意味検証の
  メッセージ・診断・例外チェーンを元のローダーと比較した。原本が受理してもRustが
  証明できなかった場合は、承認も目録の採用も行わず終了する。
  従来の応答上限を実際のscopeから求め、ロック・文書・pinの物理読取り後、ハッシュや
  検証を始める前に適用する。上限ちょうどの文書を再読取りせず診断へ渡すことと、
  1 B超過がダイジェスト不一致より先に止まることを物理fixtureで確認した。
  Rust 265件、カタログCIラッパー20件、strict Clippy、固定CPythonアダプター265件、
  producer 345件とCIスクリプト14件が通過した。
  `8ce338169` は通常のApp版だけにv3レシートを出力し、Launcherの選択に接続した。
  `harness_native_ci_inputs.py` が派生ソース、圧縮capture、同一ビルドのWasmと完全なProfile
  スキーマを照合する。`setup_component_build.rs` はWasmとレシートの両方を固定digestで
  検証し、v3だけでRustの目録を選ぶ。v1・v2は従来の読取りを選ぶ。既存Setupレビューと
  承認後の独立した確定ワーカーが、検証された同じ選択を保持する。Store作成前に不正な
  スキーマ指定を拒否することを確認した。従来の実Wasm計測用環境変数は試験専用のまま。
  Rust 266件、strict Clippy、receipt単体1件、固定CPythonアダプター269件、producer
  345件、CIスクリプト15件が通過した。`launcher-snapshot-staging` は通常Launcherの
  ソースでv3の生成設定と実Wasmのリソース封印を照合する。配布版の初回Setup、専用承認、
  設定、会話の一連の実機試験と同梱Python/PackVMの除去は後続。
  [定義CI 44320718878](https://github.com/harupipipipi/tobkiri/actions/runs/44320718878)
  は19件が通過後、旧期待件数18で失敗した。`b08f8c89d` で19に更新し、
  [定義CI 44324536754](https://github.com/harupipipipi/tobkiri/actions/runs/44324536754)で
  19件は通過。後続の両OSで旧Profile schemaパスの指定が0件に一致して失敗した。
  `9bfc0076e` で移動後のテストを指定し、全定義CIのフィルターと現行の件数を照合して
  Shell選択、Setup候補、物理カタログの期待件数も修正。4つの変更済みゲートをローカルで通過。
  [修正後の定義CI 44328203667](https://github.com/harupipipipi/tobkiri/actions/runs/44328203667)
  はLinuxとmacOSで通過。
  `f88735b2e` はコードキャッシュ派生の旧参照を削除し、ソースと計上値をどちらもLoader
  に束縛されたツリーから取る。原本の先行変更後に失敗していた専用6件を含めてproducer
  344件が通過した。
  `f2fd7528a` は解決後の固定呼び出しで、登録revisionと実catalogの定義digestが一致したときに
  resolver側のrootでAuthorityを新たに開く。実Shellを再検証し、閉じた後に4つのactivation
  識別子を検証済みポインターと比較する。最初の登録済みSetup検証と同じ稼働記録読取りを共用し、
  再承認例外をAuthorityのclose後に識別する。最終候補と同じShellを選ぶために、最初に物理検証
  したcatalog入力を同じ予算内で解決後まで保持し、再取得した定義digestも照合する。
  変更なしなら後継情報を省き、直接の親から既存Provider Packのpinだけが変わり、すべてが同じ
  カタログで検証できた場合だけ、最初のレジストリsnapshotとポインターを12項目で確認に結ぶ。
  inactive判定は独立して実行し、旧定義の確認情報との併用も元のdigestに含める。登録済み候補の
  準備時のPack pin復旧、同梱外の承認照合、登録済み置換の確定は後続。初回確定処理にはこの
  Pack pin付きの確認を渡さない。旧形式の稼働記録と未接続のschemaは従来の境界で終了する。
  Rust 245件、strict Clippy、Pack pinのCIラッパー3件、Python 256件、固定原本の専用16件が通過。
  物理macOS fixtureで変更なし、古いShell指定、別activationに切り替えられたポインターを比較。
  一つのfixture内の別レビューには個別のinvocationを使い、各回の確保量が0へ戻ることを確認した。
  固定CPython上で元の実resolverと確認を呼び、Pack pin単独と旧定義情報を併用したdigestを照合。
  `b353a181c` は登録済みSetupで、同梱と一致しない Normal/Application Pack の読み取りを接続。
  active PackとBase/ShellからID順に探索し、登録定義のpinを登録順で検証する。対象の単一pinで
  署名済みrootを独立再取得し、物理manifestと実行目録を読み直す。実行目録の再読はID順、元の
  `catalog_with_packs` が受け取る追加は探索順、置換は旧位置。同じ初回getterに結び付いた中間
  カタログを更新判断より前に作り、依存・binding・後継pinの確認に同じ有効なメタデータを使う。
  Pack pin復旧では登録順に別の探索を行い、最新レジストリで既存Provider pinだけの直接の後継を
  証明した場合に登録済み定義を使う。確認が必要なPackや実行権限はこの段階で付与しない。
  同梱物理rootのフォールバック、Host Extensionの互換性検証、任意Packの承認再照合、確定済み
  置換の保存は後続。復旧用探索と共用できるのは既存の物理readと検証で、入口のpinと順序を残す。
  Rust 247件、Python 260件、strict Clippy、固定原本の署名済み2版を使う比較、物理Shell fixtureが
  通過。前回[Ubuntu CI 44316484721](https://github.com/harupipipipi/tobkiri/actions/runs/44316484721)
  ではfixture内の独立した複数レビューが一つの30秒期限を共有して失敗した。この比較では独立した
  レビューごとに同じ上限のinvocationを使い、ローカルで各回の後処理と結果を確認した。
- [署名付きPack投影と定義CI 44106179177](https://github.com/harupipipipi/tobkiri/actions/runs/44106179177)
  と[非同期化後の定義CI 44110927026](https://github.com/harupipipipi/tobkiri/actions/runs/44110927026)
  はLinuxとmacOSで通過。[Launcher staging CI 44103401607](https://github.com/harupipipipi/tobkiri/actions/runs/44103401607)
  は同じrunで生成したWasmの検証済みセットを元のLauncherへ配置するテストに成功。
  [復旧resolver接続後の実Wasm CI 44114533643](https://github.com/harupipipipi/tobkiri/actions/runs/44114533643)
  は成果物の生成とbootstrap検証を通過した。
  [実保存を含む定義CI 44119941406](https://github.com/harupipipipi/tobkiri/actions/runs/44119941406)
  はLinux・macOSで通過し、[実Wasm CI 44119964703](https://github.com/harupipipipi/tobkiri/actions/runs/44119964703)
  は同じ世代の成果物生成とbootstrap検証を通過した。
  [tests込みClippy修正後の定義CI44124305192](https://github.com/harupipipipi/tobkiri/actions/runs/44124305192)
  もLinux・macOSで通過した。
  SetupからGUI承認、会話までの実機検証は後続。
- 通常App capability Profile FlowのCIは、成功済みrun40462099581の7日保存artifactが
  取得できず停止した。`6278cf52d` でpaired componentsと原本比較の保存を90日にした。
  再生成のbuildは成功したが、macos-14構成がGitHubに非対応として取り消されたため、
  `5eb66580e` で既存のmacos-15へ変更した。
  [原本との再比較44135235977](https://github.com/harupipipipi/tobkiri/actions/runs/44135235977)は
  build-two-worlds/compare-original-dataとも成功。`ee9b738d8` は、そのrun/SHA/artifact ID/digestを
  ordinary Flowのsupplierへ固定し直した。欠落・重複も明示エラーで拒否し、認証条件は維持する。
  workflow 22件とmaterialization 4件が成功。
  [通常Flow CI44138078966](https://github.com/harupipipipi/tobkiri/actions/runs/44138078966)も成功し、
  実受信UI entryからのProfile Flowと未承認の入れ子route拒否、実consumerのstrict Clippyを通過した。

## 043: 設定とコマンド一覧の表示生成を PackVM から移す

- `defaultspack.application-presentation` は現在の起動準備と、設定、UIカタログ、コマンド一覧の
  署名済み呼出しに使われる。`defaultspack/runtime/application_presentation.py` は、表示定義を
  作るために元の設定・コマンド処理を1,600行以上複製している。状態の読取りやコマンドの実行権限は、
  既存の各呼出し元が所有する。
- Rustゲストは同じ原本から生成した表示定義を同梱し、呼出し元が解決したモデル選択肢を
  既存の4欄に入れる。設定欄、コマンドID、表示順、ルートの識別子、数値を文字列で保持する境界、
  64 KiBの入力制限を維持する。署名済みコマンドの実行可否は元の所有者が決める。
  固定CPython 3.13.16と、外部ポートをimportしない実コンポーネントを64 MiBのゲスト上限で比較。
  空の候補、重複とUnicodeを含む候補、64 KiB境界、拒否、全50コマンドを検証した。
  Rust単体4件とdoctest 1件、strict Clippy、Pythonの表示と呼出し元31件、CI構成22件が通過した。
  コマンド一覧の既存テストは署名済みスキーマで既に使う `models.strategy_reference` に揃えた。
- クリーンな `346d55d91` で署名済みHTTP 3件が通過した。設定の通常表示と全表示、UIカタログ、
  コマンド一覧の4呼出しに同じ入力を渡し、実Wasmと元のPythonの全出力を照合した。設定保存値の
  非公開フィールド、秘密値、コマンド承認表示と容量拒否のHTTP 503も確認した。搬送はテスト用の
  PackVMアダプターで、Wasmゲストだけを置換した。現行製品の起動とbackend選択は未検証。
  差し替え候補の生成物、パッケージのソース目録、v4アーキテクチャ検証、CIの該当stepもローカルで通過。
- 旧汎用Wasm workerはLinuxのcgroup v2が必要で、macOSでは起動準備に使えない。
  macOSの製品接続には、各署名済み呼出しの親と現在のリースを証明し、64 MiBの枠で動く
  Launcher/Core側の経路を使う。元の3経路の設定と表示、秘密値の非公開、PackVMの
  割当て回数を実際の署名済みHTTPで再検証した後に、旧表示実行を撤去する。
- `conversation.turn.v1/complete` は別の起動条件として残る。通常チャットのWasm実行は
  `shell.tauri.default` の署名済み経路に束縛されているため、Side Chatは別に検証する。
- `57e7bbfd4` の[実Gemma 3 1B CI 44530373823](https://github.com/harupipipipi/tobkiri/actions/runs/44530373823)
  は、実Wasmの認証付きloopbackと保存済みRegistryのモデル選択の両方に成功した。配布用DMGの
  Setup、GUI承認、同じアプリからのモデル設定と会話は、別の実機検証が必要。
- `6cc45f68b` は、設定・UIカタログ・コマンド一覧の署名済み子呼出しをLauncherの読み取り専用
  Wasmポートへ接続した。Launcherは各親と子の署名済み文書、v6レシートのアーカイブとWITを照合し、
  3経路が揃ったときだけ管理下のプロセスにマニフェストを公開する。実行時は元の子リース、親の
  期限と予約、現在のProvider権限を確認し、ジョブが入力容量を確保してから本文を読む。
  `wasm32-unknown-unknown` とWASIアダプターなしで組み立て、Coreが効果のないポートとして実行する。
  表示の入力上限とUIの所有者を維持する。旧PythonからRustへの読取りには保存ターンやGatewayと
  同じbounded stringの変換を使う。3用途が同じ型と予算を必要とするため、共通関数の名称と
  エラー表示を一般化できる。旧プロトコルのコード削除は製品経路の廃止後に行う。
  Rustの実アーカイブ試験で全コマンド、モデル候補、上限超過、実行後の確保量0を確認した。
  クリーンな同コミットのmacOS結合試験では、3種類の署名済みHTTP経路を実Unixソケットと
  実Wasmまで通し、Python原本と出力を照合した。元のコマンド呼出しは維持され、途中で
  Provider権限を失効させた設定の返答は拒否された。Appハンドルと配布物のresolverはfixtureで、
  インストールしたDMGのGUIからの検証は後続。Python IPC 498件、Rust IPC 50件、ジョブ26件、
  署名経路の選択、レシート16件、DMG検証とCI構成387件、Tauriテストの型検査、対象crateの
  strict Clippyとv4アーキテクチャ検証が通過した。

## 044: 起動時にも選択する従来の会話完了処理

- `conversation.turn.v1/complete` は保存ターンと別の署名済み操作。Launcherの起動準備、従来の軽量
  会話画面、HTTPとCLIの互換経路から使われる。`defaultspack/runtime/conversation.py` のPackVM版は、
  メッセージとモデルからGateway入力を作り、1回のHost呼出しの後に本文とツール表示を返す。
- `033cde0a4` はこの前処理と応答整形を読み取り専用Wasm Port `local.conversation-complete.data@1.0`
  に移した。固定CPython 3.13.16で署名済み原本の実2段階呼出しから得た60ケースを、実アーカイブの
  Wasmから64 MiBのCore容量枠で比較した。Unicode、64 KiBの要求境界、512 KiBの応答境界、
  型付きエラー、本文とツールの投影が一致し、毎回の確保量が0になった。Rust単体5件とstrict Clippyも通過。
- 製品接続では元のShell→会話→Gatewayの署名済み選択と現在の親リースを確認し、Hostが1回の
  Gateway呼出しと応答の受け渡しを所有する。期限、キャンセル、権限失効、元の出力のデータ制限を
  実経路で確認した後、旧起動要件のPackVM選択と旧チャネルの継続処理を整理する。

## 045: Side Chatビューと既存の保存ターン

- `ee85c8a5c` は、画面に表示されたSide Chatの署名済み操作が404になる問題を修正した。
  PackカタログでGrantの時刻を含むレコードをAuthority用のdigestで照合し、選択済みShellから
  直接付与されたGrantと同じPlan上の呼出し元・宛先を確認する。契約から引き継いだ入力schemaは、
  Side Chatビューが宣言する読取り・開始・送信等と進捗操作にだけ使う。Hostが付与する
  `profile_id` と `_session_id` は公開入力から外し、元の署名済みschemaとBrokerで検証する。
- クリーンな同コミットで、通常の署名済みHTTPを旧PackVMアダプターと既存の保存ターンWasmを
  選択する両方の構成で確認した。会話を開き、親の履歴を保ったまま子へ返信を保存し、同じturnを
  再照合しても推論は1回。進捗の読取り宣言と、内部保存turn・別の承認操作の公開別名を拒否する
  ことも確認。公開カタログとGrantの回帰を含めて4件が通過した。
- macOSでは、同じ署名済みHTTPの送信を実Unixソケットとv6レシートに含まれる実Wasmまで通した。
  初期、読取り、入力保存後、推論後、応答保存後の全5ステップがPython原本と一致し、保存と再照合が
  通過した。Appハンドル、配布物resolver、推論応答はfixtureを使用した。
- Side Chatは独立した署名済み親から共通の保存オーケストレーションへ入る。ターンの開始、
  追記、Gatewayは通常のShell直呼び出しだけを選ぶ個別のネイティブ経路が残る。現在の
  子会話へのアクセスと、親の最新文脈や権限に結び付ける検査を移植時にも維持する。
- 既存のプロセス終了後の復旧試験を同じクリーンなソースで再実行した。ユーザー文の保存後に終了する
  ケースは成功。assistant文の保存直後に終了するケースでは、終了済みPIDの `conversations.lock`
  が残り、`confirm_saved_completion` が待ち続けた。ロックには期限が記録されていない。
  再照合の全体試験は95秒で終了させた。PythonとRustの旧会話ストアが同じロックを持つため、
  双方の所有確認と排他を保って回収できるようにし、再実行や二重保存を防ぐ回帰を通す。

## 046: 保存済み会話の強制終了からの復旧

- `373772f3a` は、macOSとLinuxのPythonとRustで既存の `conversations.lock` を共有し、各書込みで
  同じ `locks/` ディレクトリの排他を保持する。既存フィールドに形式識別子を足した完全なレコードを
  原子的に配置する。強制終了後は、現在のディレクトリ排他を確保したプロセスが、所有者と物理ファイルを
  確認して取り直せる。旧形式は記録されたPIDの終了が確認できた場合に回収する。読み取れない記録、
  生存中か再利用されたPID、リンクを含む安全性を確認できないファイルは保持する。
- macOSでPython 174件、Rustの共有Registry 24件とPython→Rust→Pythonの実プロセス回復試験が通過。
  ユーザー文の保存直後とassistant文の保存直後に強制終了する既存2ケースもクリーンなソースから再実行し、
  保存済みターンを二重実行せず再照合できた。旧ストアのファイル形式と呼出し元の書込み順序を維持した。
  会話ストアの旧Pythonによる互換書込みを廃止した段階で、旧ロック読取りを整理できる。

## 047: 署名済み会話完了処理をLauncherへ接続

- `38b2aa052` は `conversation.turn.v1/complete` を、同じ署名済みShell→Conversation→Gatewayの
  経路と親のリースを確認して、v7レシートに含まれる2段階のWasmに接続した。Gatewayの呼出しはHostが
  所有し、親と同じキャンセル状態を使う。完了処理に失敗した場合もGatewayのやり直しを行わない。
  Application Presentationの所有者も、署名済みの起動Pack IDと実体の `defaultspack` を
  resolverの結果から照合するように修正した。
- v7配布用アーカイブをローカルで組み立てた。旧v6の8件のレシート行が一致し、
  `local.conversation-complete` の追加を確認した。追加したアーカイブは60ケースの原本比較で
  使ったものとバイト単位で一致した。Rustレシート17件、DMG検証とCI構成411件
  および12 subtest、Tauriテストの型検査が通過した。
- クリーンなチェックアウトで署名済みHTTP、Python IPC、CI構成、保存直後の強制終了を含む521件が通過。
  PackVMを空にした起動準備、元の出力、途中の改変と権限失効の拒否、Gatewayを二重に呼ばないことを
  確認した。macOSの実Launcherソケットを使う試験用ビルドで、Rust IPC 53件、ジョブ28件、
  署名経路とWITの選択試験が通過した。
- 同じ署名済みHTTPの要求をmacOSの実Unixソケットとv7の実Wasmまで通した。2段階の出力を
  Python原本と比較し、Gatewayの呼出し数と権限失効後の拒否を確認した。Appハンドル、配布物の
  resolver、Gatewayの応答はfixtureを使用した。インストールしたDMGのGUI経由の検証は未了。

## 048: Side Chatの署名済みメッセージ追記

- `b44103aa5` と `818682b58` は Side Chat の送信から保存ターンのメッセージ追記まで、
  Shell → Side Chat → Turn → Saved Orchestration → Message Manage の元の署名経路を
  保持して既存のWasm追記を選ぶ。通常チャットとは別にLauncherが起動証明を持ち、両経路の
  証明はそれぞれの呼出しにだけ使う。現在の親のリース、期限、Profile、会話・turnのIDと、
  追記自身の容量予約を照合してから、元の会話ストアのロック下で書き込む。
- IPCは以前の要求形式を通常チャットとして扱う。Side Chatの要求には呼出し元を明示し、
  Launcherが保存前に署名済みPackと実行カタログを再検証する。元の保存形式、親子会話の
  照合、保存結果が不明なときの再実行禁止を維持する。署名経路の選択、IPC 54件、Launcher
  ジョブ28件と、Pythonの追記IPC 41件、Tauriテストの型検査が通過した。
- `e59696a33` のクリーンなmacOS試験で、同じ署名済みHTTP要求を実Launcherソケット、
  v7レシートのWasmオーケストレーションとWasm追記まで通した。ユーザー文とassistant文を
  既存の子会話へ保存し、親は維持された。再照合後の会話が一致し、推論は1回だった。
  Appハンドル、resolver、推論応答はfixtureを使った。Turn開始、確認、配布DMGとGUIへの
  置換完了はこの試験の対象外。
- `message_host.py` の従来書込みを整理するには、Side Chatの原本互換と通常チャットの
  追記の置換に加え、別の呼出し元および保存前にネイティブ側が受付不能になる場合の扱いも
  製品経路で確認する必要がある。Side Chatの開始、推論、確認の各Portは独立して比較する。

## 049: Launcherから製品Pythonを外す際の接続点

- macOSのLaunchボタンは、初期状態でPackVMが未準備でも利用できる。通常起動の判定は
  認証済みループバックの `/health` と、現在のProfileでの会話・表示Portの検証で行う。
  既存Setupは選択画面の前にDoctorの応答を要求する。実機でゲストを作らずにこの経路を
  通し、配布物・署名済み設定・承認ウィンドウを保った状態で確認する。
- `kernel_manager.rs` の管理対象Kernelと、`dock_registration.rs` の選択済みデスクトップ
  listenerは別のsealed Pythonプロセス。後者がHTTP、パネルの認証、署名済みSessionを
  所有する。Wasmに移した操作もこのSessionから呼ばれる。既存Launcherの
  `NativeRuntimeState::adopt_core` へ実ProfileのSession・起動時の現在性と認証済み
  Shellへの引き渡しを結び付け、元のUIと接続後に両プロセスを整理する。
- 現在のsealed Python起動がmacOS配布物内のPackVMマニフェストも照合するため、
  ゲストが未準備の配布物での起動と、PackVMを含まない配布物での起動は別の検証になる。
  `spawn_defaultspack_local_server` 付近の古いpack-shell表記も、実際の永続プロセス所有と
  揃える。配布ファイルの削除前にPythonの依存を外した同じLauncherの起動を確認する。
- 現在の通常配布の `release.yml` は `native-first-setup` を有効にしない。起動に必要な
  `conversation.turn.v1/complete` と `defaultspack.presentation.read` をWasmに接続する証明は、
  `desktop-installers.yml` のnative Setup付きCIビルドで供給している。配布物からVMを除く前に、
  同じ証明と署名の照合を通常リリースで使えるようにし、Setupが読む認証済みDoctorの応答を維持する。
  Python Sandbox、MCP Gateway、DeepThink、メディア検査の既存PackにもVM経路が残る。

## 050: Side Chatのターン開始と実行権の取得

- `f4c82701a` はSide Chatからの `begin_saved` と `claim_saved` を、通常のターンとは別の
  Launcher証明で既存のWasmに接続した。Shell → Side Chat → Saved Turn → Turn Lifecycle
  の経路をたどり、Side Chatの送信と同じターン、子会話、リビジョン、内容だけを扱う。
  リース・期限・予約は実行中の呼出しから確認し、SQLiteの既存の入力digest、ステータスと
  公開確認を維持する。書込みが確認できなければ、元の経路で勝手にやり直さない。
- `71ab11293` のLauncherは、両経路に別々の起動証明を持ち、開始直前に対象の署名済みPackと
  実行カタログを再検証する。古いIPC要求の呼出し元は従来どおり通常チャット。
  署名とファイル改変の回帰、Rust IPC 55件、Launcherジョブ29件、Python IPC 33件、
  署名済みHTTP 2件、Tauriテストの型検査が通過した。
- macOSで実Launcherソケットとv7の同じ実Wasm配布用アーカイブを使い、Side Chatの開始、
  実行権の取得、ユーザー文・assistant文の追記を確認した。再照合後の子会話と親の履歴が一致し、
  推論呼出しは1回だった。App、resolverと推論応答はfixture。実際にインストールしたDMGとGUIは
  この試験の対象外。
- Side Chatの開始・メッセージ追記で、Hostが保持する現在の親を辿る検査を共有した。
  構成に関わらず最上位の呼出しまで一致させる。ターン開始側のPythonの書込みを外すには、
  この2経路以外の元の呼出しと、開始後の確認・再照合・実行データの保存経路も検証する。

## 051: Side Chatの保存ターンの確定と再照合

- `d24b4c7c8` と `37df89803` はSide Chatの送信と明示的な再照合で、元の署名済み
  Saved Turn / Reconcile を通してSQLiteのターンをRust＋Wasmで確定できるようにした。
  通常チャットの2経路と合わせて4種類の権限を照合する。再照合では現在のターンの
  リビジョンを要求し、子会話と保存済み結果の識別子を確認する。
- Launcherは同じBundleのターン開始と確定で、物理ファイルを再読取りする処理を共有する。
  書込みの許可、結果の喪失を検出した際の非再実行、既存のSQLite行と保存済み結果の照合を
  維持する。Rust IPC 55件、Launcherジョブ30件、確定のRust試験27件、Side Chatの署名選択、
  Python IPC 28件、Tauriテストの型検査が通過した。
- macOSで署名済みHTTPから実Launcherソケットとv7の実Wasmへ送り、通常のSide Chat送信での
  確定と、送信時に結果の喪失を模擬した後の明示的な再照合をそれぞれ確認した。
  再照合は既存のメッセージとそのリビジョンを維持して確定状態を更新し、2回目は同じ結果になった。
  App、resolver、推論応答はfixture。推論は各試験で1回だった。
- SQLite側の確定の後、Conversation Storeに確定を反映する呼出しと、再照合が先に読む
  Receiptは別の署名済み経路。Side Chatの両経路に接続し、元の結果を再生してから
  Pythonの重複する保存と参照処理を外す。

## 052: Side Chatの保存済みレシート読取り

- `22d5756e8` はSide Chatの送信と再照合から、署名済みTurnの結果を同じConversation
  Storeで読み取る経路を既存のWasmに接続した。ターン確定と同じ経路選択を共用し、
  レシートを返す前にSide Chatの親、ターンと子会話の一致を確認する。
- `e2181d739` のLauncherはSide Chatの2経路に専用の証明を持ち、読取り前にBundle Lock、
  対象Packと実行カタログを再検証する。IPCは通常経路とSide Chatの4種類を区別する。
  Conversationの確定とTurnの読取りは従来の呼出し元を使う。元のバイト上限と
  保存データの投影を維持する。
- PythonのIPC 59件、レシートと署名証明のRust試験7件、Rust IPC 55件とジョブ30件、
  Side Chat・通常チャットの署名済みHTTP 2件、Tauriテストの型検査が通過した。
- macOSで送信時の結果喪失を模擬し、再照合を実Launcherソケットとv7のWasmまで通した。
  レシートが従来の読取り結果と一致し、読取りの前後で会話ファイルの全バイトが変わらない
  ことを確認した。既存結果でターンを確定し、2回目の再照合でも再推論・再追記はなかった。
  App、resolver、推論応答はfixture。配布DMGとGUIはこの試験の対象外。

## 053: Side Chatの会話ストア確定と内側のTurn読取り

- `704ab7e95` はSide Chatの送信と再照合で、Conversation Storeの確定と、その内側で行う
  Turn読取りを既存のWasmに接続した。保存ターンの親を辿る処理をレシート読取りと共用し、
  Profile、turn、子会話と現在有効なリースを照合する。確定処理は内側で取得した確定済み
  Turnを使い、保存結果が不明になった後の二重書込みを防ぐ。
- `416b558e8` のLauncherは、Shell → Side Chat → Saved Turn / Reconcile → Conversation
  Storeの確定 → Turn Resourceの署名を確認する。保存前と読取り前に、元のBundleと対象Pack、
  実行カタログを再検証する。IPCで通常チャットとSide Chatの4種類の呼出し元を共通に判定し、
  レシート・確定・読取りはそれぞれの証明で許可する。従来の入力形式とバイト上限を維持する。
- PythonのIPC 80件、署名済みHTTP 2件、Rustの署名経路と権限の回帰、IPC 55件、
  Launcherジョブ30件、Tauriテストの型検査、CIワークフローの契約テスト16件が通過した。
  macOSでは署名済みHTTPを実Launcherソケットとv7の実Wasmまで
  通し、Side Chatの送信と再照合の両方でTurn読取りと確定を確認した。Turn読取りの前後で
  SQLiteの全バイトが一致し、2回目の確定で会話ファイルが変わらず、推論は1回だった。
  App、配布物のresolver、推論応答はfixture。
- 先行コミット `350517a72` の [配布CI](https://github.com/harupipipipi/tobkiri/actions/runs/44527643512)
  は元のLauncherからのネイティブSetupの境界、署名済みWasmの梱包、macOS Launcherの
  コールドブート、arm64 DMGの作成とマウント後の収録物の照合を通過した。このDMGはCI専用で
  互換PythonとPackVM関連の資産を含む。通常のGUIから実1Bモデルで会話する検証と、
  製品から互換ランタイムを外す作業は継続する。

## 054: Side Chatの保存ターンからローカルモデルを呼ぶ経路

- `6fdecdce7` はSide Chatの送信から、Saved Turn、Saved Orchestration、Gateway、Adapter、
  Provider Registryを通る既存の呼出しに、Launcherのローカルモデル実行を接続した。
  同じAdapterを使う通常チャットとSide Chatを実行中の親から識別し、Side ChatのProfile、
  turn、子会話とリビジョンを照合する。Providerの設定、モデルの選択方法と保存形式は共通。
- `1960b9995` は通常チャットとSide Chatそれぞれの生成・ストリームに専用の起動証明を持つ。
  Side Chatの署名済みPackと実行カタログも、ローカルAPIの送信許可前に現在のBundleと照合する。
  API送信、入力のコンパイル、応答の投影は元のRustおよびWasmを再利用する。Launcherが
  Side ChatのIPC対応を示した場合にこの経路を用い、既存IPCの通常チャット契約も維持する。
- PythonのローカルチャットIPC 62件、Rustの署名選択44件、Launcher IPC 55件とジョブ30件、
  Tauriテストの型検査が通過した。通常チャットの署名済みHTTPで、既存のローカルモデルの
  開始、送信前の拒否、再試行と送信後の応答喪失の扱いを照合した。
- `1b1a3d516` の署名済みHTTP試験は、Side Chatの生成とストリーム、互換入力への再試行、
  ポリシー拒否、応答喪失と再照合を通過した。生成を選ぶ試験ではストリームの準備結果を
  fixtureで指定し、通常のストリームと既存のProfile・モデル・Provider所有者を通した。
- macOSの分離試験は実Launcherソケットとv7の実Wasmを使い、両方の経路でRustプロセスが
  ループバックAPIへ送信したことを確認した。生成は返信の保存と再照合まで完了した。
  ストリームはWasmによる応答の返却後に、元の画面と同じ30秒の要求期限に達した。
  起動証明を先に準備した試験でも、Adapterへ到達した時点の残りは5.20秒で、
  IPCに1.19秒、そのうち41回の現在性検査に0.88秒かかった。
- 同じ署名済みHTTPのストリームを既存Python搬送と同じ疑似API応答で比較し、こちらは
  保存と再照合まで通過した。署名済みHTTPを段階ごとに計測すると、通常とストリームの
  準備照会および実行で、それぞれ同じProviderのヘルスを二度読んでいた。
  返信の追記前には、間に別の処理を挟まずに子会話とその親を二度ずつ読んでいた。
- `f667a08c3` は返信の追記前に子会話と親を一度ずつ読み、確認済みの枝と容量を検査して
  保存ロック下の照合に進む。`001fcf6d7` は現在の生成またはストリーム操作に紐付く
  Providerに絞ってヘルスを読む。元の署名済みProvider照合を通す。
  `d70779e09` でGatewayの実行目録とProfileの署名対象を再生成し、6種類の生成物検査と
  アーキテクチャ検査が通過した。Gateway、保存用ブリッジ、Side Chat Pack、保存容量、
  署名済みSide Chatの保存経路のテスト238件が通過した。
  `880c818ad` はワークスペースのガード試験のfixtureに未設定だった2つのNative経路を
  初期化し、同ファイルの19件が通過した。
- 同じmacOSの分離試験でRustプロセスによる生成・ストリームの送信、実Wasm応答、
  返信の保存と再照合まで確認した。Viewの30秒の期限でストリームの送信操作は約28.4秒。
  ここではAdapterが実LauncherとWasmを通り、Setup、モデル推論と他の保存段階は試験用構成。
- 同じ分離試験で、Turnの開始・実行権取得、Saved Orchestration、メッセージ追記、確定、
  読取りも実Launcherで選択すると、GatewayがAdapterへ渡す前に期限に達した。
  開始の2回のIPCに各0.94秒と1.08秒、Saved Orchestrationの各ステップに約0.54秒、
  ユーザー文の追記に1.01秒。単独で通過したAdapterの工程より前に累積している。
- `dd5ad55a2` は保存経路の複数ターゲットを同じ同期検査で確認し、各ターゲットの
  Domain検査を通す。IPCの入力送信とLIVE応答は送信直前のガードを使い、重複した直前の
  呼出しを省く。プロンプトの別読みがないユーザー文は、子会話と親を一度ずつ読み、
  保存ロック内でも元の子のリビジョンと親の束縛を検査する。
  IPCとガードの533件、親子会話の追記と親変更拒否の4件が通過した。
- 組み合わせた分離試験でAdapterへの到達時に2.29秒残り、RustプロセスによるローカルAPI
  送信と応答を確認した。返信は保存前に期限に達した。開始の2回は0.65秒と0.76秒、
  Saved Orchestrationの各IPCは0.43〜0.44秒。そのうちWasmの準備は約0.12秒だった。
  同じ保存構成と疑似APIでAdapterだけを既存Python搬送に戻した比較でも、送信と応答の後、
  返信の保存前に期限に達した。画面の期限内に全段階を通す検証を続ける。
- `978247b17` はLauncherの待受確認で同じ全ソケットのPID判定を再利用する。
  照合の前後でPIDの起動時刻を確認し、競合調査でだけ必要なコマンドと作業フォルダを
  通常の照合から外した。実ループバックで照合1回の中央値は67.09msから30.32ms、
  Rustの単体試験2件とLauncherのテスト対象の型検査が通過した。
  上記の分離試験で待受確認はfixtureのため、ここで測った短縮は別の値。
- `5fcb795ef` はLauncherの梱包検査で不足していたHostモジュール3件を許可リストへ追加した。
  画面表示、会話完了と共通のWasm起動を担う既存モジュールで、同梱の検査4件が通過した。
  先行コミットの [DMG CI](https://github.com/harupipipipi/tobkiri/actions/runs/44558192197)
  も同じ同梱リスト検査で停止した。更新した作業ブランチでDMGを再検証する。
- `d64729908` で `AuthorityStore.inspect_dispatch_authority` のSecurityEpochと失効対象を
  1回の照会にまとめ、認証済みLeaseのダイジェスト計算を2回から1回に減らした。
  各呼び出しで新しい接続を開き、元の鍵、リース、ポリシー、8組の失効対象と全体失効を
  検査する。認可、資格情報、親呼び出しの検査とポリシーの回帰テスト129件が通過した。
  同じストアで各800回交互に計測した中央値は2.30msから2.16ms。全経路での短縮は未計測。
- `a8d207cc2` は通常の読み取り専用の経路確認で、プロンプト、Project、Workspace、Strategy
  の別読みがない場合に、同じ親子会話を取り直さない。後続の読み取りと効果実行は会話を
  検査し直す。事前確認の経路照会中に親が変わると、後続の読み取りで拒否する。プロンプトの
  読み取り中に親が変わった場合は経路照会前に拒否する。署名済みProfileのfixtureを使った
  Bridge、Side Chat、Project、Workspace、会話ライフサイクルの190件が通過した。
- 組み合わせたmacOSの分離試験では、保存ターンの実行権取得と最初の経路照会の間の会話
  読み取りが4回から2回に減り、最初の経路照会の開始時点が残り20.71秒から21.90秒になった。
  RustへのローカルAPI実行依頼を開始した時点の残りは2.71秒から3.55秒。返信の追記に
  残り0.38秒で到達したが、元の30秒期限のため完了前に停止した。実モデルとLauncherの現在性検査は
  この試験の対象外で、毎回のWasm準備の実測値から製品の全経路時間は判断できない。
- 廃止候補は、`rumi_provider_adapters_pack/runtime/adapter.py` が持つ同じローカル要求の
  コンパイル・HTTP・応答処理。現状の置換は保存済みの認証なしOpenAI互換ローカル接続。
  APIキー付きのループバックと元のProvider種別、送信前のフォールバック条件を製品経路で
  確認してから、互換ランタイムの重複を除く。
- `4dfe3f08b` は、Wasmで要求を組み立てた後のProvider照合に、同じRegistryを再解析する
  処理を省く経路を追加した。元の送信直前の物理再読を行い、Profile、接続ID、リビジョン、
  endpointとファイルのバイト列が同じ場合だけ解析結果を使う。異なる場合は元の形式と
  モデルポリシーを改めて照合する。実ループバックと物理Registryの4件、Cargoの型検査が
  通過した。分離したmacOS試験で照合の中央値は、1件の設定ファイルで0.267→0.194ms、
  201件のfixtureで4.467→0.321ms。各経路を51回交互に計測し、モデル推論は含めていない。
- 既存Setup、引き継ぎ、Launch、設定のLauncherテスト47件と、承認付き設定、保存済み子会話、
  Side Chatの進捗と復旧のwebappテスト22件が通過した。DOMとHTTPのfixtureによる回帰検査。
  配布DMGは元のSetupとLaunchを行い、権限が必要な操作から専用の承認ウィンドウを開く。
  設定を承認するケースでは prepare→GUI承認→保存を通し、保存済みの実1Bモデルで
  Side Chatを送信する。子会話を開き直して返信の永続化、親の履歴、推論の呼び出し回数を確認する。

## 055 親子会話の読み取りを同じWasmスナップショットにまとめる

- `829fd135a` は `local.saved-conversation.legacy-linked-context@1.0` に子会話と親の継承項目を
  取得する関数を追加した。Coreが対象Profileの既存ファイルを1回読み、Wasmが同じ文書で
  保存済みのリンクを照合する。別の親IDを引数に取らず、親の履歴や継承対象外のメタデータを
  省く。従来のGETと保存レシートは同じWIT、結果と認可を使う。
- Conversation Packの単体試験41件、WASI2ビルドとPack検証、macOSで実Wasmと元の会話ファイルを
  使った物理所有者の4件、CI定義の試験38件が通過した。物理試験は親のモデルとリビジョンを
  書き換え、次の呼び出しが更新を読み取ることと、Wasmの読み取りがファイルを変更しないことを確認した。
- `e0c746106` はLauncherの内部IPCを接続した。稼働中の管理対象Hostと元のSaved Orchestrationの
  証明を要求し、署名済み配布目録、Packと固定ポートを読み直す。選択するIDは子会話だけで、
  旧Packの通常GETも通る。Launcherのテスト対象の型検査、分離したIPC 56件、ジョブ31件、
  Pack宣言の互換試験1件が通過した。`f63e4f04e` は同じテストをLauncherの梱包CIに含める。
- `d140e5858` は同じWasmの文書から、親への通常GETと同じバイト列をホスト内部用に生成する。
  上限内に収まる場合だけ候補を返し、Launcherの応答は元の投影を使う。単体試験41件、
  配布Packを使うmacOSの物理所有者試験4件、Launcherの応答デコード試験が通過した。
  物理試験で、親を更新した前後の双方について、内部用の応答と通常GETをバイト単位で照合した。
- `eb6f8cd2d` はCoreに親応答の一回限りの候補を持たせる。同じ制御台帳、期限、保存先、Profile、
  親ID、Packの内容を照合し、親GETが読み取った会話ファイルのバイト列をSHA-256で照合する。
  一致しなければ、そのGETで読み取った文書を既存のWasmに渡す。実Wasmと物理ファイルの4件で、
  同一内容の再利用、同じファイル長とリビジョンの編集、別ID、シンボリックリンク拒否を確認した。
  CoreのClippyとRustの書式検査も通過した。
- Side Chatの実行経路への組み込みでは、元の親子それぞれのBroker認可、利用上限、監査結果と
  サイズ制限を維持する必要がある。親の認可を通してから投影を使い、従来の読み取りと同じ結果を
  確認する。途中で会話ファイルが更新された場合は、その時点の所有者から読み直す。実行経路への
  組み込みと30秒の全経路検証が残る。
- ネイティブ容量と受け入れ条件のCIは既存のテスト3箇所の書式で停止した。`eeeba36aa` で修正し、
  再実行した [容量検証](https://github.com/harupipipipi/tobkiri/actions/runs/44576067515)、
  [受け入れ条件6〜10](https://github.com/harupipipipi/tobkiri/actions/runs/44576067495)、
  [受け入れ条件13〜16](https://github.com/harupipipipi/tobkiri/actions/runs/44576067793) が通過した。
- [実1Bモデル CI](https://github.com/harupipipipi/tobkiri/actions/runs/44569439245) は `adbec3a8c`
  で通過した。承認済みのローカルProvider設定、認証付きループバック、保存済みのモデル選択を
  RustとWasmで確認した。`c0220c03a` での [再検証](https://github.com/harupipipipi/tobkiri/actions/runs/44574137606)
  も通過した。インストール済みDMGでのSetupからSide Chatの返信保存までの試験は別途必要。
- `6b5342749` は、元の別々の子GETと親GETに再利用を接続した。子GETの通常応答を送った後、
  Launcherが容量計上済みの親応答を一件だけ保持する。親GETは元のBroker認可を通り、同じ
  Launcherリスナーと一回限りの内部識別子を照合して受け取る。候補が使えない場合は通常の
  読み取りを行う。Launcherの型検査、分離した88件のIPC・ジョブ・サービス試験と、実UNIX
  ソケットを使った1件が通過した。実ソケット試験では取り消し、異なる識別子、使い切りと、
  同じリビジョンで書き換えられた親の再取得を確認した。
- `746f5f087` は、署名済みSide ChatのHTTP試験で親と子が別々のLeaseを取得し、各読み取り後に
  個別に確定することを検査する。再利用の識別子は通常のBroker入力に入らない。署名済みProfile
  を使ったHTTP、Host側IPC、保存ブリッジの計612件が通過した。
- 署名済みSide ChatのHTTPと実Launcherをつないだ比較では、親GETの中央値が0.46秒から
  0.31秒に短縮され、5組の親子読み取りは4.57秒から3.93秒になった。どちらもSaved
  OrchestrationのGETに同じネイティブ起動証明を用いた。再利用ありではストリームAdapterに
  残り0.48秒で到達したが、どちらもローカルAPIに送信する前に元の30秒期限に達した。
  この試験は署名済みProfileの分離環境で、DMGからの起動や実モデルの推論は別工程。
- 純Rustホストへ移した後の削除候補は、Python Hostが同じ外側の呼び出し内で子と親のBroker
  セッションを結ぶ `_LinkedParentRead` と、この機能を通知する移行期の環境変数。通常GET
  と同じ鮮度条件や別々の認可がRust側で検証できる段階で除く。
- `53d3a3798` は、ユーザー文の保存後にAI段階で行う再照合をRustへ接続した。最初の子GETと
  親GETが両方とも同じ所有ファイルの内容を確認した時だけ、通常GETと同じ親子の応答を保持する。
  再照合でも子と親が個別にBroker認可と監査を通り、Rustがその都度、現在のファイルの全バイトと
  Profile、Pack、対象ID、期限を照合する。一致したGETはその応答を使い、違えば現在の文書を
  読み取る。Prompt・Tool・Workspace・Project・Strategy・Workflowの別の読み取りを伴う
  経路では、再照合向けの候補を使わない。
- 実Wasmと物理ファイルを使う会話ストアの4件が通過した。通常GETと生の応答バイトを比較し、
  初回の親GET前、再照合の子GET前、子と親の再照合の間の編集で元の候補を使わないことを
  確認した。Launcherの分離したIPC・ジョブ・サービス88件と実UNIXソケット1件、HostのIPCと
  親子読み取りの36件、CoreのClippyも通過した。Launcherは送信済みの応答の後でだけ、次の
  フェーズ用の候補を公開する。同じリスナー、内部識別子、フェーズと一番早い期限で照合する。
- 署名済みSide ChatのHTTP回帰を、作業コミットから作ったクリーンなProfileで実行した。
  初回・再照合それぞれの親子GET 4回が異なるLeaseを取得し、個別に確定することを確認した。
  内部識別子を通常のBroker入力に入れず、元の返信・保存応答を検証した。この試験のモデルと
  ネイティブ返信はfixtureを使用した。
- 同じ署名済みSide Chatから実Launcherソケットと実Wasmへ送る分離試験で、新しい親子再照合の
  IPCは子0.33秒、親0.32秒。ストリームAdapterに残り0.42秒で到達し、疑似APIへの送信前に
  30秒の期限に達した。実モデル・製品GUIでの計測と、全ターンの期限内完了は未確認。
- 同じローカルの配布用Wasm 9件を、実行Engineの設定を揃えて7回ずつ交互にコンパイルした。
  Cranelift `OptLevel::None` で起動とコンパイルの中央値は各7〜18ms短縮したが、全9件で
  コードイメージが大きくなった。フルターンの実行時間やゲストの消費量は未計測のため、
  この比較ではコンパイラの設定を変更していない。
- 通常の会話一覧も `local.saved-conversation.legacy-list@1.0` を通せるようにした。元のShellの
  署名済み読み取り権限、認証済みHTTP、実行中のBroker Leaseと容量予約に結び付け、Coreが
  既存のProfileファイルを一度読む。ゲストは従来の保存キー順、会話とメッセージの正規化、
  migration情報とPythonの数値精度を維持する。新しいポートがある署名済みアーカイブにだけ
  IPCレベル3を通知する。親子の再照合も有効な場合は `3`、一覧だけを追加する場合は `3-list-only`。
  Packの45件、strict Clippy、WASI2リリースビルドと実CLIでのPack検証が通過した。実Wasmを
  使うmacOSの5件では、会話一覧をPython応答とバイト単位で照合し、元ファイルに書き込まないことと
  読み取り・追記・確定の互換を確認した。IPCの763件と署名済みProfileのHTTP 2件も通過した。
  HTTP試験ではLauncherへの呼び出しを差し替え、認証済み一覧と既存の保存ターンを照合した。
  元のPython `ConversationStore.snapshot()` を除くには、その他の呼び出し元とサイズ上限を超える
  既存の会話ファイルを扱う経路も置換する。表示側の検証と旧アーカイブとの互換性を保つ。

## 056 既存Provider Registryの読み取りと設定画面の投影を移植

- `24da1c8bb` は、元のProfileの物理RegistryをCoreで読み、新しい
  `local.provider-registry.legacy-resource@1.0` のWasmへ渡す。既存のRustのRegistry検証と
  設定画面用投影を共用し、モデル実行向けの応答と、資格情報ハンドルや接続先を省く設定画面の
  応答を再現する。数値を途中のCore JSONで再解釈せず、元の形式のJSONとして渡す。Packへの
  ストレージ権限や資格情報へのアクセス権の追加はない。
- WASI2ビルドとPackアーカイブの生成・検証が通過した。Packの69件、Profile別の物理ファイル、
  設定の投影、シンボリックリンク拒否、従来の承認付き保存を含む実Wasmの2件、CI定義38件と
  Core・WasmのClippyが通過した。物理試験でWIT宣言に `json` が必要と分かり、宣言を
  Coreの既存応答型と揃えて再検証した。物理試験を実1BモデルのCIにも追加した。
- `rumi_provider_registry_pack/runtime/registry.py` の `snapshot` と設定画面の投影が削除候補。
  Launcherの起動証明と、元のShell・Gateway・Adapterの署名済み経路にそれぞれ接続してから
  廃止する。Adapterの読み取りには、そのAdapter自身の個別の認可が必要であり、Gatewayの
  準備結果を転用しない。署名済みSide Chatの送信ではこの接続と30秒内の完了をまだ確認していない。
- 元の必須モデル準備確認とストリーム選択は時点と入力条件が異なる。前者はユーザー文保存前の
  拒否を担い、後者は新しい会話と稼働中のターンで確認し、使えない場合に通常応答を選ぶ。
  各段階をまとめると既存動作が変わるため、目的の移植では各段階のProvider読み取りを置き換える。
- `e74abf70e`、`2406d8ce1`、`03555a509` は、保存済み会話でAdapter自身が取得する
  Registry応答をRust＋Wasmにつないだ。従来どおり本文にProfile IDがなくても、同じ
  認可済みProfileから読む。Launcherは各Adapter経路とRegistryの受領書、Packのポート、
  実行中のHost・期限・取り消しを照合する。既存の別々のBroker Leaseと容量計上を通す。
  非対応の旧Packや形式・サイズの互換範囲では、現在性を確認して元の読み取りへ戻る。
  Shell、Gatewayと設定画面の読み取りは移植の残り。Launcherの型検査とHostのIPC試験38件が
  通過した。LauncherのIPC・ジョブ・サービスの分離試験88件も通過した。
- 署名済みSide ChatのHTTP回帰は8ケースを完走した。通常・ストリーム応答のほか、旧形式への
  切り替え、拒否、応答未確認を検証した。テスト内でAdapterとRegistryのLeaseと容量予約が別々で、
  各段階の期限を超えず、読み取り完了後に同じ認可を使用できないことを確認した。この試験では
  RegistryとモデルのネイティブIPCの応答にfixtureを使用した。
- 同じ署名済みProfileと配布用Wasmを使ったmacOSの分離ソケット試験では、Side Chatの通常・
  ストリーム両役割で物理Registryを読み取れた。取り消し、別役割の拒否、元ファイルの不変を
  併せて確認した。アプリの実プロセスマネージャーはfixtureを使用した。
- 署名済みSide ChatのHTTPから実ソケットとWasmを通し、ストリーム用のRegistry応答が元の
  所有者と一致した。読み取りは0.54秒。Rustプロセスがループバックの疑似APIに一度だけPOSTし、
  通常の30秒期限内（送信処理28.70秒、残り1.16秒）に返信を保存した。プロセスの起動は
  fixtureで、会話の他の処理には既存Hostを使用した。
- [macOS CI](https://github.com/harupipipipi/tobkiri/actions/runs/44569440075) は
  `adbec3a8c` でLauncherの起動、DMGの生成とマウント済みリソース検査が通過した。
  Setupから会話までを同じインストール済みアプリで通した結果は引き続き必要。
- [Gemma 3 1Bの再検証](https://github.com/harupipipipi/tobkiri/actions/runs/44588924001) は
  `7fa028932` で通過した。Adapterの新しい起動時IPCをつなぐ前のビルドを対象とする。
- `03c92d9f7` と `9538438c2` は、通常会話とSide ChatのGatewayが行うRegistry読み取りを
  Rust＋Wasmへつないだ。通常生成、ストリーム、両方のルート確認に、それぞれ元のGatewayと
  Registryの署名済みGrantを使う。同じターンでも各呼び出しで現在のRegistryを読み、
  Adapterも従来の個別認可で読む。旧Launcher、旧Pack、互換範囲の入力では元の所有者に戻る。
  Launcherの型検査とHostのIPC関連59件が通過した。
- 署名済みProfileを使うHTTP回帰で通常会話とSide Chatそれぞれ8ケースが通過した。
  各Registry呼び出しのLeaseと容量予約を照合し、互換読み取りと拒否も確認した。
  RegistryとモデルのIPC応答にはfixtureを使用した。
- LauncherのIPC・ジョブ・サービスの分離試験90件と、Gatewayの署名選択試験が通過した。
  macOS上で署名済みSide ChatのHTTP、実ソケット、配布用Wasmを接続した分離試験では、
  2種類のルート確認、ストリームGateway、AdapterのRegistry応答4件が元の所有者と一致した。
  各読み取りは0.48〜0.54秒。Rustプロセスがループバック疑似APIに一度だけPOSTし、返信を
  29.11秒で保存した（30秒期限まで0.77秒）。試験用のプロセス起動と既存Hostを使用した。
- 旧 `snapshot` を削除する前に、Defaultsの署名済みProfileに残る呼び出し元を移す必要が
  ある。接続先と資格情報を省く応答はShell、Model Registry更新、Commandのデータソース、
  モデル検索が使う。テキスト生成、Messages API、音声のGateway・Adapterには生のRegistry
  が必要。各入口で今と同じ応答と認可を確認するまで、共通のPython所有者を残す。
- `91317d0ba` と `3e584db85` は、既存の接続ステータスGETとModel Manage内のProvider読み取りを、
  接続先と資格情報を省くWasm投影につないだ。後者はModel Saveの実行と別のBroker Leaseを
  使い、モデルを書き込む権限を渡さない。Launcherは元のShell直下のGrantとModel Manageから
  のGrantを別々に検証する。旧Launcherでは従来の読み取りを使う。移植の残りはCommandの
  データソース、モデル検索、テキスト生成・Messages API・音声経路など。
- 既存の署名済みHTTPとHost IPCの関連68件、Launcherの型検査、IPC・ジョブ・サービスの
  分離試験90件、Shellの署名選択試験が通過した。macOSの実ソケットと配布用Wasmを使った
  分離HTTP試験では、接続ステータス1回とModel Manage内の読み取り4回で元の公開応答に一致し、
  接続先と資格情報ハンドルが含まれないことを確認した。各読み取りは0.34〜0.39秒。
  Launcher起動とModel Saveの書き込み応答はfixtureを使用した。
- `c9695a228` と `58f57ca62` は、モデル検索とCommandのデータソースに同じWasm投影を
  接続した。両方とも元のShellから各問い合わせの所有者を経る署名済み経路を要求する。
  モデル検索の接続指定なしの経路は従来どおりRegistryにアクセスしない。Defaultsの元の
  Profileにある公開投影の4呼び出し元を移した。次はWorkflowのテキスト生成とMessages API
  のGateway、およびModalityを経由する音声Adapterの生の読み取りを確認する。
- Host IPCと署名済みHTTPの関連38件、Launcherの型検査が通過した。元の正規の応答との一致、
  両方のCommandデータソース、未認証時の拒否、認可終了後の読み取り不可を確認した。
  `feb0f80bb` は既存のモデル検索の契約テストに残った重複キーを取り除き、正式な定義と揃えた。
- LauncherのIPC・ジョブ・サービスの分離試験90件と、問い合わせの署名選択試験が通過した。
  macOS上の署名済みHTTP、実ソケット、配布用Wasmを接続した分離試験で、モデル検索1件と
  Commandデータソース2件の公開応答が元の所有者と一致し、接続先と資格情報を含まないことを
  確認した。各読み取りは0.38〜0.40秒。Launcherの起動はfixtureを使用した。

- `3d47446ed` と `fddfd197d` は、Workflow のテキスト生成と Messages API の Gateway、
  および音声認識と音声合成の Adapter が行う Registry 読み取りを Rust＋Wasm へつないだ。
  Launcher は元の Shell から Workflow への入口と、各呼び出し元の署名済み Grant を確認する。
  Host は同じ Profile、Broker の親 Lease と期限、Registry に割り当てた個別の容量予約を
  照合する。旧 Launcher または署名済み経路に該当しない呼び出しは従来の経路を使う。
- `core-host` 構成の型検査と、Launcher の IPC・ジョブ・サービスの分離試験91件、追加の署名選択
  試験2件が通過した。macOS の実ソケットと配布用 Wasm を使い、署名済み Workflow の
  `run.advance` から4種類の読み取りを実行した。元の Registry 応答とすべて一致し、所要時間は
  1.03〜1.42秒だった。Launcher のプロセス管理と外部 Provider への通信には試験用の実装を
  使用した。
- Host IPC、従来とネイティブの Workflow、Gateway と音声 Adapter の関連102件が通過した。
  `426980433` は、旧モデルの資格情報ハンドルを Provider に渡すと想定していたテストを、
  現在の Provider 接続が資格情報を管理する仕様に揃えた。
- [前回の DMG CI](https://github.com/harupipipipi/tobkiri/actions/runs/44595543404) では、従来 Launcher
  構成のテスト用証明を作る2か所で `provider_snapshot` の初期化が欠けていた。両方に初期値を追加し、
  Workflow の呼び出し元と入口を個別に照合するテストを DMG の CI に組み込んだ。
- `2791b4223` と `a7c9e94a6` は、Workflow のテキスト生成と Messages API が共用する
  `rumi_provider_adapters_pack.provider.compatibility.generate` の Registry 読み取りも移した。
  元の Gateway ごとに Workflow までの親を照合し、Adapter 自身の署名済み Grant と
  個別の Broker Lease・容量予約を使う。旧 Launcher はこの二つの読み取りに従来の経路を使う。
  Host の関連116件、Rust の IPC・ジョブ・サービス91件、署名選択3件、起動証明2件が通過した。
  macOS の実ソケットと配布用 Wasm では、署名済み Workflow からの全6読み取りが元の
  Registry 応答と一致した。追加した2読み取りは1.49秒（テキスト生成）と1.24秒（Messages API）。
  Launcher のプロセス管理と外部 Provider への通信には試験用の実装を使用した。
- `b1aa43d94` と `a41cdbca1` は、設定画面の Model Access の `model-access-read` と
  `model-access-catalog` が行う物理 Registry 読み取りを Rust＋Wasm へ移した。
  元の Shell の各署名済み Grant を別々に検証する。カタログ取得の前後の2回も同じ
  Broker Lease と期限で読み、途中の削除や変更は拒否する。カタログの取得は元の個別認可を使う。
  `12207e06f` は Gateway、Adapter、Workflow と設定の Registry 経路選択を既存の照合に集約した。
- 関連91件、Rust の IPC・ジョブ・サービス92件と追加の署名選択・起動証明2件が通過した。
  macOS の実ソケットと配布用 Wasm では、表示とカタログ取得前後の3読み取りが元の応答と一致した。
  各読み取りは0.34〜0.36秒で、取得中の削除に対する拒否も確認した。プロセス管理と外部
  カタログ通信には試験用の実装を使った。
- `9ca1c6ec8` は、Model Access のポリシー表示、検索フィルターの正規化、カタログ応答の
  生成を `local.provider-registry.model-access-projection@1.0` に実装した。既存の
  `local.provider-model-access-rust` のポリシー検証と OpenRouter フィルターを共用する。
  Core は検証済み Pack と現在の Profile の物理ファイルを選ぶ。カタログでは取得前後に
  読んだ Registry を既存の変更確認に使えるよう、公開応答と一緒に内部向けの値として返す。
- WASI2 ビルドと Pack 生成・検証、コンポーネントを含む単体76件、Core と実 Wasm を使った
  物理 Registry の3件が通過した。表示と公式カタログのフィルター、別 Provider の結果の除外、
  元ファイルの不変、取得前後の Registry の差を確認した。
- `ca2f0c66f` と `537a54e58` は、Model Access 投影を既存設定画面の HTTP、Host Broker、
  Launcher IPC へ接続した。マーカー8は元の個別 Grant と配布 Pack の投影ポートがそろう場合に
  通知する。Launcher は64 MiB枠内で本文を予約し、開始フレームのフェーズと本文を照合して読む。
  カタログの取得は元の認可で一度だけ行い、前後の Registry が一致した場合に公開応答を返す。
- Launcher の分離テスト98件が通過した。配布用 Wasm と macOS の実ソケットを使った3ケースで、
  通常表示、取得中の削除の拒否、1 MiBを超えたカタログを取得済みの結果で処理する互換経路を
  確認した。外部カタログ通信と Launcher プロセス管理には試験用の実装を使った。
- `5800a1c85` は、1 MiBを超えるカタログのうち投影に必要な項目を抽出し、約512 KiBずつ
  既存のWasmへ渡す。取得は元の認可で1回。各ページで同じBroker Leaseと期限を使い、
  取得前と各ページで確認したRegistryが一致したときだけ、元の順序でモデル一覧を返す。
  IPCの1 MiB上限と64 MiBの容量予約は既存設定を維持する。
- 関連102件が通過した。未使用データの大きい応答、公開一覧で約1.55 MBになる4,000モデルの
  順序、途中のRegistry変更による応答の拒否を確認した。macOSの実ソケットと配布用Wasmを
  接続した3ケースも通過した。4ページの結果が元の投影と一致し、変更を検出したケースは
  2ページ目で拒否した。外部取得とLauncherのプロセス管理には試験用の実装を使用した。
- 単一レコードが既存の投影上限を超える場合や表現できない場合は取得済み結果での互換処理を
  継続する。互換処理とPythonの共通読み取りは、該当する経路の移植と同等性確認後に廃止する。
- [実1BのCI](https://github.com/harupipipipi/tobkiri/actions/runs/44611137608) は前の
  `d73f334bf` で成功した。認証付きのGemma 3 1Bは37個の差分を返し、完了したターンを保存した。
  次のターンをキャンセルするとFlowは61 msで終了し、その後2.350秒でllama.cppのスロットが
  空いた。保存済みのRegistryから同じモデルを選び、鍵なしのllama.cppへ接続する試験も通過した。
- Gatewayはルート選択時に、Registryのスナップショットに加えて独立した署名済みポートで
  Providerの利用可否を読む。Wasmの `local.provider-registry.legacy-health@1.0` を追加し、
  既存Rustの `Registry::health` を、Coreが選んだProfileの物理Registryに接続した。
  レコード順、未確認の `unknown`、検証済み時刻の小数を維持する。応答は接続先や資格情報を含まない。
  旧Python所有者と元のGatewayを外す前に、通常とSide Chat、通常応答とストリームそれぞれについて
  起動時の独立したヘルス権限と呼び出し経路を照合する。取得エラー時もGatewayは空のヘルスで
  処理を続けるため、実呼び出しの成功と、その値をモデル選択に使ったことも確認する。
- ソースからWASI2にビルドしてPackを検証した。macOS上ではCoreと実Wasmを通す3件、
  コンポーネントを含むRustの単体76件が通過した。元のPythonで採取した確認済み・未確認・
  不正な確認時刻の結果を照合し、別Profileとシンボリックリンクからの読取りを検査した。
  同じ物理検証を配布用のmacOS CIにも追加した。
- 通常チャットのGenerateとStreamをこのポートへ接続した。Launcherはそれぞれの
  Shell→Turn→Orchestration→Gateway→Healthの署名済み経路とWITを照合し、起動時に
  通知した経路だけ再検証して開く。Hostは個別のBroker Leaseと使用量予約、親の期限と
  現在性を確認してから渡す。Side Chat、Workflow、route quoteには各経路の既存処理を使う。
  古い配布物や互換範囲の入力では元のRegistry所有者へ戻る。
  Rustの署名済み経路の回帰52件と、PythonのIPC回帰623件が通過した。署名済みHTTPの通常
  Generate・Streamで、Healthの戻り値、別の操作権限の拒否、Borrow終了後の拒否を照合した。
  Launcher側のバンドルと起動時証明を含む試験も配布CIに追加した。
- Side ChatのGenerateとStreamも同じWasmのHealthポートにつないだ。別々の起動時証明に
  Shell→Side Chat→Turnの署名済み経路を含める。Hostは `send` で使ったターンID、会話ID、
  リビジョンと各親の呼び出しを照合し、ヘルス読み取り専用のLeaseと使用量予約を確認する。
  Side Chatの証明がある起動はIPCレベル10を通知する。Workflowとroute quoteは既存の読み取りを使う。
  Rustの経路選択52件、PythonのIPC 627件、署名済みHTTPの通常チャットとSide Chatが通過した。
  HTTPではネイティブ呼び出しを差し替え、両方の配信方式、権限拒否、親の終了後の拒否と
  互換処理を比較した。インストールしたLauncherでの実呼び出しは配布物で確認する。
- 通常チャットとSide Chatのルート見積もりもWasmのHealthポートにつないだ。元のProfileは
  `ai-gateway.route-quote` に `.generate` と `.stream` のヘルス参照を個別に認可する。
  起動時は会話入口、Gateway機能、Health処理を組み合わせて照合し、対応する証明があれば
  IPCレベル11を通知する。Hostは `buffered` または `incremental` の実リクエストから
  見積もり方式を決め、親を保持した状態で合致する読み取りを実行する。Registry一覧とHealthは
  同じPackの独立したWasmポートを使い、各操作の署名とLeaseを照合する。
  Rustの経路選択52件とPythonのIPC 637件が通過。署名済みHTTPの4件では、ネイティブ呼び出しを
  差し替え、4種の見積もり、反対側の配信方式を元の所有者で処理すること、親の終了後の拒否、
  従来の互換経路を確認した。Launcherのソケットとバンドルを含む試験は配布CIで行う。
- Workflow TextとMessagesの `.generate` をWasmのHealthポートへ接続した。Launcherは
  元のShellの入口、Workflowから各Gatewayへの署名済み権限、Health用の署名とWITを照合する。
  同梱Profileの入口は `run.advance` と `run.step.resume` を使い、Registry一覧と入口の照合を共用する。
  Hostは実行中の入口とGateway、子のLeaseと容量予約を読み取りの前後で検査する。
  起動時はIPCレベル12を通知し、Healthポートのみがある場合は `12-health-only` で通知する。
  この場合、通常のRegistry一覧には元の読み取りを使う。型付き音声とネストしたAdapterの
  Registry一覧は、引き続きそれぞれのポートと実行経路を使う。
  署名済みWorkflowの回帰3件では、ネイティブ応答を差し替え、TextとMessagesの応答、
  `.stream` の拒否、Lease終了後の拒否、従来およびRegistry一覧の経路を照合した。
  Rustの経路選択54件とPythonのIPC 674件が通過し、Healthポートだけを通知した場合の
  IPCフレームと通常読み取りの互換処理を検証した。Launcherを含む試験は配布CIで行う。
- `conversation.turn.v1/complete` が呼ぶGatewayの `.generate` もWasmのHealthポートへ接続した。
  Launcherは元のShell、Application、会話完了、Gatewayの署名済み文書にHealth権限を加えて照合する。
  Hostは会話完了とGatewayの実行中のLease、子の専用予約、実行ドメインを照合する。
  IPCレベル13を通知し、Healthポートだけの場合は `13-health-only` を使う。
  Rustの経路選択55件が通過し、保存ターン用の権限や `.stream` のHealth権限を使わないことを確認した。
  IPC 703件と署名済みHTTPの会話完了2件が通過した。HTTPでWasm側の応答とGatewayの推論結果を
  差し替え、Wasmを選ぶ場合と元のPackVM ABIを通す場合の両方で、同じ署名済みHealth呼び出しと
  終了後の拒否を照合した。PackVM経路では旧Registry所有者への互換処理も確認した。
- 権限レビューのQuoteとGenerateをWasmのHealthポートへ接続した。Launcherはレビュー担当Packの
  署名済みの識別情報と、用途ごとのGateway・Health権限を照合する。Gatewayの権限は
  `request_surface` と `tool_calling` を固定し、Generateでは `allow_failover` も固定する。
  Hostは準備したGateway要求のダイジェスト、レビュー専用セッション、元の保存ターンを保持し、
  Healthの読み取り前後に有効なLeaseと呼び出しの一致を確認する。起動時にはIPCレベル14を通知し、
  Healthポートのみを使う場合は `14-health-only` を通知する。
  Rustの経路選択57件とPythonのIPC・レビュー・Broker試験767件が通過した。署名済みProfileで
  QuoteとGenerateの経路が別々に選ばれることを確認した。実Brokerを使うGenerateの試験では、
  Healthの応答を差し替えて利用不可時の生成停止と権限失効後の拒否を確認した。署名済みProfileの
  レビューから配布Launcher上のWasmまでを通した試験は残っている。
- 旧 `GET /api/ui/provider-health` の宣言と、カタログ順・資格情報の設定有無・スコープを投影する
  `provider-health.v2-compat` はv3互換コードに残る。配布LauncherのLaunchは署名済みv4の経路表を使い、
  この旧経路の直接呼び出しとv4名前空間経由の呼び出しはどちらも404になる。認証済みの本番構成HTTP
  回帰では両方の拒否に加え、設定画面が使う署名済み `GET /api/connections/status` の成功を照合する。
  接続状況は起動時に個別の読み取り権限とWasmポートを確認できた場合にRegistryのネイティブ投影を使い、
  互換範囲では同じ署名済み操作のPython Registry所有者が処理する。

## 057 ProfileのPackバージョン保存後に編集を再開

- [PR #1521の追加レビュー](https://github.com/harupipipipi/tobkiri/pull/1521#pullrequestreview-5980274898)
  の指摘を `988a0a785` で修正した。同じProfileの定義更新が途中で届いた場合も保存側は
  操作ロックを解放する。定義更新がすでに一覧を読み直した場合は、保存完了による再取得を省く。
  回帰試験は修正前に失敗し、修正後は次の保存に新しいリビジョンと世代が使われることを確認した。
  既存を含む23件とLauncherフロントエンドの型検査が通過した。

## 058 既存Profileに拡張Packを追加する際の権限上限

- `56fb9d45c` は `tobkiri profile add` と `profile update` に `--limits` を追加した。
  ProfileのPack一覧に加えるとき、対象Packの権限上限も同じ保存に含める。追加先のProfileと
  検証済みPackの両方が許す範囲を使い、追加の権限は元の承認経路で確認する。上限を指定しない
  更新は、保存済みの上限を引き継ぐ。
- 以前はPackのpinだけを追加したため、実行に必要な権限を指定するにはProfile定義を別工程で
  書き換える必要があった。CLIでのPack生成とインストールの手順を `packs/README.md` に記載した。
  既存CLIのProfile作成・実行・公開・拒否・再取込みとユーザーOverlayの試験が通過した。
- 実CLIで外部Flow Overlayを生成・検証し、新しいEcho Profileと同梱Harnessから作った
  Profileに追加した。テスト用の定義に、対象の既存ロールだけを明示的に許可した。両方で、
  承認後の会話にOverlayが反映され、元のユーザー入力と変更後の返信が保存された。
- 権限上限の超過、承認の拒否、Overlayの競合、必須Packの削除を試し、承認済みの状態を
  維持することを確認した。更新では追加した監査権限だけを承認し、公開後の再取込みでは
  全Packに新規承認を要求した。会話履歴のない状態から同じOverlayで実行できた。
  [実CLI・WasmのCI](https://github.com/harupipipipi/tobkiri/actions/runs/44615300106) も
  `154d71fda` で通過した。

## 059 新規Wasmツールの型付きポートを既存Profileで使う

- `433c3c480` は `profile add --allow-provided-port` を追加した。新しいPack自身が定義し
  提供する正確なバージョンのポートを検証し、Packのpinと一緒にProfileの上限へ追加する。
  先に上限へ未定義のポートを書くと、元のProfileコンパイラが拒否する。同じ保存で扱うことで
  該当する権限を独立して審査できる。Packを外すとそのPackが定義する上限の記載も外し、
  残った呼び出し元と必須ポートは従来どおりコンパイルで検査する。
- 新規RustゲストをWASI2にビルドし、CLIでWITとバイナリからPackを生成・検証・追加した。
  Echoと同梱Harness由来のテスト用Profileで、既存のFlowは宣言なしのツール呼び出しを拒否した。
  Flow本体を保ったPackの複製で、新しいポートの追加だけを通常の更新で承認した。
  その後はツール本体の監査権限を指定しない呼び出しが拒否され、承認すると型付き応答が
  元のプロンプト構築に渡り、会話の応答として保存された。両方の拒否と実行主体を監査で照合した。
- 呼び出しが残るツールの削除を拒否し、利用側を外して元のFlowに戻すとツールを削除できた。
  元のProfileの公開定義と一致し、既存の会話を実行できた。この試験はCLI経由のFlowで、
  LauncherのGUI接続は引き続き確認する。

## 060 DMGからインストールしたLauncherの初回Setupを確認する

- native Setup付きのCI用DMGは、マウントしたSetup・Registryリソースと署名を確認した後、
  アプリを一時Applicationsフォルダにコピーする。DMGを取り外し、コピーの署名を検査してから、
  新規のCI用データディレクトリでアプリを起動する。既存の起動検査はこの段階で行う。
- Launcher配下のKernelの認証済みhealthとパネル認証に続き、ProfileがSetup待ちの状態で、
  認証済みの `/panel/setup` が既存のパネルを返すことを照合する。マウントからのコピーと
  取り外しの順序、既に有効なProfileや認証用ページをSetup成功としない挙動をローカルで検証した。
  同じDMGでのGUI操作、設定、会話までの証跡は引き続き必要。
- [先行のステージングCI](https://github.com/harupipipipi/tobkiri/actions/runs/44611033606) は
  署名済みWasmを使ったLauncherのテストで、Side Chatのフィルターが名前の異なる2件目を
  選ばず、成功1件と期待2件の不一致で停止した。現在は2件の完全名を指定し、実行件数も照合する。
- インストールしたコピーの検証は、Launcherの正常終了とKernel・Broker両ポートの解放も
  待つ。Kernelは別のプロセスグループで動くため、アプリの終了処理が完了しなかった場合は
  失敗を返し、実行に使ったコピーとデータを保持する。引き継いだ終了予算内で待ち、実行が
  確認できた場合は一時ファイルを整理する。
- 配布前の手動検証では、同じDMG検証に `--wait-for-user-setup-seconds` を指定できる。
  同梱Wasm・Registryの固定値とコピーの署名の確認後、分離したデータで通常のLauncherを開く。
  利用者が既存GUIでSetupを確認する間、検証器は認証済みの状態を読む。Kernelの再起動、
  同じLauncherの子プロセス、所有者専用のHost契約のProfile・plan・activationとの一致と、
  実行時の準備完了を観測してから終了を確認する。
- `--wait-for-user-chat SECONDS PROVIDER_ID MODEL_ID` を併用すると、Setup後も同じLauncherを
  起動した状態で、利用者がGUIから送る検証文を待つ。認証済みの読み取りで、接続と保存モデルの
  組み合わせ、検証文に対応する完了済みターン、ユーザー文と応答の保存IDを照合する。Kernelの
  所有者やactivationが変わると失敗する。検証器は承認や設定保存を代行せず、会話内容、鍵、認証情報を
  記録しない。失敗時の診断からアプリの出力も除外する。LauncherとDMG検証のテスト455件が通過した。
- 実際のDMGでは、専用承認ウィンドウの表示と操作、接続先URL、llama.cppサーバーでの実推論を
  同じ実行で確認する。GUIのローカルOpenAI互換接続は鍵なしのHTTPループバックを使える。
