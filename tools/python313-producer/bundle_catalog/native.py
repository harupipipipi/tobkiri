"""Comparison-only native DATA export over the same immutable adapter."""

import wit_world.exports
from componentize_py_types import Err
from wit_world.exports.projection import Error, ErrorCode

from bundle_catalog_adapter import (
    BundleCatalogError,
    embedded_owner,
    identity_text,
    read_catalog,
    source_identity,
)


class Projection(wit_world.exports.Projection):
    def project(self, payload: str) -> str:
        """Use the same fixed resource provider and reader as the ordinary Pack."""
        try:
            if payload == "tobkiri.embedded-bundle-identity/1":
                return identity_text(source_identity(embedded_owner()))
            if payload == "tobkiri.embedded-bundle-read/1":
                return read_catalog(embedded_owner())
            raise BundleCatalogError(
                "invalid-request", "unknown bundle comparison command"
            )
        except BundleCatalogError as error:
            code = (
                ErrorCode.INVALID_REQUEST
                if error.code == "invalid-request"
                else ErrorCode.PROJECTION_FAILED
            )
            raise Err(Error(code, error.message)) from None
