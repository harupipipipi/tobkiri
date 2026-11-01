"""Thin native DATA comparison export; no Core imports, rights or new IO."""
import wit_world.exports
from componentize_py_types import Err
from wit_world.exports.projection import Error, ErrorCode

from projection_adapter import ProjectionError, project


class Projection(wit_world.exports.Projection):
    def project(self, payload: str) -> str:
        """Call the identical App adapter used by the ordinary Pack world."""
        try:
            return project(payload)
        except ProjectionError as error:
            code = (ErrorCode.INVALID_REQUEST if error.code == "invalid-request"
                    else ErrorCode.PROJECTION_FAILED)
            raise Err(Error(code, error.message)) from None
