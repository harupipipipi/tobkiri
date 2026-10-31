"""CI-only split-call observations of the actual cold App schema loader.

No workload, resources, names or validation results are prepared at import time.
The host keeps one original cumulative Store budget across every call. Splitting
the original loop adds boundary/observation costs and is never an acceptance gate.
"""
import json

import wit_world.exports
from componentize_py_types import Err
from wit_world.exports.projection import Error, ErrorCode

# Preserve the normal native export's eager adapter/validation metadata imports.
# This does not construct an owner, read schemas or run validation.
import bundle_catalog_adapter as app


class Projection(wit_world.exports.Projection):
    def project(self, payload: str) -> str:
        """List real captured schema names or call the real loader positionally."""
        import _tobkiri_schema_kernel as kernel
        from app_schema_provider import AppSchemaProvider
        from tobkiri_protocol import validation

        if (validation.Draft202012Validator is not AppSchemaProvider
                or kernel.production_enabled is not False):
            raise RuntimeError('explicit actual App frame provider required')
        resources = validation._captured_schema_resources()
        if resources is None or type(resources).__module__ != 'bundle_catalog_loader':
            raise RuntimeError('actual captured schema resource owner required')
        if validation._schema_store.cache_info().currsize != 0:
            raise RuntimeError('split diagnosis must not prefill the original schema store')
        outcome = 'metadata'
        row = {'format': 'tobkiri.app-schema-progress-step/1',
               'qualification': False, 'production_enabled': False,
               'captured_resource_owner': True, 'adapter_module': app.__name__}
        if payload == 'tobkiri.app-schema-progress-list/1':
            cache = validation.load_schema.cache_info()
            if ((cache.hits, cache.misses, cache.currsize) != (0, 0, 0)
                    or kernel.app_schema_entry_count() != 0
                    or kernel.app_schema_frame_count() != 0):
                raise RuntimeError('diagnostic must begin with the actual cold loader')
            # Match _schema_store's captured-resource branch and exact ordering.
            row['names'] = [path.name for path in sorted(
                (path for path in resources.iterdir() if path.name.endswith('.schema.json')),
                key=lambda path: path.name,
            )]
            if len(row['names']) != 42:
                raise RuntimeError('complete original42 captured schemas required')
        elif payload.startswith('tobkiri.app-schema-progress-load/1:'):
            name = payload.removeprefix('tobkiri.app-schema-progress-load/1:')
            if (not name.endswith('.schema.json') or len(name) > 128
                    or any(c in name for c in ('/', '\\', '\0'))):
                raise Err(Error(ErrorCode.INVALID_REQUEST, 'invalid schema name'))
            row['name'] = name
            try:
                # Same real argument form used by the original _schema_store.
                schema = validation.load_schema(name)
            except validation.SchemaValidationError as error:
                # The original store loop also continues after this typed error.
                outcome = 'schema-error'
                row['error'] = str(error)[:1024]
            else:
                outcome = 'loaded'
                row['schema_id'] = schema.get('$id')
        else:
            raise Err(Error(ErrorCode.INVALID_REQUEST, 'unknown schema diagnostic command'))
        cache = validation.load_schema.cache_info()
        row.update(outcome=outcome,
                   schema_cache={'hits': cache.hits, 'misses': cache.misses,
                                 'size': cache.currsize},
                   backend_entries=kernel.app_schema_entry_count(),
                   backend_frames=kernel.app_schema_frame_count())
        return json.dumps(row, sort_keys=True, separators=(',', ':'))
