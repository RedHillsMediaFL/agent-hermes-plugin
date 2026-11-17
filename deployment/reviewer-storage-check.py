#!/usr/bin/env python3
"""Refuse reviewer startup unless writable state uses the bounded owned filesystems."""
import json
from pathlib import Path
import os
import stat
import subprocess

VOLUMES = {"hermes": (2 * 1024**3, 32768, 992, 978), "provider-state": (32 * 1024**2, 2048, 993, 993)}


def check():
    loops = json.loads(subprocess.run(["losetup", "--json", "--list", "--output", "NAME,BACK-FILE"],
        text=True, capture_output=True, check=True, timeout=10).stdout)["loopdevices"]
    backing = {item["name"]: item["back-file"] for item in loops}
    result = []
    for name, (limit, inode_limit, uid, gid) in VOLUMES.items():
        target = Path("/var/lib/agent-beta") / name
        image = Path("/var/lib/agent-beta/volumes") / (name + ".ext4")
        if target.resolve(strict=True) != target or image.resolve(strict=True) != image:
            raise RuntimeError("Reviewer state paths must not contain symlinks")
        owner, backing_stat = target.stat(), image.stat()
        if ((owner.st_uid, owner.st_gid, stat.S_IMODE(owner.st_mode)) != (uid, gid, 0o700)
                or not stat.S_ISREG(backing_stat.st_mode) or backing_stat.st_nlink != 1
                or backing_stat.st_uid != 0 or stat.S_IMODE(backing_stat.st_mode) != 0o600):
            raise RuntimeError("Reviewer state ownership or backing file identity differs")
        mount = json.loads(subprocess.run(["findmnt", "--json", "--target", str(target)],
            text=True, capture_output=True, check=True, timeout=10).stdout)["filesystems"][0]
        stats = os.statvfs(target)
        if (mount["target"] != str(target) or mount["fstype"] != "ext4"
                or backing.get(mount["source"]) != str(image)
                or image.stat().st_size != limit or stats.f_blocks * stats.f_frsize > limit
                or stats.f_files > inode_limit
                or not {"rw", "nosuid", "nodev", "noexec"}.issubset(mount["options"].split(","))):
            raise RuntimeError("Reviewer writable state is not on its bounded owned filesystem")
        result.append({"path": str(target), "maximum_bytes": limit, "inodes": stats.f_files,
                       "filesystem": mount["fstype"], "allocated_backing_bytes": backing_stat.st_blocks * 512,
                       "owner_uid": uid, "owner_gid": gid, "private_mode": True, "bounded": True})
    return {"ok": True, "volumes": result}


if __name__ == "__main__":
    print(json.dumps(check(), sort_keys=True))
