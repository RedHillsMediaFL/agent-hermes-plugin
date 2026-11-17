"""Read-only AI recipient disclosure from an already sanitized configuration snapshot.

No provider resolution, environment reads, credential access or dispatch hooks occur here.
``complete`` describes the supplied declarations; ``enforced`` is a separate native-runtime
attestation and defaults to False. An operator declaration alone cannot enable enforcement.

Operator format::

    {"schema_version": 1, "routes": {
        "main": {"complete": True, "recipients": [
            {"id": "publisher-ai", "name": "Publisher AI",
             "origins": ["https://ai.example.com"]}]},
        "auxiliary.default": {...}, "stt": {...}}}

Route IDs are ``main``, ``main.fallback_providers[i]``, ``main.fallback_model[i]``,
``auxiliary.<task>``, ``auxiliary.<task>.fallback_chain[i]`` and ``stt`` (plus
equivalent explicitly configured STT fallback chains). Missing auxiliary defaults
remain a separate ``auxiliary.default`` route; missing/enabled auto STT remains ``stt``.
Every named recipient gets two distinct purpose entries, never a combined consent grant.
"""

import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

PURPOSES = ("manual_ai", "automatic_personal_context")
MAX_ROUTES = 128
MAX_RECIPIENTS = 64
MAX_CHAIN = 16
MAX_ORIGINS = 16
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECRET = re.compile(r"(?:key|token|secret|password|authorization|cookie|credential|headers?|auth)", re.I)
_AUX_META = frozenset({"transient_retries", "free_only", "openrouter_model", "stream_only_base_urls"})

# Public direct API identities only. A local model name is never recipient evidence.
# Callers can inject audited public registry metadata instead; this module imports no runtime registry.
PUBLIC_PROVIDERS = {
    "openai": {"id": "openai", "name": "OpenAI", "origins": ["https://api.openai.com"]},
    "openai-api": {"id": "openai", "name": "OpenAI", "origins": ["https://api.openai.com"]},
    "openai-codex": {"id": "openai", "name": "OpenAI", "origins": ["https://chatgpt.com"]},
    "anthropic": {"id": "anthropic", "name": "Anthropic", "origins": ["https://api.anthropic.com"]},
    "deepseek": {"id": "deepseek", "name": "DeepSeek", "origins": ["https://api.deepseek.com"]},
    "groq": {"id": "groq", "name": "Groq", "origins": ["https://api.groq.com"]},
    "mistral": {"id": "mistral", "name": "Mistral AI", "origins": ["https://api.mistral.ai"]},
    "xai": {"id": "xai", "name": "xAI", "origins": ["https://api.x.ai"]},
    "elevenlabs": {"id": "elevenlabs", "name": "ElevenLabs", "origins": ["https://api.elevenlabs.io"]},
    "openrouter": {"id": "openrouter", "name": "OpenRouter", "origins": ["https://openrouter.ai"], "opaque": True},
}


class InvalidDisclosure(ValueError):
    """Malformed/over-limit input; messages deliberately omit all input values."""


def _require(value):
    if not value:
        raise InvalidDisclosure("Invalid or oversized AI sharing disclosure configuration.")


def _text(value, limit=512, *, empty=True):
    _require(type(value) is str and len(value) <= limit and
             not any(ord(c) < 32 or ord(c) == 127 for c in value))
    value = value.strip()
    _require(empty or bool(value))
    return value


def _scope(value):
    value = _text(value, 128, empty=False)
    _require(_SCOPE.fullmatch(value) is not None and value not in {".", ".."})
    return value


def _url(value, *, origin_only=False):
    value = _text(value, 2048, empty=False)
    _require(not any(c.isspace() for c in value) and not any(c in value for c in "\\?#"))
    try:
        parts = urlsplit(value)
        _require(parts.scheme.lower() in {"http", "https"} and parts.hostname is not None and
                 parts.username is None and parts.password is None and not parts.query and not parts.fragment)
        host = parts.hostname
        _require("%" not in host)
        try:
            parsed_ip = ipaddress.ip_address(host)
            host = '[' + parsed_ip.compressed + ']' if parsed_ip.version == 6 else parsed_ip.compressed
        except ValueError:
            host = host.encode('idna').decode('ascii').lower()
            _require(len(host) <= 253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                            for label in host.rstrip('.').split('.')))
        port = parts.port
        _require(port is None or 1 <= port <= 65535)
        if (parts.scheme.lower(), port) in {('http', 80), ('https', 443)}:
            port = None
        origin = parts.scheme.lower() + '://' + host + ((':' + str(port)) if port else '')
        _require(not origin_only or parts.path in {'', '/'})
        return origin, origin + parts.path.rstrip('/')
    except (ValueError, UnicodeError):
        raise InvalidDisclosure("Invalid or oversized AI sharing disclosure configuration.") from None


