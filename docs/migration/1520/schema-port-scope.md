# Private schema engine の移植境界

2026-10-31 更新。source の範囲と検証の要求対応を記録する。
末尾の限定された CI 結果を除き、実行・互換性・性能の証拠ではない。
候補 library は実呼び出し時の trap と参照の構造的な問題で不採用。
以下は着手した直接移植の対象範囲で、source 実装と実検証の完了は別に扱う。
製品 validator は置換していない。

## 固定された原本

[fetch_schema.py](../../../tools/python313-producer/fetch_schema.py) は
jsonschema 4.26.0、jsonschema-specifications 2025.9.1、referencing 0.37.0 を固定する。
以下は repo と各公式 tag の source に基づく。実 handler ごとの coverage は未観測。

## Draft 2020-12 の全登録 handler

36 個すべてが `jsonschema._keywords` にある。別名以外は keyword と関数名が同じ。
これは実装済み一覧でも、対象を一部に限定する許可でもない。

| 群 | keyword → 関数 |
| --- | --- |
| 参照 | `$ref` → `ref`, `$dynamicRef` → `dynamicRef` |
| object の子 | `properties`, `patternProperties`, `propertyNames`, `additionalProperties` |
| object 依存 | `required`, `dependentRequired`, `dependentSchemas` |
| object 件数 | `minProperties`, `maxProperties` |
| array 位置 | `prefixItems`, `items` |
| array 制約 | `contains`, `uniqueItems`, `minItems`, `maxItems` |
| 数値 | `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf` |
| 文字列 | `minLength`, `maxLength`, `pattern`, `format` |
| 値・型 | `const`, `enum`, `type` |
| 組合せ | `allOf`, `anyOf`, `oneOf`, `not` → `not_` |
| 条件 | `if` → `if_` |
| 評価済みの要素 | `unevaluatedItems`, `unevaluatedProperties` |

`then` / `else` は `if_`、`minContains` / `maxContains` は `contains` が処理する。
ID・anchor・schema declaration は登録 handler 以外にも参照解決へ影響する。
[登録](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/validators.py)、
[handler](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/_keywords.py)。

## 実 entry と lifetime

[validation.py](../../../tobkiri_runtime/tobkiri_protocol/validation.py) の動作を維持する。

- `load_schema`: 原本 read → `strict_loads` → object 検査 → `check_schema`。
  LRU の key は元の引数なので alias と filename を勝手に統合しない。
- `_schema_store`: sorted filename 順に load し、SchemaValidationError は skip、string `$id`
  の schema を保持する。一つの cached store で、validation 結果の cache ではない。
- `validate_document`: strict parse または mapping deepcopy / canonical 検査 → version 選択 →
  schema 取得 → fresh `RefResolver.from_schema(..., store=...)` と generic `FormatChecker()` →
  `iter_errors` → rendered absolute path と message による stable sort → semantic diagnostics。

## 走査・解決・error の同等性

- `create → __attrs_post_init__ → iter_errors → handler → descend/evolve` の順序・schema の
  insertion order・lazy な error 発生を維持する。`check_schema` は最初の yielded error を
  `SchemaError.create_from` で raise する。
- `check_schema` は class `FORMAT_CHECKER` と referencing registry を使う。
  document validation の explicit legacy RefResolver だけを移植して終わりにしない。
  evaluated-member helper は同時に `_resolver.lookup` も使い、ref・条件・applicator の
  scope と評価済み要素に影響する。
- 既存 RefResolver の `resolve → push_scope → list(descend) → pop_scope`、既定 cache・
  resource / base URI / dynamic anchor・実 referenced object を保持する。
  元 adapter にない offline policy を黙って追加しない。外部 IO の権限は別途 Core で維持する。
- 実 `ValidationError` / `SchemaError`、`instance` / `schema` / `validator_value` の参照、
  unset と None の差、context の順序・同じ child と parent、実 cause、path deque と
  derived absolute path を維持する。`_set` は unset のみ補完し、`create_from` は参照を
  保ち child を reparent する。文字列表示は Python の repr / dictionary order を使う。
- bool と number の区別、integral float の integer 判定、bigint、Python regex を維持する。
  Rust / serde への変換で近似しない。

