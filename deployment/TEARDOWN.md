# Reviewer removal and storage

`reviewer-teardown.py` inventories the exact publisher deployment and defaults to a dry run. It does not operate on ordinary self-hosted installations, the owner's Hermes, D/P2, or the shared LFM inference service. Install the reviewed script at `/opt/agent-beta/reviewer-teardown.py` before using the commands below.

Create a fresh private manifest after the accepted provider/restart checks. The script emits identifiers, ownership, paths and hashes, never environment variables, credentials, transcript contents or model weights. The shell refuses to replace an existing manifest:

```sh
sudo sh -c 'set -C; umask 077; python3 /opt/agent-beta/reviewer-teardown.py inventory > /opt/agent-beta/removal-inventory.json'
sudo python3 /opt/agent-beta/reviewer-teardown.py cleanup --manifest /opt/agent-beta/removal-inventory.json
sudo python3 /opt/agent-beta/reviewer-teardown.py remove --manifest /opt/agent-beta/removal-inventory.json
```

Review the printed plan, then repeat the selected command with `--apply`. Inventory is not approval to delete later, changed resources: regenerate and review the manifest if an identity, configuration, network, image tag or build tree has changed. Use a new manifest pathname rather than overwriting evidence. Removal and cleanup are separate operations; take a fresh inventory between them.

## Reclaim disposable build copies

`cleanup` can remove only the two inventoried build contexts `/opt/agent-beta/build` and `/opt/agent-beta/provider-build-safe-errors`. It lists unused inventoried reviewer image candidates, but removes an image only when its immutable ID is explicitly selected with `--remove-image sha256:…`. An unused image may still be an in-progress candidate or a needed rollback. It refuses a changed tree, root/path symlink, overlapping container mount, nested mount or active process consumer. Context tree identity includes every entry's device, inode, mode, size and modification time; symlink targets are recorded without following them.

Before removing a context it saves its Dockerfiles, reviewer proxy sources, image IDs and named build/source manifests in private, content-addressed `/opt/agent-beta/build-provenance` receipts, with an index of original paths and hashes. It retains the deployment's source, offline companion mirror, immutable revision/build receipts and logs. Rebuilds use the published reviewed native/companion revisions and their checked-in tooling; a full disposable source checkout or duplicate tarball is unnecessary. Inspect the `preserve_recipe_receipts_first` list before applying cleanup; add any separately identified unique build input to the retained provenance before proceeding.

All containers on the daemon, including stopped containers, protect their referenced images. Add `--keep-image sha256:…` for an explicitly needed rollback image. Images with any non-reviewer tag are refused. Apply rechecks tags, immutable IDs and container references immediately before ordinary non-force image removal. Docker retains shared layers used by another image. The displayed size of each image is not the amount reclaimed.

The default builder also contains unrelated historical work. This tool never invokes any Docker prune operation, removes build-cache records, deletes anonymous volumes, or changes a shared builder. Do not infer cache ownership from its age or build-step text. Future reviewer builds should use a dedicated builder with a recorded owner and explicit storage budget; migrating or deleting the existing daemon-wide cache is separate work.

## Remove the reviewer runtime

`remove` requires an unchanged manifest on the same host, exact Compose project/service labels, immutable container IDs/images/mounts, dedicated network IDs/bridges/subnets, no other network consumers, root-owned configuration and the exact enabled public route. Systemd overrides or another container using the reviewer Compose project cause refusal.

Apply closes the exact public reviewer virtual host with HTTP 503 while preserving its certificate and renewal challenge. It then disables/stops the reviewer service, removes its recorded containers and empty networks, removes only its exact subnet firewall jumps/two owned chains/bridge table, stops the two state mounts normally, and removes only its recorded service/mount-unit files and loopback Nginx configuration. It keeps isolation in place until the containers are gone. Busy unmounts fail; it never forces or lazily unmounts a filesystem. An operation can stop partway if a resource changes; preserve its output and inventory the remaining resources before continuing.

Private reviewer state, pre-quota backups, credentials, backing files, source/provenance receipts, image references, TLS material and the small public 503 virtual host remain. This is deliberate data preservation, and the plan lists it. This tool does not provide a data-destruction flag. Erasing reviewer transcripts or credentials requires a separate exact-path, mount/backing-file and ownership review; never recursively delete `/var/lib/agent-beta`, `~/.config/rhm`, Docker's storage root, or shared model directories. Removing this deployment does not stop, reconfigure, copy or delete the existing LFM service or models.

## Final decommission, including retained data

`remove` retires the runtime; it does **not** claim complete storage reclamation. Final decommission is a separate, explicitly reviewed data-erasure operation. Keep the small source/provenance and final removal receipts outside any directory selected for deletion. Before reclaiming retained resources:

