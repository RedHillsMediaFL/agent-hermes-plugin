# Agent companion for Hermes

A free MIT-licensed Hermes plugin that supplies optional connection labels, explicit agent roles, and interface preferences for the Agent client. It uses Hermes' normal plugin manager, dashboard API, and profile-resolved plugin storage. It does not replace Hermes core files, register model tools or hooks, change inference settings, or require a relay.

The plugin requires **Hermes v0.23.2 or later**. It supports both in-process and hosted dashboard HTTP execution. This initial version contains metadata features only; installing it does not add a second chat engine or make an older client compatible with a different Hermes session protocol.

## Install

The public repository and release commit must be confirmed before these placeholders become an install command. Replace `OWNER/REPO` and `FULL_COMMIT_SHA` with the published source and its complete immutable 40-character commit:

```sh
hermes plugins install OWNER/REPO --ref FULL_COMMIT_SHA
hermes plugins enable agent-companion
hermes plugins doctor agent-companion --ci
hermes plugins show agent-companion
```

Run these commands in the Hermes profile that serves the connection. The normal Hermes enable/discovery lifecycle applies; this package has no custom installer or service. No new port is needed. A dashboard-only client installation must include Hermes' supported web dependencies.

For an unpinned installation, `hermes plugins update agent-companion` follows its installed source. A pinned installation remains on its selected revision; update it explicitly:

```sh
hermes plugins install OWNER/REPO --force --ref NEW_FULL_COMMIT_SHA
hermes plugins doctor agent-companion --ci
```

Disable or uninstall through Hermes:

```sh
hermes plugins disable agent-companion
hermes plugins remove agent-companion
```

Settings live in Hermes' profile-resolved `plugin-data/agent-companion/data.db`, outside the installed code directory. Reset them through the settings API before uninstalling if desired. Uninstalling this plugin does not delete chats, credentials, model configuration, or another plugin's data.

## API

The dashboard mounts all routes beneath **`/api/plugins/agent-companion`** and enforces its normal authentication. In-process execution requires the verified session/native-owner context supplied by that middleware; hosted execution accepts only the private, request-local admission decision supplied by Hermes. A client header or body cannot supply that decision. Legacy unverified loopback/session-token access is not an in-process identity and receives HTTP 401.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/capabilities` | Schema/plugin versions, explicit implemented features and unsupported integrations; no storage write. |
| GET | `/settings` | Current revision, installation ID, scope and complete display settings. First access creates the neutral settings record. |
| PUT | `/settings` | Replace the closed settings object using an exact `expected_revision`. |
| DELETE | `/settings?expected_revision=N` | Reset display settings to defaults without rotating installation identity or touching chat data. |

Settings are scoped to the **effective Hermes profile**, shared by authenticated dashboard operators of that installation. This is not a multi-tenant account-preference service. Neither a display label nor a profile identifier grants authority or proves that a profile exists. The client must reconcile role metadata with Hermes' actual roster and handle stale/deleted identifiers.

Example successful read:

```json
{
  "ok": true,
  "schema_version": 1,
  "settings_scope": "hermes_profile",
  "installation_id": "460e858f-fb54-4088-82ef-b02917ae48d1",
  "revision": 0,
  "settings": {
    "connection": {"display_name": null},
    "roles": {"primary_profile": null, "background_profiles": []},
    "ui_preferences": {"default_destination": "agents", "show_background_agents": false}
  }
}
```

PUT accepts exactly `{"expected_revision":0,"settings":<the complete settings object>}`. All fields shown in `settings` are required; unknown fields, duplicate JSON keys, type coercions, control characters, invalid profile IDs and overlapping roles are rejected. The connection label accepts null or 1–80 trimmed characters. Profile IDs retain case and match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`; at most 64 distinct background profiles are allowed. No profile is hidden or made primary automatically. The default destination is `agents` or `chats`.

Successful changes increment `revision`; an identical save is idempotent. A stale revision returns HTTP 409 with `detail.code: settings_revision_conflict` and the current receipt. Reload and let the user reconcile it; do not silently overwrite another save. Invalid JSON/settings returns 400, oversized bodies 413, unauthenticated access 401, and unreadable/corrupt storage 503. Request bodies are capped at 16 KiB; errors do not echo arbitrary submitted values. Responses use `Cache-Control: no-store`.

## Feature boundaries

Core authentication, ticketed WebSockets, session history, chat execution, file upload, and supported management operations remain Hermes' responsibility. Client/server protocol compatibility must be verified independently of this plugin. The plugin does not proxy inference or infer a canonical agent conversation from a title, recent row, or message text.

The capability receipt explicitly reports the following as unavailable: canonical ongoing-session resolution, durable reply linkage, groups, remote push, Live Activities, Life collection, usage uploads and account-scoped preferences. Clients must respect those values. Existing separate integrations are not silently adopted. No subscription, purchase, analytics collector, APNs key, mailbox configuration, private endpoint, or credential is included.

## Development and verification

Use an isolated environment, install `requirements-dev.txt`, and run:

```sh
python -m unittest discover -s tests -v
hermes plugins validate /path/to/agent-hermes-plugin --json
hermes plugins doctor /path/to/agent-hermes-plugin --ci
```

Tests use temporary plugin storage, synthetic authentication, and in-memory ASGI requests. They do not call a model, start a listening server, or use a production Hermes home. They cover concurrent/stale writes, profile storage selection, corruption preservation, strict inputs, authentication before parsing/storage, and unsupported capabilities.

See [PROVENANCE.md](PROVENANCE.md) for the inspected upstream version and source boundaries. Package tests do not establish a complete Agent app sign-in/create/send/history acceptance result.

Upstream documentation: [Hermes plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins), [dashboard backend APIs](https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard#backend-api-routes).
