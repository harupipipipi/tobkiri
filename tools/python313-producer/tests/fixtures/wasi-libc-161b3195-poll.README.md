# Exact upstream poll source fixture

`wasi-libc-161b3195-poll.c` is an unchanged source-only copy of
[`poll.c` at wasi-libc commit 161b3195fc2558d2b1ba3eb9ffae3b2b47407623](https://github.com/WebAssembly/wasi-libc/blob/161b3195fc2558d2b1ba3eb9ffae3b2b47407623/libc-bottom-half/cloudlibc/src/libc/poll/poll.c).
It retains the upstream copyright and BSD-2-Clause SPDX notice.

- Bytes: 17,319.
- Git blob: `6d77f338199221a22d9f79545f87bdc5857ca6c5`.
- SHA-256: `22b6fa1ef5462b5a6d3b91b2c26e62bb857c6aa964f5f1047d2c45ffd3c4c0c9`.

The source derivation tests preserve every original byte outside the one
insertion. They do not build libc or prove ABI, errno, timing, signal,
cancellation, actual SDK binary correspondence or cold schema behavior.
The candidate is inactive until its independent runtime comparisons succeed.
