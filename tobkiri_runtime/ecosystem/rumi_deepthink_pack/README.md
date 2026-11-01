# DeepThink Strategy Pack

DeepThink は、応答を計画・下書き・レビューする、回数と予算に上限を設けた AI 戦略 Pack です。Tobkiri に Pack としてインストールして有効化すると、モデル選択に **DeepThink** が追加され、`/deepthink`（`/dt`）でも選べます。

この Pack は `tobkiri.service.ai.strategy.execute.v1` の provider
`rumi_deepthink_pack.deepthink.execute` を提供します。実際のモデル呼び出しは含みません。各段階で、Host が解決した次の汎用契約だけを PackVM capability bridge 経由で使います。

- `tobkiri.resource.ai.route.quote.v1` で非秘密の経路と価格を確認します。
- `tobkiri.service.ai.generate.v1` で、確認済みの経路に固定して生成します。

## 導入と有効化

この Pack は通常の sandbox Pack です。署名済み Pack をインストールし、プロファイルで有効にしてください。プロファイルには、AI strategy runtime Pack と、quote/generate 契約を提供する Gateway Pack も含める必要があります。必要な Profile edge は契約と Pack dependency から導出されるため、Host や Gateway が DeepThink 固有の実装を登録する必要はありません。

UI contribution は `frontend/contributions/ai-strategy.json` にある宣言型 descriptor です。署名済み catalog と一致した場合だけ、選択肢とコマンドとして表示されます。

## 上限と安全性

- 最大 5 回の生成と、quote を含む最大 10 bridge hop です。
- 予算は外側の `maximum_cost_microusd`（整数 micro-USD）で固定されます。Host は Pack の継続状態を信用せず、quote と生成結果から別に予算を検証します。
- deadline は Unix epoch millisecond の正整数です。
- 価格と使用コストは canonical decimal string として bridge を渡ります。quote の単位は `usd_per_token` であり、Pack は rate×token 数を micro-USD に切り上げて予約します。浮動小数は拒否されます。
- route binding を各生成に渡し、failover を無効にして、見積もった経路以外へ送らないようにします。
- API key、credential、秘密情報、ネットワーク権限は受け取らず、所有しません。
- 画像は Host が所有する URL や resource reference をそのまま渡せます。base64 などの inline media は継続状態に入れません。

そのため、新しい strategy Pack も同じ契約、catalog、bridge の仕組みで導入できます。DeepThink のためだけの Host/Gateway 分岐はありません。

## Regression and live acceptance boundary

Tool-bearing draft/revision phases add `requirements.tool_calling: true` to both
quote and generate requests. A route without provider-native tool calling must
therefore be unavailable before it sees host tool definitions. Other phases
retain the caller's requirements. The ordinary provider compiler is unchanged.

Reviewer output must contain exactly a boolean `approved` and string `feedback`.
Malformed review, empty repair, and a rejected final review fail closed without
publishing an unverified answer. A cancelled Host bridge returns `CANCELLED`
and produces no further bridge request.

For a live test, the Host must admit the signed contribution
`rumi_deepthink_pack.deepthink.execute` in the active Profile. Select that exact
strategy through the normal saved-turn composer and use the current conversation
model. Do not send a legacy virtual model or a `deepthink` provider flag.

The strategy's generic execute input is:

```json
{
  "request_id": "acceptance-request",
  "model_reference": {"profile_id": "host-selected-model-profile"},
  "messages": [{"role": "user", "content": "What is 17 times 19?"}],
  "tools": [],
  "requirements": {},
  "profile_id": "host-selected-runtime-profile",
  "deadline": 1800000000000,
  "idempotency_key": "acceptance-request",
  "maximum_cost_microusd": 50000
}
```

Replace the identifiers with admitted selections and set deadline to the current
Unix millisecond time plus a bounded acceptance window. Keys/credential handles
are never part of this payload. The Host resolves quote/generate providers,
seals every continuation, binds each actual generation to its quote, and owns
cancellation. Require actual phase/cost evidence and an authoritative completed
turn result; model/provider replies alone are not installed Pack acceptance.

A no-network deterministic fixture can be run from `tobkiri_runtime` with
`python -B -m pytest --noconftest tests/test_deepthink_strategy_pack.py -q`.
This isolates the strategy state machine and does not verify installation,
signed Profile activation, PackVM supervision, live model behavior, or UI.

## Direct HTTP source acceptance

A bounded real OpenRouter source probe passed with the actual strategy
controller and normal provider compiler. See
[the sanitized acceptance report](docs/direct-http-source-acceptance.md)
for phase outputs, cost, tokens, and cancellation evidence. This result does
not establish installed/admitted PackVM or Launcher GUI acceptance.
