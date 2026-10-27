"""Comparison-only native DATA export over the same immutable adapter."""

import wit_world.exports
from componentize_py_types import Err
from wit_world.exports.projection import Error, ErrorCode

from bundle_catalog_adapter import (
    BundleCatalogError,
    embedded_owner,
    identity_text,
    make_owner,
    read_catalog,
    source_identity,
)


class Projection(wit_world.exports.Projection):
    def project(self, payload: str) -> str:
        """Read only the original catalog collections from supplied DATA."""
        try:
            if payload == "tobkiri.embedded-bundle-identity/1":
                return identity_text(source_identity(embedded_owner()))
            return read_catalog(make_owner(payload))
        except BundleCatalogError as error:
            code = (
                ErrorCode.INVALID_REQUEST
                if error.code == "invalid-request"
                else ErrorCode.PROJECTION_FAILED
            )
            raise Err(Error(code, error.message)) from None
