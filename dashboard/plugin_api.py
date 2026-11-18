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

_sharing_name = "agent_companion_sharing_policy_v1"
_sharing_spec = importlib.util.spec_from_file_location(_sharing_name, Path(__file__).with_name("sharing_policy.py"))
sharing_policy = importlib.util.module_from_spec(_sharing_spec)
sys.modules[_sharing_name] = sharing_policy
_sharing_spec.loader.exec_module(sharing_policy)

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


def authorize_operator(request):
    """Policy declarations belong to the local server operator, not any signed-in user.

    Hermes stamps this request-local grant only after redeeming its private,
    same-user native HTTP ticket. Hosted IPC conveys authentication alone and
    cannot establish operator authority; never accept a client-supplied claim.
    """
    authorize(request)
    if _local_operator(request):
        return
    raise HTTPException(403, "Verified local operator authority required.", headers=_NO_STORE)


def _local_operator(request):
    grant = getattr(request.state, 'native_http_principal', None)
    return (type(grant) is dict and grant.get('purpose') == 'native-http'
            and type(grant.get('capabilities')) is frozenset
            and grant['capabilities'] == frozenset({'http:owner'}))


def _profile_operator(request, profile, capability):
    # A published core helper validates the authenticated account, installation,
    # exact profile, expiry and persisted revocation state. The companion never
    # reads or treats a client header/body as an operator claim.
    try:
        from hermes_cli.plugins import operator_authorization
    except ImportError:
        return False
    return operator_authorization(request, profile=profile, capability=capability) is not None


def _profile_scope(request, profile):
    try:
        sharing_policy.profile_name(profile)
    except sharing_policy.InvalidPolicy as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    if request.query_params.getlist('profile') != [profile]:
        raise HTTPException(400, {'code': 'profile_scope_mismatch'}, headers=_NO_STORE)


def authorize_profile_operator(request, profile):
    authorize(request)
    _profile_scope(request, profile)
    if not _profile_operator(request, profile, 'recipient-policy:write'):
        raise HTTPException(403, {'code': 'operator_authority_required'}, headers=_NO_STORE)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


async def _body(request, maximum=state.MAX_BODY_BYTES):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > maximum:
            raise HTTPException(413, "Request is too large.", headers=_NO_STORE)
    try:
        return json.loads(data, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Invalid JSON request.", headers=_NO_STORE) from None


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
    result = state.capabilities()
    result['features']['ai_sharing_metadata'] = True
    result['features']['ai_sharing_profile_setup'] = True
    return result


async def _policy_call(operation, *args):
    try:
        return await run_in_threadpool(operation, *args)
    except sharing_policy.InvalidPolicy as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    except sharing_policy.Conflict as exc:
        raise HTTPException(409, {'code': 'ai_sharing_policy_revision_conflict', 'current': exc.current},
                            headers=_NO_STORE) from None
    except sharing_policy.Unavailable:
        raise HTTPException(503, {'code': 'ai_sharing_policy_unavailable'}, headers=_NO_STORE) from None


@router.get("/ai-sharing", operation_id="agent_companion_ai_sharing")
async def read_ai_sharing(request: Request, response: Response, profile: str = "default",
                          session_id: str | None = None):
    authorize(request)
    response.headers.update(_NO_STORE)
    return await _policy_call(sharing_policy.disclosure, profile, session_id)


@router.get('/ai-sharing/policy', operation_id='agent_companion_read_ai_sharing_policy')
async def read_ai_sharing_policy(request: Request, response: Response):
    authorize(request)
    response.headers.update(_NO_STORE)
    return await _policy_call(sharing_policy.read)


@router.get('/ai-sharing/setup', operation_id='agent_companion_profile_sharing_setup')
async def read_profile_sharing_setup(request: Request, response: Response, profile: str):
    authorize(request)
    response.headers.update(_NO_STORE)
    _profile_scope(request, profile)
    result = await _policy_call(sharing_policy.profile_setup, profile)
    result['can_edit'] = _profile_operator(request, profile, 'recipient-policy:write')
    return result


def _replace_profile_for_request(request, profile, expected, entry):
    # Revalidate after body parsing and thread-pool scheduling, immediately before
    # the scoped storage operation. An earlier pairing/setup receipt is not a lease.
    authorize_profile_operator(request, profile)
    return sharing_policy.replace_profile(expected, profile, entry)


@router.put('/ai-sharing/policy/profiles/{profile}', operation_id='agent_companion_replace_profile_sharing')
async def write_profile_sharing_policy(profile: str, request: Request, response: Response):
    authorize_profile_operator(request, profile)
    response.headers.update(_NO_STORE)
    body = await _body(request, maximum=sharing_policy.MAX_BODY_BYTES)
    try:
        expected, entry = sharing_policy.validate_profile_update(profile, body)
    except sharing_policy.InvalidPolicy as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    result = await _policy_call(_replace_profile_for_request, request, profile, expected, entry)
    result['can_edit'] = _profile_operator(request, profile, 'recipient-policy:write')
    return result


@router.put('/ai-sharing/policy', operation_id='agent_companion_replace_ai_sharing_policy')
async def write_ai_sharing_policy(request: Request, response: Response):
    authorize_operator(request)
    response.headers.update(_NO_STORE)
    body = await _body(request, maximum=sharing_policy.MAX_BODY_BYTES)
    try:
        expected, policy = sharing_policy.validate_update(body)
    except sharing_policy.InvalidPolicy as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    return await _policy_call(sharing_policy.replace, expected, policy)


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
