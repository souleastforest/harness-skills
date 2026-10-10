"""Fake-SDK tests of invocation, privacy, finish results and owned-worker cleanup.

No installed SDK/runtime, provider credential or network request is used here.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[2]
HELPER = PROJECT / "dsh-invoker" / "scripts" / "dsh_invoker.py"
SPEC = importlib.util.spec_from_file_location("dsh_invoker_sdk_under_test", HELPER)
invoker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(invoker)


def _windows_pid_stopped(pid, timeout_ms=4000):
    # os.kill(pid, 0) is NOT a passive Windows process probe. Open only a
    # synchronization handle: no termination, console events or extra privilege.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return True
        raise ctypes.WinError(error)  # access/query denial is not cleanup success
    try:
        status = kernel.WaitForSingleObject(handle, timeout_ms)
        if status == 0:  # WAIT_OBJECT_0: the process has exited
            return True
        if status == 0x102:  # WAIT_TIMEOUT: the process is still alive
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class FakeSdkFixture(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT / ".dsh-invoker-profile" / "dev-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="fake-sdk-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project spaces 项目"
        self.project.mkdir()
        self.workspace = self.base / "workspace spaces 工作"
        self.workspace.mkdir()
        self.home = self.base / "dsh home"
        self.os_home = self.base / "os-home"
        self.os_home.mkdir()
        self.prompt_file = self.workspace / "prompt file.txt"
        self.prompt_file.write_text("Review the mock task: 中文 and spaces.\n", encoding="utf-8")
        self.capture = self.base / "test-only-capture.json"
        self.worker_script = self.base / "fake_worker.py"
        self._write_worker()
        self.real_supervise = invoker._supervise
        self.cli_ok("configure", "--home", str(self.home), "--confirmed")

    def cli(self, action, *args, stdin=None, env=None, project=None):
        output, error = io.StringIO(), io.StringIO()
        argv = [action, "--project-root", str(project or self.project), *map(str, args)]
        with mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(invoker.Path, "home", return_value=self.os_home), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            if stdin is None:
                code = invoker.main(argv)
            else:
                with mock.patch.object(sys, "stdin", stdin):
                    code = invoker.main(argv)
        self.assertEqual("", error.getvalue())
        self.assertNotIn("FAKE_SENSITIVE", output.getvalue(), "SDK logs or raw exceptions leaked")
        return code, json.loads(output.getvalue())

    def cli_ok(self, *args, **kwargs):
        code, document = self.cli(*args, **kwargs)
        self.assertEqual(0, code, document)
        return document

    def invoke(self, *options, fake_mode="completed", stdin=None, timeout=5):
        def fake_supervise(command, request, *, cwd, environment, timeout):
            # Substitute only the SDK import fixture; use the production supervisor.
            environment = dict(environment)
            environment.update({"FAKE_MODE": fake_mode, "FAKE_CAPTURE": str(self.capture)})
            return self.real_supervise([sys.executable, "-I", "-B", str(self.worker_script)], request,
                                       cwd=cwd, environment=environment, timeout=timeout)
        arguments = ["--cwd", str(self.workspace), "--timeout-seconds", str(timeout)]
        arguments += ["--stdin"] if stdin is not None else ["--prompt-file", str(self.prompt_file)]
        arguments += list(options)
        with mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker, "_supervise", side_effect=fake_supervise):
            return self.cli("invoke", *arguments, stdin=stdin)

    def binding_files(self):
        state = self.project / invoker.STATE_DIRECTORY
        return {str(path.relative_to(state)): path.read_bytes() for path in state.rglob("*") if path.is_file()}

    def _write_worker(self):
        # The fixture is code under test, not downloaded data. Only its explicit
        # absolute helper is imported; -I prevents fixture-directory shadowing.
        self.worker_script.write_text(textwrap.dedent(f'''\
            import importlib.util
            import json
            import os
            from pathlib import Path
            import signal
            import subprocess
            import sys
            import time
            import types

            spec = importlib.util.spec_from_file_location("helper", {str(HELPER)!r})
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            helper._require_sdk = lambda: None
            mode = os.environ.get("FAKE_MODE", "completed")
            capture = Path(os.environ["FAKE_CAPTURE"])
            calls = {{"init": 0, "enter": 0, "run": 0, "exit": 0, "close": 0}}
            record = {{"calls": calls}}
            def save():
                capture.write_text(json.dumps(record), encoding="utf-8")
            def noisy():
                print("FAKE_SENSITIVE stdout", flush=True)
                print("FAKE_SENSITIVE stderr", file=sys.stderr, flush=True)
                os.write(1, b"FAKE_SENSITIVE native stdout\\n")
                os.write(2, b"FAKE_SENSITIVE native stderr\\n")
            class Harness:
                def __init__(self, **options):
                    calls["init"] += 1
                    record["options"] = options
                    record["patches_is_tuple"] = isinstance(options["patches"], tuple)
                    record["worker_cwd"] = os.getcwd()
                    record["temp"] = os.environ.get("TMPDIR")
                    record["argv"] = sys.argv
                    save()
                    noisy()
                    if mode == "init-error":
                        raise RuntimeError("FAKE_SENSITIVE constructor failure")
                def __enter__(self):
                    calls["enter"] += 1
                    save()
                    noisy()
                    if mode == "enter-error":
                        raise RuntimeError("FAKE_SENSITIVE partially started runtime")
                    return self
                def run(self, prompt, **kwargs):
                    calls["run"] += 1
                    record["prompt"] = prompt
                    record["run_kwargs"] = kwargs
                    save()
                    noisy()
                    if mode == "run-error":
                        raise RuntimeError("FAKE_SENSITIVE authentication and raw stderr")
                    if mode == "rpc-timeout":
                        raise TimeoutError("FAKE_SENSITIVE RPC diagnostics")
                    if mode in ("hang", "child-hang", "orphan-child"):
                        if mode != "hang":
                            child = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            record["owned_child_pid"] = child.pid
                            save()
                        if mode != "orphan-child":
                            time.sleep(30)
                    session = kwargs.get("session_id", "session-fake-001")
                    reason = mode if mode in ("error", "max-tokens") else "completed"
                    if mode == "unknown-finish":
                        reason = "FAKE_SENSITIVE_UNKNOWN_REASON"
                    if mode == "missing-finish":
                        reason = None
                    return types.SimpleNamespace(session_id=session, finish_reason=reason,
                                                 final_response="Mock final response 中文.",
                                                 events=[{{"FAKE_SENSITIVE": "event"}}],
                                                 notifications=[{{"FAKE_SENSITIVE": "notification"}}])
                def __exit__(self, exc_type, exc, tb):
                    calls["exit"] += 1
                    save()
                    noisy()
                    self.close()
                    if mode == "exit-error":
                        raise RuntimeError("FAKE_SENSITIVE teardown failure")
                def close(self):
                    calls["close"] += 1
                    save()
            if mode == "import-error":
                def failed_import(name):
                    noisy()
                    raise ImportError("FAKE_SENSITIVE import error")
                helper.importlib.import_module = failed_import
            else:
                sys.modules["deepseek_harness"] = types.SimpleNamespace(DeepSeekHarness=Harness)
            raise SystemExit(helper.main(["_worker"]))
        '''), encoding="utf-8")


class InvocationTests(FakeSdkFixture):
    def test_completed_result_exact_surface_sdk_paths_and_new_session(self):
        code, result = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual({"session_id", "finish_reason", "final_response"}, set(result))
        self.assertEqual("completed", result["finish_reason"])
        self.assertEqual("Mock final response 中文.", result["final_response"])
        record = json.loads(self.capture.read_text(encoding="utf-8"))
        options = record["options"]
        self.assertEqual(str(self.home), options["dsh_home"])
        self.assertEqual("sdk", options["profile"])
        self.assertEqual(str(self.workspace), options["cwd"])
        self.assertTrue(record["patches_is_tuple"])
        self.assertEqual([str(invoker.PRIVACY_PATCH)], options["patches"])
        self.assertEqual({}, record["run_kwargs"])
        self.assertEqual(self.prompt_file.read_bytes().decode("utf-8"), record["prompt"])
        self.assertEqual(1, record["calls"]["run"])
        self.assertEqual(1, record["calls"]["exit"])
        self.assertEqual(1, record["calls"]["close"])
        self.assertEqual(str(self.project / invoker.STATE_DIRECTORY), record["worker_cwd"])
        self.assertEqual(str(self.project / invoker.STATE_DIRECTORY / "tmp"), record["temp"])
        self.assertNotIn(record["prompt"], json.dumps(record["argv"]))
        files = self.binding_files()
        self.assertEqual({"binding.json"}, set(files))
        self.assertNotIn(b"Mock final", files["binding.json"])
        self.assertNotIn(b"Review the mock", files["binding.json"])

    def test_stdin_prompt_and_unsupported_cross_process_session_continuity(self):
        code, result = self.invoke("--session-id", "intentional-session:42", stdin=io.StringIO("Follow-up prompt 中文.\n"))
        self.assertEqual(3, code, result)
        self.assertEqual("UNSUPPORTED_CONTINUATION", result["error"]["code"])
        self.assertFalse(self.capture.exists(), "unsupported restore must not start a fake or real SDK")
        # UTF-8 stdin remains supported for new independent tasks.
        code, result = self.invoke(stdin=io.StringIO("Independent prompt 中文.\n"))
        self.assertEqual(0, code, result)
        record = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual("Independent prompt 中文.\n", record["prompt"])
        self.assertEqual({}, record["run_kwargs"])

    def test_binary_stdin_uses_utf8_not_platform_locale_codec(self):
        prompt = "Piped UTF-8 中文.\n"
        # A cp1252-wrapped Windows stdin must not mojibake UTF-8 pipeline bytes.
        with io.TextIOWrapper(io.BytesIO(prompt.encode("utf-8")), encoding="cp1252") as source:
            code, result = self.invoke(stdin=source)
        self.assertEqual(0, code, result)
        self.assertEqual(prompt, json.loads(self.capture.read_text(encoding="utf-8"))["prompt"])

    def test_non_utf8_binary_stdin_fails_without_sdk_start(self):
        with io.TextIOWrapper(io.BytesIO(b"\xffbad"), encoding="cp1252") as source:
            code, result = self.invoke(stdin=source)
        self.assertNotEqual(0, code)
        self.assertEqual("INVALID_PROMPT", result["error"]["code"])
        self.assertFalse(self.capture.exists())

    def test_relative_prompt_file_is_resolved_against_explicit_task_cwd(self):
        with mock.patch.object(invoker, "_require_sdk"), \
                mock.patch.object(invoker, "_supervise", return_value=({"session_id": "s", "finish_reason": "completed", "final_response": "ok"}, 0)) as worker:
            result = self.cli_ok("invoke", "--cwd", self.workspace, "--prompt-file", self.prompt_file.name)
        self.assertEqual("ok", result["final_response"])
        self.assertEqual(self.prompt_file.read_bytes().decode("utf-8"), worker.call_args.args[1]["prompt"])

    def test_prompt_file_preserves_utf8_bytes_for_lf_and_crlf(self):
        for ending in ("\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                prompt = "Mock prompt 中文." + ending
                self.prompt_file.write_bytes(prompt.encode("utf-8"))
                code, result = self.invoke()
                self.assertEqual(0, code, result)
                self.assertEqual(prompt, json.loads(self.capture.read_text(encoding="utf-8"))["prompt"])
                with mock.patch.object(invoker, "_require_sdk"), \
                        mock.patch.object(invoker, "_supervise", return_value=({"session_id": "s", "finish_reason": "completed", "final_response": "ok"}, 0)) as worker:
                    self.cli_ok("invoke", "--cwd", self.workspace, "--prompt-file", self.prompt_file.name)
                self.assertEqual(prompt, worker.call_args.args[1]["prompt"])

    def test_model_rpc_options_passed_without_api_key_or_approval_bypass(self):
        code, result = self.invoke("--provider", "mock-provider", "--model", "mock-model", "--max-tokens", "128",
                                   "--request-timeout-seconds", "2.5")
        self.assertEqual(0, code, result)
        options = json.loads(self.capture.read_text(encoding="utf-8"))["options"]
        self.assertEqual(("mock-provider", "mock-model", 128, 2.5),
                         (options["provider"], options["model"], options["max_tokens"], options["request_timeout_seconds"]))
        self.assertNotIn("api_key", options)
        self.assertNotIn("base_url", options)
        self.assertNotIn("answerer", options)
        self.assertEqual("sdk", options["profile"])

    def test_privacy_patch_is_last_after_every_absolute_user_patch(self):
        first = self.base / "first patch.yml"
        second = self.base / "second patch.yml"
        first.write_text("- id: session-log-deepseek\n  disabled: false\n", encoding="utf-8")
        second.write_text("- id: session-log-deepseek\n  disabled: false\n", encoding="utf-8")
        originals = {path: path.read_bytes() for path in (first, second, invoker.PRIVACY_PATCH)}
        code, result = self.invoke("--patch", str(first), "--patch", str(invoker.PRIVACY_PATCH), "--patch", str(second))
        self.assertEqual(0, code, result)
        record = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertTrue(record["patches_is_tuple"])
        self.assertEqual([str(first), str(second), str(invoker.PRIVACY_PATCH)], record["options"]["patches"])
        self.assertEqual(originals, {path: path.read_bytes() for path in originals})
        self.assertFalse(self.home.exists(), "Fake SDK must not initialize a real profile")

    def test_changed_or_missing_privacy_patch_fails_before_worker(self):
        invalid = self.base / "invalid privacy.yml"
        for data in (b"disabled: false\n", b"- id: session-log-deepseek\n  disabled: true\n"):
            invalid.write_bytes(data)
            with mock.patch.object(invoker, "PRIVACY_PATCH", invalid), mock.patch.object(invoker, "_supervise") as worker:
                code, result = self.invoke()
            self.assertNotEqual(0, code)
            self.assertEqual("INVALID_PRIVACY_PATCH", result["error"]["code"])
            worker.assert_not_called()
        with mock.patch.object(invoker, "PRIVACY_PATCH", self.base / "missing.yml"):
            code, result = self.invoke()
        self.assertNotEqual(0, code)
        self.assertEqual("INVALID_PRIVACY_PATCH", result["error"]["code"])
        self.assertFalse(self.capture.exists())

    def test_privacy_asset_matches_release_row_identity_not_unknown_setting(self):
        self.assertEqual(b"- id: session-log-deepseek\n  name: '@deepseek-ai/dsh-session-log-deepseek'\n  disabled: true\n",
                         invoker.PRIVACY_PATCH.read_bytes().replace(b"\r\n", b"\n"))

    def test_non_completed_finish_reasons_return_nonzero_with_plain_three_fields(self):
        for reason in ("error", "max-tokens"):
            with self.subTest(reason=reason):
                code, result = self.invoke(fake_mode=reason)
                self.assertEqual(4, code, result)
                self.assertEqual({"session_id", "finish_reason", "final_response"}, set(result))
                self.assertEqual(reason, result["finish_reason"])
                self.assertEqual(1, json.loads(self.capture.read_text(encoding="utf-8"))["calls"]["run"])

    def test_missing_unknown_finish_reason_never_claims_success_or_echoes_events(self):
        for mode in ("missing-finish", "unknown-finish"):
            with self.subTest(mode=mode):
                code, result = self.invoke(fake_mode=mode)
                self.assertNotEqual(0, code)
                self.assertEqual("INVALID_SDK_RESULT", result["error"]["code"])

    def test_sdk_import_start_run_shutdown_errors_are_sanitized_without_retry(self):
        for mode, error in (("import-error", "SDK_IMPORT_FAILED"), ("init-error", "SDK_FAILURE"),
                            ("enter-error", "SDK_FAILURE"), ("run-error", "SDK_FAILURE"), ("exit-error", "SDK_FAILURE"),
                            ("rpc-timeout", "SDK_REQUEST_TIMEOUT")):
            with self.subTest(mode=mode):
                self.capture.unlink(missing_ok=True)
                code, result = self.invoke(fake_mode=mode)
                self.assertNotEqual(0, code)
                self.assertEqual(error, result["error"]["code"])
                if self.capture.exists():
                    calls = json.loads(self.capture.read_text(encoding="utf-8"))["calls"]
                    self.assertEqual(1, calls["init"])
                    self.assertLessEqual(calls["run"], 1)
                    if mode in ("run-error", "rpc-timeout"):
                        self.assertGreaterEqual(calls["close"], 1)

    def test_failed_enter_still_closes_partially_started_sdk(self):
        code, result = self.invoke(fake_mode="enter-error")
        self.assertNotEqual(0, code)
        self.assertEqual("SDK_FAILURE", result["error"]["code"])
        calls = json.loads(self.capture.read_text(encoding="utf-8"))["calls"]
        self.assertEqual(1, calls["enter"])
        self.assertEqual(0, calls["exit"])
        self.assertGreaterEqual(calls["close"], 1)

    def test_absent_or_wrong_version_sdk_never_spawns_a_worker_or_fallback(self):
        for version in (None, "9.9.9"):
            with self.subTest(version=version), mock.patch.object(invoker, "_sdk_metadata", return_value={"ready": False}), \
                    mock.patch.object(invoker, "_supervise") as worker:
                code, result = self.cli("invoke", "--cwd", self.workspace, "--prompt-file", self.prompt_file)
            self.assertNotEqual(0, code)
            self.assertEqual("SDK_NOT_READY", result["error"]["code"])
            worker.assert_not_called()
        self.assertFalse(self.capture.exists())

    def test_no_binding_or_stale_custom_binding_prevents_worker_start(self):
        binding = self.project / invoker.STATE_DIRECTORY / invoker.BINDING_FILENAME
        saved = binding.read_bytes()
        binding.unlink()
        code, result = self.invoke()
        self.assertNotEqual(0, code)
        self.assertEqual("BINDING_REQUIRED", result["error"]["code"])
        binding.write_text(json.dumps({"schema_version": 1, "home": str(self.home), "profile": "missing-custom"}), encoding="utf-8")
        code, result = self.invoke()
        self.assertNotEqual(0, code)
        self.assertEqual("MISSING_CUSTOM_PROFILE", result["error"]["code"])
        self.assertFalse(self.capture.exists())
        binding.write_bytes(saved)

    def test_invalid_prompt_paths_empty_non_utf8_oversized_and_nul_prevent_run(self):
        invalid = self.workspace / "invalid.txt"
        cases = (b"", b" \n\t", b"\xff\xfe", b"prompt\0", b"a" * (invoker.MAX_PROMPT_BYTES + 1))
        with mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker, "_supervise") as worker:
            for raw in cases:
                with self.subTest(size=len(raw)):
                    invalid.write_bytes(raw)
                    code, result = self.cli("invoke", "--cwd", self.workspace, "--prompt-file", invalid)
                    self.assertNotEqual(0, code)
                    self.assertEqual("INVALID_PROMPT", result["error"]["code"])
            code, result = self.cli("invoke", "--cwd", self.workspace, "--prompt-file", self.workspace / "absent")
            self.assertEqual("INVALID_PROMPT", result["error"]["code"])
            worker.assert_not_called()

    def test_stdin_tty_is_rejected_without_reading_or_starting_worker(self):
        terminal = mock.Mock()
        terminal.isatty.return_value = True
        terminal.read.side_effect = AssertionError("Interactive stdin must not be read")
        with mock.patch.object(invoker, "_require_sdk"), mock.patch.object(invoker, "_supervise") as worker:
            code, result = self.cli("invoke", "--cwd", self.workspace, "--stdin", stdin=terminal)
        self.assertNotEqual(0, code)
        self.assertEqual("INVALID_PROMPT", result["error"]["code"])
        terminal.read.assert_not_called()
        worker.assert_not_called()

    def test_invalid_session_and_timeout_values_are_fixed_errors(self):
        for session in ("../path", "a/b", "a\\b", "", "..", "secret\nvalue"):
            with self.subTest(session=repr(session)):
                code, result = self.invoke("--session-id", session)
                self.assertNotEqual(0, code)
                self.assertEqual("INVALID_SESSION_ID", result["error"]["code"])
        for option in ("--timeout-seconds", "--request-timeout-seconds", "--max-tokens"):
            for value in ("0", "-1", "nan", "inf", "FAKE_SENSITIVE"):
                with self.subTest(option=option, value=value):
                    code, result = self.invoke(option, value)
                    self.assertNotEqual(0, code)
                    self.assertEqual("INVALID_ARGUMENTS", result["error"]["code"])
        self.assertFalse(self.capture.exists())

    def test_project_temp_symlink_is_rejected_before_worker_or_outside_writes(self):
        state = self.project / invoker.STATE_DIRECTORY
        outside = self.base / "outside temp"
        outside.mkdir()
        try:
            (state / "tmp").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        code, result = self.invoke()
        self.assertNotEqual(0, code)
        self.assertEqual("UNSAFE_STATE_PATH", result["error"]["code"])
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse(self.capture.exists())


class WorkerOwnershipTests(FakeSdkFixture):
    def test_windows_exit_probe_is_passive_and_fails_closed(self):
        import ctypes

        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 0x123456789
        kernel.CloseHandle.return_value = True
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=5, create=True) as last_error, \
                mock.patch.object(ctypes, "WinError", side_effect=lambda code: OSError(code, "fixture Windows error"), create=True), \
                mock.patch.object(os, "kill", side_effect=AssertionError("exit probe must not signal")):
            kernel.WaitForSingleObject.return_value = 0x102
            with mock.patch.object(os, "name", "nt"):
                with self.assertRaises(AssertionError):
                    self.assert_pid_stopped(1234)
            kernel.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
            kernel.WaitForSingleObject.assert_called_once_with(0x123456789, 4000)
            kernel.CloseHandle.assert_called_once_with(0x123456789)
            # WAIT_OBJECT_0, not a PID lookup alone, proves the held process exited.
            kernel.WaitForSingleObject.return_value = 0
            with mock.patch.object(os, "name", "nt"):
                self.assert_pid_stopped(1234)
            kernel.WaitForSingleObject.return_value = 0xFFFFFFFF
            with self.assertRaises(OSError):
                _windows_pid_stopped(1234)
            self.assertEqual(kernel.CloseHandle.call_count, 3, "failed wait leaked a handle")
            kernel.OpenProcess.return_value = None
            last_error.return_value = 87
            self.assertTrue(_windows_pid_stopped(1234), "missing PID was not treated as stopped")
            self.assertEqual(kernel.WaitForSingleObject.call_count, 3)
            self.assertEqual(kernel.CloseHandle.call_count, 3)
            last_error.return_value = 5
            with self.assertRaises(OSError):
                _windows_pid_stopped(1234)
            kernel.OpenProcess.return_value = 0x123456789
            kernel.WaitForSingleObject.return_value = 0
            kernel.CloseHandle.return_value = False
            with self.assertRaises(OSError):
                _windows_pid_stopped(1234)
            kernel.TerminateProcess.assert_not_called()
            kernel.GenerateConsoleCtrlEvent.assert_not_called()

    def assert_pid_stopped(self, pid):
        if os.name == "nt":
            self.assertTrue(_windows_pid_stopped(pid), f"Owned fixture child {pid} is still running")
            return
        # A killed process can remain a zombie until its platform parent reaps it.
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if os.name == "posix":
                status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8",
                                        timeout=3, check=False).stdout.strip()
                if not status or status.startswith("Z"):
                    return
            time.sleep(0.05)
        self.fail(f"Owned fixture child {pid} is still running")

    def test_actual_deadline_not_merely_sdk_request_timeout(self):
        start = time.monotonic()
        code, result = self.invoke(fake_mode="hang", timeout=0.5)
        self.assertEqual(124, code, result)
        self.assertEqual("INVOCATION_DEADLINE", result["error"]["code"])
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(1, json.loads(self.capture.read_text(encoding="utf-8"))["calls"]["run"])

    def test_deadline_kills_only_owned_descendant_tree(self):
        sibling = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if os.name == "nt":
                self.assertFalse(_windows_pid_stopped(sibling.pid, timeout_ms=0))
                self.assertIsNone(sibling.poll(), "Passive Windows query must not terminate a live process")
            code, result = self.invoke(fake_mode="child-hang", timeout=0.7)
            self.assertEqual(124, code, result)
            self.assertIsNone(sibling.poll(), "Cleanup must not select processes by executable name")
            child = json.loads(self.capture.read_text(encoding="utf-8"))["owned_child_pid"]
            self.assert_pid_stopped(child)
        finally:
            sibling.kill()
            sibling.wait(timeout=5)

    def test_success_also_reaps_owned_child_left_by_sdk(self):
        code, result = self.invoke(fake_mode="orphan-child")
        self.assertEqual(0, code, result)
        child = json.loads(self.capture.read_text(encoding="utf-8"))["owned_child_pid"]
        self.assert_pid_stopped(child)

    def test_arbitrary_worker_stdout_and_stderr_are_not_returned(self):
        script = self.base / "malformed worker.py"
        script.write_text("import sys\nprint('FAKE_SENSITIVE raw stdout')\nprint('FAKE_SENSITIVE raw stderr', file=sys.stderr)\n", encoding="utf-8")
        with self.assertRaises(invoker.InvokerError) as error:
            self.real_supervise([sys.executable, "-I", "-B", str(script)], {}, cwd=self.base,
                                environment={}, timeout=3)
        self.assertEqual("WORKER_FAILURE", error.exception.code)
        self.assertNotIn("FAKE_SENSITIVE", str(error.exception))

    def test_worker_error_message_is_recreated_not_trusted(self):
        script = self.base / "untrusted message worker.py"
        value = {"ok": False, "action": "invoke", "error": {"code": "SDK_FAILURE", "message": "FAKE_SENSITIVE message"}}
        script.write_text(f"import sys\nprint({json.dumps(value)!r})\nsys.exit(5)\n", encoding="utf-8")
        with self.assertRaises(invoker.InvokerError) as error:
            self.real_supervise([sys.executable, "-I", "-B", str(script)], {}, cwd=self.base,
                                environment={}, timeout=3)
        self.assertEqual("SDK_FAILURE", error.exception.code)
        self.assertNotIn("FAKE_SENSITIVE", str(error.exception))

    def test_sdk_version_check_requires_both_exact_published_distributions(self):
        for versions in (("0.1.5rc1", "0.1.5rc1"), ("0.1.5rc1", "0.1.5"), ("0.1.5rc1", None)):
            def version(name):
                item = versions[0 if name == "deepseek-harness-sdk" else 1]
                if item is None:
                    raise invoker.importlib.metadata.PackageNotFoundError(name)
                return item
            with mock.patch.object(invoker.importlib.metadata, "version", side_effect=version):
                self.assertEqual(versions == ("0.1.5rc1", "0.1.5rc1"), invoker._sdk_metadata()["ready"])


if __name__ == "__main__":
    unittest.main()
