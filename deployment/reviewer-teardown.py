#!/usr/bin/env python3
"""Inventoried reviewer removal; dry-run unless --apply. Never prunes Docker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile

ROOT = Path('/opt/agent-beta')
PROJECT = 'agent-beta'
CONTAINERS = {'agent-beta-hermes': 'hermes', 'agent-beta-provider': 'provider'}
NETWORKS = {'agent-beta-internal': ('172.29.244.0/28', 'br-agent-beta-i'),
            'agent-beta-egress': ('172.29.244.16/28', 'br-agent-beta-e')}
CHAINS = {'AGENT_BETA_IN', 'AGENT_BETA_FWD'}
UNITS = ['agent-beta.service', 'var-lib-agent\\x2dbeta-hermes.mount',
         'var-lib-agent\\x2dbeta-provider\\x2dstate.mount']
PUBLIC = Path('/etc/nginx/sites-available/agent-beta.redhillsmediafl.com')
ENABLED = Path('/etc/nginx/sites-enabled/agent-beta.redhillsmediafl.com')
LOOPBACK = Path('/etc/nginx/conf.d/agent-beta-loopback.conf')
CONTEXTS = [ROOT / 'build', ROOT / 'provider-build-safe-errors']
RECIPES = {'Dockerfile', 'ReviewerProvider.Dockerfile', 'reviewer-provider-proxy.py',
           'image-id', 'build-manifest.json', 'build-receipt.json', 'source-manifest.json'}
RETAINED = [Path('/var/lib/agent-beta/volumes/hermes.ext4'),
            Path('/var/lib/agent-beta/volumes/provider-state.ext4'),
            Path('/var/lib/agent-beta/hermes.pre-quota-20261117T033402Z'),
            Path('/var/lib/agent-beta/provider-state.pre-quota-20261117T033529Z'),
            Path('/var/lib/agent-beta/provider-secret'),
            Path('/home/kyle/.config/rhm/agent-beta/reviewer-login.json')]
FILES = [Path('/etc/systemd/system') / name for name in UNITS] + [
    PUBLIC, LOOPBACK, ROOT / 'compose.yaml', ROOT / 'image.env',
    ROOT / 'reviewer-isolation.py', ROOT / 'isolation.json']


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True, timeout=90)


def require(value, message):
    if not value:
        raise ValueError(message)


def safe_path(path):
    path = Path(path)
    require(path.is_absolute(), 'Expected an absolute owned path')
    require(str(path) == os.path.normpath(str(path)), 'Noncanonical owned path')
    for part in [path, *path.parents]:
        require(not part.is_symlink(), 'Symlink in owned path')
    return path


def file_record(path):
    safe_path(path)
    if not path.exists():
        return None
    st = path.stat()
    require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, 'Unexpected owned file type/link count')
    require(st.st_uid == 0 and not stat.S_IMODE(st.st_mode) & 0o022,
            'Owned host configuration must be root-owned and not group/world writable')
    return {'device': st.st_dev, 'inode': st.st_ino, 'mode': stat.S_IMODE(st.st_mode),
            'uid': st.st_uid, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def retained_metadata():
    """Record future erasure identities without reading credential or state contents."""
    result = {}
    for path in RETAINED:
        safe_path(path)
        if not path.exists():
            result[str(path)] = None
            continue
        st = path.stat()
        result[str(path)] = {'device': st.st_dev, 'inode': st.st_ino,
                             'mode': stat.S_IMODE(st.st_mode), 'uid': st.st_uid,
                             'gid': st.st_gid, 'links': st.st_nlink, 'bytes': st.st_size,
                             'allocated_bytes': st.st_blocks * 512,
                             'type': 'directory' if stat.S_ISDIR(st.st_mode) else
                                     'file' if stat.S_ISREG(st.st_mode) else 'unexpected'}
    return result


def tree_record(path):
    safe_path(path)
    if not path.exists():
        return None
    require(path.is_dir(), 'Build context is not a directory')
    root_stat = path.stat()
    require(root_stat.st_uid == 0 and not stat.S_IMODE(root_stat.st_mode) & 0o022,
            'Build context root must be root-owned and not group/world writable')
    rows, allocated = [], 0
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            entry = Path(parent) / name
            st = entry.lstat()
            require(st.st_dev == root_stat.st_dev, 'Unexpected filesystem in build context')
            rows.append([str(entry.relative_to(path)), st.st_dev, st.st_ino,
                         st.st_mode, st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns,
                         os.readlink(entry) if entry.is_symlink() else None])
            allocated += st.st_blocks * 512
    return {'device': root_stat.st_dev, 'inode': root_stat.st_ino, 'uid': root_stat.st_uid,
            'mode': stat.S_IMODE(root_stat.st_mode),
            'entries': len(rows), 'allocated_bytes': allocated,
            'metadata_sha256': hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()}


def recipe_records():
    result = []
    for context in CONTEXTS:
        if not context.exists(): continue
        for parent, dirs, files in os.walk(context, followlinks=False):
            for name in sorted(set(files) & RECIPES):
                path = Path(parent) / name
                safe_path(path)
                require(path.is_file() and path.stat().st_size <= 1024 * 1024,
                        'Unexpected build recipe size/type')
                result.append({'path': str(path), 'bytes': path.stat().st_size,
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return sorted(result, key=lambda x: x['path'])


def preserve_recipes(records):
    destination = ROOT / 'build-provenance'
    safe_path(destination)
    destination.mkdir(mode=0o700, exist_ok=True)
    st = destination.stat()
    require(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o700,
            'Build provenance destination must be private and root-owned')
    for record in records:
        path = safe_path(record['path'])
        require(any(context in path.parents for context in CONTEXTS), 'Recipe outside owned contexts')
        data = path.read_bytes()
        require(len(data) == record['bytes'] and hashlib.sha256(data).hexdigest() == record['sha256'],
                'Build recipe changed before preservation')
        target = destination / (record['sha256'] + '-' + path.name)
        safe_path(target)
        if target.exists():
            require(target.is_file() and target.stat().st_nlink == 1 and
                    target.stat().st_uid == 0 and stat.S_IMODE(target.stat().st_mode) == 0o600 and
                    hashlib.sha256(target.read_bytes()).hexdigest() == record['sha256'],
                    'Existing provenance receipt differs')
        else:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
    index = json.dumps(records, sort_keys=True, indent=2).encode()
    target = destination / (hashlib.sha256(index).hexdigest() + '-source-index.json')
    safe_path(target)
    if target.exists():
        st = target.stat()
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == 0 and
                stat.S_IMODE(st.st_mode) == 0o600 and st.st_size == len(index) and
                target.read_bytes() == index, 'Existing provenance source index differs')
    else:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(index); stream.flush(); os.fsync(stream.fileno())
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def container_record(value):
    labels = value['Config'].get('Labels') or {}
    name = value['Name'].lstrip('/')
    require(name in CONTAINERS, 'Unexpected reviewer container name')
    require(labels.get('com.docker.compose.project') == PROJECT and
            labels.get('com.docker.compose.service') == CONTAINERS[name],
            'Reviewer container Compose ownership differs')
    mounts = sorted([m['Type'], m.get('Source', ''), m['Destination'], m['RW']]
                    for m in value.get('Mounts', []))
    for kind, source, target, writable in mounts:
        require(kind == 'bind', 'Unexpected reviewer volume type')
        require(source.startswith('/opt/agent-beta/') or source.startswith('/var/lib/agent-beta/'),
                'Unexpected reviewer mount source')
    require(set(value['NetworkSettings']['Networks']) <= set(NETWORKS),
            'Reviewer container has an unexpected network')
    return {'id': value['Id'], 'name': name, 'image': value['Image'],
            'mounts': mounts, 'networks': sorted(value['NetworkSettings']['Networks']),
            'project': PROJECT, 'service': CONTAINERS[name]}


def firewall_record():
    text = run('iptables', '-S').stdout
    lines = []
    for line in text.splitlines():
        words = shlex.split(line)
        if len(words) > 1 and (words[1] in CHAINS or
                              ('-j' in words and words[words.index('-j') + 1] in CHAINS)):
            if words[1] not in CHAINS:
                require(words[1] in {'INPUT', 'DOCKER-USER'}, 'Foreign reference to reviewer chain')
                require(any(n in words for n, _ in NETWORKS.values()), 'Unscoped reviewer firewall jump')
            lines.append(line)
    nft = run('nft', '--json', 'list', 'table', 'bridge', 'agent_beta', check=False)
    require(nft.returncode == 0 or not lines, 'Missing reviewer bridge policy')
    value = json.loads(nft.stdout) if nft.returncode == 0 else None
    if value:
        value['nftables'] = [x for x in value['nftables'] if 'metainfo' not in x]
    return {'iptables': lines, 'nft': value}


def inventory():
    ids = run('docker', 'ps', '-aq').stdout.split()
    all_containers = json.loads(run('docker', 'inspect', *ids).stdout) if ids else []
    require(all(x['Name'].lstrip('/') in CONTAINERS for x in all_containers
                if (x['Config'].get('Labels') or {}).get('com.docker.compose.project') == PROJECT),
            'Unexpected container uses the reviewer Compose project')
    owned = [container_record(x) for x in all_containers if x['Name'].lstrip('/') in CONTAINERS]
    networks = []
    for name, (subnet, bridge) in NETWORKS.items():
        result = run('docker', 'network', 'inspect', name, check=False)
        if result.returncode:
            continue
        value = json.loads(result.stdout)[0]
        require((value.get('Labels') or {}).get('com.docker.compose.project') == PROJECT,
                'Reviewer network Compose ownership differs')
        require(value['Driver'] == 'bridge' and value.get('Options', {}).get('com.docker.network.bridge.name') == bridge,
                'Reviewer network bridge differs')
        require([x['Subnet'] for x in value['IPAM']['Config']] == [subnet], 'Reviewer subnet differs')
        endpoints = set((value.get('Containers') or {}).keys())
        require(endpoints <= {x['id'] for x in owned}, 'Reviewer network has another consumer')
        networks.append({'name': name, 'id': value['Id'], 'subnet': subnet, 'bridge': bridge})
    image_ids = run('docker', 'image', 'ls', '-aq', '--no-trunc').stdout.split()
    images = json.loads(run('docker', 'image', 'inspect', *sorted(set(image_ids))).stdout) if image_ids else []
    owned_images = []
    for image in images:
        tags = image.get('RepoTags') or []
        if any(t.startswith(('agent-beta-hermes:', 'agent-beta-provider:')) for t in tags) or image['Id'] in {x['image'] for x in owned}:
            owned_images.append({'id': image['Id'], 'tags': sorted(tags), 'bytes': image['Size']})
    enabled = None
    if ENABLED.exists() or ENABLED.is_symlink():
        require(ENABLED.is_symlink() and ENABLED.resolve() == PUBLIC,
                'Public reviewer enabled route differs')
        st = ENABLED.lstat()
        require(st.st_uid == 0, 'Enabled reviewer route is not root-owned')
        enabled = {'inode': st.st_ino, 'device': st.st_dev, 'target': os.readlink(ENABLED)}
    require(not run('systemctl', 'show', '--property=DropInPaths', '--value', 'agent-beta.service').stdout.strip(),
            'Reviewer service has unreviewed systemd overrides')
    return {'schema': 1, 'host': hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),
            'containers': sorted(owned, key=lambda x: x['name']), 'networks': networks,
            'files': {str(p): file_record(p) for p in FILES},
            'contexts': {str(p): tree_record(p) for p in CONTEXTS},
            'images': owned_images, 'firewall': firewall_record(), 'enabled_route': enabled,
            'recipes': recipe_records(), 'retained_state_metadata_only': retained_metadata()}


def load_manifest(path):
    safe_path(path)
    st = path.stat()
    require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid in {0, os.getuid()} and
            not stat.S_IMODE(st.st_mode) & 0o022, 'Manifest must be an owned non-writable regular file')
    value = json.loads(path.read_text())
    require(value.get('schema') == 1, 'Unknown manifest schema')
    require(set(value.get('contexts', {})) == {str(p) for p in CONTEXTS}, 'Unexpected context paths in manifest')
    require(set(value.get('files', {})) == {str(p) for p in FILES}, 'Unexpected file paths in manifest')
    return value


def assert_scope(expected, current, *, removal):
    require(expected['host'] == current['host'], 'Manifest belongs to another host')
    if removal:
        for key in ['containers', 'networks', 'files', 'firewall', 'enabled_route']:
            require(expected[key] == current[key], 'Reviewer resource changed since inventory: ' + key)


def context_unused(path):
    safe_path(path)
    require(shutil.rmtree.avoids_symlink_attacks, 'Platform lacks safe directory removal')
    ids = run('docker', 'ps', '-aq').stdout.split()
    for value in json.loads(run('docker', 'inspect', *ids).stdout) if ids else []:
        for mount in value.get('Mounts', []):
            source = Path(mount.get('Source', '/'))
            require(not (source == path or path in source.parents or source in path.parents),
                    'Build context overlaps a container mount')
    mounts = run('findmnt', '--json', '--output', 'TARGET').stdout
    def walk(items):
        for item in items:
            target = Path(item['target'])
            require(target != path and path not in target.parents, 'Mounted filesystem inside build context')
            walk(item.get('children', []))
    walk(json.loads(mounts)['filesystems'])
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            descriptors = list((process / 'fd').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise ValueError('Cannot exclude an unreadable process consumer') from None
        for link in [process / 'cwd', process / 'exe', *descriptors]:
            try:
                target = Path(os.readlink(link))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                raise ValueError('Cannot exclude an unreadable process consumer') from None
            require(target != path and path not in target.parents, 'Build context has an active process consumer')
        try:
            with (process / 'maps').open() as stream:
                mappings = stream.read(16 * 1024 * 1024 + 1)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise ValueError('Cannot exclude an unreadable process mapping consumer') from None
        require(len(mappings) <= 16 * 1024 * 1024, 'Process mapping inventory exceeds its safe bound')
        for mapping in mappings.splitlines():
            fields = mapping.split(None, 5)
            if len(fields) != 6 or not fields[5].startswith('/'):
                continue
            # procfs escapes tabs/newlines/backslashes in mapped pathnames.
            value = fields[5]
            for encoded, decoded in [('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')]:
                value = value.replace(encoded, decoded)
            target = Path(value)
            require(target != path and path not in target.parents,
                    'Build context has an active process mapping consumer')


def cleanup_plan(expected, current, keep, remove_ids=None):
    assert_scope(expected, current, removal=False)
    require(expected.get('recipes') == current.get('recipes'), 'Build recipes changed since inventory')
    active = set(run('docker', 'ps', '-aq').stdout.split())
    referenced = set()
    if active:
        referenced = {x['Image'] for x in json.loads(run('docker', 'inspect', *sorted(active)).stdout)}
    contexts, candidates = [], []
    for path, record in expected['contexts'].items():
        if record is not None:
            require(record == current['contexts'].get(path), 'Build context changed since inventory')
            context_unused(Path(path))
            contexts.append(path)
    available = {x['id']: x for x in current['images']}
    for image in expected['images']:
        if image['id'] in referenced or image['id'] in keep or image['id'] not in available:
            continue
        require(image == available[image['id']], 'Image tags changed since inventory')
        require(all(t.startswith(('agent-beta-hermes:', 'agent-beta-provider:')) for t in image['tags']),
                'Image also has a shared/unowned tag')
        candidates.append(image)
    requested = set(remove_ids or [])
    require(not requested & keep, 'An image cannot be both kept and removed')
    require(requested <= {x['id'] for x in candidates},
            'Explicit image removal is referenced, changed, missing or outside the reviewed scope')
    images = [x for x in candidates if x['id'] in requested]
    images.sort(key=lambda x: x['bytes'], reverse=True)
    return {'operation': 'cleanup', 'contexts': contexts, 'images': images,
            'unused_owned_image_candidates': candidates,
            'preserve_recipe_receipts_first': expected.get('recipes', []),
            'preserves': ['all state and credentials', 'shared models and inference', 'referenced/kept images', 'all Docker build cache']}


def public_gate():
    domain = 'agent-beta.redhillsmediafl.com'
    return (f'server {{ listen 80; listen [::]:80; server_name {domain};\n'
            'location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; try_files $uri =404; }\n'
            'location / { return 503; } }\n'
            f'server {{ listen 443 ssl; listen [::]:443 ssl; server_name {domain};\n'
            f'ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;\n'
            f'ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;\n'
            'include /etc/letsencrypt/options-ssl-nginx.conf;\n'
            'ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; location / { return 503; } }\n')


def close_public():
    previous = PUBLIC.read_bytes()
    def replace(data):
        fd, name = tempfile.mkstemp(prefix='.agent-beta-', dir=PUBLIC.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.chmod(name, 0o644)
            os.replace(name, PUBLIC)
        finally:
            if os.path.exists(name): os.unlink(name)
    replace(public_gate().encode())
    try:
        run('nginx', '-t')
    except subprocess.CalledProcessError:
        replace(previous)
        raise
    run('systemctl', 'reload', 'nginx')


def remove(expected):
    require(expected['enabled_route'] is not None and expected['files'][str(PUBLIC)] is not None,
            'Verified public reviewer gate is required')
    close_public()
    run('systemctl', 'disable', '--now', 'agent-beta.service')
    for record in expected['containers']:
        result = run('docker', 'inspect', record['id'], check=False)
        if result.returncode == 0:
            require(container_record(json.loads(result.stdout)[0]) == record, 'Container changed during removal')
            run('docker', 'stop', record['id'])
            run('docker', 'rm', record['id'])
    for record in expected['networks']:
        result = run('docker', 'network', 'inspect', record['id'], check=False)
        if result.returncode == 0:
            require(not json.loads(result.stdout)[0].get('Containers'), 'Network still has consumers')
            run('docker', 'network', 'rm', record['id'])
    require(firewall_record() == expected['firewall'], 'Firewall changed during removal')
    for line in expected['firewall']['iptables']:
        words = shlex.split(line)
        if words[0] == '-A' and words[1] not in CHAINS:
            run('iptables', '-w', '5', '-D', *words[1:])
    for chain in sorted(CHAINS):
        run('iptables', '-w', '5', '-F', chain)
        run('iptables', '-w', '5', '-X', chain)
    if expected['firewall']['nft']:
        run('nft', 'delete', 'table', 'bridge', 'agent_beta')
    for unit in UNITS[1:]:
        run('systemctl', 'disable', '--now', unit)
    for path in [Path('/etc/systemd/system') / u for u in UNITS] + [LOOPBACK]:
        require(file_record(path) == expected['files'][str(path)], 'Owned configuration changed during removal')
        if path.exists(): path.unlink()
    run('systemctl', 'daemon-reload')
    run('nginx', '-t')
    run('systemctl', 'reload', 'nginx')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inventory', 'remove', 'cleanup'])
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--keep-image', action='append', default=[])
    parser.add_argument('--remove-image', action='append', default=[])
    args = parser.parse_args()
    require(args.action == 'cleanup' or not (args.keep_image or args.remove_image),
            'Image selections apply only to cleanup')
    current = inventory()
    if args.action == 'inventory':
        require(not args.apply, 'Inventory is read-only')
        print(json.dumps(current, indent=2)); return
    require(args.manifest is not None, 'An explicit reviewed manifest is required')
    expected = load_manifest(args.manifest)
    if args.action == 'cleanup':
        plan = cleanup_plan(expected, current, set(args.keep_image), args.remove_image)
    else:
        assert_scope(expected, current, removal=True)
        plan = {'operation': 'remove', 'containers': expected['containers'], 'networks': expected['networks'],
                'units': UNITS, 'public_route': 'retained with HTTP 503 and certificate renewal',
                'preserves': ['reviewer state/backups/credentials', 'all images/build cache', 'shared models/inference', 'deployment source/receipts']}
    print(json.dumps({'apply': args.apply, 'plan': plan}, indent=2), flush=True)
    if not args.apply: return
    require(os.geteuid() == 0, 'Apply requires root')
    if args.action == 'remove':
        remove(expected)
    else:
        preserve_recipes(plan['preserve_recipe_receipts_first'])
        for path in plan['contexts']:
            require(tree_record(Path(path)) == expected['contexts'][path], 'Build context changed during cleanup')
            context_unused(Path(path)); shutil.rmtree(path)
        for image in plan['images']:
            present = json.loads(run('docker', 'image', 'inspect', image['id']).stdout)[0]
            require(sorted(present.get('RepoTags') or []) == image['tags'], 'Image tags changed during cleanup')
            ids = run('docker', 'ps', '-aq').stdout.split()
            require(not any(x['Image'] == image['id'] for x in
                            (json.loads(run('docker', 'inspect', *ids).stdout) if ids else [])),
                    'Image acquired a container reference during cleanup')
            # Docker's ordinary non-force removal preserves every referenced layer.
            # Bind removal to the checked immutable identity, never a mutable tag.
            # Multiple references cause Docker to refuse rather than forcing it.
            run('docker', 'image', 'rm', image['id'])
    print(json.dumps({'completed': args.action, 'shared_models_and_private_owner_data_unchanged': True}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Command stderr is withheld because service output may contain private data.
        raise SystemExit('Reviewer operation refused/stopped: ' +
                         (str(error) if isinstance(error, ValueError) else type(error).__name__))
