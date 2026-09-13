# Tobkiri Pack v4 開発クイックスタート

このガイドは外部 **Normal Sandbox Pack** の v4 authoring と Host admission を説明します。
旧 `ecosystem.json`、PackImporter、mutable ecosystem directory への直接コピーは互換投影であり、
新しい Pack の authority、install、activation には使用しません。

## 1. v4 Pack を作る

```bash
cd tobkiri_runtime
python -m core_runtime.pack_scaffold my.pack --template minimal --output /secure/build/output
```

authoritative artifact set は次の4文書と、indexに列挙されたruntime fileです。

```text
my.pack/
├── pack.v4.json
├── contracts.v4.json
├── executables.v4.json
├── artifact-index.v4.json
└── runtime/
    └── handler.py
```

- `pack.v4.json` は Pack ID、version、`kind: normal_sandbox`、Function、requirementsを宣言します。
- `contracts.v4.json` は exact Contract/Operation schema と revision digest を持ちます。
- `executables.v4.json` は Function、Operation、implementation digest、PackVM backendを固定します。
- `artifact-index.v4.json` は全runtime artifactをdigestで列挙し、integrity sealを持ちます。
- Host Extensionは別package kind・署名namespace・install APIです。Normal Packのmanifest変更で
  Host Extensionへ昇格できません。

生成後は公式validatorを実行します。

```bash
python scripts/quality/validate_pack_architecture.py
```

## 2. publisher署名を作る

外部Packは `.tobkiri/signed-pack.json` にEd25519署名envelopeと完全なfile inventoryを持たせます。
署名はpublisher identityとintegrityを証明するだけで、approval、Grant、Host authority、enableを
与えません。秘密鍵をPack rootやリポジトリへ保存してはいけません。

Host側trust storeはPack外の安全なHost policy pathに置き、publisher/key/namespace/version/
Contract/capability要求をexactにpinします。unsigned、revoked key、extra/missing file、symlink、
digest不一致はfail closedです。

## 3. Host-owned admission

信頼済みinstallerだけが内部Host portを呼びます。desktop/web clientへsource pathやcatalog
documentを渡すAPIはありません。

```python
from pathlib import Path
from core_runtime.external_pack_catalog_v4 import admit_signed_external_pack

entry = admit_signed_external_pack(
    Path("/host-selected/quarantine/my.pack"),
    trust_store_path=Path("/host-policy/publisher-trust.json"),
)
```

Hostは署名file inventoryを検証し、非実行quarantineからdigest-pinned read-only CASへatomicに
promotionし、HMAC認証catalogとappend-only installed journalをcommitします。bundled canonical
catalogは変更しません。同じID/digestは冪等、同じID/異digestとbundled ID shadowはconflictです。

## 4. install、approval、Profile activation

admission後もPackは未install・未approval・disabledです。canonical Pack-control Contractへ
clientが送れるのはcatalogに存在する `pack_id` とone-shot candidateだけです。

1. `pack.install` — admitted Pack IDをHost stateへpinする。
2. `approval.candidate` / `approval.approve` — exact catalog/profile revisionへ署名済みapprovalを作る。
3. `pack.enable` — valid approvalとactive Grantを確認し、新しいimmutable Profile revisionを作る。

source path、Contract ID、Operation ID、`approved`、`enabled`、catalog recordをclientが注入する
generic dispatch/mutation経路はありません。disable/revokeは新Profile revisionを作り、古いsnapshotは
不変です。

## 5. 実行境界

ResolvedPlanはPack/Function/Contract/Operation/implementation digest/Profile/Authorityをexactに
固定します。HostはPack-selected pathをguestへ渡さず、index済みregular fileをdescriptor-relativeに
captureしてdigest-pinned payloadとしてPackVM supervisorへ渡します。guestは固定CAS namespaceへ
read-only stagingし、filesystem identityと全digestをinvoke直前にも再検証します。

直接executor、legacy ID、generic dispatch、unsigned artifact、暗黙enable、client authorityは使用禁止です。

## 6. 最低限の検証

```bash
python -m pytest tests/test_external_pack_catalog_v4.py -q
python -m pytest tests/test_artifact_materialization.py -q
python -m pytest tests/test_pack_control_v4.py -q
python scripts/quality/scan_pack_architecture.py
python scripts/quality/validate_pack_architecture.py
```

配布bundleを作る場合、`distribution_v1.schema.json` のintegrity blockとEd25519
`signature_envelope`が必須です。DistributionはPack IDとexact artifact digestをpinするだけで、
approvalやProfile activationを内包しません。
