from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..host_contract import host_contract_value


@dataclass(frozen=True)
class CredentialEnvelope:
    credential_id: str
    provider_id: str
    connection_id: str
    material_type: str
    ciphertext: str
    key_version: str


class CredentialStore(Protocol):
    def put(self, provider_id: str, connection_id: str, material_type: str, secret_material: dict) -> CredentialEnvelope: ...
    def get(self, credential_id: str) -> dict: ...
    def delete(self, credential_id: str) -> None: ...


class LocalEncryptedCredentialStore:
    """Small encrypted local store for self-host/dev.

    Production hosted Rumi should back this with the platform secret manager/KMS.
    This implementation intentionally requires a key. No silent plaintext fallback.
    """

    def __init__(self, path: str | Path, key: str | None = None, key_version: str = "local-v1") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = key or host_contract_value("credential_store_key")
        self.key_version = key_version
        if not self.key:
            raise RuntimeError("RUMI_CREDENTIAL_FERNET_KEY is required for LocalEncryptedCredentialStore")
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise RuntimeError("cryptography is required for LocalEncryptedCredentialStore") from exc
        self._fernet = Fernet(self.key.encode("utf-8"))

    @staticmethod
    def generate_key() -> str:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode("utf-8")

    def _read_all(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_all(self, data: dict[str, dict]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        _chmod_private(tmp)
        tmp.replace(self.path)
        _chmod_private(self.path)

    def put(self, provider_id: str, connection_id: str, material_type: str, secret_material: dict) -> CredentialEnvelope:
        credential_id = f"cred_{uuid4().hex}"
        plaintext = json.dumps(secret_material, separators=(",", ":")).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext).decode("utf-8")
        envelope = CredentialEnvelope(
            credential_id=credential_id,
            provider_id=provider_id,
            connection_id=connection_id,
            material_type=material_type,
            ciphertext=ciphertext,
            key_version=self.key_version,
        )
        data = self._read_all()
        data[credential_id] = asdict(envelope)
        self._write_all(data)
        return envelope

    def get(self, credential_id: str) -> dict:
        data = self._read_all()
        if credential_id not in data:
            raise KeyError(f"Unknown credential: {credential_id}")
        plaintext = self._fernet.decrypt(data[credential_id]["ciphertext"].encode("utf-8"))
        return json.loads(plaintext.decode("utf-8"))

    def delete(self, credential_id: str) -> None:
        data = self._read_all()
        data.pop(credential_id, None)
        self._write_all(data)


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
