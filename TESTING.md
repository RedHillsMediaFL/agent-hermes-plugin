# Testing the Agent companion

`v0.1.0` is an optional metadata preview. It adds connection labels, explicit profile roles, and interface preferences. Chat, authentication, models, and history remain Hermes features. Installing this plugin does not establish compatibility with every Hermes server or Agent build.

## Install an immutable revision

Use a normal Hermes installation with its supported web dependencies and a configured model provider. The verified stock baseline is **Hermes v0.23.2** (`ae2a81ccc00e9b7c06301523e5bb09608397149c`); later versions require their own acceptance checks. Run commands in the profile that will serve the connection:

```sh
hermes plugins install RedHillsMediaFL/agent-hermes-plugin --ref e1a5cdc3d5226fee69c45ce6ae6838c0682d2877
hermes plugins enable agent-companion
hermes plugins doctor agent-companion --ci
hermes plugins show agent-companion
```

This full SHA is the source commit of `v0.1.0`. Hermes requires a 40-character commit SHA for `--ref`; tag and branch names are rejected. Pinned installations stay pinned. To update, replace `REVIEWED_FULL_COMMIT_SHA` below with the reviewed release's full commit:

```sh
hermes plugins install RedHillsMediaFL/agent-hermes-plugin --force --ref REVIEWED_FULL_COMMIT_SHA
hermes plugins doctor agent-companion --ci
```

## Run a compatible authenticated server

The current Agent client supports the legacy dashboard chat protocol and the `basic` username/password provider. Its native PKCE flow posts to `/auth/password-login`; it has no browser OAuth/OIDC sign-in flow. The supported setup is a private LAN/VPN connection over HTTPS. Public OAuth-only servers are not supported by this client, although Hermes itself supports them.

For an iPhone or remote Mac, configure an HTTPS URL reachable only through your trusted LAN/VPN, with a certificate trusted by that device. Set TLS termination to proxy to the loopback backend, including WebSocket upgrades, and merge the exact external origin into the serving profile's `config.yaml`:

```yaml
dashboard:
  public_url: "https://hermes.example.com"
```

Replace the example hostname with your own. Forward the original host and HTTPS scheme. A proxy on another host/container needs its exact address or a bounded proxy network in `dashboard.trusted_proxies`. The public URL engages Hermes' authentication gate even when the backend binds to loopback.

Configure Hermes' bundled `basic` provider before unattended startup. The first interactive launch offers username/password setup when the gate engages and no provider exists. Keep this password-based dashboard private to the LAN/VPN; it is not a public-internet deployment recipe. Start stock Hermes' dedicated server using its supported `--isolated` flag:

```sh
hermes dashboard --isolated --host 127.0.0.1 --port 9119 --no-open
```

For a headless backend, use `hermes serve --isolated --host 127.0.0.1 --port 9119`. Choose an available port if 9119 is occupied. Both commands use Hermes' standard server implementation; `--isolated` controls routing, not data-store or tenant isolation. Restart after configuration changes. See the [upstream authentication and proxy guide](https://github.com/NousResearch/hermes-agent/blob/v0.23.2/website/docs/user-guide/features/web-dashboard.md#authentication-gated-mode) for details.

## Check the installation

1. Open `https://hermes.example.com/api/status` through the private LAN/VPN. Confirm `auth_required: true`, `basic` in `auth_providers`, and `native_pkce` in `auth_flows`.
2. Sign in through Agent using that HTTPS URL and the configured username/password. Create a topic chat, attach a small test file, and send a harmless prompt. Confirm streaming and a completed reply, reopen the chat, then verify both sides of the conversation survive an app restart and a controlled backend restart.
3. Keep ongoing agent conversations separate from topic chats. A fresh profile can have no ongoing conversation; the newest topic must not silently become one.
4. In an authenticated dashboard browser session, read `/api/plugins/agent-companion/capabilities` and `/api/plugins/agent-companion/settings`. Expect plugin version `0.1.0`, `settings_scope: hermes_profile`, and only the advertised metadata features enabled. See [README.md](README.md#api) for revision-checked settings updates.

A server advertising `gateway.ready.payload.session_authority: true` uses a different canonical chat protocol. The current Agent client rejects it before sending legacy chat calls. The companion cannot translate that protocol. Groups, remote push, Live Activities, Life, usage uploads, durable reply linkage, and plugin-provided ongoing-session resolution remain unavailable in this preview.

## Verification status

The package passed 18 isolated unit tests and all 15 stock v0.23.2 plugin-validator checks. No-port tests against unchanged stock Hermes also passed synthetic password/PKCE sign-in, bearer authentication, ticketed legacy WebSockets, session creation, file attachment, streamed events, live history, durable user history, resume, and revision/generation-fenced rename, pin, and delete operations with stale-request and receipt-replay checks.

Those tests used a synthetic identity provider and Hermes' stock synthetic-turn seam. They made no live model calls and do not establish durable assistant history after a backend restart, public HTTPS deployment acceptance, or complete Agent release acceptance. Complete the checks above with the exact Hermes version, Agent build, and provider you intend to support. Report those versions and redacted errors; omit credentials, tokens, and private conversations.
