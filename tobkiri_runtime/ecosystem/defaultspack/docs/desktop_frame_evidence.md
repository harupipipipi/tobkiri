# Desktop frame evidence

`desktop_frame` is a read-only observation operation. It returns an in-memory
frame and never writes a screenshot to disk. This remains true for the managed
desktop HTTP `GET /api/desktops/{seat_id}/frame` route, including repeated reads
of the same cached frame.

Durable visual QA evidence is a separate operation:

1. Call `desktop_frame` and inspect the returned `frame_seq`.
2. When a screenshot is necessary for QA or a bug report, request approval for
   `desktop_frame_evidence`.
3. Call it with `action=persist`, the exact `seat_id` and `frame_seq`, and one of
   `visual_qa`, `bug_report`, or `accessibility_qa` as `purpose`.
4. Keep the returned opaque `artifact_ref` in the tool history or QA report.

The evidence operation rechecks access to the seat and requires server-derived
run, conversation, workspace, seat, and principal identity. It commits only the
latest cached observation with the exact requested revision. A different seat,
stale revision, replayed authority context, or client-provided identity fails
closed. It does not recapture the screen and has no host execution fallback.

## Privacy and retention

Screenshots may contain credentials, personal information, notifications, or
content from another workspace. Evidence therefore uses these boundaries:

- Saving, exporting, deleting, and run cleanup are approval-aware write-like
  operations governed by the captured ProfileLock/ResolvedPlan tool policy.
- Results expose an opaque `frame_evidence_...` reference, not a filesystem
  path, seat path, principal, device identifier, or storage root.
- Files and metadata are local-only with directory mode `0700` and file mode
  `0600`. They are not encrypted at rest; deployments needing encryption must
  provide it at the filesystem/device layer.
- The default TTL is seven days and may be set from 60 seconds up to 30 days.
  Expired records are removed during evidence access or the next commit.
- Each run/principal binding is limited to 50 artifacts and 50 MiB. Recommitting
  the same revision, content, purpose, and authority binding returns the same
  reference without another file.
- `action=delete` removes one exact bound reference. `action=cleanup_run`
  removes evidence for the exact current run/conversation/workspace/seat/
  principal binding. Run completion otherwise retains evidence until its TTL so
  post-run issue review remains possible.
- `action=export` returns verified bytes only after the same binding and access
  checks. Audit records contain the opaque reference, digest, size, purpose, and
  retention outcome, never screenshot bytes or a local path.

Storage failure is reported only by the explicit evidence operation. It cannot
turn an ordinary frame read into a failing or write-like request. The feature
uses no network service or cloud key and preserves defaultspack local-first
startup.
