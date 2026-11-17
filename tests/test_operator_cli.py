"""Real profile-scoped storage through the operator CLI, with no server or network."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from test_companion import StorageFixture, state

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('operator_cli_test', ROOT / 'operator_cli.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def example():
    return {'schema_version': 1, 'profiles': {'default': {
        'complete': True, 'opaque': False, 'recipients': [{
            'id': 'example', 'name': 'Example Operator', 'origins': ['https://example.com'],
            'purposes': ['manual_ai']}]}}}


class OperatorCLITests(StorageFixture, unittest.TestCase):
    def setUp(self):
        self.start_storage()
        self.path = Path(self.directory.name) / 'policy.json'
        self.path.write_text(json.dumps(example()))

    def tearDown(self):
        self.stop_storage()

    def run_cli(self, action='set', revision=0):
        out = io.StringIO()
        with redirect_stdout(out):
            status = cli.command(SimpleNamespace(agent_sharing_action=action,
                expected_revision=revision, file=str(self.path)))
        return status, json.loads(out.getvalue())

    def test_local_operator_save_is_visible_to_dashboard_store_and_idempotent(self):
        status, saved = self.run_cli()
        self.assertEqual(status, 0)
        self.assertEqual(saved['policy_revision'], 1)
        self.assertEqual(cli.policy.disclosure('default')['recipients'],
                         example()['profiles']['default']['recipients'])
        self.assertEqual(self.run_cli(revision=1)[1], saved)
        self.assertEqual(self.run_cli('show')[1], saved)

    def test_stale_save_cannot_overwrite_operator_changes(self):
        self.run_cli()
        value = example(); value['profiles']['default']['recipients'][0]['name'] = 'New Operator'
        self.path.write_text(json.dumps(value))
        status, refused = self.run_cli(revision=0)
        self.assertEqual(status, 1)
        self.assertEqual(refused, {'ok': False, 'code': 'ai_sharing_policy_revision_conflict',
                                  'policy_revision': 1})
        self.assertEqual(self.run_cli('show')[1]['policy'], example())

    def test_effective_hermes_profile_isolation(self):
        self.run_cli()
        self.home.set('beta')
        self.assertEqual(self.run_cli('show')[1]['policy']['profiles'], {})
        self.home.set('alpha')
        self.assertEqual(self.run_cli('show')[1]['policy'], example())

    def test_invalid_files_and_revision_fail_before_storage_and_hide_input(self):
        for raw in ['{"schema_version":1,"schema_version":1,"profiles":{}}',
                    '{"secret":"do-not-echo"}', '{"schema_version":NaN}',
                    'x' * (cli.policy.metadata.MAX_POLICY_BYTES + 1), '\ud800']:
            self.path.write_bytes(raw.encode('utf-8', errors='surrogatepass'))
            self.assertEqual(self.run_cli(), (1, {'ok': False, 'code': 'invalid_policy'}))
            self.assertEqual(self.opens, [])
        self.assertEqual(self.run_cli(revision=-1), (1, {'ok': False, 'code': 'invalid_policy'}))
        self.assertEqual(self.opens, [])

    def test_missing_file_does_not_create_policy_database(self):
        self.path.unlink()
        self.assertEqual(self.run_cli(), (1, {'ok': False, 'code': 'invalid_policy'}))
        self.assertEqual(self.opens, [])

    def test_only_local_cli_is_registered_without_agent_tools_or_hooks(self):
        root_spec = importlib.util.spec_from_file_location('operator_plugin_test', ROOT / '__init__.py')
        plugin = importlib.util.module_from_spec(root_spec)
        root_spec.loader.exec_module(plugin)
        entries = []
        plugin.register(SimpleNamespace(register_cli_command=lambda **kw: entries.append(kw)))
        self.assertEqual([entry['name'] for entry in entries], ['agent-sharing'])
        self.assertTrue(callable(entries[0]['setup_fn']))
        self.assertTrue(callable(entries[0]['handler_fn']))
        self.assertEqual(self.opens, [])

    def test_installed_package_and_http_capabilities_report_the_same_release(self):
        root_spec = importlib.util.spec_from_file_location('operator_version_test', ROOT / '__init__.py')
        plugin = importlib.util.module_from_spec(root_spec)
        root_spec.loader.exec_module(plugin)
        manifest_version = next(line.split(':', 1)[1].strip().strip('"')
                                for line in (ROOT / 'plugin.yaml').read_text().splitlines()
                                if line.startswith('version:'))
        versions = [plugin.__version__, state.VERSION, manifest_version,
                    json.loads((ROOT / 'dashboard/manifest.json').read_text())['version']]
        self.assertEqual(len(set(versions)), 1)


if __name__ == '__main__':
    unittest.main()
