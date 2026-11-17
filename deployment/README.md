# Publisher review host

These files are optional publisher tooling. Ordinary self-hosted Hermes users install the companion normally and use their own provider; they do not need this proxy or a Red Hills account.

The reviewer host runs a separate, non-root Hermes container with its own account and state. It has no host home, Agency files, Docker socket, direct inference route, internet route, or provider key. A second non-root container adapts one existing local model and is the only member with an egress network. Both join a private internal network; neither container publishes a host port. Hermes uses `http://provider:3010/v1`. The existing host Nginx process supplies the loopback entry and public TLS reverse proxy.

The proxy permits only its startup-configured local model, strips tools and provider overrides, fixes reasoning to `none`, limits input to 16,000 characters and output to 256 tokens, admits one concurrent request, five requests per minute and 50 per UTC day, and caps response bytes. The quota is committed before contacting the local inference server and survives restarts. It does not log messages, responses or request paths. Its SQLite journal contains only admission timestamps and UTC dates, pruned after two days. No external paid-provider credential is used.

Upstream client errors retain their HTTP status with a generic message; response bodies and headers are never forwarded on failure. In particular, an existing inference gateway's idle-only `409` refusal is preserved. This adapter never forces a model load, downloads weights, changes shared model defaults, or bypasses the owner's admission rules. A healthy model list is insufficient: verify an actual bounded turn before reviewer activation.

Install `reviewer-provider-config.example.json` privately as `/opt/agent-beta/provider-config.json`, replacing its example model/IP with an already installed model and reviewed wired-LAN address. This read-only startup file permits only `model` and `upstream_url`; the URL must be one literal private IPv4 address, an explicit port, and `/v1/chat/completions`. It accepts no credentials, public target, redirect, query string, alternate route, or request-selected model. The firewall must name that same IP and port. HTTP environment proxies are disabled for this fixed local request.

## Storage and container boundaries

`reviewer-compose.yaml` requires reviewed immutable image IDs in a private `image.env`. Both containers use read-only roots, dedicated non-root service identities, dropped capabilities, no privilege escalation, IPv6 disabled, bounded logs, and PID/CPU/memory limits. Bind mounts refuse missing source paths instead of creating ordinary directories. An explicit empty, read-only `/opt/data` bind prevents the base image's inherited `VOLUME` declaration from creating an anonymous writable volume.

The two writable state directories must be separate, fixed-size ext4 loop filesystems, mounted `nosuid,nodev,noexec`:

| State | Maximum backing size | Maximum inodes | Owner |
|---|---:|---:|---|
| `/var/lib/agent-beta/hermes` | 2 GiB | 32,768 | `992:978`, mode `0700` |
| `/var/lib/agent-beta/provider-state` | 32 MiB | 2,048 | `993:993`, mode `0700` |

Use dedicated systemd mount units and require both mounts before starting the Compose service. Preserve and compare any existing state before moving it to these filesystems. `reviewer-storage-check.py` refuses startup if paths contain symlinks or a mount, ownership/mode, regular single-link backing file, filesystem limit, inode limit, or safety mount option differs. It reports actual allocated backing bytes separately from logical maximum size. An ordinary directory bind is not a disk quota. Container `/tmp` is bounded tmpfs: 128 MiB for Hermes and 16 MiB for the provider.

The adapter's image adds only a small Python layer to the same reviewed Hermes base. Docker's displayed virtual image sizes share those base layers and must not be added together. Model weights remain on the existing inference host; do not copy or mount them into reviewer storage. Keep build staging and preserved rollback artifacts in the explicit removal inventory rather than accumulating untracked copies.

## Network boundary

`reviewer-isolation.py` owns only `AGENT_BETA_IN`, `AGENT_BETA_FWD`, their dedicated-subnet jumps, and the `bridge agent_beta` nftables table. It leaves other host services and bridges alone and does not change global bridge sysctls. Install its private `/opt/agent-beta/isolation.json` from the example after reviewing the dedicated subnets and provider addresses.

- Hermes joins only the internal bridge and can reach only provider port `3010`.
- The provider joins that bridge and one egress bridge. Its sole egress is TCP to the reviewed local inference IP and port; public internet egress is denied.
- New container connections to host services, all other LAN addresses/ports, Tailscale, metadata endpoints, other Docker networks, arbitrary internet addresses, and external DNS are denied.
- Internal-bridge nftables rules enforce the peer boundary even when bridge traffic does not traverse iptables. Host Nginx can reach Hermes port `9119` through the internal bridge gateway.

The proxy startup file and firewall's single private IP/port exception must match. Checked-in values are examples; select the existing wired inference address before deployment. No DNS, GitHub, generic web egress, tunnel, additional inference server, or model download is required. Exact nftables expressions are verified, including destination addresses, ports, hook and final denial.

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

1. Exact reviewed base/provider image IDs; no anonymous volumes, published container ports, host sockets, unrelated mounts, model mounts, or paid-provider key mounts.
2. Actual container network negatives for host, other private addresses/ports, metadata, DNS, and arbitrary public destinations; Hermes-to-provider health/models and the fixed local inference route pass.
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

See [TEARDOWN.md](TEARDOWN.md) for scope-checked inventory and removal. Removal begins with a dry run and preserves shared inference, model files, unrelated Docker resources and owner state unless their explicit scoped deletion is authorized.
