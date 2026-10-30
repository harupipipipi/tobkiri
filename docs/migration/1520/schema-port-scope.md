# Private schema engine の移植境界

2026-10-30 の source inventory。実行・互換性・性能の証拠ではない。
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
