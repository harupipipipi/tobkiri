# Development Host handover

This macOS development Launcher feature preserves a verified base VM image
when switching between development builds, whose user-data namespaces are
bound to their executable digests. Build the updated Launcher through the
normal desktop workflow described in the repository startup instructions.

For a switch, quit the previous Launcher and wait for its Kernel,
Application and guest processes to exit. Keep the previous app and checkout
available. In the updated Launcher, enable development tools and open
Settings → Previous development Host. The native pickers select the old
`.app` and its exact `user_data` folder. Review the displayed plan before
choosing **Move storage and import data**. A browser or Pack cannot provide
the selected paths or approve this operation. The feature rejects live
owners, changed files, existing target storage and stopped registrations.
Closed guest allocations may remain only in the previous namespace: the
plan binds their exact finite file inventory, inode/size/change-time identities
and the bounded allocation-record digest without reading private guest seeds,
requires exited owners and quiescent storage, and rechecks them at publication
and recovery. Unknown files or unresolved allocation recovery still block
handover. These allocations are never moved, copied, deleted or resumed.
Use ordinary Launcher Quit rather than the separate
administrative PackVM Stop command; the latter is not a handover operation.

The plan transfers only the authenticated instance metadata and verified
immutable base image, using exclusive moves on the same volume. It imports
Profile definitions, disabled connection/model metadata and inert text
history through their data owners. Credentials, authority, grants, active
Profile pointers and execution continuations remain in the original
namespace. Attachments, widgets, events, tool logs and Profile content
projections also remain there. This is not a live guest resume or a complete
copy of all Pack data. Register credentials again and review/activate the new
Profile through the normal approval flow before execution.

If interrupted, use **Check migration recovery**. Recovery has its own exact
native review and fresh approval. It returns the transferred storage when
its identities still match; staged data, immutable destination Profile
successors, keys and recovery evidence are retained. The updated Host stays
blocked after recovery. A lost initial intent can be recorded as abandoned
only when neither staging nor VM custody occurred. Lost or tampered evidence
after custody fails closed and requires diagnosis; do not delete journals,
keys or staging directories. Keep the old Host stopped until recovery has
finished. A previous build without the handover protocol cannot prevent its
own later restart, so the operator must enforce that condition.
