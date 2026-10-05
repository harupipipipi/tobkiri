/*
 * tobkiri worker memory guard (macOS direct-process path)
 * =======================================================
 *
 * A userspace committed-bytes hard cap for one supervised worker process.
 * The supervisor injects this library with DYLD_INSERT_LIBRARIES; the
 * __interpose section routes every mediated memory-commit entry point used
 * by the sealed worker runtime (the Python interpreter, libwasmtime and the
 * guest's linear-memory commits) through accounting that fails the call
 * once a ceiling would be crossed:
 *
 *   - TOBKIRI_WASM_GUARD_CAP_BYTES (constructor): lifetime committed cap.
 *     Any commit past it fails at the call site, covering interpreter
 *     startup, the Wasmtime compiler and guest execution alike.
 *   - tobkiri_wasm_guard_arm_headroom() (called by the worker after
 *     compilation): tightens the cap to used + headroom so the guest phase
 *     can only grow committed bytes by its declared budget.
 *
 * Failing calls return ENOMEM / KERN_RESOURCE_SHORTAGE, so the guarded
 * process can never hold more private committed memory than the cap: a
 * transient allocation spike cannot physically occur inside a sampling
 * interval, it fails at commit time instead.
 *
 * Accounting model — exact committed extents.  The region table is a
 * sorted set of disjoint committed extents: every byte of a tracked entry
 * is committed.  A commit is charged only the bytes of its span not
 * already covered (so two disjoint commits inside one reservation are
 * charged in full, each), and a real decommit (munmap, vm_deallocate,
 * mach_vm_deallocate — the only mediated calls whose XNU semantics
 * actually drop the pages) releases exactly the committed bytes inside
 * its own span (never more, even when the span is partly uncommitted).
 * A middle unmap splits an extent so the tail keeps its accounting.
 * Committed bytes can therefore never be under-counted.
 * Every successful new mapping additionally reconciles its range against
 * kernel ground truth before adding: the kernel never places a fresh
 * range over a live mapping, so extents still recorded there are stale
 * (kernel-side frees no userspace call can observe) and are released
 * first — an ordered sub then add on allocate/map/remap/read/reclaim
 * paths.
 *
 * Retention is not release.  On XNU, madvise(MADV_DONTNEED) and
 * posix_madvise(POSIX_MADV_DONTNEED) leave the pages resident and
 * readable (verified: contents survive intact after 2 GiB of allocation
 * pressure), and mprotect/vm_protect/mach_vm_protect to a non-usable
 * protection (PROT_NONE) likewise retains them recoverably — none of
 * them is a decommit, so NONE of them releases charge: the range stays
 * counted exactly like MADV_FREE, and only a real free above drops it.
 * Treating them as frees was a demonstrated cap bypass (16 x 64 MiB of
 * mmap+touch+DONTNEED read as 27 MiB used under a 256 MiB cap while
 * phys_footprint held 1.07 GiB).
 *
 * Page envelopes.  The kernel acts on whole pages, so every hooked range
 * is normalized to the kernel's effective page-aligned envelope —
 * base rounded DOWN to a page boundary and base+len rounded UP — before
 * any charge/delta is computed and before any extent op, matching XNU's
 * vm_map_trunc_page/vm_map_round_page treatment of the vm_ / mach_vm_
 * arguments and the length rounding of mmap/mprotect/madvise/munmap.
 * mprotect/munmap still EINVAL an unaligned address in the kernel, so for
 * them the rounding only ever affects the length end.  An unaligned
 * vm_protect/mach_vm_protect (and the same-shape vm_copy/mach_vm_copy,
 * vm_wire/mach_vm_wire) therefore can no longer slip a sub-page charge or
 * over-release past the cap.
 *
 * Lock contention — bounded pending ring, bounded spin fallback.  Hooks
 * mutate the extent table under a try-lock so signal/allocator re-entry
 * never blocks.  A hook that loses the race enqueues the completed
 * operation {add|sub, base, end, gen} into a bounded lock-free ring, and
 * whichever thread holds the lock drains the ring before unlocking
 * (every holder drains first, before its own table op, so the table
 * reflects every already-completed op it can see).  A contended commit
 * is still tracked — a later unmap releases it — and a contended
 * decommit still frees its charge.
 *
 * Deferred-SUB ordering — per-op generations, stamped asymmetrically.
 * A deferred op is enqueued AFTER its kernel syscall returned, so
 * preemption between return and enqueue can invert ring order vs kernel
 * order: a SUB that freed a range could apply after a younger ADD that
 * re-committed it and wipe the live extent.  Generation stamps alone
 * could not fully prevent that while every op stamped post-syscall: a
 * decommitter preempted between its syscall return and its stamp would
 * draw a generation NEWER than a commit that landed after its decommit
 * and would release the live extent — the demonstrated under-count.
 * Decommit-class ops therefore stamp their generation BEFORE issuing
 * the decommit syscall, while commit-class ops stamp AFTER the commit
 * syscall returns.  A SUB's generation is then always older than the
 * generation of any commit whose kernel operation postdates the SUB's
 * own (that commit stamps after it completes, which is after the SUB
 * stamped), so a SUB can never appear newer than a live extent it would
 * wrongly release.  The mirror image remains and is safe: a SUB whose
 * kernel decommit ran AFTER a same-range commit still carries the
 * older generation and is skipped, keeping a bounded phantom
 * (over-count) released by the next real decommit or placement
 * reconcile.  Placement reconciles stamp the same way: the
 * reconcile-SUB generation predates the placement syscall, the ADD
 * generation postdates it.
 *
 * Ring exhaustion — no latch.  A producer that finds the ring full does
 * not drop the op and does not permanently stale the table: it takes
 * the owner-encoded lock word by spinning — safe because the word
 * itself identifies the holder, so the spin is entered only when the
 * holder is ANOTHER thread whose bounded critical section releases
 * promptly; a signal frame that interrupted this thread's own locked
 * frame reads its own id and keeps the stale fallback — drains the
 * backlog itself and applies its own op synchronously.  The ring
 * (16384 slots) is thus effectively unbounded under realistic load:
 * sustained contention degrades to serialized synchronous accounting,
 * never to a permanent stale latch, so g_used cannot diverge.  The one
 * non-releasing hold — a holder dying mid-section — is caught by the
 * spin's wall-time deadline: the guard is poisoned (g_dead) and every
 * later counted commit is refused, failing closed instead of wedging.
 *
 * Documented over-count cases (accounting may exceed the true committed
 * bytes, never fall below):
 *
 *   - Ring exhaustion in a re-entrant (signal-in-locked-frame) context,
 *     or the stale-SUB phantom described above: the table then stays
 *     stale/keeps the phantom span and commits keep charging full spans
 *     until a real decommit or reconcile clears it.
 *   - Extent-table exhaustion (8192 entries): a commit that cannot be
 *     recorded is still reserved in g_used, and the untracked span stays
 *     fully chargeable on later commits and releases nothing on decommit.
 *   - MADV_FREE, MADV_DONTNEED and POSIX_MADV_DONTNEED are not
 *     decommits on XNU (FREE marks pages reusable; DONTNEED leaves
 *     them resident and readable — verified), so every madvise-class
 *     range stays counted until a real free releases it.  The same
 *     holds for mprotect/vm_protect/mach_vm_protect to a non-usable
 *     protection: PROT_NONE pages survive pressure recoverably
 *     (verified), so a non-usable protect releases nothing.
 *   - vm_copy/mach_vm_copy charge the whole uncommitted share of the
 *     destination span; vm_wire/mach_vm_wire and mlock charge the
 *     uncommitted share of the wired span; vm_read_overwrite/
 *     mach_vm_read_overwrite and vm_write/mach_vm_write charge the
 *     uncommitted share of the destination range they populate.
 *   - vm_* task-self entry points can be thin wrappers over mach_vm_* on
 *     some libsystem builds: one logical commit may then be counted twice.
 *   - vm_write/mach_vm_write and the protect/copy/wire families charge
 *     this process even when the target map is a foreign task (the
 *     accounting cannot attribute foreign commits — conservative).
 *     Deallocations are the opposite: vm_deallocate/mach_vm_deallocate,
 *     remap destination placements, and the reclamation resize/flush on
 *     a foreign target release bytes in that task's map, so they are
 *     accounted ONLY when target == mach_task_self() — clipping our
 *     extents by a foreign range would under-count.
 *   - The seed is a deterministic vm_region sweep at load: every usable,
 *     non-reserved, anonymous (external_pager == 0) region is charged
 *     exactly as the hooks would have charged it had the guard been
 *     mapped first — file-backed MAP_PRIVATE regions (__TEXT etc.)
 *     commit nothing at map time under the hook predicate, so the sweep
 *     skips them too — so the reported used bytes and the armed headroom
 *     cap are identical for identical launches (the old resident_size
 *     seed varied run to run, RSS <> committed).  Only if the sweep
 *     itself fails does a resident_size fallback stand in, still
 *     conservative.
 *   - File-backed or PROT_NONE mappings carry no commit charge at map
 *     time (only anonymous usable mappings are charged, private or
 *     MAP_SHARED — anonymous shared pages are real committed bytes, e.g.
 *     Python's MAP_SHARED|MAP_ANON mmap.mmap).  If such a mapping is
 *     later made writable it is charged by the protect hook like a
 *     private commit.  A shared-anonymous region another process grows
 *     via an object this task also holds is charged fully at map time
 *     here — conservative over-count.
 *   - mach_vm_protect/vm_protect with set_max=TRUE changes only the
 *     maximum protection and is accounted as nothing.
 *   - mlockall(MCL_CURRENT) charges a post-hoc vm_region sweep of usable
 *     private non-reserved regions; a region added or split mid-sweep can
 *     be missed — it is charged by whichever hooked commit touches it
 *     next.  munlock/munlockall are deliberately unhooked: unwiring does
 *     not decommit, so the pages stay counted until a real decommit.
 *   - The reclamation buffer's backing mapping is created at its
 *     max_len/max_capacity footprint — round_page(0x30 + 16*max)
 *     usable bytes — and never changes size afterwards (xnu
 *     vm_reclaim.c: vdrm_ring_size is immutable, resize may only
 *     republish the vdrm_buffer_len watermark within that span —
 *     KERN_NO_SPACE beyond it — and flush's bytes_reclaimed is the
 *     VA total of reclaimed VICTIM regions, not a buffer shrink).
 *     Only allocate commits, so the max-capacity footprint is
 *     charged there and resize/flush are deliberate no-accounting
 *     pass-throughs; the buffer's bytes stay counted until a real
 *     decommit of its span, and reclaimed victim regions stay
 *     counted too — an in-kernel release no hook can see
 *     (over-count only).
 *
 * Mediated entry points (interpose coverage table):
 *   allocate/map : mmap, vm_allocate, mach_vm_allocate, vm_map,
 *                  mach_vm_map, vm_remap, vm_remap_new, mach_vm_remap,
 *                  mach_vm_remap_new
 *   commit-in-place : mprotect, vm_protect, mach_vm_protect,
 *                  vm_copy, mach_vm_copy, vm_wire, mach_vm_wire, mlock,
 *                  mlockall (post-hoc region sweep), vm_read_overwrite,
 *                  mach_vm_read_overwrite, vm_write, mach_vm_write
 *   kernel-allocates-here : vm_read, mach_vm_read,
 *                  mach_vm_deferred_reclamation_buffer_allocate,
 *                  mach_vm_reclaim_ring_allocate — both charge the
 *                  MAX-capacity footprint the kernel maps at allocate
 *                  (the userspace ring API reaches the kernel buffer
 *                  through intra-libsystem direct calls that bypass
 *                  interposition, so the ring entry points themselves
 *                  are hooked).  mach_vm_deferred_reclamation_buffer_
 *                  resize/flush and mach_vm_reclaim_ring_resize stay
 *                  interposed as deliberate no-accounting
 *                  pass-throughs: the buffer mapping is immutable
 *                  after allocate and bytes_reclaimed counts
 *                  reclaimed VICTIM regions, not a buffer shrink.
 *   decommit     : munmap, vm_deallocate, mach_vm_deallocate — the only
 *                  mediated calls that really drop pages on XNU
 *   retained, not released (pass-through hooks that keep the charge,
 *                  verified non-decommits): madvise(MADV_DONTNEED),
 *                  posix_madvise(POSIX_MADV_DONTNEED), and the
 *                  non-usable-protection half of mprotect/vm_protect/
 *                  mach_vm_protect
 *   Python mmap.mmap (-1, len) reaches the guard through the mmap hook —
 *   verified empirically.
 *
 * Unmediated commit/release channels — listed honest boundary:
 *
 *   - vm_read_list/mach_vm_read_list: like vm_read the kernel allocates
 *     the returned buffers inside this map — an uncounted commit (not
 *     issued by the sealed interpreter/libwasmtime/Pulley stack).
 *   - mach_make_memory_entry/mach_make_memory_entry_64: creating the
 *     entry commits nothing, but a later vm_map/mach_vm_map of that
 *     object — especially with copy=TRUE — grows private pages by in-
 *     kernel COW fault that no userspace hook can observe: entry-backed
 *     mappings are uncharged at map time and their written growth bypasses
 *     the cap.  The same hole exists for MAP_PRIVATE file-backed mmap:
 *     the first write to each page is an in-kernel COW fault (N4).
 *   - vm_purgable_control/mach_vm_purgable_control and
 *     mach_memory_entry_purgable_control: marking memory volatile lets
 *     the kernel reclaim it with no userspace event — a release we cannot
 *     see, so those bytes stay counted (over-count only).
 *   - mach_vm_deferred_reclamation_buffer_update_reclaimable_bytes: same
 *     invisible-reclaim shape as purgable control (over-count only).
 *   - mach_msg OOL receive: a mach_msg receive carrying out-of-line
 *     descriptors commits fresh pages inside this map in-kernel — the
 *     same unmediated-commit class as vm_read_list.  There is no
 *     per-descriptor userspace entry point to interpose (the receive is
 *     a bare trap), so OOL receives stay uncounted — documented, not
 *     interposed.
 *   - mach_vm_reclaim_ring_flush, mach_vm_reclaim_try_cancel,
 *     mach_vm_reclaim_update_kernel_accounting and the ring
 *     introspection calls (capacity/query_state/round_capacity/
 *     is_reusable): the kernel reclaims the regions a ring entry
 *     describes with no userspace event on the region itself — a
 *     release we cannot see, so those bytes stay counted (over-count
 *     only).  The ring buffer's own commit is charged at its
 *     allocate-time max-capacity footprint (interposed above).
 *   - mach_vm_reclaim_try_enter: entering a full ring may auto-grow the
 *     kernel-side ring buffer up to the allocate-time max_capacity with
 *     no mediated call — an unmediated commit bounded by the ring's
 *     remaining (max_capacity - capacity) entry footprint.  Not
 *     interposed (no stable public prototype; libsystem reaches the
 *     kernel path directly): documented boundary, like vm_read_list.
 *   - pthread stack teardown: __bsdthread_terminate releases a dying
 *     thread's stack inside the kernel — no userspace call exists to
 *     interpose, and pthread_join skips __pthread_deallocate for a
 *     kernel-released stack.  The tracked extent stays counted until the
 *     address is recycled, so thread churn leaves a bounded phantom pool
 *     (peak concurrent stacks x stack size, ~576 KiB each) instead of a
 *     monotonic leak: every successful new mapping performs an ordered
 *     sub->add reconcile — the kernel only places a range where nothing
 *     is mapped, so extents it still holds there are stale and released
 *     before the new span is added.  Empirically the residual is flat
 *     across repeated create/join generations.
 *   - fork(): the child inherits the private map with no hooked call;
 *     the guard is per-process and the supervisor re-injects at spawn.
 *
 * Cost bound (N5): every extent op is an O(n) linear scan with
 * n <= GUARD_MAX_EXTENTS (8192) — a bounded ~8K-entry sweep per mediated
 * call — and the pending drain adds O(k) for k queued ops where k is
 * bounded by the ring depth (16384) and in practice by the thread count.
 * Acceptable for the worker's allocation profile; no heap, no syscalls.
 *
 * Environment contract — TOBKIRI_WASM_GUARD_CAP_BYTES:
 *   absent    -> counting-only mode: g_used still tracks every counted
 *                commit (tobkiri_wasm_guard_used_bytes) but no cap is
 *                enforced.  The supervisor injects the variable only
 *                when it configured a cap, so an absent value is a
 *                legitimate no-cap launch, not a failure.
 *   malformed -> fail closed: cap 0, every counted commit refused.
 *   valid     -> lifetime committed cap in bytes (<= 16 GiB).
 *
 * One refusal applies in every mode including counting-only: a single
 * commit span larger than GUARD_MAX_CAP can never fit under any
 * expressible cap, and its delta would overflow the signed balance
 * arithmetic (the demonstrated g_used wipe), so it is refused outright
 * rather than counted loosely.
 *
 * tobkiri_wasm_guard_arm_headroom(bytes): installs cap = used + bytes,
 *   never looser than an existing cap.  bytes == 0 arms to exactly the
 *   bytes in use — the zero-growth bound.  Returns the installed cap; a
 *   return of 0 means the request exceeded GUARD_MAX_CAP and the previous
 *   cap stands (callers treat 0 as refusal, which fails closed).
 *
 * Honest boundary — this is NOT a kernel physical-footprint limit.  On
 * macOS 15 an unentitled process has none: setrlimit(RLIMIT_AS/DATA/RSS) is
 * EINVAL at every value, memorystatus_control and
 * task_set_phys_footprint_limit require com.apple.private.memorystatus,
 * ledger limits are debug-only, posix_spawnattr_setjetsam is an iOS stub
 * and proc_policy memory flavours are ENOTSUP (all verified on the target
 * OS).  The residual channels this library cannot see — a raw syscall()
 * or unlisted Mach trap variant that bypasses the interposed entry points
 * (none are issued by the sealed interpreter/libwasmtime/Pulley-guest
 * stack), the unmediated entry points listed above, demand-faulted
 * shared-cache pages and kernel-side page-table accounting — remain under
 * the supervisor's sampled RSS kill and the worker's post-completion
 * ru_maxrss rejection.  The production kernel boundary stays the Linux
 * cgroup v2 controller or a PackVM machine boundary.
 */

