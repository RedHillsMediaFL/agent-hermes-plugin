"""Reviewer removal refusals; no host commands, writes only in temporary directories."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "review_teardown", Path(__file__).with_name("reviewer-teardown.py"))
teardown = importlib.util.module_from_spec(spec)
# Review the current source even when another verifier wrote a same-second .pyc.
source_path = Path(__file__).with_name('reviewer-teardown.py')
exec(compile(source_path.read_bytes(), str(source_path), 'exec'), teardown.__dict__)


def result(value="", code=0):
    return SimpleNamespace(stdout=value if isinstance(value, str) else json.dumps(value),
                           returncode=code)


def container(name="agent-beta-hermes", project="agent-beta"):
    return {"Id": "container-owned", "Name": "/" + name, "Image": "sha256:owned",
            "Config": {"Labels": {"com.docker.compose.project": project,
                                  "com.docker.compose.service": "hermes"}},
            "Mounts": [{"Type": "bind", "Source": "/var/lib/agent-beta/hermes",
                        "Destination": "/opt/data", "RW": True}],
            "NetworkSettings": {"Networks": {"agent-beta-internal": {}}}}


def image(identifier="sha256:owned", tags=None):
    return {"id": identifier, "tags": ["agent-beta-hermes:reviewed"] if tags is None else tags,
            "bytes": 4096}


def manifest():
    return {"schema": 1, "host": "synthetic-host",
            "containers": [teardown.container_record(container())],
            "networks": [{"id": "network-owned", "name": "agent-beta-internal"}],
            "files": {str(p): None for p in teardown.FILES},
            "contexts": {str(p): None for p in teardown.CONTEXTS},
            "images": [image()], "firewall": {"iptables": [], "nft": None},
            "enabled_route": None}


class ScopeTests(unittest.TestCase):
    def test_other_host_is_refused_for_removal_and_cleanup(self):
        expected, current = manifest(), manifest()
        current["host"] = "other-host"
        for removal in (False, True):
            with self.subTest(removal=removal), self.assertRaisesRegex(ValueError, "another host"):
                teardown.assert_scope(expected, current, removal=removal)

    def test_every_removal_resource_scope_change_is_refused(self):
        expected = manifest()
        for key in ("containers", "networks", "files", "firewall", "enabled_route"):
            current = copy.deepcopy(expected)
            current[key] = {"foreign": True}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                teardown.assert_scope(expected, current, removal=True)

    def test_changed_container_image_is_refused(self):
        expected, current = manifest(), manifest()
        current["containers"][0]["image"] = "sha256:replacement"
        with self.assertRaisesRegex(ValueError, "containers"):
            teardown.assert_scope(expected, current, removal=True)

    def test_foreign_container_ownership_mount_or_network_is_refused(self):
        cases = []
        foreign = container(project="owner-production")
        cases.append(foreign)
        foreign = container()
        foreign["Mounts"][0]["Source"] = "/home/owner"
        cases.append(foreign)
        foreign = container()
        foreign["Mounts"][0]["Type"] = "volume"
        cases.append(foreign)
        foreign = container()
        foreign["NetworkSettings"]["Networks"]["owner-inference"] = {}
        cases.append(foreign)
        for index, value in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                teardown.container_record(value)

    def test_unexpected_container_in_reviewer_project_is_refused(self):
        foreign = container(name="agent-beta-extra")
        def read_only(*args, **kwargs):
            if args == ("docker", "ps", "-aq"):
                return result("container-owned")
            if args == ("docker", "inspect", "container-owned"):
                return result([foreign])
            self.fail("Unexpected command: " + repr(args))
        with patch.object(teardown, "run", side_effect=read_only), self.assertRaisesRegex(ValueError, "Unexpected container"):
            teardown.inventory()

    def test_foreign_network_consumer_is_refused_before_any_mutation(self):
        value = {"Id": "network-owned", "Labels": {"com.docker.compose.project": "agent-beta"},
                 "Driver": "bridge", "Options": {"com.docker.network.bridge.name": "br-agent-beta-i"},
                 "IPAM": {"Config": [{"Subnet": "172.29.244.0/28"}]},
                 "Containers": {"container-owned": {}, "foreign-owner": {}}}
        def read_only(*args, **kwargs):
            if args == ("docker", "ps", "-aq"):
                return result("container-owned")
            if args == ("docker", "inspect", "container-owned"):
                return result([container()])
            if args == ("docker", "network", "inspect", "agent-beta-internal"):
                return result([value])
            self.fail("Unexpected command: " + repr(args))
        with patch.object(teardown, "run", side_effect=read_only), self.assertRaisesRegex(ValueError, "another consumer"):
            teardown.inventory()


class ManifestPathTests(unittest.TestCase):
    def test_symlink_in_any_owned_path_component_is_refused(self):
        for symlink in (Path("/opt/agent-beta/build"), Path("/opt/agent-beta")):
            with self.subTest(symlink=str(symlink)), patch.object(
                    Path, "is_symlink", autospec=True, side_effect=lambda path: path == symlink):
                with self.assertRaisesRegex(ValueError, "Symlink"):
                    teardown.safe_path(Path("/opt/agent-beta/build"))

    def test_relative_or_traversing_paths_are_refused(self):
        for value in ("relative/build", "/opt/agent-beta/../owner"):
            with self.subTest(path=value), self.assertRaises(ValueError):
                teardown.safe_path(Path(value))

    def test_hardlinked_owned_file_is_refused_without_reading_content(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=2, st_uid=0)
        with patch.object(teardown, "safe_path"), patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "stat", return_value=metadata), patch.object(Path, "read_bytes") as read:
            with self.assertRaisesRegex(ValueError, "link count"):
                teardown.file_record(Path("/opt/agent-beta/image.env"))
            read.assert_not_called()

    def test_manifest_cannot_introduce_foreign_context_or_file_paths(self):
        for key in ("contexts", "files"):
            value = manifest()
            value[key]["/home/owner/keep"] = None
            path = Mock()
            path.stat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=0)
            path.read_text.return_value = json.dumps(value)
            with self.subTest(key=key), patch.object(teardown, "safe_path"), \
                    self.assertRaisesRegex(ValueError, "Unexpected .* paths"):
                teardown.load_manifest(path)

    def test_manifest_group_or_world_writable_is_refused(self):
        for permission in (0o620, 0o602):
            path = Mock()
            path.stat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | permission, st_nlink=1, st_uid=0)
            with self.subTest(permission=permission), patch.object(teardown, "safe_path"), self.assertRaises(ValueError):
                teardown.load_manifest(path)
            path.read_text.assert_not_called()


class CleanupPlanTests(unittest.TestCase):
    def plan(self, expected, current=None, keep=(), containers=(), remove_ids=()):
        def read_only(*args, **kwargs):
            if args == ("docker", "ps", "-aq"):
                return result(" ".join(x["Id"] for x in containers))
            if args[:2] == ("docker", "inspect"):
                return result(list(containers))
            self.fail("Unexpected command: " + repr(args))
        with patch.object(teardown, "run", side_effect=read_only):
            return teardown.cleanup_plan(expected, expected if current is None else current, set(keep), remove_ids)

    def test_no_image_removal_is_planned_without_explicit_ids(self):
        plan = self.plan(manifest())
        self.assertEqual(plan["images"], [])
        self.assertEqual(plan["unused_owned_image_candidates"], [image()])

    def test_explicit_unused_reviewed_image_can_be_planned(self):
        plan = self.plan(manifest(), remove_ids=["sha256:owned"])
        self.assertEqual(plan["images"], [image()])

    def test_explicit_reference_keep_missing_and_foreign_ids_are_refused(self):
        current = manifest()
        current["images"] = []
        cases = [{"containers": [{"Id": "owner", "Image": "sha256:owned"}]},
                 {"keep": ["sha256:owned"]}, {"current": current},
                 {"remove_ids": ["sha256:foreign"]}]
        for case in cases:
            arguments = {"remove_ids": ["sha256:owned"], **case}
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.plan(manifest(), **arguments)

    def test_referenced_images_are_preserved_including_stopped_containers(self):
        plan = self.plan(manifest(), containers=[{"Id": "stopped-owner", "Image": "sha256:owned"}])
        self.assertEqual(plan["images"], [])

    def test_explicit_kept_images_are_preserved(self):
        self.assertEqual(self.plan(manifest(), keep=["sha256:owned"])["images"], [])

    def test_disappeared_images_are_preserved_without_removal_attempt(self):
        current = manifest()
        current["images"] = []
        self.assertEqual(self.plan(manifest(), current)["images"], [])

    def test_changed_image_tags_are_refused(self):
        current = manifest()
        current["images"][0]["tags"] = ["agent-beta-hermes:replacement"]
        with self.assertRaisesRegex(ValueError, "tags changed"):
            self.plan(manifest(), current)

    def test_shared_image_tag_is_refused_even_if_present_in_reviewed_inventory(self):
        expected = manifest()
        expected["images"][0]["tags"].append("owner-production:current")
        with self.assertRaisesRegex(ValueError, "shared/unowned"):
            self.plan(expected)

    def test_changed_context_is_refused_before_consumer_check(self):
        expected, current = manifest(), manifest()
        path = str(teardown.CONTEXTS[0])
        expected["contexts"][path] = {"inode": 1}
        current["contexts"][path] = {"inode": 2}
        with patch.object(teardown, "context_unused") as unused, self.assertRaisesRegex(ValueError, "context changed"):
            self.plan(expected, current)
        unused.assert_not_called()

    def test_active_context_is_refused_and_preserved(self):
        expected = manifest()
        expected["contexts"][str(teardown.CONTEXTS[0])] = {"inode": 1}
        with patch.object(teardown, "context_unused", side_effect=ValueError("active process consumer")), \
                patch.object(teardown.shutil, "rmtree") as remove, self.assertRaisesRegex(ValueError, "active process"):
            self.plan(expected)
        remove.assert_not_called()


class ContextConsumerTests(unittest.TestCase):
    def check(self, *, containers=(), mounts=None, processes=(), target="/unrelated",
              executable="/unrelated", mappings="", unreadable_maps=False):
        def read_only(*args, **kwargs):
            if args == ("docker", "ps", "-aq"):
                return result(" ".join(str(index) for index, _ in enumerate(containers)))
            if args[:2] == ("docker", "inspect"):
                return result(list(containers))
            if args == ("findmnt", "--json", "--output", "TARGET"):
                return result({"filesystems": [] if mounts is None else mounts})
            self.fail("Unexpected command: " + repr(args))
        def link_target(path):
            return executable if Path(path).name == "exe" else target
        def open_maps(path, *args, **kwargs):
            self.assertEqual(Path(path).name, "maps")
            if unreadable_maps:
                raise PermissionError("synthetic unreadable process mappings")
            return io.StringIO(mappings)
        with patch.object(teardown, "safe_path"), patch.object(teardown, "run", side_effect=read_only), \
                patch.object(Path, "iterdir", autospec=True,
                             side_effect=lambda path: iter(processes if path == Path("/proc") else [])), \
                patch.object(teardown.os, "readlink", side_effect=link_target), \
                patch.object(Path, "open", autospec=True, side_effect=open_maps):
            teardown.context_unused(teardown.CONTEXTS[0])

    def test_parent_equal_and_child_container_mounts_preserve_context(self):
        for source in ("/opt/agent-beta", "/opt/agent-beta/build", "/opt/agent-beta/build/file"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "container mount"):
                self.check(containers=[{"Mounts": [{"Source": source}]}])

    def test_nested_mounted_filesystem_preserves_context(self):
        with self.assertRaisesRegex(ValueError, "Mounted filesystem"):
            self.check(mounts=[{"target": "/", "children": [{"target": "/opt/agent-beta/build/cache"}]}])

    def test_active_process_cwd_preserves_context(self):
        with self.assertRaisesRegex(ValueError, "active process consumer"):
            self.check(processes=[Path("/proc/42")], target="/opt/agent-beta/build/work")

    def test_active_executable_preserves_context_with_unrelated_cwd_and_closed_descriptors(self):
        with self.assertRaisesRegex(ValueError, "active process consumer"):
            self.check(processes=[Path("/proc/42")], executable="/opt/agent-beta/build/native-hermes")

    def test_mapped_file_preserves_context_with_closed_descriptors_and_unrelated_executable(self):
        for suffix in ("", " (deleted)"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "active process (mapping )?consumer"):
                self.check(processes=[Path("/proc/42")], mappings=(
                    "00400000-00401000 r-xp 00000000 08:01 42 "
                    "/opt/agent-beta/build/native library.so" + suffix + "\n"))

    def test_unreadable_process_mappings_refuse_cleanup(self):
        with self.assertRaisesRegex(ValueError, "unreadable process"):
            self.check(processes=[Path("/proc/42")], unreadable_maps=True)

    def test_unrelated_mappings_do_not_block_owned_context_cleanup(self):
        self.check(processes=[Path("/proc/42")], mappings=(
            "00400000-00401000 r-xp 00000000 08:01 42 /usr/bin/other\n"
            "00500000-00501000 rw-p 00000000 00:00 0 [heap]\n"))

    def test_open_process_file_preserves_context_even_when_cwd_is_elsewhere(self):
        def read_only(*args, **kwargs):
            if args == ("docker", "ps", "-aq"):
                return result("")
            if args == ("findmnt", "--json", "--output", "TARGET"):
                return result({"filesystems": []})
            self.fail("Unexpected command: " + repr(args))
        def link_target(path):
            return "/opt/agent-beta/build/open-file" if Path(path).name == "7" else "/unrelated"
        with patch.object(teardown, "safe_path"), patch.object(teardown, "run", side_effect=read_only), \
                patch.object(Path, "iterdir", autospec=True, side_effect=lambda path: iter(
                    [Path("/proc/42")] if path == Path("/proc") else [Path("/proc/42/fd/7")])), \
                patch.object(teardown.os, "readlink", side_effect=link_target), \
                self.assertRaisesRegex(ValueError, "active process consumer"):
            teardown.context_unused(teardown.CONTEXTS[0])

    def test_unreadable_process_descriptors_or_cwd_refuse_cleanup(self):
        for denied in ("descriptors", "cwd"):
            def read_only(*args, **kwargs):
                if args == ("docker", "ps", "-aq"):
                    return result("")
                if args == ("findmnt", "--json", "--output", "TARGET"):
                    return result({"filesystems": []})
                self.fail("Unexpected command: " + repr(args))
            def entries(path):
                if path == Path("/proc"):
                    return iter([Path("/proc/42")])
                if denied == "descriptors":
                    raise PermissionError("synthetic unreadable process")
                return iter([])
            with self.subTest(denied=denied), patch.object(teardown, "safe_path"), \
                    patch.object(teardown, "run", side_effect=read_only), \
                    patch.object(Path, "iterdir", autospec=True, side_effect=entries), \
                    patch.object(teardown.os, "readlink", side_effect=PermissionError("synthetic unreadable cwd")), \
                    self.assertRaisesRegex(ValueError, "unreadable process"):
                teardown.context_unused(teardown.CONTEXTS[0])


class ProvenanceIndexTests(unittest.TestCase):
    def preserve_with_existing_index(self, kind):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            context = root / "build"
            context.mkdir(mode=0o700)
            source = context / "Dockerfile"
            data = b"FROM scratch\n"
            source.write_bytes(data)
            records = [{"path": str(source), "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()}]
            destination = root / "build-provenance"
            destination.mkdir(mode=0o700)
            index = json.dumps(records, sort_keys=True, indent=2).encode()
            target = destination / (hashlib.sha256(index).hexdigest() + "-source-index.json")
            if kind == "directory":
                target.mkdir(mode=0o700)
            else:
                target.write_bytes(b"corrupt index" if kind == "corrupt" else index)
                target.chmod(0o600)
                if kind == "hard-linked":
                    os.link(target, destination / "second-link.json")

            original_stat = Path.stat
            def root_owned_stat(path, *args, **kwargs):
                metadata = list(original_stat(path, *args, **kwargs))
                metadata[4] = 0  # Model the root-owned deployment without requiring root tests.
                return os.stat_result(metadata)

            with patch.object(teardown, "ROOT", root), \
                    patch.object(teardown, "CONTEXTS", [context, root / "provider-build-safe-errors"]), \
                    patch.object(Path, "stat", autospec=True, side_effect=root_owned_stat):
                if kind == "valid":
                    teardown.preserve_recipes(records)
                    self.assertEqual(target.read_bytes(), index)
                else:
                    with self.assertRaises(ValueError):
                        teardown.preserve_recipes(records)
                self.assertEqual(source.read_bytes(), data)

    def test_corrupt_existing_provenance_index_is_refused(self):
        self.preserve_with_existing_index("corrupt")

    def test_directory_shaped_provenance_index_is_refused(self):
        self.preserve_with_existing_index("directory")

    def test_hard_linked_provenance_index_is_refused(self):
        self.preserve_with_existing_index("hard-linked")

    def test_valid_existing_provenance_index_can_be_reused(self):
        self.preserve_with_existing_index("valid")


class CommandSafetyTests(unittest.TestCase):
    def test_remove_and_cleanup_dry_run_never_mutate(self):
        for action in ("remove", "cleanup"):
            expected = manifest()
            def read_only(*args, **kwargs):
                if args == ("docker", "ps", "-aq"):
                    return result("")
                self.fail("Dry-run attempted unexpected command: " + repr(args))
            with self.subTest(action=action), patch.object(sys, "argv", ["reviewer-teardown.py", action, "--manifest", "/reviewed.json"]), \
                    patch.object(teardown, "inventory", return_value=copy.deepcopy(expected)), \
                    patch.object(teardown, "load_manifest", return_value=expected), \
                    patch.object(teardown, "run", side_effect=read_only), \
                    patch.object(teardown, "remove") as remove, patch.object(teardown, "close_public") as close, \
                    patch.object(teardown, "preserve_recipes") as preserve, \
                    patch.object(teardown.shutil, "rmtree") as rmtree, patch.object(Path, "unlink") as unlink, \
                    patch.object(sys, "stdout", new_callable=io.StringIO) as output:
                teardown.main()
                report = json.loads(output.getvalue())
                self.assertFalse(report["apply"])
                self.assertEqual(report["plan"]["operation"], action)
                for mutation in (remove, close, preserve, rmtree, unlink):
                    mutation.assert_not_called()

    def test_inventory_apply_flag_is_refused_without_mutation(self):
        with patch.object(sys, "argv", ["reviewer-teardown.py", "inventory", "--apply"]), \
                patch.object(teardown, "inventory", return_value=manifest()), \
                patch.object(teardown, "run") as command, self.assertRaisesRegex(ValueError, "read-only"):
            teardown.main()
        command.assert_not_called()

    def test_apply_requires_root_before_mutation(self):
        expected = manifest()
        with patch.object(sys, "argv", ["reviewer-teardown.py", "remove", "--manifest", "/reviewed.json", "--apply"]), \
                patch.object(teardown, "inventory", return_value=expected), patch.object(teardown, "load_manifest", return_value=expected), \
                patch.object(teardown.os, "geteuid", return_value=501), patch.object(teardown, "remove") as remove, \
                patch.object(sys, "stdout", new_callable=io.StringIO), self.assertRaisesRegex(ValueError, "root"):
            teardown.main()
        remove.assert_not_called()

    def test_image_retagged_after_planning_is_refused_before_removal(self):
        expected = manifest()
        plan = {"operation": "cleanup", "contexts": [], "images": expected["images"],
                "preserve_recipe_receipts_first": []}
        def read_only(*args, **kwargs):
            if args == ("docker", "image", "inspect", "sha256:owned"):
                return result([{"Id": "sha256:owned", "RepoTags": ["owner-production:current"]}])
            self.fail("Image mutation before fresh ownership validation: " + repr(args))
        with patch.object(sys, "argv", ["reviewer-teardown.py", "cleanup", "--manifest", "/reviewed.json", "--apply"]), \
                patch.object(teardown, "inventory", return_value=expected), patch.object(teardown, "load_manifest", return_value=expected), \
                patch.object(teardown, "cleanup_plan", return_value=plan), patch.object(teardown.os, "geteuid", return_value=0), \
                patch.object(teardown, "preserve_recipes"), patch.object(teardown, "run", side_effect=read_only), \
                patch.object(sys, "stdout", new_callable=io.StringIO), \
                self.assertRaisesRegex(ValueError, "tags changed"):
            teardown.main()

    def test_image_referenced_after_planning_is_refused_before_removal(self):
        expected = manifest()
        plan = {"operation": "cleanup", "contexts": [], "images": expected["images"],
                "preserve_recipe_receipts_first": []}
        def read_only(*args, **kwargs):
            if args == ("docker", "image", "inspect", "sha256:owned"):
                return result([{"Id": "sha256:owned", "RepoTags": image()["tags"]}])
            if args == ("docker", "ps", "-aq"):
                return result("new-owner-container")
            if args == ("docker", "inspect", "new-owner-container"):
                return result([{"Image": "sha256:owned"}])
            self.fail("Referenced image mutation attempted: " + repr(args))
        with patch.object(sys, "argv", ["reviewer-teardown.py", "cleanup", "--manifest", "/reviewed.json", "--apply"]), \
                patch.object(teardown, "inventory", return_value=expected), patch.object(teardown, "load_manifest", return_value=expected), \
                patch.object(teardown, "cleanup_plan", return_value=plan), patch.object(teardown.os, "geteuid", return_value=0), \
                patch.object(teardown, "preserve_recipes"), patch.object(teardown, "run", side_effect=read_only), \
                patch.object(sys, "stdout", new_callable=io.StringIO), self.assertRaisesRegex(ValueError, "container reference"):
            teardown.main()

    def test_apply_removes_only_explicit_immutable_image_id_without_force_or_pruning(self):
        expected = manifest()
        plan = {"operation": "cleanup", "contexts": [], "images": expected["images"],
                "preserve_recipe_receipts_first": []}
        calls = []
        def checked_command(*args, **kwargs):
            calls.append(args)
            if args == ("docker", "image", "inspect", "sha256:owned"):
                return result([{"Id": "sha256:owned", "RepoTags": image()["tags"]}])
            if args == ("docker", "ps", "-aq"):
                return result("")
            if args == ("docker", "image", "rm", "sha256:owned"):
                return result("")
            self.fail("Unscoped or mutable-reference mutation attempted: " + repr(args))
        with patch.object(sys, "argv", ["reviewer-teardown.py", "cleanup", "--manifest", "/reviewed.json", "--apply"]), \
                patch.object(teardown, "inventory", return_value=expected), patch.object(teardown, "load_manifest", return_value=expected), \
                patch.object(teardown, "cleanup_plan", return_value=plan), patch.object(teardown.os, "geteuid", return_value=0), \
                patch.object(teardown, "preserve_recipes"), patch.object(teardown, "run", side_effect=checked_command), \
                patch.object(sys, "stdout", new_callable=io.StringIO):
            teardown.main()
        self.assertEqual(calls[-1], ("docker", "image", "rm", "sha256:owned"))


if __name__ == "__main__":
    unittest.main()
