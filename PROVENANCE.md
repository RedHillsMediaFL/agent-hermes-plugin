# Source provenance and compatibility

The code in this repository was written for Agent companion and is licensed under the included MIT license. It contains no copied legacy Agent server modules, runtime patches, private service configuration, mailbox skill, production data, model weights, or bundled third-party library source. Hermes and the Python packages it supplies retain their own licenses.

The implementation was inspected against public Hermes **v0.23.2**, peeled commit **`ae2a81ccc00e9b7c06301523e5bb09608397149c`**, on November 16, 2026. The review copy is excluded from this repository. Supported extension seams used here:

- `plugin.yaml`, `__init__.py:register(ctx)`, and the ordinary plugin enable lifecycle.
- `dashboard/manifest.json` declaring a module-level FastAPI `router` under `/api/plugins/<name>`.
- `plugins.plugin_storage.plugin_db`, resolving the effective profile on every operation.
- Verified dashboard request context and the host's private hosted-HTTP admission projection, without importing private core modules.

No module is monkeypatched, no canonical runtime table is written, and no arbitrary process-global owner identity is inferred. This package does not depend on the former `hermes-ios-bridge`, `hermes-ios-push`, `hermes-ios-live-activity`, custom room helpers, or APNs relay.

## Agent client compatibility evidence

Stock v0.23.2 source includes the current client's PKCE/password native-auth endpoints and refresh, `/api/auth/ws-ticket`, the `hermes-gateway-v1` and `hermes-gateway-ticket.` WebSocket subprotocols, `/api/profiles`, aggregated/scoped session lists, paginated transcript reads, managed files and multipart uploads.

Those paths alone do not establish wire compatibility. A `gateway.ready` event with `session_authority: true` selects Hermes' canonical session authority; legacy session RPCs cannot be retried through the legacy writer when the canonical owner rejects them. The canonical create/submit/control contracts differ from older client traffic. A conforming client must negotiate and honor canonical references, revisions, execution generations, admissions, events and request semantics.

There is **no `hermes dashboard --standalone` option** in the reviewed v0.23.2 parser. Its `--isolated` option selects a dedicated profile server and is not evidence of a legacy protocol mode. This package therefore publishes no undocumented startup workaround. Full app compatibility requires a separate clean, synthetic authenticated sign-in/WebSocket/create/send/history acceptance run.

## Publication boundary

The public source destination is [RedHillsMediaFL/agent-hermes-plugin](https://github.com/RedHillsMediaFL/agent-hermes-plugin). Publish only this package's reviewed tracked files. Do not copy ignored review caches, internal rollout records, signing material, service credentials, private runtime state, or the application's unrelated repository history into it. Re-run secret and standard Hermes plugin validation on the exact release commit.