[validator/resolver](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/validators.py)、
[utilities](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/_utils.py)、
[exceptions](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/exceptions.py)、
[types](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/_types.py)、
[referencing](https://github.com/python-jsonschema/referencing/blob/v0.37.0/referencing/_core.py)。

## FormatChecker は実環境でも確認する

generic `FormatChecker()` と Draft202012 の class checker は同じ集合とは限らない。
optional RFC3339 module がない場合の draft-3 `time` fallback などが異なる。
未知の format は pass し、既知の format 失敗は元の cause を保持する。
選択済み closure には optional format distribution がないが、実 guest の checker 登録集合と
handler coverage はまだ観測していない。source から推測した個数を実測値として扱わない。
[format source](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/_format.py)。

[整理メモ 004](cleanup-notes.md#004-スキーマ処理の-rust-化後の実装依存整理) と併せて更新する。

## 元要件と移植時に追加した仮説の区別

必須範囲は issue R1–R11、ユーザーが合意した完全な GUI / Setup / 設定 / 会話、
原本の **839 migration units**、出荷 caller と公開 SDK の契約から決める。
839 はテスト件数ではない。移植中に追加した API 群だけで元の全機能を定義し直さない。
追加した private 反射の検査を、自動的に新しいユーザー要件に昇格させない。

具体的な出荷 caller の根拠は次のとおり。

- [workflow integration](../../../tobkiri_runtime/core_runtime/workflow_v4/integration.py):
  `validators.extend` の 4 引数 generator callback、実 ValidationError、captured
  Registry / Resource による解決と存在しない参照の拒否。
- [protocol validation](../../../tobkiri_runtime/tobkiri_protocol/validation.py):
  local-store RefResolver、標準 FormatChecker、iter_errors の path / message。
- [Pack SDK](../../../tobkiri_runtime/core_runtime/pack_sdk.py): 標準 format 検査と診断の整列。
- [frontend caller tests](../../../tobkiri_runtime/tests/test_frontend_host.py): caller の
  validator 依存がない場合の拒否。private function の code 差替えを保証する検査ではない。

[公式の拡張契約](https://python-jsonschema.readthedocs.io/en/stable/creating/) は keyword
callable の 4 引数、既存 callable への委譲、type / format checker の接続を説明する。
[Validator protocol](https://python-jsonschema.readthedocs.io/en/stable/validate/) は実検証 API
と属性を定義する。この契約から Python function 固有の内部構造すべてを保存すべきとは
直ちに推論しない。一方、文書に記載がないことだけで既存 caller の動作を除外もしない。

### 現在の 18 API 群の要求対応（暫定）

「回帰」は既存の挙動を守る追加検査。「仮説」は private 実装への追加互換性観測。
混在する群は、将来判断する場合も部分ごとに扱う。**現在の gate / 18 群は変更しない。**

| 群 | 主な根拠・分類 |
| --- | --- |
| lazy_start_and_resume | 公開 iter_errors / custom keyword の遅延検証。send の細部は追加観測 |
| actual_error_objects_and_references | 元 protocol / SDK caller の実 error、path / message、参照・原因の回帰 |
| outer_close_keeps_retained_callback_open | 元走査の終了と retained callback に関する lifecycle 回帰 |
| interleaved_iterators | 同じ実 validator を使う独立した検証の回帰 |
| pep479_and_failed_iterator_closed | custom callback の例外を成功にしない、generator 終了の回帰 |
| false_descend_preserves_original_early_paths | boolean schema と子 error path の回帰 |
| evolve_preserves_actual_class_and_schema | 公開 evolve / draft 選択と実 schema の回帰 |
| unregistered_extension_class_not_cached | 拡張 class の不要な保持を防ぐ memory / lifetime 回帰 |
| keyword_delegation_closes_retained_child | delegated child の終了回帰 |
| keyword_deallocation_closes_retained_child | deallocation 時の終了回帰 |
| keyword_cycle_collection_closes_retained_child | cycle / GC 時の終了回帰 |
| unstarted_keyword_does_not_acquire_child | 開始前に callback の副作用を起こさない回帰 |
| default_type_dispatch_matches_original | JSON 型 / 数値 / checker の契約。co_consts identity 等は追加実装仮説 |
| evolve_order_live_aliases_and_name_resolution | evolve の実 schema / checker / resolver。private namespace 改変は追加仮説 |
| post_init_order_live_callbacks_and_state | 実 constructor / registry / callback / error の回帰。内部の詳細観測は別途対応が必要 |
| urljoin_actual_helpers_callbacks_and_registry | 実 URI / registry 解決。private helper / cache 改変等は追加仮説 |
| descend_keyword_binding_and_generator_protocol | callback の引数 / error / lazy 境界。private descriptor / code 観測も含む |
| live_keyword_and_checker_code_changes | private 同一 function の code 改変は追加仮説。偽 metadata による FFI 越境拒否は安全性上必須 |

current product/runtime/tests、SDK、docs を producer と移植用 probe から分けて検索した範囲で、
jsonschema private function の `__code__` 改変・function 型判別・`__globals__` 参照を要求する
出荷 caller は見つからなかった。これは別途固定した全歴史 corpus の証明ではなく、
検索で見つからないことだけで正式な対象外にはしない。supported custom validators / type
checkers / format callbacks / resolver の通常 dispatch を省略する根拠にもならない。

Astra high に境界を再確認し、任意の private 反射互換を一律必須にする前提は強すぎたとの
回答を得た。今後 native callable を公開する設計を検討する場合は、上の根拠と元 unit / SDK
の完全な対応を先に確認する。予算に失敗した後にテストを黙って落とす手順は取らない。

## 2026-10-31 の限定された実測

source `1cbd05f75` の通常 CI `41117640666` / job `124275891668` は実 compile、
全 64 比較と API18 に成功。元 source / 36 signature の閉じた照合にも成功し、API 前の
公開定義経由の実 Rust entry は 59,245 回。一括 Wasm は元 2B fuel で停止し deadline は保持。

別の累積診断 `41117649051` / job `124275924360` は同じ contract_v4 まで
1,182,899,525 fuel、直前版の 1,242,952,199 から約 4.8% 減。9 件完了のまま、次の
executable_catalog_v4 で停止した。これは同じ診断 protocol 内の実測で、一括合格、
上限推定、全 catalog / 製品の合格ではない。今回 API18 は合格しており、上の暫定分類は
失敗した検査を除外するための判断ではない。
