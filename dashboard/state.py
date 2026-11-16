"""Closed, profile-scoped display settings in Hermes' supported plugin storage."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import re
import sqlite3
import uuid

PLUGIN_ID = "agent-companion"
VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_BODY_BYTES = 16_384
MAX_REVISION = 2**53 - 1
MAX_PROFILES = 64
PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

DEFAULTS = {
    "connection": {"display_name": None},
    "roles": {"primary_profile": None, "background_profiles": []},
    "ui_preferences": {"default_destination": "agents", "show_background_agents": False},
}


class InvalidSettings(ValueError):
    """The closed public settings contract was not met."""


class Conflict(ValueError):
    def __init__(self, current):
        super().__init__("Settings changed; reload before saving.")
        self.current = current


class Unavailable(RuntimeError):
    """Existing settings cannot be read safely."""


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise InvalidSettings("Invalid settings fields.")


def revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise InvalidSettings("Invalid settings revision.")
    return value


def _profile(value, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not PROFILE_ID.fullmatch(value):
        raise InvalidSettings("Invalid profile identifier.")
    return value


def validate(value):
    """Reject unknown data, coercions, secret-shaped free-form settings and role overlap."""
    _object(value, DEFAULTS)
    connection, roles, ui = value["connection"], value["roles"], value["ui_preferences"]
    _object(connection, DEFAULTS["connection"])
    _object(roles, DEFAULTS["roles"])
    _object(ui, DEFAULTS["ui_preferences"])
    label = connection["display_name"]
    if label is not None:
        if (not isinstance(label, str) or not 1 <= len(label) <= 80
                or label != label.strip() or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            raise InvalidSettings("Use a display name of 1 to 80 characters.")
    primary = _profile(roles["primary_profile"], nullable=True)
    background = roles["background_profiles"]
    if not isinstance(background, list) or len(background) > MAX_PROFILES:
        raise InvalidSettings("Invalid background profile list.")
    for profile in background:
        _profile(profile)
    if len(set(background)) != len(background) or primary in background:
        raise InvalidSettings("Profile roles must be distinct.")
    if ui["default_destination"] not in ("agents", "chats") or type(ui["show_background_agents"]) is not bool:
        raise InvalidSettings("Invalid interface preferences.")
    return deepcopy(value)


def validate_update(value):
    _object(value, ["expected_revision", "settings"])
    return revision(value["expected_revision"]), validate(value["settings"])


def capabilities():
    """Describe only implemented plugin features; never infer upstream runtime abilities."""
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "plugin": {"id": PLUGIN_ID, "version": VERSION},
        "minimum_hermes_version": "0.23.2",
        "settings_scope": "hermes_profile",
        "core_chat_requires_plugin": False,
        "features": {
            "connection_metadata": True,
            "profile_roles": True,
            "ui_preferences": True,
            "account_scoped_preferences": False,
            "canonical_session_resolution": False,
            "durable_reply_linkage": False,
            "groups": False,
            "remote_push": False,
            "live_activities": False,
            "life_context": False,
            "usage_upload": False,
        },
    }


def _database():
    # Resolve on EVERY call: Hermes changes the effective home for profile-routed requests.
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_ID)


def _receipt(row):
    version, installation, current_revision, raw = row
    if version != SCHEMA_VERSION or type(version) is not int:
        raise Unavailable("Settings storage is unavailable.")
    try:
        if str(uuid.UUID(installation)) != installation:
            raise ValueError()
        revision(current_revision)
        settings = validate(json.loads(raw))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        raise Unavailable("Settings storage is unavailable.") from None
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "settings_scope": "hermes_profile",
        "installation_id": installation,
        "revision": current_revision,
        "settings": settings,
    }


def _read_locked(db):
    db.execute("""CREATE TABLE IF NOT EXISTS companion_settings (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        schema_version INTEGER NOT NULL, installation_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 0), settings_json TEXT NOT NULL
    )""")
    row = db.execute("SELECT schema_version,installation_id,revision,settings_json FROM companion_settings WHERE singleton=1").fetchone()
    if row is None:
        db.execute("INSERT INTO companion_settings VALUES (1,?,?,0,?)", (
            SCHEMA_VERSION, str(uuid.uuid4()), json.dumps(DEFAULTS, separators=(",", ":")),
        ))
        row = db.execute("SELECT schema_version,installation_id,revision,settings_json FROM companion_settings WHERE singleton=1").fetchone()
    return _receipt(row)


def read():
    try:
        with closing(_database()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return _read_locked(db)
    except (sqlite3.Error, OSError):
        raise Unavailable("Settings storage is unavailable.") from None


def replace(expected_revision, settings):
    revision(expected_revision)
    clean = validate(settings)
    try:
        with closing(_database()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = _read_locked(db)
            if current["revision"] != expected_revision:
                # A stale first request may have initialized the installation identity.
                # Keep that identity stable without applying the rejected settings.
                db.commit()
                raise Conflict(current)
            if clean == current["settings"]:
                return current
            if current["revision"] == MAX_REVISION:
                raise Unavailable("Settings storage is unavailable.")
            db.execute("UPDATE companion_settings SET revision=?,settings_json=? WHERE singleton=1", (
                current["revision"] + 1, json.dumps(clean, separators=(",", ":")),
            ))
            row = db.execute("SELECT schema_version,installation_id,revision,settings_json FROM companion_settings WHERE singleton=1").fetchone()
            return _receipt(row)
    except (sqlite3.Error, OSError):
        raise Unavailable("Settings storage is unavailable.") from None


def reset(expected_revision):
    return replace(expected_revision, DEFAULTS)
