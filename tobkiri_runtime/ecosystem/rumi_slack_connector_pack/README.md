# Rumi Slack Connector Pack

Owns Slack `v0` signature verification, five-minute request timestamp checks,
Events API normalization, and receipt-gated `chat.postMessage` delivery. Slack
credentials are resolved only for this pack and operation scope. No chat or
Company implementation is imported.

It also provides the Slack side of the connector OAuth contract: an
S256-PKCE authorization URL and a bounded token exchange. Client credentials,
OAuth codes, access tokens, and signing secrets remain inside credential-bound
pack calls and are never included in the public connector configuration.

