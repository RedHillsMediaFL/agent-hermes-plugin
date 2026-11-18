"""Per-profile setup uses synthetic processors and temporary plugin storage."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import MappingProxyType, ModuleType, SimpleNamespace
import sys
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx

from test_ai_sharing_api import api, declaration, policy
from test_companion import StorageFixture


class ProfileSetupStorageTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        self.start_storage()

    def tearDown(self):
        self.stop_storage()

    def test_fresh_profile_is_unconfigured_and_not_ready(self):
        value = policy.profile_setup('Assistant')
        self.assertEqual(value['profile'], 'Assistant')
        self.assertEqual(value['policy_revision'], 0)
        self.assertFalse(value['configured'])
        self.assertFalse(value['ready'])
        self.assertEqual(value['entry'], {'complete': False, 'opaque': False, 'recipients': []})
        self.assertFalse(value['declaration']['complete'])
        self.assertNotIn('policy', value)

    def test_manual_recipient_declaration_is_ready_without_consent_or_enforcement(self):
        entry = declaration()
        entry['recipients'][0]['purposes'] = ['manual_ai']
        saved = policy.replace_profile(0, 'Assistant', entry)
        self.assertTrue(saved['configured'])
        self.assertTrue(saved['ready'])
        self.assertEqual(saved['entry'], entry)
        self.assertEqual(saved, policy.profile_setup('Assistant'))
        self.assertNotIn('consent', saved)
        self.assertNotIn('enforced', saved['declaration'])
        self.assertEqual(saved['declaration']['source'], 'operator_declared')

    def test_automatic_context_alone_cannot_make_chat_ready(self):
        entry = declaration()
        entry['recipients'][0]['purposes'] = ['automatic_personal_context']
        saved = policy.replace_profile(0, 'Assistant', entry)
        self.assertTrue(saved['declaration']['complete'])
        self.assertFalse(saved['ready'])
        self.assertIn('unavailable_reason', saved)

    def test_opaque_incomplete_and_empty_entries_stay_unready(self):
        for change in ({'opaque': True}, {'complete': False}, {'recipients': []}):
            with self.subTest(change=change):
                entry = declaration()
                entry.update(change)
                before = policy.read()
                saved = policy.replace_profile(before['policy_revision'], 'Assistant', entry)
                self.assertFalse(saved['ready'])
                self.assertFalse(saved['entry']['complete'])

    def test_scoped_edit_preserves_other_profiles_and_settings(self):
        settings = api.state.read()
        initial = {'schema_version': 1, 'profiles': {'Private': declaration()}}
        policy.replace(0, initial)
        changed = declaration()
        changed['recipients'][0]['name'] = 'Different Processor'
        saved = policy.replace_profile(1, 'Assistant', changed)
        self.assertEqual(saved['policy_revision'], 2)
        self.assertEqual(policy.read()['policy']['profiles']['Private'], initial['profiles']['Private'])
        self.assertEqual(policy.read()['policy']['profiles']['Assistant'], changed)
        self.assertEqual(api.state.read(), settings)
        self.assertNotIn('Private', repr(saved))
        self.assertNotIn('policy', saved)

    def test_same_entry_with_current_revision_is_idempotent(self):
        saved = policy.replace_profile(0, 'Assistant', declaration())
        self.assertEqual(policy.replace_profile(1, 'Assistant', declaration()), saved)
        self.assertEqual(policy.read()['policy_revision'], 1)

    def test_stale_conflict_contains_only_requested_profile(self):
        initial = {'schema_version': 1, 'profiles': {'Private': declaration()}}
        policy.replace(0, initial)
        with self.assertRaises(policy.Conflict) as failure:
            policy.replace_profile(0, 'Assistant', declaration())
        receipt = failure.exception.current
        self.assertEqual(receipt, policy.profile_setup('Assistant'))
        self.assertNotIn('Private', repr(receipt))
        self.assertNotIn('policy', receipt)
        self.assertEqual(policy.read()['policy'], initial)

    def test_profile_names_match_exactly_without_aliasing(self):
        policy.replace_profile(0, 'Assistant', declaration())
        self.assertFalse(policy.profile_setup('assistant')['configured'])
        self.assertFalse(policy.profile_setup('default')['configured'])
        self.assertTrue(policy.profile_setup('Assistant')['configured'])

    def test_invalid_scope_revision_or_entry_cannot_open_storage(self):
        for profile, expected, entry in [
            ('../Assistant', 0, declaration()),
            ('Assistant', True, declaration()),
            ('Assistant', -1, declaration()),
            ('Assistant', 0, {**declaration(), 'unexpected': True}),
        ]:
            with self.subTest(profile=profile, expected=expected):
                with self.assertRaises(policy.InvalidPolicy):
                    policy.replace_profile(expected, profile, entry)
        for profile in ('../Assistant', '', ' Assistant'):
            with self.assertRaises(policy.InvalidPolicy):
                policy.profile_setup(profile)
        self.assertEqual(self.opens, [])

    def test_closed_profile_request_rejects_whole_policy_or_scope_in_body(self):
        for value in ({'expected_revision': 0, 'policy': {}},
                      {'expected_revision': 0, 'entry': declaration(), 'profile': 'Other'}):
            with self.assertRaises(policy.InvalidPolicy):
                policy.validate_profile_update('Assistant', value)
        self.assertEqual(self.opens, [])

    def test_merged_global_limits_cannot_be_evaded_by_scoped_writes(self):
        initial = {'schema_version': 1, 'profiles': {
            f'Agent{i}': declaration() for i in range(64)}}
        before = policy.replace(0, initial)
        with self.assertRaises(policy.InvalidPolicy):
            policy.replace_profile(1, 'Overflow', declaration())
        self.assertEqual(policy.read(), before)

    def test_concurrent_profile_edits_do_not_lose_each_others_updates(self):
        policy.read()

        def save(profile):
            try:
                return policy.replace_profile(0, profile, deepcopy(declaration()))['policy_revision']
            except policy.Conflict as conflict:
                self.assertEqual(conflict.current['profile'], profile)
                self.assertNotIn('policy', conflict.current)
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save, ['First', 'Second'])), [1, 'conflict'])
        self.assertEqual(policy.read()['policy_revision'], 1)
        self.assertEqual(len(policy.read()['policy']['profiles']), 1)


class ProfileSetupAPITests(StorageFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.start_storage()
        self.allowed_profiles = set()
        self.effective_profile = 'Assistant'
        self.authority_unavailable = False
        self.authority_calls = []
        self.context = {'session': SimpleNamespace(
            provider='basic', user_id='fixture-caller', expires_at=time.time() + 60)}

        def operator_authorization(request, *, profile, capability):
            self.authority_calls.append((profile, capability, len(self.opens)))
            if self.authority_unavailable:
                raise HTTPException(503, {'code': 'operator_authority_unavailable'})
            if profile not in self.allowed_profiles or profile != self.effective_profile:
                return None
            return MappingProxyType({'profile': profile,
                'capabilities': frozenset({'recipient-policy:read', 'recipient-policy:write'})})

        package, helper = ModuleType('hermes_cli'), ModuleType('hermes_cli.plugins')
        package.__path__ = []
        helper.operator_authorization = operator_authorization
        self.helper_modules = patch.dict(sys.modules, {'hermes_cli': package, 'hermes_cli.plugins': helper})
        self.helper_modules.start()
        app = FastAPI()

        @app.middleware('http')
        async def admission(request, call_next):
            for key, value in self.context.items():
                setattr(request.state, key, value)
            return await call_next(request)

        app.include_router(api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.helper_modules.stop()
        self.stop_storage()

    async def test_setup_reports_missing_declaration_before_first_send(self):
        result = await self.client.get('/ai-sharing/setup', params={'profile': 'Assistant'})
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()['ready'])
        self.assertFalse(result.json()['configured'])
        self.assertFalse(result.json()['can_edit'])
        self.assertEqual(result.headers['cache-control'], 'no-store')

    async def test_verified_exact_profile_grant_can_save_without_granting_consent(self):
        self.allowed_profiles.add('Assistant')
        status = await self.client.get('/ai-sharing/setup', params={'profile': 'Assistant'})
        self.assertTrue(status.json()['can_edit'])
        saved = await self.client.put('/ai-sharing/policy/profiles/Assistant?profile=Assistant',
            json={'expected_revision': 0, 'entry': declaration()})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()['ready'])
        self.assertTrue(saved.json()['can_edit'])
        self.assertNotIn('consent', saved.json())
        self.assertEqual((await self.client.get('/ai-sharing/setup',
            params={'profile': 'Assistant'})).json(), saved.json())
        refused = await self.client.put('/ai-sharing/policy/profiles/Other?profile=Other', content='invalid-json')
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()['detail']['code'], 'operator_authority_required')
        self.assertEqual(set(policy.read()['policy']['profiles']), {'Assistant'})

    async def test_ordinary_authentication_and_wire_claims_do_not_establish_operator_authority(self):
        for headers in ({}, {'X-Hermes-Operator-Grant': 'unverified-fixture-value'},
                        {'X-Operator': 'true', 'X-Profile': 'Assistant'}):
            with self.subTest(headers=list(headers)):
                response = await self.client.put('/ai-sharing/policy/profiles/Assistant?profile=Assistant',
                    headers=headers, content='invalid-json')
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['detail']['code'], 'operator_authority_required')
        self.assertEqual(self.opens, [])
        self.assertTrue(all(count == 0 for _, _, count in self.authority_calls))

    async def test_scoped_grant_cannot_replace_whole_policy(self):
        self.allowed_profiles.add('Assistant')
        response = await self.client.put('/ai-sharing/policy', content='invalid-json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.authority_calls, [])
        self.assertEqual(self.opens, [])

    async def test_path_query_scope_disagreement_is_refused_before_authority_and_storage(self):
        self.allowed_profiles.add('Assistant')
        for query in ('', '?profile=Other', '?profile=Assistant&profile=Other',
                      '?profile=Assistant&profile=Assistant'):
            response = await self.client.put('/ai-sharing/policy/profiles/Assistant' + query,
                content='invalid-json')
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['detail']['code'], 'profile_scope_mismatch')
        self.assertEqual(self.authority_calls, [])
        self.assertEqual(self.opens, [])

    async def test_valid_grant_with_wrong_effective_profile_cannot_write_default_store(self):
        self.allowed_profiles.add('Other')
        response = await self.client.put('/ai-sharing/policy/profiles/Other?profile=Other',
            json={'expected_revision': 0, 'entry': declaration()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.opens, [])

    async def test_local_owner_scoped_write_still_uses_supported_profile_binding_helper(self):
        self.context['native_http_principal'] = {'purpose': 'native-http',
            'capabilities': frozenset({'http:owner'})}
        response = await self.client.put('/ai-sharing/policy/profiles/Assistant?profile=Assistant',
            content='invalid-json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.authority_calls), 1)
        self.assertEqual(self.opens, [])

    async def test_unauthenticated_or_explicitly_refused_requests_do_not_reach_authority_or_storage(self):
        self.allowed_profiles.add('Assistant')
        for context in ({}, {'hosted_http_authenticated': False}):
            self.context = context
            for method, path in (('GET', '/ai-sharing/setup?profile=Assistant'),
                                 ('PUT', '/ai-sharing/policy/profiles/Assistant?profile=Assistant')):
                response = await self.client.request(method, path, content='invalid-json')
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.authority_calls, [])
        self.assertEqual(self.opens, [])

    async def test_revocation_after_body_read_is_checked_before_policy_storage(self):
        self.allowed_profiles.add('Assistant')
        original_body = api._body

        async def body_then_revoke(*args, **kwargs):
            value = await original_body(*args, **kwargs)
            self.allowed_profiles.clear()
            return value

        with patch.object(api, '_body', body_then_revoke):
            response = await self.client.put('/ai-sharing/policy/profiles/Assistant?profile=Assistant',
                json={'expected_revision': 0, 'entry': declaration()})
        self.assertEqual(response.status_code, 403)
        self.assertGreaterEqual(len(self.authority_calls), 2)
        self.assertEqual(self.opens, [])

    async def test_authority_storage_failure_is_503_before_body_and_policy_storage(self):
        self.authority_unavailable = True
        response = await self.client.put('/ai-sharing/policy/profiles/Assistant?profile=Assistant', content='invalid-json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'operator_authority_unavailable')
        self.assertEqual(self.opens, [])

    async def test_conflict_does_not_disclose_another_profiles_policy(self):
        policy.replace(0, {'schema_version': 1, 'profiles': {'Private': declaration()}})
        self.allowed_profiles.add('Assistant')
        response = await self.client.put('/ai-sharing/policy/profiles/Assistant?profile=Assistant',
            json={'expected_revision': 0, 'entry': declaration()})
        self.assertEqual(response.status_code, 409)
        detail = response.json()['detail']
        self.assertEqual(detail['code'], 'ai_sharing_policy_revision_conflict')
        self.assertEqual(detail['current']['profile'], 'Assistant')
        self.assertNotIn('Private', response.text)
        self.assertNotIn('policy', detail['current'])


if __name__ == '__main__':
    unittest.main()
