"""Offline fresh-process import selection and closed bootstrap regressions."""

from pathlib import Path
import subprocess
import sys
import unittest


class BootstrapSourceTests(unittest.TestCase):
    def test_first_import_uses_bound_sources_and_preserves_package_protocol(self):
        source = (Path(__file__).resolve().parents[1]
                  / "schema_bootstrap_source.py").read_text()
        script = r"""
import sys
assert 'urllib' not in sys.modules and 'urllib.parse' not in sys.modules
helper = type(sys)('schema_bootstrap_source')
sys.modules[helper.__name__] = helper
exec(compile(SOURCE, 'schema_bootstrap_source.py', 'exec'), helper.__dict__)
assert 'urllib' not in sys.modules and 'urllib.parse' not in sys.modules
files = {
    'urllib/__init__.py': b'events = []\nfrom . import parse\n',
    'urllib/parse.py': b'from . import events\nevents.append("bound-source")\nVALUE = object()\n',
}
finder = helper.install(files, '/captured')
# A broken ambient search path must not silently select another urllib.
original_path = sys.path[:]
sys.path[:] = ['/not-the-captured-source']
import urllib.parse as parse
import urllib
assert urllib.parse is parse and urllib.events == ['bound-source']
assert parse.__package__ == 'urllib' and urllib.__path__ == ['/captured/urllib']
assert parse.__file__ == parse.__spec__.origin == '/captured/urllib/parse.py'
assert parse.__loader__ is parse.__spec__.loader
sys.path[:] = original_path
assert parse.__loader__.get_source('urllib.parse').encode() == files['urllib/parse.py']
saved = parse.VALUE
import urllib.parse
assert parse.VALUE is saved and urllib.events == ['bound-source']
assert finder.find_spec('unlisted.module') is None
try:
    finder.find_spec('urllib.parse', target=parse)
except ImportError:
    pass
else:
    raise AssertionError('bootstrap must not reload a target')
try:
    helper.install(files, '/captured')
except ValueError:
    pass
else:
    raise AssertionError('preloaded target/parent must be refused')
assert sys.modules['urllib.parse'] is parse and parse.VALUE is saved
assert finder.loaded == ['urllib', 'urllib.parse']
"""
        # The fixture is inert source DATA, never a product Pack or approved
        # Profile. Execute in a new interpreter so parent pathlib cannot warm it.
        script = "SOURCE = " + repr(source) + "\n" + script
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()
