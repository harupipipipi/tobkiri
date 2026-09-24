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
 * Honest boundary — this is NOT a kernel physical-footprint limit.  On
 * macOS 15 an unentitled process has none: setrlimit(RLIMIT_AS/DATA/RSS) is
 * EINVAL at every value, memorystatus_control and
 * task_set_phys_footprint_limit require com.apple.private.memorystatus,
 * ledger limits are debug-only, posix_spawnattr_setjetsam is an iOS stub
 * and proc_policy memory flavours are ENOTSUP (all verified on the target
 * OS).  The residual channels this library cannot see — a raw syscall()
 * that bypasses the interposed libc/Mach entry points (none are issued by
 * the sealed interpreter/libwasmtime/Pulley-guest stack), demand-faulted
 * shared-cache pages and kernel-side page-table accounting — remain under
 * the supervisor's sampled RSS kill and the worker's post-completion
 * ru_maxrss rejection.  The production kernel boundary stays the Linux
 * cgroup v2 controller or a PackVM machine boundary.
 */

#include <errno.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define GUARD_MAX_REGIONS 8192
#define GUARD_CAP_ENV "TOBKIRI_WASM_GUARD_CAP_BYTES"
#define GUARD_MAX_CAP ((int64_t)16 * 1024 * 1024 * 1024)

typedef struct {
    uintptr_t base;
    size_t len;
    size_t committed; /* counted committed share of this region */
} guard_region_t;

static guard_region_t g_regions[GUARD_MAX_REGIONS];
static size_t g_nregions;
static atomic_flag g_lock = ATOMIC_FLAG_INIT;
static _Atomic int64_t g_used;      /* seeded with resident bytes at load */
static _Atomic int64_t g_cap = -1;  /* -1: cap not configured */

__attribute__((visibility("default")))
const uint32_t tobkiri_wasm_guard_marker = 0x54424731u;

