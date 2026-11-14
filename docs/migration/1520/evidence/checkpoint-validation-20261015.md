# Archived 2026-10-15 checkpoint

The original checkpoint JSON is retained byte for byte in
[checkpoint-validation-20261015.json.gz](checkpoint-validation-20261015.json.gz).
The [original path](../checkpoint-validation-20261015.json) is now a small index
containing both SHA-256 digests, original/compressed sizes and historical scope.
The original document was streamed to gzip, decompressed and verified against
the unchanged source before replacing that source with the index. All original
checks, logs and metadata remain in the archive; no historical result was
reclassified as current validation or completion.

For inspection, decompress into a separate file, then verify its SHA-256 against
`original_sha256` in the index. Historical evidence which references the old JSON
path must use these verified decompressed original bytes when checking an old
digest. Do not treat the archive index as the original execution receipt.

This removes an oversized diagnostic payload from production-source scanning
without relaxing its existing 512 KiB limit. The historical evidence directory
already has its own explicit classification in the compatibility check.
