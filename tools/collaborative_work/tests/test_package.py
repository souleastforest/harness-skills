import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


SKILLS = ["brainstorm", "complexity", "develop", "help", "maintain", "matt", "start", "story", "team"]
REFERENCES = ["brainstorming-dialogue", "collaboration-patterns", "commands", "matt-execution",
              "skill-maintenance", "software-development", "story-learning", "task-complexity",
              "team-orchestration"]
TOOL_ROOT = Path(__file__).resolve().parents[1]


def write_fixture(root, version="1.2.3"):
    root.mkdir(parents=True)
    files = {
        ".codex-plugin/plugin.json": json.dumps({"name": "collaborative-work", "version": version,
                                                   "skills": "./skills/"}),
        ".agents/plugins/marketplace.json": json.dumps({"name": "fixture-marketplace", "plugins": [{
            "name": "collaborative-work", "source": {"source": "local", "path": "./"}}]}),
        "README.md": "# Fixture\n[Start](skills/start/SKILL.md)\n",
        "skills/start/LICENSE.superpowers": "Upstream license bytes\n",
    }
    for name in SKILLS:
        files[f"skills/{name}/SKILL.md"] = (f"---\nname: {name}\ndescription: Fixture {name}.\n"
                                              f"metadata:\n  version: '{version}'\n---\n\n# Fixture\n")
        files[f"skills/{name}/agents/openai.yaml"] = (
            f"interface:\n  display_name: 'collaborative-work:{name}'\n"
            f"  short_description: 'Fixture {name}'\n"
            f"  default_prompt: 'Use $collaborative-work:{name} for this task.'\n"
            + ("policy:\n  allow_implicit_invocation: false\n" if name == "team" else ""))
    for name in REFERENCES:
        files[f"skills/start/references/{name}.md"] = f"# {name}\n"
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = write_fixture(self.base / "source")

    @property
    def api(self):
        return importlib.import_module("cwrelease.package")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "cwrelease", *map(str, arguments)],
                              cwd=TOOL_ROOT, capture_output=True, text=True)

    def build(self):
        return self.api.build(self.source, self.base / "dist")

    def verify(self, built, **kwargs):
        return self.api.verify(built["archive"], built["manifest"], built["checksum"], **kwargs)

    def refresh_receipts(self, built, mutate_manifest=None):
        archive = Path(built["archive"])
        manifest_path = Path(built["manifest"])
        manifest = json.loads(manifest_path.read_text())
        manifest["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        if mutate_manifest:
            mutate_manifest(manifest)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        Path(built["checksum"]).write_text("".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted([archive, manifest_path])))

    def rewrite_zip(self, built, extra_name=None, duplicate=False, symlink=False):
        path = Path(built["archive"])
        with zipfile.ZipFile(path) as old:
            members = [(i.filename, old.read(i)) for i in old.infolist()]
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as new:
            for name, content in members:
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o120777 if symlink and name.endswith("README.md") else 0o100644) << 16
                new.writestr(info, content)
            if extra_name:
                new.writestr(extra_name, b"escape")
            if duplicate:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    new.writestr(members[0][0], members[0][1])
        self.refresh_receipts(built)

    def test_cli_validate_returns_version_from_manifest(self):
        result = self.cli("validate", self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["version"], "1.2.3")

    def test_validate_preserves_source_and_reports_full_byte_inventory(self):
        before = {p.relative_to(self.source).as_posix(): p.read_bytes()
                  for p in self.source.rglob("*") if p.is_file()}
        result = self.api.validate_source(self.source)
        self.assertEqual(result["skills"], SKILLS)
        self.assertEqual(result["marketplace"], "fixture-marketplace")
        self.assertEqual(result["files"]["README.md"], hashlib.sha256(before["README.md"]).hexdigest())
        self.assertEqual(set(result["files"]), set(before))
        self.assertEqual(before, {p.relative_to(self.source).as_posix(): p.read_bytes()
                                  for p in self.source.rglob("*") if p.is_file()})

    def test_missing_skill_fails(self):
        shutil.rmtree(self.source / "skills/help")
        with self.assertRaises(self.api.ReleaseError):
            self.api.validate_source(self.source)

    def test_wrong_identity_or_version_fails(self):
        p = self.source / "skills/help/SKILL.md"
        for bad in [p.read_text().replace("name: help", "name: story"),
                    p.read_text().replace("'1.2.3'", "'1.2.4'")]:
            with self.subTest(bad=bad):
                p.write_text(bad)
                with self.assertRaises(self.api.ReleaseError):
                    self.api.validate_source(self.source)

    def test_duplicate_yaml_or_json_keys_fail(self):
        p = self.source / "skills/help/SKILL.md"
        p.write_text(p.read_text().replace("name: help", "name: help\nname: story"))
        with self.assertRaises(self.api.ReleaseError):
            self.api.validate_source(self.source)
        p.write_text(p.read_text().replace("name: help\nname: story", "name: help"))
        (self.source / ".codex-plugin/plugin.json").write_text(
            '{"name":"collaborative-work","version":"1.2.3","version":"2.0.0","skills":"./skills/"}')
        with self.assertRaises(self.api.ReleaseError):
            self.api.validate_source(self.source)

    def test_duplicate_catalog_entry_fails(self):
        p = self.source / ".agents/plugins/marketplace.json"
        d = json.loads(p.read_text())
        d["plugins"].append(d["plugins"][0])
        p.write_text(json.dumps(d))
        with self.assertRaises(self.api.ReleaseError):
            self.api.validate_source(self.source)

    def test_manifest_license_metadata_accepts_mit(self):
        path = self.source / ".codex-plugin/plugin.json"
        manifest = json.loads(path.read_text())
        manifest["license"] = "MIT"
        path.write_text(json.dumps(manifest))
        result = self.cli("validate", self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["version"], "1.2.3")

    def test_manifest_license_metadata_rejects_empty_strings_and_invalid_types(self):
        path = self.source / ".codex-plugin/plugin.json"
        manifest = json.loads(path.read_text())
        for value in ["", " \t\n", None, 123, True, ["MIT"], {"id": "MIT"}]:
            with self.subTest(license=value):
                manifest["license"] = value
                path.write_text(json.dumps(manifest))
                with self.assertRaises(self.api.ReleaseError):
                    self.api.validate_source(self.source)

    def test_manifest_license_does_not_enable_executable_components(self):
        path = self.source / ".codex-plugin/plugin.json"
        manifest = json.loads(path.read_text())
        manifest["license"] = "MIT"
        for component in ["hooks", "mcpServers"]:
            with self.subTest(component=component):
                candidate = {**manifest, component: {"command": "unreviewed"}}
                path.write_text(json.dumps(candidate))
                with self.assertRaises(self.api.ReleaseError):
                    self.api.validate_source(self.source)

    def test_frontmatter_delimiters_are_lines_not_description_text(self):
        p = self.source / "skills/help/SKILL.md"
        p.write_text(p.read_text().replace("description: Fixture help.", "description: 'before --- after'"))
        self.api.validate_source(self.source)

    def test_declared_third_party_license_is_included_in_inventory(self):
        (self.source / "LICENSE.matt").write_bytes(b"Original upstream license\r\n")
        result = self.api.validate_source(self.source)
        self.assertEqual(result["files"]["LICENSE.matt"],
                         hashlib.sha256(b"Original upstream license\r\n").hexdigest())

    def test_team_policy_requires_yaml_boolean_false(self):
        p = self.source / "skills/team/agents/openai.yaml"
        original = p.read_text()
        for replacement in ["true", "'false'", "null"]:
            with self.subTest(value=replacement):
                p.write_text(original.replace("false", replacement))
                with self.assertRaises(self.api.ReleaseError):
                    self.api.validate_source(self.source)

    def test_ui_prompt_requires_complete_matching_namespace(self):
        p = self.source / "skills/help/agents/openai.yaml"
        p.write_text(p.read_text().replace("$collaborative-work:help", "$help"))
        with self.assertRaises(self.api.ReleaseError):
            self.api.validate_source(self.source)

    def test_links_must_exist_and_stay_inside_root(self):
        p = self.source / "README.md"
        for target in ["../outside.md", "%2e%2e/outside.md", "/etc/passwd", "missing.md", "C:\\outside.md",
                       "file:///etc/passwd"]:
            with self.subTest(target=target):
                p.write_text(f"[unsafe]({target})\n")
                with self.assertRaises(self.api.ReleaseError):
                    self.api.validate_source(self.source)

    def test_sibling_skill_links_and_external_links_are_valid(self):
        p = self.source / "skills/help/SKILL.md"
        p.write_text(p.read_text() + "[start](../start/SKILL.md#section)\n[web](https://example.com/a)\n")
        self.api.validate_source(self.source)

    def test_markdown_nested_labels_fail_validate_build_and_verify(self):
        built = self.build()
        invalid = "[outer [inner]](../missing-outside.md)\n"
        (self.source / "README.md").write_text(invalid)
        for operation in [lambda: self.api.validate_source(self.source),
                          lambda: self.api.build(self.source, self.base / "invalid-dist")]:
            with self.subTest(operation=operation):
                with self.assertRaises(self.api.ReleaseError):
                    operation()
        archive_path = Path(built["archive"])
        with zipfile.ZipFile(archive_path) as old:
            members = [(info, old.read(info)) for info in old.infolist()]
        with zipfile.ZipFile(archive_path, "w") as new:
            for info, content in members:
                new.writestr(info, invalid.encode() if info.filename.endswith("README.md") else content)
        self.refresh_receipts(built, lambda manifest: manifest["files"].update(
            {"README.md": hashlib.sha256(invalid.encode()).hexdigest()}))
        with self.assertRaises(self.api.ReleaseError):
            self.verify(built)

    def test_markdown_images_and_used_references_check_resolved_targets(self):
        cases = [
            "![outer [inner]](../missing-outside.md)\n",
            "[outer [inner]][outside]\n\n[outside]: ../missing-outside.md\n",
            "[outside][]\n\n[outside]: ../missing-outside.md\n",
            "[outside]\n\n[outside]: ../missing-outside.md\n",
            "![label][picture]\n\n[picture]: ../missing-outside.md\n",
        ]
        for text in cases:
            with self.subTest(markdown=text):
                (self.source / "README.md").write_text(text)
                with self.assertRaises(self.api.ReleaseError):
                    self.api.validate_source(self.source)

    def test_markdown_nested_labels_and_references_accept_existing_targets(self):
        (self.source / "README.md").write_text(
            '[outer [inner]](skills/start/SKILL.md "Title")\n\n'
            '[go][target] ![picture][target]\n\n'
            '[target]: <skills/start/SKILL.md>\n  "Multiline title"\n')
        self.api.validate_source(self.source)

    def test_markdown_code_and_escaped_examples_are_not_links(self):
        (self.source / "README.md").write_text(
            "`[inline](../outside.md)`\n\n"
            "```md\n[block](../outside.md)\n![image](../outside.md)\n"
            "[ref]: ../outside.md\n```\n\n"
            "    [indented](../outside.md)\n\n"
            "\\[escaped](../outside.md)\n\n"
            "[real](skills/start/SKILL.md)\n")
        self.api.validate_source(self.source)

    def test_markdown_code_does_not_hide_a_real_link_later_in_document(self):
        (self.source / "README.md").write_text(
            "`[example](../outside.md)`\n\n[outer [inner]](../outside.md)\n")
        with self.assertRaises(self.api.ReleaseError):
            self.api.validate_source(self.source)

    def test_unexpected_runtime_files_or_directories_fail(self):
        for path in [".env", "install.sh", "skills/help/run.py", "skills/start/references/unreviewed.md", "empty"]:
            with self.subTest(path=path):
                p = self.source / path
                if path == "empty":
                    p.mkdir()
                else:
                    p.write_text("unexpected")
                with self.assertRaises(self.api.ReleaseError):
                    self.api.validate_source(self.source)
                p.rmdir() if p.is_dir() else p.unlink()

    def test_snapshot_rejects_symlink_root_and_members(self):
        alias = self.base / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(self.api.ReleaseError):
            self.api.snapshot_tree(alias)
        alias.unlink()
        (self.source / "README.md").unlink()
        (self.source / "README.md").symlink_to(self.base / "missing")
        with self.assertRaises(self.api.ReleaseError):
            self.api.snapshot_tree(self.source)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_snapshot_rejects_special_file_without_reading_it(self):
        os.mkfifo(self.source / "fifo")
        with self.assertRaises(self.api.ReleaseError):
            self.api.snapshot_tree(self.source)

    def test_build_is_reproducible_across_directories_timestamps_and_modes(self):
        first = self.build()
        copied = self.base / "another/source"
        shutil.copytree(self.source, copied)
        for p in copied.rglob("*"):
            os.utime(p, (1700000000, 1700000000))
            if p.is_file():
                p.chmod(0o600)
        second = self.api.build(copied, self.base / "another/dist")
        for key in ["archive", "manifest", "checksum"]:
            self.assertEqual(Path(first[key]).read_bytes(), Path(second[key]).read_bytes())
        with zipfile.ZipFile(first["archive"]) as archive:
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in archive.infolist()))
        self.verify(first)

    def test_same_version_changed_bytes_never_overwrites_outputs(self):
        built = self.build()
        before = {key: Path(built[key]).read_bytes() for key in ["archive", "manifest", "checksum"]}
        self.api.build(self.source, self.base / "dist")
        (self.source / "README.md").write_text("# Changed\n")
        with self.assertRaises(self.api.ReleaseError):
            self.api.build(self.source, self.base / "dist")
        for key, content in before.items():
            self.assertEqual(Path(built[key]).read_bytes(), content)

    def test_build_output_cannot_mutate_source_tree(self):
        with self.assertRaises(self.api.ReleaseError):
            self.api.build(self.source, self.source / "dist")
        self.assertFalse((self.source / "dist").exists())

    def test_verify_extracts_matching_bytes_into_new_directory(self):
        built = self.build()
        destination = self.base / "extracted"
        result = self.verify(built, extract_to=destination)
        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(self.api.snapshot_tree(destination), self.api.snapshot_tree(self.source))
        with self.assertRaises(self.api.ReleaseError):
            self.verify(built, extract_to=destination)

    def test_verify_fails_on_corruption_or_untrusted_checksum(self):
        built = self.build()
        archive = Path(built["archive"])
        archive.write_bytes(archive.read_bytes() + b"tampered")
        with self.assertRaises(self.api.ReleaseError):
            self.verify(built)

    def test_verify_rejects_manifest_paths_before_extracting(self):
        for bad in ["../escape", "/absolute", "C:/drive", "a\\b", "a//b", "./README.md"]:
            with self.subTest(path=bad):
                built = self.api.build(self.source, self.base / str(len(list(self.base.iterdir()))))
                self.refresh_receipts(built, lambda m: m["files"].update({bad: "0" * 64}))
                destination = self.base / "should-not-exist"
                with self.assertRaises(self.api.ReleaseError):
                    self.verify(built, extract_to=destination)
                self.assertFalse(destination.exists())
        self.assertFalse((self.base / "escape").exists())

    def test_verify_rejects_archive_paths_duplicates_and_symlinks(self):
        for options in [{"extra_name": "../escape"}, {"extra_name": "/absolute"},
                        {"extra_name": "collaborative-work/../escape"}, {"duplicate": True}, {"symlink": True}]:
            with self.subTest(options=options):
                built = self.api.build(self.source, self.base / str(len(list(self.base.iterdir()))))
                self.rewrite_zip(built, **options)
                with self.assertRaises(self.api.ReleaseError):
                    self.verify(built)

    def test_verify_checks_member_bytes_even_with_refreshed_archive_checksum(self):
        built = self.build()
        path = Path(built["archive"])
        with zipfile.ZipFile(path) as old:
            members = [(i, old.read(i)) for i in old.infolist()]
        with zipfile.ZipFile(path, "w") as new:
            for i, content in members:
                new.writestr(i, b"changed" if i.filename.endswith("README.md") else content)
        self.refresh_receipts(built)
        with self.assertRaises(self.api.ReleaseError):
            self.verify(built)

    def test_verify_rejects_nul_in_original_zip_filename(self):
        built = self.build()
        path = Path(built["archive"])
        with zipfile.ZipFile(path) as old:
            members = [(i, old.read(i)) for i in old.infolist()]
        with zipfile.ZipFile(path, "w") as new:
            for info, content in members:
                if info.filename.endswith("README.md"):
                    info.filename += "X"
                new.writestr(info, content)
        raw_name = b"collaborative-work/README.mdX"
        self.assertEqual(path.read_bytes().count(raw_name), 2)
        path.write_bytes(path.read_bytes().replace(raw_name, b"collaborative-work/README.md\x00"))
        self.refresh_receipts(built)
        with self.assertRaises(self.api.ReleaseError):
            self.verify(built)

    def test_manifest_checksum_is_checked_independently(self):
        built = self.build()
        p = Path(built["manifest"])
        p.write_bytes(p.read_bytes() + b"\n")
        with self.assertRaises(self.api.ReleaseError):
            self.verify(built)

    def test_cli_build_verify_and_failure_exit_codes(self):
        result = self.cli("build", self.source, "--output", self.base / "dist")
        self.assertEqual(result.returncode, 0, result.stderr)
        built = json.loads(result.stdout)
        checked = self.cli("verify", built["archive"], "--manifest", built["manifest"], "--checksum", built["checksum"])
        self.assertEqual(checked.returncode, 0, checked.stderr)
        (self.source / ".env").write_text("forbidden")
        invalid = self.cli("validate", self.source)
        self.assertNotEqual(invalid.returncode, 0)


if __name__ == "__main__":
    unittest.main()
