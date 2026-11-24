/* Private source-patched WASI implementation API. No public component export. */
#ifndef Py_INTERNAL_TOBKIRI_ENTROPY_H
#define Py_INTERNAL_TOBKIRI_ENTROPY_H
#include "Python.h"
#include <stddef.h>
#include <stdint.h>

typedef int (*_PyTobkiriLiveFill)(uint64_t owner, uint64_t epoch,
                                 void *bytes, size_t length);
typedef int (*_PyTobkiriLiveCheck)(uint64_t owner, uint64_t epoch);
typedef int (*_PyTobkiriReseed)(void);
typedef int (*_PyTobkiriRngAudit)(void);

int _PyTobkiri_EntropyBeginBuild(const unsigned char seed[32]);
int _PyTobkiri_EntropySeal(void);
int _PyTobkiri_EntropyFill(void *bytes, Py_ssize_t length, int raise);
int _PyTobkiri_EntropyRequireActive(void);
int _PyTobkiri_EntropyRequireRng(int implicit);
int _PyTobkiri_EntropyIsBuild(void);
int _PyTobkiri_EntropySeedEnter(void);
void _PyTobkiri_EntropySeedLeave(void);
int _PyTobkiri_EntropySetReseed(_PyTobkiriReseed callback);
int _PyTobkiri_EntropySetRngAudit(_PyTobkiriRngAudit callback);
/* Core-private bridge must provide Gate-backed callbacks and own the epoch. */
int _PyTobkiri_EntropyActivate(uint64_t owner, uint64_t epoch,
                              _PyTobkiriLiveFill fill,
                              _PyTobkiriLiveCheck check);
void _PyTobkiri_EntropyInvalidate(void);
#endif
