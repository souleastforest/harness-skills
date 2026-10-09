"""Offline bootstrap tests; no downloads, installs, credentials or DSH requests.

Run with Python -I -B -m unittest discover -s <skill>/tests -p 'test_bootstrap*.py'.
All fixtures live under the repository's .dsh-invoker-profile/dev-tests (or the
explicit DSH_INVOKER_TEST_TMP/DSH_INVOKER_TEST_ROOT directory) and are removed.
The fake uv/interpreter verify argument plans, not real installation correctness.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parent
SCRIPT = SKILL / "scripts/bootstrap.sh"
BASH = shutil.which("bash")
SPEC = importlib.util.spec_from_file_location("dsh_bootstrap_support", SKILL / "scripts/bootstrap_support.py")
SUPPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPPORT)

FAKE_UV = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["UV_PYTHON_INSTALL_DIR"]).parent
with (state / "uv-calls.jsonl").open("a", encoding="utf-8") as stream:
    keys = ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "TMPDIR", "HOME", "NETRC", "HTTP_PROXY", "UV_INDEX_URL", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PIP_EXTRA_INDEX_URL")
    json.dump({"args": args, "cwd": os.getcwd(), "env": {k: os.environ[k] for k in keys if k in os.environ}}, stream)
    stream.write("\n")
if "--version" in args:
    print("uv " + os.environ.get("FAKE_UV_VERSION", "0.9.20") + " (offline test)")
    raise SystemExit(0)
if "--help" in args:
    flags = "--install-dir --no-bin --no-registry --no-project --managed-python --no-python-downloads --python --only-binary --link-mode --prerelease --default-index --keyring-provider --no-sources"
    print(flags.replace(os.environ.get("FAKE_MISSING_FLAG", "<none>"), ""))
    raise SystemExit(0)
if "python" in args and "install" in args:
    if os.environ.get("FAKE_UV_FAIL"):
        print("https://secret-proxy:password@example.invalid token=PRIVATE", file=sys.stderr)
        raise SystemExit(7)
    managed = state / "python/offline-python/bin"
    managed.mkdir(parents=True)
    wrapper = "#!/bin/bash\nexec " + os.environ["FAKE_REAL_PYTHON_QUOTED"] + " -I -B " + os.environ["FAKE_RUNTIME_QUOTED"] + " \"$@\"\n"
    (managed / "python").write_text(wrapper, encoding="utf-8")
    (managed / "python").chmod(0o700)
elif "venv" in args:
    venv = pathlib.Path(args[-1]); (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(state / "python/offline-python/bin/python")
    if os.environ.get("FAKE_ESCAPE_PHASE") == "venv":
        site = venv / "lib/python3.12/site-packages"
        site.parent.mkdir(parents=True)
        site.symlink_to(os.environ["FAKE_EXTERNAL_SITE"], target_is_directory=True)
elif "pip" in args:
    if os.environ.get("FAKE_ESCAPE_PHASE") == "pip":
        site = state / "venv/lib/python3.12/site-packages"
        site.parent.mkdir(parents=True, exist_ok=True)
        site.symlink_to(os.environ["FAKE_EXTERNAL_SITE"], target_is_directory=True)
else:
    raise SystemExit(9)
'''
FAKE_RUNTIME = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["UV_PYTHON_INSTALL_DIR"]).parent
# Model automatic site startup using only a test-authored .pth fixture. If the
# native wrapper preflight is missing, the marker is written before support runs.
site_dir = state / "venv/lib/python3.12/site-packages"
if site_dir.is_dir():
    import site
    site.addsitedir(str(site_dir))
with (state / "python-calls.jsonl").open("a", encoding="utf-8") as stream:
    json.dump({"args": args, "cwd": os.getcwd()}, stream); stream.write("\n")
if len(args) >= 4 and args[2].endswith("bootstrap_support.py"):
    if args[3] == "record":
        (state / "runtime.json").write_text('{"offline_fixture":true}\n', encoding="utf-8")
        print('{"ok":true,"offline_fixture":true}')
    elif args[3] == "validate" and "--quiet" not in args:
        print('{"ok":true,"reused":true,"offline_fixture":true}')
else:
    print(json.dumps({"args": args, "home": os.environ.get("HOME"), "cwd": os.getcwd(), "UV_INDEX_URL": os.environ.get("UV_INDEX_URL")}))
'''


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("DSH_INVOKER_TEST_TMP") or os.environ.get("DSH_INVOKER_TEST_ROOT")
        cls.scratch_root = REPO / ".dsh-invoker-profile"
        cls.scratch_tmp = Path(configured) if configured else cls.scratch_root / "dev-tests"
        cls.had_root = cls.scratch_root.exists()
        cls.had_tmp = cls.scratch_tmp.exists()
        cls.scratch_tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if not cls.had_tmp:
            try:
                cls.scratch_tmp.rmdir()
            except OSError:
                pass
        if not cls.had_root:
            try:
                cls.scratch_root.rmdir()
            except OSError:
                pass

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bootstrap-", dir=self.scratch_tmp)
        self.base = Path(self.temporary.name)
        self.project = self.base / ('项目 with spaces' if os.name == 'nt' else '项目 with spaces "quoted"')
        self.project.mkdir()
        self.home = self.base / "user home"
        (self.home / ".dsh/profiles/sdk").mkdir(parents=True)
        (self.home / ".dsh/profiles/custom name").mkdir()
        (self.home / ".dsh/profiles/sdk/secret.yaml").write_text("SECRET_DO_NOT_PRINT", encoding="utf-8")
        self.tools = self.base / "controlled bin"
        self.tools.mkdir()
        self.env = dict(os.environ)
        for key in tuple(self.env):
            if key.startswith(("UV_", "PIP_", "CONDA_", "PYTHON")) or key in ("VIRTUAL_ENV", "DSH_HOME"):
                self.env.pop(key, None)
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = str(self.tools)
        for name in ("dirname", "uname", "sw_vers", "getconf", "mkdir", "rmdir", "mktemp", "curl", "tar", "sha256sum", "shasum", "cp", "chmod", "rm", "readlink"):
            source = shutil.which(name)
            if source and os.name != "nt":
                (self.tools / name).symlink_to(source)

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def state(self):
        return self.project / ".dsh-invoker-profile"

    def bash(self, action, *args, env=None, script=SCRIPT):
        return subprocess.run([BASH, str(script), action, "--project-root", str(self.project), *args], env=env or self.env,
                              cwd=SKILL / "scripts", text=True, encoding="utf-8", capture_output=True, timeout=30)

    def fake_uv(self):
        code = self.base / "fake_uv.py"
        runtime = self.base / "fake_runtime.py"
        code.write_text(FAKE_UV, encoding="utf-8")
        runtime.write_text(FAKE_RUNTIME, encoding="utf-8")
        command = "#!/bin/bash\nexec " + shlex.quote(sys.executable) + " -I -B " + shlex.quote(str(code)) + ' "$@"\n'
        (self.tools / "uv").write_text(command, encoding="utf-8")
        (self.tools / "uv").chmod(0o700)
        self.env["FAKE_REAL_PYTHON_QUOTED"] = shlex.quote(sys.executable)
        self.env["FAKE_RUNTIME_QUOTED"] = shlex.quote(str(runtime))


@unittest.skipIf(os.name == "nt" or BASH is None, "POSIX Bash lane; native PowerShell tests run separately")
class PosixBootstrapTests(FixtureCase):
    def test_check_no_uv_or_python_is_read_only(self):
        before = sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))
        result = self.bash("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["uv"]["available"])
        self.assertEqual(data["runtime"]["status"], "missing")
        self.assertEqual(data["runtime"]["python_path"], str(self.state / "venv/bin/python"))
        self.assertEqual(data["home_candidates"][0]["profiles"], ["custom name", "sdk"])
        self.assertNotIn("SECRET_DO_NOT_PRINT", result.stdout + result.stderr)
        self.assertEqual(before, sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*")))
        self.assertFalse(self.state.exists())

    def test_check_does_not_execute_uv_python_or_dsh(self):
        for name in ("uv", "python", "python3", "dsh"):
            path = self.tools / name
            path.write_text("#!/bin/bash\nprintf 'EXECUTED' >&2\nexit 91\n", encoding="utf-8")
            path.chmod(0o700)
        result = self.bash("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("EXECUTED", result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["uv"]["available"])
        self.assertEqual(data["uv"]["path"], str(self.tools / "uv"))
        self.assertIsNone(data["uv"]["version"])

    def test_env_home_relative_and_blank(self):
        self.env["DSH_HOME"] = "relative/../custom dsh"
        result = self.bash("check")
        data = json.loads(result.stdout)
        self.assertEqual(data["home_candidates"][0]["home"], str(self.project / "custom dsh"))
        self.assertFalse(data["home_candidates"][0]["exists"])
        self.env["DSH_HOME"] = " \t\r\n"
        self.assertEqual(len(json.loads(self.bash("check").stdout)["home_candidates"]), 1)

    def test_missing_exec_and_relative_project_never_install(self):
        result = self.bash("exec", "--", "-c", "print('not executed')")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        result = subprocess.run([BASH, str(SCRIPT), "check", "--project-root", "."], env=self.env,
                                cwd=self.project, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())

    def test_state_symlink_escape_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.state.symlink_to(outside, target_is_directory=True)
        for action in ("check", "setup", "exec"):
            self.assertNotEqual(self.bash(action).returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unowned_and_incomplete_runtime_not_overwritten(self):
        self.state.mkdir()
        sentinel = self.state / "venv"
        sentinel.mkdir()
        self.assertNotEqual(self.bash("setup").returncode, 0)
        self.assertEqual(list(self.state.iterdir()), [sentinel])
        sentinel.rmdir()
        (self.state / ".bootstrap-owner").write_text("dsh-invoker-bootstrap-v1\n", encoding="utf-8")
        result = self.bash("setup")
        self.assertIn("incomplete_runtime", result.stderr)
        self.assertFalse((self.state / "python").exists())

    def test_setup_argument_plan_env_isolation_and_exec_reuse(self):
        self.fake_uv()
        self.env.update(UV_INDEX_URL="https://credential:secret@evil.invalid", UV_PYTHON_INSTALL_DIR="/external/python",
                        CONDA_PREFIX="/external/conda", VIRTUAL_ENV="/external/venv", PYTHONPATH="/external/inject",
                        PIP_EXTRA_INDEX_URL="https://evil.invalid", HTTP_PROXY="http://proxy:password@localhost:8888")
        self.state.mkdir()
        (self.state / "dev-tests").mkdir()
        (self.state / "ci-scenarios").mkdir()
        binding = self.state / "binding.json"
        binding.write_text('{"home":"not read","profile":"sdk"}', encoding="utf-8")
        result = self.bash("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(binding.read_text(encoding="utf-8"), '{"home":"not read","profile":"sdk"}')
        calls = [json.loads(line) for line in (self.state / "uv-calls.jsonl").read_text(encoding="utf-8").splitlines()]
        plans = [call["args"] for call in calls]
        for call in calls:
            self.assertEqual(call["args"][:3], ["--no-config", "--cache-dir", str(self.state / "cache")])
            self.assertEqual(call["cwd"], str(SKILL / "scripts"))
            self.assertEqual(call["env"]["UV_PYTHON_INSTALL_DIR"], str(self.state / "python"))
            self.assertEqual(call["env"]["TMPDIR"], str(self.state / "tmp"))
            self.assertEqual(call["env"]["HOME"], str(self.state / "credentials"))
            self.assertEqual(call["env"]["NETRC"], str(self.state / "credentials/no-netrc"))
            self.assertEqual(call["env"]["HTTP_PROXY"], self.env["HTTP_PROXY"])
            for key in ("UV_INDEX_URL", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PIP_EXTRA_INDEX_URL"):
                self.assertNotIn(key, call["env"])
        python_install = next(p for p in plans if "install" in p and "python" in p and "--help" not in p)
        self.assertEqual(python_install[3:], ["python", "install", "--install-dir", str(self.state / "python"), "--no-bin", "--no-registry", "3.12"])
        venv = next(p for p in plans if "venv" in p and "--help" not in p)
        for flag in ("--no-project", "--managed-python", "--no-python-downloads"):
            self.assertIn(flag, venv)
        pip = next(p for p in plans if "pip" in p and "--help" not in p)
        for flag in ("--no-python-downloads", "--link-mode", "copy", "--only-binary", ":all:", "--prerelease", "explicit", "--default-index", "https://pypi.org/simple", "--keyring-provider", "disabled", "--no-sources"):
            self.assertIn(flag, pip)
        self.assertEqual(pip[pip.index("--python") + 1], str(self.state / "venv/bin/python"))
        self.assertIn("deepseek-harness-sdk==0.1.5rc1", pip)
        self.assertIn("deepseek-harness-runtime-bin==0.1.5rc1", pip)
        reused = self.bash("setup")
        self.assertEqual(reused.returncode, 0, reused.stderr)
        self.assertTrue(json.loads(reused.stdout)["reused"])
        self.assertEqual(len(calls), len((self.state / "uv-calls.jsonl").read_text(encoding="utf-8").splitlines()))
        execution = self.bash("exec", "--", "-c", "script args are forwarded", "value with spaces")
        self.assertEqual(execution.returncode, 0, execution.stderr)
        data = json.loads(execution.stdout)
        self.assertEqual(data["args"], ["-I", "-B", "-c", "script args are forwarded", "value with spaces"])
        self.assertEqual(data["home"], str(self.home))
        self.assertIsNone(data["UV_INDEX_URL"])
        self.assertFalse((self.state / ".bootstrap-lock").exists())
        self.assertFalse((self.project / ".gitignore").exists())
        self.assertEqual(self.env["UV_PYTHON_INSTALL_DIR"], "/external/python")

    def external_site(self):
        directory = self.base / "authored external site"
        directory.mkdir(exist_ok=True)
        marker = directory / "startup-ran.txt"
        (directory / "outside.pth").write_text(
            "import pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text('UNSAFE_STARTUP', encoding='utf-8')\n",
            encoding="utf-8",
        )
        return directory, marker

    def ready_fixture(self):
        self.fake_uv()
        result = self.bash("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Write genuine ready-metadata shape, not a missing/invalid manifest:
        # the negative case must be rejected for its link BEFORE Python starts.
        python = self.state / "venv/bin/python"
        base = self.state / "python/offline-python"
        with mock.patch.object(SUPPORT.sys, "prefix", str(self.state / "venv")), \
             mock.patch.object(SUPPORT.sys, "base_prefix", str(base)), \
             mock.patch.object(SUPPORT.sys, "executable", str(python)), \
             mock.patch.object(SUPPORT.sys, "version_info", (3, 12, 9)), \
             mock.patch.object(SUPPORT.platform, "python_version", return_value="3.12.9"), \
             mock.patch.object(SUPPORT.importlib.metadata, "version", return_value="0.1.5rc1"):
            metadata = SUPPORT.expected_metadata(self.project, self.state)
            metadata["uv"] = {"path": str(self.tools / "uv"), "version": "0.9.20", "source": "path", "sha256": None}
            (self.state / "runtime.json").write_text(json.dumps(metadata), encoding="utf-8")
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(SUPPORT.main(["validate", "--project-root", str(self.project)]), 0)
        return (self.state / "uv-calls.jsonl").read_text(encoding="utf-8"), (self.state / "python-calls.jsonl").read_text(encoding="utf-8")

    def test_nested_startup_links_blocked_before_uv_or_python_on_reuse(self):
        uv_before, python_before = self.ready_fixture()
        outside, marker = self.external_site()
        site = self.state / "venv/lib/python3.12/site-packages"
        site.parent.mkdir(parents=True)
        for kind in ("external", "external_chain", "dangling", "loop", "internal_alias_escape"):
            with self.subTest(kind=kind):
                if kind == "external":
                    site.symlink_to(outside, target_is_directory=True)
                elif kind == "external_chain":
                    alias = self.state / "python/alias"
                    alias.symlink_to(outside, target_is_directory=True)
                    site.symlink_to("../../../python/alias", target_is_directory=True)
                elif kind == "dangling":
                    site.symlink_to(self.state / "missing", target_is_directory=True)
                elif kind == "loop":
                    site.symlink_to("site-packages", target_is_directory=True)
                else:
                    alias = self.state / "startup-alias"
                    alias.mkdir()
                    (alias / "escape").symlink_to(outside, target_is_directory=True)
                    site.symlink_to("../../../startup-alias", target_is_directory=True)
                for action in ("setup", "exec"):
                    result = self.bash(action, *(["--", "-c", "not executed"] if action == "exec" else []))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unsafe_path", result.stderr)
                    self.assertFalse(marker.exists(), "external .pth executed before native rejection")
                    self.assertEqual((self.state / "uv-calls.jsonl").read_text(encoding="utf-8"), uv_before)
                    self.assertEqual((self.state / "python-calls.jsonl").read_text(encoding="utf-8"), python_before)
                site.unlink()
                if kind == "external_chain":
                    alias.unlink()
                elif kind == "internal_alias_escape":
                    (alias / "escape").unlink()
                    alias.rmdir()

    def test_internal_relative_startup_links_are_allowed(self):
        self.ready_fixture()
        library = self.state / "venv/lib"
        library.mkdir(exist_ok=True)
        site = library / "python3.12/site-packages"
        site.parent.mkdir()
        internal = self.state / "python/internal-site"
        internal.mkdir()
        site.symlink_to("../../../python/internal-site", target_is_directory=True)
        (self.state / "venv/lib64").symlink_to("lib", target_is_directory=True)
        (internal / "parent-alias").symlink_to(".", target_is_directory=True)
        for action in ("setup", "exec"):
            result = self.bash(action, *(["--", "-c", "safe internal aliases"] if action == "exec" else []))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_setup_checks_links_before_first_python_and_before_record(self):
        self.fake_uv()
        outside, marker = self.external_site()
        self.env["FAKE_EXTERNAL_SITE"] = str(outside)
        original = self.project
        for phase in ("venv", "pip"):
            with self.subTest(phase=phase):
                self.project = original / phase
                self.project.mkdir()
                self.env["FAKE_ESCAPE_PHASE"] = phase
                result = self.bash("setup")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe_path", result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse((self.state / "runtime.json").exists())
                calls = self.state / "python-calls.jsonl"
                if phase == "venv":
                    self.assertFalse(calls.exists())
                    plans = [json.loads(line)["args"] for line in (self.state / "uv-calls.jsonl").read_text(encoding="utf-8").splitlines()]
                    self.assertFalse(any("pip" in args and "--help" not in args for args in plans))
                else:
                    self.assertEqual(len(calls.read_text(encoding="utf-8").splitlines()), 1, "record ran after escaping wheel link")
                    self.assertIn("check-python", calls.read_text(encoding="utf-8"))
        self.project = original

    def test_uv_failures_are_redacted_and_existing_uv_not_updated(self):
        self.fake_uv()
        self.env["FAKE_UV_FAIL"] = "1"
        result = self.bash("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python_install", result.stderr)
        self.assertNotIn("password", result.stdout + result.stderr)
        self.assertNotIn("PRIVATE", result.stdout + result.stderr)
        self.assertFalse((self.state / "runtime.json").exists())
        self.assertFalse((self.state / ".bootstrap-lock").exists())
        self.assertTrue((self.tools / "uv").is_file())

    def test_old_uv_fails_without_fallback_or_update(self):
        self.fake_uv()
        self.env["FAKE_UV_VERSION"] = "0.8.0"
        result = self.bash("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv_version", result.stderr)
        calls = [json.loads(line)["args"] for line in (self.state / "uv-calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertIn("--version", calls[0])

    def test_missing_uv_flag_fails_before_install(self):
        self.fake_uv()
        self.env["FAKE_MISSING_FLAG"] = "--no-registry"
        result = self.bash("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv_capability", result.stderr)
        self.assertFalse((self.state / "venv").exists())

    def test_absent_uv_archive_rejects_traversal_symlink_and_bad_digest(self):
        # Local test-authored archives replace curl in a copied fixture skill.
        # No remote bytes are executed; extraction is never reached on errors.
        for kind in ("traversal", "symlink", "digest"):
            with self.subTest(kind=kind):
                project = self.project / kind
                project.mkdir()
                copied = self.base / ("skill-" + kind)
                (copied / "scripts").mkdir(parents=True)
                (copied / "assets").mkdir()
                shutil.copyfile(SCRIPT, copied / "scripts/bootstrap.sh")
                metadata = json.loads((SKILL / "assets/uv-release.json").read_text(encoding="utf-8"))
                platform_name = json.loads(self.bash("check").stdout)["platform"]
                asset, _ = metadata["assets"][platform_name]
                root = asset.removesuffix(".tar.gz")
                archive = self.base / (kind + ".tar.gz")
                with tarfile.open(archive, "w:gz") as tf:
                    entry = tarfile.TarInfo(root + "/uv" if kind != "traversal" else "../uv")
                    if kind == "symlink":
                        entry.type = tarfile.SYMTYPE; entry.linkname = "/external/uv"; tf.addfile(entry)
                    else:
                        data = b"not executable"; entry.size = len(data); tf.addfile(entry, io.BytesIO(data))
                metadata["assets"][platform_name][1] = "0" * 64 if kind == "digest" else hashlib.sha256(archive.read_bytes()).hexdigest()
                (copied / "assets/uv-release.json").write_text(json.dumps(metadata, indent=2).replace('[\n      "' + asset + '",\n      "', '["' + asset + '", "').replace('"\n    ]', '"]'), encoding="utf-8")
                # Preserve the one-line-per-asset format used for Python-free
                # shell reading; other metadata is not needed by this test.
                (copied / "assets/uv-release.json").write_text('{\n "version": "0.12.24",\n "' + platform_name + '": ["' + asset + '", "' + metadata["assets"][platform_name][1] + '"]\n}\n', encoding="utf-8")
                curl = self.tools / "curl"
                if curl.is_symlink(): curl.unlink()
                curl.write_text('#!/bin/bash\nwhile [[ $# -gt 0 ]]; do if [[ "$1" = --output ]]; then shift; out="$1"; fi; shift; done\n' + shlex.quote(shutil.which("cp")) + ' ' + shlex.quote(str(archive)) + ' "$out"\n', encoding="utf-8")
                curl.chmod(0o700)
                result = subprocess.run([BASH, str(copied / "scripts/bootstrap.sh"), "setup", "--project-root", str(project)], env=self.env,
                                        cwd=SKILL / "scripts", text=True, encoding="utf-8", capture_output=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("uv_digest" if kind == "digest" else "uv_archive", result.stderr)
                self.assertFalse((project / ".dsh-invoker-profile/bin/uv").exists())
                self.assertFalse((self.base / "uv").exists())


class RuntimeManifestTests(FixtureCase):
    def minimal_state(self):
        self.state.mkdir()
        for directory in SUPPORT.TECHNICAL_DIRS:
            (self.state / directory).mkdir()
        (self.state / ".bootstrap-owner").write_text(SUPPORT.OWNER_MARKER + "\n", encoding="utf-8")
        venv_python = self.state / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        venv_python.parent.mkdir(exist_ok=True)
        venv_python.write_text("offline-fixture", encoding="utf-8")
        base = self.state / "python/offline-base"
        base.mkdir()
        return venv_python, base

    def test_pinned_release_metadata_matches_verified_official_digests(self):
        data = json.loads((SKILL / "assets/uv-release.json").read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "0.12.24")
        expected = {
            "macos-arm64": "0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732",
            "macos-x86_64": "4fa82e37cb94767661f532b001e470b67a186c7260e305bd84ddb78fd545c0b6",
            "linux-aarch64": "5231be65f496304623895dacdbf1de8504fec90303684bdf05805aa34414dd21",
            "linux-x86_64": "b4dfaef47d491a7296981f8374a4595f55dbf84e8937c8ecd2983574d8bb3da6",
            "windows-x86_64": "7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019",
        }
        self.assertEqual({key: value[1] for key, value in data["assets"].items()}, expected)

    def test_record_validate_and_reject_mutated_version(self):
        python, base = self.minimal_state()
        patches = [mock.patch.object(SUPPORT.sys, "prefix", str(self.state / "venv")),
                   mock.patch.object(SUPPORT.sys, "base_prefix", str(base)),
                   mock.patch.object(SUPPORT.sys, "executable", str(python)),
                   mock.patch.object(SUPPORT.sys, "version_info", (3, 12, 9)),
                   mock.patch.object(SUPPORT.platform, "python_version", return_value="3.12.9"),
                   mock.patch.object(SUPPORT.importlib.metadata, "version", return_value="0.1.5rc1")]
        for patch in patches: patch.start(); self.addCleanup(patch.stop)
        args = ["--project-root", str(self.project)]
        with mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(SUPPORT.main(["record", *args, "--platform", SUPPORT.actual_platform(), "--uv-path", str(self.tools / "uv"), "--uv-version", "0.9.20", "--uv-source", "path"]), 0, err.getvalue())
            self.assertEqual(SUPPORT.main(["validate", *args]), 0, err.getvalue())
            self.assertEqual(SUPPORT.main(["record", *args]), 1)
        manifest = self.state / "runtime.json"
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertNotIn("profile", saved)
        self.assertNotIn("home", saved)
        saved["sdk_version"] = "0.1.6"
        manifest.write_text(json.dumps(saved), encoding="utf-8")
        with mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(SUPPORT.main(["validate", *args]), 1)
            self.assertIn("runtime_version", err.getvalue())

    def test_nested_symlink_escape_rejected(self):
        self.minimal_state()
        if os.name == "nt": self.skipTest("Native junction safety covered by PowerShell lane")
        (self.state / "cache/escape").symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(SUPPORT.BootstrapError) as error:
            SUPPORT.validate_paths(self.project)
        self.assertEqual(error.exception.code, "unsafe_path")

    def test_project_uv_digest_change_rejected(self):
        self.minimal_state()
        uv = self.state / "bin" / ("uv.exe" if os.name == "nt" else "uv")
        uv.write_bytes(b"test uv")
        metadata = {"path": str(uv), "version": "0.12.24", "source": "project", "sha256": hashlib.sha256(b"test uv").hexdigest()}
        SUPPORT.validate_uv(metadata, self.state)
        uv.write_bytes(b"changed uv")
        with self.assertRaises(SUPPORT.BootstrapError): SUPPORT.validate_uv(metadata, self.state)


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "requires native Windows and pwsh")
class PowerShellBootstrapTests(FixtureCase):
    def test_native_nested_junction_rejected_before_interpreter_and_internal_allowed(self):
        pwsh = shutil.which("pwsh")
        self.state.mkdir()
        for name in SUPPORT.TECHNICAL_DIRS:
            (self.state / name).mkdir()
        (self.state / ".bootstrap-owner").write_text(SUPPORT.OWNER_MARKER + "\n", encoding="utf-8")
        base = self.state / "python/offline-base"
        base.mkdir()
        for name in ("python.exe", "python312.dll"):
            (base / name).write_text("not executed: offline native fixture", encoding="utf-8")
        python = self.state / "venv/Scripts/python.exe"
        python.parent.mkdir()
        python.write_text("not executed: offline native fixture", encoding="utf-8")
        (self.state / "venv/pyvenv.cfg").write_text("home = " + str(base) + "\n", encoding="utf-8")
        library = self.state / "venv/Lib"
        library.mkdir()
        site = library / "site-packages"
        outside, marker = self.external_site_for_windows()
        # Replace only process creation in a fixture COPY. This records whether
        # either uv or Python would start, without executing an EXE or installing.
        code = (SKILL / "scripts/bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("#requires -Version 7.2", code)
        self.assertNotIn("$PSNativeCommandUseErrorActionPreference", code)
        instrumented = self.base / "bootstrap.ps1"
        code = code.replace("$null = $process.Start()", "[IO.File]::WriteAllText($env:BOOTSTRAP_PROCESS_MARKER, 'unexpected interpreter/uv start'); throw 'fixture process intercepted'")
        instrumented.write_text(code, encoding="utf-8")
        env = dict(self.env, BOOTSTRAP_PROCESS_MARKER=str(marker))
        with mock.patch.object(SUPPORT.sys, "prefix", str(self.state / "venv")), \
             mock.patch.object(SUPPORT.sys, "base_prefix", str(base)), \
             mock.patch.object(SUPPORT.sys, "executable", str(python)), \
             mock.patch.object(SUPPORT.sys, "version_info", (3, 12, 9)), \
             mock.patch.object(SUPPORT.platform, "python_version", return_value="3.12.9"), \
             mock.patch.object(SUPPORT.importlib.metadata, "version", return_value="0.1.5rc1"):
            metadata = SUPPORT.expected_metadata(self.project, self.state)
            metadata["uv"] = {"path": str(self.tools / "uv.exe"), "version": "0.9.20", "source": "path", "sha256": None}
            (self.state / "runtime.json").write_text(json.dumps(metadata), encoding="utf-8")
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(SUPPORT.main(["validate", "--project-root", str(self.project)]), 0)
        for target in (outside, self.state / "missing", base):
            if target.name == "missing":
                target.mkdir()
            command = "$ErrorActionPreference='Stop'; $null=New-Item -ItemType Junction -Path $env:TEST_LINK -Target $env:TEST_TARGET"
            linked = subprocess.run([pwsh, "-NoProfile", "-Command", command], env=dict(env, TEST_LINK=str(site), TEST_TARGET=str(target)),
                                    cwd=SKILL / "scripts", capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(linked.returncode, 0, linked.stderr)
            if target.name == "missing":
                target.rmdir()
            for action in ("setup", "exec"):
                result = subprocess.run([pwsh, "-NoProfile", "-File", str(instrumented), "-Action", action, "-ProjectRoot", str(self.project)],
                                        env=env, cwd=SKILL / "scripts", capture_output=True, text=True, encoding="utf-8", timeout=30)
                if target == base:
                    self.assertNotIn("unsafe_path", result.stderr, result.stderr)
                    self.assertTrue(marker.exists(), "legitimate internal junction did not reach instrumented interpreter")
                    marker.unlink()
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unsafe_path", result.stderr)
                    self.assertFalse(marker.exists(), "Python/uv started before native junction rejection")
            site.rmdir()

    def external_site_for_windows(self):
        directory = self.base / "authored external site"
        directory.mkdir()
        return directory, directory / "interpreter-started.txt"

    def test_check_no_uv_python_or_dsh_is_read_only(self):
        env = dict(self.env, USERPROFILE=str(self.home), DSH_HOME=str(self.home / '.dsh'))
        result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(SKILL / "scripts/bootstrap.ps1"),
                                 "-Action", "check", "-ProjectRoot", str(self.project)],
                                env=env, cwd=SKILL / "scripts", capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["uv"]["source"], "absent")
        self.assertEqual(value["home_candidates"][0]["home"], str(self.home / ".dsh"))
        self.assertFalse(self.state.exists())
        self.assertNotIn("SECRET_DO_NOT_PRINT", result.stdout + result.stderr)

    def test_exec_missing_runtime_does_not_install(self):
        result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(SKILL / "scripts/bootstrap.ps1"),
                                 "-Action", "exec", "-ProjectRoot", str(self.project)],
                                env=self.env, cwd=SKILL / "scripts", capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
