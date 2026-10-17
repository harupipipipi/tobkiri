# Linux owner admission and generation inspection

The new Linux allocation-owner branch admits only x86_64 GNU with an actual
ext4 mount. It combines the held directory fd's `fstatfs` and `statx` identity
with a matching mount-ID/device/ext4 record read from verified procfs, then
checks the same fd again. Ext-family magic alone is insufficient. Unknown APIs,
filesystems, malformed or excessive evidence remain unavailable. The bounded
parser currently rejects non-ASCII mountinfo, including otherwise legal mount
paths. Existing UID/mode checks, inode comparison, flock, PID/fork restriction,
Arc custody, quota/VFS, checked rename/fsync and eight data-move crash stages
remain in place.

Six parser tests ran successfully on Windows. This is not native Linux proof.
The dedicated workflow explicitly runs the real ext4/fork/exec owner suite,
actual tmpfs refusal, existing owner/quota integrations and unchanged Store/Core
tests. Its native facts and all substantive assertions must pass before Linux
acceptance. A process-kill test does not prove power-loss durability. Existing
Mac and Windows product admission branches are unchanged.

`packs/recovery/` supplies an independent ordinary read-only OpenRouter
generation reader. It uses the Profile's existing opaque key under its own
Gate-authorized principal, fixed GET endpoints and bounded responses. Its
workspace does not change the original provider workspace lock or artifacts.
The profile preparation helper preserves every old TPK byte and DeepThink
configuration, then adds only the reader and typed User port. Normal Profile
apply still requires its expected head and approval.

Pack archive verification is separate from WIT/component admission. The new
ignored `abi::generation_reader_tests` test takes
`TOBKIRI_TEST_GENERATION_READER_STAGE`, verifies a real built Pack, resolves its
authoritative shape/world and checks the actual component against production
import and ABI validation. It never starts a Session or performs a lookup. The
dedicated workflow builds and stages this reader separately from the existing
Pack/Profile inventory before running that exact test.

Metadata availability, native/normalized token counts, exact decimal cost,
terminal state and optional stored public output are separate observations.
Missing cost stays unknown. Identity mismatches return bounded diagnostic
identity fields while withholding usage, terminal acceptance and content.
They never approve aliases or enable regeneration. Raw inputs, reasoning,
headers and error bodies are not returned. See `packs/recovery/README.md`.

The existing interrupted MiMo attempt remains unresolved. This reader does not
rewrite its checkpoint, finalize the attempt, resend it or declare the workflow
complete. A future resolution needs immutable attempt/config binding, actual
accounting, public-prefix/schema checks and a durable single-use claim before
any explicit new attempt. KV replacement alone supplies no compare-and-swap
guarantee. Terminal-Bench remains gated on complete workflow/tool acceptance.
