"""Agent's optional profile metadata API, mounted by Hermes behind its auth gate."""

import importlib.util
import json
import math
from pathlib import Path
import sys
import time

from fastapi import APIRouter, HTTPException, Request, Query, Response
from starlette.concurrency import run_in_threadpool

_name = "agent_companion_state_v1"
_spec = importlib.util.spec_from_file_location(_name, Path(__file__).with_name("state.py"))
state = importlib.util.module_from_spec(_spec)
sys.modules[_name] = state
_spec.loader.exec_module(state)

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
