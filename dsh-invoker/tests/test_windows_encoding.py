"""Offline bootstrap JSON regression with a strict Windows ANSI pipe codec.

The streams explicitly use cp1252 even on UTF-8 hosts, reproducing redirected
Windows output independently of locale or PYTHONUTF8 (ignored by Python -I).
No SDK/runtime, provider, installation or native Windows execution is required.
"""
from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_encoding_under_test", SKILL / "scripts/bootstrap_support.py")
SUPPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPPORT)


class BootstrapWindowsEncodingTests(unittest.TestCase):
    def setUp(self):
        scratch = SKILL.parent / ".dsh-invoker-profile/dev-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="windows-encoding-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve() / "项目 with spaces"
        self.state = self.project / ".dsh-invoker-profile"
        self.state.mkdir(parents=True)
        self.uv = self.project / "工具/uv.exe"

    def run_with_ansi_pipes(self, argv):
        output, error = io.BytesIO(), io.BytesIO()
        with io.TextIOWrapper(output, encoding="cp1252", errors="strict") as stdout, \
                io.TextIOWrapper(error, encoding="cp1252", errors="strict") as stderr, \
                mock.patch.object(SUPPORT.sys, "stdout", stdout), \
                mock.patch.object(SUPPORT.sys, "stderr", stderr):
            code = SUPPORT.main(argv)
            stdout.flush()
            stderr.flush()
            return code, output.getvalue(), error.getvalue()

    def test_record_and_validate_non_ascii_paths_on_cp1252_stdout(self):
        # Only toolchain validation is stubbed: record/write/readback/reporting
        # use the production code and a disposable UTF-8 manifest on disk.
        with mock.patch.object(SUPPORT, "validate_paths", return_value=(self.project, self.state)):
            record = ["record", "--project-root", str(self.project),
                      "--platform", SUPPORT.actual_platform(), "--uv-path", str(self.uv),
                      "--uv-version", "0.9.20", "--uv-source", "path"]
            for argv, reused in ((record, False), (["validate", "--project-root", str(self.project)], True)):
                with self.subTest(action=argv[0]):
                    code, output, error = self.run_with_ansi_pipes(argv)
                    self.assertEqual(code, 0, error.decode("ascii"))
                    self.assertEqual(error, b"")
                    self.assertTrue(output.isascii())
                    report = json.loads(output.decode("ascii"))
                    self.assertTrue(report["ok"])
                    self.assertEqual(report["reused"], reused)
                    self.assertEqual(report["project_root"], str(self.project))
                    self.assertEqual(report["uv"]["path"], str(self.uv))
                    manifest = self.state / "runtime.json"
                    self.assertEqual(report["runtime_manifest"], str(manifest))
                    saved = json.loads(manifest.read_text(encoding="utf-8"))
                    self.assertEqual(saved["project_root"], str(self.project))
                    self.assertEqual(saved["python_path"], report["python_path"])
                    self.assertEqual(saved["uv"], report["uv"])

    def test_bootstrap_error_json_is_ascii_on_cp1252_stderr(self):
        failure = SUPPORT.BootstrapError("路径错误", "无法验证项目路径。")
        with mock.patch.object(SUPPORT, "validate_paths", side_effect=failure):
            code, output, error = self.run_with_ansi_pipes(["validate", "--project-root", str(self.project)])
        self.assertEqual(code, 1)
        self.assertEqual(output, b"")
        self.assertTrue(error.isascii())
        self.assertEqual(json.loads(error.decode("ascii")),
                         {"ok": False, "error": failure.code, "message": failure.message})

    def test_unexpected_error_remains_ascii_and_sanitized_on_cp1252_stderr(self):
        diagnostic = "本地诊断不应输出"
        with mock.patch.object(SUPPORT, "validate_paths", side_effect=OSError(diagnostic)):
            code, output, error = self.run_with_ansi_pipes(["validate", "--project-root", str(self.project)])
        self.assertEqual(code, 1)
        self.assertEqual(output, b"")
        self.assertTrue(error.isascii())
        report = json.loads(error.decode("ascii"))
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "runtime_validation")
        self.assertNotIn(diagnostic, report["message"])


if __name__ == "__main__":
    unittest.main()