#include <errno.h>
#include <mach/host_priv.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach/mach_vm.h>
#include <mach/vm_inherit.h>
#include <mach/vm_map.h>
#include <mach/vm_region.h>
#include <mach/vm_statistics.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define GUARD_MAX_EXTENTS 8192
#define GUARD_PENDING_MAX 16384 /* power of two */
#define GUARD_CAP_ENV "TOBKIRI_WASM_GUARD_CAP_BYTES"
#define GUARD_MAX_CAP ((int64_t)16 * 1024 * 1024 * 1024)
#define GUARD_OP_ADD 1
#define GUARD_OP_SUB 2
/* Wall-time bound for the lock-owner spin (see guard_lock_spin): a
 * holder that has not released within this window is presumed dead
 * mid-section.  Seconds-scale so a merely descheduled holder — whose
 * critical section is microseconds long — can never trip it. */
#define GUARD_SPIN_DEADLINE_NS ((uint64_t)10 * 1000 * 1000 * 1000)

/* A committed extent: every byte of [base, base+len) is counted committed.
 * The table is kept sorted by base, disjoint and non-adjacent, so each
 * committed byte is attributable to exactly one entry.  `gen` is the
 * operation generation that last added to the extent (the maximum over a
 * merge): a deferred decommit may only release extent bytes stamped with
 * a generation <= its own, so a stale SUB can never wipe an extent that
 * recorded a commit newer than the SUB's own kernel operation. */
typedef struct {
    uintptr_t base;
    size_t len;
    uint64_t gen;
} guard_extent_t;

static guard_extent_t g_extents[GUARD_MAX_EXTENTS];
static size_t g_nextents;
/* The table lock is an owner-encoded word: 0 = free, otherwise the
 * holding thread's TPIDRRO-derived id.  Encoding ownership IN the lock
 * word is what lets a contended hook know — atomically, with no TLS,
 * no window — whether IT is the holder (a signal handler that
 * interrupted a locked frame) and therefore must not spin. */
static _Atomic uintptr_t g_lock_word;
static _Atomic int64_t g_used;      /* seeded by a vm_region sweep at load */
static _Atomic int64_t g_cap = -1;  /* -1: counting-only mode, no cap */
static _Atomic bool g_stale;        /* table may hold phantom extents */
/* Fail-closed latch: set when a lock holder vanished mid-section (the
 * bounded spin below exhausted).  The extent table can then no longer
 * be trusted — a section died halfway — so guard_reserve refuses every
 * further counted commit rather than accounting against a corrupt
 * table.  Releases and forced reserves still run. */
static _Atomic bool g_dead;
/* Monotonic operation sequence.  Commit-class ops stamp a generation
 * AFTER their commit syscall returns; decommit-class ops stamp BEFORE
 * issuing the decommit syscall (the asymmetric ordering the header's
 * Deferred-SUB section requires: a SUB can then never carry a
 * generation newer than a commit whose kernel operation postdates it).
 * Extent entries and deferred ops carry it; it is what lets a
 * late-applying SUB recognise extents that recorded commits newer than
 * itself. */
static _Atomic uint64_t g_opseq;

/* Page granularity the kernel actually commits at — seeded for a 16 KiB
 * page so pre-constructor hooks normalize with the conservative arm64
 * envelope (this TU's TPIDRRO_EL0 asm makes it arm64-only, and arm64
 * macOS commits at 16 KiB: a smaller seed would under-round and
 * under-charge early commits), then set to the real page size by the
 * constructor. */
static uintptr_t g_page_mask = 16383;

__attribute__((visibility("default")))
const uint32_t tobkiri_wasm_guard_marker = 0x54424731u;

__attribute__((visibility("default")))
uint64_t tobkiri_wasm_guard_arm_headroom(uint64_t bytes) {
    if (bytes > (uint64_t)GUARD_MAX_CAP) return 0;
    int64_t cur = atomic_load_explicit(&g_used, memory_order_relaxed);
    /* The reserve/release paths below keep g_used >= 0, so a negative
     * balance is corrupt state: arm against zero rather than letting
     * used + bytes stay negative — a negative installed cap reads as
     * counting-only mode and would permanently disable the cap. */
    if (cur < 0) cur = 0;
    int64_t cap_new;
    if (__builtin_add_overflow(cur, (int64_t)bytes, &cap_new))
        cap_new = GUARD_MAX_CAP;
    int64_t existing = atomic_load_explicit(&g_cap, memory_order_relaxed);
    if (existing >= 0 && cap_new > existing) cap_new = existing;
    atomic_store_explicit(&g_cap, cap_new, memory_order_relaxed);
    return (uint64_t)cap_new;
}

