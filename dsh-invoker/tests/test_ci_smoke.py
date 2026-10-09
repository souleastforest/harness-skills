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
        scratch = ci.CHECKOUT / ci.STATE_NAME / "dev-tests"
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
