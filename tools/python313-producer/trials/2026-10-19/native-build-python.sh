#!/bin/sh
set -eu
umask 022
mkdir -p /work/native2
cd /work/native2
/usr/bin/gcc --version
/usr/bin/make --version
/source/cpython/configure --prefix=/work/native2/install
/usr/bin/make -j1
./python -I -B -c 'import sys, unicodedata, hashlib, json; assert sys.version_info[:3] == (3,13,16); assert unicodedata.unidata_version == "15.1.0"; print(json.dumps({"python":sys.version,"unicode":unicodedata.unidata_version,"sha256":hashlib.sha256(b"abc").hexdigest(),"native_build_python_only":True}))'
