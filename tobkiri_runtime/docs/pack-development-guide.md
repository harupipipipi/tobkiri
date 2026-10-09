# Tobkiri Pack v4 開発クイックスタート

このガイドは外部 **Normal Sandbox Pack** の v4 authoring と Host admission を説明します。
旧 `ecosystem.json`、PackImporter、mutable ecosystem directory への直接コピーは互換投影であり、
新しい Pack の authority、install、activation には使用しません。

## 1. v4 Pack を作る

```bash
cd tobkiri_runtime
python -m core_runtime.pack_scaffold my.pack --template minimal --output /secure/build/output
```

このコマンドは実行権限のない空の **authoring scaffold** を生成します。
`minimal` の実際の出力は次の6ファイルです。`runtime/handler.py` は生成しません。

```text
my.pack/
├── scaffold-source.v1.json
├── README.md
├── pack.v4.json
├── contracts.v4.json
├── executables.v4.json
└── artifact-index.v4.json
```

- 4つのv4文書がPackの宣言、公開Contract、実装の対応、ファイルdigestを保持します。
- 生成直後は `execution_boundary: declarative_only`、Functionsとcapabilitiesは空です。
- `capability`、`flow`、`full` は追加のActivity/Skill/Toolと関数ソースの例を生成しますが、
  それらも実行可能なv4 Functionとしては登録されません。
- `refresh_scaffold_artifacts` は空の実行カタログを含む雛形を再生成します。
  追加した関数ソースを自動登録するコンパイラとして使わないでください。
- 外部Packの開発だけでbundled catalogや他Packの内部ファイルを変更する必要はありません。

### 指定したPackのメタデータを検証する

上の `tobkiri_runtime` ディレクトリから実行します。
対象パスは自分が生成したディレクトリに置き換えてください。

```bash
python - /secure/build/output/my.pack <<'PY'
import sys
from pathlib import Path
from tobkiri_host.artifact_compiler import compile_pack_root

compiled = compile_pack_root(Path(sys.argv[1]))
print("Pack metadata compiled:", compiled.artifact.pack_id)
print("This is not signature, install, activation, or runtime verification.")
PY
```

この検証は4文書間の整合性と宣言された実装の対応を確認します。
外部Packの署名・全ファイルinventory検証は後述のHost admissionで別途行います。
`python scripts/quality/validate_pack_architecture.py` はTobkiriリポジトリ全体の
検証です。リポジトリ外に作ったPackをこのコマンドだけで検証できたとは判断しません。

### 動く機能にするために残る作業

公開Contract/Operation、入出力schema、Function、実装とdigestの対応を定義し、
ロジック単体のテスト、署名、Host admission、Profile選択、実際の呼び出しを順に確認します。
現状、この雛形コマンドだけでは「実行可能なhello world」まで完成しません。
UIのJSON宣言や独自画面を追加した場合も、表示と実行の検証はそれぞれ必要です。

Host Extensionは別package kind・署名namespace・install APIです。
Normal Packのmanifest変更でHost Extensionへ昇格できません。

## 2. publisher署名を作る

外部Packは `.tobkiri/signed-pack.json` にEd25519署名envelopeと完全なfile inventoryを持たせます。
署名はpublisher identityとintegrityを証明するだけで、approval、Grant、Host authority、enableを
与えません。秘密鍵をPack rootやリポジトリへ保存してはいけません。

Host側trust storeはPack外の安全なHost policy pathに置き、publisher/key/namespace/version/
Contract/capability要求をexactにpinします。unsigned、revoked key、extra/missing file、symlink、
digest不一致はfail closedです。

## 3. Host-owned admission

信頼済みinstallerだけが内部Host portを呼びます。Web UI と Pack-control Contract に
source path や catalog document を渡す API はありません。

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

Launcher の **Packs → Trust and add signed Pack** では、ネイティブダイアログで署名済み
Pack フォルダと、そのフォルダ外にある Ed25519 公開鍵 PEM を別々に選びます。Host が署名、
全ファイル、Normal Sandbox v4 manifest を検証し、Pack ID、publisher、公開鍵 fingerprint、
要求 capability、Contract、artifact digest を返します。Launcher のネイティブ確認ダイアログで
明示承認すると、Host が再検証して publisher policy と exact install record を同一の Host policy
書き込みで保存し、signed CAS catalog へ admission します。選択パスは Web UI に渡りません。
Pack 内の鍵を自動信頼する経路はありません。署名・install path・artifact の検証に失敗すると
登録されません。Launcher は `user_data/pack_control/publisher_trust.v4.json` を Host policy path
として使い、Kernel と Defaultspack の両方へ同じ `RUMI_PACK_PUBLISHER_TRUST_STORE` を渡します。
既に Host policy と exact install record がある場合は **Add from trusted folder** も使えます。
この policy の安全なディレクトリ検証は現在 POSIX Host 専用です。Windows ではこの追加操作を
Launcher に表示しません。

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
