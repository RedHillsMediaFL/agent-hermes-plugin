"""Isolated contract tests: temporary plugin data, synthetic authentication, no network."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("agent_companion_test_api", ROOT / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = api
_spec.loader.exec_module(api)
state = api.state


class StorageFixture:
    def start_storage(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agent-companion-runtime-tests-")
        self.home = ContextVar("test_profile_home", default="alpha")
        self.opens = []

        def plugin_db(name, filename="data.db"):
            self.opens.append((self.home.get(), name, filename))
            directory = Path(self.directory.name) / self.home.get() / name
            directory.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(directory / filename, timeout=10)
            db.execute("PRAGMA foreign_keys=ON")
            return db

        package, storage = ModuleType("plugins"), ModuleType("plugins.plugin_storage")
        package.__path__ = []
        storage.plugin_db = plugin_db
        self.modules = patch.dict(sys.modules, {"plugins": package, "plugins.plugin_storage": storage})
        self.modules.start()

    def stop_storage(self):
        self.modules.stop()
        self.directory.cleanup()


class StorageTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        self.start_storage()

    def tearDown(self):
        self.stop_storage()

    def test_initial_state_is_neutral_and_stable(self):
        first, second = state.read(), state.read()
        self.assertEqual(first, second)
        self.assertEqual(first["revision"], 0)
        self.assertEqual(first["settings"], state.DEFAULTS)
        self.assertIsNone(first["settings"]["roles"]["primary_profile"])
        self.assertEqual(first["settings_scope"], "hermes_profile")

    def test_changes_increment_revision_and_stale_save_is_rejected(self):
        before = state.read()
        settings = deepcopy(state.DEFAULTS)
        settings["roles"]["primary_profile"] = "writer"
        after = state.replace(0, settings)
        self.assertEqual(after["revision"], 1)
        self.assertEqual(after["installation_id"], before["installation_id"])
        with self.assertRaises(state.Conflict) as error:
            state.replace(0, state.DEFAULTS)
        self.assertEqual(error.exception.current, after)
        self.assertEqual(state.read(), after)

    def test_identical_write_is_idempotent(self):
        initial = state.read()
        self.assertEqual(state.replace(0, deepcopy(state.DEFAULTS)), initial)

    def test_stale_first_write_does_not_rotate_installation_identity(self):
        with self.assertRaises(state.Conflict) as error:
            state.replace(9, state.DEFAULTS)
        self.assertEqual(state.read(), error.exception.current)

    def test_profile_storage_resolves_on_each_call(self):
        first = state.read()
        token = self.home.set("beta")
        try:
            second = state.read()
            settings = deepcopy(state.DEFAULTS)
            settings["connection"]["display_name"] = "Second server"
            state.replace(0, settings)
        finally:
            self.home.reset(token)
        self.assertNotEqual(first["installation_id"], second["installation_id"])
        self.assertEqual(state.read(), first)
        self.assertEqual({entry[0] for entry in self.opens}, {"alpha", "beta"})

    def test_reset_preserves_installation_identity(self):
        initial = state.read()
        changed = deepcopy(state.DEFAULTS)
        changed["ui_preferences"]["default_destination"] = "chats"
        state.replace(0, changed)
        reset = state.reset(1)
        self.assertEqual(reset["installation_id"], initial["installation_id"])
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(reset["settings"], state.DEFAULTS)

    def test_concurrent_writes_do_not_silently_overwrite(self):
        state.read()

        def save(label):
            settings = deepcopy(state.DEFAULTS)
            settings["connection"]["display_name"] = label
            try:
                return state.replace(0, settings)["revision"]
            except state.Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["First", "Second"]))
        self.assertCountEqual(results, [1, "conflict"])

    def test_corrupt_existing_data_is_not_replaced(self):
        state.read()
        path = Path(self.directory.name) / "alpha/agent-companion-runtime/data.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE companion_settings SET settings_json='invalid-json'")
        with self.assertRaises(state.Unavailable):
            state.read()
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute("SELECT settings_json FROM companion_settings").fetchone()[0], "invalid-json")

    def test_settings_have_a_closed_shape(self):
        invalid = []
        value = deepcopy(state.DEFAULTS); value["token"] = "not-a-supported-field"; invalid.append(value)
        value = deepcopy(state.DEFAULTS); value["roles"]["background_profiles"] = ["../other"]; invalid.append(value)
        value = deepcopy(state.DEFAULTS); value["roles"] = {"primary_profile": "worker", "background_profiles": ["worker"]}; invalid.append(value)
        value = deepcopy(state.DEFAULTS); value["roles"]["background_profiles"] = ["a", "a"]; invalid.append(value)
        value = deepcopy(state.DEFAULTS); value["connection"]["display_name"] = "bad\nlabel"; invalid.append(value)
        value = deepcopy(state.DEFAULTS); value["ui_preferences"]["show_background_agents"] = 1; invalid.append(value)
        value = deepcopy(state.DEFAULTS); value["ui_preferences"]["default_destination"] = "other"; invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(state.InvalidSettings):
                state.replace(0, value)
        self.assertEqual(self.opens, [])

    def test_revision_is_strict(self):
        for value in [True, -1, 0.0, "0", state.MAX_REVISION + 1]:
            with self.subTest(value=value), self.assertRaises(state.InvalidSettings):
                state.replace(value, state.DEFAULTS)

    def test_capabilities_never_imply_optional_backends_exist(self):
        value = state.capabilities()
        self.assertFalse(value["core_chat_requires_plugin"])
        self.assertFalse(value["features"]["canonical_session_resolution"])
        self.assertFalse(value["features"]["durable_reply_linkage"])
        self.assertFalse(value["features"]["remote_push"])
        self.assertFalse(value["features"]["account_scoped_preferences"])


class APITests(StorageFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.start_storage()
        self.admitted = True
        app = FastAPI()

        @app.middleware("http")
        async def host_admission(request, call_next):
            request.state.hosted_http_authenticated = self.admitted
            return await call_next(request)

        app.include_router(api.router, prefix="/api/plugins/agent-companion-runtime")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")
        self.base = "/api/plugins/agent-companion-runtime"

    async def asyncTearDown(self):
        await self.client.aclose()
        self.stop_storage()

    async def test_get_put_conflict_reset_and_no_cache(self):
        before = await self.client.get(self.base + "/settings")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.headers["cache-control"], "no-store")
        settings = deepcopy(before.json()["settings"])
        settings["roles"]["primary_profile"] = "assistant"
        after = await self.client.put(self.base + "/settings", json={"expected_revision": 0, "settings": settings})
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.json()["revision"], 1)
        stale = await self.client.put(self.base + "/settings", json={"expected_revision": 0, "settings": settings})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "settings_revision_conflict")
        reset = await self.client.delete(self.base + "/settings", params={"expected_revision": 1})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["settings"], state.DEFAULTS)

    async def test_authentication_precedes_body_or_storage(self):
        self.admitted = False
        for method, path in [("GET", "/capabilities"), ("GET", "/settings"), ("PUT", "/settings"), ("DELETE", "/settings?expected_revision=0")]:
            response = await self.client.request(method, self.base + path, content=b"not-json", headers={"X-Hermes-Hosted-Authenticated": "true"})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.opens, [])

    async def test_unknown_and_duplicate_fields_are_rejected_without_echo(self):
        response = await self.client.put(self.base + "/settings", content=b'{"expected_revision":0,"expected_revision":1,"settings":{}}')
        self.assertEqual(response.status_code, 400)
        response = await self.client.put(self.base + "/settings", json={"expected_revision": 0, "settings": state.DEFAULTS, "owner": "arbitrary-identity"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("arbitrary-identity", response.text)
        self.assertEqual(self.opens, [])

    async def test_oversized_body_rejected_before_storage(self):
        response = await self.client.put(self.base + "/settings", content=b" " * (state.MAX_BODY_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.opens, [])

    async def test_capabilities_are_available_without_creating_state(self):
        response = await self.client.get(self.base + "/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema_version"], 1)
        self.assertEqual(self.opens, [])


class InProcessAuthTests(unittest.TestCase):
    def test_in_process_requires_verified_context(self):
        request = SimpleNamespace(state=SimpleNamespace())
        with self.assertRaises(HTTPException):
            api.authorize(request)
        request.state.session = SimpleNamespace(provider="fixture", user_id="synthetic-user", expires_at=time.time() + 60)
        api.authorize(request)
        request.state.session.expires_at = time.time() - 60
        with self.assertRaises(HTTPException):
            api.authorize(request)

    def test_in_process_native_owner_is_accepted_without_core_import(self):
        request = SimpleNamespace(state=SimpleNamespace(native_http_principal=object()))
        api.authorize(request)


if __name__ == "__main__":
    unittest.main()
