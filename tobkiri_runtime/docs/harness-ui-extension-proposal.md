# Harness UI 拡張の分割案

状態: 設計提案。既存の公開プロトコルや実装済み機能を表すものではない。
基準: PR1322 `273aed66b50190b640c6b93e03cea9519b21b7f1`。

## 目的

別々の作者が Settings、Chat、Input を開発し、相手の実装を読まずに組み合わせる。
独自フロントも同じ公開接続仕様を実装すれば Input を再利用できるようにする。
画面の見た目を整える作業と、状態・権限の分離は段階的に進める。

## 現在確認できること

- Defaults Profile の解決済み構成は 37 Packs。多くの backend owner は既に分離されている
- defaultspack の canonical Functions は presentation、conversation、saved conversation の 3 個
- App と Composer の画面・状態接続は大きい。React component であるだけでは独立 Pack ではない
- canonical な外部 frontend contribution は inert declarative route に限定される
- isolated iframe の frontend コードは存在するが、canonical admission で利用可能という証拠にはならない
- JSON の入力フィールドを描画する既存コードはあるが、structured input 等を含む saved-turn 送信は明示的に停止する
- 入力欄には API キー保存、workspace trust、tool review approval の callback も接続されている

## 推奨する所有者

| 要素 | UI の担当 | 状態・実行の担当 |
| --- | --- | --- |
| Harness Shell | 配置、navigation、選択された UI の mount/unmount | 解決済み Profile と有効な接続の管理 |
| Settings UI | 項目、検索、編集、保存結果の表示 | 各設定を所有する既存 backend |
| Chat UI | message、stream、tool activity、履歴の描画 | conversation store と turn executor |
| Input UI | draft、添付の表示、mention、送信操作 | Draft owner、resource broker、共通 submit |
| 承認・秘密情報の画面 | 信頼された Harness 共通画面 | approval/credential owner |

UI ごとに conversation store や executor を複製しない。
UI を閉じることと実行のキャンセルを別操作にする。
Input UI の交換で送信済みの処理が失われないことを必須にする。

## Input の公開接続仕様に必要なもの

1. 接続時に host が対応 version と機能を通知する
2. host が現在の conversation と view generation に束縛した接続を渡す
3. draft の読取・更新には revision を使う。古い更新は競合として扱う
4. 添付は broker 発行の resource reference を使う。任意のローカルパスを渡さない
5. tool/model の選択候補は公開 projection から取得する
6. submit は draft revision と request ID を受け取る
7. 同じ request ID の再送は同じ処理を参照し、異なる内容なら拒否する
8. 受付済み receipt と処理完了の receipt を区別する
9. UI 再接続時は snapshot と cursor から表示を復元する
10. 認証情報の設定や強い承認は、目的を指定して共通画面を開く

React の FormEvent、setState、App 全体の object、関数 callback は公開 wire 仕様に含めない。
秘密情報を含む汎用 settings object も Input UI に渡さない。
UI が指定した profile/pack ID を権限の根拠にしてはいけない。

## JSON 追加とコード拡張

JSON は text/textarea/select 等の安全な標準部品に使う。
各 field は所有 Pack と名前で識別し、schema version とサイズ上限を持つ。
値の意味を backend が明示的に受け入れるまでは送信可能と表示しない。
未知 field を黙って捨てたり、全て prompt の文字列へ連結して済ませたりしない。

JSON で表せない入力体験には、分離されたコード UI を使える設計にする。
ただしその受け入れ経路、資産の完全性、通信相手の識別、権限、失効を先に実装する。
ページ全体の DOM や同一 origin の module を任意 Pack に公開する方法は採用しない。

## 組み合わせ・競合のルール

- 画面 slot は公開された名前と version で指定する
- 一意の slot に複数候補がある場合、Profile の明示選択で決める
- インストール順、配列順、最後に読み込まれたもの勝ちにしない
- 複数表示可能な slot は決定的な順序と上限を定義する
- route/ID の衝突は診断を出す。片方の private module を参照して解決しない
- optional な UI が失敗しても他の画面と進行中の turn を停止させない

## 実装順

### 1. 公開接続と状態を先に定義する

現在の App の send flow と view fence を挙動の参考にする。
Draft、selection、submission、event replay を別の責務として境界を定める。
既存の承認・署名・revision の検証を緩めて UI を動かすことはしない。

### 2. Settings の一画面を外部化する

秘密情報の読取を必要としない小さな設定ページを選ぶ。
標準 JSON form と独自 renderer の両方が同じ設定契約に接続できるか確認する。
設定権限がない場合の表示・保存失敗・revision 競合を試す。

### 3. Chat と Input を別作者に渡す

両者には公開仕様と contract test だけを渡す。
第三の作者に異なる Input UI を作ってもらい、同じ Chat で交換する。
相手のソースを読まないと接続できない場合は SDK/仕様の欠陥として直す。

### 4. 実機で故障時も確認する

- 入力途中の UI 交換、会話 A→B→A、送信二重クリック
- 受付後の応答消失と同一 request の再確認
- stream の重複、順番入れ替わり、欠落と再同期
- UI 削除・権限失効・Profile 更新中の呼出し
- 添付を別会話に流用、設定 UI から秘密情報を要求
- 2 つの独立 Pack の route/slot/ID 衝突
- desktop と mobile で同じ draft/turn を操作

既存の設計モデルでは 13 ケースを試したが、これは本番 Host/PackVM の証拠ではない。
上記を実際にインストールした Pack と native frontend で通してから拡張契約を安定版とする。

## 今すぐ分離しないもの

- 各小ボタンを別 Pack にする: API と寿命管理の負担が先に増える
- turn 実行を Chat UI に所属させる: 画面切替が実行継続を壊す
- credential store を汎用 Settings UI と同居させる: UI 交換の権限が過大になる
- Input UI に provider 固有の HTTP 実装を含める: 別フロントへの再利用を妨げる

まず機能の所有者単位で分け、表示上の部品単位の細分化は必要が出てから行う。
