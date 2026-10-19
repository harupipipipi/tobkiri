#!/bin/sh
set -eu
mkdir /work/zlib
cd /work/zlib
export AR=/sdk/bin/ar CC=/sdk/bin/clang RANLIB=/sdk/bin/ranlib
export CFLAGS='--target=wasm32-wasip2 --sysroot=/sdk/share/wasi-sysroot -I/sdk/share/wasi-sysroot/include/wasm32-wasip2 -D_WASI_EMULATED_SIGNAL -fPIC'
export LDFLAGS='--target=wasm32-wasip2 --sysroot=/sdk/share/wasi-sysroot -L/sdk/share/wasi-sysroot/lib -lwasi-emulated-signal'
/bin/sh /source/dependencies/zlib/configure --static --prefix=/work/wasi/deps
/usr/bin/make -j1 AR=/sdk/bin/ar ARFLAGS=rcs CC=/sdk/bin/clang static install
/sdk/bin/llvm-ar t /work/wasi/deps/lib/libz.a
sha256sum /work/wasi/deps/lib/libz.a
