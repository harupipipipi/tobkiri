/* Public build entropy is never a runtime cryptographic provider.
 * This single-threaded WASI guard is source-only and not a runtime acceptance
 * proof. The host bridge is absent, so an admitted runtime cannot become ACTIVE.
 */
#include "Python.h"
#if defined(__wasi__)
#include "pycore_tobkiri_entropy.h"
#include <string.h>

enum entropy_mode { NEW, BUILD_PREINIT, DORMANT, ACTIVATING, ACTIVE, QUARANTINED };
static enum entropy_mode mode = NEW;
static uint64_t build_state[4];
static uint64_t build_counter;
static unsigned seed_depth;
static uint64_t live_owner, live_epoch;
static _PyTobkiriLiveFill live_fill;
static _PyTobkiriLiveCheck live_check;
static _PyTobkiriReseed reseed;
static _PyTobkiriRngAudit rng_audit;

static int unavailable(int raise, const char *reason)
{
    if (raise && Py_IsInitialized()) {
        PyErr_SetString(PyExc_RuntimeError, reason);
    }
    return -1;
}

int _PyTobkiri_EntropyBeginBuild(const unsigned char seed[32])
{
    if (mode != NEW || seed == NULL) return -1;
    for (size_t word = 0; word < 4; word++) {
        uint64_t value = 0;
        for (size_t byte = 0; byte < 8; byte++)
            value |= (uint64_t)seed[word * 8 + byte] << (byte * 8);
        build_state[word] = value;
    }
    build_counter = 0;
    mode = BUILD_PREINIT;
    return 0;
}

int _PyTobkiri_EntropyIsBuild(void) { return mode == BUILD_PREINIT; }

int _PyTobkiri_EntropySeedEnter(void)
{
    /* Only _random's audited implicit initialization/reseed may consume build
     * entropy. Public os.urandom at module import would retain an unaudited
     * cache and is unavailable. This is not a generic Python taint tracker. */
    if (seed_depth != 0) return unavailable(1, "entropy seed reentry denied");
    if (mode != BUILD_PREINIT && mode != ACTIVE && mode != ACTIVATING)
        return unavailable(1, "entropy mode inactive");
    seed_depth = 1;
    return 0;
}
void _PyTobkiri_EntropySeedLeave(void) { seed_depth = 0; }

static uint64_t public_build_word(void)
{
    /* Fixed seed-word/counter mixer over the public declaration. This is
     * noncryptographic; fixed byte order and overflow arithmetic are defined.
     * Both seed and state are erased before snapshot sealing. */
    uint64_t value = build_state[build_counter & 3] + build_counter;
    build_counter++;
    value += UINT64_C(0x9e3779b97f4a7c15);
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

int _PyTobkiri_EntropyFill(void *bytes, Py_ssize_t length, int raise)
{
    if (length < 0 || (length && bytes == NULL)) return unavailable(raise, "invalid entropy length");
    if (mode == BUILD_PREINIT) {
        if (seed_depth != 1) return unavailable(raise, "Unavailable: preinit public entropy cache (CACHE-01)");
        if ((uint64_t)length / 8 + 1 > UINT64_MAX - build_counter)
            return unavailable(raise, "public entropy counter exhausted");
        unsigned char *output = bytes;
        for (Py_ssize_t position = 0; position < length;) {
            uint64_t word = public_build_word();
            for (unsigned shift = 0; shift < 8 && position < length; shift++, position++)
                output[position] = (unsigned char)(word >> (shift * 8));
        }
        return 0;
    }
    if (mode != ACTIVE && !(mode == ACTIVATING && seed_depth == 1))
        return unavailable(raise, "entropy mode inactive");
    if (!live_check || live_check(live_owner, live_epoch) != 0 || !live_fill) {
        _PyTobkiri_EntropyInvalidate();
        return unavailable(raise, "live Core entropy authority unavailable");
    }
    if (live_fill(live_owner, live_epoch, bytes, (size_t)length) != 0) {
        if (bytes && length > 0) memset(bytes, 0, (size_t)length);
        _PyTobkiri_EntropyInvalidate();
        return unavailable(raise, "live Core entropy denied");
    }
    return 0;
}

int _PyTobkiri_EntropySetReseed(_PyTobkiriReseed callback)
{
    if (mode != BUILD_PREINIT || reseed != NULL || callback == NULL) return -1;
    reseed = callback;
    return 0;
}

int _PyTobkiri_EntropySetRngAudit(_PyTobkiriRngAudit callback)
{
    if (mode != BUILD_PREINIT || rng_audit != NULL || callback == NULL) return -1;
    rng_audit = callback;
    return 0;
}

int _PyTobkiri_EntropySeal(void)
{
    if (mode != BUILD_PREINIT || seed_depth != 0) return -1;
    if (rng_audit && rng_audit() != 0) {
        _PyTobkiri_EntropyInvalidate();
        return -1;
    }
    memset(build_state, 0, sizeof(build_state));
    build_counter = 0;
    live_fill = NULL;
    live_check = NULL;
    live_owner = live_epoch = 0;
    mode = DORMANT;
    return 0;
}

int _PyTobkiri_EntropyActivate(uint64_t owner, uint64_t epoch,
                              _PyTobkiriLiveFill fill, _PyTobkiriLiveCheck check)
{
    /* Callback presence is mandatory; no random grant is requested until fill
     * is actually needed. A no-random subset still installs a denying broker. */
    if (mode != DORMANT || !owner || !epoch || !check || !fill) return -1;
    mode = ACTIVATING;
    live_owner = owner;
    live_epoch = epoch;
    live_fill = fill;
    live_check = check;
    /* Reseed's primitive calls see ACTIVATING but public reentry does not. */
    if (check(owner, epoch) != 0 || (reseed && reseed() != 0) || check(owner, epoch) != 0) {
        _PyTobkiri_EntropyInvalidate();
        return -1;
    }
    mode = ACTIVE;
    return 0;
}

int _PyTobkiri_EntropyRequireActive(void)
{
    if (mode != ACTIVE || !live_check || live_check(live_owner, live_epoch) != 0) {
        if (mode == ACTIVE) _PyTobkiri_EntropyInvalidate();
        return -1;
    }
    return 0;
}

int _PyTobkiri_EntropyRequireRng(int implicit)
{
    if (mode == BUILD_PREINIT)
        return unavailable(1, "Unavailable: preinit public RNG state/cache (CACHE-01)");
    if (_PyTobkiri_EntropyRequireActive() != 0)
        return unavailable(1, "RNG use outside active Core epoch");
    /* Explicit application seeds remain noncrypto/reproducible. Implicit
     * retained generators require the live entropy route to remain present. */
    if (implicit && !live_fill) return unavailable(1, "implicit RNG entropy unavailable");
    return 0;
}

void _PyTobkiri_EntropyInvalidate(void)
{
    mode = QUARANTINED;
    memset(build_state, 0, sizeof(build_state));
    build_counter = 0;
    seed_depth = 0;
    live_fill = NULL;
    live_check = NULL;
    live_owner = live_epoch = 0;
    reseed = NULL;
    rng_audit = NULL;
}
#endif /* __wasi__: native CPython behavior is unchanged. */