1. Confirm `agent-beta.service` and both mount units are inactive, the two recorded container IDs are absent, and no other container mounts any reviewer state/backing path. Recheck all daemon container references before removing an image. Keep any image with a non-reviewer tag or other consumer. Use fresh inventory plus explicit `--remove-image` identities for the remaining reviewer images, without `--force`; shared layers survive. A known untagged predecessor must be tied to its prior container/build receipt before considering its immutable ID for removal.
2. For each of the two backing files, record and independently compare its canonical path, device/inode, regular-file type, one-link count, owner/mode and exact size against the pre-removal record. The exact files are `/var/lib/agent-beta/volumes/hermes.ext4` (2,147,483,648 bytes) and `/var/lib/agent-beta/volumes/provider-state.ext4` (33,554,432 bytes). Refuse any symlink component, identity change or unexpected consumer. `findmnt --mountpoint` must show neither state mount, and `losetup --json --list --output NAME,BACK-FILE` must show neither backing file attached. If either remains mounted or attached, stop and resolve that ownership; never force/lazily unmount or detach a busy/shared loop device.
3. Inspect the two exact preserved pre-quota directories from the original storage receipt: `/var/lib/agent-beta/hermes.pre-quota-20261117T033402Z` and `/var/lib/agent-beta/provider-state.pre-quota-20261117T033529Z`. Record their complete metadata/content fingerprints, prove no mount/symlink escape or process/container consumer, and confirm they contain only the obsolete reviewer copies. A newer or changed backup is not covered by these names. Delete these exact directories only after the retained data has been approved for erasure; do not use globs or delete their parent recursively.
4. Remove only the retired reviewer credential copies `/var/lib/agent-beta/provider-secret` and, if the publisher explicitly includes its saved reviewer login in the erasure decision, `/home/kyle/.config/rhm/agent-beta/reviewer-login.json`. Check regular-file type, one link, canonical path and recorded device/inode/owner before unlinking. Do not read or log their contents, delete parent credential directories, revoke shared credentials, or touch any underlying owner provider key.
5. After those checks pass, unlink only the two exact backing files and approved credential files, remove only the two exact obsolete backup directories, then use `rmdir` for the empty `hermes`, `provider-state`, `volumes` and reviewer parent directories. `rmdir` must fail if an unexpected file remains. Preserve deployment source/provenance/rollback receipts, or archive the reviewed small receipt set first and separately review any final source-directory removal. The retained Nginx 503 virtual host and TLS renewal material are small and can remain until the publisher separately retires the domain; they are not model or container storage.

Release the two publisher port claims only after the runtime is gone and Nginx has reloaded without its loopback entry. Port 18444 was `project=agent-beta, service=agent-beta-hermes`; port 3010 was `project=agent-beta, service=agent-beta-provider-internal`. Read the registry as its owner, match these exact identities immediately before each release, and refuse a reassigned port. Port 3010 was internal to the removed provider container, so container absence also matters; a host listener check alone is insufficient.

```sh
sudo -u kyle /home/kyle/.local/bin/portclaim list --project agent-beta --json
sudo ss -ltnp 'sport = :18444 or sport = :3010'
# After verifying the exact identities above and no remaining consumer:
sudo -u kyle /home/kyle/.local/bin/portclaim release 18444
sudo -u kyle /home/kyle/.local/bin/portclaim release 3010
sudo -u kyle /home/kyle/.local/bin/portclaim list --project agent-beta --json
```

Do not use registry-wide `doctor`, `reap`, project-wide releases, Docker pruning, or shared-model deletion as decommission shortcuts. Recheck allocated disk use afterward; report the remaining small receipts, TLS virtual host and ambiguous global build cache separately rather than claiming they were erased.

## Bounds and measured footprint

The existing loop filesystems cap Hermes at 2 GiB / 32,768 inodes and provider state at 32 MiB / 2,048 inodes. Container logs are configured for 32 MiB combined; `/tmp` is 144 MiB of combined tmpfs, which uses memory rather than persistent disk. Loop files are sparse: their cap is not reserved host space. Deployment sources, build cache, host Nginx/journal logs and retained backups need separate accounting.

Read-only host inventory on 2026-11-17 found approximately 844.35 MB in the disposable main build context, 6.03 MB of Hermes data and 12 KB of provider data. Backing files occupied approximately 82.96 MB on the host, including filesystem metadata. Six reviewer image entries each displayed approximately 3.23 GB but shared nearly all those layers. Total daemon build cache was 10.80 GB, including unrelated older work; 5.92 GB was created during the task window, which does not establish exclusive ownership. These measurements are a snapshot; rerun inventory and disk accounting after cleanup.
