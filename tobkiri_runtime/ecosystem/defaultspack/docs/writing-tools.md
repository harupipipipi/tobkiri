# Writing Tools

defaults Pack のツール定義と、旧動的 Python ツールからの移行ガイドです。ツールの登録・実行は `domain/tool/registry.py`、`domain/tool/executor.py`、`domain/tool/security.py` を参照してください。

## ツールの authoring

tool はエージェントが呼び出す機能の単位で、名前、説明、JSON Schema 形式のパラメータ、実行先を定義します。新しいツールは reviewed Pack、MCP server、connector を使って提供します。Pack の authoring と activation は [Pack development guide](../../../docs/pack-development-guide.md) を参照してください。

実行先の主な authorable type は次の通りです。定義が通ること自体は実行権限の付与を意味せず、Pack の review、必要な権限、通常の approval path が適用されます。

| `execution.type` | 必須の実行先フィールド |
|---|---|
| `rumi_function` | `qualified_name` |
| `capability` | `permission_id` |
| `mcp` | `server_name` |
| `global_contract` | `contract_id`, `operation` |

`local`、`handler`、`dynamic` は trusted first-party 向けの legacy compatibility path です。ユーザーが任意の Python handler を authoring する経路ではありません。`prompt` tool の authoring もサポートされません。プロンプトから tool が必要な場合は [Writing Prompts](writing-prompts.md) を参照してください。

例えば `rumi_function` の実行先は次の形で指定します。`qualified_name` は実際に提供される reviewed function に置き換え、Pack の tool manifest 全体と必要な権限を定義してください。

```json
{
  "type": "rumi_function",
  "qualified_name": "replace.with.reviewed.function"
}
```

## 旧動的 Python ツールの移行

旧 `POST /api/tools/create` と `PUT /api/tools/{name}` の handler は `MIGRATION_REQUIRED` を返します。`handler_code` の指定や AI 生成による新規作成・更新はできません。旧 `user_data/shared/tools/` の定義は互換処理で読み込まれる場合も無効かつ移行必要として扱われ、Python source は実行・export されません。reviewed Pack、MCP server、connector に移行してください。

旧 dynamic 定義の削除 handler は、対象が registry に存在する場合、通常の承認を経て削除します。export は利用可能な定義の metadata を返し、旧 `handler_code` を含みません。

## ベストプラクティス

`name` と説明は機能を明確に表し、パラメータの JSON Schema には型と必須項目を記述してください。実行先は必要な権限を明示し、review と approval の仕組みを通してください。
