"""Same-user operator commands through Hermes' normal plugin CLI registration."""
import argparse
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'agent_companion_cli_policy', Path(__file__).parent / 'dashboard' / 'sharing_policy.py')
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


def register_cli(parser):
    actions = parser.add_subparsers(dest='agent_sharing_action', required=True)
    actions.add_parser('show', help='Read this Hermes profile\'s AI recipient declaration')
    update = actions.add_parser('set', help='Save a reviewed declaration with its current revision')
    update.add_argument('--file', required=True, help='UTF-8 JSON policy file')
    update.add_argument('--expected-revision', required=True, type=int)


def _read_policy(path):
    try:
        with Path(path).open('rb') as source:
            raw = source.read(policy.metadata.MAX_POLICY_BYTES + 1)
        if len(raw) > policy.metadata.MAX_POLICY_BYTES:
            raise ValueError()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=policy._unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return policy.validate(value)
    except (OSError, ValueError, TypeError, RecursionError):
        raise policy.InvalidPolicy('Invalid AI sharing policy file.') from None


def command(args):
    """Local filesystem authority; no network, tickets, models, or runtime changes."""
    try:
        action = getattr(args, 'agent_sharing_action', None)
        if action == 'show':
            result = policy.read()
        elif action == 'set':
            expected = policy.revision(args.expected_revision)
            result = policy.replace(expected, _read_policy(args.file))
        else:
            print(json.dumps({'ok': False, 'code': 'invalid_operation'}))
            return 2
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
        return 0
    except policy.Conflict as error:
        print(json.dumps({'ok': False, 'code': 'ai_sharing_policy_revision_conflict',
                          'policy_revision': error.current['policy_revision']}))
    except policy.InvalidPolicy:
        print(json.dumps({'ok': False, 'code': 'invalid_policy'}))
    except policy.Unavailable:
        print(json.dumps({'ok': False, 'code': 'policy_unavailable'}))
    return 1
