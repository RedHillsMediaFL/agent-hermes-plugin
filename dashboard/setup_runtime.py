"""Read-only compatibility-install guidance; never mutates a live Hermes runtime.

Stock Hermes has no plugin contract that can prove every serving process drained
before source replacement. The compatible initial installation is therefore an
explicit prerequisite; the app still installs this plugin through the normal manager.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

TARGET_ID = "agent-native-v1"
_TARGET_FILE = Path(__file__).with_name("runtime_target.json")


class SetupError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def target():
    value = json.loads(_TARGET_FILE.read_text())
    if (set(value) != {"id", "repository", "branch", "revision", "reviewed_upstream_version"}
            or value["id"] != TARGET_ID):
        raise SetupError("invalid_reviewed_target")
    ready = (isinstance(value["revision"], str) and re.fullmatch(r"[0-9a-f]{40}", value["revision"])
             and value["repository"] == "https://github.com/RedHillsMediaFL/hermes-agent"
             and value["branch"] == "agent-companion-v1")
    return {**value, "reviewed": bool(ready)}


def validate_request(body):
    if not isinstance(body, dict) or set(body) != {"operation_id", "target"} or body["target"] != TARGET_ID:
        raise SetupError("invalid_setup_request")
    try:
        operation = str(uuid.UUID(body["operation_id"]))
    except (ValueError, TypeError, AttributeError):
        raise SetupError("invalid_setup_request") from None
    if operation != body["operation_id"]:
        raise SetupError("invalid_setup_request")
    return operation


def _installation():
    # The supported plugin API does not identify or control the serving runtime's
    # supervisor. Leave that decision with its owner instead of guessing from
    # private modules, paths, process ledgers or installation heuristics.
    return {"mode": "owner_managed", "reason": "initial_compatible_install_required"}


def status():
    selected = target()
    installation = _installation()
    reason = installation["reason"] if selected["reviewed"] else "reviewed_release_unavailable"
    return {
        "ok": True, "schema_version": 1, "target": selected,
        "admission": {"can_update": False, "reason": reason},
        "installation": {"mode": installation["mode"], "initial_install_required": True,
                         "restart_required": True, "automatic_update": False},
        "job": None,
    }


def queue(body):
    """Keep the closed endpoint fail-closed, including retries of an old client intent."""
    validate_request(body)
    raise SetupError(status()["admission"]["reason"])
