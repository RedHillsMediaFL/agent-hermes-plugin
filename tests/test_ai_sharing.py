"""Pure disclosure-builder tests: synthetic config only, no runtime/provider imports."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'dashboard' / 'ai_sharing.py'
spec = importlib.util.spec_from_file_location('ai_sharing_contract_test', SOURCE)
sharing = importlib.util.module_from_spec(spec)
exec(compile(SOURCE.read_bytes(), str(SOURCE), 'exec'), sharing.__dict__)


def recipient(identifier='operator-ai', name='Operator AI', origin='https://operator.example'):
    return {'id': identifier, 'name': name, 'origins': [origin]}


def declaration(routes=None):
    return {'schema_version': 1, 'routes': routes or {
        'auxiliary.default': {'complete': True, 'recipients': [recipient()]}}}


def config():
    return {'model': {'provider': 'openai', 'default': 'test-model'}, 'stt': {'enabled': False}}


def build(value=None, **kwargs):
    return sharing.build_disclosure(config() if value is None else value, profile='default', **kwargs)


class DisclosureTests(unittest.TestCase):
    def test_contract_and_default_non_enforcement(self):
        result = build(downstreams=declaration())
        self.assertEqual(result['schema_version'], 1)
        self.assertTrue(result['complete'])
        self.assertFalse(result['enforced'])
        self.assertEqual(result['profile'], 'default')
        self.assertNotIn('session_id', result)
        self.assertRegex(result['revision'], r'^[0-9a-f]{64}$')
        self.assertIn('enforcement', result['unavailable_reason'])
        self.assertEqual({r['id'] for r in result['recipients']}, {'openai', 'operator-ai'})
        for row in result['recipients']:
            self.assertEqual(row['purposes'], ['manual_ai', 'automatic_personal_context'])

    def test_missing_runtime_defaults_remain_incomplete(self):
        result = build({'model': {'provider': 'openai'}})
        self.assertFalse(result['complete'])
        self.assertEqual([r['id'] for r in result['recipients']], ['openai'])

    def test_explicit_transcription_disable_does_not_invent_local_recipient(self):
        result = build(downstreams=declaration())
        self.assertTrue(result['complete'])
        self.assertNotIn('local', json.dumps(result))

    def test_local_lfm_and_model_brand_do_not_establish_recipient(self):
        for provider in ('local', 'lfm', 'custom', 'auto'):
            value = {'model': {'provider': provider, 'model': 'anthropic/lfm',
                               'base_url': 'http://192.0.2.1:8000/v1'}, 'stt': {'enabled': False}}
            with self.subTest(provider=provider):
                result = build(value)
                self.assertFalse(result['complete'])
                self.assertEqual(result['recipients'], [])
                self.assertNotIn('anthropic', json.dumps(result))

    def test_local_route_requires_named_complete_first_hop_declaration(self):
        value = {'provider': 'custom', 'base_url': 'http://192.0.2.1:8000/v1',
                 'model': 'lfm', 'stt': {'enabled': False}}
        declared = declaration()
        declared['routes']['main'] = {'complete': True, 'recipients': [
            recipient('publisher-lfm', 'Publisher-hosted LFM', 'http://192.0.2.1:8000')]}
        result = build(value, downstreams=declared)
        self.assertTrue(result['complete'])
        self.assertIn('Publisher-hosted LFM', [r['name'] for r in result['recipients']])
        declared['routes']['main']['recipients'][0]['origins'] = ['https://different.example']
        self.assertFalse(build(value, downstreams=declared)['complete'])

    def test_provider_override_does_not_mislabel_proxy_as_builtin(self):
        value = config()
        value['model']['base_url'] = 'https://proxy.example/openai/v1'
        result = build(value)
        self.assertFalse(result['complete'])
        self.assertEqual(result['recipients'], [])

    def test_known_aggregator_discloses_first_hop_but_not_guessed_model_vendor(self):
        value = {'model': {'provider': 'openrouter', 'model': 'anthropic/test'}, 'stt': {'enabled': False}}
        result = build(value, downstreams=declaration())
        self.assertFalse(result['complete'])
        self.assertEqual({r['id'] for r in result['recipients']}, {'openrouter', 'operator-ai'})
        self.assertNotIn('Anthropic', json.dumps(result))
        declared = declaration()
        declared['routes']['main'] = {'complete': True, 'recipients': [recipient('actual-downstream')]}
        self.assertTrue(build(value, downstreams=declared)['complete'])

    def test_all_main_fallbacks_are_included_even_legacy_chain(self):
        value = config()
        value['fallback_providers'] = [{'provider': 'anthropic', 'model': 'fallback-a'}]
        value['fallback_model'] = {'provider': 'deepseek', 'model': 'fallback-b'}
        result = build(value, downstreams=declaration())
        self.assertTrue(result['complete'])
        self.assertEqual({r['id'] for r in result['recipients']},
                         {'openai', 'anthropic', 'deepseek', 'operator-ai'})

    def test_auxiliary_tasks_and_fallback_chain_are_included(self):
        value = config()
        value['auxiliary'] = {'compression': {'provider': 'anthropic',
                                               'fallback_chain': [{'provider': 'groq'}, {'provider': 'deepseek'}]}}
        result = build(value, downstreams=declaration())
        self.assertTrue(result['complete'])
        self.assertEqual({r['id'] for r in result['recipients']},
                         {'openai', 'anthropic', 'groq', 'deepseek', 'operator-ai'})

    def test_auto_auxiliary_does_not_silently_inherit_main_or_global_declaration(self):
        value = config()
        value['auxiliary'] = {'compression': {'provider': 'auto'}}
        self.assertFalse(build(value, downstreams=declaration())['complete'])

    def test_selected_transcription_section_and_fallbacks(self):
        value = config()
        value['stt'] = {'enabled': True, 'provider': 'groq', 'groq': {'model': 'transcribe',
                        'fallback_chain': [{'provider': 'mistral'}]},
                        'openai': {'base_url': 'https://unused.example'}}
        result = build(value, downstreams=declaration())
        self.assertTrue(result['complete'])
        self.assertEqual({r['id'] for r in result['recipients']}, {'openai', 'groq', 'mistral', 'operator-ai'})
        self.assertNotIn('unused.example', json.dumps(result))

    def test_transcription_provider_case_does_not_hide_endpoint_override(self):
        value = config()
        value['stt'] = {'provider': 'Groq', 'groq': {'base_url': 'https://private-proxy.example/v1'}}
        result = build(value, downstreams=declaration())
        self.assertFalse(result['complete'])
        self.assertNotIn('groq', {r['id'] for r in result['recipients']})

    def test_custom_provider_definition_origin_requires_operator_identity(self):
        value = {'model': {'provider': 'named-local'}, 'providers': {
            'named-local': {'base_url': 'http://192.0.2.20:8000/v1', 'api_key': 'synthetic-credential'}},
            'stt': {'enabled': False}}
        result = build(value)
        self.assertFalse(result['complete'])
        self.assertEqual(result['recipients'], [])
        declared = declaration()
        declared['routes']['main'] = {'complete': True, 'recipients': [
            recipient('named-local', 'My operator', 'http://192.0.2.20:8000')]}
        self.assertTrue(build(value, downstreams=declared)['complete'])

    def test_custom_provider_list_is_supported_without_credential_access(self):
        value = {'provider': 'named-local', 'custom_providers': [
            {'name': 'named-local', 'base_url': 'http://192.0.2.22:8000/v1', 'api_key_env': 'SYNTHETIC_KEY'}]}
        result = build(value)
        self.assertFalse(result['complete'])
        self.assertNotIn('SYNTHETIC_KEY', json.dumps(result))

    def test_injected_registry_is_read_only_and_does_not_import_runtime(self):
        registry = {'fixture': {'id': 'fixture-provider', 'name': 'Fixture Provider',
                                'origins': ['https://fixture.example']}}
        original = deepcopy(registry)
        result = build({'provider': 'fixture', 'stt': {'enabled': False}},
                       provider_registry=registry, downstreams=declaration())
        self.assertTrue(result['complete'])
        self.assertEqual(registry, original)

    def test_provider_definition_case_cannot_hide_or_ambiguously_select_a_proxy(self):
        value = config(); value['providers'] = {'OpenAI': {'base_url': 'https://proxy.example/v1'}}
        self.assertFalse(build(value, downstreams=declaration())['complete'])
        value['providers']['openai'] = {'base_url': 'https://different.example/v1'}
        with self.assertRaises(sharing.InvalidDisclosure): build(value)

    def test_recipients_merge_origins_but_keep_distinct_purposes(self):
        declared = declaration()
        declared['routes']['main'] = {'complete': True, 'recipients': [recipient(origin='https://second.example')]}
        result = build(downstreams=declared)
        row = next(r for r in result['recipients'] if r['id'] == 'operator-ai')
        self.assertEqual(row['origins'], ['https://operator.example', 'https://second.example'])
        self.assertEqual(row['purposes'], list(sharing.PURPOSES))

    def test_conflicting_identity_names_are_refused(self):
        declared = declaration()
        declared['routes']['main'] = {'complete': True, 'recipients': [recipient(name='Conflicting identity')]}
        with self.assertRaises(sharing.InvalidDisclosure):
            build(downstreams=declared)

    def test_origin_canonicalization_paths_are_never_returned(self):
        value = config()
        value['model']['base_url'] = 'https://API.OPENAI.COM:443/private/v1/'
        result = build(value)
        self.assertEqual(result['recipients'][0]['origins'], ['https://api.openai.com'])
        self.assertNotIn('private', json.dumps(result))
        declared = declaration()
        declared['routes']['main'] = {'complete': True, 'recipients': [
            recipient('ipv6', 'IPv6 operator', 'http://[2001:db8::1]:8000/')]}
        self.assertIn('http://[2001:db8::1]:8000', json.dumps(build(downstreams=declared)))

    def test_urls_with_credentials_query_fragment_or_ambiguous_host_are_refused(self):
        urls = ['https://user:synthetic-secret@api.example/v1', 'https://api.example/v1?key=synthetic-secret',
                'https://api.example/#secret', 'ftp://api.example', 'https://api.example:70000',
                'https://api.example\\@other.example', 'https://api.%65xample',
                'https://api.example/?', 'https://api.example/#', 'https://api.example/with space']
        for url in urls:
            value = config(); value['model']['base_url'] = url
            with self.subTest(url=url), self.assertRaises(sharing.InvalidDisclosure) as caught:
                build(value)
            self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_operator_origins_must_be_origins_not_api_paths(self):
        declared = declaration()
        declared['routes']['auxiliary.default']['recipients'][0]['origins'] = ['https://api.example/v1']
        with self.assertRaises(sharing.InvalidDisclosure):
            build(downstreams=declared)

    def test_operator_schema_unknown_keys_and_non_boolean_coverage_are_refused(self):
        values = [[], {}, {'schema_version': True, 'routes': {}},
                  {'schema_version': 1, 'routes': {}, 'api_key': 'synthetic-secret'},
                  declaration({'main': {'complete': 'true', 'recipients': [recipient()]}}),
                  declaration({'main': {'complete': True, 'recipients': []}}),
                  declaration({'not-a-route': {'complete': True, 'recipients': [recipient()]}})]
        for value in values:
            with self.subTest(value=value), self.assertRaises(sharing.InvalidDisclosure) as caught:
                build(downstreams=value)
            self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_config_cannot_self_assert_enforcement(self):
        value = config(); value['enforced'] = True
        result = build(value, downstreams=declaration())
        self.assertFalse(result['enforced'])
        self.assertTrue(build(value, downstreams=declaration(), enforced=True)['enforced'])
        with self.assertRaises(sharing.InvalidDisclosure):
            build(value, enforced='true')

    def test_profile_and_session_scope_are_bound_to_revision(self):
        first = build(downstreams=declaration(), session_id='session-one')
        second = build(downstreams=declaration(), session_id='session-two')
        third = sharing.build_disclosure(config(), profile='second', downstreams=declaration(), session_id='session-one')
        self.assertEqual(first['session_id'], 'session-one')
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertNotEqual(first['revision'], third['revision'])
        for profile in ('../owner', '', 'x\nother'):
            with self.assertRaises(sharing.InvalidDisclosure):
                sharing.build_disclosure(config(), profile=profile)

    def test_revision_changes_on_recipient_route_model_and_fallback_order(self):
        first = build(downstreams=declaration())['revision']
        for field, replacement in [('provider', 'deepseek'), ('model', 'second-model'),
                                   ('base_url', 'https://api.openai.com/second-route')]:
            value = config(); value['model'][field] = replacement
            self.assertNotEqual(first, build(value, downstreams=declaration())['revision'])
        declared = declaration(); declared['routes']['auxiliary.default']['recipients'][0]['name'] = 'Renamed provider'
        self.assertNotEqual(first, build(downstreams=declared)['revision'])
        value = config(); value['fallback_providers'] = [{'provider': 'groq'}, {'provider': 'deepseek'}]
        before = build(value)['revision']; value['fallback_providers'].reverse()
        self.assertNotEqual(before, build(value)['revision'])

    def test_revision_and_output_exclude_credentials_everywhere(self):
        first = config()
        first['api_key'] = 'synthetic-first'
        first['model']['api_key'] = 'synthetic-first'
        first['model']['extra_body'] = {'authorization': 'synthetic-first', 'headers': {'X-Key': 'synthetic-first'}}
        first['fallback_model'] = {'provider': 'groq', 'api_key': 'synthetic-first'}
        second = deepcopy(first)
        second['api_key'] = second['model']['api_key'] = second['model']['extra_body']['authorization'] = 'synthetic-second'
        second['model']['extra_body']['headers']['X-Key'] = 'synthetic-second'
        second['fallback_model']['api_key'] = 'synthetic-second'
        one, two = build(first), build(second)
        self.assertEqual(one, two)
        self.assertNotIn('synthetic-', json.dumps(one))
        self.assertNotIn('api_key', json.dumps(one))

    def test_builder_never_mutates_config_or_declarations(self):
        value, declared = config(), declaration()
        original = deepcopy((value, declared))
        build(value, downstreams=declared)
        self.assertEqual((value, declared), original)

    def test_non_secret_routing_controls_change_revision_and_are_opaque(self):
        value = config(); value['model']['extra_body'] = {'provider': {'order': ['first']}}
        first = build(value)
        value['model']['extra_body']['provider']['order'] = ['second']
        self.assertNotEqual(first['revision'], build(value)['revision'])
        self.assertFalse(first['complete'])
        self.assertNotIn('order', json.dumps(first))

    def test_malformed_route_inputs_fail_closed(self):
        values = [[], {'model': 1}, {'model': {'provider': False}}, {'fallback_providers': 'invalid'},
                  {'fallback_model': [None]}, {'auxiliary': []}, {'auxiliary': {'compression': 'invalid'}},
                  {'stt': {'enabled': 1}}, {'stt': {'provider': 'openai', 'openai': []}}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(sharing.InvalidDisclosure):
                build(value)

    def test_route_and_numeric_control_sizes_are_bounded(self):
        value = config(); value['model'].update({f'key{i}': 'x' for i in range(129)})
        with self.assertRaises(sharing.InvalidDisclosure): build(value)
        value = config(); value['model']['extra_body'] = {'limit': 2 ** 100000}
        with self.assertRaises(sharing.InvalidDisclosure): build(value)

    def test_fallback_route_and_control_depth_limits(self):
        value = config(); value['fallback_providers'] = [{'provider': 'openai'}] * (sharing.MAX_CHAIN + 1)
        with self.assertRaises(sharing.InvalidDisclosure): build(value)
        value = config(); value['auxiliary'] = {f'task{i}': {'provider': 'openai'} for i in range(65)}
        with self.assertRaises(sharing.InvalidDisclosure): build(value)
        nested = {'provider': {'order': ['one']}}
        for _ in range(8): nested = {'nested': nested}
        value = config(); value['model']['extra_body'] = nested
        with self.assertRaises(sharing.InvalidDisclosure): build(value)

    def test_duplicate_recipient_origin_budget_is_globally_bounded(self):
        value = config()
        value['auxiliary'] = {f'task{i}': {'provider': 'auto'} for i in range(8)}
        routes = {'auxiliary.' + f'task{i}': {'complete': True, 'recipients': [recipient()] * 40} for i in range(8)}
        with self.assertRaises(sharing.InvalidDisclosure): build(value, downstreams=declaration(routes))


if __name__ == '__main__':
    unittest.main()