__attribute__((visibility("default")))
uint64_t tobkiri_wasm_guard_used_bytes(void) {
    return (uint64_t)atomic_load_explicit(&g_used, memory_order_relaxed);
}



/* Region-table mutations take the lock only when it is free: the hooks are
 * re-enterable from signal and allocator contexts where blocking could
 * deadlock.  A contended update is queued in the pending ring (below) so
 * it is applied by the next lock holder instead of dropped; only a full
 * ring falls back to conservative counting (see the header). */
/* The owning thread's identity for the lock word.  pthread_self() is NOT
 * usable here: on macOS 15 it runs a thread-structure integrity check
 * that aborts when invoked during early libsystem bootstrap (verified:
 * a hooked mach_vm_* call from _os_alloc_slow died inside pthread_self
 * with "PThread Corruption"), and __builtin_thread_pointer() does not
 * map to the thread register on Darwin arm64 (verified: it returns a
 * small non-unique constant).  TPIDRRO_EL0 is what pthread_self itself
 * reads — a per-thread pointer with the low bits used as flags — read
 * directly with one instruction: no checks, no allocation, no syscall,
 * safe in every context the hooks run in, including _os_alloc re-entry.
 * The low nibble is masked off: libpthread parks runtime flags there,
 * so a thread whose flag bits change between taking and re-checking the
 * lock would otherwise fail to recognise its own ownership and spin on
 * itself to deadlock — masking keeps the identity stable for the
 * thread's lifetime (aligned struct pointers differ in higher bits, so
 * uniqueness is preserved).  Bit 0 is then forced set so a pre-TLS
 * bootstrap thread (register still 0) never collides with the free
 * value; two pre-TLS threads both read as 1 — each treats the other's
 * hold as self-hold and takes the stale fallback, which is conservative
 * and bootstrap-only. */
static uintptr_t guard_thread_id(void) {
    uintptr_t t;
    __asm__ volatile("mrs %0, TPIDRRO_EL0" : "=r"(t));
    return (t & ~(uintptr_t)0xF) | 1;
}

static bool guard_try_lock(void) {
    uintptr_t expect = 0;
    return atomic_compare_exchange_strong_explicit(
        &g_lock_word, &expect, guard_thread_id(),
        memory_order_acquire, memory_order_relaxed);
}

static void guard_mark_stale(void);

/* mach_absolute_time ticks in GUARD_SPIN_DEADLINE_NS of wall time.
 * mach_timebase_info is a bare trap returning a process-invariant
 * ratio; the conversion is cached after the first call. */
static uint64_t guard_spin_deadline_mach(void) {
    static mach_timebase_info_data_t s_tb;
    static _Atomic uint32_t s_tb_ready;
    if (!atomic_load_explicit(&s_tb_ready, memory_order_acquire)) {
        (void)mach_timebase_info(&s_tb);
        atomic_store_explicit(&s_tb_ready, 1, memory_order_release);
    }
    return (uint64_t)((__uint128_t)GUARD_SPIN_DEADLINE_NS * s_tb.denom /
                      (s_tb.numer ? s_tb.numer : 1));
}

/* Blocking acquire used ONLY by the ring-full fallback.  Returns false
 * the moment THIS thread is seen to hold the lock — the word itself
 * carries the owner id, so a signal handler interrupting a locked frame
 * sees its own id and takes the stale fallback instead of spinning on
 * itself to deadlock — and otherwise spins until the lock is free.
 * The spin IS bounded: an unbounded spin wedges forever if the holder
 * died mid-section (faulted, killed thread), so after
 * GUARD_SPIN_DEADLINE_NS of wall time the guard is poisoned (g_dead)
 * and every later counted commit is refused — fail closed rather than
 * hang.  The bound is deliberately generous: a measured small bound
 * (2^24 CAS attempts ~ 100 ms under a 24-thread storm) expired while a
 * legitimately descheduled holder still owned the word, so the
 * deadline is seconds-scale — far beyond any preemption of the
 * bounded critical section (one fast syscall plus an O(n) table op) —
 * making a false timeout a practical impossibility while a dead holder
 * can never wedge a spinner indefinitely.  sched_yield is a bare syscall — no
 * userspace state, no locks — safe in the same contexts the hooks' own
 * mach traps are; mach_absolute_time reads the commpage, no trap. */
static bool guard_lock_spin(void) {
    const uintptr_t self = guard_thread_id();
    const uint64_t deadline =
        mach_absolute_time() + guard_spin_deadline_mach();
    for (;;) {
        uintptr_t expect = 0;
        if (atomic_compare_exchange_weak_explicit(
                &g_lock_word, &expect, self,
                memory_order_acquire, memory_order_relaxed))
            return true;
        if (expect == self) return false;
        if (atomic_load_explicit(&g_dead, memory_order_relaxed))
            return false;
        if ((int64_t)(mach_absolute_time() - deadline) >= 0) {
            /* Holder presumed dead mid-section: poison so later commits
             * fail closed instead of trusting a half-mutated table. */
            atomic_store_explicit(&g_dead, true, memory_order_release);
            guard_mark_stale();
            return false;
        }
        sched_yield();
    }
}

static uint64_t guard_seq_next(void) {
    return atomic_fetch_add_explicit(&g_opseq, 1, memory_order_relaxed) + 1;
}

static void guard_mark_stale(void) {
    atomic_store_explicit(&g_stale, true, memory_order_release);
}
static bool guard_is_stale(void) {
    return atomic_load_explicit(&g_stale, memory_order_acquire);
}

/* Diagnostics for the supervisor's test harness: pending-op backlog
 * (low 32 bits), extent count (bits 32-61) and the stale flag (bit 63).
 * Not part of the security boundary. */
typedef struct {
    _Atomic uint64_t seq;   /* ticket+1 once the slot is populated */
    uintptr_t base;
    uintptr_t end;
    int64_t reserved;       /* g_used bytes this op already holds (ADD) */
    /* Operation generation — stamped BEFORE the decommit syscall for
     * SUB ops, AFTER the commit syscall returns for ADD ops (the
     * asymmetric ordering that makes a stale SUB unable to release a
     * live extent). */
    uint64_t gen;
    int op;                 /* GUARD_OP_ADD / GUARD_OP_SUB */
} guard_pending_t;

static guard_pending_t g_pending[GUARD_PENDING_MAX];
static _Atomic uint64_t g_pending_head; /* next ticket a producer claims */
static _Atomic uint64_t g_pending_tail; /* next ticket the holder drains */

static bool guard_lock_acquire(void);
static void guard_unlock(void);

__attribute__((visibility("default")))
uint64_t tobkiri_wasm_guard_debug_state(void) {
    uint64_t h = atomic_load_explicit(&g_pending_head, memory_order_relaxed);
    uint64_t t = atomic_load_explicit(&g_pending_tail, memory_order_relaxed);
    uint64_t depth = h - t;
    if (depth > GUARD_PENDING_MAX) depth = GUARD_PENDING_MAX;
    return depth | (guard_is_stale() ? (1ull << 63) : 0) |
           ((uint64_t)g_nextents << 32);
}

static int64_t guard_cap(void) {
    return atomic_load_explicit(&g_cap, memory_order_relaxed);
}

/* Count `delta` committed bytes; roll back and fail past the cap.  The
 * counter tracks commits even in counting-only mode (cap < 0).  Once
 * the guard is poisoned (a lock holder died mid-section) every counted
 * commit is refused — the table can no longer be trusted, so failing
 * closed is the only safe direction.
 *
 * The delta bound is part of the accounting invariant.  A delta larger
 * than GUARD_MAX_CAP — the largest cap the guard can express — can
 * never be admitted under any armed cap, and a delta near INT64_MAX
 * (produced by the guard_span_len clamp for a giant request) wraps the
 * signed balance arithmetic: prev + delta overflows negative, the cap
 * check passes, and the error-path release then wipes g_used — the
 * demonstrated repeatable cap bypass.  Oversized deltas are refused
 * before they touch the counter, in every mode; a negative delta never
 * legitimately reaches reserve and would silently uncount.  A wrapped
 * or corrupt (negative) balance is refused the same way: fail closed. */
static bool guard_reserve(int64_t delta) {
    if (atomic_load_explicit(&g_dead, memory_order_acquire)) return false;
    if (delta < 0 || delta > GUARD_MAX_CAP) return false;
    int64_t prev = atomic_fetch_add_explicit(&g_used, delta, memory_order_relaxed);
    int64_t c = guard_cap();
    int64_t total;
    const bool ovf = __builtin_add_overflow(prev, delta, &total);
    if (!ovf && prev >= 0 && (c < 0 || total <= c)) return true;
    /* Refused: roll back exactly — subtracting delta restores prev no
     * matter how the add wrapped. */
    atomic_fetch_sub_explicit(&g_used, delta, memory_order_relaxed);
    return false;
}
/* Charge a commit that already happened in the kernel and therefore
 * cannot be refused (mlockall sweep, vm_read reconciliation): count past
 * the cap so future commits are refused sooner — never dropped.  The
 * counter must never WRAP either: a wrapped negative balance
 * under-counts every later commit, so the add saturates at INT64_MAX
 * instead (and lifts a corrupt negative balance the same way).  The
 * CAS re-reads the balance on contention so a racing update is never
 * lost. */
static void guard_reserve_forced(int64_t delta) {
    if (delta <= 0) return;
    int64_t cur = atomic_load_explicit(&g_used, memory_order_relaxed);
    for (;;) {
        int64_t next;
        if (__builtin_add_overflow(cur, delta, &next) || next < 0)
            next = INT64_MAX;
        if (atomic_compare_exchange_weak_explicit(
                &g_used, &cur, next,
                memory_order_relaxed, memory_order_relaxed))
            return;
    }
}
/* Release committed bytes with a hard floor at zero and NO arithmetic
 * that can overflow: an over-release — or a delta near INT64_MAX —
 * must never drive the balance negative (a negative balance
 * under-counts later commits and silently uncaps them), and must never
 * be repaired by adding delta - prev, which overflows on a wrapped
 * balance and was the demonstrated wipe of every tracked byte to
 * exactly 0.  The CAS re-reads the balance on contention, so a racing
 * reserve is never lost, and it only ever writes cur - delta or the 0
 * floor — a positive balance can never be zeroed out from under a
 * live charge. */
static void guard_release(int64_t delta) {
    if (delta <= 0) return;
    int64_t cur = atomic_load_explicit(&g_used, memory_order_relaxed);
    for (;;) {
        int64_t next = cur > delta ? cur - delta : 0;
        if (atomic_compare_exchange_weak_explicit(
                &g_used, &cur, next,
                memory_order_relaxed, memory_order_relaxed))
            return;
    }
}

static bool usable_prot(int prot) {
    return (prot & (PROT_READ | PROT_WRITE | PROT_EXEC)) != 0;
}

