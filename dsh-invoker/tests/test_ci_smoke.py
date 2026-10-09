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

    def test_native_initialize_categories_withhold_private_sdk_details(self):
        # No SDK import/process/provider request: simulate the installed error's
        # public fields, including the private data/stderr initialize can append.
        error_type = type("JsonRpcError", (RuntimeError,), {})
        signatures = (
            ("loader fibers failed", "loader-settlement"),
            ("initialize reasoningEffort must be a non-empty string", "initialize-parameters"),
            ("initialize maxTokens must be a positive safe integer", "initialize-parameters"),
            ('no adapter registered for provider "deepseek-official"', "model-resolution"),
            ('adapter returned invalid exact model metadata for provider "deepseek-official" model "ci-mock-model"', "model-resolution"),
            ('adapter returned invalid context metadata for provider "deepseek-official" model "ci-mock-model"', "model-resolution"),
        )
        for stage in ("initialize", "harness-enter"):
            for message, category in signatures:
                with self.subTest(stage=stage, category=category):
                    error = error_type(SECRET)
                    error.code = -32603
                    error.message = message + "\n" + SECRET
                    error.data = {"private": SECRET, "path": SECRET}
                    envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": stage, "private": SECRET})
                    self.assertEqual(envelope, {
                        "fixture_failed": True, "error_type": "JsonRpcError", "fixture_stage": stage,
                        "failure_category": category, "rpc_code": -32603,
                    })
                    self.assertNotIn(SECRET, json.dumps(envelope))
                    result = self.owned_result("", json.dumps({**envelope, "message": SECRET, "data": error.data}), returncode=1)
                    def command_failure(case):
                        case.assert_owned_success(result, stage="lifecycle")

                    diagnostic = ci.run_suite(self.native_suite(command_failure), "native")
                    self.assertFalse(diagnostic["ok"])
                    detail = diagnostic["native_failures"][0]
                    self.assertEqual(detail["failure_category"], category)
                    self.assertEqual(detail["rpc_code"], -32603)
                    self.assertEqual(detail["fixture_stage"], stage)
                    self.assertNotIn(SECRET, json.dumps(diagnostic))
                    public = self.command_failure(self.owned_result(json.dumps(diagnostic), SECRET), lane="native")
                    self.assertIn('"failure_category": "' + category + '"', public)
                    self.assertIn('"rpc_code": -32603', public)
                    for field in ("message", "data", "private", "path"):
                        self.assertNotIn('"' + field + '"', public)

    def test_native_initialize_unknowns_do_not_imply_a_failure_cause(self):
        error_type = type("JsonRpcError", (RuntimeError,), {})
        for code, stage, message in (
            (-32603, "initialize", SECRET),
            (-32603, "initialize", SECRET + "\nloader fibers failed"),
            (-32603, "initialize", "loader fibers failed " + SECRET),
            (-32603, "initialize", SECRET + "x" * ci.MAX_DIAGNOSTIC),
            (-32603, "initialize", ["loader fibers failed"]),
            (-32603, "initialize", {"message": "loader fibers failed"}),
            (-32601, "initialize", "loader fibers failed"),
            (-32603, "restore-request", "loader fibers failed"),
            (-32603, "runtime-resolve", "initialize maxTokens must be a positive safe integer"),
            (-32603, "initialize", 'no adapter registered for provider "' + SECRET + '"'),
            (-32603, "initialize", 'adapter returned invalid context metadata for provider "deepseek-official" model "' + SECRET + '"'),
        ):
            with self.subTest(code=code, stage=stage, kind=type(message).__name__):
                error = error_type(SECRET)
                error.code, error.message, error.data = code, message, {"private": SECRET}
                envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": stage})
                self.assertEqual(envelope["failure_category"], "unknown")
                self.assertEqual(envelope["rpc_code"], code)
                self.assertNotIn(SECRET, json.dumps(envelope))
        error = TimeoutError(SECRET + "\nloader fibers failed")
        envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": "harness-enter"})
        self.assertEqual(envelope, {
            "fixture_failed": True, "error_type": "TimeoutError", "fixture_stage": "harness-enter",
            "failure_category": "unknown",
        })
        self.assertNotIn(SECRET, json.dumps(envelope))

    def test_native_rpc_codes_and_categories_are_strictly_allowlisted(self):
        error_type = type("JsonRpcError", (RuntimeError,), {})
        for code in (-32601, -32603, -32700, -32602, 5, True, -2**31 - 1, 2**32, SECRET, [], {}):
            with self.subTest(kind=type(code).__name__):
                error = error_type(SECRET)
                error.code, error.message = code, "loader fibers failed"
                envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": "initialize"})
                allowed = type(code) is int and code in ci._support.RPC_ERROR_CODES
                self.assertEqual("rpc_code" in envelope, allowed)
                if allowed:
                    self.assertEqual(envelope["rpc_code"], code)
                else:
                    self.assertEqual(envelope["failure_category"], "unknown")
                self.assertNotIn(SECRET, json.dumps(envelope))
        for code in (-32601, -32603):
            for category in ci._support.FIXTURE_FAILURE_CATEGORIES:
                envelope = {"fixture_failed": True, "rpc_code": code, "failure_category": category, "private": SECRET}
                self.assertEqual(ci._support.command_diagnostic(1, False, "", json.dumps(envelope)), {
                    "returncode": 1, "timed_out": False, "rpc_code": code, "failure_category": category,
                })
        for code, category in ((True, SECRET), (-32602, []), (SECRET, {}), ([], 1), ({}, None)):
            envelope = {"fixture_failed": True, "rpc_code": code, "failure_category": category}
            self.assertEqual(ci._support.command_diagnostic(1, False, "", json.dumps(envelope)), {
                "returncode": 1, "timed_out": False,
            })

    def test_loader_wrapper_fixed_families_and_variable_paths_are_safe(self):
        error_type = type("JsonRpcError", (RuntimeError,), {})
        module = "@deepseek-ai/dsh-subprocess-local"
        signatures = [
            (signature, "native-addon", token)
            for signature, token in ci._support._LOADER_NATIVE_SIGNATURES.items()
        ] + [
            ("Module did not self-register: '" + SECRET + "'.", "native-addon", "MODULE_DID_NOT_SELF_REGISTER"),
            ("spawn pwsh ENOENT", "shell", "PWSH_NOT_FOUND"),
            ("pwsh-local: timeoutMs must be a positive finite number", "shell", "PWSH_INVALID_CONFIG"),
            ("Cannot find module '" + SECRET + "'", "import", "MODULE_NOT_FOUND"),
            ("Cannot find package '" + SECRET + "' imported from C:\\private\\runtime.js", "import", "ERR_MODULE_NOT_FOUND"),
            ("Only URLs with a scheme in: file, data, and node are supported by the default ESM loader. "
             "On Windows, absolute paths must be valid file:// URLs. Received protocol 'd:'", "import", "ERR_UNSUPPORTED_ESM_URL_SCHEME"),
        ]
        for path in ("C:\\private home 空格\\" + SECRET + ".node", "D:/different workspace/" + SECRET + ".node"):
            for code, signature in ci._support._LOADER_OS_SIGNATURES.items():
                signatures.append((code + ": " + signature + ", open '" + path + "'", "os", code))
        for loader_stage in ci._support.LOADER_FAILURE_STAGES:
            for cause, family, token in signatures:
                with self.subTest(loader_stage=loader_stage, family=family, token=token):
                    error = error_type(SECRET)
                    error.code = -32603
                    error.message = "failed to " + loader_stage + " loader entry arbitrary.secret-entry:17 (" + module + "): " + cause
                    # SDK initialize() appends raw stderr after the wire message;
                    # no later-line signature or data may change this classification.
                    error.message += "\nstderr tail:\n" + SECRET + "\nloader fibers failed"
                    if family == "native-addon" and token != "MODULE_DID_NOT_SELF_REGISTER":
                        error.message = error.message.replace("\nstderr tail:", "\r\nstderr tail:", 1)
                    error.data = {"private": SECRET}
                    envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": "initialize"})
                    self.assertEqual(envelope, {
                        "fixture_failed": True, "error_type": "JsonRpcError", "fixture_stage": "initialize",
                        "rpc_code": -32603, "failure_category": "loader-settlement",
                        "loader_stage": loader_stage, "loader_module": module, "loader_cause": family, "loader_token": token,
                    })
                    self.assertNotIn(SECRET, json.dumps(envelope))
                    self.assertNotIn("arbitrary.secret-entry", json.dumps(envelope))
                    result = self.owned_result("", json.dumps({**envelope, "data": error.data, "message": SECRET}), returncode=1)
                    def command_failure(case):
                        case.assert_owned_success(result, stage="lifecycle")

                    diagnostic = ci.run_suite(self.native_suite(command_failure), "native")
                    detail = diagnostic["native_failures"][0]
                    for field in ci._support.LOADER_DIAGNOSTIC_FIELDS:
                        self.assertEqual(detail[field], envelope[field])
                    public = self.command_failure(self.owned_result(json.dumps(diagnostic), SECRET), lane="native")
                    self.assertIn('"loader_token": "' + token + '"', public)
                    self.assertNotIn(SECRET, public)
                    self.assertNotIn("arbitrary.secret-entry", public)

    def test_loader_wrapper_rejects_malformed_modules_and_multiline_spoofs(self):
        error_type = type("JsonRpcError", (RuntimeError,), {})
        prefix = "failed to apply loader entry private-entry (@deepseek-ai/dsh-subprocess-local): "
        for message in (
            SECRET + "\n" + prefix + "spawn pwsh ENOENT",
            "failed to apply loader entry private-entry\n(@deepseek-ai/dsh-subprocess-local): spawn pwsh ENOENT",
            "failed to apply loader entry private-entry\r (@deepseek-ai/dsh-subprocess-local): spawn pwsh ENOENT",
            "failed to apply loader entry private-entry\t (@deepseek-ai/dsh-subprocess-local): spawn pwsh ENOENT",
            prefix.replace("apply", "start", 1) + "spawn pwsh ENOENT",
            prefix.replace("private-entry", "entry id with spaces") + "spawn pwsh ENOENT",
            prefix.replace("private-entry", "x" * 129) + "spawn pwsh ENOENT",
            prefix.replace("@deepseek-ai/dsh-subprocess-local", "@private/" + SECRET) + "spawn pwsh ENOENT",
            prefix.replace("@deepseek-ai/dsh-subprocess-local", "@deepseek-ai/dsh-subprocess-local-spoof") + "spawn pwsh ENOENT",
            prefix.replace("): ", "):") + "spawn pwsh ENOENT",
            prefix + "spawn pwsh ENOENT\r" + SECRET,
            prefix + "x" * ci.MAX_DIAGNOSTIC,
        ):
            with self.subTest(kind=len(message)):
                error = error_type(SECRET)
                error.code, error.message, error.data = -32603, message, {"private": SECRET}
                envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": "harness-enter"})
                self.assertEqual(envelope, {
                    "fixture_failed": True, "error_type": "JsonRpcError", "fixture_stage": "harness-enter",
                    "rpc_code": -32603, "failure_category": "unknown",
                })
                self.assertNotIn(SECRET, json.dumps(envelope))
        # Exact wrapper, but unrecognized cause: keep a fixed unknown, never
        # scan stderr for a known token or infer a native cause from a mention.
        for cause in (
            SECRET, "unknown private error: ENOENT " + SECRET, "spawn pwsh ENOENT " + SECRET,
            "Cannot find module '" + SECRET + "' " + SECRET,
            "The specified module could not be found. " + SECRET,
            "ENOENT: no such file or directory, open 'C:/" + SECRET + "' " + SECRET,
        ):
            error = error_type(SECRET)
            error.code, error.message = -32603, prefix + cause + "\nThe specified module could not be found."
            envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": "initialize"})
            self.assertEqual(envelope["failure_category"], "loader-settlement")
            self.assertEqual(envelope["loader_cause"], "unknown")
            self.assertNotIn("loader_token", envelope)
            self.assertNotIn(SECRET, json.dumps(envelope))
        for stage, code in (("restore-request", -32603), ("initialize", -32601)):
            error = error_type(SECRET)
            error.code, error.message = code, prefix + "spawn pwsh ENOENT"
            envelope = ci._support.fixture_failure_diagnostic(error, {"fixture_stage": stage})
            self.assertEqual(envelope["failure_category"], "unknown")
            self.assertFalse(set(ci._support.LOADER_DIAGNOSTIC_FIELDS) & envelope.keys())

    def test_loader_wrapper_fields_are_validated_at_each_boundary(self):
        baseline = {
            "fixture_failed": True, "error_type": "JsonRpcError", "fixture_stage": "initialize", "rpc_code": -32603,
            "failure_category": "loader-settlement", "loader_stage": "apply",
            "loader_module": "@deepseek-ai/dsh-subprocess-local", "loader_cause": "os", "loader_token": "ENOENT",
        }
        result = self.owned_result("", json.dumps(baseline), returncode=1)
        def command_failure(case):
            case.assert_owned_success(result, stage="lifecycle")

        diagnostic = ci.run_suite(self.native_suite(command_failure), "native")
        detail = diagnostic["native_failures"][0]
        allowed = ci.allowed_test_ids("native")
        for field, invalid in (
            ("loader_stage", SECRET), ("loader_stage", []), ("loader_module", SECRET), ("loader_module", {}),
            ("loader_cause", SECRET), ("loader_cause", []), ("loader_token", SECRET), ("loader_token", {}),
            ("loader_cause", "unknown"), ("loader_cause", "import"), ("rpc_code", -32601),
            ("fixture_stage", "first-turn"), ("failure_category", "unknown"),
        ):
            with self.subTest(field=field, kind=type(invalid).__name__):
                envelope = {**baseline, field: invalid}
                command = ci._support.command_diagnostic(1, False, "", json.dumps(envelope))
                self.assertFalse(set(ci._support.LOADER_DIAGNOSTIC_FIELDS) & command.keys())
                self.assertIsNone(ci.native_failure_diagnostic({**detail, field: invalid}, allowed))
        for field in ci._support.LOADER_DIAGNOSTIC_FIELDS:
            envelope, incomplete_detail = dict(baseline), dict(detail)
            del envelope[field], incomplete_detail[field]
            self.assertFalse(set(ci._support.LOADER_DIAGNOSTIC_FIELDS) & ci._support.command_diagnostic(1, False, "", json.dumps(envelope)).keys())
            self.assertIsNone(ci.native_failure_diagnostic(incomplete_detail, allowed))
        unknown = {**baseline, "loader_cause": "unknown"}
        del unknown["loader_token"]
        command = ci._support.command_diagnostic(1, False, "", json.dumps(unknown))
        self.assertEqual(command["loader_cause"], "unknown")
        self.assertNotIn("loader_token", command)
        unknown_detail = {key: value for key, value in detail.items() if key != "loader_token"}
        unknown_detail["loader_cause"] = "unknown"
        self.assertIsNotNone(ci.native_failure_diagnostic(unknown_detail, allowed))

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
            ("failure_category", SECRET), ("failure_category", []), ("failure_category", {}),
            ("rpc_code", True), ("rpc_code", -32602), ("rpc_code", SECRET), ("rpc_code", []), ("rpc_code", {}),
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
