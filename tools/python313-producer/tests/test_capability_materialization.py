"""Source/data checks for ordinary Pack staging; no guest or runtime executes."""
import ast
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_capability_projection as materialize

ROOT=Path(__file__).resolve().parents[3]


class CapabilityMaterializationTests(unittest.TestCase):
    def test_local_pack_manifest_binds_actual_members_without_grants_or_signatures(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            files={"component.wasm":b"inert source fixture, not a real Wasm component", "wit/pack.wit":b"source fixture"}
            record=materialize.write_pack(root,materialize.PROJECTOR,files,
                shapes=[(materialize.PROJECTION,"port")],provides=[materialize.PROJECTION],
                consumes=[],allowed=[],flows=[],toolchain="source-fixture-only")
            target=root/'packs'/materialize.PROJECTOR
            raw=(target/'pack.json').read_bytes()
            manifest=json.loads(raw)
            self.assertIsNone(manifest['publisher_key'])
            self.assertEqual(record['manifest_sha256'],materialize.identity(raw)['sha256'])
            for row in manifest['files']:
                actual=materialize.identity(files[row['path']])
                self.assertEqual(row['sha256'],'sha256:'+actual['sha256'])
                self.assertEqual(row['size'],actual['bytes'])
            self.assertEqual(manifest['permissions'],materialize.permissions([]))
            self.assertFalse((target/'signature.json').exists())

    def test_request_flow_keeps_payload_and_calls_the_distinct_original_projector(self):
        source=ROOT/'packs/local.harness-capability-input'
        flow=json.loads((source/'flows/project.flow.json').read_text())
        self.assertEqual(flow['implements'],materialize.FLOW)
        self.assertEqual(flow['loop_limits'],'profile-default')
        self.assertEqual(flow['trigger']['input'],materialize.REQUESTS)
        self.assertEqual(flow['steps'],[{'call':{'args':{'payload':'$input.payload'},
            'op':'project','shape':materialize.PROJECTION},'id':'projection','out':'projection'}])
        text=(source/'src/lib.rs').read_text()
        self.assertIn('flow::run_flow(PROJECTION_FLOW, &frame)',text)
        self.assertNotIn('terminal_read',text)
        self.assertNotIn('call_user_port',text)

    def test_real_flow_consumer_uses_received_attachment_not_user_utility_or_context_forgery(self):
        source=(ROOT/'crates/tobkiri-e2e/tests/capability_projection.rs').read_text()
        compact=re.sub(r'\s+','',source)
        for required in ('session.ui_invoke_received(', 'self.core.save_profile(',
                         'self.core.start(', 'original_terminal_pair('):
            self.assertTrue(required in compact,'missing actual received/Core call: '+required)
        for refused in ('call_user_port(', 'CallCx::', 'approve_literal_user('):
            self.assertTrue(refused not in compact,'unexpected authority substitute: '+refused)

    def test_materializer_cli_guards_real_bytes_before_any_directory_creation(self):
        tree=ast.parse((ROOT/'tools/python313-producer/materialize_capability_projection.py').read_text())
        run=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run')
        first=run.body[0]
        self.assertIsInstance(first,ast.If)
        self.assertIn('GITHUB_ACTIONS',ast.get_source_segment(
            (ROOT/'tools/python313-producer/materialize_capability_projection.py').read_text(),first))
        calls=[n for n in ast.walk(run) if isinstance(n,ast.Call)]
        self.assertTrue(any(isinstance(n.func,ast.Attribute) and n.func.attr=='verify_component' for n in calls))


if __name__=='__main__':
    unittest.main()
