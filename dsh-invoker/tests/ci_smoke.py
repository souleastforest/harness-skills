#!/usr/bin/env python3
"""Native CI driver: existing uv, then a genuinely uv-free child PATH.

Start this ONLY through the already-approved bootstrap ``exec`` entrypoint in a
fresh CI project. It deliberately exercises ``setup`` for a SECOND disposable
project; invoking it locally therefore downloads tools/dependencies unless that
scenario already exists. It never uninstalls or modifies an existing uv.

No PyYAML/pytest, provider keys, artifact upload, cache of homes, or private user
profile is needed. All scenario files live under the checkout's ignored state.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
CHECKOUT = SKILL.parent
STATE_NAME = ".dsh-invoker-profile"
VERSION = "0.1.5rc1"
TEST_MODULES = (
    "test_bootstrap", "test_ci_smoke", "test_invoker", "test_invoker_regressions",
    "test_invoker_sdk", "test_runtime_integration", "test_windows_encoding",
)
COUNT_KEYS = ("tests", "failures", "errors", "skipped", "expected_failures", "unexpected_successes")
MAX_TEST_ID = 256
MAX_DIAGNOSTIC = 64 * 1024
BOOTSTRAP_ACTIONS = frozenset(("check", "setup", "exec"))
# Literal codes from the two native bootstrap entrypoints and their validator.
# Never forward their messages, paths, exception strings or other envelope data.
BOOTSTRAP_ERROR_CODES = frozenset((
    "arguments", "bootstrap_failed", "bootstrap_tool", "existing_venv", "filesystem", "hash_tool",
    "incomplete_runtime", "powershell_version", "project_root", "python_install", "release_metadata",
    "runtime_busy", "runtime_interpreter", "runtime_manifest", "runtime_missing", "runtime_validation",
    "runtime_version", "script_path", "sdk_install", "unowned_runtime", "unsafe_path", "unsupported_platform",
    "uv_archive", "uv_capability", "uv_digest", "uv_download", "uv_version", "venv_create",
))

# Native fields are validated against literal/source-derived values after loading
# the checked-in fixture below; native child envelopes are not trusted wholesale.

# Import our test infrastructure, not another SDK/protocol implementation.
_spec = importlib.util.spec_from_file_location("dsh_ci_native_test_support", TESTS / "test_runtime_integration.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("native CI process support is missing")
_support = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _support
_spec.loader.exec_module(_support)
NATIVE_STAGES = _support.COMMAND_STAGES
NATIVE_EXCEPTIONS = _support.EXCEPTION_TYPES
NATIVE_HELPER_ERRORS = _support.HELPER_ERROR_CODES


def native_source_sites() -> frozenset[tuple[str, int]]:
    """Only executable function/line pairs from checked-in fixture source."""
    code = compile((TESTS / "test_runtime_integration.py").read_text(encoding="utf-8"), str(TESTS / "test_runtime_integration.py"), "exec")
    def sites(function: Any) -> set[tuple[str, int]]:
        result = {(function.co_name, line) for _start, _end, line in function.co_lines() if line is not None}
        for constant in function.co_consts:
            if isinstance(constant, type(code)):
                result.update(sites(constant))
        return result
    return frozenset(sites(code))


NATIVE_SOURCE_SITES = native_source_sites()


class VerificationError(RuntimeError):
    """Only fixed driver messages and validated diagnostics may be public."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def allowed_test_ids(lane: str) -> frozenset[str]:
    """Build IDs from trusted source identifiers, never test descriptions/data."""
    identifiers = set()
    for module in TEST_MODULES:
        if (module == "test_runtime_integration") != (lane == "native"):
            continue
        tree = ast.parse((TESTS / (module + ".py")).read_text(encoding="utf-8"))
        identifiers.add("unittest.loader._FailedTest." + module)
        for phase in ("setUpModule", "tearDownModule"):
            identifiers.add(f"{phase} ({module})")
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for phase in ("setUpClass", "tearDownClass"):
                    identifiers.add(f"{phase} ({module}.{node.name})")
                for method in node.body:
                    if isinstance(method, ast.FunctionDef) and method.name.startswith("test_"):
                        identifiers.add(f"{module}.{node.name}.{method.name}")
    return frozenset(identifier for identifier in identifiers if valid_test_id(identifier, identifiers))


