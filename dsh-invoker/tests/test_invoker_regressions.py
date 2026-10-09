"""Repair regressions; synthetic homes only, no provider requests or installs.

Released-runtime tests use the existing loopback/owned-watchdog fixture and skip
only when the pinned wheels are absent (the required native lane fails instead).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tracemalloc
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "dsh-invoker" / "scripts" / "dsh_invoker.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


invoker = load("invoker_repair_under_test", HELPER)
runtime = load("invoker_repair_native_fixture", Path(__file__).with_name("test_runtime_integration.py"))


class RepairFixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".dsh-invoker-profile" / "tmp" / "repair-tests"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="repair-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project 项目"
        self.workspace = self.root / "workspace"
        self.home = self.root / "synthetic dsh home"
        self.project.mkdir()
        self.workspace.mkdir()
        self.env = runtime.isolated_environment(self.root)
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(invoker.Path, "home", return_value=self.root / "os home 空格")
        patch.start()
        self.addCleanup(patch.stop)
        self.configure()

    def args(self, action, *options):
        return invoker.build_parser().parse_args([action, "--project-root", str(self.project), *map(str, options)])

    def configure(self, *options, home=None):
        return invoker.configure(self.project, self.args("configure", "--home", home or self.home, "--confirmed", *options))

    def request(self):
        return {
            "project_root": str(self.project), "cwd": str(self.workspace), "prompt": "first synthetic prompt",
            "session_id": None, "patches": [], "provider": "deepseek-official", "model": "ci-mock-model",
            "max_tokens": None, "request_timeout_seconds": 5.0,
        }

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(invoker.InvokerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def script(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return [sys.executable, "-I", "-B", str(path)]

    def living(self, pid):
        result = subprocess.run(
            ["/bin/ps" if sys.platform == "darwin" else "/usr/bin/ps", "-o", "stat=", "-p", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", timeout=3,
        )
        return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


class BindingRepairTests(RepairFixture):
    def test_binding_snapshot_rejects_legitimate_reconfiguration(self):
        request = self.request()
        options, _, _ = invoker._validate_request(request)
        self.assertEqual(str(self.home), options["dsh_home"])
        self.configure("--reconfigure", home=self.root / "another synthetic home")
        self.error("BINDING_CHANGED", invoker._validate_request, request)

    def test_initialized_sdk_disappearance_requires_reconfigure(self):
        profile = self.home / "profiles" / "sdk"
        profile.mkdir(parents=True)
        for name in invoker.PROFILE_MARKERS:
            (profile / name).write_text("[]", encoding="utf-8")
        invoker._record_initialized(self.project, invoker._load_binding(self.project))
        shutil.rmtree(self.home)
        self.assertEqual("STALE_BINDING", invoker.status(self.project)["profile_problem"])
        self.error("STALE_BINDING", invoker._validate_request, self.request())
        self.configure("--reconfigure")
        self.assertIsNone(invoker.status(self.project)["profile_problem"])
        binding = json.loads((self.project / invoker.STATE_DIRECTORY / invoker.BINDING_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual({"schema_version", "home", "profile"}, set(binding))

    def test_invalid_unselected_environment_is_only_diagnostic(self):
        loop = self.root / "loop"
        try:
            loop.symlink_to(loop)
        except (OSError, NotImplementedError):
            self.skipTest("native symlinks unavailable")
        with mock.patch.dict(os.environ, {"DSH_HOME": str(loop)}):
            result = invoker.status(self.project)
        self.assertEqual(str(self.home), result["selection"]["home"])
        self.assertIsNone(result["profile_problem"])
        self.assertIn({"source": "DSH_HOME", "code": "INVALID_ENV_HOME"}, result["candidate_problems"])

    def test_oversized_regular_binding_explicitly_repairable(self):
        binding = self.project / invoker.STATE_DIRECTORY / invoker.BINDING_FILENAME
        binding.write_bytes(b"x" * (invoker.MAX_BINDING_BYTES + 1))
        self.error("INVALID_BINDING", self.configure)
        self.configure("--reconfigure")
        self.assertEqual(str(self.home), invoker._load_binding(self.project)["home"])

    def test_reserved_profile_names_are_casefolded(self):
        for name in ("Web", "DESKTOP", "Headless", "AcP", "TuI"):
            with self.subTest(name=name):
                self.error("NON_SDK_PROFILE", invoker._check_selected_profile, self.home, name)

    def test_gitignore_broad_negation_and_bare_cr_repaired(self):
        environment = {**self.env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", str(self.project)], env=environment, check=True, stdout=subprocess.DEVNULL)
        for original in (b"/.dsh-invoker-profile/\n!*\n", b"# previous comment\r"):
            with self.subTest(original=original):
                (self.project / ".gitignore").write_bytes(original)
                self.assertTrue(self.configure()["gitignore_updated"])
                result = subprocess.run(
                    ["git", "-C", str(self.project), "-c", "core.excludesFile=" + os.devnull,
                     "check-ignore", "--no-index", ".dsh-invoker-profile/binding.json"],
                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                self.assertEqual(0, result.returncode)
                self.assertFalse(self.configure()["gitignore_updated"])


class InputAndSdkRepairTests(RepairFixture):
    def test_parser_rejects_unknown_followup_flag_before_input_or_sdk(self):
        with mock.patch.object(invoker, "_read_prompt") as reader, \
                mock.patch.object(invoker, "_require_sdk") as sdk, \
                mock.patch.object(invoker, "_supervise") as worker:
            self.error("INVALID_ARGUMENTS", self.args, "invoke", "--cwd", self.workspace,
                       "--stdin", "--followup-file", self.root / "not-opened.txt")
        reader.assert_not_called()
        sdk.assert_not_called()
        worker.assert_not_called()

    def test_worker_request_rejects_followups_before_sdk(self):
        request = {**self.request(), "followups": ["unwanted additional turn"]}
        self.error("INVALID_WORKER_REQUEST", invoker._validate_request, request)
        with mock.patch.object(invoker, "_require_sdk") as sdk, \
                mock.patch.object(invoker.importlib, "import_module") as factory:
            self.error("INVALID_WORKER_REQUEST", invoker._execute_sdk, request)
        sdk.assert_not_called()
        factory.assert_not_called()

    def test_cross_process_session_id_refused_before_input_or_sdk(self):
        arguments = self.args("invoke", "--cwd", self.workspace, "--stdin", "--session-id", "persisted-001")
        with mock.patch.object(invoker, "_read_prompt") as reader, \
                mock.patch.object(invoker, "_require_sdk") as sdk, \
                mock.patch.object(invoker, "_supervise") as worker:
            self.error("UNSUPPORTED_CONTINUATION", invoker.invoke, self.project, arguments)
        reader.assert_not_called()
        sdk.assert_not_called()
        worker.assert_not_called()
        request = self.request()
        request["session_id"] = "persisted-001"
        with mock.patch.object(invoker, "_require_sdk") as sdk, \
                mock.patch.object(invoker.importlib, "import_module") as factory:
            self.error("UNSUPPORTED_CONTINUATION", invoker._execute_sdk, request)
        sdk.assert_not_called()
        factory.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO regression")
    def test_fifo_prompt_refused_without_open_wait(self):
        fifo = self.root / "prompt.fifo"
        os.mkfifo(fifo)
        started = time.monotonic()
        self.error("INVALID_PROMPT", invoker._read_prompt, argparse.Namespace(prompt_file=str(fifo)), self.workspace,
                   deadline=started + 0.15)
        self.assertLess(time.monotonic() - started, 1)

    @unittest.skipUnless(os.name == "posix", "native selectable stdin pipe")
    def test_never_closed_stdin_hits_actual_input_deadline(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"partial UTF-8 prompt")
            with os.fdopen(read_fd, "r", encoding="cp1252") as source, mock.patch.object(sys, "stdin", source):
                started = time.monotonic()
                self.error("INVOCATION_DEADLINE", invoker._read_prompt, argparse.Namespace(prompt_file=None), self.workspace,
                           deadline=started + 0.15)
                self.assertLess(time.monotonic() - started, 1)
        finally:
            os.close(write_fd)

    def test_runtime_mode_forced_before_sdk_factory_for_fresh_turn(self):
        calls = []
        options_seen = []

        class Harness:
            def __init__(self, **options):
                self.assert_exe = os.environ.get("DSH_RUNTIME_MODE") == "exe"
                options_seen.append((self.assert_exe, options))
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def run(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return types.SimpleNamespace(session_id="same-runtime-001", finish_reason="completed", final_response=prompt)

        request = self.request()
        with mock.patch.object(invoker, "_require_sdk"), \
                mock.patch.object(invoker.importlib, "import_module", return_value=types.SimpleNamespace(DeepSeekHarness=Harness)), \
                mock.patch.dict(os.environ, {"DSH_RUNTIME_MODE": "node"}):
            result = invoker._execute_sdk(request)
        self.assertTrue(options_seen[0][0])
        self.assertEqual([("first synthetic prompt", {})], calls)
        self.assertEqual("first synthetic prompt", result["final_response"])
        self.assertEqual((str(invoker.PRIVACY_PATCH),), options_seen[0][1]["patches"])

    def test_incomplete_turn_never_retried(self):
        calls = []
        class Harness:
            def __init__(self, **options):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def run(self, prompt, **kwargs):
                calls.append(prompt)
                return types.SimpleNamespace(session_id="s", finish_reason="max-tokens", final_response="partial")
        request = self.request()
        with mock.patch.object(invoker, "_require_sdk"), \
                mock.patch.object(invoker.importlib, "import_module", return_value=types.SimpleNamespace(DeepSeekHarness=Harness)):
            result = invoker._execute_sdk(request)
        self.assertEqual("max-tokens", result["finish_reason"])
        self.assertEqual([request["prompt"]], calls)


class OwnedProcessRepairTests(RepairFixture):
    def test_output_cap_enforced_during_stream_without_full_buffer(self):
        command = self.script("oversized-worker.py", "import os,sys,time\nsys.stdin.buffer.read()\nfor _ in range(256): os.write(1,b'x'*65536)\ntime.sleep(30)\n")
        tracemalloc.start()
        try:
            self.error("WORKER_FAILURE", invoker._supervise, command, {}, cwd=self.root, environment=self.env, timeout=5)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 20 * 1024 * 1024, "output was buffered beyond the 8 MiB streaming limit")

    @unittest.skipUnless(os.name == "posix", "POSIX detached child regression")
    def test_detached_long_lived_child_cleanup_and_unrelated_survival(self):
        capture = self.root / "owned-pids.json"
        command = self.script("detached-worker.py", (
            "import json,subprocess,sys,time,os\nfrom pathlib import Path\nsys.stdin.buffer.read()\n"
            "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(30)'],start_new_session=True,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f"Path({str(capture)!r}).write_text(json.dumps([os.getpid(),p.pid]),encoding='utf-8')\n"
            "time.sleep(30)\n"
        ))
        sibling = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time;time.sleep(30)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env)
        try:
            self.error("INVOCATION_DEADLINE", invoker._supervise, command, {}, cwd=self.root, environment=self.env, timeout=0.6)
            pids = json.loads(capture.read_text(encoding="utf-8"))
            self.assertTrue(all(not self.living(pid) for pid in pids), "observed detached child survived cleanup")
            self.assertIsNone(sibling.poll(), "cleanup selected an unrelated process")
        finally:
            sibling.kill()
            sibling.wait(timeout=5)
            if capture.exists():
                for pid in json.loads(capture.read_text(encoding="utf-8")):
                    if self.living(pid):
                        os.kill(pid, signal.SIGKILL)  # exact test-created PID only

    @unittest.skipUnless(os.name == "posix", "POSIX supervisor signal regression")
    def test_sigterm_and_sighup_unwind_cleanup_with_correct_exit(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                capture = self.root / f"signal-{signum}.json"
                worker = self.script(f"signal-worker-{signum}.py", (
                    "import json,os,subprocess,sys,time\nfrom pathlib import Path\nsys.stdin.buffer.read()\n"
                    "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(30)'],start_new_session=True,"
                    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                    f"Path({str(capture)!r}).write_text(json.dumps([os.getpid(),p.pid]),encoding='utf-8')\n"
                    "time.sleep(30)\n"
                ))
                driver = self.script(f"supervisor-{signum}.py", (
                    "import importlib.util,sys\n"
                    f"s=importlib.util.spec_from_file_location('helper',{str(HELPER)!r});h=importlib.util.module_from_spec(s);s.loader.exec_module(h)\n"
                    "h._require_sdk=lambda:None\noriginal=h._supervise\n"
                    f"h._supervise=lambda command,request,**kwargs:original({worker!r},request,**kwargs)\n"
                    "raise SystemExit(h.main(sys.argv[1:]))\n"
                ))
                prompt = self.root / "signal-prompt.txt"
                prompt.write_text("synthetic prompt", encoding="utf-8")
                process = subprocess.Popen(
                    [*driver, "invoke", "--project-root", str(self.project), "--cwd", str(self.workspace),
                     "--prompt-file", str(prompt), "--timeout-seconds", "15"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=self.root, env=self.env, start_new_session=True,
                )
                tree = runtime._PosixOwnedTree(process)
                try:
                    until = time.monotonic() + 5
                    while not capture.exists() and process.poll() is None and time.monotonic() < until:
                        time.sleep(0.03)
                    self.assertTrue(capture.exists(), "owned synthetic worker did not start")
                    time.sleep(0.1)  # permit an ancestry sample before cancellation
                    os.kill(process.pid, signum)
                    stdout, stderr = process.communicate(timeout=6)
                    self.assertEqual(128 + signum, process.returncode)
                    self.assertEqual(b"", stderr)
                    self.assertEqual("INTERRUPTED", json.loads(stdout)["error"]["code"])
                    self.assertTrue(all(not self.living(pid) for pid in json.loads(capture.read_text(encoding="utf-8"))))
                finally:
                    tree.cleanup(timed_out=True)
                    process.wait(timeout=5)
                    for stream in (process.stdout, process.stderr):
                        stream.close()

    def test_supervisor_restores_signal_handlers(self):
        before = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
        with invoker._termination_handlers():
            pass
        self.assertEqual(before, {signum: signal.getsignal(signum) for signum in before})


class ReleasedRuntimeRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.NativeRuntimeIntegration.setUpClass.__func__(cls)

    def setUp(self):
        parent = ROOT / ".dsh-invoker-profile" / "tmp" / "repair-tests"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="released-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = runtime.RuntimeFixture(self.root)

    def successful(self, command, env):
        result = runtime.run_owned(command, cwd=self.fixture.workspace, env=env, timeout=65)
        self.assertFalse(result.timed_out, "independent owned watchdog fired")
        self.assertEqual(0, result.returncode, "native operation failed; raw diagnostics withheld")
        self.assertNotIn(runtime.DUMMY_KEY, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def configure(self, env):
        self.successful([sys.executable, "-I", "-B", str(HELPER), "configure", "--project-root", str(self.fixture.project),
                         "--home", str(self.fixture.home), "--profile", "sdk", "--confirmed"], env)

    def test_independent_fresh_calls_persist_privately_and_old_id_sends_no_request(self):
        first_prompt, second_prompt = "REPAIR_FIRST_USER_94b", "REPAIR_INDEPENDENT_USER_7d2"
        first_answer, second_answer = "REPAIR_FIRST_ASSISTANT_abc", "REPAIR_SECOND_ASSISTANT_def"
        class Model(runtime.MockModel):
            def chunks(self, body):
                messages = body["messages"]
                users = [runtime._message_text(message.get("content")) for message in messages
                         if message.get("role") == "user" and runtime._message_text(message.get("content")) in (first_prompt, second_prompt)]
                if users == [first_prompt]:
                    return runtime._text_chunks(first_answer)
                if users == [second_prompt] and all(first_prompt not in json.dumps(message) and first_answer not in json.dumps(message)
                                                    for message in messages):
                    return runtime._text_chunks(second_answer)
                raise AssertionError("independent task inherited another session's history")
        fixture = self.fixture
        fixture.prompt.write_text(first_prompt, encoding="utf-8")
        second_input = self.root / "independent-task.txt"
        second_input.write_text(second_prompt, encoding="utf-8")
        command = [
            sys.executable, "-I", "-B", str(HELPER), "invoke", "--project-root", str(fixture.project),
            "--cwd", str(fixture.workspace), "--patch", str(fixture.overlay), "--timeout-seconds", "45",
        ]
        with Model() as model:
            env = fixture.environment(model.url)
            env["DSH_RUNTIME_MODE"] = "node"  # worker must force the published exe resolver
            self.configure(env)
            first = self.successful([*command, "--prompt-file", str(fixture.prompt)], env)
            self.assertEqual(1, len(model.requests))
            # Reject the old ID even before attempting to read a missing input.
            rejected = runtime.run_owned([
                *command, "--prompt-file", str(self.root / "must-not-be-read.txt"), "--session-id", first["session_id"],
            ], cwd=fixture.workspace, env=env, timeout=10)
            self.assertFalse(rejected.timed_out)
            self.assertEqual(3, rejected.returncode)
            self.assertEqual("UNSUPPORTED_CONTINUATION", json.loads(rejected.stdout)["error"]["code"])
            self.assertEqual(1, len(model.requests), "unsupported continuation made a second model request")
            second = self.successful([*command, "--prompt-file", str(second_input)], env)
            for value, answer in ((first, first_answer), (second, second_answer)):
                self.assertEqual({"session_id", "finish_reason", "final_response"}, set(value))
                self.assertEqual("completed", value["finish_reason"])
                self.assertEqual(answer, value["final_response"])
            self.assertNotEqual(first["session_id"], second["session_id"])
            self.assertEqual(2, len(model.requests))
            self.assertFalse(model.errors)
            self.assertTrue(all("dsh_session_log" not in body for _, body in model.requests))
        for value, prompt, answer in ((first, first_prompt, first_answer), (second, second_prompt, second_answer)):
            runtime.NativeRuntimeIntegration.assert_local_session(self, fixture, value["session_id"], prompt)
            runtime.NativeRuntimeIntegration.assert_local_session(self, fixture, value["session_id"], answer)
        self.assertTrue(all(path.read_bytes() == original for path, original in fixture.unchanged.items()))

    def test_released_public_client_rejects_restore_methods_without_model_requests(self):
        fixture = self.fixture
        # Child uses only the official client (not a private wire implementation).
        script = self.root / "verify-released-protocol.py"
        script.write_text(
            "import json\nfrom deepseek_harness.client import HarnessClient,HarnessConfig\n"
            "from deepseek_harness.errors import JsonRpcError\nfrom pydantic import BaseModel\n"
            "class Response(BaseModel): pass\n"
            f"with HarnessClient(HarnessConfig(dsh_home={str(fixture.home)!r},profile='sdk',cwd={str(fixture.workspace)!r},"
            f"patches=({str(runtime.PRIVACY_PATCH)!r},),request_timeout_seconds=5)) as c:\n"
            f" c.initialize(cwd={str(fixture.workspace)!r},provider='deepseek-official',model='ci-mock-model')\n"
            " rejected=[]\n for method in ('session/restore','session/resume','session/load'):\n"
            "  try: c.request(method,{'sessionId':'synthetic-id'},response_model=Response)\n"
            "  except JsonRpcError: rejected.append(method)\n"
            " print(json.dumps({'rejected':rejected}))\n", encoding="utf-8")
        with runtime.MockModel() as model:
            env = fixture.environment(model.url)
            value = self.successful([sys.executable, "-I", "-B", str(script)], env)
            self.assertEqual(["session/restore", "session/resume", "session/load"], value["rejected"])
            self.assertEqual([], model.requests)


if __name__ == "__main__":
    unittest.main()
