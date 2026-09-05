このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR: 未作成（push/PR許可後に記入）
- Wave: 8
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA: PR作成時に記入
- Related issue: #1156

## 実装内容

- 移動するownership: workspace、file、shell、terminal、Git、patch、IDE、
  coding sandbox、browser、desktop、clipboard、capture、media inspection
- 旧authoritative owner: defaultspack／default toolsの直接host実装
- 新authoritative owner: Wave 8 read/write・observe/control・inspect/execute packs
- 追加するglobal contract: workspace/file/shell/terminal/Git/IDE/sandbox/
  browser/desktop/clipboard/media contracts
- 削除する旧経路: defaultspack clipboard subprocess、screenshot stub、
  canonical browser/computer function entrypointの直接driver呼出し
- compatibility／migration: finite legacy action map、top-level HostIntent、
  atomic profile cutover、no dual-write/no host downgrade rollback

## 必須実環境

- OS: macOS、Windows、Linux（各対応境界）
- Profile: default-profile、新規profile、各service pack除外profile
- Bundle: local source bundle
- Surface: browser UI、CLI、tool invocation、Viewer host broker
- Migration fixture: workspace、Git repo、browser profile/cookies、terminal/IDE
  session、clipboard、screen/audio/camera fixtures
- Packあり／なしの両構成: ありは正常、provider欠落はfail-closed

## 必須確認

- clean startup
- effective pack setと選択providerが一意
- contract resolutionとpermission projection
- file inspect/write/delete/move/patchのworkspace jailとstale hash
- shell inspect/execute、timeout、cancel、process-group cleanup
- terminal/IDE session observe/control、restart、shutdown
- Git read/write/publish分離、remote URL再確認、force-with-lease
- sandbox COW、secret/symlink除外、digest pin、network none、host downgrade拒否
- browser session/profile/navigation/cookies/capture/download
- desktop window/observe/capture/input/accessibility control
- clipboard read/writeと1 MiB上限
- screen/microphone/system-audio/camera capture、5分上限、cancel
- document/image/audio/recording inspectとunsupported decoder
- vision/transcription adapterのproviderあり／なし
- legacy tool ID migration、pack削除時surface消失
- migration、restart persistence、rollback、clean shutdown

## Security／integrity

- Authority境界
- approval token binding
- service receipt operation/caller/args/profile/workspace/session/expiry/replay binding
- client `approved`／token／`yolo_mode`が信用されないこと
- secret scope
- workspace bindingとsymlink escape拒否
- stale revision／stale hash／remote URL TOCTOU
- direct cross-pack private accessがないこと
- dual-write／first-found fallback／host downgradeがないこと
- raw clipboard/media payloadがaudit/storageへ残らないこと

## 必須証拠

- 実行コマンド
- OS／環境
- selected profile
- effective pack set
- selected contract providers
- redacted authority／receipt／host-broker logs
- screenshots（秘密・clipboard・media payloadは除外）
- startup result
- shutdown result
- migration result
- rollback result
- packあり／なしのroute/tool/UI差分

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
