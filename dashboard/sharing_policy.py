"""Operator-declared AI processor metadata in supported, profile-scoped plugin SQLite.

No runtime configuration, credentials, sessions or native policy hooks are imported.
Reads are shared by authenticated users of the effective Hermes profile; HTTP
writes additionally require Hermes' verified local operator grant.
"""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3

PLUGIN_ID = 'agent-companion-runtime'
MAX_REVISION = 2 ** 53 - 1

# Bind our own local pure helper module: independent dashboard loader instances
# must not change exception identities or metadata validation beneath one another.
_spec = importlib.util.spec_from_file_location(
    'agent_companion_operator_metadata', Path(__file__).with_name('ai_sharing.py'))
metadata = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(metadata)
MAX_BODY_BYTES = metadata.MAX_POLICY_BYTES + 1024
DEFAULT = {'schema_version': 1, 'profiles': {}}


class InvalidPolicy(ValueError):
    pass


class Conflict(ValueError):
    def __init__(self, current):
        super().__init__('AI sharing policy changed; reload before saving.')
        self.current = current


class Unavailable(RuntimeError):
    pass


def revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise InvalidPolicy('Invalid AI sharing policy revision.')
    return value


def validate(value):
    try:
        return metadata.validate_operator_policy(value)
    except metadata.InvalidDisclosure:
        raise InvalidPolicy('Invalid AI sharing policy.') from None


def validate_update(value):
    if type(value) is not dict or set(value) != {'expected_revision', 'policy'}:
        raise InvalidPolicy('Invalid AI sharing policy fields.')
    return revision(value['expected_revision']), validate(value['policy'])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _database():
    # Resolve every call through the public plugin API, never a cached home/path.
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_ID)


def _receipt(row):
    version, current_revision, raw = row
    try:
        if type(version) is not int or version != 1 or type(raw) is not str:
            raise ValueError()
        revision(current_revision)
        policy = validate(json.loads(raw, object_pairs_hook=_unique_object,
                                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    except (ValueError, TypeError, RecursionError):
        raise Unavailable('AI sharing policy storage is unavailable.') from None
    return {'schema_version': 1, 'policy_revision': current_revision, 'policy': policy}


def _read_locked(db):
    db.execute('''CREATE TABLE IF NOT EXISTS companion_ai_sharing_policy (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        schema_version INTEGER NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 0),
        policy_json TEXT NOT NULL)''')
    row = db.execute('''SELECT schema_version,revision,
        CASE WHEN LENGTH(CAST(policy_json AS BLOB)) <= ? THEN policy_json ELSE NULL END
        FROM companion_ai_sharing_policy WHERE singleton=1''', (metadata.MAX_POLICY_BYTES,)).fetchone()
    if row is None:
        db.execute('INSERT INTO companion_ai_sharing_policy VALUES (1,1,0,?)',
                   (json.dumps(DEFAULT, separators=(',', ':')),))
        row = (1, 0, json.dumps(DEFAULT, separators=(',', ':')))
    return _receipt(row)


def read():
    try:
        with closing(_database()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            return _read_locked(db)
    except (ImportError, OSError, sqlite3.Error):
        raise Unavailable('AI sharing policy storage is unavailable.') from None


def replace(expected_revision, policy):
    revision(expected_revision)
    clean = validate(policy)
    try:
        with closing(_database()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = _read_locked(db)
            if current['policy_revision'] != expected_revision:
                db.commit()
                raise Conflict(current)
            if clean == current['policy']:
                return current
            if current['policy_revision'] == MAX_REVISION:
                raise Unavailable('AI sharing policy storage is unavailable.')
            db.execute('UPDATE companion_ai_sharing_policy SET revision=?,policy_json=? WHERE singleton=1',
                       (current['policy_revision'] + 1, json.dumps(clean, ensure_ascii=False, separators=(',', ':'))))
            return {'schema_version': 1, 'policy_revision': current['policy_revision'] + 1, 'policy': clean}
    except (ImportError, OSError, sqlite3.Error):
        raise Unavailable('AI sharing policy storage is unavailable.') from None


def disclosure(profile, session_id=None):
    # Validate scope before opening storage. Metadata does not authorize or inspect a session.
    try:
        metadata.operator_profile(profile)
        if session_id is not None:
            metadata.operator_session(session_id)
    except metadata.InvalidDisclosure:
        raise InvalidPolicy('Invalid AI sharing metadata scope.') from None
    return metadata.build_operator_disclosure(read()['policy'], profile=profile, session_id=session_id)