/* Only anonymous writable-capable mappings commit at map time: PROT_NONE
 * reserves address space only and a file-backed fd is not an anonymous
 * commit (page cache is attributed elsewhere).  MAP_SHARED anonymous is
 * still charged: its pages are real committed bytes in this process —
 * Python's mmap.mmap(-1, len) maps MAP_SHARED|MAP_ANON and must not slip
 * the cap (charging the whole span is conservative if pages stay
 * untouched). */
static bool chargeable_mmap(int prot, int flags, int fd) {
    if (!usable_prot(prot)) return false;
    if (fd >= 0 && !(flags & MAP_ANON)) return false;
    return true;
}

/* ---- page-envelope normalization (see header: N2) ---- */

static uintptr_t guard_page_down(uintptr_t a) {
    return a & ~g_page_mask;
}
static uintptr_t guard_page_up(uintptr_t a) {
    uintptr_t m = g_page_mask;
    return a > UINTPTR_MAX - m ? UINTPTR_MAX : (a + m) & ~m;
}
/* Kernel envelope of the range [base, base+len): base rounded down, end
 * rounded up, saturating on wrap so the charge is a huge positive reserve
 * the cap refuses — never a wrap to a negative delta. */
static uintptr_t guard_range_end(uintptr_t base, size_t len) {
    uintptr_t end = base + len;
    return guard_page_up(end < base ? UINTPTR_MAX : end);
}
static uintptr_t guard_round_len(size_t len) {
    return guard_page_up((uintptr_t)len);
}
static int64_t guard_span_len(uintptr_t base, uintptr_t end) {
    uint64_t span = (uint64_t)(end - base);
    return span > (uint64_t)INT64_MAX ? INT64_MAX : (int64_t)span;
}

/* ---- extent table; callers hold the lock ---- */

/* Committed bytes of [base,end) already covered by tracked extents. */
static int64_t guard_covered(uintptr_t base, uintptr_t end) {
    int64_t n = 0;
    for (size_t i = 0; i < g_nextents; i++) {
        guard_extent_t *e = &g_extents[i];
        uintptr_t ee = e->base + e->len;
        uintptr_t is = base > e->base ? base : e->base;
        uintptr_t ie = end < ee ? end : ee;
        if (is < ie) n += (int64_t)(ie - is);
    }
    return n;
}

/* Merge [base,end) into the extent set (coalescing overlap/adjacency).
 * The merged entry's generation is the maximum of the parts: a later SUB
 * may only release bytes that predate it.  Returns false only when the
 * table is full and the span merged with nothing — the caller keeps the
 * reserve and the span stays fully chargeable later, which can only
 * over-count. */
static bool guard_extent_add(uintptr_t base, uintptr_t end, uint64_t gen) {
    if (base >= end) return true;
    size_t n = g_nextents;
    size_t lo = 0;
    while (lo < n && g_extents[lo].base + g_extents[lo].len < base) lo++;
    size_t hi = lo;
    uintptr_t nb = base, ne = end;
    while (hi < n && g_extents[hi].base <= end) {
        if (g_extents[hi].base < nb) nb = g_extents[hi].base;
        uintptr_t ee = g_extents[hi].base + g_extents[hi].len;
        if (ee > ne) ne = ee;
        if (g_extents[hi].gen > gen) gen = g_extents[hi].gen;
        hi++;
    }
    if (hi == lo && n >= GUARD_MAX_EXTENTS) return false;
    memmove(&g_extents[lo + 1], &g_extents[hi],
            (n - hi) * sizeof(g_extents[0]));
    g_extents[lo] = (guard_extent_t){nb, (size_t)(ne - nb), gen};
    g_nextents = n - (hi - lo) + 1;
    return true;
}

/* Drop [base,end) from the extent set — clipping heads, splitting
 * middles — and return exactly the committed bytes removed, which is all
 * the caller may release.  `max_gen` bounds which extents may be
 * removed: an extent stamped with a generation NEWER than the caller's
 * operation recorded a commit that postdates the caller's kernel
 * decommit, so those bytes are still live and are skipped — the stale
 * SUB can never under-count, only keep a bounded phantom (released by
 * the next real decommit or by the stale-extent reconcile on a later
 * placement). */
static int64_t guard_extent_sub(uintptr_t base, uintptr_t end,
                                uint64_t max_gen) {
    int64_t freed = 0;
    for (size_t i = 0; i < g_nextents; i++) {
        guard_extent_t *e = &g_extents[i];
        if (e->gen > max_gen) continue;
        uintptr_t ee = e->base + e->len;
        uintptr_t is = base > e->base ? base : e->base;
        uintptr_t ie = end < ee ? end : ee;
        if (is >= ie) continue;
        freed += (int64_t)(ie - is);
        if (is <= e->base && ie >= ee) {           /* fully covered */
            memmove(e, e + 1, (g_nextents - i - 1) * sizeof(*e));
            g_nextents--;
            i--;
        } else if (ie >= ee) {                     /* tail committed off */
            e->len = (size_t)(is - e->base);
        } else if (is <= e->base) {                /* head committed off */
            e->base = ie;
            e->len = (size_t)(ee - ie);
        } else {                                   /* middle: split */
            if (g_nextents < GUARD_MAX_EXTENTS) {
                memmove(e + 2, e + 1, (g_nextents - i - 1) * sizeof(*e));
                e[1] = (guard_extent_t){ie, (size_t)(ee - ie), e->gen};
                g_nextents++;
                e->len = (size_t)(is - e->base);
                i++;
            } else {
                /* No slot for the tail: the head stays tracked and the
                 * tail's bytes stay counted in g_used but untracked —
                 * a permanent over-count, never an under-count. */
                e->len = (size_t)(is - e->base);
            }
        }
    }
    return freed;
}

/* ---- deferred-op ring (see header: lock contention) ----
 *
 * A bounded MPSC ring of completed operations waiting for a lock holder.
 * Producers claim contiguous tickets with a CAS on g_pending_head only
 * while head-tail < GUARD_PENDING_MAX (so a granted ticket is always
 * written and the drain can never stall on a burned slot), publish the
 * slot with a release store on seq = ticket+1, then prod the lock once
 * more so a promptly freed lock drains immediately.  The consumer is the
 * lock holder: it drains in ticket order, stopping at the first slot a
 * producer claimed but has not yet published — that stall self-heals when
 * the producer resumes and the next holder drains.
 */
static bool guard_pending_enqueue(int op, uintptr_t base, uintptr_t end,
                                  int64_t reserved, uint64_t gen) {
    for (int tries = 0; tries < 64; tries++) {
        uint64_t h = atomic_load_explicit(&g_pending_head, memory_order_relaxed);
        uint64_t t = atomic_load_explicit(&g_pending_tail, memory_order_acquire);
        if (h - t >= (uint64_t)GUARD_PENDING_MAX) {
            return false; /* full */
        }
        if (atomic_compare_exchange_weak_explicit(
                &g_pending_head, &h, h + 1, memory_order_relaxed,
                memory_order_relaxed)) {
            guard_pending_t *s = &g_pending[h & (GUARD_PENDING_MAX - 1)];
            s->base = base;
            s->end = end;
            s->reserved = reserved;
            s->gen = gen;
            s->op = op;
            atomic_store_explicit(&s->seq, h + 1, memory_order_release);
            return true;
        }
    }
    return false; /* persistently contended — caller falls back */
}

/* Drain every published op in ticket order.  Callers hold the lock.
 *
 * An ADD carries the reserve its hook already took (the whole span: a
 * contended commit cannot see the table before its syscall).  The drain
 * reconciles that reserve against the table it actually merges into —
 * bytes an earlier ticket already recorded are released back, so an
 * overlapping contended commit can never leak its double-charged share.
 * When the table is stale coverage is untrustworthy, so the full reserve
 * stands: over-count, never under-count. */
/* Apply one completed op against the table.  Callers hold the lock. */
static void guard_apply_sub(uintptr_t base, uintptr_t end, uint64_t gen) {
    guard_release(guard_extent_sub(base, end, gen));
}
static void guard_apply_add(uintptr_t base, uintptr_t end, int64_t reserved,
                            uint64_t gen) {
    int64_t span = guard_span_len(base, end);
    int64_t actual = span;
    if (!guard_is_stale()) {
        int64_t cov = guard_covered(base, end);
        actual = cov < span ? span - cov : 0;
    }
    if (reserved > actual) {
        guard_release(reserved - actual);
    } else if (reserved < actual) {
        guard_reserve_forced(actual - reserved);
    }
    (void)guard_extent_add(base, end, gen);
}

static void guard_pending_drain(void) {
    for (;;) {
        uint64_t t = atomic_load_explicit(&g_pending_tail, memory_order_relaxed);
        guard_pending_t *s = &g_pending[t & (GUARD_PENDING_MAX - 1)];
        if (atomic_load_explicit(&s->seq, memory_order_acquire) != t + 1)
            break;
        uintptr_t base = s->base, end = s->end;
        int64_t reserved = s->reserved;
        uint64_t gen = s->gen;
        int op = s->op;
        /* Free the slot before applying: producers may reuse it as soon
         * as tail+MAX tickets are claimed. */
        atomic_store_explicit(&g_pending_tail, t + 1, memory_order_release);
        if (op == GUARD_OP_SUB) {
            guard_apply_sub(base, end, gen);
        } else {
            guard_apply_add(base, end, reserved, gen);
        }
    }
}

/* Try-lock that immediately drains anything already completed: every
 * holder sees the newest reachable table before touching it, so a pending
 * decommit cannot discount a commit whose kernel order it precedes. */
static bool guard_lock_acquire(void) {
    if (!guard_try_lock()) return false;
    guard_pending_drain();
    return true;
}
static void guard_unlock(void) {
    /* Catch ops that raced in while this hook worked. */
    guard_pending_drain();
    atomic_store_explicit(&g_lock_word, 0, memory_order_release);
}

/* Post-syscall deferred accounting: queue the op for the next holder,
 * then prod the lock once — if it is already free we drain our own op
 * immediately.  `gen` is the caller's operation generation — stamped
 * before the syscall for SUB-class ops, after it for ADD-class ops —
 * so a deferred SUB applies only to extents no newer than itself and a
 * reordered same-range commit can never be released under it.
 *
 * Ring exhaustion is not a latch: a producer that cannot claim a slot
 * degrades to synchronous accounting — it takes the lock by spinning
 * (safe: guard_lock_spin returns false at once if this thread is itself
 * the holder, so the only non-spin case is a signal handler that
 * interrupted a locked frame, which keeps the stale fallback), drains
 * the backlog itself and applies its own op.  Under sustained
 * contention the ring can therefore never stay full and g_used cannot
 * diverge permanently. */
static void guard_defer_add(uintptr_t base, uintptr_t end, int64_t reserved,
                            uint64_t gen) {
    if (!guard_pending_enqueue(GUARD_OP_ADD, base, end, reserved, gen)) {
        if (guard_lock_spin()) {
            guard_pending_drain();
            guard_apply_add(base, end, reserved, gen);
            guard_unlock();
            return;
        }
        guard_mark_stale();
    }
    if (guard_lock_acquire()) guard_unlock();
}
static void guard_defer_sub(uintptr_t base, uintptr_t end, uint64_t gen) {
    if (!guard_pending_enqueue(GUARD_OP_SUB, base, end, 0, gen)) {
        if (guard_lock_spin()) {
            guard_pending_drain();
            guard_apply_sub(base, end, gen);
            guard_unlock();
            return;
        }
        guard_mark_stale();
    }
    if (guard_lock_acquire()) guard_unlock();
}