def valid_test_id(identifier: Any, allowed: Any) -> bool:
    return (
        isinstance(identifier, str) and len(identifier) <= MAX_TEST_ID
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_. ()]*", identifier) is not None
        and identifier in allowed
    )


def lane_succeeded(lane: str, counts: dict[str, int]) -> bool:
    return (
        counts["tests"] > 0 and counts["failures"] == counts["errors"] == counts["unexpected_successes"] == 0
        and (lane != "native" or counts["skipped"] == 0)
    )


def native_failure_diagnostic(value: Any, allowed: frozenset[str]) -> dict[str, Any] | None:
    """Reconstruct fixed source codes only; reject unknown diagnostic values."""
    if type(value) is not dict or not valid_test_id(value.get("test_id"), allowed):
        return None
    if (type(value.get("exception")) is not str or value["exception"] not in NATIVE_EXCEPTIONS
            or type(value.get("site")) is not str or type(value.get("line")) is not int
            or (value["site"], value["line"]) not in NATIVE_SOURCE_SITES):
        return None
    safe = {key: value[key] for key in ("test_id", "exception", "site", "line")}
    for field, codes in (("stage", NATIVE_STAGES), ("process_stage", _support.PROCESS_STAGES),
                         ("fixture_stage", _support.FIXTURE_STAGES), ("helper_error", NATIVE_HELPER_ERRORS),
                         ("child_exception", NATIVE_EXCEPTIONS), ("failure_category", _support.FIXTURE_FAILURE_CATEGORIES)):
        if field in value:
            if type(value[field]) is not str or value[field] not in codes:
                return None
            safe[field] = value[field]
    if "rpc_code" in value:
        if type(value["rpc_code"]) is not int or value["rpc_code"] not in _support.RPC_ERROR_CODES:
            return None
        safe["rpc_code"] = value["rpc_code"]
    loader_keys = set(_support.LOADER_DIAGNOSTIC_FIELDS) & value.keys()
    if loader_keys:
        loader = _support.loader_failure_fields(value)
        if (set(loader) != loader_keys or value.get("failure_category") != "loader-settlement"
                or value.get("rpc_code") != -32603 or value.get("fixture_stage") not in ("initialize", "harness-enter")):
            return None
        safe.update(loader)
    if "returncode" in value:
        code = value["returncode"]
        if type(code) is not int or not -(2**31) <= code < 2**32:
            return None
        safe["returncode"] = code
    if "timed_out" in value:
        if type(value["timed_out"]) is not bool:
            return None
        safe["timed_out"] = value["timed_out"]
    return safe


class SafeTestResult(unittest.TestResult):
    """Count outcomes without retaining exceptions, tracebacks or skip reasons."""

    def __init__(self, lane: str, allowed: frozenset[str]) -> None:
        super().__init__()
        self.lane = lane
        self.allowed = allowed
        self.counts = dict.fromkeys(COUNT_KEYS, 0)
        self.failed_test_ids: set[str] = set()
        self.native_failures: list[dict[str, Any]] = []

    def record_native_failure(self, test: Any, err: Any) -> None:
        if self.lane != "native":
            return
        site, line = None, None
        trace = err[2]
        while trace is not None:
            code = trace.tb_frame.f_code
            if code.co_filename == str(TESTS / "test_runtime_integration.py") and (code.co_name, trace.tb_lineno) in NATIVE_SOURCE_SITES:
                site, line = code.co_name, trace.tb_lineno
            trace = trace.tb_next
        value = {"test_id": test.id(), "exception": err[0].__name__, "site": site, "line": line}
        for command in (vars(test).get("ci_diagnostic"), vars(err[1]).get("ci_diagnostic")):
            if type(command) is dict:
                # Reconstruct known keys only; never preserve raw attributes.
                value.update({key: command[key] for key in (
                    "stage", "process_stage", "returncode", "timed_out", "helper_error", "child_exception", "fixture_stage",
                    "rpc_code", "failure_category", *_support.LOADER_DIAGNOSTIC_FIELDS,
                ) if key in command})
        safe = native_failure_diagnostic(value, self.allowed)
        if safe is not None and safe not in self.native_failures and len(self.native_failures) < len(self.allowed):
            self.native_failures.append(safe)

    def record(self, outcome: str, test: Any) -> None:
        self.counts[outcome] += 1
        identifier = test.id()
        if valid_test_id(identifier, self.allowed):
            self.failed_test_ids.add(identifier)

    def addFailure(self, test: Any, err: Any) -> None:
        self.record("failures", test)
        self.record_native_failure(test, err)

    def addError(self, test: Any, err: Any) -> None:
        self.record("errors", test)
        self.record_native_failure(test, err)

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
        if err is not None:
            # The parent ID is source-defined; subtest IDs may embed HTTP data.
            self.record("failures" if issubclass(err[0], test.failureException) else "errors", test)
            self.record_native_failure(test, err)

    def addSkip(self, test: Any, reason: Any) -> None:
        if self.lane == "native":
            self.record("skipped", test)
        else:
            self.counts["skipped"] += 1

    def addExpectedFailure(self, test: Any, err: Any) -> None:
        self.counts["expected_failures"] += 1

    def addUnexpectedSuccess(self, test: Any) -> None:
        self.record("unexpected_successes", test)

    def wasSuccessful(self) -> bool:
        return lane_succeeded(self.lane, {**self.counts, "tests": self.testsRun})

    def diagnostic(self) -> dict[str, Any]:
        value = {
            "schema_version": 1, "kind": "unittest", "lane": self.lane, "ok": self.wasSuccessful(),
            "counts": {**self.counts, "tests": self.testsRun}, "failed_test_ids": sorted(self.failed_test_ids),
        }
        if self.native_failures:
            value["native_failures"] = self.native_failures
        return value


