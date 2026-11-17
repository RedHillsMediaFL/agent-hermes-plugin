#!/usr/bin/env python3
"""Refuse reviewer startup unless writable state uses the bounded owned filesystems."""
import json
from pathlib import Path
import os
import subprocess

VOLUMES = {"hermes": (2 * 1024**3, 32768), "provider-state": (32 * 1024**2, 2048)}


def check():
    loops = json.loads(subprocess.run(["losetup", "--json", "--list", "--output", "NAME,BACK-FILE"],
        text=True, capture_output=True, check=True, timeout=10).stdout)["loopdevices"]
    backing = {item["name"]: item["back-file"] for item in loops}
    result = []
    for name, (limit, inode_limit) in VOLUMES.items():
        target = Path("/var/lib/agent-beta") / name
        image = Path("/var/lib/agent-beta/volumes") / (name + ".ext4")
        mount = json.loads(subprocess.run(["findmnt", "--json", "--target", str(target)],
            text=True, capture_output=True, check=True, timeout=10).stdout)["filesystems"][0]
        stats = os.statvfs(target)
        if (mount["target"] != str(target) or mount["fstype"] != "ext4"
                or backing.get(mount["source"]) != str(image)
                or image.stat().st_size != limit or stats.f_blocks * stats.f_frsize > limit
                or stats.f_files > inode_limit
                or not {"nosuid", "nodev", "noexec"}.issubset(mount["options"].split(","))):
            raise RuntimeError("Reviewer writable state is not on its bounded owned filesystem")
        result.append({"path": str(target), "maximum_bytes": limit, "inodes": stats.f_files,
                       "filesystem": mount["fstype"], "bounded": True})
    return {"ok": True, "volumes": result}


if __name__ == "__main__":
    print(json.dumps(check(), sort_keys=True))