/* Committed bytes a commit over [base,end) adds: the uncovered share when
 * the table is trustworthy, the whole span when the lock was contended or
 * the table went stale — never less than the true new commit. */
static int64_t guard_commit_charge(uintptr_t base, uintptr_t end, bool locked) {
    int64_t span = guard_span_len(base, end);
    if (!locked || guard_is_stale()) return span;
    int64_t covered = guard_covered(base, end);
    return covered < span ? span - covered : 0;
}

/* ---- interposed commit entry points ---- */

static void *guard_mmap(void *addr, size_t len, int prot, int flags, int fd,
                        off_t off) {
    const uintptr_t plen = guard_round_len(len);
    const int64_t charge = chargeable_mmap(prot, flags, fd)
                               ? guard_span_len(0, plen)
                               : 0;
    if (charge && !guard_reserve(charge)) {
        errno = ENOMEM;
        return MAP_FAILED;
    }
    /* Reconcile-SUB generation: stamped BEFORE the placement syscall so
     * it can never appear newer than a commit that lands after the
     * placement (asymmetric stamping, see the header). */
    const uint64_t sseq = guard_seq_next();
    void *p = mmap(addr, len, prot, flags, fd, off);
    if (p == MAP_FAILED) {
        if (charge) guard_release(charge);
        return p;
    }
    const uintptr_t b = guard_page_down((uintptr_t)p);
    const uintptr_t e = guard_range_end((uintptr_t)p, len);
    /* Same ordered sub->add reconcile as guard_place_account: a fixed map
     * may overwrite tracked extents, and a non-fixed placement can only
     * land where the kernel considers the range free — stale tracked
     * extents there are phantom and must be released first.  The ADD
     * generation stamps AFTER the kernel committed the mapping. */
    const uint64_t aseq = guard_seq_next();
    if (guard_lock_acquire()) {
        guard_release(guard_extent_sub(b, e, sseq));
        if (charge) (void)guard_extent_add(b, e, aseq);
        guard_unlock();
    } else {
        guard_defer_sub(b, e, sseq);
        if (charge) guard_defer_add(b, e, charge, aseq);
    }
    return p;
}

static int guard_mprotect(void *addr, size_t len, int prot) {
    /* XNU RETAINS the pages under a non-usable protection (verified:
     * PROT_NONE pages stay resident and recoverable through heavy
     * pressure) — it is not a decommit, so nothing is released; the
     * range stays counted until a real free (munmap/vm_deallocate). */
    if (!usable_prot(prot)) return mprotect(addr, len, prot);
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, len);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    int rc;
    if (delta > 0 && !guard_reserve(delta)) {
        errno = ENOMEM;
        rc = -1;
    } else {
        rc = mprotect(addr, len, prot);
        if (rc == 0) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return rc;
}

static int guard_madvise(void *addr, size_t len, int advice) {
    /* No madvise advice decommits on XNU: MADV_DONTNEED leaves the pages
     * resident and readable (verified: contents intact after 2 GiB of
     * pressure), and MADV_FREE only marks them reusable — the kernel may
     * keep them indefinitely.  Neither releases charge; the range stays
     * counted until a real free (munmap/vm_deallocate).  The hook stays
     * interposed purely to document that deliberation. */
    return madvise(addr, len, advice);
}

static int guard_posix_madvise(void *addr, size_t len, int advice) {
    /* POSIX_MADV_DONTNEED is the same retained-pages operation as
     * MADV_DONTNEED on XNU — not a decommit, releases nothing. */
    return posix_madvise(addr, len, advice);
}

static int guard_munmap(void *addr, size_t len) {
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, len);
    /* Decommit-class op: stamp the generation BEFORE the syscall runs
     * so it can never appear newer than a commit whose kernel operation
     * postdates this unmap (see the header's asymmetric stamping). */
    const uint64_t seq = guard_seq_next();
    const bool locked = guard_lock_acquire();
    /* The table is dropped only after the syscall succeeds; if munmap
     * fails the accounting stays (the stamp is simply skipped), and if
     * the lock was contended the free is deferred to the next holder
     * rather than silently lost. */
    int rc = munmap(addr, len);
    if (rc == 0) {
        if (locked) {
            guard_release(guard_extent_sub(b, e, seq));
        } else {
            guard_defer_sub(b, e, seq);
        }
    }
    if (locked) guard_unlock();
    return rc;
}

/* Post-call accounting shared by the allocate/map families: the region
 * [b,e) was just committed by the kernel.  Anything the table still has
 * in that range is STALE — the kernel would never hand out an address
 * that still overlaps a live mapping — so tracked extents there are
 * released first (sub), then the new span is added (add).  This ordered
 * sub->add reconcile also self-heals channels the kernel frees without
 * a userspace call (e.g. bsdthread_terminate releasing a thread stack):
 * the phantom extent is dropped as soon as the address is recycled.
 * Contended calls defer the same ordered pair, ticket order preserved.
 * `sseq` is the reconcile-SUB generation (stamped before the placement
 * syscall) and `aseq` the ADD generation (stamped after it) — the
 * header's asymmetric stamping.  A foreign-target placement happened
 * in another task's map: the reserve stands (conservative charge, like
 * vm_copy/vm_write) but the extent set — which describes THIS address
 * space only — is left alone; clipping it by a foreign range would
 * under-count. */
static void guard_place_account(uintptr_t b, uintptr_t e, bool fixed,
                                bool self, int64_t charge,
                                uint64_t sseq, uint64_t aseq) {
    (void)fixed;
    if (!self) return;
    if (guard_lock_acquire()) {
        guard_release(guard_extent_sub(b, e, sseq));
        if (charge) (void)guard_extent_add(b, e, aseq);
        guard_unlock();
    } else {
        guard_defer_sub(b, e, sseq);
        if (charge) guard_defer_add(b, e, charge, aseq);
    }
}

