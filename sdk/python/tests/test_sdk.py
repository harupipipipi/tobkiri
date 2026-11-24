"""Guest SDK contract tests; no native effects or broad test gate are used."""

import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tobkiri_sdk import GuestError, http, storage
from tobkiri_sdk import _binding
from tobkiri_sdk.floats import to_canon_float


class Imports:
    """Explicit generated-import test double that records, never performs effects."""

    def __init__(self):
        self.calls = []

    def invoke(self, interface, operation, **arguments):
        self.calls.append((interface, operation, arguments))
        if interface == "network":
            class Response:
                def status(self): return 200
                def headers(self): return [("content-type", "text/event-stream")]
                def read(self, max): return None
            return Response()
        raise GuestError("denied")


class SdkTests(unittest.TestCase):
    def tearDown(self):
        _binding._imports = None

    def test_unbound_sdk_fails_without_os_fallback(self):
        with self.assertRaisesRegex(GuestError, "not bound"):
            http.get("http://127.0.0.1:18080")
        with self.assertRaises(GuestError): storage.connect("conversations")

    def test_opaque_secret_header_binding_never_stringifies_secret(self):
        from tobkiri_sdk.secrets import SecretRef
        bindings = Imports()
        _binding.bind(bindings)
        opaque = object()
        http.post("http://127.0.0.1:18080/v1/chat/completions",
                  headers={"authorization": http.SecretHeader(SecretRef(opaque), "Bearer ")},
                  json={"stream": True})
        request = bindings.calls[0][2]
        self.assertEqual(request["req"]["secret_headers"], [{"name":"authorization","prefix":"Bearer ","index":0}])
        self.assertEqual(request["secrets"], [opaque])
        self.assertNotIn("authorization", dict(request["req"]["headers"]))

    def test_float_width_and_negative_zero(self):
        self.assertEqual(to_canon_float(0.1,"f32"),"1e-1")
        self.assertEqual(to_canon_float(1.5,"f64"),"1.5e0")
        self.assertEqual(to_canon_float(-0.0,"f32"),"-0e0")
        self.assertEqual(to_canon_float(2**-149,"f32"),"1e-45")
        with self.assertRaises(ValueError): to_canon_float(float("inf"),"f64")
        with self.assertRaises(ValueError): to_canon_float(1e100,"f32")

    def test_python_float_conversion_matches_rust_golden_cases(self):
        import json
        import struct
        vectors=json.loads((Path(__file__).resolve().parents[2]/"js/tests/float-golden.json").read_text())
        for vector in vectors:
            value=struct.unpack("!f" if vector["width"]=="f32" else "!d",bytes.fromhex(vector["bits"]))[0]
            self.assertEqual(to_canon_float(value,vector["width"]),vector["expect"],vector["bits"])


if __name__ == "__main__": unittest.main()
