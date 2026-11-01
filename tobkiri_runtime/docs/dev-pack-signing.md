# Persistent development Pack signing

The Pack SDK can keep one local development signer across Pack versions.
Initialization is explicit. Signing only loads an existing signer and refuses
missing, insecure, corrupt, changed, or replaced stores.

Choose an absolute store path outside the repository, Pack trees, artifact CAS,
VM state, Native credentials, and generation-budget state. Its parent must
already exist, belong to the caller, and exclude group/other writes and extended
macOS ACL permissions. Pass the deployment's actual protected roots through
`--forbidden-root` when initializing and `--dev-signer-forbidden-root` when
signing. The SDK always excludes this repository and known state namespaces;
signing also excludes its Pack root.

From `tobkiri_runtime/`, initialize once:

```sh
python -B scripts/tobkiri_pack.py dev-signer-init /absolute/dev-signers/publisher \
  --publisher-id publisher.example \
  --forbidden-root /absolute/runtime-state \
  --forbidden-root /absolute/artifact-cas
```

Retain the returned public `key_id` and `fingerprint` independently. The store
contains an unencrypted Ed25519 `private.pem` and `identity.json`, both owned,
single-link, regular files with mode `0600`, inside an owned `0700` directory.
The metadata contains the public key and its identity. CLI output contains only
public identity fields, never private key material. No existing directory or
file is replaced, including a partial initialization.

Sign each version using the same publisher, store, and previously recorded key
ID:

```sh
python -B scripts/tobkiri_pack.py sign /absolute/my-pack \
  --dev-signer-store /absolute/dev-signers/publisher \
  --dev-signer-key-id PREVIOUSLY_RECORDED_32_HEX_KEY_ID \
  --publisher-id publisher.example --pack-id example.pack --version 1.0.1 \
  --core-compatibility '>=1.10,<2' \
  --dev-signer-forbidden-root /absolute/runtime-state \
  --dev-signer-forbidden-root /absolute/artifact-cas
```

The supplied key ID prevents a newly generated replacement store from silently
changing signing lineage. Store publisher identity must match exactly. Files and
directory identities remain checked through the signing operation, including
immediately before manifest publication. Loading and signing never initialize,
repair, regenerate, migrate, or discard a signer.

Back up the complete store through the operator's existing private-key backup
process. If its key is lost, initialization cannot recover that key. An artifact
already blocked by a different pinned publisher key remains blocked. A new key
requires the legitimate Native publisher-key addition/version process; this CLI
does not update trust, admission, permissions, or generation budgets, and does
not create a new publisher automatically.

The existing `sign --private-key PATH` route remains available with its prior
behavior. It is mutually exclusive with `--dev-signer-store`.
