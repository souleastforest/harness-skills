"""Offline tests: temporary homes, fake SDK objects, and owned stdlib workers.

Run with Python -I -B -m unittest discover -s <this directory> -p 'test_invoker*.py'.
The caller sets TMPDIR/TMP/TEMP beneath the project's .dsh-invoker-profile so
fixtures, fake prompts and fake process scripts never touch a real DSH home.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dsh_invoker.py"
SPEC = importlib.util.spec_from_file_location("dsh_invoker_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
invoker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(invoker)
SECRET = "sk-fake-sensitive-value-never-print"


class InvokerTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path(__file__).resolve().parents[2] / ".dsh-invoker-profile" / "dev-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="invoker-test-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project with spaces 项目"
        self.project.mkdir()
        self.user = self.root / "fake user"
        self.user.mkdir()
        self.home = self.root / "harness home 配置"
        self.workspace = self.root / "disposable work"
        self.workspace.mkdir()
        self.environment = mock.patch.dict(os.environ, {"DSH_HOME": "", "DEEPSEEK_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path_home = mock.patch.object(invoker.Path, "home", return_value=self.user)
        self.path_home.start()
        self.addCleanup(self.path_home.stop)

    @property
    def state(self) -> Path:
        return self.project / invoker.STATE_DIRECTORY

    @property
    def binding_path(self) -> Path:
        return self.state / invoker.BINDING_FILENAME

    def args(self, action: str, *extra: str) -> argparse.Namespace:
        return invoker.build_parser().parse_args([action, "--project-root", str(self.project), *extra])

    def configure(self, *extra: str, home: Path | None = None) -> dict:
        return invoker.configure(self.project, self.args("configure", "--home", str(home or self.home), "--confirmed", *extra))

    def write_profile(self, name: str = "sdk", home: Path | None = None) -> Path:
        directory = (home or self.home) / "profiles" / name
        directory.mkdir(parents=True)
        for marker in invoker.PROFILE_MARKERS:
            (directory / marker).write_text(SECRET, encoding="utf-8")
        return directory

    def write_binding(self, value: object) -> None:
        self.state.mkdir(exist_ok=True)
        self.binding_path.write_text(json.dumps(value), encoding="utf-8")

    def request(self, **changes: object) -> dict:
        self.configure()
        request = {
            "project_root": str(self.project), "binding": invoker._load_binding(self.project), "cwd": str(self.workspace), "prompt": "Inspect the fake task.",
            "session_id": None, "patches": [], "provider": "deepseek-official", "model": "deepseek-v4-flash",
            "max_tokens": None, "request_timeout_seconds": 60.0,
        }
        request.update(changes)
        return request

    def run_main(self, action: str, *extra: str, stdin: str = "") -> tuple[int, dict, str]:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = invoker.main([action, "--project-root", str(self.project), *extra])
        text = output.getvalue()
        return code, json.loads(text), text

    def assert_error(self, code: str, callable_, *args, **kwargs) -> invoker.InvokerError:
        with self.assertRaises(invoker.InvokerError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(SECRET, raised.exception.message)
        return raised.exception

    def fake_sdk(self, *, reason: object = "completed", failure: str | None = None):
        capture: dict = {"constructed": 0, "entered": 0, "closed": 0, "runs": 0}

        class Result:
            session_id = "session-new"
            final_response = "fake final answer"
            finish_reason = reason

            @property
            def events(self):
                raise AssertionError("Raw events must not be inspected or printed")

            @property
            def notifications(self):
                raise AssertionError("Raw notifications must not be inspected or printed")

        class Harness:
            def __init__(self, **options):
                capture["constructed"] += 1
                capture["options"] = options
                if failure == "constructor":
                    raise RuntimeError(SECRET)

            def __enter__(self):
                capture["entered"] += 1
                if failure == "enter":
                    raise RuntimeError(SECRET)
                return self

            def close(self):
                capture["closed"] += 1

            def close(self):
                capture["closed"] += 1

            def __exit__(self, *exception):
                self.close()
                if failure == "close":
                    raise RuntimeError(SECRET)

            def run(self, prompt, **options):
                capture["runs"] += 1
                capture["prompt"] = prompt
                capture["run_options"] = options
                if failure == "run":
                    raise RuntimeError(SECRET)
                if failure == "timeout":
                    raise TimeoutError(SECRET)
                return Result()

        return capture, types.SimpleNamespace(DeepSeekHarness=Harness)

    def execute_fake(self, request: dict, **options):
        capture, sdk = self.fake_sdk(**options)
        with mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker.importlib, "import_module", return_value=sdk):
            result = invoker._execute_sdk(request)
        return capture, result

    def test_discover_default_and_zero_writes(self):
        before = set(self.project.iterdir())
        document = invoker.discover(self.project)
        self.assertEqual(document["selection"], {"home": str(self.user / ".dsh"), "profile": "sdk", "source": "user-home-default", "confirmed": False})
        self.assertEqual(document["binding"], {"state": "missing"})
        self.assertEqual(set(self.project.iterdir()), before)
        self.assertFalse(self.home.exists())

    def test_discover_nonblank_environment_first(self):
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}):
            document = invoker.discover(self.project)
        self.assertEqual(document["candidates"][0]["source"], "DSH_HOME")
        self.assertEqual(document["selection"]["home"], str(self.home))
        self.assertFalse(self.state.exists())

    def test_discover_whitespace_environment_ignored(self):
        with mock.patch.dict(os.environ, {"DSH_HOME": " \t "}):
            document = invoker.discover(self.project)
        self.assertEqual(document["selection"]["home"], str(self.user / ".dsh"))

    def test_discover_relative_environment_resolves_against_project(self):
        with mock.patch.dict(os.environ, {"DSH_HOME": "local home"}):
            document = invoker.discover(self.project)
        self.assertEqual(document["selection"]["home"], str(self.project / "local home"))

    def test_discover_lists_directories_and_markers_without_content(self):
        profile = self.write_profile()
        self.write_profile("desktop")
        (self.home / "profiles" / "node_modules").mkdir()
        (self.home / "profiles" / "ordinary-file").write_text(SECRET, encoding="utf-8")
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}), mock.patch.object(Path, "read_text", side_effect=AssertionError("Do not read profile content")), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Do not read profile content")):
            document = invoker.discover(self.project)
        names = [item["name"] for item in document["candidates"][0]["profiles"]]
        self.assertEqual(names, ["desktop", "sdk"])
        self.assertTrue(all(document["candidates"][0]["profiles"][1]["markers"].values()))
        self.assertNotIn(SECRET, json.dumps(document))
        self.assertEqual((profile / "package.json").read_text(encoding="utf-8"), SECRET)
        self.assertEqual(document["candidates"][0]["profiles"][0]["sdk_compatibility"], "not-sdk-profile")

    def test_discover_omits_redirected_profile_directories(self):
        self.write_profile()
        outside = self.root / "external profile"
        outside.mkdir()
        self.symlink(outside, self.home / "profiles" / "linked", True)
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}):
            document = invoker.discover(self.project)
        self.assertEqual([entry["name"] for entry in document["candidates"][0]["profiles"]], ["sdk"])
        self.assertFalse(self.state.exists())

    def test_saved_binding_authoritative_when_environment_changes(self):
        self.configure()
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.root / "different-home")}):
            document = invoker.discover(self.project)
        self.assertEqual(document["selection"]["home"], str(self.home))
        self.assertEqual(document["selection"]["source"], "saved-binding")
        self.assertTrue(document["selection"]["confirmed"])
        self.assertEqual(self.binding_path.read_text(encoding="utf-8"), json.dumps({"schema_version": 1, "home": str(self.home), "profile": "sdk"}, ensure_ascii=True, indent=2) + "\n")

    def test_configure_requires_confirmation_before_any_writes(self):
        self.assert_error("CONFIRMATION_REQUIRED", invoker.configure, self.project, self.args("configure", "--home", str(self.home)))
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertFalse(self.home.exists())

    def test_configure_missing_sdk_only_records_minimal_binding(self):
        document = self.configure()
        self.assertEqual(json.loads(self.binding_path.read_text(encoding="utf-8")), {"schema_version": 1, "home": str(self.home), "profile": "sdk"})
        self.assertEqual(set(path.name for path in self.state.iterdir()), {"binding.json"})
        self.assertTrue(document["profile"]["initialization_required"])
        self.assertFalse(self.home.exists())
        self.assertEqual((self.project / ".gitignore").read_bytes(), b"/.dsh-invoker-profile/\n")

    def test_configure_does_not_read_or_copy_profile_or_credentials(self):
        profile = self.write_profile()
        (self.home / ".env").write_text("DEEPSEEK_API_KEY=" + SECRET, encoding="utf-8")
        (self.home / ".credentials.yaml").write_text(SECRET, encoding="utf-8")
        self.configure()
        for file in self.state.rglob("*"):
            if file.is_file():
                self.assertNotIn(SECRET, file.read_text(encoding="utf-8"))
        self.assertEqual((profile / "package.json").read_text(encoding="utf-8"), SECRET)
        self.assertEqual((profile / "cordis.patch.yml").read_text(encoding="utf-8"), SECRET)

    def test_gitignore_preserves_bytes_rules_and_newline_style(self):
        path = self.project / ".gitignore"
        original = b"# existing\r\nbuild/\r\n\xffnonutf8"
        path.write_bytes(original)
        self.configure()
        self.assertEqual(path.read_bytes(), original + b"\r\n/.dsh-invoker-profile/\r\n")
        with mock.patch.object(invoker, "_atomic_write") as writer:
            document = self.configure()
        writer.assert_not_called()
        self.assertFalse(document["changed"])
        self.assertFalse(document["gitignore_updated"])

    def test_gitignore_later_direct_negation_reinforces_local_ignore(self):
        path = self.project / ".gitignore"
        original = b"/.dsh-invoker-profile/\n!/.dsh-invoker-profile/\n"
        path.write_bytes(original)
        self.configure()
        self.assertEqual(path.read_bytes(), original + b"/.dsh-invoker-profile/\n")

    def test_changed_binding_requires_explicit_reconfigure(self):
        self.configure()
        original = self.binding_path.read_bytes()
        other = self.root / "new home"
        self.assert_error("RECONFIGURE_REQUIRED", self.configure, home=other)
        self.assertEqual(self.binding_path.read_bytes(), original)
        self.configure("--reconfigure", home=other)
        self.assertEqual(json.loads(self.binding_path.read_text(encoding="utf-8"))["home"], str(other))

    def test_malformed_binding_not_overwritten_without_reconfigure(self):
        self.state.mkdir()
        original = ("{bad " + SECRET).encode()
        self.binding_path.write_bytes(original)
        self.assert_error("INVALID_BINDING", self.configure)
        self.assertEqual(self.binding_path.read_bytes(), original)
        self.assertFalse((self.project / ".gitignore").exists())
        self.configure("--reconfigure")
        self.assertNotIn(SECRET, self.binding_path.read_text(encoding="utf-8"))

    def test_unknown_secret_fields_rejected_and_never_printed(self):
        self.write_binding({"schema_version": 1, "home": str(self.home), "profile": "sdk", "api_key": SECRET})
        for action in ("discover", "status"):
            code, document, text = self.run_main(action)
            self.assertEqual(code, 2)
            self.assertEqual(document["error"]["code"], "INVALID_BINDING")
            self.assertNotIn(SECRET, text)
        self.assert_error("INVALID_BINDING", self.configure)
        self.assertIn(SECRET, self.binding_path.read_text(encoding="utf-8"))

    def test_invalid_binding_shapes(self):
        for value in ([], None, {}, {"schema_version": True, "home": str(self.home), "profile": "sdk"}, {"schema_version": 2, "home": str(self.home), "profile": "sdk"}, {"schema_version": 1, "home": "relative", "profile": "sdk"}, {"schema_version": 1, "home": str(self.home), "profile": "../sdk"}):
            with self.subTest(value=value):
                self.write_binding(value)
                self.assert_error("INVALID_BINDING", invoker.discover, self.project)

    def test_duplicate_binding_keys_rejected(self):
        self.state.mkdir()
        self.binding_path.write_text('{"schema_version":1,"home":"' + str(self.home) + '","profile":"sdk","profile":"web"}', encoding="utf-8")
        self.assert_error("INVALID_BINDING", invoker.discover, self.project)

    def test_invalid_profile_names(self):
        for name in ("", ".", "..", "../sdk", "/sdk", "x/y", "x\\y", "node_modules", "NODE_MODULES", " sdk", "sdk ", "sdk\n", "NUL", "aux.txt", "COM1", "sdk.", "C:profile"):
            with self.subTest(name=name):
                self.assert_error("INVALID_PROFILE", invoker.validate_profile_name, name)
        for name in ("sdk", "sdk-minimal", "custom-配置", "team profile"):
            self.assertEqual(invoker.validate_profile_name(name), name)

    def test_non_sdk_surface_not_accepted_as_sdk(self):
        for profile in ("desktop", "web", "headless", "acp", "tui"):
            with self.subTest(profile=profile):
                self.assert_error("NON_SDK_PROFILE", self.configure, "--profile", profile)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_missing_custom_profile_fails_without_writes(self):
        self.assert_error("MISSING_CUSTOM_PROFILE", self.configure, "--profile", "custom")
        self.assertFalse(self.state.exists())
        self.assertFalse((self.project / ".gitignore").exists())
        self.assertFalse(self.home.exists())

    def test_custom_profile_markers_only_no_compatibility_guarantee(self):
        self.write_profile("custom")
        document = self.configure("--profile", "custom")
        self.assertEqual(document["binding"]["profile"], "custom")
        self.assertEqual(document["profile"]["sdk_compatibility"], "unverified-until-sdk-initializes")

    def test_custom_profile_missing_marker_fails(self):
        path = self.write_profile("custom")
        (path / "cordis.patch.yml").unlink()
        self.assert_error("MISSING_CUSTOM_PROFILE", self.configure, "--profile", "custom")
        self.assertFalse(self.state.exists())

    def test_explicit_profile_directory_split_only(self):
        path = self.write_profile()
        document = invoker.configure(self.project, self.args("configure", "--profile-dir", str(path), "--confirmed"))
        self.assertEqual(document["binding"]["home"], str(self.home))
        self.assertEqual(document["binding"]["profile"], "sdk")
        self.assert_error("HOME_IS_PROFILE_DIRECTORY", self.configure, home=path)

    def test_profile_directory_rejects_arbitrary_layout_and_conflicts(self):
        paths = (self.home / "sdk", Path("relative/profiles/sdk"), self.home / "profiles" / ".." / "sdk")
        for path in paths:
            with self.subTest(path=path):
                self.assert_error("INVALID_PROFILE_DIRECTORY", invoker.configure, self.project, self.args("configure", "--profile-dir", str(path), "--confirmed"))
        self.assert_error("PROFILE_DIRECTORY_CONFLICT", invoker.configure, self.project, self.args("configure", "--profile-dir", str(self.home / "profiles" / "sdk"), "--profile", "other", "--confirmed"))
        self.assertFalse(self.state.exists())

    def test_relative_and_missing_project_working_paths_fail(self):
        self.assert_error("INVALID_PATH", invoker._absolute_path, "relative")
        self.assert_error("INVALID_PATH", invoker._absolute_path, str(self.root / "missing"), directory=True)
        self.assert_error("INVALID_PATH", self.configure, home=Path("relative"))
        self.assertFalse(self.state.exists())

    def test_absolute_paths_are_canonicalized(self):
        value = str(self.root / "parent" / ".." / "home")
        document = invoker.configure(self.project, self.args("configure", "--home", value, "--confirmed"))
        self.assertEqual(document["binding"]["home"], str(self.root / "home"))

    def symlink(self, target: Path, link: Path, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest("Host does not permit temporary symlinks")

    def test_state_directory_symlink_cannot_escape(self):
        external = self.root / "outside"
        external.mkdir()
        self.symlink(external, self.state, True)
        self.assert_error("UNSAFE_STATE_PATH", self.configure)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.project / ".gitignore").exists())

    def test_gitignore_symlink_cannot_escape(self):
        external = self.root / "outside.ignore"
        external.write_text(SECRET, encoding="utf-8")
        self.symlink(external, self.project / ".gitignore")
        self.assert_error("UNSAFE_STATE_PATH", self.configure)
        self.assertFalse(self.state.exists())
        self.assertEqual(external.read_text(encoding="utf-8"), SECRET)

    def test_binding_symlink_never_overwritten_even_reconfigure(self):
        external = self.root / "outside.binding"
        external.write_text(SECRET, encoding="utf-8")
        self.state.mkdir()
        self.symlink(external, self.binding_path)
        self.assert_error("UNSAFE_STATE_PATH", self.configure, "--reconfigure")
        self.assertEqual(external.read_text(encoding="utf-8"), SECRET)
        self.assertFalse((self.project / ".gitignore").exists())

    def test_profiles_and_profile_symlinks_rejected(self):
        external = self.root / "external-profiles"
        external.mkdir()
        self.home.mkdir()
        self.symlink(external, self.home / "profiles", True)
        self.assert_error("UNSAFE_STATE_PATH", self.configure)
        (self.home / "profiles").unlink()
        (self.home / "profiles").mkdir()
        self.symlink(external, self.home / "profiles" / "sdk", True)
        self.assert_error("UNSAFE_STATE_PATH", self.configure)
        self.assertFalse(self.state.exists())

    def test_profile_marker_symlink_rejected(self):
        profile = self.write_profile()
        external = self.root / "external-marker"
        external.write_text(SECRET, encoding="utf-8")
        (profile / "package.json").unlink()
        self.symlink(external, profile / "package.json")
        self.assert_error("UNSAFE_STATE_PATH", self.configure)
        self.assertEqual(external.read_text(encoding="utf-8"), SECRET)

    def test_junction_reparse_detection_without_windows(self):
        info = types.SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
        self.assertTrue(invoker._is_link(info))
        self.assertFalse(invoker._is_link(types.SimpleNamespace(st_mode=0o40755, st_file_attributes=0)))

    def test_atomic_write_failure_cleans_temporary_and_preserves_target(self):
        self.configure()
        original = self.binding_path.read_bytes()
        with mock.patch.object(invoker.os, "replace", side_effect=OSError(SECRET)):
            with self.assertRaises(OSError):
                invoker._atomic_write(self.binding_path, b"replacement")
        self.assertEqual(self.binding_path.read_bytes(), original)
        self.assertEqual(set(path.name for path in self.state.iterdir()), {"binding.json"})

    def test_status_reports_presence_without_reading_credentials(self):
        self.configure()
        self.home.mkdir()
        (self.home / ".env").write_text(SECRET, encoding="utf-8")
        (self.home / ".credentials.yaml").write_text(SECRET, encoding="utf-8")
        (self.project / ".env").write_text(SECRET, encoding="utf-8")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}), mock.patch.object(invoker, "_sdk_metadata", return_value={"ready": False}):
            document = invoker.status(self.project)
        self.assertTrue(document["credentials"]["environment_key_present"])
        self.assertTrue(document["credentials"]["home_credentials_file_exists"])
        self.assertTrue(document["credentials"]["home_dotenv_exists"])
        self.assertTrue(document["credentials"]["project_dotenv_exists"])
        self.assertFalse(document["credentials"]["verified"])
        self.assertNotIn(SECRET, json.dumps(document))

    def test_status_env_absence_does_not_claim_no_credentials(self):
        self.configure()
        document = invoker.status(self.project)
        self.assertFalse(document["credentials"]["environment_key_present"])
        self.assertFalse(document["credentials"]["verified"])
        self.assertIn("does not rule out", document["credentials"]["note"])

    def test_status_missing_bound_custom_profile_is_problem_not_fallback(self):
        path = self.write_profile("custom")
        self.configure("--profile", "custom")
        for marker in invoker.PROFILE_MARKERS:
            (path / marker).unlink()
        path.rmdir()
        document = invoker.status(self.project)
        self.assertEqual(document["profile_problem"], "MISSING_CUSTOM_PROFILE")
        self.assertEqual(document["selection"]["profile"], "custom")

    def test_discover_configure_status_never_import_sdk(self):
        with mock.patch.object(invoker.importlib, "import_module", side_effect=AssertionError("SDK must remain lazy")):
            invoker.discover(self.project)
            self.configure()
            invoker.status(self.project)

    def test_sdk_version_checks_both_published_distributions(self):
        with mock.patch.object(invoker.importlib.metadata, "version", return_value="0.1.5rc1"):
            self.assertTrue(invoker._sdk_metadata()["ready"])
            invoker._require_sdk()
        with mock.patch.object(invoker.importlib.metadata, "version", side_effect=["0.1.5rc1", "0.1.4"]):
            self.assert_error("SDK_NOT_READY", invoker._require_sdk)
        with mock.patch.object(invoker.importlib.metadata, "version", side_effect=invoker.importlib.metadata.PackageNotFoundError):
            metadata = invoker._sdk_metadata()
        self.assertFalse(metadata["ready"])
        self.assertTrue(all(value is None for value in metadata["versions"].values()))

    def test_execute_passes_exact_absolute_sdk_options_and_new_session(self):
        request = self.request()
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.root / "unconfirmed")}), mock.patch.object(invoker.Path, "cwd", side_effect=AssertionError("Must not infer task cwd")):
            capture, result = self.execute_fake(request)
        options = capture["options"]
        self.assertEqual(options["dsh_home"], str(self.home))
        self.assertEqual(options["profile"], "sdk")
        self.assertEqual(options["cwd"], str(self.workspace))
        self.assertIsInstance(options["patches"], tuple)
        self.assertEqual(options["patches"], (str(invoker.PRIVACY_PATCH),))
        self.assertEqual(options["env"], {"DSH_RUNTIME_MODE": "exe"})
        self.assertNotIn("dsh_bin", options)
        self.assertNotIn("api_key", options)
        self.assertEqual(capture["run_options"], {})
        self.assertEqual(capture["closed"], 1)
        self.assertEqual(set(result), {"session_id", "finish_reason", "final_response"})
        self.assertEqual(result["finish_reason"], "completed")

    def test_explicit_continuity_rejected_and_model_options_invocation_only(self):
        request = self.request(session_id="intentional-session", provider="custom-provider", model="custom-model", max_tokens=512)
        with mock.patch.object(invoker.importlib, "import_module") as importer:
            self.assert_error("UNSUPPORTED_CONTINUATION", invoker._execute_sdk, request)
        importer.assert_not_called()
        # Provider/model options still apply to an ordinary independent task.
        request["session_id"] = None
        capture, result = self.execute_fake(request)
        self.assertEqual(capture["run_options"], {})
        self.assertEqual(capture["options"]["provider"], "custom-provider")
        self.assertEqual(capture["options"]["model"], "custom-model")
        self.assertEqual(capture["options"]["max_tokens"], 512)
        self.assertEqual(set(json.loads(self.binding_path.read_text(encoding="utf-8"))), {"schema_version", "home", "profile"})
        self.assertNotIn("intentional-session", self.binding_path.read_text(encoding="utf-8"))
        self.assertNotIn("custom-model", self.binding_path.read_text(encoding="utf-8"))

    def test_sdk_patches_paths_tuple_and_privacy_last(self):
        patch = self.root / "user override.patch.yml"
        patch.write_text("- id: session-log-deepseek\n  disabled: false\n", encoding="utf-8")
        request = self.request(patches=[str(invoker.PRIVACY_PATCH), str(patch)])
        capture, result = self.execute_fake(request)
        self.assertEqual(capture["options"]["patches"], (str(patch), str(invoker.PRIVACY_PATCH)))
        self.assertEqual(invoker.PRIVACY_PATCH.read_bytes().replace(b"\r\n", b"\n"), invoker.PRIVACY_PATCH_BYTES)

    def test_privacy_patch_tamper_fails_before_sdk_import(self):
        request = self.request()
        original = Path.read_bytes
        def read(path):
            return b"- id: other\n" if path == invoker.PRIVACY_PATCH else original(path)
        with mock.patch.object(Path, "read_bytes", read), mock.patch.object(invoker.importlib, "import_module") as importer:
            self.assert_error("INVALID_PRIVACY_PATCH", invoker._execute_sdk, request)
        importer.assert_not_called()

    def test_sdk_result_error_and_max_tokens_remain_plain(self):
        request = self.request()
        for reason in ("error", "max-tokens"):
            with self.subTest(reason=reason):
                capture, result = self.execute_fake(request, reason=reason)
                self.assertEqual(result["finish_reason"], reason)
                self.assertEqual(set(result), {"session_id", "finish_reason", "final_response"})
                self.assertEqual(capture["closed"], 1)

    def test_sdk_result_unknown_and_missing_reason_are_failures(self):
        request = self.request()
        for reason in (None, "unknown", "max_tokens", SECRET):
            with self.subTest(reason=reason):
                capture, sdk = self.fake_sdk(reason=reason)
                with mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker.importlib, "import_module", return_value=sdk):
                    self.assert_error("INVALID_SDK_RESULT", invoker._execute_sdk, request)
                self.assertEqual(capture["closed"], 1)

    def test_sensitive_sdk_exceptions_sanitized_no_retry(self):
        request = self.request()
        for stage in ("constructor", "enter", "run", "close", "timeout"):
            with self.subTest(stage=stage):
                capture, sdk = self.fake_sdk(failure=stage)
                with mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker.importlib, "import_module", return_value=sdk):
                    self.assert_error("SDK_REQUEST_TIMEOUT" if stage == "timeout" else "SDK_FAILURE", invoker._execute_sdk, request)
                self.assertEqual(capture["constructed"], 1)
                self.assertLessEqual(capture["runs"], 1)
                if stage in ("enter", "run", "close", "timeout"):
                    self.assertEqual(capture["closed"], 1)

    def test_sdk_import_error_never_echoes_secret(self):
        request = self.request()
        with mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker.importlib, "import_module", side_effect=ImportError(SECRET)):
            error = self.assert_error("SDK_IMPORT_FAILED", invoker._execute_sdk, request)
        self.assertEqual(error.exit_code, 3)

    def test_invoke_requires_binding_and_does_not_install(self):
        args = self.args("invoke", "--cwd", str(self.workspace), "--stdin")
        with mock.patch.object(sys, "stdin", io.StringIO("fake prompt")), mock.patch.object(invoker, "_require_sdk") as dependency, mock.patch.object(invoker, "_supervise") as supervisor:
            self.assert_error("BINDING_REQUIRED", invoker.invoke, self.project, args)
        dependency.assert_not_called()
        supervisor.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_invoke_sdk_unavailable_fails_without_process_or_temp(self):
        self.configure()
        args = self.args("invoke", "--cwd", str(self.workspace), "--stdin")
        with mock.patch.object(sys, "stdin", io.StringIO("fake prompt")), mock.patch.object(invoker.importlib.metadata, "version", side_effect=invoker.importlib.metadata.PackageNotFoundError), mock.patch.object(invoker, "_supervise") as supervisor:
            error = self.assert_error("SDK_NOT_READY", invoker.invoke, self.project, args)
        self.assertEqual(error.exit_code, 3)
        supervisor.assert_not_called()
        self.assertFalse((self.state / "tmp").exists())

    def test_prompt_file_relative_to_explicit_cwd_not_project(self):
        file = self.workspace / "prompt text 提问.txt"
        prompt = "Read this UTF-8 prompt: 测试"
        file.write_text(prompt, encoding="utf-8")
        args = self.args("invoke", "--cwd", str(self.workspace), "--prompt-file", file.name)
        self.assertEqual(invoker._read_prompt(args, self.workspace), prompt)

    def test_stdin_prompt_non_tty(self):
        args = self.args("invoke", "--cwd", str(self.workspace), "--stdin")
        with mock.patch.object(sys, "stdin", io.StringIO("plain prompt\n")):
            self.assertEqual(invoker._read_prompt(args, self.workspace), "plain prompt\n")

    def test_stdin_tty_rejected_without_reading(self):
        args = self.args("invoke", "--cwd", str(self.workspace), "--stdin")
        source = mock.Mock()
        source.isatty.return_value = True
        with mock.patch.object(sys, "stdin", source):
            self.assert_error("INVALID_PROMPT", invoker._read_prompt, args, self.workspace)
        source.read.assert_not_called()

    def test_invalid_empty_oversized_and_non_utf8_prompts(self):
        args = self.args("invoke", "--cwd", str(self.workspace), "--stdin")
        for prompt in ("", " \n", "with\x00nul", "x" * (invoker.MAX_PROMPT_BYTES + 1)):
            with self.subTest(length=len(prompt)), mock.patch.object(sys, "stdin", io.StringIO(prompt)):
                self.assert_error("INVALID_PROMPT", invoker._read_prompt, args, self.workspace)
        file = self.workspace / "bad.txt"
        file.write_bytes(b"\xff" + SECRET.encode())
        self.assert_error("INVALID_PROMPT", invoker._read_prompt, self.args("invoke", "--cwd", str(self.workspace), "--prompt-file", str(file)), self.workspace)

    def test_invoke_owned_worker_argv_no_prompt_and_local_temp(self):
        self.configure()
        args = self.args("invoke", "--cwd", str(self.workspace), "--stdin", "--timeout-seconds", "12", "--request-timeout-seconds", "3")
        result = {"session_id": "new", "finish_reason": "completed", "final_response": "answer"}
        with mock.patch.object(sys, "stdin", io.StringIO(SECRET)), mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker, "_supervise", return_value=(result, 0)) as supervisor:
            document, code = invoker.invoke(self.project, args)
        command, request = supervisor.call_args.args
        self.assertEqual(command[:3], [sys.executable, "-I", "-B"])
        self.assertEqual(command[-1], "_worker")
        self.assertNotIn(SECRET, " ".join(command))
        self.assertEqual(request["prompt"], SECRET)
        self.assertEqual(request["request_timeout_seconds"], 3.0)
        self.assertGreater(supervisor.call_args.kwargs["timeout"], 0.0)
        self.assertLessEqual(supervisor.call_args.kwargs["timeout"], 12.0)  # prompt/setup time is already charged
        self.assertEqual(supervisor.call_args.kwargs["cwd"], self.state)
        for variable in ("TMP", "TMPDIR", "TEMP"):
            self.assertEqual(supervisor.call_args.kwargs["environment"][variable], str(self.state / "tmp"))
        self.assertNotIn(SECRET, self.binding_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(document, result)

    def test_invoke_temporary_symlink_rejected_before_worker(self):
        self.configure()
        outside = self.root / "outside-temp"
        outside.mkdir()
        self.symlink(outside, self.state / "tmp", True)
        args = self.args("invoke", "--cwd", str(self.workspace), "--stdin")
        with mock.patch.object(sys, "stdin", io.StringIO("prompt")), mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker, "_supervise") as supervisor:
            self.assert_error("UNSAFE_STATE_PATH", invoker.invoke, self.project, args)
        supervisor.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_session_and_patch_arguments_fail_without_sdk(self):
        request = self.request()
        for session in ("", "../session", "x/y", "x\\y", "has space", "\n" + SECRET):
            with self.subTest(session=session):
                self.assert_error("INVALID_SESSION_ID", invoker._validate_request, {**request, "session_id": session})
        self.assert_error("INVALID_PATH", invoker._validate_request, {**request, "patches": ["relative.patch.yml"]})
        self.assert_error("INVALID_PATCH", invoker._validate_request, {**request, "patches": [str(self.root / "absent.patch.yml")]})

    def test_invalid_numeric_flags_sanitized(self):
        for flag, value in (("--timeout-seconds", "nan"), ("--timeout-seconds", "inf"), ("--timeout-seconds", "0"), ("--request-timeout-seconds", "-1"), ("--max-tokens", "0"), ("--max-tokens", SECRET)):
            with self.subTest(flag=flag, value=value):
                code, document, text = self.run_main("invoke", "--cwd", str(self.workspace), "--stdin", flag, value)
                self.assertEqual(code, 2)
                self.assertEqual(document["error"]["code"], "INVALID_ARGUMENTS")
                self.assertNotIn(SECRET, text)

    def test_unknown_argument_never_echoes_sensitive_value(self):
        code, document, text = self.run_main("discover", "--api-key", SECRET)
        self.assertEqual(code, 2)
        self.assertEqual(document["error"]["code"], "INVALID_ARGUMENTS")
        self.assertNotIn(SECRET, text)

    def test_main_sdk_result_exit_codes(self):
        for reason, expected in (("completed", 0), ("error", 4), ("max-tokens", 4)):
            result = {"session_id": "new", "finish_reason": reason, "final_response": "answer"}
            with self.subTest(reason=reason), mock.patch.object(invoker, "invoke", return_value=(result, expected)):
                code, document, text = self.run_main("invoke", "--cwd", str(self.workspace), "--stdin")
            self.assertEqual(code, expected)
            self.assertEqual(document, result)
            self.assertNotIn("events", text)

    def test_main_io_and_interrupt_errors_sanitized(self):
        with mock.patch.object(invoker, "discover", side_effect=OSError(SECRET)):
            code, document, text = self.run_main("discover")
        self.assertEqual(code, 5)
        self.assertEqual(document["error"]["code"], "IO_OR_STATE_FAILURE")
        self.assertNotIn(SECRET, text)
        with mock.patch.object(invoker, "discover", side_effect=KeyboardInterrupt):
            code, document, text = self.run_main("discover")
        self.assertEqual(code, 130)
        self.assertEqual(document["error"]["code"], "INTERRUPTED")

    def test_public_cli_isolated_read_only(self):
        environment = os.environ.copy()
        environment["DSH_HOME"] = str(self.home)
        # A task's json.py must not be imported by the helper, even when its
        # process is launched there. The public bootstrap also uses a safe cwd.
        (self.workspace / "json.py").write_text('raise RuntimeError("' + SECRET + '")', encoding="utf-8")
        process = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "discover", "--project-root", str(self.project)], cwd=self.workspace, env=environment, capture_output=True, timeout=10, check=False)
        self.assertEqual(process.returncode, 0, process.stdout.decode())
        self.assertEqual(json.loads(process.stdout)["selection"]["home"], str(self.home))
        self.assertEqual(process.stderr, b"")
        self.assertFalse(self.state.exists())
        self.assertFalse((self.workspace / "__pycache__").exists())

    @unittest.skipIf(os.name == "nt", "POSIX process-group preflight only")
    def test_denied_process_ownership_fails_before_sdk_worker(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.stdin = mock.Mock()
        with mock.patch.object(invoker.subprocess, "Popen", return_value=process) as constructor, mock.patch.object(invoker.os, "killpg", side_effect=PermissionError(SECRET)):
            error = self.assert_error("PROCESS_OWNERSHIP_FAILED", invoker._supervise, ["never-run-sdk-worker"], {}, cwd=self.project, environment=os.environ.copy(), timeout=1)
        self.assertEqual(error.exit_code, 5)
        self.assertEqual(constructor.call_count, 1)
        self.assertIn("sys.stdin.buffer.read", constructor.call_args.args[0][-1])
        process.communicate.assert_not_called()
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        process.stdin.close.assert_called_once()

    @unittest.skipIf(os.name == "nt", "POSIX deterministic ownership test only")
    def test_mocked_worker_deadline_owned_tree_cleanup(self):
        process = mock.Mock()
        process.pid = 987654
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        tree = mock.Mock()
        with mock.patch.object(invoker, "_probe_process_group"), mock.patch.object(invoker.subprocess, "Popen", return_value=process), mock.patch.object(invoker, "_PosixTree", return_value=tree), mock.patch.object(invoker, "_collect_worker", side_effect=invoker._deadline_error()):
            error = self.assert_error("INVOCATION_DEADLINE", invoker._supervise, ["fake-owned-worker"], {}, cwd=self.project, environment=os.environ.copy(), timeout=0.01)
        self.assertEqual(error.exit_code, 124)
        tree.cleanup.assert_called_once()
        process.wait.assert_called_once()
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    @unittest.skipIf(os.name == "nt", "POSIX deterministic ownership test only")
    def test_cleanup_denial_reports_possible_descendant_not_success(self):
        process = mock.Mock()
        process.pid = 987654
        process.poll.return_value = 0
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        tree = mock.Mock()
        tree.cleanup.side_effect = PermissionError(SECRET)
        error = self.assert_error("PROCESS_CLEANUP_FAILED", invoker._stop_owned_worker, process, None, released=True, tree=tree)
        self.assertEqual(error.exit_code, 5)
        self.assertIn("descendant might remain", error.message)
        tree.cleanup.assert_called_once()
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    def worker_command(self, *, reason: str = "completed", failure: bool = False) -> list[str]:
        script = self.root / ("fake-worker-" + reason + ("-fail" if failure else "") + ".py")
        script.write_text(
            "import importlib.util, os, sys, types\n"
            f"spec=importlib.util.spec_from_file_location('invoker_worker_test', {str(SCRIPT)!r})\n"
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            "module._require_sdk=lambda: None\n"
            "class Fake:\n"
            " def __init__(self, **options):\n"
            f"  os.write(1, {SECRET.encode()!r}); os.write(2, {SECRET.encode()!r})\n"
            " def __enter__(self): return self\n"
            " def __exit__(self, *exc): pass\n"
            " def run(self, prompt, **options):\n"
            + (f"  raise RuntimeError({SECRET!r})\n" if failure else f"  return types.SimpleNamespace(session_id='fake-session',finish_reason={reason!r},final_response='fake result',events=[{SECRET!r}],notifications=[{SECRET!r}])\n")
            + "module.importlib.import_module=lambda name: types.SimpleNamespace(DeepSeekHarness=Fake)\n"
            "raise SystemExit(module._worker_main())\n",
            encoding="utf-8",
        )
        return [sys.executable, "-I", "-B", str(script)]

    def require_native_ownership(self) -> None:
        if os.environ.get("DSH_INVOKER_SKIP_NATIVE_CLEANUP") == "1":
            if os.environ.get("CI") or os.environ.get("DSH_INVOKER_REQUIRE_NATIVE_CLEANUP") == "1":
                self.fail("Native cleanup cannot be skipped in required CI")
            self.skipTest("Explicit local skip: runner denied owned process-group signals; native CI remains required")

    def test_real_owned_worker_fake_sdk_hides_native_stdout_stderr(self):
        request = self.request()
        self.require_native_ownership()
        for reason, expected in (("completed", 0), ("error", 4), ("max-tokens", 4)):
            with self.subTest(reason=reason):
                result, code = invoker._supervise(self.worker_command(reason=reason), request, cwd=self.state, environment=os.environ.copy(), timeout=10)
                self.assertEqual(code, expected)
                self.assertEqual(result, {"session_id": "fake-session", "finish_reason": reason, "final_response": "fake result"})
                self.assertNotIn(SECRET, json.dumps(result))

    def test_real_owned_worker_sensitive_sdk_failure_sanitized(self):
        request = self.request()
        self.require_native_ownership()
        error = self.assert_error("SDK_FAILURE", invoker._supervise, self.worker_command(failure=True), request, cwd=self.state, environment=os.environ.copy(), timeout=10)
        self.assertEqual(error.exit_code, 5)

    def test_worker_invalid_json_does_not_echo_it(self):
        self.configure()
        self.require_native_ownership()
        script = self.root / "bad-worker.py"
        script.write_text(f"import sys\nsys.stdin.buffer.read()\nprint({SECRET!r})\n", encoding="utf-8")
        self.assert_error("WORKER_FAILURE", invoker._supervise, [sys.executable, "-I", "-B", str(script)], {}, cwd=self.state, environment=os.environ.copy(), timeout=10)

    def test_worker_error_message_not_trusted(self):
        self.configure()
        self.require_native_ownership()
        script = self.root / "untrusted-error-worker.py"
        document = {"ok": False, "action": "invoke", "error": {"code": "SDK_FAILURE", "message": SECRET}}
        script.write_text("import sys\nsys.stdin.buffer.read()\n" + f"print({json.dumps(document)!r})\nraise SystemExit(5)\n", encoding="utf-8")
        self.assert_error("SDK_FAILURE", invoker._supervise, [sys.executable, "-I", "-B", str(script)], {}, cwd=self.state, environment=os.environ.copy(), timeout=10)

    def sleeper_command(self, *, complete: bool) -> tuple[list[str], Path]:
        marker = self.root / ("escaped-normal-child" if complete else "escaped-timeout-child")
        child = self.root / "owned-child.py"
        child.write_text("import pathlib, sys, time\ntime.sleep(1.4)\npathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')\n", encoding="utf-8")
        worker = self.root / ("normal-parent.py" if complete else "hanging-parent.py")
        worker.write_text(
            "import json, subprocess, sys, time\n"
            "sys.stdin.buffer.read()\n"  # Windows job owns the worker before this gate opens.
            f"subprocess.Popen([sys.executable,'-I','-B',{str(child)!r},{str(marker)!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            + ("print(json.dumps({'session_id':'fake','finish_reason':'completed','final_response':'ok'}),flush=True)\n" if complete else "time.sleep(30)\n"),
            encoding="utf-8",
        )
        return [sys.executable, "-I", "-B", str(worker)], marker

    def test_owned_worker_deadline_cleans_child_without_sdk(self):
        self.configure()
        self.require_native_ownership()
        command, marker = self.sleeper_command(complete=False)
        started = time.monotonic()
        error = self.assert_error("INVOCATION_DEADLINE", invoker._supervise, command, {}, cwd=self.state, environment=os.environ.copy(), timeout=0.35)
        self.assertEqual(error.exit_code, 124)
        self.assertLess(time.monotonic() - started, 4)
        time.sleep(1.5)
        self.assertFalse(marker.exists(), "A worker child survived its owned deadline")

    def test_owned_worker_normal_exit_cleans_remaining_children(self):
        self.configure()
        self.require_native_ownership()
        command, marker = self.sleeper_command(complete=True)
        result, code = invoker._supervise(command, {}, cwd=self.state, environment=os.environ.copy(), timeout=4)
        self.assertEqual(code, 0)
        time.sleep(1.5)
        self.assertFalse(marker.exists(), "A child survived a completed owned worker")

    def test_owned_cleanup_does_not_kill_unrelated_process(self):
        self.configure()
        self.require_native_ownership()
        marker = self.root / "unrelated-survived"
        script = self.root / "unrelated.py"
        script.write_text("import pathlib,sys,time\ntime.sleep(.7)\npathlib.Path(sys.argv[1]).write_text('safe', encoding='utf-8')\n", encoding="utf-8")
        unrelated = subprocess.Popen([sys.executable, "-I", "-B", str(script), str(marker)], cwd=self.state)
        try:
            command, escaped = self.sleeper_command(complete=False)
            self.assert_error("INVOCATION_DEADLINE", invoker._supervise, command, {}, cwd=self.state, environment=os.environ.copy(), timeout=0.25)
            unrelated.wait(timeout=4)
            self.assertEqual(marker.read_text(encoding="utf-8"), "safe")
        finally:
            if unrelated.poll() is None:
                unrelated.kill()
                unrelated.wait()


if __name__ == "__main__":
    unittest.main()
