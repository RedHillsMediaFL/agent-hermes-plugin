# Testing the Agent companion

The current plugin ID is `agent-companion-runtime`. The optional v0.1.0 metadata preview retains the separate `agent-companion` ID and its original source/tag/store. Install the new plugin beside the preview; no forced replacement or removal is required.

## Exact source boundaries

- Stock baseline: Hermes v0.23.2, `ae2a81ccc00e9b7c06301523e5bb09608397149c`.
- Reviewed native runtime: `RedHillsMediaFL/hermes-agent`, branch `agent-companion-v1`, full commit `d2dfb3ee41abfb79d99d9a45b0559de81b78c062`.
- Companion v0.3.2: full commit `d6e485a6ea3ac9055fa92675b3b9c8f06dfc630d`.
- Legacy metadata preview: v0.1.0, `e1a5cdc3d5226fee69c45ce6ae6838c0682d2877`.
- Hermes requires the full 40-character SHA for `--ref`; tags and branches are not accepted.

Use a standard Hermes installation with supported web dependencies. Agent supports Hermes' `basic` username/password authentication with `native_pkce`; it does not implement browser OAuth-only sign-in. A stock server may accept authentication yet use a different chat protocol. Only the native compatibility endpoint and a verified WebSocket handshake establish app compatibility.

## Install through Hermes

```sh
hermes plugins install RedHillsMediaFL/agent-hermes-plugin --ref d6e485a6ea3ac9055fa92675b3b9c8f06dfc630d
hermes plugins enable agent-companion-runtime
hermes plugins doctor agent-companion-runtime --ci
hermes plugins show agent-companion-runtime
```

Run these in the serving profile. The app uses the same authenticated stock plugin manager, an immutable reviewed ref and `force:false`. Keep any v0.1 preview installed during migration and verify its source edits and settings database remain unchanged.

The reviewed runtime supplies both canonical session-authority and legacy-dashboard conversation adapters. Agent must select the matching protocol from authenticated capabilities and `gateway.ready`, and must never retry a canonical refusal through the legacy writer. `hermes serve --isolated` selects a dedicated profile server; the flag alone does not establish its chat protocol.

When a gateway already exposes native HTTP, test and reuse that service and its origin. Use isolated `hermes serve` only for standalone installations without a serving gateway.

Reuse/claim an available port before launching the server. Expose an HTTPS origin trusted by the client device, proxy WebSocket upgrades, and set the serving profile's `dashboard.public_url` to that exact HTTPS origin. A proxy on another host/container also needs its bounded address in `dashboard.trusted_proxies`. Configure the bundled basic provider and stable signing secret before unattended startup; do not expose an unauthenticated dashboard. Use the upstream authentication/proxy guide for your installation shape.

## In-app setup acceptance

1. Start the exact reviewed runtime above with its own temporary Hermes home and synthetic or dedicated test credentials. Do not copy a personal Hermes account, provider configuration or conversations into a test installation.
2. Verify public `/api/status` advertises `auth_required: true`, `basic` and `native_pkce`. Sign in from Agent and confirm explicit message/file sharing consent is required.
3. Install the new runtime companion from the setup screen. Verify the normal manager's code scan, dependency admission and activation; the new namespace must become live even if the old metadata API remains loaded.
4. Read authenticated `/api/plugins/agent-companion-runtime/setup/runtime`. Confirm the fixed repository, branch and full runtime commit. It must report `automatic_update:false`, `can_update:false`, and explicit initial-install/restart or managed-host requirements. Dirty and pending Git-operation checkouts must remain untouched.
5. Submit the closed setup intent twice and verify both requests refuse with HTTP 409. Attempt an arbitrary source/ref/command and verify HTTP 400 without echoing it. No source files, job store, installer child, model/provider default or live process may change.
6. Use the standard supported initial compatible-Hermes installer or your existing image/hosting mechanism, at a safely controlled administrator-selected time. Preserve original account/configuration/provider selections and source work. The companion must not claim to perform this operation automatically.
7. After startup require supported `/api/agent/v1/capabilities`, a read-only ticketed WebSocket handshake/ping, and a second matching installation/profile identity read. Installer success or plugin presence alone must leave Connect unavailable.
8. Send a harmless test message, reply to a specific message, create a task thread, reopen both histories, and verify linkage survives app and controlled backend restart. Keep each agent's ongoing conversation separate from topic chats and verify two profiles cannot adopt each other's message/thread references.

Managed containers and Desktop installations must use their existing hosting mechanism to deploy the reviewed runtime; the companion must report that state without mutating an image. The publisher's reviewer container/proxy is separate test infrastructure and is not needed for normal self-hosted installs.

## Verification status

The current package has isolated settings and setup contract tests using temporary state, synthetic authentication and in-memory ASGI requests. They make no model calls, open no listening ports and use no production Hermes home. The stock source also has targeted preservation checks proving local user files survive normal plugin updates and edited tracked files receive backups.

These passing contract checks are not live installation or restart acceptance. Before releasing, complete the exact-source lifecycle checks above and record source commits, artifact/build identity, public HTTPS/auth/native HTTP/WebSocket evidence and actual source/state preservation. Include redacted error codes; exclude passwords, keys, tokens and private conversations.