def _recipient(value):
    _require(type(value) is dict and set(value) == {'id', 'name', 'origins'})
    identifier = _text(value['id'], 64, empty=False)
    _require(_ID.fullmatch(identifier) is not None)
    name = _text(value['name'], 128, empty=False)
    origins = value['origins']
    _require(type(origins) is list and 1 <= len(origins) <= MAX_ORIGINS)
    return {'id': identifier, 'name': name,
            'origins': sorted({_url(x, origin_only=True)[0] for x in origins}),
            'purposes': list(PURPOSES)}


def _registry(value):
    _require(type(value) is dict and len(value) <= 128)
    result = {}
    for provider, record in value.items():
        provider = _scope(provider).lower()
        _require(provider not in result)
        _require(type(record) is dict and set(record) <= {'id', 'name', 'origins', 'opaque'})
        _require(type(record.get('opaque', False)) is bool)
        result[provider] = (_recipient({k: record[k] for k in ('id', 'name', 'origins')})
                            if all(k in record for k in ('id', 'name', 'origins')) else None,
                            record.get('opaque', False))
        _require(result[provider][0] is not None)
    return result


def _controls(value, depth=0, budget=None):
    """Bounded non-secret routing metadata for the fingerprint; never returned to clients."""
    budget = [512] if budget is None else budget
    budget[0] -= 1
    _require(depth <= 5 and budget[0] >= 0)
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        _require(abs(value) <= 2 ** 63)
        return value
    if type(value) is float:
        _require(abs(value) <= 1e12)
        return value
    if type(value) is str:
        return _text(value, 2048)
    if type(value) is list:
        _require(len(value) <= 32)
        return [_controls(x, depth + 1, budget) for x in value]
    _require(type(value) is dict and len(value) <= 64)
    result = {}
    for key in value:
        _text(key, 128)
        if not _SECRET.search(key):
            result[key] = _controls(value[key], depth + 1, budget)
    return result


def _provider_definition(config, provider):
    providers = config.get('providers', {})
    _require(type(providers) is dict and len(providers) <= 128)
    matches = [value for key, value in providers.items() if _scope(key).lower() == provider]
    _require(len(matches) <= 1)
    if matches:
        entry = matches[0]
        _require(type(entry) is dict and len(entry) <= 128)
        if entry.get('base_url') is not None:
            _text(entry['base_url'], 2048)
        return entry
    custom = config.get('custom_providers', [])
    _require(type(custom) is list and len(custom) <= 128)
    for item in custom:
        _require(type(item) is dict and len(item) <= 128)
        if any(_text(item[key], 128).lower() == provider for key in ('name', 'id')
               if key in item and item[key] is not None):
            if item.get('base_url') is not None:
                _text(item['base_url'], 2048)
            return item
    return {}


def _routes(config):
    routes = []
    control_budget = [2048]

    def add(identifier, value):
        _require(type(value) is dict and len(value) <= 128 and len(routes) < MAX_ROUTES)
        for key in ('provider', 'base_url', 'model', 'default'):
            if key in value and value[key] is not None:
                _text(value[key], 2048 if key == 'base_url' else 512)
        provider = _text(value.get('provider') or 'auto', 128, empty=False).lower()
        _require(_SCOPE.fullmatch(provider) is not None)
        definition = _provider_definition(config, provider)
        base = _text(value.get('base_url') or definition.get('base_url') or '', 2048)
        origin, normalized = _url(base) if base else (None, '')
        model = _text(value.get('model') or value.get('default') or '', 512)
        controls = _controls({key: value[key] for key in ('extra_body', 'provider_routing', 'api_mode') if key in value},
                             budget=control_budget)
        controls = {key: item for key, item in controls.items() if item not in ({}, [], None, '')}
        routes.append({'id': identifier, 'provider': provider, 'base_url': normalized,
                       'origin': origin, 'model': model, 'controls': controls,
                       'opaque_controls': bool(controls.get('extra_body') or controls.get('provider_routing'))})

    def chain(prefix, field, value):
        if value is None:
            return
        entries = [value] if type(value) is dict else value
        _require(type(entries) is list and len(entries) <= MAX_CHAIN)
        for i, entry in enumerate(entries):
            add(f'{prefix}.{field}[{i}]', entry)

    model = config.get('model', {})
    if type(model) is str:
        model = {'model': model}
    _require(type(model) is dict and len(model) <= 128)
    main = {key: config[key] for key in ('provider', 'base_url') if key in config}
    main.update(model)
    add('main', main)
    for key in ('fallback_providers', 'fallback_model'):
        chain('main', key, config.get(key))
    auxiliary = config.get('auxiliary', {})
    _require(type(auxiliary) is dict and len(auxiliary) <= 64)
    # Runtime auto/default auxiliary discovery cannot be proven absent from a raw config.
    add('auxiliary.default', {'provider': 'auto',
                             'extra_body': {k: auxiliary[k] for k in _AUX_META if k in auxiliary}})
    for task, entry in auxiliary.items():
        if task in _AUX_META:
            continue
        task = _scope(task)
        add('auxiliary.' + task, entry)
        for key in ('fallback_chain', 'fallback_providers', 'fallback_model'):
            chain('auxiliary.' + task, key, entry.get(key))
    stt = config.get('stt', {})
    _require(type(stt) is dict and len(stt) <= 128 and type(stt.get('enabled', True)) is bool)
    if stt.get('enabled', True):
        provider = _text(stt.get('provider') or 'auto', 128).lower()
        section = stt.get(provider, {})
        _require(type(section) is dict and len(section) <= 128)
        route = dict(stt)
        route.update(section)
        route['provider'] = provider
        if 'model_id' in section and 'model' not in section:
            route['model'] = section['model_id']
        add('stt', route)
        for key in ('fallback_chain', 'fallback_providers', 'fallback_model'):
            chain('stt', key, route.get(key))
    return routes


