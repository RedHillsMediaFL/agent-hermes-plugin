"""Synthetic native-runtime metadata admission; no provider or network calls."""
import asyncio
import copy
from pathlib import Path
import importlib.util
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException, Response

spec = importlib.util.spec_from_file_location('ai_sharing_endpoint_fixture',
    Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)


def payload():
    return {'schema_version': 1, 'revision': 'a' * 64, 'complete': True, 'enforced': True,
            'profile': 'assistant', 'recipients': [{'id': 'named-ai', 'name': 'Named AI',
                'origins': ['https://ai.invalid'], 'purposes': list(api.ai_sharing.PURPOSES)}],
            'enforced_operations': ['chat']}


class AISharingEndpointTests(unittest.TestCase):
    def native(self, value=None):
        package = ModuleType('hermes_cli'); package.__path__ = []
        policy = ModuleType('hermes_cli.ai_sharing_policy')
        policy.read_disclosure = Mock(return_value=payload() if value is None else value)
        return package, policy

    def test_anonymous_refusal_happens_before_native_metadata_access(self):
        package, policy = self.native()
        with patch.dict(sys.modules, {'hermes_cli': package, 'hermes_cli.ai_sharing_policy': policy}):
            with self.assertRaises(HTTPException) as denied:
                asyncio.run(api.read_ai_sharing(SimpleNamespace(state=SimpleNamespace()), Response(), profile='assistant'))
            self.assertEqual(denied.exception.status_code, 401)
            policy.read_disclosure.assert_not_called()

    def test_missing_native_enforcement_is_explicitly_unavailable(self):
        package = ModuleType('hermes_cli'); package.__path__ = []
        with patch.dict(sys.modules, {'hermes_cli': package}):
            sys.modules.pop('hermes_cli.ai_sharing_policy', None)
            value = api._recipient_disclosure(None, 'assistant', None, None, 'chat')
        self.assertFalse(value['complete'])
        self.assertFalse(value['enforced'])
        self.assertEqual(value['enforced_operations'], [])

    def test_native_contract_is_bound_to_requested_profile_session_and_operation(self):
        package, policy = self.native()
        request = SimpleNamespace(state=SimpleNamespace(native_http_principal=object()))
        response = Response()
        with patch.dict(sys.modules, {'hermes_cli': package, 'hermes_cli.ai_sharing_policy': policy}):
            value = asyncio.run(api.read_ai_sharing(request, response, profile='assistant', operation='chat'))
        self.assertTrue(value['enforced'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        policy.read_disclosure.assert_called_once_with(request, profile='assistant', session_id=None,
                                                       room_id=None, operation='chat')

    def test_wrong_scope_unknown_fields_and_credential_origins_are_refused(self):
        bad = []
        value = payload(); value['profile'] = 'other'; bad.append(value)
        value = payload(); value['session_id'] = 'other'; bad.append(value)
        value = payload(); value['api_key'] = 'fixture-only-not-a-key'; bad.append(value)
        value = payload(); value['recipients'][0]['origins'] = ['https://user:password@ai.invalid']; bad.append(value)
        value = payload(); value['enforced_operations'] = [['chat']]; bad.append(value)
        value = payload(); value['recipients'].append(copy.deepcopy(value['recipients'][0])); bad.append(value)
        for value in bad:
            with self.subTest(value=value):
                package, policy = self.native(value)
                with patch.dict(sys.modules, {'hermes_cli': package, 'hermes_cli.ai_sharing_policy': policy}):
                    with self.assertRaises(api.ai_sharing.InvalidDisclosure):
                        api._recipient_disclosure(None, 'assistant', None, None, 'chat')


if __name__ == '__main__':
    unittest.main()
