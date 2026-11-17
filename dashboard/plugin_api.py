"""Agent's optional profile metadata API, mounted by Hermes behind its auth gate."""

import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

from fastapi import APIRouter, HTTPException, Request, Query, Response
from starlette.concurrency import run_in_threadpool

_name = "agent_companion_runtime_state_v1"
_spec = importlib.util.spec_from_file_location(_name, Path(__file__).with_name("state.py"))
state = importlib.util.module_from_spec(_spec)
sys.modules[_name] = state
_spec.loader.exec_module(state)

_runtime_name = "agent_companion_runtime_setup_v1"
_runtime_spec = importlib.util.spec_from_file_location(_runtime_name, Path(__file__).with_name("setup_runtime.py"))
runtime_setup = importlib.util.module_from_spec(_runtime_spec)
sys.modules[_runtime_name] = runtime_setup
_runtime_spec.loader.exec_module(runtime_setup)

_sharing_name = "agent_companion_ai_sharing_v1"
_sharing_spec = importlib.util.spec_from_file_location(_sharing_name, Path(__file__).with_name("ai_sharing.py"))
ai_sharing = importlib.util.module_from_spec(_sharing_spec)
sys.modules[_sharing_name] = ai_sharing
_sharing_spec.loader.exec_module(ai_sharing)

router = APIRouter(tags=["Agent companion"])
_NO_STORE = {"Cache-Control": "no-store"}


def authorize(request):
    # This is Hermes' own request-local IPC admission, never a client header or owner ID.
    hosted = getattr(request.state, "hosted_http_authenticated", None)
    if hosted is not None:
        if hosted is not True:
            raise HTTPException(401, "Authentication required.", headers=_NO_STORE)
        return
    # These fields are populated by Hermes' verified outer middleware, not by a
    # plugin-parsed header. No core module is imported or mutated to obtain them.
    if getattr(request.state, "native_http_principal", None) is not None:
        return
    session = getattr(request.state, "session", None)
    expires = getattr(session, "expires_at", None)
    if (session is not None and isinstance(getattr(session, "provider", None), str)
            and session.provider.strip() and isinstance(getattr(session, "user_id", None), str)
            and session.user_id.strip() and type(expires) in (int, float)
            and math.isfinite(expires) and expires > time.time()):
        return
    raise HTTPException(401, "Authentication required.", headers=_NO_STORE)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