def _declarations(value, route_ids):
    if value is None:
        return {}
    _require(type(value) is dict and set(value) == {'schema_version', 'routes'} and
             type(value['schema_version']) is int and value['schema_version'] == 1)
    values = value['routes']
    _require(type(values) is dict and len(values) <= MAX_ROUTES and set(values) <= route_ids)
    result = {}
    total_recipients, total_origins = 0, 0
    for identifier, declaration in values.items():
        _require(type(declaration) is dict and set(declaration) == {'complete', 'recipients'} and
                 type(declaration['complete']) is bool)
        recipients = declaration['recipients']
        _require(type(recipients) is list and 1 <= len(recipients) <= MAX_RECIPIENTS)
        total_recipients += len(recipients)
        _require(total_recipients <= 256)
        for recipient in recipients:
            _require(type(recipient) is dict and type(recipient.get('origins')) is list)
            total_origins += len(recipient['origins'])
            _require(total_origins <= 1024)
        result[identifier] = {'complete': declaration['complete'],
                              'recipients': [_recipient(x) for x in recipients]}
    return result


def build_disclosure(config, *, profile, session_id=None, downstreams=None,
                     provider_registry=None, enforced=False):
    """Return schema v1; never infer dynamic/opaque recipients or mutate inputs.

    ``config`` must be the caller's effective sanitized profile/session snapshot.
    Environment overrides, plugin hooks and per-call dispatch are not resolved here.
    The caller may attest native enforcement via the keyword argument, never config.
    """
    _require(type(config) is dict and len(config) <= 256 and type(enforced) is bool)
    profile = _scope(profile)
    if session_id is not None:
        session_id = _scope(session_id)
    registry = _registry(PUBLIC_PROVIDERS if provider_registry is None else provider_registry)
    routes = _routes(config)
    declarations = _declarations(downstreams, {r['id'] for r in routes})
    recipients, complete = {}, True

    def merge(recipient):
        previous = recipients.get(recipient['id'])
        if previous:
            _require(previous['name'] == recipient['name'])
            previous['origins'] = sorted(set(previous['origins']) | set(recipient['origins']))
            _require(len(previous['origins']) <= MAX_ORIGINS)
        else:
            _require(len(recipients) < MAX_RECIPIENTS)
            recipients[recipient['id']] = {**recipient, 'origins': list(recipient['origins']),
                                           'purposes': list(PURPOSES)}

    for route in routes:
        record, opaque = registry.get(route['provider'], (None, True))
        known = record is not None and (route['origin'] is None or route['origin'] in record['origins'])
        if known:
            merge({**record, 'origins': [route['origin']] if route['origin'] else record['origins']})
        declaration = declarations.get(route['id'])
        declared_origins = set(record['origins'] if known else [])
        if declaration:
            for recipient in declaration['recipients']:
                merge(recipient)
                declared_origins.update(recipient['origins'])
        unresolved = (not known or opaque or route['provider'] in {'auto', 'custom', 'local', 'local_command'}
                      or route['opaque_controls'])
        covered = (declaration is not None and declaration['complete'] and
                   (route['origin'] is None or route['origin'] in declared_origins))
        if (unresolved and not covered) or (declaration is not None and not declaration['complete']):
            complete = False

    output = {'schema_version': 1, 'complete': complete, 'enforced': enforced,
              'profile': profile, 'recipients': sorted(recipients.values(), key=lambda x: x['id'])}
    if session_id is not None:
        output['session_id'] = session_id
    material = {**output, 'routes': routes, 'declarations': declarations}
    output['revision'] = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':'),
                                                 ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    if not complete:
        output['unavailable_reason'] = 'Recipients are not fully declared for every enabled AI route.'
    elif not enforced:
        output['unavailable_reason'] = 'Server-side recipient enforcement is unavailable.'
    return output
