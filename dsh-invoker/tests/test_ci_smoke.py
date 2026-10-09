"""Offline CI-driver regressions: safe diagnostics and genuinely uv-free tools."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("dsh_ci_smoke_under_test", Path(__file__).with_name("ci_smoke.py"))
assert SPEC is not None and SPEC.loader is not None
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
SECRET = "sk-fake-ci-private-request-payload-never-print"


class CiSmokeTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("DSH_INVOKER_TEST_TMP") or os.environ.get("DSH_INVOKER_TEST_ROOT")
        scratch = Path(configured) if configured else ci.CHECKOUT / ci.STATE_NAME / "dev-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="ci-smoke-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def synthetic_suite(self, function, *, identifier=None):
        # This method name is from the source allowlist. Intentional failures are
        # constructed at runtime, never collected as real regression cases.
        name = self._testMethodName
        attributes = {"__module__": "test_ci_smoke", name: function}
        if identifier is not None:
            attributes["id"] = lambda self: identifier
        case = type("CiSmokeTests", (unittest.TestCase,), attributes)
        return unittest.TestSuite([case(name)])

    def owned_result(self, stdout="", stderr="", returncode=1):
        return ci._support.OwnedResult(returncode, stdout, stderr, False, 1)

    def command_failure(self, result, **options):
        with mock.patch.object(ci._support, "run_owned", return_value=result):
            with self.assertRaises(ci.VerificationError) as caught:
                ci.run_command(["not executed"], environment={}, cwd=self.root, timeout=1,
                               label="fixture command", **options)
        public = str(caught.exception)
        self.assertNotIn(SECRET, public)
        self.assertIn("raw tool/runtime diagnostics withheld", public)
        return public

    def test_failure_diagnostics_discard_private_output(self):
        def fail_with_private_data(case):
            print(SECRET)
            print(SECRET, file=sys.stderr)
            with case.subTest(request_payload=SECRET):
                case.fail(SECRET)
            raise RuntimeError(SECRET)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             mock.patch.object(unittest.TestResult, "_exc_info_to_string", side_effect=AssertionError("must not format errors")):
            diagnostic = ci.run_suite(self.synthetic_suite(fail_with_private_data), "unit")
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertFalse(diagnostic["ok"])
        self.assertEqual(diagnostic["counts"], {
            "tests": 1, "failures": 1, "errors": 1, "skipped": 0, "expected_failures": 0, "unexpected_successes": 0,
        })
        identifier = "test_ci_smoke.CiSmokeTests." + self._testMethodName
        self.assertEqual(diagnostic["failed_test_ids"], [identifier])
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        # Failed child stderr and even extra JSON fields must not be forwarded.
        child = self.owned_result(json.dumps({**diagnostic, "private": SECRET}), SECRET)
        public = self.command_failure(child, lane="unit", action="exec")
        self.assertIn(identifier, public)
        self.assertIn('"tests": 1', public)
        self.assertIn('"failures": 1', public)
        self.assertIn('"errors": 1', public)
        self.assertNotIn('"private"', public)

    def test_unlisted_control_and_oversized_ids_are_not_public(self):
        identifier = "test_ci_smoke.CiSmokeTests." + self._testMethodName
        for invalid in (identifier + "\n" + SECRET, identifier + "\x7f", identifier + "x" * 300,
                        "test_ci_smoke.CiSmokeTests.test_unknown_data", SECRET):
            with self.subTest(kind=len(invalid)):
                diagnostic = ci.run_suite(self.synthetic_suite(lambda case: case.fail(SECRET), identifier=invalid), "unit")
                self.assertEqual(diagnostic["counts"]["failures"], 1)
                self.assertEqual(diagnostic["failed_test_ids"], [])
                injected = {**diagnostic, "failed_test_ids": [invalid]}
                self.assertIsNone(ci.unittest_diagnostic(json.dumps(injected), "unit"))
                public = self.command_failure(self.owned_result(json.dumps(injected), SECRET), lane="unit", action="exec")
                self.assertNotIn("safe diagnostic=", public)

    def test_expected_and_unexpected_success_counts(self):
        @unittest.expectedFailure
        def expected_failure(case):
            case.fail(SECRET)

        diagnostic = ci.run_suite(self.synthetic_suite(expected_failure), "unit")
        self.assertTrue(diagnostic["ok"])
        self.assertEqual(diagnostic["counts"]["expected_failures"], 1)
        self.assertEqual(diagnostic["failed_test_ids"], [])
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        diagnostic = ci.run_suite(self.synthetic_suite(unittest.expectedFailure(lambda case: None)), "unit")
        self.assertFalse(diagnostic["ok"])
        self.assertEqual(diagnostic["counts"]["unexpected_successes"], 1)
        self.assertEqual(diagnostic["failed_test_ids"], ["test_ci_smoke.CiSmokeTests." + self._testMethodName])

    def test_native_missing_wheels_and_skips_still_fail(self):
        case = ci._support.NativeRuntimeIntegration
        for required, outcome in (("1", "errors"), ("0", "skipped")):
            with self.subTest(required=required), \
                 mock.patch.dict(os.environ, {"DSH_INVOKER_REQUIRE_RUNTIME": required}), \
                 mock.patch.object(ci._support.importlib.metadata, "version", side_effect=ci._support.importlib.metadata.PackageNotFoundError), \
                 mock.patch.object(case, "__module__", "test_runtime_integration"):
                diagnostic = ci.run_suite(unittest.TestLoader().loadTestsFromTestCase(case), "native")
            self.assertFalse(diagnostic["ok"])
            self.assertEqual(diagnostic["counts"]["tests"], 0)
            self.assertEqual(diagnostic["counts"][outcome], 1)
            self.assertEqual(diagnostic["failed_test_ids"], ["setUpClass (test_runtime_integration.NativeRuntimeIntegration)"])
            public = self.command_failure(self.owned_result(json.dumps(diagnostic), SECRET), lane="native", action="exec")
            self.assertIn("setUpClass (test_runtime_integration.NativeRuntimeIntegration)", public)
        name = "test_published_executable_initialize_shutdown_has_zero_model_requests"
        skipped_case = type("NativeRuntimeIntegration", (unittest.TestCase,), {
            "__module__": "test_runtime_integration", name: lambda case: case.skipTest(SECRET),
        })
        diagnostic = ci.run_suite(unittest.TestSuite([skipped_case(name)]), "native")
        self.assertEqual(diagnostic["counts"]["tests"], 1)
        self.assertEqual(diagnostic["counts"]["skipped"], 1)
        self.assertFalse(diagnostic["ok"], "a required native skip must fail even when a test ran")
        self.assertEqual(diagnostic["failed_test_ids"], ["test_runtime_integration.NativeRuntimeIntegration." + name])
        self.assertNotIn(SECRET, json.dumps(diagnostic))

    def test_discovery_and_class_errors_keep_source_identifiers(self):
        failed_import = unittest.loader._FailedTest("test_ci_smoke", ImportError(SECRET))
        diagnostic = ci.run_suite(unittest.TestSuite([failed_import]), "unit")
        self.assertEqual(diagnostic["counts"]["errors"], 1)
        self.assertEqual(diagnostic["failed_test_ids"], ["unittest.loader._FailedTest.test_ci_smoke"])
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        # Every normally discovered case has an exact source-defined allowlisted
        # ID, but discovery alone never executes the SDK/runtime integrations.
        def cases(suite):
            for item in suite:
                if isinstance(item, unittest.TestSuite):
                    yield from cases(item)
                else:
                    yield item
        for lane in ("unit", "native"):
            allowed = ci.allowed_test_ids(lane)
            discovered = list(cases(ci.suite_for(lane)))
            self.assertTrue(discovered)
            self.assertTrue(all(case.id() in allowed for case in discovered), "discovered ID is missing from source allowlist")

    def test_bootstrap_diagnostics_only_expose_allowed_codes_and_action(self):
        for field in ("error", "code"):
            envelope = {"ok": False, field: "uv_digest", "action": "setup", "message": SECRET, "request": SECRET}
            result = self.owned_result("", json.dumps(envelope))
            public = self.command_failure(result, action="setup")
            self.assertIn('"error": "uv_digest"', public)
            self.assertIn('"action": "setup"', public)
            self.assertNotIn('"message"', public)
            self.assertNotIn('"request"', public)
        envelope = {"ok": False, "error": "runtime_missing", "message": SECRET}
        public = self.command_failure(self.owned_result(json.dumps(envelope), ""), action="exec")
        self.assertIn('"error": "runtime_missing"', public)
        self.assertIn('"action": "exec"', public)

    def test_bootstrap_raw_or_unknown_errors_are_withheld(self):
        allowed = {"ok": False, "error": "uv_digest", "message": SECRET}
        rejected = (
            SECRET, SECRET + json.dumps(allowed), json.dumps(allowed) + "\n" + SECRET,
            json.dumps({**allowed, "error": SECRET}), json.dumps({**allowed, "ok": True}),
            json.dumps({**allowed, "action": "check"}), " " * ci.MAX_DIAGNOSTIC + json.dumps(allowed),
        )
        for text in rejected:
            with self.subTest(length=len(text)):
                public = self.command_failure(self.owned_result("", text), action="setup")
                self.assertNotIn("safe diagnostic=", public)

    def test_run_lane_emits_safe_failure_json(self):
        def fail_with_private_data(case):
            print(SECRET)
            case.fail(SECRET)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ci.sys, "prefix", str(self.root / ci.STATE_NAME / "venv")), \
             mock.patch.object(ci, "suite_for", return_value=self.synthetic_suite(fail_with_private_data)), \
             redirect_stdout(out), redirect_stderr(err):
            code = ci.run_lane("unit", self.root)
        self.assertEqual(code, 1)
        diagnostic = json.loads(out.getvalue())
        self.assertFalse(diagnostic["ok"])
        self.assertEqual(diagnostic["counts"]["failures"], 1)
        self.assertEqual(diagnostic["failed_test_ids"], ["test_ci_smoke.CiSmokeTests." + self._testMethodName])
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_successful_child_only_prints_validated_summary(self):
        diagnostic = ci.run_suite(self.synthetic_suite(lambda case: None), "unit")
        child = self.owned_result(json.dumps({**diagnostic, "private": SECRET}), SECRET, returncode=0)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ci._support, "run_owned", return_value=child), redirect_stdout(out), redirect_stderr(err):
            ci.run_tests(self.root, {}, "unit")
        self.assertEqual(json.loads(out.getvalue()), diagnostic)
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(SECRET, out.getvalue())

    def native_suite(self, function):
        name = "test_published_executable_initialize_shutdown_has_zero_model_requests"
        case = type("NativeRuntimeIntegration", (unittest.TestCase,), {
            "__module__": "test_runtime_integration", name: function,
            "run_command": ci._support.NativeRuntimeIntegration.run_command,
            "assert_owned_success": ci._support.NativeRuntimeIntegration.assert_owned_success,
        })
        return unittest.TestSuite([case(name)])

    def test_native_command_diagnostics_withhold_child_data(self):
        result = self.owned_result(
            json.dumps({"ok": False, "action": "invoke", "error": {"code": "SDK_FAILURE", "message": SECRET}}),
            json.dumps({"fixture_failed": True, "error_type": "RuntimeError", "private": SECRET}),
            returncode=5,
        )
        def command_failure(case):
            ci._support.NativeRuntimeIntegration.assert_owned_success(case, result, stage="lifecycle")

        with mock.patch.object(unittest.TestResult, "_exc_info_to_string", side_effect=AssertionError("must not format errors")):
            diagnostic = ci.run_suite(self.native_suite(command_failure), "native")
        self.assertEqual(diagnostic["counts"]["failures"], 1)
        self.assertEqual(diagnostic["counts"]["errors"], 0)
        detail = diagnostic["native_failures"][0]
        self.assertIn((detail["site"], detail["line"]), ci.NATIVE_SOURCE_SITES)
        self.assertEqual(detail, {
            "test_id": "test_runtime_integration.NativeRuntimeIntegration.test_published_executable_initialize_shutdown_has_zero_model_requests",
            "exception": "NativeCommandFailure", "site": "assert_owned_success", "line": detail["line"], "stage": "lifecycle",
            "returncode": 5, "timed_out": False, "helper_error": "SDK_FAILURE", "child_exception": "RuntimeError",
        })
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        public = self.command_failure(self.owned_result(json.dumps(diagnostic), SECRET), lane="native", action="exec")
        self.assertIn('"stage": "lifecycle"', public)
        self.assertIn('"returncode": 5', public)
        self.assertNotIn('"message"', public)
        self.assertNotIn('"private"', public)
        # Raw, mixed, oversized and unknown child diagnostics remain withheld.
        for stderr in (SECRET, SECRET + result.stderr, " " * ci.MAX_DIAGNOSTIC + result.stderr,
                       json.dumps({"fixture_failed": True, "error_type": SECRET})):
            bad = self.owned_result("", stderr, returncode=1)
            failure = ci._support.NativeCommandFailure("lifecycle", bad)
            detail_input = {**detail, **failure.ci_diagnostic}
            detail_input.pop("helper_error", None)
            detail_input.pop("child_exception", None)
            if "child_exception" in failure.ci_diagnostic:
                detail_input["child_exception"] = failure.ci_diagnostic["child_exception"]
                self.assertIsNone(ci.native_failure_diagnostic(detail_input, ci.allowed_test_ids("native")))
            else:
                safe = ci.native_failure_diagnostic(detail_input, ci.allowed_test_ids("native"))
                self.assertNotIn(SECRET, json.dumps(safe))
                self.assertNotIn("child_exception", safe)

    def test_native_assertion_sites_and_cleanup_errors_preserve_outcomes(self):
        # Use real checked-in methods as traceback sites; never publish the
        # exception string (which intentionally contains private data here).
        result = self.owned_result("", SECRET, returncode=0)
        def assertion_failure(case):
            ci._support.NativeRuntimeIntegration.assert_owned_success(case, result)

        diagnostic = ci.run_suite(self.native_suite(assertion_failure), "native")
        self.assertEqual(diagnostic["counts"]["failures"], 1)
        self.assertEqual(diagnostic["native_failures"][0]["site"], "assert_owned_success")
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        fixture = ci._support.RuntimeFixture(self.root / "synthetic native diagnostics")
        def cleanup_failure(case):
            ci._support.NativeRuntimeIntegration.configure(case, fixture, {})

        for exception, outcome in ((AssertionError(SECRET), "failures"), (PermissionError(SECRET), "errors")):
            with mock.patch.object(ci._support, "run_owned", side_effect=exception):
                diagnostic = ci.run_suite(self.native_suite(cleanup_failure), "native")
            self.assertEqual(diagnostic["counts"][outcome], 1)
            self.assertEqual(diagnostic["native_failures"][0]["site"], "run_command")
            self.assertEqual(diagnostic["native_failures"][0]["stage"], "helper-configure")
            self.assertEqual(diagnostic["native_failures"][0]["exception"], type(exception).__name__)
            self.assertNotIn(SECRET, json.dumps(diagnostic))

    def test_native_cleanup_failure_keeps_precleanup_exit_and_stage(self):
        # The command finished, but cleanup raises: keep the safe exit evidence
        # even though run_owned has no OwnedResult to return. No process starts.
        process = mock.Mock(returncode=5)
        process.communicate.return_value = (
            json.dumps({"ok": False, "action": "invoke", "error": {"code": "SDK_FAILURE", "message": SECRET}}).encode(),
            json.dumps({"fixture_failed": True, "error_type": "RuntimeError", "fixture_stage": "initialize", "private": SECRET}).encode(),
        )
        process.stdin = process.stdout = process.stderr = None
        tree, job = mock.Mock(), mock.Mock()
        for owner in (tree, job):
            owner.cleanup.side_effect = [AssertionError(SECRET), 1]
        fixture = ci._support.RuntimeFixture(self.root / "cleanup diagnostic")
        def fail_after_command(case):
            case.run_command(["not executed"], stage="lifecycle", cwd=fixture.workspace, env={}, timeout=1)

        with mock.patch.object(ci._support.subprocess, "Popen", return_value=process), \
             mock.patch.object(ci._support, "_PosixOwnedTree", return_value=tree), \
             mock.patch.object(ci._support, "_WindowsOwnedJob", return_value=job):
            diagnostic = ci.run_suite(self.native_suite(fail_after_command), "native")
        self.assertEqual(diagnostic["counts"]["failures"], 1)
        self.assertEqual(diagnostic["counts"]["errors"], 0)
        detail = diagnostic["native_failures"][0]
        self.assertEqual(detail["site"], "run_owned")
        self.assertEqual(detail["stage"], "lifecycle")
        self.assertEqual(detail["process_stage"], "cleanup")
        self.assertEqual(detail["returncode"], 5)
        self.assertIs(detail["timed_out"], False)
        self.assertEqual(detail["helper_error"], "SDK_FAILURE")
        self.assertEqual(detail["child_exception"], "RuntimeError")
        self.assertEqual(detail["fixture_stage"], "initialize")
        self.assertIn((detail["site"], detail["line"]), ci.NATIVE_SOURCE_SITES)
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        public = self.command_failure(self.owned_result(json.dumps(diagnostic), SECRET), lane="native")
        self.assertIn('"process_stage": "cleanup"', public)
        self.assertIn('"fixture_stage": "initialize"', public)

    def test_native_command_parser_rejects_raw_and_unknown_metadata(self):
        baseline = {"returncode": 1, "timed_out": False}
        for raw in (
            SECRET, SECRET + '{}', json.dumps({"fixture_failed": True, "error_type": SECRET, "fixture_stage": SECRET}),
            json.dumps({"fixture_failed": True, "error_type": [], "fixture_stage": {}}),
            " " * ci.MAX_DIAGNOSTIC + '{}', '[' * 2000 + ']' * 2000,
        ):
            with self.subTest(kind=len(raw)):
                self.assertEqual(ci._support.command_diagnostic(1, False, raw, raw), baseline)
        self.assertEqual(ci._support.command_diagnostic(1, False, b"\xff", b"\xff"), baseline)
        for code in (True, -2**31 - 1, 2**32, SECRET):
            self.assertEqual(ci._support.command_diagnostic(code, False), {"timed_out": False})
        for code in (-2**31, -9, 0, 5, 124, 2**32 - 1):
            self.assertEqual(ci._support.command_diagnostic(code, False), {"returncode": code, "timed_out": False})

    def test_native_diagnostics_reject_unknown_values(self):
        def command_failure(case):
            ci._support.NativeRuntimeIntegration.assert_owned_success(case, self.owned_result(), stage="lifecycle")

        diagnostic = ci.run_suite(self.native_suite(command_failure), "native")
        detail = diagnostic["native_failures"][0]
        allowed = ci.allowed_test_ids("native")
        self.assertIsNotNone(ci.unittest_diagnostic(json.dumps(diagnostic), "native"))
        for field, invalid in (
            ("test_id", SECRET), ("exception", SECRET), ("site", SECRET), ("stage", SECRET),
            ("helper_error", SECRET), ("child_exception", SECRET), ("exception", []), ("site", {}),
            ("stage", []), ("returncode", True), ("returncode", 2**32), ("timed_out", 1),
            ("line", True), ("line", -1), ("line", 10**9), ("process_stage", SECRET), ("process_stage", []),
            ("fixture_stage", SECRET), ("fixture_stage", {}),
        ):
            with self.subTest(field=field, kind=type(invalid).__name__):
                rejected = {**detail, field: invalid}
                self.assertIsNone(ci.native_failure_diagnostic(rejected, allowed))
                child = {**diagnostic, "native_failures": [rejected]}
                public = self.command_failure(self.owned_result(json.dumps(child), SECRET), lane="native")
                self.assertNotIn("safe diagnostic=", public)
        extra = {**detail, "private": SECRET, "traceback": SECRET, "locals": SECRET}
        safe = ci.native_failure_diagnostic(extra, allowed)
        self.assertEqual(safe, detail)
        self.assertNotIn(SECRET, json.dumps(safe))

    @unittest.skipIf(os.name == "nt", "POSIX system utility fixture; no Windows symlinks")
    def test_controlled_path_hashes_known_file_without_uv(self):
        environment = ci._support.isolated_environment(self.root / "environment")
        tools = self.root / "system tools 空格"
        controlled = ci.controlled_no_uv_path(tools, environment)
        environment["PATH"] = controlled
        self.assertEqual(controlled, str(tools))
        for name in ("uv", "uv.exe", "uv.cmd", "uv.bat", "python", "python3"):
            self.assertIsNone(shutil.which(name, path=controlled))
        ci.verify_absence_in_native_shell(environment, self.root)
        known = self.root / "known bytes 项目.txt"
        known.write_bytes(b"CI native SHA-256 regression\n")
        found_hash_tool = False
        for name, flags in (("sha256sum", ["--"]), ("shasum", ["-a", "256", "--"])):
            if shutil.which(name, path=controlled) is None:
                continue
            found_hash_tool = True
            result = ci.run_command([str(tools / name), *flags, str(known)], environment=environment,
                                    cwd=self.root, timeout=15, label="offline native hash proof")
            self.assertEqual(result.stdout.split()[0], ci.digest(known))
        self.assertTrue(found_hash_tool, "a native SHA-256 utility is required")
        self.assertFalse((self.root / ci.STATE_NAME).exists(), "hash/absence proofs must not install a runtime")


if __name__ == "__main__":
    unittest.main()
