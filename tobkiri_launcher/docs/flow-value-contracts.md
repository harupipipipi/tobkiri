# Flow の値とノード設定

Flow は API フィールドをすべて線で接続する画面ではありません。ノードはデータを受け取り、処理したデータを返す部品です。

- TTS: text → sound
- STT: sound → text
- テキスト生成: text → text
- モデル、声、速度などはノードの設定です

内部の JSON は転送形式として引き続き使います。入力の `input`、出力の `content` など既存の Contract パスを表示名だけで書き換えません。既存 Profile の権限、承認、Broker、契約 revision を維持します。

## Pack 共通の Sound v1

`x-tobkiri-value-type: tobkiri.value.sound.v1` は、音声値を一つの端子として宣言します。
Python の Pack は `tobkiri_protocol.flow_values.sound_schema()` が返す独立したスキーマを自分の公開 Contract に埋め込めます。JSON を使う Pack も同じスキーマを埋め込めます。他の Pack の実装名や内部ストレージを知る必要はありません。

Sound v1 の値には次だけを含めます。

- content_id: 音声バイト列の SHA-256
- media_type: 対応した audio MIME
- data_base64: 音声の標準 base64
- byte_size: 復号後の長さ
- filename: 任意の表示用ファイル名

URL、ローカルパス、API キー、権限は含みません。上限は復号後1 MiBです。大きな音声とストリームは別のバージョン付き Artifact/Stream 契約を必要とし、現在の Sound v1 のふりをさせません。

JSON の object 同士という理由だけで画像や任意の辞書を Sound に接続することはできません。コンパイル時に、capture 済みスキーマの正確な型識別子を確認します。実行時は解決した値の MIME・長さ・base64・内容ハッシュを再検証してから次のノードを呼びます。この型識別子は権限を付与しません。

## 設定とデータの宣言

Contract のプロパティに `x-tobkiri-flow-role` を付けます。

- data: 通常のデータ端子
- configuration: ノードの設定。通常は端子として表示しない
- metadata: 状態や Provider ID など。通常は端子として表示しない

指定のない既存 Contract は従来通り data として扱います。詳細表示では設定やメタデータへの接続も確認できます。既存の接続線は通常表示でも隠しません。型付きの値自体は、base64などの内部フィールドに展開せず一つの端子として扱います。

独自型はバージョン付きの名前と値スキーマで宣言できます。Workflow は特定の Pack 名を判定して配線を許可しません。任意の型名を名乗るだけでバイト列の安全性や権限が証明されるわけではなく、消費側の完全なスキーマと検証が必要です。

## 再生と生成は別の動作

TTS は生成した Sound を返し、自動でスピーカーを鳴らしません。現在は結果の音声プレイヤーから明示操作で再生できます。実機のスピーカー制御を独立した実行ノードとして登録する処理は、この型整理とは別の実装範囲です。型が同じでも、実機への作用やネットワーク権限を勝手に追加してはいけません。

## 汎用の値接続

音声はこの仕組みを検証する例にすぎません。文字列・整数・真偽値・配列・オブジェクト、各Packが宣言した独自の型にも同じ配線規則を使います。Contractの転送エンベロープはオブジェクトのままです。文字列などの値はその中の型付きフィールドを端子として公開します。

具体的なフィールドを持たない、意図的に汎用なobject契約では、オブジェクト全体を一つの端子として扱います。`${steps.node.output}` は同じRunの正常終了済み出力全体を参照します。従来の `${steps.node.output.field}` も維持します。宣言したIDの集合に対して参照が曖昧なら拒否します。新しい全体接続はpaletteの `whole_value_reference_format` による明示的な対応宣言を必要とします。

汎用objectに対して具体的なフィールドの型を推測してはいけません。backendの入力・出力定義が未整備の箇所は、具体的な型付きノードへの移行対象として残ります。object全体を接続できることは、すべての既存操作の意味や型が十分に定義されたという証明ではありません。

## Operationごとの契約

canonical Pack sourceのprovided Contractにある各Operationは、任意の `schemas` オブジェクトで `input` / `output` / `error` を個別に宣言できます。省略した項目は既存のContract定義を継承します。生成したschemaのdigestとContract revisionは通常どおり固定され、effect ceilingを増やすことはできません。同じ共有Contractを提供する複数Packの整合性検査も維持します。

JSONのキーがcamelCase、ドット・スラッシュを含む名前、日本語などの場合は `${steps.node.output@/a.b}` のJSON Pointer形式を使用します。ドットを含む1つのキーと、複数階層のキーを混同しません。`~1`はスラッシュ、`~0`はチルダを表します。コード評価、外部スキーマのネットワーク取得、別Runの出力参照は行いません。Hostが `json_pointer_reference_format` を宣言した場合にのみ、新しい編集画面はこの形式を生成します。


## Tool display hints and nominal values

Tool argument schemas may use the bounded display-only annotations
`x-tobkiri-flow-role` (`data`, `configuration`, `metadata`) and
`x-tobkiri-selector` (`model-profile`, `tool-definition` on string identifiers).
These never relax JSON validation or grant a capability. A compound tool picker
binds the top-level `tool_id` and `expected_definition_hash`; nested selector
hints do not replace that outer definition and may fall back to ordinary fields.

Nominal types in dynamic tool definitions still require authoritative node-instance
schema capture, not just UI projections. In particular, the captured Broker
operation has generic `arguments` and `result` slots. A Sound-shaped tool widget
is not enough to make those slots authoritative Sound connections. Keep the
nominal mismatch checks and unsupported tool-value annotations fail-closed until
selected-definition schemas are captured with their hash and run compilation.
Static Pack operation contracts already provide the authoritative schema path.
