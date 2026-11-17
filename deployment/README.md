# Publisher review host

These files are optional publisher tooling. Ordinary self-hosted Hermes users install the companion normally and use their own provider; they do not need this proxy or a Red Hills account.

The reviewer host runs a separate, non-root Hermes container with its own account and state. It has no host home, Agency files, Docker socket, direct internet route, or provider key. A second non-root container holds the provider key and is the only member with an egress network. Both join a private internal network; neither container publishes a host port. Hermes uses `http://provider:3010/v1`. The existing host Nginx process supplies the loopback entry and public TLS reverse proxy.

The proxy permits only `deepseek-chat` text requests, strips tools and provider overrides, limits input to 16,000 characters and output to 256 tokens, admits two concurrent requests, five requests per minute and 50 per UTC day, and caps response bytes. The quota is committed before contacting the provider and survives restarts. It does not log messages, responses, credentials or request paths. Its SQLite journal contains only admission timestamps and UTC dates, pruned after two days. The provider receives the submitted messages under its own processing policy.

Upstream client errors retain their HTTP status with a generic message; provider response bodies and headers are never forwarded on failure. This prevents an unfunded provider account from becoming a retryable `503`. A healthy model list does not establish that inference is funded: verify an actual bounded chat turn before reviewer activation.

## Storage and container boundaries

`reviewer-compose.yaml` requires reviewed immutable image IDs in a private `image.env`. Both containers use read-only roots, dedicated non-root service identities, dropped capabilities, no privilege escalation, IPv6 disabled, bounded logs, and PID/CPU/memory limits. The provider key is a read-only file mounted only into the provider. An explicit empty, read-only `/opt/data` bind prevents the base image's inherited `VOLUME` declaration from creating an anonymous writable volume.

The two writable state directories must be separate, fixed-size ext4 loop filesystems, mounted `nosuid,nodev,noexec`:

| State | Maximum backing size | Maximum inodes | Owner |
|---|---:|---:|---|
| `/var/lib/agent-beta/hermes` | 2 GiB | 32,768 | `992:978`, mode `0700` |
| `/var/lib/agent-beta/provider-state` | 32 MiB | 2,048 | `993:993`, mode `0700` |

Use dedicated systemd mount units and require both mounts before starting the Compose service. Preserve and compare any existing state before moving it to these filesystems. `reviewer-storage-check.py` refuses startup if a mount, backing file, filesystem limit, inode limit, or safety mount option differs. An ordinary directory bind is not a disk quota. Container `/tmp` is bounded tmpfs: 128 MiB for Hermes and 16 MiB for the provider.

## Network boundary

`reviewer-isolation.py` owns only `AGENT_BETA_IN`, `AGENT_BETA_FWD`, their dedicated-subnet jumps, and the `bridge agent_beta` nftables table. It leaves other host services and bridges alone and does not change global bridge sysctls. Install its private `/opt/agent-beta/isolation.json` from the example after reviewing the dedicated subnets and provider addresses.

- Hermes joins only the internal bridge and can reach only provider port `3010`.
- The provider joins that bridge and one egress bridge. Its only public egress is TCP `443` to the reviewed DeepSeek IPv4 allowlist.
- New container connections to host services, LAN, Tailscale, metadata endpoints, other Docker networks, arbitrary internet addresses, and external DNS are denied.
- Internal-bridge nftables rules enforce the peer boundary even when bridge traffic does not traverse iptables. Host Nginx can reach Hermes port `9119` through the internal bridge gateway.

The provider's `extra_hosts` entry and the firewall allowlist must match. The checked-in IPv4 is a deployment example, not a stable DeepSeek API address; re-resolve and review it before deployment. DNS changes fail closed until both are deliberately updated. No GitHub or generic web egress is required.

Docker internal networks do not provide the host port publishing used by ordinary bridge deployments. Install `reviewer-loopback.nginx.conf` into the existing Nginx configuration after reserving its loopback port. The external virtual host must continue returning `503` until acceptance completes.

## Offline stock companion installation

Mount a read-only bare companion mirror at `/opt/reviewer/companion.git`, whose shallow `main` points to the exact published companion revision. Mount this config-only Git template at `/opt/reviewer/git-template/config`:

```ini
[url "file:///opt/reviewer/companion.git"]
    insteadOf = https://github.com/RedHillsMediaFL/agent-hermes-plugin.git
```

Compose sets `GIT_TEMPLATE_DIR` and `GIT_ALLOW_PROTOCOL=file`. Stock Hermes preserves that template while sanitizing Git's ambient configuration; its unchanged GitHub URL and immutable `ref` therefore clone from the local mirror. The native authenticated install route remains the stock dashboard installer with `enable:true` and `force:false`, including its local scan and activation. The companion has no runtime dependency downloads. Do not add a Git shim, Git HTTP service, or installer replacement.

## Acceptance and activation

The systemd service must run these checks before Compose starts, require the state mounts, and require a publisher-owned readiness receipt:

```sh
python3 /opt/agent-beta/reviewer-storage-check.py
python3 /opt/agent-beta/reviewer-isolation.py apply
```

A health response alone does not establish isolation or inference readiness. Keep the service disabled and public HTTPS at `503` until all of these have concrete receipts:

1. Exact reviewed base/provider image IDs; no anonymous volumes, published container ports, host sockets, unrelated mounts, or provider key in Hermes.
2. Actual container network negatives for host, private, metadata, DNS, and arbitrary public destinations; Hermes-to-provider health/models and certificate-verified TLS to the fixed upstream pass.
3. Actual `ENOSPC` from allocating beyond each state filesystem's maximum in a unique task-owned file, with cleanup in `finally`.
4. Native PKCE sign-in, unauthorized refusal, unchanged stock companion install at the published SHA, loaded plugin capabilities, and native protocol capabilities.
5. Bounded real inference, attributed message reply/idempotency/readback, separate task/idempotency/origin/readback, and ongoing-conversation reopening.
6. Container restart preserves transcript, reply/task linkage, ongoing identity, plugin activation, and quota rows; public TLS, sign-in and WebSocket paths pass after deliberate activation.

A new isolated Hermes store is initialized by the authenticated `session.ongoing_open` mutation. Initial read-only browsing can correctly return `503` before that mutation; browsing does not create or repair the owner's database. A required plugin restart is explicit and precedes final acceptance. Preserve predecessor images and private receipts for rollback; never prune unrelated containers, images, volumes or state.

Run the provider preparation, privacy, upstream-error, and durable-quota tests without network access:

```sh
python3 -m unittest discover -s deployment -p 'test_reviewer_proxy.py'
```

Never put the provider key or reviewer password into this repository or a container image. Do not offer the private Apple review account as a shared public tester login; testers use their own compatible server and account.