static kern_return_t guard_vm_allocate(vm_map_t target, vm_address_t *addr,
                                       vm_size_t size, int flags) {
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_span_len(0, plen);
    if (!guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = vm_allocate(target, addr, size, flags);
    if (kr != KERN_SUCCESS) {
        guard_release(charge);
        return kr;
    }
    /* Out-params are only dereferenced on success; a success reporting
     * no placement keeps its reserve — an unaccountable commit must
     * stay charged, never silently dropped. */
    const uintptr_t placed = (addr != NULL) ? (uintptr_t)*addr : 0;
    if (placed == 0) return kr;
    const uintptr_t b = guard_page_down(placed);
    const uintptr_t e = guard_range_end(placed, (size_t)size);
    guard_place_account(b, e, (flags & VM_FLAGS_ANYWHERE) == 0,
                        target == mach_task_self(), charge, sseq,
                        guard_seq_next());
    return kr;
}

static kern_return_t guard_mach_vm_allocate(vm_map_t target,
                                            mach_vm_address_t *addr,
                                            mach_vm_size_t size, int flags) {
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_span_len(0, plen);
    if (!guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = mach_vm_allocate(target, addr, size, flags);
    if (kr != KERN_SUCCESS) {
        guard_release(charge);
        return kr;
    }
    const uintptr_t placed = (addr != NULL) ? (uintptr_t)*addr : 0;
    if (placed == 0) return kr;
    const uintptr_t b = guard_page_down(placed);
    const uintptr_t e = guard_range_end(placed, (size_t)size);
    guard_place_account(b, e, (flags & VM_FLAGS_ANYWHERE) == 0,
                        target == mach_task_self(), charge, sseq,
                        guard_seq_next());
    return kr;
}

/* Charge a vm_map-family placement for a fresh usable anonymous entry:
 * a real memory object commits no new private bytes (the COW growth
 * hole for object-backed copy=TRUE maps is documented in the header),
 * and PROT_NONE only reserves address space.  An anonymous entry is
 * charged even with VM_INHERIT_SHARE — shared-anonymous pages commit
 * on fault exactly like MAP_SHARED|MAP_ANON mmap pages and must not
 * slip the cap (charging the whole span is conservative while they
 * stay untouched). */
static int64_t guard_vm_map_charge(vm_prot_t cur, mem_entry_name_port_t object,
                                   vm_inherit_t inheritance, uintptr_t plen) {
    (void)inheritance;
    if (!usable_prot((int)cur)) return 0;
    if (object != MACH_PORT_NULL) return 0;
    return guard_span_len(0, plen);
}

static kern_return_t guard_vm_map(vm_map_t target, vm_address_t *addr,
                                  vm_size_t size, vm_address_t mask, int flags,
                                  mem_entry_name_port_t object,
                                  vm_offset_t offset, boolean_t copy,
                                  vm_prot_t cur, vm_prot_t max,
                                  vm_inherit_t inheritance) {
    const bool fixed = (flags & VM_FLAGS_ANYWHERE) == 0;
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_vm_map_charge(cur, object, inheritance, plen);
    if (charge && !guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = vm_map(target, addr, size, mask, flags, object, offset,
                              copy, cur, max, inheritance);
    if (kr != KERN_SUCCESS) {
        if (charge) guard_release(charge);
        return kr;
    }
    const uintptr_t placed = (addr != NULL) ? (uintptr_t)*addr : 0;
    if (placed == 0) return kr;
    const uintptr_t b = guard_page_down(placed);
    const uintptr_t e = guard_range_end(placed, (size_t)size);
    guard_place_account(b, e, fixed, target == mach_task_self(), charge,
                        sseq, guard_seq_next());
    return kr;
}

static kern_return_t guard_mach_vm_map(vm_map_t target, mach_vm_address_t *addr,
                                       mach_vm_size_t size, mach_vm_offset_t mask,
                                       int flags, mem_entry_name_port_t object,
                                       memory_object_offset_t offset,
                                       boolean_t copy, vm_prot_t cur,
                                       vm_prot_t max, vm_inherit_t inheritance) {
    const bool fixed = (flags & VM_FLAGS_ANYWHERE) == 0;
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_vm_map_charge(cur, object, inheritance, plen);
    if (charge && !guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = mach_vm_map(target, addr, size, mask, flags, object,
                                   offset, copy, cur, max, inheritance);
    if (kr != KERN_SUCCESS) {
        if (charge) guard_release(charge);
        return kr;
    }
    const uintptr_t placed = (addr != NULL) ? (uintptr_t)*addr : 0;
    if (placed == 0) return kr;
    const uintptr_t b = guard_page_down(placed);
    const uintptr_t e = guard_range_end(placed, (size_t)size);
    guard_place_account(b, e, fixed, target == mach_task_self(), charge,
                        sseq, guard_seq_next());
    return kr;
}

static kern_return_t guard_vm_protect(vm_map_t target, vm_address_t addr,
                                      vm_size_t size, boolean_t set_max,
                                      vm_prot_t prot) {
    if (set_max) return vm_protect(target, addr, size, set_max, prot);
    /* A non-usable protection is NOT a decommit on XNU — the pages stay
     * resident and recoverable (verified) — so nothing is released for
     * any target; the range stays counted until a real free. */
    if (!usable_prot((int)prot))
        return vm_protect(target, addr, size, set_max, prot);
    /* Usable-prot commits charge this process even for a foreign target
     * (the accounting cannot attribute foreign commits — conservative,
     * like vm_copy/vm_write). */
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = vm_protect(target, addr, size, set_max, prot);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static kern_return_t guard_mach_vm_protect(vm_map_t target,
                                           mach_vm_address_t addr,
                                           mach_vm_size_t size,
                                           boolean_t set_max, vm_prot_t prot) {
    if (set_max) return mach_vm_protect(target, addr, size, set_max, prot);
    if (!usable_prot((int)prot))
        return mach_vm_protect(target, addr, size, set_max, prot);
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = mach_vm_protect(target, addr, size, set_max, prot);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

/* Deallocations on a FOREIGN target task release bytes in that task's
 * map, never ours: clipping our extent table by the foreign range would
 * under-count.  Only self-targeted decommits are accounted. */
static kern_return_t guard_vm_deallocate(vm_map_t target, vm_address_t addr,
                                         vm_size_t size) {
    if (target != mach_task_self())
        return vm_deallocate(target, addr, size);
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)size);
    /* Decommit-class op: generation stamped before the syscall. */
    const uint64_t seq = guard_seq_next();
    const bool locked = guard_lock_acquire();
    kern_return_t kr = vm_deallocate(target, addr, size);
    if (kr == KERN_SUCCESS) {
        if (locked) {
            guard_release(guard_extent_sub(b, e, seq));
        } else {
            guard_defer_sub(b, e, seq);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static kern_return_t guard_mach_vm_deallocate(vm_map_t target,
                                              mach_vm_address_t addr,
                                              mach_vm_size_t size) {
    if (target != mach_task_self())
        return mach_vm_deallocate(target, addr, size);
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)size);
    const uint64_t seq = guard_seq_next();
    const bool locked = guard_lock_acquire();
    kern_return_t kr = mach_vm_deallocate(target, addr, size);
    if (kr == KERN_SUCCESS) {
        if (locked) {
            guard_release(guard_extent_sub(b, e, seq));
        } else {
            guard_defer_sub(b, e, seq);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

/* Remap placements follow the same kernel-truth reconcile as
 * guard_place_account: the kernel only places the destination where the
 * range is free, so stale tracked extents there are phantom — sub first,
 * then add.  A foreign-target placement happened in another map: the
 * reserve stands (conservative charge, like vm_copy/vm_write) but the
 * extent set — which describes THIS address space only — is left alone;
 * clipping it by a foreign range would under-count.  `fixed` is
 * retained for signature parity with callers that report placement
 * style. */
static void guard_remap_account(uintptr_t b, uintptr_t e, bool fixed,
                                bool self, int64_t charge,
                                uint64_t sseq, uint64_t aseq) {
    (void)fixed;
    if (!self) return;
    if (guard_lock_acquire()) {
        guard_release(guard_extent_sub(b, e, sseq));
        (void)guard_extent_add(b, e, aseq);
        guard_unlock();
    } else {
        guard_defer_sub(b, e, sseq);
        guard_defer_add(b, e, charge, aseq);
    }
}

static kern_return_t guard_vm_remap(vm_map_t target, vm_address_t *dst,
                                    vm_size_t size, vm_address_t mask, int flags,
                                    vm_map_t src_task, vm_address_t src,
                                    boolean_t copy, vm_prot_t *cur,
                                    vm_prot_t *max, vm_inherit_t inheritance) {
    const bool fixed = (flags & VM_FLAGS_ANYWHERE) == 0;
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_span_len(0, plen);
    if (!guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = vm_remap(target, dst, size, mask, flags, src_task, src,
                                copy, cur, max, inheritance);
    if (kr != KERN_SUCCESS) {
        guard_release(charge);
        return kr;
    }
    const uintptr_t placed = (dst != NULL) ? (uintptr_t)*dst : 0;
    if (placed == 0) return kr;
    guard_remap_account(guard_page_down(placed),
                        guard_range_end(placed, (size_t)size),
                        fixed, target == mach_task_self(), charge,
                        sseq, guard_seq_next());
    return kr;
}

static kern_return_t guard_vm_remap_new(vm_map_t target, vm_address_t *dst,
                                        vm_size_t size, vm_address_t mask,
                                        int flags, vm_map_read_t src_task,
                                        vm_address_t src, boolean_t copy,
                                        vm_prot_t *cur, vm_prot_t *max,
                                        vm_inherit_t inheritance) {
    const bool fixed = (flags & VM_FLAGS_ANYWHERE) == 0;
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_span_len(0, plen);
    if (!guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = vm_remap_new(target, dst, size, mask, flags, src_task,
                                    src, copy, cur, max, inheritance);
    if (kr != KERN_SUCCESS) {
        guard_release(charge);
        return kr;
    }
    const uintptr_t placed = (dst != NULL) ? (uintptr_t)*dst : 0;
    if (placed == 0) return kr;
    guard_remap_account(guard_page_down(placed),
                        guard_range_end(placed, (size_t)size),
                        fixed, target == mach_task_self(), charge,
                        sseq, guard_seq_next());
    return kr;
}

static kern_return_t guard_mach_vm_remap(vm_map_t target,
                                         mach_vm_address_t *dst,
                                         mach_vm_size_t size,
                                         mach_vm_offset_t mask, int flags,
                                         vm_map_t src_task,
                                         mach_vm_address_t src, boolean_t copy,
                                         vm_prot_t *cur, vm_prot_t *max,
                                         vm_inherit_t inheritance) {
    const bool fixed = (flags & VM_FLAGS_ANYWHERE) == 0;
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_span_len(0, plen);
    if (!guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = mach_vm_remap(target, dst, size, mask, flags, src_task,
                                   src, copy, cur, max, inheritance);
    if (kr != KERN_SUCCESS) {
        guard_release(charge);
        return kr;
    }
    const uintptr_t placed = (dst != NULL) ? (uintptr_t)*dst : 0;
    if (placed == 0) return kr;
    guard_remap_account(guard_page_down(placed),
                        guard_range_end(placed, (size_t)size),
                        fixed, target == mach_task_self(), charge,
                        sseq, guard_seq_next());
    return kr;
}

static kern_return_t guard_mach_vm_remap_new(vm_map_t target,
                                             mach_vm_address_t *dst,
                                             mach_vm_size_t size,
                                             mach_vm_offset_t mask, int flags,
                                             vm_map_read_t src_task,
                                             mach_vm_address_t src,
                                             boolean_t copy, vm_prot_t *cur,
                                             vm_prot_t *max,
                                             vm_inherit_t inheritance) {
    const bool fixed = (flags & VM_FLAGS_ANYWHERE) == 0;
    const uintptr_t plen = guard_round_len((uintptr_t)size);
    const int64_t charge = guard_span_len(0, plen);
    if (!guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = mach_vm_remap_new(target, dst, size, mask, flags,
                                       src_task, src, copy, cur, max,
                                       inheritance);
    if (kr != KERN_SUCCESS) {
        guard_release(charge);
        return kr;
    }
    const uintptr_t placed = (dst != NULL) ? (uintptr_t)*dst : 0;
    if (placed == 0) return kr;
    guard_remap_account(guard_page_down(placed),
                        guard_range_end(placed, (size_t)size),
                        fixed, target == mach_task_self(), charge,
                        sseq, guard_seq_next());
    return kr;
}

/* Shared shape for calls that populate an existing caller range —
 * vm_copy/mach_vm_copy, vm_read_overwrite/mach_vm_read_overwrite and
 * vm_write/mach_vm_write all commit the uncovered share of the
 * destination envelope. */
static kern_return_t guard_vm_copy(vm_map_t target, vm_address_t src,
                                   vm_size_t size, vm_address_t dst) {
    const uintptr_t b = guard_page_down((uintptr_t)dst);
    const uintptr_t e = guard_range_end((uintptr_t)dst, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = vm_copy(target, src, size, dst);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static kern_return_t guard_mach_vm_copy(vm_map_t target,
                                        mach_vm_address_t src,
                                        mach_vm_size_t size,
                                        mach_vm_address_t dst) {
    const uintptr_t b = guard_page_down((uintptr_t)dst);
    const uintptr_t e = guard_range_end((uintptr_t)dst, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = mach_vm_copy(target, src, size, dst);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

/* vm_read_overwrite/mach_vm_read_overwrite/vm_write/mach_vm_write commit
 * the uncommitted pages of a caller-supplied destination range in the
 * target map — same charge shape as vm_copy. */
static kern_return_t guard_vm_read_overwrite(vm_map_read_t target,
                                             vm_address_t addr,
                                             vm_size_t size,
                                             vm_address_t data,
                                             vm_size_t *outsize) {
    const uintptr_t b = guard_page_down((uintptr_t)data);
    const uintptr_t e = guard_range_end((uintptr_t)data, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = vm_read_overwrite(target, addr, size, data, outsize);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static kern_return_t guard_mach_vm_read_overwrite(vm_map_read_t target,
                                                  mach_vm_address_t addr,
                                                  mach_vm_size_t size,
                                                  mach_vm_address_t data,
                                                  mach_vm_size_t *outsize) {
    const uintptr_t b = guard_page_down((uintptr_t)data);
    const uintptr_t e = guard_range_end((uintptr_t)data, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = mach_vm_read_overwrite(target, addr, size, data, outsize);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static kern_return_t guard_vm_write(vm_map_t target, vm_address_t addr,
                                    vm_offset_t data,
                                    mach_msg_type_number_t dataCnt) {
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)dataCnt);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = vm_write(target, addr, data, dataCnt);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static kern_return_t guard_mach_vm_write(vm_map_t target,
                                         mach_vm_address_t addr,
                                         vm_offset_t data,
                                         mach_msg_type_number_t dataCnt) {
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)dataCnt);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = mach_vm_write(target, addr, data, dataCnt);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

/* vm_read/mach_vm_read make the kernel allocate a fresh private region in
 * THIS map and return it out-of-line — a real commit the cap must gate.
 * The request size bounds the allocation, so the rounded size is reserved
 * up front and reconciled to the returned data size after the call; the
 * returned region is tracked so the paired vm_deallocate releases it. */
static void guard_read_account(int64_t reserved, uintptr_t data,
                               uintptr_t dcnt, uint64_t sseq) {
    if (data == 0 || dcnt == 0) {
        guard_release(reserved);
        return;
    }
    const uintptr_t b = guard_page_down(data);
    const uintptr_t e = guard_range_end(data, (size_t)dcnt);
    const int64_t actual = guard_span_len(b, e);
    const uint64_t aseq = guard_seq_next();
    if (actual < reserved) {
        guard_release(reserved - actual);
    } else if (actual > reserved) {
        guard_reserve_forced(actual - reserved);
    }
    if (guard_lock_acquire()) {
        /* Kernel placed a fresh region: stale tracked extents in its
         * range are phantom — sub first (pre-syscall generation), then
         * add (post-syscall generation). */
        guard_release(guard_extent_sub(b, e, sseq));
        (void)guard_extent_add(b, e, aseq);
        guard_unlock();
    } else {
        /* The reserve we still hold after the local reconcile is
         * `actual` — the drain re-reconciles it against coverage. */
        guard_defer_sub(b, e, sseq);
        guard_defer_add(b, e, actual, aseq);
    }
}

static kern_return_t guard_vm_read(vm_map_read_t target, vm_address_t addr,
                                   vm_size_t size, vm_offset_t *data,
                                   mach_msg_type_number_t *dataCnt) {
    const int64_t charge = guard_span_len(0, guard_round_len((uintptr_t)size));
    if (charge && !guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = vm_read(target, addr, size, data, dataCnt);
    /* Out-params are only dereferenced on success: a failed call leaves
     * them undefined and the reserve is simply released. */
    const uintptr_t d = (kr == KERN_SUCCESS && data != NULL)
                            ? (uintptr_t)*data : 0;
    const uintptr_t dc = (kr == KERN_SUCCESS && dataCnt != NULL)
                             ? (uintptr_t)*dataCnt : 0;
    guard_read_account(charge, d, dc, sseq);
    return kr;
}

static kern_return_t guard_mach_vm_read(vm_map_read_t target,
                                        mach_vm_address_t addr,
                                        mach_vm_size_t size, vm_offset_t *data,
                                        mach_msg_type_number_t *dataCnt) {
    const int64_t charge = guard_span_len(0, guard_round_len((uintptr_t)size));
    if (charge && !guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = mach_vm_read(target, addr, size, data, dataCnt);
    const uintptr_t d = (kr == KERN_SUCCESS && data != NULL)
                            ? (uintptr_t)*data : 0;
    const uintptr_t dc = (kr == KERN_SUCCESS && dataCnt != NULL)
                             ? (uintptr_t)*dataCnt : 0;
    guard_read_account(charge, d, dc, sseq);
    return kr;
}

/* Wiring faults the range in and pins it: charge its uncommitted share.
 * mlock/munlock POSIX wrappers commit/unwire the same way — munlock is
 * deliberately unhooked since unwiring does not decommit. */
static kern_return_t guard_vm_wire(host_priv_t host_priv, vm_map_t task,
                                   vm_address_t addr, vm_size_t size,
                                   vm_prot_t access) {
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = vm_wire(host_priv, task, addr, size, access);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static kern_return_t guard_mach_vm_wire(host_priv_t host_priv, vm_map_t task,
                                        mach_vm_address_t addr,
                                        mach_vm_size_t size, vm_prot_t access) {
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, (size_t)size);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    kern_return_t kr;
    if (delta > 0 && !guard_reserve(delta)) {
        kr = KERN_RESOURCE_SHORTAGE;
    } else {
        kr = mach_vm_wire(host_priv, task, addr, size, access);
        if (kr == KERN_SUCCESS) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return kr;
}

static int guard_mlock(const void *addr, size_t len) {
    const uintptr_t b = guard_page_down((uintptr_t)addr);
    const uintptr_t e = guard_range_end((uintptr_t)addr, len);
    const bool locked = guard_lock_acquire();
    int64_t delta = guard_commit_charge(b, e, locked);
    int rc;
    if (delta > 0 && !guard_reserve(delta)) {
        errno = ENOMEM;
        rc = -1;
    } else {
        rc = mlock(addr, len);
        if (rc == 0) {
            const uint64_t seq = guard_seq_next();
            if (locked) (void)guard_extent_add(b, e, seq);
            else guard_defer_add(b, e, delta, seq);
        } else if (delta > 0) {
            guard_release(delta);
        }
    }
    if (locked) guard_unlock();
    return rc;
}

/* mlockall(MCL_CURRENT) wires every usable private mapping already in the
 * map — a commit fact discovered only after the call, so it is charged
 * unconditionally (a forced reserve past the cap is correct: the bytes
 * exist, and the cap then refuses the NEXT commit).  MCL_FUTURE needs no
 * accounting: the future maps it wires are charged by the map hooks. */
static int guard_mlockall(int flags) {
    int rc = mlockall(flags);
    if (rc != 0 || (flags & MCL_CURRENT) == 0) return rc;
    mach_vm_address_t a = 0;
    for (;;) {
        mach_vm_size_t sz = 0;
        vm_region_basic_info_data_64_t info;
        mach_msg_type_number_t n = VM_REGION_BASIC_INFO_COUNT_64;
        mach_port_t obj = MACH_PORT_NULL;
        if (mach_vm_region(mach_task_self(), &a, &sz, VM_REGION_BASIC_INFO_64,
                           (vm_region_info_t)&info, &n,
                           &obj) != KERN_SUCCESS)
            break;
        uintptr_t b = (uintptr_t)a;
        if (a + sz <= a) break;
        a += sz;
        if (!usable_prot((int)info.protection) || info.shared || info.reserved)
            continue;
        uintptr_t e = guard_range_end(b, (size_t)sz);
        const bool locked = guard_lock_acquire();
        int64_t delta = guard_commit_charge(b, e, locked);
        const uint64_t seq = guard_seq_next();
        guard_reserve_forced(delta);
        if (locked) {
            (void)guard_extent_add(b, e, seq);
            guard_unlock();
        } else {
            guard_defer_add(b, e, delta, seq);
        }
    }
    return rc;
}

/* Deferred reclamation buffer: not declared in the public mach_vm.h, so
 * the MIG prototypes are replicated from the kernel-side implementation
 * (xnu osfmk/vm/vm_user.c: mach_vm_deferred_reclamation_buffer_allocate
 * is (task, address*, initial_capacity, max_capacity) — FOUR args; an
 * earlier revision of this file declared a phantom leading
 * sampling_period* that shifted len/max_len into the wrong registers)
 * and verified against the shipped libsystem_kernel stub on macOS 15.6:
 * the userspace stub reads exactly x0, x1, w2, w3 (the two entry counts
 * are marshalled with stp w2, w3 — no fourth argument register is
 * touched).  The buffer is wired kernel memory inside this map — a
 * real commit.  len/max_len (and the ring API's capacities) are ENTRY
 * COUNTS, not bytes: the kernel maps the buffer at its MAX footprint —
 * round_page(offsetof(entries) + 16 * max_len) = round_page(0x30 +
 * 16 * max_len) usable bytes at allocate
 * (vm_deferred_reclamation_buffer_allocate_internal →
 * vmdr_round_len_to_size → mach_vm_map_kernel; verified via
 * mach_vm_region: init=1021/max=8388605 maps a 128 MiB RW region).
 * The mapped size is immutable afterwards (vdrm_ring_size): resize
 * only republishes the vdrm_buffer_len watermark within that span —
 * KERN_NO_SPACE beyond it, and no bytes are unmapped on shrink —
 * and flush's bytes_reclaimed is the VA total of reclaimed VICTIM
 * regions, not a shrink of the buffer mapping.  So allocate alone
 * commits buffer bytes; resize/flush move none. */
extern kern_return_t mach_vm_deferred_reclamation_buffer_allocate(
    vm_map_t target_task, mach_vm_address_t *address,
    uint32_t len, uint32_t max_len);
extern kern_return_t mach_vm_deferred_reclamation_buffer_resize(
    vm_map_t target_task, uint32_t new_len, mach_vm_size_t *bytes_reclaimed);
extern kern_return_t mach_vm_deferred_reclamation_buffer_flush(
    vm_map_t target_task, uint32_t num_entries_to_reclaim,
    mach_vm_size_t *bytes_reclaimed);

/* Committed byte footprint of a reclamation buffer of `entries`
 * entries — round_page(0x30 + 16 * entries).  Shared by the MIG
 * buffer API and the userspace reclaim ring, which is a libsystem
 * wrapper over the same kernel buffer object. */
static uint64_t guard_reclaim_buffer_bytes(uint64_t entries) {
    return (uint64_t)guard_round_len((uintptr_t)(0x30 + 16 * entries));
}

static kern_return_t guard_vm_reclaim_allocate(
    vm_map_t target, mach_vm_address_t *address,
    uint32_t len, uint32_t max_len) {
    if (target != mach_task_self()) {
        /* A foreign-task buffer commits in that task's map: accounting it
         * here would charge phantom bytes. */
        return mach_vm_deferred_reclamation_buffer_allocate(
            target, address, len, max_len);
    }
    /* The kernel maps round_page(0x30 + 16*max_len) usable bytes at
     * allocate — charge and track the MAX-derived footprint, not the
     * entry-count len (charging len bytes under-counted the real
     * commit: the demonstrated 128 MiB map / 16 KiB charge bypass).
     * max(len, max_len) stays correct even if a kernel ever skipped
     * the max_len >= len check. */
    const uint64_t entries =
        len > max_len ? (uint64_t)len : (uint64_t)max_len;
    const uint64_t footprint = guard_reclaim_buffer_bytes(entries);
    const int64_t charge = (int64_t)footprint;
    if (charge && !guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    kern_return_t kr = mach_vm_deferred_reclamation_buffer_allocate(
        target, address, len, max_len);
    if (kr != KERN_SUCCESS) {
        if (charge) guard_release(charge);
        return kr;
    }
    /* Success reporting no placement keeps the reserve — an
     * unaccountable commit stays charged, never silently dropped. */
    const mach_vm_address_t placed =
        (address != NULL) ? *address : 0;
    if (placed == 0) return kr;
    const uintptr_t b = guard_page_down((uintptr_t)placed);
    const uintptr_t e = b + (uintptr_t)footprint;
    const uint64_t aseq = guard_seq_next();
    if (guard_lock_acquire()) {
        guard_release(guard_extent_sub(b, e, sseq));
        (void)guard_extent_add(b, e, aseq);
        guard_unlock();
    } else {
        guard_defer_sub(b, e, sseq);
        guard_defer_add(b, e, charge, aseq);
    }
    return kr;
}

static kern_return_t guard_vm_reclaim_resize(vm_map_t target, uint32_t new_len,
                                             mach_vm_size_t *bytes_reclaimed) {
    /* Pure watermark change: the backing mapping keeps its
     * allocate-time max_len footprint — vdrm_ring_size is immutable,
     * new_len only republishes vdrm_buffer_len (KERN_NO_SPACE beyond
     * the mapped span; verified: the region is unchanged after
     * shrink).  bytes_reclaimed is the VA total of reclaimed VICTIM
     * regions, not a buffer shrink.  No buffer bytes move, so there
     * is nothing to charge or release — interposed to document that
     * deliberation. */
    return mach_vm_deferred_reclamation_buffer_resize(
        target, new_len, bytes_reclaimed);
}

static kern_return_t guard_vm_reclaim_flush(vm_map_t target,
                                            uint32_t num_entries_to_reclaim,
                                            mach_vm_size_t *bytes_reclaimed) {
    /* bytes_reclaimed is the VA total of reclaimed VICTIM regions
     * (xnu reclaim_chunk), NOT a shrink of the buffer mapping — the
     * buffer's mapped span never shrinks, so releasing its extent
     * tail would drop charge on still-mapped bytes (an under-count).
     * The victims' own release is an in-kernel event no hook can
     * see; their extents stay counted (documented over-count). */
    return mach_vm_deferred_reclamation_buffer_flush(
        target, num_entries_to_reclaim, bytes_reclaimed);
}

/* macOS 15.4+ vm_reclaim userspace ring API.  The ring lives in wired
 * memory inside this map exactly like the MIG reclamation buffer — and
 * these entry points MUST be interposed separately because libsystem
 * calls the underlying mach_vm_deferred_reclamation_buffer_* routines
 * through intra-image direct branches that dyld interposition cannot
 * see (verified in the shipped libsystem_kernel disassembly).  No public
 * header declares them; prototypes are replicated from xnu's
 * osfmk/mach/vm_reclaim.h.  A ring's byte footprint for C entries is
 * round_page(0x30 + 16*C) — mach_vm_reclaim_round_capacity's own layout
 * arithmetic (guard_reclaim_buffer_bytes above).  The kernel maps the
 * buffer at the MAX capacity at allocate; resize is a pure watermark
 * update inside that immutable span. */
typedef struct mach_vm_reclaim_ring_s *guard_reclaim_ring_t;
typedef mach_error_t guard_reclaim_error_t;
/* err_vm|err_sub(1)|6 — VM_RECLAIM_RESOURCE_SHORTAGE, matching the value
 * libsystem itself returns for an exhausted ring. */
#define GUARD_VM_RECLAIM_RESOURCE_SHORTAGE ((guard_reclaim_error_t)0x20004006)
extern guard_reclaim_error_t mach_vm_reclaim_ring_allocate(
    guard_reclaim_ring_t *ring, uint32_t initial_capacity,
    uint32_t max_capacity);
extern guard_reclaim_error_t mach_vm_reclaim_ring_resize(
    guard_reclaim_ring_t ring, uint32_t capacity);

static guard_reclaim_error_t guard_vm_reclaim_ring_allocate(
    guard_reclaim_ring_t *ring, uint32_t initial_capacity,
    uint32_t max_capacity) {
    /* The kernel maps round_page(0x30 + 16*max_capacity) usable bytes
     * at allocate — charge and track the MAX-capacity footprint, not
     * initial_capacity's (charging the initial footprint under-counted
     * the real commit: 128 MiB mapped vs 16 KiB charged let the
     * process hold 353 MiB under a 256 MiB cap).  max(initial, max)
     * stays correct even if a kernel ever skipped the max >= initial
     * check. */
    const uint64_t entries = initial_capacity > max_capacity
                                 ? (uint64_t)initial_capacity
                                 : (uint64_t)max_capacity;
    const uint64_t footprint = guard_reclaim_buffer_bytes(entries);
    const int64_t charge = (int64_t)footprint;
    if (charge && !guard_reserve(charge))
        return GUARD_VM_RECLAIM_RESOURCE_SHORTAGE;
    const uint64_t sseq = guard_seq_next();
    guard_reclaim_error_t err =
        mach_vm_reclaim_ring_allocate(ring, initial_capacity, max_capacity);
    if (err != 0) {
        if (charge) guard_release(charge);
        return err;
    }
    const uintptr_t placed = (ring != NULL) ? (uintptr_t)*ring : 0;
    if (placed == 0) return err;
    const uintptr_t b = guard_page_down(placed);
    const uintptr_t e = b + (uintptr_t)footprint;
    const uint64_t aseq = guard_seq_next();
    if (guard_lock_acquire()) {
        guard_release(guard_extent_sub(b, e, sseq));
        (void)guard_extent_add(b, e, aseq);
        guard_unlock();
    } else {
        guard_defer_sub(b, e, sseq);
        guard_defer_add(b, e, charge, aseq);
    }
    return err;
}

static guard_reclaim_error_t guard_vm_reclaim_ring_resize(
    guard_reclaim_ring_t ring, uint32_t capacity) {
    /* Pure watermark change inside the immutable max-capacity mapping:
     * resize only republishes the usable entry count — the backing
     * region neither grows nor shrinks (verified: the 128 MiB region
     * is unchanged after shrink), so no extents are added or released
     * (releasing on shrink dropped charge on still-mapped bytes — an
     * under-count).  Interposed to document that deliberation. */
    return mach_vm_reclaim_ring_resize(ring, capacity);
}

typedef struct { const void *new_fn; const void *orig_fn; } guard_interpose_t;
__attribute__((used)) static const guard_interpose_t guard_interposers[]
    __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)guard_mmap, (const void *)mmap},
    {(const void *)guard_mprotect, (const void *)mprotect},
    {(const void *)guard_madvise, (const void *)madvise},
    {(const void *)guard_posix_madvise, (const void *)posix_madvise},
    {(const void *)guard_munmap, (const void *)munmap},
    {(const void *)guard_mlock, (const void *)mlock},
    {(const void *)guard_mlockall, (const void *)mlockall},
    {(const void *)guard_vm_allocate, (const void *)vm_allocate},
    {(const void *)guard_vm_map, (const void *)vm_map},
    {(const void *)guard_vm_protect, (const void *)vm_protect},
    {(const void *)guard_vm_deallocate, (const void *)vm_deallocate},
    {(const void *)guard_vm_remap, (const void *)vm_remap},
    {(const void *)guard_vm_remap_new, (const void *)vm_remap_new},
    {(const void *)guard_vm_copy, (const void *)vm_copy},
    {(const void *)guard_vm_wire, (const void *)vm_wire},
    {(const void *)guard_vm_read, (const void *)vm_read},
    {(const void *)guard_vm_read_overwrite, (const void *)vm_read_overwrite},
    {(const void *)guard_vm_write, (const void *)vm_write},
    {(const void *)guard_mach_vm_allocate, (const void *)mach_vm_allocate},
    {(const void *)guard_mach_vm_map, (const void *)mach_vm_map},
    {(const void *)guard_mach_vm_protect, (const void *)mach_vm_protect},
    {(const void *)guard_mach_vm_deallocate, (const void *)mach_vm_deallocate},
    {(const void *)guard_mach_vm_remap, (const void *)mach_vm_remap},
    {(const void *)guard_mach_vm_remap_new, (const void *)mach_vm_remap_new},
    {(const void *)guard_mach_vm_copy, (const void *)mach_vm_copy},
    {(const void *)guard_mach_vm_wire, (const void *)mach_vm_wire},
    {(const void *)guard_mach_vm_read, (const void *)mach_vm_read},
    {(const void *)guard_mach_vm_read_overwrite,
     (const void *)mach_vm_read_overwrite},
    {(const void *)guard_mach_vm_write, (const void *)mach_vm_write},
    {(const void *)guard_vm_reclaim_allocate,
     (const void *)mach_vm_deferred_reclamation_buffer_allocate},
    {(const void *)guard_vm_reclaim_resize,
     (const void *)mach_vm_deferred_reclamation_buffer_resize},
    {(const void *)guard_vm_reclaim_flush,
     (const void *)mach_vm_deferred_reclamation_buffer_flush},
    {(const void *)guard_vm_reclaim_ring_allocate,
     (const void *)mach_vm_reclaim_ring_allocate},
    {(const void *)guard_vm_reclaim_ring_resize,
     (const void *)mach_vm_reclaim_ring_resize},
};

/* Seed the counter AND the extent table with a deterministic committed
 * set: one vm_region sweep counting exactly the regions the hooks charge
 * — usable, non-reserved and ANONYMOUS.  The hooks charge a mapping at
 * map time only when it is anonymous (fd-backed maps commit nothing:
 * page cache is attributed elsewhere, and MAP_PRIVATE COW growth is an
 * in-kernel fault no hook can see), so the seed must apply the same
 * predicate or the baseline is inflated by every file-backed __TEXT /
 * __DATA_CONST region — VM_REGION_EXTENDED_INFO's external_pager flag
 * is precisely "backed by an external (file) pager".  Shared-anonymous
 * regions (external_pager == 0) stay charged, matching the mmap hook's
 * MAP_SHARED|MAP_ANON charge.  Counting with the hooks' own predicate
 * makes the baseline identical every launch and lets pre-injection
 * mappings release correctly on unmap.  If the sweep itself fails the
 * resident_size fallback keeps the cap conservative.
 * The env contract: absent = counting-only mode, malformed = cap 0. */
__attribute__((constructor)) static void guard_init(void) {
    g_page_mask = (uintptr_t)getpagesize() - 1;
    bool swept = false;
    mach_vm_address_t a = 0;
    for (;;) {
        mach_vm_size_t sz = 0;
        vm_region_basic_info_data_64_t info;
        mach_msg_type_number_t n = VM_REGION_BASIC_INFO_COUNT_64;
        mach_port_t obj = MACH_PORT_NULL;
        if (mach_vm_region(mach_task_self(), &a, &sz, VM_REGION_BASIC_INFO_64,
                           (vm_region_info_t)&info, &n,
                           &obj) != KERN_SUCCESS)
            break;
        swept = true;
        uintptr_t b = (uintptr_t)a;
        if (a + sz <= a) break;
        a += sz;
        if (!usable_prot((int)info.protection) || info.reserved)
            continue;
        /* Anonymous check: a second region query reports external_pager
         * for the same region (file/object-backed).  Failing closed —
         * charging when the query fails — only inflates the baseline. */
        vm_region_extended_info_data_t xinfo;
        mach_msg_type_number_t xn = VM_REGION_EXTENDED_INFO_COUNT;
        mach_port_t xobj = MACH_PORT_NULL;
        mach_vm_address_t xa = a - sz;
        mach_vm_size_t xsz = 0;
        if (mach_vm_region(mach_task_self(), &xa, &xsz,
                           VM_REGION_EXTENDED_INFO,
                           (vm_region_info_t)&xinfo, &xn,
                           &xobj) == KERN_SUCCESS &&
            xa == (mach_vm_address_t)b && xinfo.external_pager)
            continue;
        uintptr_t e = guard_range_end(b, (size_t)sz);
        if (guard_lock_acquire()) {
            int64_t delta = guard_commit_charge(b, e, true);
            guard_reserve_forced(delta);
            (void)guard_extent_add(b, e, 0);
            guard_unlock();
        } else {
            /* Constructor contention is not expected; still safe. */
            guard_reserve_forced(guard_span_len(b, e));
            guard_defer_add(b, e, guard_span_len(b, e), 0);
        }
    }
    if (!swept) {
        mach_msg_type_number_t count = TASK_BASIC_INFO_64_COUNT;
        task_basic_info_64_data_t info;
        if (task_info(mach_task_self(), TASK_BASIC_INFO_64,
                      (task_info_t)&info, &count) == KERN_SUCCESS &&
            info.resident_size > 0) {
            guard_reserve_forced((int64_t)info.resident_size);
        }
    }
    const char *raw = getenv(GUARD_CAP_ENV);
    if (raw == NULL) return;
    /* Strict decimal bytes — identical to the worker-side
     * _parse_guard_bytes: ASCII digits only, no leading zero, no sign,
     * no whitespace, within range.  Anything else installs a zero cap so
     * every counted commit is refused (fail closed). */
    int64_t parsed = 0;
    if (raw[0] != '\0' && raw[0] != '0') {
        uint64_t v = 0;
        bool ok = true;
        for (const char *p = raw; ok && *p != '\0'; p++) {
            if (*p < '0' || *p > '9') {
                ok = false;
            } else {
                v = v * 10 + (uint64_t)(*p - '0');
                if (v > (uint64_t)GUARD_MAX_CAP) ok = false;
            }
        }
        if (ok) parsed = (int64_t)v;
    }
    atomic_store_explicit(&g_cap, parsed, memory_order_relaxed);
}
