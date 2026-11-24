"""Request-time observations around one unchanged actual full-catalog call.

No validation or owner/cache construction happens at module initialization.
All observation work executes inside the original invocation budget.
"""
import hashlib
import json
import sys


def read_catalog_observed(*, app_backend: bool = False) -> str:
    """Return a bounded receipt of the actual result and its real read path."""
    import _tobkiri_schema_kernel as kernel
    import jsonschema
    from jsonschema import _keywords
    import bundle_catalog_adapter as app
    from tobkiri_protocol import validation

    if type(app_backend) is not bool:
        raise RuntimeError('explicit App backend mode required')
    if app_backend:
        from app_schema_provider import AppSchemaProvider
        if (validation.Draft202012Validator is not AppSchemaProvider
                or getattr(_keywords, '_tobkiri_translated_keywords', False)
                or kernel.app_schema_frame_count() != 0):
            raise RuntimeError('actual App-owned provider and original document path required')
        count_entries = kernel.app_schema_entry_count
    else:
        if (getattr(_keywords, '_tobkiri_keyword_kernel', None) is not kernel
                or getattr(_keywords, '_tobkiri_translated_keywords', False) is not True
                or jsonschema.Draft202012Validator.iter_errors.__module__ != 'schema_adapter'):
            raise RuntimeError('actual generic Rust-connected library required')
        count_entries = kernel.keyword_entry_count
    if (kernel.production_enabled is not False
            or not kernel.__file__.endswith('/_tobkiri_schema_kernel.abi3.so')):
        raise RuntimeError('actual source-built Rust extension required')
    before = count_entries()
    if (before != 0 or validation.load_schema.cache_info().currsize != 0
            or validation._schema_store.cache_info().currsize != 0):
        raise RuntimeError('App workload was performed before the cold invocation')
    resources = validation._captured_schema_resources()
    if resources is None or type(resources).__module__ != 'bundle_catalog_loader':
        raise RuntimeError('actual captured schema owner required')
    resource_type = type(resources)
    read_was_owned = 'read_bytes' in resource_type.__dict__
    original_read = resource_type.read_bytes
    occurrence_type = app._OccurrenceReader
    original_occurrence = occurrence_type.__call__
    schema_reads = []
    reads = 0

    def read(path):
        raw = original_read(path)
        if path.tree is resources.tree and path.path[:-1] == resources.path:
            if len(schema_reads) >= 45:
                raise RuntimeError('actual schema read observation exceeded its bound')
            schema_reads.append(path.name)
        return raw

    def occurrence(reader, index, path, reread):
        nonlocal reads
        raw = original_occurrence(reader, index, path, reread)
        reads += 1
        if reads > 453:
            raise RuntimeError('actual entry read observation exceeded its bound')
        return raw

    resource_type.read_bytes = read
    occurrence_type.__call__ = occurrence
    try:
        # Keep the actual owner construction, source accessor and full reader.
        actual = app.read_catalog(app.embedded_owner())
    finally:
        if read_was_owned:
            resource_type.read_bytes = original_read
        else:
            del resource_type.read_bytes
        occurrence_type.__call__ = original_occurrence
    after = count_entries()
    cache = validation.load_schema.cache_info()
    if (after <= before or len(schema_reads) != 45 or reads != 453
            or (cache.misses, cache.hits, cache.currsize) != (45, 302, 45)):
        raise RuntimeError('actual App/Rust/resource/cache path differs')
    encoded = actual.encode('utf-8', errors='strict')
    if app_backend and after != 45:
        raise RuntimeError('each actual cold schema must enter the Rust frame backend once')
    receipt = {
        'format': ('tobkiri.app-frame-bundle-catalog-observation/1' if app_backend
                   else 'tobkiri.rust-bundle-catalog-observation/1'),
        'actual_sha256': hashlib.sha256(encoded).hexdigest(), 'actual_bytes': len(encoded),
        ('backend_entries_before' if app_backend else 'keyword_entries_before'): before,
        ('backend_entries_after' if app_backend else 'keyword_entries_after'): after,
        'schema_reads': schema_reads, 'entry_reads': reads,
        'schema_cache': {'misses': cache.misses, 'hits': cache.hits, 'size': cache.currsize},
        'captured_resource_owner': True,
        'kernel_file': kernel.__file__, 'keyword_file': _keywords.__file__,
        'keyword_loader': type(_keywords.__loader__).__module__,
        'python': list(sys.version_info[:3]),
        'qualification': False, 'production_enabled': False,
    }
    if app_backend:
        receipt['reference_counts'] = kernel.app_reference_counts()
    return json.dumps(receipt, sort_keys=True, separators=(',', ':'))
