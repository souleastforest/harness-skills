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
import hashlib
import importlib.util
import json
import os
import platform
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

# Import our test infrastructure, not another SDK/protocol implementation.
_spec = importlib.util.spec_from_file_location("dsh_ci_native_test_support", TESTS / "test_runtime_integration.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("native CI process support is missing")
_support = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _support
_spec.loader.exec_module(_support)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


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
) -> Any:
    result = _support.run_owned(command, cwd=cwd, env=environment, timeout=timeout)
    require(not result.timed_out, f"independent CI wall-clock watchdog fired: {label}")
    if result.returncode != 0:
        # A failing unittest traceback may render unexpected captured data.
        # Withhold it just like raw bootstrap/SDK/HTTP diagnostics.
        raise RuntimeError(f"{label} failed (exit {result.returncode}); raw tool/runtime diagnostics withheld")
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
    suite = suite_for(lane)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if lane == "native" and result.skipped:
        raise RuntimeError("REQUIRED native runtime tests silently skipped")
    require(result.testsRun > 0, f"{lane} lane executed no tests")
    return 0 if result.wasSuccessful() else 1


def run_tests(root: Path, environment: dict[str, str], lane: str) -> None:
    command = bootstrap_command("exec", root, [str(Path(__file__).resolve()), "--lane", lane, "--project-root", str(root)])
    result = run_command(command, environment=environment, cwd=root, timeout=240, label=f"{lane} tests")
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)


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
        check = run_command(bootstrap_command("check", absent_root), environment=absent_environment, cwd=absent_root, timeout=20, label="shell-only check without uv")
        value = read_object(check.stdout, "bootstrap check")
        require(value.get("uv", {}).get("source") == "absent" and value.get("uv", {}).get("path") is None, "bootstrap check did not report genuinely absent uv")
        require(not state.exists(), "read-only shell check wrote the state directory")
        setup = run_command(bootstrap_command("setup", absent_root), environment=absent_environment, cwd=absent_root, timeout=420, label="real absent-uv bootstrap")
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
    except Exception as error:
        print(f"CI verification failed: {error}", file=sys.stderr)
        raise SystemExit(1)
