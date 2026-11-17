"""Operator metadata: temporary SQLite, synthetic admission, no network or AI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import importlib.util
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from test_companion import StorageFixture

spec = importlib.util.spec_from_file_location('ai_sharing_endpoint_fixture',
    Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)
policy = api.sharing_policy
metadata = policy.metadata


def declaration():
    return {'complete': True, 'opaque': False, 'recipients': [{
        'id': 'publisher-ai', 'name': 'Publisher AI', 'origins': ['https://ai.example.com'],
        'purposes': ['manual_ai', 'automatic_personal_context']}]}


def example():
    return {'schema_version': 1, 'profiles': {'Assistant': declaration()}}


def owner_context():
    # Outer middleware fixture, never a client-supplied wire claim.
    return {'native_http_principal': {'purpose': 'native-http',
        'capabilities': frozenset({'http:owner'}), 'profile_id': '/synthetic/profile',
        'scope': 'profile', 'subject': 'fixture-owner'}}


class OperatorMetadataTests(unittest.TestCase):
    def test_complete_is_operator_declared_without_enforcement_claim(self):
        value = metadata.build_operator_disclosure(example(), profile='Assistant')
        self.assertEqual(value['source'], 'operator_declared')
        self.assertTrue(value['complete'])
        self.assertEqual(value['recipients'], declaration()['recipients'])
        self.assertEqual(len(value['revision']), 64)
        self.assertNotIn('enforced', value)
        self.assertNotIn('enforced_operations', value)

    def test_missing_exact_profile_is_incomplete_without_aliasing_or_inference(self):
        for profile in ('assistant', 'default', 'LFM3', 'Local', 'Other'):
            with self.subTest(profile=profile):
                value = metadata.build_operator_disclosure(example(), profile=profile)
                self.assertFalse(value['complete'])
                self.assertEqual(value['profile'], profile)
                self.assertEqual(value['recipients'], [])
                self.assertIn('unavailable_reason', value)

    def test_opaque_empty_or_incomplete_declaration_cannot_claim_complete(self):
        for change in ({'opaque': True}, {'recipients': []}, {'complete': False}):
            value = example(); value['profiles']['Assistant'].update(change)
            self.assertFalse(metadata.validate_operator_policy(value)['profiles']['Assistant']['complete'])
            result = metadata.build_operator_disclosure(value, profile='Assistant')
            self.assertFalse(result['complete'])
            self.assertIn('unavailable_reason', result)

    def test_session_is_echo_and_revision_scope_not_a_lookup(self):
        first = metadata.build_operator_disclosure(example(), profile='Assistant', session_id='not-a-real-session')
        other = metadata.build_operator_disclosure(example(), profile='Assistant', session_id='different')
        self.assertEqual(first['session_id'], 'not-a-real-session')
        self.assertTrue(first['complete'])
        self.assertNotEqual(first['revision'], other['revision'])
        value = example(); value['profiles']['Unrelated'] = declaration()
        self.assertEqual(first, metadata.build_operator_disclosure(value, profile='Assistant', session_id='not-a-real-session'))
        value['profiles']['Assistant']['recipients'][0]['name'] = 'Changed processor'
        self.assertNotEqual(first['revision'], metadata.build_operator_disclosure(
            value, profile='Assistant', session_id='not-a-real-session')['revision'])

    def test_normalization_is_stable_and_does_not_merge_purposes(self):
        value = example(); row = value['profiles']['Assistant']['recipients'][0]
        row['origins'] = ['HTTPS://AI.Example.Com:443/', 'https://ai.example.com']
        row['purposes'] = ['automatic_personal_context', 'manual_ai']
        self.assertEqual(metadata.validate_operator_policy(value), example())
        row['purposes'] = ['manual_ai']
        self.assertEqual(metadata.validate_operator_policy(value)['profiles']['Assistant']['recipients'][0]['purposes'], ['manual_ai'])

    def test_unicode_processor_names_fit_the_native_utf8_limit(self):
        value = example(); row = value['profiles']['Assistant']['recipients'][0]
        row['name'] = '漢' * 53
        self.assertEqual(metadata.validate_operator_policy(value)['profiles']['Assistant']['recipients'][0]['name'], row['name'])
        for name in ('漢' * 54, '\ud800'):
            row['name'] = name
            with self.subTest(byte_case='oversized' if len(name) > 1 else 'invalid_unicode'):
                with self.assertRaises(metadata.InvalidDisclosure):
                    metadata.validate_operator_policy(value)

    def test_closed_schema_and_recipient_validation(self):
        invalid = []
        value = example(); value['secret'] = 'fixture-value'; invalid.append(value)
        value = example(); value['schema_version'] = True; invalid.append(value)
        value = example(); value['profiles'] = []; invalid.append(value)
        for name in ('../other', ' Assistant', 'Assistant ', '*', 'all/profiles', ''):
            value = example(); value['profiles'] = {name: declaration()}; invalid.append(value)
        for key, item in [('complete', 1), ('opaque', 'false'), ('recipients', {}), ('extra', True)]:
            value = example(); value['profiles']['Assistant'][key] = item; invalid.append(value)
        for key, item in [('id', 'Bad ID'), ('name', ''), ('name', 'bad\nname'), ('purposes', []),
                          ('purposes', ['unknown']), ('purposes', ['manual_ai', 'manual_ai']),
                          ('purposes', [True]), ('api_key', 'fixture-value')]:
            value = example(); value['profiles']['Assistant']['recipients'][0][key] = item; invalid.append(value)
        value = example(); value['profiles']['Assistant']['recipients'] *= 2; invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(metadata.InvalidDisclosure):
                metadata.validate_operator_policy(value)

    def test_origins_reject_credentials_paths_and_malformed_hosts(self):
        for origin in ('https://user:fixture-value@ai.example.com', 'https://ai.example.com/v1',
                       'https://ai.example.com?token=fixture-value', 'https://ai.example.com#value',
                       'file:///tmp/model', 'http://ai.example.com:0', 'https://bad_host',
                       'https://ai.example.com\\evil', 'https://[not-ipv6]', 'https://ai.example.com/%2f'):
            value = example(); value['profiles']['Assistant']['recipients'][0]['origins'] = [origin]
            with self.subTest(origin=origin), self.assertRaises(metadata.InvalidDisclosure):
                metadata.validate_operator_policy(value)

    def test_profile_recipient_origin_and_byte_limits(self):
        invalid = [{'schema_version': 1, 'profiles': {f'Profile{i}': declaration() for i in range(65)}}]
        value = example(); value['profiles']['Assistant']['recipients'] *= 65; invalid.append(value)
        for count, profiles, origins in ((52, 5, 1), (33, 2, 16)):
            value = {'schema_version': 1, 'profiles': {}}
            for i in range(profiles):
                entry = declaration(); entry['recipients'] = []
                for j in range(count):
                    row = deepcopy(declaration()['recipients'][0]); row['id'] = f'processor-{j}'
                    row['origins'] = [f'https://ai{k}.example.com' for k in range(origins)]
                    entry['recipients'].append(row)
                value['profiles'][f'Profile{i}'] = entry
            invalid.append(value)
        value = example(); value['profiles']['Assistant']['recipients'][0]['origins'] = [f'https://ai{i}.example.com' for i in range(17)]
        invalid.append(value)
        value = example(); value['profiles']['Assistant']['recipients'] = []
        domain = '.'.join(['a' * 60] * 4)
        for i in range(18):
            row = deepcopy(declaration()['recipients'][0]); row['id'] = f'processor-{i}'
            row['origins'] = [f'https://{j}.{domain}' for j in range(16)]
            value['profiles']['Assistant']['recipients'].append(row)
        invalid.append(value)
        for value in invalid:
            with self.subTest(profiles=len(value['profiles'])), self.assertRaises(metadata.InvalidDisclosure):
                metadata.validate_operator_policy(value)


class OperatorPolicyStorageTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        self.start_storage()

    def tearDown(self):
        self.stop_storage()

    @property
    def database(self):
        return Path(self.directory.name) / 'alpha/agent-companion-runtime/data.db'

    def test_initial_policy_is_neutral_stable_and_missing_profile_incomplete(self):
        before = policy.read()
        self.assertEqual(before, {'schema_version': 1, 'policy_revision': 0, 'policy': policy.DEFAULT})
        self.assertEqual(policy.read(), before)
        self.assertFalse(policy.disclosure('default')['complete'])
        self.assertEqual(set(self.opens), {('alpha', 'agent-companion-runtime', 'data.db')})

    def test_cas_idempotence_and_stale_conflict(self):
        first = policy.replace(0, example())
        self.assertEqual(first['policy_revision'], 1)
        self.assertEqual(policy.replace(1, example()), first)
        with self.assertRaises(policy.Conflict) as conflict:
            policy.replace(0, example())
        self.assertEqual(conflict.exception.current, first)
        self.assertEqual(policy.read(), first)

    def test_stale_first_save_preserves_neutral_initial_receipt(self):
        with self.assertRaises(policy.Conflict) as conflict:
            policy.replace(9, example())
        self.assertEqual(policy.read(), conflict.exception.current)
        self.assertEqual(policy.read()['policy_revision'], 0)

    def test_concurrent_writes_admit_one_matching_revision(self):
        policy.read()
        def save(label):
            value = example(); value['profiles']['Assistant']['recipients'][0]['name'] = label
            try:
                return policy.replace(0, value)['policy_revision']
            except policy.Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save, ['First processor', 'Second processor'])), [1, 'conflict'])

    def test_effective_profile_is_resolved_on_every_call(self):
        first = policy.replace(0, example())
        token = self.home.set('beta')
        try:
            self.assertEqual(policy.read()['policy_revision'], 0)
            self.assertFalse(policy.disclosure('Assistant')['complete'])
            value = example(); value['profiles']['Assistant']['recipients'][0]['name'] = 'Beta processor'
            policy.replace(0, value)
        finally:
            self.home.reset(token)
        self.assertEqual(policy.read(), first)
        self.assertEqual({row[0] for row in self.opens}, {'alpha', 'beta'})

    def test_existing_settings_table_and_installation_identity_are_untouched(self):
        settings = api.state.read()
        with closing(sqlite3.connect(self.database)) as db:
            before = db.execute('SELECT * FROM companion_settings').fetchall()
        policy.replace(0, example())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT * FROM companion_settings').fetchall(), before)
        self.assertEqual(api.state.read(), settings)

    def test_corrupt_duplicate_and_oversized_storage_is_preserved(self):
        policy.read()
        for raw in ('not-json', '{"schema_version":1,"schema_version":1,"profiles":{}}',
                    '{"schema_version":NaN,"profiles":{}}', ' ' * (metadata.MAX_POLICY_BYTES + 1)):
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('UPDATE companion_ai_sharing_policy SET policy_json=?', (raw,))
            for operation in (policy.read, lambda: policy.replace(0, example())):
                with self.subTest(length=len(raw)), self.assertRaises(policy.Unavailable):
                    operation()
            with closing(sqlite3.connect(self.database)) as db:
                self.assertEqual(db.execute('SELECT policy_json FROM companion_ai_sharing_policy').fetchone()[0], raw)

    def test_invalid_inputs_are_rejected_before_storage(self):
        for revision in (True, -1, 0.0, '0', policy.MAX_REVISION + 1):
            with self.subTest(revision=revision), self.assertRaises(policy.InvalidPolicy):
                policy.replace(revision, example())
        for profile, session in (('../other', None), ('Assistant', ' spaced '), ('Assistant', 'bad/session')):
            with self.subTest(profile=profile, session=session), self.assertRaises(policy.InvalidPolicy):
                policy.disclosure(profile, session)
        for value in ({}, {'expected_revision': 0, 'policy': example(), 'owner': True}):
            with self.assertRaises(policy.InvalidPolicy):
                policy.validate_update(value)
        self.assertEqual(self.opens, [])

    def test_revision_exhaustion_refuses_changes_and_allows_identical_save(self):
        policy.replace(0, example())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE companion_ai_sharing_policy SET revision=?', (policy.MAX_REVISION,))
        self.assertEqual(policy.replace(policy.MAX_REVISION, example())['policy_revision'], policy.MAX_REVISION)
        with self.assertRaises(policy.Unavailable):
            policy.replace(policy.MAX_REVISION, policy.DEFAULT)
        self.assertEqual(policy.read()['policy'], example())


class OperatorPolicyAPITests(StorageFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.start_storage()
        self.context = {'hosted_http_authenticated': True}
        app = FastAPI()
        @app.middleware('http')
        async def admission(request, call_next):
            for key, value in self.context.items():
                setattr(request.state, key, value)
            return await call_next(request)
        app.include_router(api.router, prefix='/api/plugins/agent-companion-runtime')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test.invalid')
        self.base = '/api/plugins/agent-companion-runtime'

    async def asyncTearDown(self):
        await self.client.aclose()
        self.stop_storage()

    async def test_authenticated_reads_use_metadata_without_native_imports(self):
        with patch.dict(sys.modules, {'hermes_cli.ai_sharing_policy': None}):
            receipt = await self.client.get(self.base + '/ai-sharing/policy')
            self.assertEqual(receipt.status_code, 200)
            self.assertEqual(receipt.json()['policy'], policy.DEFAULT)
            value = await self.client.get(self.base + '/ai-sharing', params={'profile': 'Assistant', 'session_id': 'unknown'})
        self.assertEqual(value.status_code, 200)
        self.assertFalse(value.json()['complete'])
        self.assertEqual(value.json()['source'], 'operator_declared')
        self.assertEqual(value.json()['session_id'], 'unknown')
        self.assertNotIn('enforced', value.json())
        self.assertNotIn('enforced_operations', value.json())
        self.assertEqual(value.headers['cache-control'], 'no-store')

    async def test_owner_write_readback_exact_profile_and_conflict(self):
        self.context = owner_context()
        saved = await self.client.put(self.base + '/ai-sharing/policy', json={'expected_revision': 0, 'policy': example()})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['policy_revision'], 1)
        self.assertEqual(saved.headers['cache-control'], 'no-store')
        self.context = {'hosted_http_authenticated': True}
        self.assertEqual((await self.client.get(self.base + '/ai-sharing/policy')).json(), saved.json())
        self.assertTrue((await self.client.get(self.base + '/ai-sharing', params={'profile': 'Assistant'})).json()['complete'])
        self.assertFalse((await self.client.get(self.base + '/ai-sharing', params={'profile': 'assistant'})).json()['complete'])
        self.context = owner_context()
        stale = await self.client.put(self.base + '/ai-sharing/policy', json={'expected_revision': 0, 'policy': example()})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['detail']['code'], 'ai_sharing_policy_revision_conflict')
        self.assertEqual(stale.json()['detail']['current'], saved.json())

    async def test_authentication_precedes_parsing_and_storage(self):
        self.context = {}
        for method, path in [('GET', '/ai-sharing'), ('GET', '/ai-sharing/policy'), ('PUT', '/ai-sharing/policy')]:
            response = await self.client.request(method, self.base + path, content=b'not-json',
                headers={'X-Hermes-Hosted-Authenticated': 'true', 'X-Owner': 'true'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.opens, [])

    async def test_nonowner_writes_are_refused_before_body_parsing(self):
        for context in ({'hosted_http_authenticated': True},
                        {'session': SimpleNamespace(provider='fixture', user_id='fixture', expires_at=time.time() + 600)},
                        {'native_http_principal': object()},
                        {'native_http_principal': {'purpose': 'interactive', 'capabilities': frozenset({'http:owner'})}},
                        {'native_http_principal': {'purpose': 'native-http', 'capabilities': frozenset({'session:read'})}},
                        {'native_http_principal': {'purpose': 'native-http', 'capabilities': ['http:owner']}}):
            self.context = context
            response = await self.client.put(self.base + '/ai-sharing/policy', content=b'not-json',
                headers={'X-Owner': 'true', 'X-Hermes-Native-Principal': 'http:owner'})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.opens, [])

    async def test_explicit_hosted_refusal_cannot_be_overridden(self):
        self.context = {**owner_context(), 'hosted_http_authenticated': False}
        response = await self.client.put(self.base + '/ai-sharing/policy', content=b'not-json')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.opens, [])

    async def test_body_owner_spoof_does_not_enable_hosted_write(self):
        response = await self.client.put(self.base + '/ai-sharing/policy', json={
            'expected_revision': 0, 'policy': example(), 'owner': True, 'capabilities': ['http:owner']})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.opens, [])

    async def test_invalid_json_fields_and_credentials_are_redacted(self):
        self.context = owner_context()
        for raw in (b'not-json', b'\xff', b'{"expected_revision":0,"expected_revision":1,"policy":{}}',
                    b'{"expected_revision":NaN,"policy":{}}'):
            response = await self.client.put(self.base + '/ai-sharing/policy', content=raw)
            self.assertEqual(response.status_code, 400)
        value = example(); value['profiles']['Assistant']['recipients'][0]['origins'] = ['https://user:fixture-value@ai.example.com']
        for body in ({'expected_revision': 0, 'policy': value}, {'expected_revision': True, 'policy': example()},
                     {'expected_revision': 0, 'policy': example(), 'owner': 'fixture-value'}):
            response = await self.client.put(self.base + '/ai-sharing/policy', json=body)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('fixture-value', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.opens, [])

    async def test_body_and_scope_limits_are_checked_before_storage(self):
        self.context = owner_context()
        response = await self.client.put(self.base + '/ai-sharing/policy', content=b' ' * (policy.MAX_BODY_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        for params in ({'profile': '../other'}, {'profile': 'Assistant', 'session_id': ' spaced '}):
            self.assertEqual((await self.client.get(self.base + '/ai-sharing', params=params)).status_code, 400)
        self.assertEqual(self.opens, [])

    async def test_unavailable_storage_is_503_without_echo(self):
        policy.read()
        path = Path(self.directory.name) / 'alpha/agent-companion-runtime/data.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE companion_ai_sharing_policy SET policy_json=?', ('fixture-value-invalid',))
        self.context = owner_context()
        for method, route in [('GET', '/ai-sharing/policy'), ('GET', '/ai-sharing'), ('PUT', '/ai-sharing/policy')]:
            response = await self.client.request(method, self.base + route,
                json={'expected_revision': 0, 'policy': example()} if method == 'PUT' else None)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('fixture-value', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')

    async def test_capability_has_no_database_access_or_enforcement_claim(self):
        response = await self.client.get(self.base + '/capabilities')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['features']['ai_sharing_metadata'])
        self.assertNotIn('ai_sharing_enforced', response.json()['features'])
        self.assertEqual(self.opens, [])


if __name__ == '__main__':
    unittest.main()