def run_suite(suite: unittest.TestSuite, lane: str) -> dict[str, Any]:
    result = SafeTestResult(lane, allowed_test_ids(lane))
    # Discard, do not capture, test prints/warnings. The result never formats err.
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        suite.run(result)
    return result.diagnostic()


def diagnostic_object(text: str) -> dict[str, Any] | None:
    if len(text) > MAX_DIAGNOSTIC:
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def unittest_diagnostic(text: str, lane: str) -> dict[str, Any] | None:
    value = diagnostic_object(text)
    if value is None or value.get("schema_version") != 1 or value.get("kind") != "unittest" or value.get("lane") != lane:
        return None
    counts = value.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(COUNT_KEYS):
        return None
    if any(type(count) is not int or not 0 <= count <= 100_000 for count in counts.values()):
        return None
    identifiers = value.get("failed_test_ids")
    allowed = allowed_test_ids(lane)
    if not isinstance(identifiers, list) or len(identifiers) > len(allowed):
        return None
    if any(not valid_test_id(identifier, allowed) for identifier in identifiers):
        return None
    ok = lane_succeeded(lane, counts)
    if value.get("ok") is not ok:
        return None
    safe = {
        "schema_version": 1, "kind": "unittest", "lane": lane, "ok": ok,
        "counts": counts, "failed_test_ids": sorted(set(identifiers)),
    }
    if "native_failures" in value:
        details = value["native_failures"]
        if lane != "native" or type(details) is not list or len(details) > len(allowed):
            return None
        validated = [native_failure_diagnostic(detail, allowed) for detail in details]
        if any(detail is None or detail["test_id"] not in identifiers for detail in validated):
            return None
        safe["native_failures"] = validated
    return safe


def bootstrap_diagnostic(stdout: str, stderr: str, action: str) -> dict[str, Any] | None:
    if action not in BOOTSTRAP_ACTIONS:
        return None
    for text in (stderr, stdout):
        value = diagnostic_object(text)
        if value is None or value.get("ok") is not False or value.get("action", action) != action:
            continue
        code = value.get("error", value.get("code"))
        if isinstance(code, str) and code in BOOTSTRAP_ERROR_CODES:
            return {"kind": "bootstrap", "action": action, "error": code}
    return None


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def normalized_arch() -> str:
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "unsupported")


def powershell_quote(value: str) -> str:
    require("\x00" not in value and "\n" not in value and "\r" not in value, "invalid PowerShell argument")
    return "'" + value.replace("'", "''") + "'"


