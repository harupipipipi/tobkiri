# Rumi Media Inspect Service Pack

This read-only service parses bounded workspace text documents and reports
metadata for image, audio and recording artifacts. Every file access goes through
`rumi.service.file.inspect.v1`; absolute paths and parent traversal are rejected.

Binary decoders and vision inference are intentionally replaceable downstream
contracts. Unsupported document formats return `unavailable` rather than fake
parsed content. This pack owns no capture device and writes no artifact.

Validation was not executed by the implementation agent.
Independent testing is required before merge.

