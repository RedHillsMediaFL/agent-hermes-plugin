# Agent companion for Hermes

A free MIT-licensed Hermes plugin that supplies optional connection labels, explicit agent roles, and interface preferences for the Agent client. It uses Hermes' normal plugin manager, dashboard API, and profile-resolved plugin storage. It registers no model tools or hooks and requires no relay.

The plugin requires **Hermes v0.23.2 or later**. It supports both in-process and hosted dashboard HTTP execution. Version 0.2 reports the reviewed Hermes compatibility release recorded in `dashboard/runtime_target.json` and the prerequisites for connecting to it. The published v0.1.0 metadata preview remains separate and unchanged. Installing a plugin alone never attests chat compatibility.

## Install

The current plugin ID is **`agent-companion-runtime`**. Install from this free, MIT-licensed repository using Hermes' normal plugin manager:

```sh
hermes plugins install RedHillsMediaFL/agent-hermes-plugin --ref f954aaa7109aefb070c92559ef546edd85000dcb
hermes plugins enable agent-companion-runtime
hermes plugins doctor agent-companion-runtime --ci
```

The example pins the reviewed v0.2.0 code using its full 40-character commit. `--ref` does not accept tags or branches. Agent's in-app installer always uses its reviewed full commit and `force:false`.

Run the commands in the Hermes profile serving the connection. The dashboard/plugin manager performs normal scanning, dependency admission and activation. A dashboard-only Hermes installation must include the supported web dependencies. This plugin opens no additional listening port and installs no permanent service.

### Existing v0.1.0 metadata preview

The published v0.1.0 tag and commit `e1a5cdc3d5226fee69c45ce6ae6838c0682d2877` remain unchanged. That preview's plugin ID is `agent-companion`. Version 0.2 installs **beside it** under the new `agent-companion-runtime` ID. This lets stock Hermes hot-mount the new setup API without replacing an already-loaded preview router, forcing a restart, or overwriting an existing plugin.

The old preview's source, user edits and `plugin-data/agent-companion/data.db` remain intact. The new plugin uses its own profile settings store. Removing the old preview later is optional; disable or remove it only if you no longer use its metadata. Do not use `--force` to migrate the preview.

Unpinned current installations can update through `hermes plugins update agent-companion-runtime`. Pinned installations retain their selected revision until explicitly re-pinned using Hermes' documented manager. Disable or remove the current plugin through Hermes:

```sh
hermes plugins disable agent-companion-runtime
hermes plugins remove agent-companion-runtime
```

Uninstalling a plugin does not delete chats, provider credentials, model configuration or another plugin's data. Profile settings live outside code under `plugin-data/agent-companion-runtime/data.db`; the compatibility inspector creates no job store.

## API

The dashboard mounts all routes beneath **`/api/plugins/agent-companion-runtime`** and enforces its normal authentication. In-process execution requires the verified session/native-owner context supplied by that middleware; hosted execution accepts only the private, request-local admission decision supplied by Hermes. A client header or body cannot supply that decision. Legacy unverified loopback/session-token access is not an in-process identity and receives HTTP 401.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/capabilities` | Schema/plugin versions, explicit implemented features and unsupported integrations; no storage write. |
| GET | `/settings` | Current revision, installation ID, scope and complete display settings. First access creates the neutral settings record. |
| PUT | `/settings` | Replace the closed settings object using an exact `expected_revision`. |
| DELETE | `/settings?expected_revision=N` | Reset display settings to defaults without rotating installation identity or touching chat data. |
| GET | `/setup/runtime` | Read-only reviewed source target and explicit initial-install/restart/managed-host requirements. |
| POST | `/setup/runtime` | Refuse automatic source replacement with a bounded prerequisite code; no runtime or source changes. |

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

## Initial compatible Hermes installation

A compatible Hermes runtime is an explicit prerequisite. Install it from the free public [RedHillsMediaFL/hermes-agent](https://github.com/RedHillsMediaFL/hermes-agent/tree/agent-companion-v1) source using Hermes' supported installer and the exact reviewed commit in `dashboard/runtime_target.json`. The source-install contract is `HERMES_REPO_URL` plus `--branch`, full `--commit` and `--non-interactive`:

```sh
HERMES_REPO_URL=https://github.com/RedHillsMediaFL/hermes-agent \
  bash scripts/install.sh --branch agent-companion-v1 \
  --commit REVIEWED_RUNTIME_COMMIT --non-interactive
```

Run this from a standard Hermes source checkout with the reviewed installer. Choose your own Hermes home and provider configuration. Managed containers, Desktop bundles and other image-managed installations must deploy the compatible release through their existing hosting mechanism. Finish or safely pause work before an administrator changes/restarts its runtime. Then start the server, connect from Agent, and let the app install this companion through Hermes' normal authenticated plugin API.

The companion does not replace a live checkout, restart a runtime, change model/provider defaults, spawn an updater worker or install a permanent service. Stock Hermes' generic updater pause is not a fail-closed proof that every serving process has drained, so the app does not promise automatic migration from an arbitrary running stock server.

Authenticated GET `/setup/runtime` reports the fixed source target and owner-managed installation boundary, `automatic_update:false`, `can_update:false`, and initial-install/restart guidance. A POST accepts only the closed intent shape `{"operation_id":"<canonical lowercase UUID>","target":"agent-native-v1"}` before returning HTTP 409 with a bounded prerequisite code. It never executes a source, ref, command or owner/profile supplied by a client. Repeated requests cannot trigger a mutation because no mutation path exists.

The app independently obtains supported native `/api/agent/v1/capabilities`, verifies a read-only WebSocket handshake/ping, and re-reads the same installation/profile identity before connecting. An installed plugin, a reachable HTTP page or source installation alone never proves a live chat connection. The v0.1.0 preview remains untouched beside the new plugin ID; removing it later is optional.

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

Tests use temporary plugin storage, synthetic authentication, and in-memory ASGI requests. They do not call a model, start a listening server, or use a production Hermes home. They cover concurrent/stale writes, profile storage selection, corruption preservation, strict inputs, authentication before parsing/storage, unsupported capabilities, fail-closed setup refusal and source-target separation. These tests do not establish a successful live server installation or app connection; the exact release still requires isolated HTTP/WebSocket and state-preservation acceptance.

See [PROVENANCE.md](PROVENANCE.md) for the inspected upstream version and source boundaries. Package tests do not establish a complete Agent app sign-in/create/send/history acceptance result.

Upstream documentation: [Hermes plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins), [dashboard backend APIs](https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard#backend-api-routes).
