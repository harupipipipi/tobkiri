# tobkirithink Strategy Pack

tobkirithink は、応答を計画・下書き・レビューする、回数と予算に上限を設けた AI 戦略 Pack です。Tobkiri に Pack としてインストールして有効化すると、UI contribution の表示名は **tobkirithink**、コマンド宣言は `/tobkirithink` です。既存の `/deepthink` と `/dt` は互換 alias として宣言を維持します。実際の表示・選択・コマンド実行には、Host の admission と対応する UI が必要です。

表示名は lowercase の `tobkirithink` です。既存のインストール、Profile、保存済み strategy reference を維持するため、Pack ID `rumi_deepthink_pack` と契約・operation ID は変更しません。

この Pack は `tobkiri.service.ai.strategy.execute.v1` の provider
`rumi_deepthink_pack.deepthink.execute` を提供します。実際のモデル呼び出しは含みません。各段階で、Host が解決した次の汎用契約だけを PackVM capability bridge 経由で使います。

- `tobkiri.resource.ai.route.quote.v1` で非秘密の経路と価格を確認します。
- `tobkiri.service.ai.generate.v1` で、確認済みの経路に固定して生成します。

## 導入と有効化

この Pack は通常の sandbox Pack です。署名済み Pack をインストールし、プロファイルで有効にしてください。プロファイルには、AI strategy runtime Pack と、quote/generate 契約を提供する Gateway Pack も含める必要があります。必要な Profile edge は契約と Pack dependency から導出されるため、Host や Gateway が tobkirithink 固有の実装を登録する必要はありません。

UI contribution は `frontend/contributions/ai-strategy.json` にある宣言型 descriptor です。署名済み catalog と一致した場合だけ、選択肢とコマンドとして表示されます。

## 上限と安全性

- 最大 5 回の生成と、quote を含む最大 10 bridge hop です。
- 予算は外側の `maximum_cost_microusd`（整数 micro-USD）で固定されます。Host は Pack の継続状態を信用せず、quote と生成結果から別に予算を検証します。
- deadline は Unix epoch millisecond の正整数です。
- 価格と使用コストは canonical decimal string として bridge を渡ります。quote の単位は `usd_per_token` であり、Pack は rate×token 数を micro-USD に切り上げて予約します。浮動小数は拒否されます。
- route binding を各生成に渡し、failover を無効にして、見積もった経路以外へ送らないようにします。
- API key、credential、秘密情報、ネットワーク権限は受け取らず、所有しません。
- 画像は Host が所有する URL や resource reference をそのまま渡せます。base64 などの inline media は継続状態に入れません。

そのため、新しい strategy Pack も同じ契約、catalog、bridge の仕組みで導入できます。tobkirithink のためだけの Host/Gateway 分岐はありません。