__attribute__((visibility("default")))
uint64_t tobkiri_wasm_guard_arm_headroom(uint64_t bytes) {
    if (bytes == 0 || bytes > (uint64_t)GUARD_MAX_CAP) return 0;
    int64_t cur = atomic_load_explicit(&g_used, memory_order_relaxed);
    int64_t cap_new = cur + (int64_t)bytes;
    if (cap_new < cur) cap_new = GUARD_MAX_CAP;
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
 * deadlock, so a contended update falls back to conservative counting. */
static bool guard_try_lock(void) {
    return !atomic_flag_test_and_set_explicit(&g_lock, memory_order_acquire);
}
static void guard_unlock(void) {
    atomic_flag_clear_explicit(&g_lock, memory_order_release);
}

static int64_t guard_cap(void) {
    return atomic_load_explicit(&g_cap, memory_order_relaxed);
}

/* Reserve `delta` committed bytes; roll back and fail past the cap. */
static bool guard_reserve(int64_t delta) {
    int64_t c = guard_cap();
    if (c < 0) return true;
    int64_t prev = atomic_fetch_add_explicit(&g_used, delta, memory_order_relaxed);
    if (prev + delta <= c) return true;
    atomic_fetch_sub_explicit(&g_used, delta, memory_order_relaxed);
    return false;
}
static void guard_release(int64_t delta) {
    if (guard_cap() < 0) return;
    atomic_fetch_sub_explicit(&g_used, delta, memory_order_relaxed);
}

static bool usable_prot(int prot) {
    return (prot & (PROT_READ | PROT_WRITE | PROT_EXEC)) != 0;
}

/* ---- region table; callers hold the lock ---- */

static void guard_track(uintptr_t base, size_t len, size_t committed) {
    if (g_nregions >= GUARD_MAX_REGIONS) return;
    g_regions[g_nregions++] = (guard_region_t){base, len, committed};
}

/* Drop accounting for [base,base+len); returns the committed bytes released. */
static int64_t guard_untrack(uintptr_t base, size_t len) {
    uintptr_t end = base + len;
    int64_t freed = 0;
    for (size_t i = 0; i < g_nregions; i++) {
        guard_region_t *r = &g_regions[i];
        uintptr_t re = r->base + r->len;
        uintptr_t is = base > r->base ? base : r->base;
        uintptr_t ie = end < re ? end : re;
        if (is >= ie) continue;
        size_t inter = ie - is;
        size_t rel = r->committed < inter ? r->committed : inter;
        freed += (int64_t)rel;
        r->committed -= rel;
        if (is <= r->base && ie >= re) {           /* fully covered */
            memmove(r, r + 1, (g_nregions - i - 1) * sizeof(guard_region_t));
            g_nregions--;
            i--;
        } else if (ie >= re) {                     /* tail unmapped */
            r->len = is - r->base;
        } else if (is <= r->base) {                /* head unmapped */
            r->base = ie;
            r->len = re - ie;
        } else {                                   /* middle: keep head half */
            r->len = is - r->base;
        }
    }
    return freed;
}

/* Committed bytes a protection change to a usable prot would add over
 * [base,base+len): growth beyond each tracked region's counted share, plus
 * the whole of any untracked span (conservative: a commit on an untracked
 * region is counted in full, so accounting can over- but never
 * under-count). */
static int64_t guard_commit_delta(uintptr_t base, size_t len) {
    uintptr_t end = base + len;
    int64_t add = 0, covered = 0;
    for (size_t i = 0; i < g_nregions; i++) {
        guard_region_t *r = &g_regions[i];
        uintptr_t re = r->base + r->len;
        uintptr_t is = base > r->base ? base : r->base;
        uintptr_t ie = end < re ? end : re;
        if (is >= ie) continue;
        size_t inter = ie - is;
        covered += (int64_t)inter;
        if (r->committed < inter) add += (int64_t)(inter - r->committed);
    }
    return add + (int64_t)len - covered;
}
static void guard_apply_commit(uintptr_t base, size_t len) {
    uintptr_t end = base + len;
    for (size_t i = 0; i < g_nregions; i++) {
        guard_region_t *r = &g_regions[i];
        uintptr_t re = r->base + r->len;
        uintptr_t is = base > r->base ? base : r->base;
        uintptr_t ie = end < re ? end : re;
        if (is >= ie) continue;
        size_t inter = ie - is;
        if (r->committed < inter) {
            size_t missing = inter - r->committed;
            size_t grown = r->committed + missing;
            r->committed = grown > r->len ? r->len : grown;
        }
    }
}
static int64_t guard_decommit_delta(uintptr_t base, size_t len) {
    uintptr_t end = base + len;
    int64_t rel = 0;
    for (size_t i = 0; i < g_nregions; i++) {
        guard_region_t *r = &g_regions[i];
        uintptr_t re = r->base + r->len;
        uintptr_t is = base > r->base ? base : r->base;
        uintptr_t ie = end < re ? end : re;
        if (is >= ie) continue;
        size_t inter = ie - is;
        rel += r->committed < inter ? (int64_t)r->committed : (int64_t)inter;
    }
    return rel;
}
static void guard_apply_decommit(uintptr_t base, size_t len) {
    uintptr_t end = base + len;
    for (size_t i = 0; i < g_nregions; i++) {
        guard_region_t *r = &g_regions[i];
        uintptr_t re = r->base + r->len;
        uintptr_t is = base > r->base ? base : r->base;
        uintptr_t ie = end < re ? end : re;
        if (is >= ie) continue;
        size_t inter = ie - is;
        size_t rel = r->committed < inter ? r->committed : inter;
        r->committed -= rel;
    }
}

/* ---- interposed commit entry points ---- */

static void *guard_mmap(void *addr, size_t len, int prot, int flags, int fd,
                        off_t off) {
    int64_t charge = usable_prot(prot) ? (int64_t)len : 0;
    if (charge && !guard_reserve(charge)) {
        errno = ENOMEM;
        return MAP_FAILED;
    }
    void *p = mmap(addr, len, prot, flags, fd, off);
    if (p == MAP_FAILED) {
        if (charge) guard_release(charge);
        return p;
    }
    if (guard_try_lock()) {
        if (flags & MAP_FIXED) guard_release(guard_untrack((uintptr_t)p, len));
        guard_track((uintptr_t)p, len, (size_t)charge);
        guard_unlock();
    }
    return p;
}

static int guard_mprotect(void *addr, size_t len, int prot) {
    bool committed_op = usable_prot(prot);
    int64_t delta;
    bool locked = guard_try_lock();
    if (locked) {
        delta = committed_op
            ? guard_commit_delta((uintptr_t)addr, len)
            : -guard_decommit_delta((uintptr_t)addr, len);
    } else {
        /* Signal-context fallback: count commits conservatively, never
         * release outside the table so accounting can only fail earlier. */
        delta = committed_op ? (int64_t)len : 0;
    }
    if (delta > 0 && !guard_reserve(delta)) {
        errno = ENOMEM;
        return -1;
    }
    int rc = mprotect(addr, len, prot);
    if (rc != 0) {
        if (delta > 0) guard_release(delta);
        return rc;
    }
    if (locked) {
        if (committed_op) {
            guard_apply_commit((uintptr_t)addr, len);
        } else {
            guard_apply_decommit((uintptr_t)addr, len);
            guard_release(-delta);
        }
        guard_unlock();
    }
    return rc;
}

static int guard_munmap(void *addr, size_t len) {
    int64_t freed = 0;
    bool locked = guard_try_lock();
    if (locked) {
        freed = guard_untrack((uintptr_t)addr, len);
        guard_unlock();
    }
    int rc = munmap(addr, len);
    if (rc == 0 && freed) guard_release(freed);
    return rc;
}

static kern_return_t guard_mach_vm_allocate(vm_map_t target,
                                            mach_vm_address_t *addr,
                                            mach_vm_size_t size, int flags) {
    if (!guard_reserve((int64_t)size)) return KERN_RESOURCE_SHORTAGE;
    kern_return_t kr = mach_vm_allocate(target, addr, size, flags);
    if (kr != KERN_SUCCESS) {
        guard_release((int64_t)size);
        return kr;
    }
    if (guard_try_lock()) {
        guard_track((uintptr_t)*addr, (size_t)size, (size_t)size);
        guard_unlock();
    }
    return kr;
}

static kern_return_t guard_mach_vm_map(vm_map_t target, mach_vm_address_t *addr,
                                       mach_vm_size_t size, mach_vm_offset_t mask,
                                       int flags, mem_entry_name_port_t object,
                                       memory_object_offset_t offset,
                                       boolean_t copy, vm_prot_t cur,
                                       vm_prot_t max, vm_inherit_t inheritance) {
    int64_t charge = usable_prot((int)cur) ? (int64_t)size : 0;
    if (charge && !guard_reserve(charge)) return KERN_RESOURCE_SHORTAGE;
    kern_return_t kr = mach_vm_map(target, addr, size, mask, flags, object,
                                 offset, copy, cur, max, inheritance);
    if (kr != KERN_SUCCESS) {
        if (charge) guard_release(charge);
        return kr;
    }
    if (guard_try_lock()) {
        guard_track((uintptr_t)*addr, (size_t)size, (size_t)charge);
        guard_unlock();
    }
    return kr;
}

static kern_return_t guard_mach_vm_protect(vm_map_t target,
                                           mach_vm_address_t addr,
                                           mach_vm_size_t size,
                                           boolean_t set_max, vm_prot_t prot) {
    bool committed_op = usable_prot((int)prot);
    int64_t delta;
    bool locked = guard_try_lock();
    if (locked) {
        delta = committed_op
            ? guard_commit_delta((uintptr_t)addr, (size_t)size)
            : -guard_decommit_delta((uintptr_t)addr, (size_t)size);
    } else {
        delta = committed_op ? (int64_t)size : 0;
    }
    if (delta > 0 && !guard_reserve(delta)) return KERN_RESOURCE_SHORTAGE;
    kern_return_t kr = mach_vm_protect(target, addr, size, set_max, prot);
    if (kr != KERN_SUCCESS) {
        if (delta > 0) guard_release(delta);
        return kr;
    }
    if (locked) {
        if (committed_op) {
            guard_apply_commit((uintptr_t)addr, (size_t)size);
        } else {
            guard_apply_decommit((uintptr_t)addr, (size_t)size);
            guard_release(-delta);
        }
        guard_unlock();
    }
    return kr;
}

static kern_return_t guard_mach_vm_deallocate(vm_map_t target,
                                              mach_vm_address_t addr,
                                              mach_vm_size_t size) {
    int64_t freed = 0;
    bool locked = guard_try_lock();
    if (locked) {
        freed = guard_untrack((uintptr_t)addr, (size_t)size);
        guard_unlock();
    }
    kern_return_t kr = mach_vm_deallocate(target, addr, size);
    if (kr == KERN_SUCCESS && freed) guard_release(freed);
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
    if (!guard_reserve((int64_t)size)) return KERN_RESOURCE_SHORTAGE;
    kern_return_t kr = mach_vm_remap(target, dst, size, mask, flags, src_task,
                                   src, copy, cur, max, inheritance);
    if (kr != KERN_SUCCESS) {
        guard_release((int64_t)size);
        return kr;
    }
    if (guard_try_lock()) {
        guard_track((uintptr_t)*dst, (size_t)size, (size_t)size);
        guard_unlock();
    }
    return kr;
}

typedef struct { const void *new_fn; const void *orig_fn; } guard_interpose_t;
__attribute__((used)) static const guard_interpose_t guard_interposers[]
    __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)guard_mmap, (const void *)mmap},
    {(const void *)guard_mprotect, (const void *)mprotect},
    {(const void *)guard_munmap, (const void *)munmap},
    {(const void *)guard_mach_vm_allocate, (const void *)mach_vm_allocate},
    {(const void *)guard_mach_vm_map, (const void *)mach_vm_map},
    {(const void *)guard_mach_vm_protect, (const void *)mach_vm_protect},
    {(const void *)guard_mach_vm_deallocate, (const void *)mach_vm_deallocate},
    {(const void *)guard_mach_vm_remap, (const void *)mach_vm_remap},
};

/* Seed the counter with the resident bytes already committed at load so the
 * cap covers baseline + growth, then install the supervisor's byte cap. A
 * malformed cap fails closed: every counted commit is refused. */
__attribute__((constructor)) static void guard_init(void) {
    int64_t seed = 0;
    mach_msg_type_number_t count = TASK_BASIC_INFO_64_COUNT;
    task_basic_info_64_data_t info;
    if (task_info(mach_task_self(), TASK_BASIC_INFO_64, (task_info_t)&info,
                  &count) == KERN_SUCCESS) {
        seed = (int64_t)info.resident_size;
    }
    atomic_store_explicit(&g_used, seed, memory_order_relaxed);
    const char *raw = getenv(GUARD_CAP_ENV);
    if (raw == NULL) return;
    /* Any value the supervisor did not produce (zero, empty, malformed,
     * absurd) installs a zero cap so every counted commit is refused. */
    int64_t parsed = 0;
    char *end = NULL;
    unsigned long long v = strtoull(raw, &end, 10);
    if (end != NULL && *end == '\0' && v > 0 &&
        v <= (unsigned long long)GUARD_MAX_CAP) {
        parsed = (int64_t)v;
    }
    atomic_store_explicit(&g_cap, parsed, memory_order_relaxed);
}