def bootstrap_command(action: str, root: Path, python_args: list[str] | None = None) -> list[str]:
    if os.name == "nt":
        pwsh = shutil.which("pwsh")
        require(pwsh is not None, "native Windows lane requires PowerShell 7 (pwsh)")
        script = SKILL / "scripts" / "bootstrap.ps1"
        command = (
            "$ErrorActionPreference = 'Stop'; & " + powershell_quote(str(script))
            + " -Action " + powershell_quote(action) + " -ProjectRoot " + powershell_quote(str(root))
        )
        if python_args is not None:
            command += " -PythonArgs @(" + ",".join(map(powershell_quote, python_args)) + ")"
        command += "; exit $LASTEXITCODE"
        # No persistent execution-policy change or bypass.
        return [str(pwsh), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    bash = next((candidate for candidate in ("/bin/bash", "/usr/bin/bash") if Path(candidate).is_file()), None)
    require(bash is not None, "POSIX lane requires native bash")
    command = [str(bash), str(SKILL / "scripts" / "bootstrap.sh"), action, "--project-root", str(root)]
    if python_args is not None:
        command.extend(["--", *python_args])
    return command


def run_command(
    command: list[str], *, environment: dict[str, str], cwd: Path, timeout: float, label: str,
    lane: str | None = None, action: str | None = None,
) -> Any:
    result = _support.run_owned(command, cwd=cwd, env=environment, timeout=timeout)
    require(not result.timed_out, f"independent CI wall-clock watchdog fired: {label}")
    if result.returncode != 0:
        # Failed children can contain secrets, including unittest assertion data.
        # Only reconstruct source-allowlisted IDs/counts or literal bootstrap codes.
        diagnostic = unittest_diagnostic(result.stdout, lane) if lane is not None else None
        if diagnostic is None and action is not None:
            diagnostic = bootstrap_diagnostic(result.stdout, result.stderr, action)
        safe = "" if diagnostic is None else "; safe diagnostic=" + json.dumps(diagnostic, ensure_ascii=True, sort_keys=True)
        raise VerificationError(f"{label} failed (exit {result.returncode}); raw tool/runtime diagnostics withheld{safe}")
    return result


def read_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"{label} did not emit exactly one JSON object") from error
    require(isinstance(value, dict), f"{label} must be an object")
    return value


def checked_runtime(root: Path, expected_uv: Path, source: str) -> None:
    state = root / STATE_NAME
    manifest_path = state / "runtime.json"
    require(manifest_path.is_file() and not manifest_path.is_symlink(), "bootstrap did not create a regular runtime manifest")
    manifest = read_object(manifest_path.read_text(encoding="utf-8"), "runtime.json")
    require(manifest.get("schema_version") == 1 and manifest.get("owner") == "dsh-invoker", "unknown runtime ownership schema")
    require(manifest.get("state") == "ready", "bootstrap runtime was not finalized")
    require(manifest.get("sdk_version") == VERSION and manifest.get("runtime_version") == VERSION,
            "bootstrap did not pin matching published SDK/runtime wheels")
    require(Path(manifest.get("project_root", "")).resolve() == root.resolve(), "manifest records the wrong project root")
    require(manifest.get("python_request") == "3.12", "bootstrap did not request managed Python 3.12")
    uv = manifest.get("uv", {})
    require(uv.get("source") == source, f"wrong uv acquisition branch: expected {source}")
    require(uv.get("version") == "0.12.24", "native CI must use fixed uv 0.12.24")
    uv_path = Path(uv.get("path", ""))
    python_path = Path(manifest.get("python_path", ""))
    require(uv_path.is_absolute() and uv_path.is_file(), "reported uv is not an absolute existing executable")
    require(uv_path.resolve() == expected_uv.resolve(), "bootstrap unexpectedly replaced or relocated the selected uv")
    require(python_path.is_absolute() and python_path.is_file(), "reported Python is not an absolute existing executable")
    expected_python = state / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    require(python_path.absolute() == expected_python.absolute(), "bootstrap selected the wrong venv interpreter")
    require(python_path.resolve().is_relative_to(state.resolve()), "venv Python escaped the project-managed state")
    require(str(manifest.get("python_version", "")).startswith("3.12."), "native tests must use managed Python 3.12")
    base_python = Path(manifest.get("base_python_prefix", ""))
    require(base_python.is_absolute() and base_python.resolve().is_relative_to((state / "python").resolve()),
            "base Python escaped the project-managed Python directory")
    if source == "project":
        require(uv_path.resolve().is_relative_to((state / "bin").resolve()), "bootstrapped uv escaped the project-local bin directory")
        require(uv.get("sha256") == digest(uv_path), "project-local uv binary no longer matches its recorded digest")
    else:
        require(source == "path" and uv.get("sha256") is None, "existing uv must record source path and no asset digest")


def suite_for(lane: str) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted(TESTS.glob("test*.py")):
        is_native = path.name == "test_runtime_integration.py"
        if is_native == (lane == "native"):
            suite.addTests(loader.discover(str(TESTS), pattern=path.name))
    require(suite.countTestCases() > 0, f"{lane} tests were not discovered")
    return suite


