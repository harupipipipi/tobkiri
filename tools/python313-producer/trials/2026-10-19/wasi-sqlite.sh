#!/bin/sh
set -eu
mkdir /work/sqlite
cd /work/sqlite
export AR=/sdk/bin/ar CC=/sdk/bin/clang RANLIB=/sdk/bin/ranlib CC_FOR_BUILD=/usr/bin/gcc
export CFLAGS='--target=wasm32-wasip2 --sysroot=/sdk/share/wasi-sysroot -I/sdk/share/wasi-sysroot/include/wasm32-wasip2 -D_WASI_EMULATED_SIGNAL -D_WASI_EMULATED_PROCESS_CLOCKS -fPIC -O2 -DSQLITE_OMIT_WAL -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_OMIT_LOCALTIME -DSQLITE_OMIT_RANDOMNESS -DSQLITE_OMIT_SHARED_CACHE'
export LDFLAGS='--target=wasm32-wasip2 --sysroot=/sdk/share/wasi-sysroot -L/sdk/share/wasi-sysroot/lib'
/bin/sh /source/dependencies/sqlite/configure --host=wasm32-wasip2 --prefix=/work/wasi/deps --disable-shared --enable-static --disable-readline --disable-threadsafe --disable-load-extension
/usr/bin/make -j1 AR=/sdk/bin/ar ARFLAGS=rcs libsqlite3.a
mkdir -p /work/wasi/deps/lib /work/wasi/deps/include
cp libsqlite3.a /work/wasi/deps/lib/libsqlite3.a
cp /source/dependencies/sqlite/sqlite3.h /source/dependencies/sqlite/sqlite3ext.h /work/wasi/deps/include/
/sdk/bin/llvm-ar t /work/wasi/deps/lib/libsqlite3.a
sha256sum /work/wasi/deps/lib/libsqlite3.a
