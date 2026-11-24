#!/bin/sh
set -eu
cd /work/wasi
test ! -e Makefile
export WASI_SDK_PATH=/sdk CONFIG_SITE=/source/cpython/Tools/wasm/config.site-wasm32-wasi
export CFLAGS='--target=wasm32-wasip2 -fPIC -I/work/wasi/deps/include'
export LDFLAGS='--target=wasm32-wasip2 -L/work/wasi/deps/lib'
BUILD=$(/bin/sh /source/cpython/config.guess)
printf '%s\n' "$BUILD"
/bin/sh /source/cpython/Tools/wasm/wasi-env /bin/sh /source/cpython/configure -C --host=wasm32-unknown-wasip2 --build="$BUILD" --with-build-python=/work/native2/python --prefix=/work/wasi/install --disable-test-modules --enable-wasm-dynamic-linking --enable-ipv6
printf '%s\n' '_sqlite3 _sqlite/blob.c _sqlite/connection.c _sqlite/cursor.c _sqlite/microprotocols.c _sqlite/module.c _sqlite/prepare_protocol.c _sqlite/row.c _sqlite/statement.c _sqlite/util.c -I/work/wasi/deps/include -L/work/wasi/deps/lib -lsqlite3' > Modules/Setup.local
/usr/bin/make -j1 build_all install
/sdk/bin/clang --target=wasm32-wasip2 -shared -o /work/wasi/libpython3.13.so -Wl,--whole-archive /work/wasi/libpython3.13.a -Wl,--no-whole-archive /work/wasi/Modules/_hacl/libHacl_Hash_SHA2.a /work/wasi/Modules/_decimal/libmpdec/libmpdec.a /work/wasi/Modules/expat/libexpat.a /work/wasi/deps/lib/libz.a /work/wasi/deps/lib/libsqlite3.a -lwasi-emulated-signal -lwasi-emulated-getpid -lwasi-emulated-process-clocks -ldl
sha256sum /work/wasi/libpython3.13.a /work/wasi/libpython3.13.so