def run_lane(lane: str, root: Path) -> int:
    require(Path(sys.prefix).resolve() == (root / STATE_NAME / "venv").resolve(), "tests are not running in the bootstrap-owned venv")
    require(sys.version_info[:2] == (3, 12), "CI requires managed Python 3.12")
    if lane == "native":
        require(os.environ.get("DSH_INVOKER_REQUIRE_RUNTIME") == "1", "native runtime lane must be REQUIRED")
    # Discovery/import failures are TestResult errors, not raw loader tracebacks.
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        suite = suite_for(lane)
    diagnostic = run_suite(suite, lane)
    print(json.dumps(diagnostic, ensure_ascii=True, sort_keys=True))
    return 0 if diagnostic["ok"] else 1


def run_tests(root: Path, environment: dict[str, str], lane: str) -> None:
    command = bootstrap_command("exec", root, [str(Path(__file__).resolve()), "--lane", lane, "--project-root", str(root)])
    result = run_command(command, environment=environment, cwd=root, timeout=240, label=f"{lane} tests", lane=lane, action="exec")
    diagnostic = unittest_diagnostic(result.stdout, lane)
    require(diagnostic is not None and diagnostic["ok"], f"{lane} tests did not emit a successful safe diagnostic")
    print(json.dumps(diagnostic, ensure_ascii=True, sort_keys=True))


def controlled_no_uv_path(directory: Path, environment: dict[str, str]) -> str:
    """Use only native shell utilities; never remove/rename a runner tool."""
    directory.mkdir()
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        pwsh = shutil.which("pwsh")
        require(pwsh is not None, "PowerShell 7 is unavailable")
        entries = [system_root / "System32", system_root, Path(str(pwsh)).parent]
        controlled = os.pathsep.join(map(str, entries))
        environment["NoDefaultCurrentDirectoryInExePath"] = "1"
    else:
        # Exact command allowlist: no Python/uv entry or unfiltered system bin
        # directory can accidentally hide a supposedly absent preinstalled uv.
        utility_names = (
            "bash", "sh", "env", "uname", "curl", "mkdir", "rmdir", "mktemp", "tar", "gzip",
            "sha256sum", "shasum", "cp", "mv", "chmod", "rm", "dirname", "basename",
            "readlink", "realpath", "stat", "sleep", "grep", "tr", "cut", "wc", "head",
            "sed", "awk", "sort", "find", "touch", "date", "sw_vers", "getconf",
            "ldd", "expr", "cat", "tee", "install",
        )
        for name in utility_names:
            source = next((Path(prefix) / name for prefix in ("/usr/bin", "/bin", "/usr/sbin", "/sbin") if (Path(prefix) / name).is_file()), None)
            if source is not None:
                if name == "shasum" and sys.platform == "darwin":
                    # Apple's /usr/bin/perl dispatcher cannot be relocated with
                    # shasum. Use an adjacent native script with its own absolute
                    # versioned shebang, without adding any system dir to PATH.
                    for sibling in sorted(source.parent.glob("shasum[0-9]*"), reverse=True):
                        if not re.fullmatch(r"shasum[0-9]+\.[0-9]+(?:\.[0-9]+)?", sibling.name) or not sibling.is_file():
                            continue
                        with sibling.open("rb") as handle:
                            shebang = handle.readline(128)
                        if re.fullmatch(rb"#!/usr/bin/perl[0-9]+\.[0-9]+(?:\.[0-9]+)?\n", shebang) and Path(os.fsdecode(shebang[2:].strip())).is_file():
                            source = sibling
                            break
                (directory / name).symlink_to(source)
        controlled = str(directory)
    for name in ("uv", "uv.exe", "uv.cmd", "uv.bat"):
        require(shutil.which(name, path=controlled) is None, "uv is discoverable in controlled child PATH; absent-uv branch is not valid")
    return controlled


def verify_absence_in_native_shell(environment: dict[str, str], root: Path) -> None:
    if os.name == "nt":
        pwsh = shutil.which("pwsh")
        command = [str(pwsh), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                   "if (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue) { exit 1 }; exit 0"]
    else:
        command = ["/bin/bash", "--noprofile", "--norc", "-c", "! command -v uv >/dev/null 2>&1"]
    run_command(command, environment=environment, cwd=root, timeout=15, label="native shell uv-absence proof")


