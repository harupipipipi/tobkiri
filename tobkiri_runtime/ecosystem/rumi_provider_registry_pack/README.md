# Tobkiri Provider Registry Pack

This pack is the authoritative owner for configured provider instances. A
record references an adapter ID, optional endpoint, and opaque credential
handle. It contains neither provider catalog data nor provider execution code.

Remote capability and health are `unknown` until fresh, verified adapter
evidence is stored. Client-supplied optimism is never treated as availability.
Mutations require an expected revision; migration is source-hash pinned and
retains an owner-only rollback backup.

OpenRouter native `routing.max_price` policies use exact nonnegative USD
decimal strings, for example `{"prompt": "1", "completion": "2.7"}`. Inputs
must be unsigned plain decimals with at most six fractional digits, no leading
integer zeros, and a value no greater than `9007199254.740991` USD. Trailing
fractional zeros are removed when the policy is normalized. Existing bounded
integer prices, including zero, keep their integer representation and approval
digests. Binary floats, exponents, signs, and whitespace are rejected.

The request, approved plan, and saved registry retain exact canonical JSON
values. Only the provider compiler converts prices to OpenRouter's numeric
wire format; the host generation budget still checks the outbound rates.
Compilation rejects a decimal limit if conversion to the provider's numeric
format would change its exact value.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including startup, stale revision, secret rejection,
unknown health, migration, rollback, and pack-removal isolation.

