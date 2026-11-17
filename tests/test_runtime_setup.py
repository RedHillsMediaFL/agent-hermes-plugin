"""Read-only setup guidance and refusal contracts; no runtime, network or source writes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi import FastAPI
import httpx

from test_companion import api

setup = api.runtime_setup
SELECTED = {"id": "agent-native-v1", "repository": "https://github.com/RedHillsMediaFL/hermes-agent",
            "branch": "agent-companion-v1", "revision": "a" * 40, "reviewed_upstream_version": "0.23.2", "reviewed": True}


class RuntimeSetupTests(unittest.TestCase):
    def test_every_installation_shape_refuses_automatic_mutation(self):
        for mode, reason in [("source", "initial_compatible_install_required"), ("source", "local_changes_present"),
                             ("managed", "externally_managed_install"), ("unknown", "runtime_drain_contract_unavailable")]:
            with self.subTest(mode=mode, reason=reason), patch.object(setup, "target", return_value=deepcopy(SELECTED)), patch.object(setup, "_installation", return_value={"mode": mode, "reason": reason}):
                result = setup.status()
                self.assertFalse(result["admission"]["can_update"])
                self.assertFalse(result["installation"]["automatic_update"])
                self.assertIsNone(result["job"])
                body = {"operation_id": str(uuid.uuid4()), "target": "agent-native-v1"}
                for _ in range(2):
                    with self.assertRaises(setup.SetupError) as error:
                        setup.queue(body)
                    self.assertEqual(error.exception.code, reason)

    def test_unknown_fields_and_bad_operation_ids_are_rejected(self):
        for field in ["repository", "revision", "command", "owner", "profile"]:
            body = {"operation_id": str(uuid.uuid4()), "target": "agent-native-v1", field: "synthetic-private-input"}
            with self.subTest(field=field), self.assertRaises(setup.SetupError):
                setup.validate_request(body)
        for operation in [True, 0, None, "../job", "not-a-uuid", str(uuid.uuid4()).upper()]:
            with self.subTest(operation=operation), self.assertRaises(setup.SetupError):
                setup.validate_request({"operation_id": operation, "target": "agent-native-v1"})

    def test_corrupt_target_does_not_become_a_reviewed_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "target.json"
            value = {k: v for k, v in SELECTED.items() if k != "reviewed"}
            value["repository"] = "https://github.com/foreign-owner/runtime"
            path.write_text(json.dumps(value))
            with patch.object(setup, "_TARGET_FILE", path):
                self.assertFalse(setup.target()["reviewed"])
            value["repository"] = SELECTED["repository"]; value["revision"] = None
            path.write_text(json.dumps(value))
            with patch.object(setup, "_TARGET_FILE", path):
                self.assertFalse(setup.target()["reviewed"])

    def test_inspection_never_spawns_a_process_or_opens_a_runtime_store(self):
        with patch.object(setup, "target", return_value=deepcopy(SELECTED)), patch("subprocess.run", side_effect=AssertionError("Setup inspection cannot execute commands")), patch("sqlite3.connect", side_effect=AssertionError("Setup inspection cannot open runtime state")):
            result = setup.status()
        self.assertEqual(result["installation"]["mode"], "owner_managed")
        self.assertFalse(result["admission"]["can_update"])


class RuntimeAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admitted = True
        self.patches = [patch.object(setup, "target", return_value=deepcopy(SELECTED)),
                        patch.object(setup, "_installation", return_value={"mode": "source", "reason": "initial_compatible_install_required"})]
        for value in self.patches: value.start()
        app = FastAPI()
        @app.middleware("http")
        async def admission(request, call_next):
            request.state.hosted_http_authenticated = self.admitted
            return await call_next(request)
        app.include_router(api.router, prefix="/api/plugins/agent-companion-runtime")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid")
        self.path = "/api/plugins/agent-companion-runtime/setup/runtime"

    async def asyncTearDown(self):
        await self.client.aclose()
        for value in reversed(self.patches): value.stop()

    async def test_auth_precedes_body_and_inspection(self):
        self.admitted = False
        with patch.object(setup, "_installation", side_effect=AssertionError("Unadmitted requests cannot inspect the runtime")):
            for method in ["GET", "POST"]:
                response = await self.client.request(method, self.path, content=b"not-json", headers={"X-Hermes-Hosted-Authenticated": "true"})
                self.assertEqual(response.status_code, 401)

    async def test_status_explains_the_initial_installer_and_restart_prerequisite(self):
        response = await self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(response.json()["admission"]["can_update"])
        self.assertTrue(response.json()["installation"]["initial_install_required"])
        self.assertTrue(response.json()["installation"]["restart_required"])

    async def test_repeated_post_refuses_with_bounded_code(self):
        body = {"operation_id": str(uuid.uuid4()), "target": "agent-native-v1"}
        for _ in range(2):
            response = await self.client.post(self.path, json=body)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json(), {"detail": {"code": "initial_compatible_install_required"}})
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_injected_commands_are_rejected_without_echo(self):
        response = await self.client.post(self.path, json={"operation_id": str(uuid.uuid4()), "target": "agent-native-v1", "command": "synthetic-private-command"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("synthetic-private-command", response.text)


if __name__ == "__main__":
    unittest.main()