async def _body(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > state.MAX_BODY_BYTES:
            raise HTTPException(413, "Settings request is too large.", headers=_NO_STORE)
    try:
        return json.loads(data, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Invalid settings JSON.", headers=_NO_STORE) from None


async def _call(operation, *args):
    try:
        return await run_in_threadpool(operation, *args)
    except state.InvalidSettings as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    except state.Conflict as exc:
        raise HTTPException(409, {"code": "settings_revision_conflict", "current": exc.current}, headers=_NO_STORE) from None
    except state.Unavailable:
        raise HTTPException(503, "Settings storage is unavailable.", headers=_NO_STORE) from None


@router.get("/capabilities", operation_id="agent_companion_capabilities")
async def read_capabilities(request: Request, response: Response):
    authorize(request)
    response.headers.update(_NO_STORE)
    return state.capabilities()


_AI_OPERATIONS = frozenset({"chat", "reply", "task", "transcription", "group", "automation",
                            "command", "response", "agent_draft", "personal_context"})


def _recipient_disclosure(request, profile, session_id, room_id, operation):
    profile = ai_sharing._scope(profile)
    if session_id is not None:
        session_id = ai_sharing._scope(session_id)
    if room_id is not None:
        ai_sharing._scope(room_id)
    if operation not in _AI_OPERATIONS:
        raise ai_sharing.InvalidDisclosure()
    try:
        # This is an explicit compatibility-runtime contract, not a monkeypatch
        # or an assumption that an installed plugin enforces provider dispatch.
        from hermes_cli.ai_sharing_policy import read_disclosure
    except ImportError:
        result = ai_sharing.build_disclosure({}, profile=profile, session_id=session_id)
        result["enforced_operations"] = []
        return result
    result = read_disclosure(request, profile=profile, session_id=session_id,
                             room_id=room_id, operation=operation)
    allowed = {"schema_version", "revision", "complete", "enforced", "profile", "session_id",
               "recipients", "unavailable_reason", "enforced_operations"}
    if (type(result) is not dict or set(result) - allowed or result.get("schema_version") != 1
            or result.get("profile") != profile or result.get("session_id") != session_id
            or type(result.get("complete")) is not bool or type(result.get("enforced")) is not bool
            or type(result.get("revision")) is not str
            or len(result["revision"]) != 64
            or any(c not in "0123456789abcdef" for c in result["revision"])
            or type(result.get("enforced_operations")) is not list
            or any(type(value) is not str for value in result["enforced_operations"])
            or not set(result["enforced_operations"]) <= _AI_OPERATIONS
            or len(result["enforced_operations"]) > len(_AI_OPERATIONS)
            or len(set(result["enforced_operations"])) != len(result["enforced_operations"])
            or type(result.get("recipients")) is not list or len(result["recipients"]) > 64):
        raise ai_sharing.InvalidDisclosure()
    for recipient in result["recipients"]:
        if (type(recipient) is not dict or set(recipient) != {"id", "name", "origins", "purposes"}
                or recipient["purposes"] != list(ai_sharing.PURPOSES)):
            raise ai_sharing.InvalidDisclosure()
        ai_sharing._recipient({key: recipient[key] for key in ("id", "name", "origins")})
    if len({value["id"] for value in result["recipients"]}) != len(result["recipients"]):
        raise ai_sharing.InvalidDisclosure()
    return result


@router.get("/ai-sharing", operation_id="agent_companion_ai_sharing")
async def read_ai_sharing(request: Request, response: Response, profile: str = "default",
                          session_id: str | None = None, room_id: str | None = None,
                          operation: str = "chat"):
    authorize(request)
    response.headers.update(_NO_STORE)
    try:
        return await run_in_threadpool(_recipient_disclosure, request, profile, session_id, room_id, operation)
    except ai_sharing.InvalidDisclosure:
        raise HTTPException(503, {"code": "ai_recipient_disclosure_unavailable"}, headers=_NO_STORE) from None


@router.get("/settings", operation_id="agent_companion_settings")
async def read_settings(request: Request, response: Response):
    authorize(request)
    response.headers.update(_NO_STORE)
    return await _call(state.read)


@router.put("/settings", operation_id="agent_companion_replace_settings")
async def write_settings(request: Request, response: Response):
    authorize(request)
    response.headers.update(_NO_STORE)
    body = await _body(request)
    try:
        expected, settings = state.validate_update(body)
    except state.InvalidSettings as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    return await _call(state.replace, expected, settings)


@router.delete("/settings", operation_id="agent_companion_reset_settings")
async def clear_settings(request: Request, response: Response,
                         expected_revision: int = Query(..., ge=0, le=state.MAX_REVISION)):
    authorize(request)
    response.headers.update(_NO_STORE)
    return await _call(state.reset, expected_revision)


async def _runtime_call(operation, *args):
    try:
        return await run_in_threadpool(operation, *args)
    except runtime_setup.SetupError as exc:
        status = 400 if exc.code == "invalid_setup_request" else 409
        raise HTTPException(status, {"code": exc.code}, headers=_NO_STORE) from None
    except (OSError, ValueError, sqlite3.Error):
        raise HTTPException(503, {"code": "setup_storage_unavailable"}, headers=_NO_STORE) from None


@router.get("/setup/runtime", operation_id="agent_companion_runtime_setup_status")
async def runtime_setup_status(request: Request, response: Response):
    authorize(request)
    response.headers.update(_NO_STORE)
    return await _runtime_call(runtime_setup.status)


@router.post("/setup/runtime", operation_id="agent_companion_runtime_setup_start")
async def runtime_setup_start(request: Request, response: Response):
    authorize(request)
    response.headers.update(_NO_STORE)
    body = await _body(request)
    return await _runtime_call(runtime_setup.queue, body)
