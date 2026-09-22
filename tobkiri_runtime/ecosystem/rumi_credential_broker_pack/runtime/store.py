"""Compatibility exports for the Host-owned credential material store."""

from tobkiri_host.credential_store import (
    KEY_VERSION,
    STORE_VERSION,
    HostCredentialMaterialStore,
)

CredentialBrokerStore = HostCredentialMaterialStore

__all__ = ["CredentialBrokerStore", "KEY_VERSION", "STORE_VERSION"]