def drive(args: argparse.Namespace) -> int:
    root = Path(args.project_root)
    require(root.is_absolute() and root.is_dir(), "project root must be an existing absolute directory")
    root = root.resolve()
    require(root.is_relative_to(CHECKOUT / STATE_NAME / "ci-scenarios"), "CI projects must stay inside checkout-managed ci-scenarios")
    expected_os = {"linux": "Linux", "darwin": "macOS", "win32": "Windows"}.get(sys.platform, "unsupported")
    require(expected_os == args.expected_os and normalized_arch() == args.expected_arch, "runner OS/architecture does not match the named native lane")
    expected_uv = Path(args.uv_path)
    require(expected_uv.is_absolute() and expected_uv.is_file(), "existing uv path must be absolute")
    checked_runtime(root, expected_uv, "path")
    before_uv = digest(expected_uv)
    require(Path(sys.prefix).resolve() == (root / STATE_NAME / "venv").resolve(), "driver must run through real bootstrap exec")
    environment = _support.isolated_environment(root / STATE_NAME / "ci environment")
    environment.update({
        "DSH_INVOKER_REQUIRE_RUNTIME": "1",
        "DSH_INVOKER_TEST_ROOT": str(root / STATE_NAME / "tmp" / "runtime-tests"),
    })
    run_tests(root, environment, "unit")
    run_tests(root, environment, "native")
    require(digest(expected_uv) == before_uv, "existing uv binary changed during tests")

    # New, empty, owned operation root. No personal/global cache or temp tree.
    operation = Path(tempfile.mkdtemp(prefix="no uv scenario 空格 ", dir=root.parent)).resolve()
    try:
        absent_root = operation / "project absent uv 非ASCII"
        absent_root.mkdir()
        absent_environment = _support.isolated_environment(operation / "isolated environment")
        absent_environment["PATH"] = controlled_no_uv_path(operation / "system-tools-only", absent_environment)
        verify_absence_in_native_shell(absent_environment, absent_root)
        state = absent_root / STATE_NAME
        require(not state.exists(), "absent-uv scenario already had a state/toolchain directory")
        check = run_command(bootstrap_command("check", absent_root), environment=absent_environment, cwd=absent_root, timeout=20, label="shell-only check without uv", action="check")
        value = read_object(check.stdout, "bootstrap check")
        require(value.get("uv", {}).get("source") == "absent" and value.get("uv", {}).get("path") is None, "bootstrap check did not report genuinely absent uv")
        require(not state.exists(), "read-only shell check wrote the state directory")
        setup = run_command(bootstrap_command("setup", absent_root), environment=absent_environment, cwd=absent_root, timeout=420, label="real absent-uv bootstrap", action="setup")
        value = read_object(setup.stdout, "bootstrap setup")
        local_uv = Path(value.get("uv", {}).get("path", ""))
        require(value.get("uv", {}).get("version") == "0.12.24", "bootstrap did not use fixed uv 0.12.24")
        checked_runtime(absent_root, local_uv, "project")
        # No local uv is added to PATH as a side effect; exec uses absolute Python.
        verify_absence_in_native_shell(absent_environment, absent_root)
        absent_environment.update({
            "DSH_INVOKER_REQUIRE_RUNTIME": "1",
            "DSH_INVOKER_TEST_ROOT": str(state / "tmp" / "runtime-tests"),
        })
        run_tests(absent_root, absent_environment, "native")
        require(digest(expected_uv) == before_uv, "absent-uv scenario modified the existing runner uv")
    finally:
        # Only this freshly claimed operation tree, never a preexisting user dir.
        shutil.rmtree(operation)
    print(f"Native {args.expected_os}/{args.expected_arch}: units, existing uv, absent uv, and REQUIRED published runtime passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--uv-path")
    parser.add_argument("--expected-os", choices=("Linux", "macOS", "Windows"))
    parser.add_argument("--expected-arch", choices=("x64", "arm64"))
    parser.add_argument("--lane", choices=("unit", "native"))
    args = parser.parse_args()
    root = Path(args.project_root)
    if args.lane:
        return run_lane(args.lane, root.resolve())
    require(bool(args.uv_path and args.expected_os and args.expected_arch), "driver requires existing uv path and expected native OS/architecture")
    return drive(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as error:
        print(f"CI verification failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print("CI verification failed; unexpected raw diagnostics withheld", file=sys.stderr)
        raise SystemExit(1)
