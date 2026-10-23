#!/usr/bin/env python3
"""Runtime ownership/version verification; never imports or starts DSH.

Invoked only by the project venv interpreter with -I -B after shell/PowerShell
preflight. Archive bootstrap deliberately does not require Python.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import sys
from pathlib import Path

SCHEMA_VERSION = 1
OWNER_MARKER = "dsh-invoker-bootstrap-v1"
PACKAGE_VERSION = "0.1.5rc1"
PYTHON_REQUEST = "3.12"
TECHNICAL_DIRS = (
    "bin", "python", "python-downloads", "python-bin", "tools", "tool-bin",
    "cache", "credentials", "tmp", "staging", "venv",
)


class BootstrapError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def fail(code: str, message: str) -> None:
    raise BootstrapError(code, message)


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def read_json(path: Path) -> dict:
    if path.is_symlink() or is_reparse(path) or not path.is_file() or path.stat().st_size > 16384:
        fail("runtime_manifest", "Runtime metadata must be a small regular file.")
    with path.open(encoding="utf-8") as stream:
        result = json.load(stream)
    if not isinstance(result, dict):
        fail("runtime_manifest", "Runtime metadata has an invalid schema.")
    return result


def validate_paths(project: Path, *, check_packages: bool = True) -> tuple[Path, Path]:
    if not project.is_absolute() or not project.is_dir():
        fail("project_root", "An existing absolute project root is required.")
    project = project.resolve(strict=True)
    state = project / ".dsh-invoker-profile"
    if state.is_symlink() or is_reparse(state) or not state.is_dir():
        fail("unsafe_path", "The runtime directory must be a real project directory.")
    for name in TECHNICAL_DIRS:
        directory = state / name
        if not directory.is_dir() or directory.is_symlink() or is_reparse(directory):
            fail("unsafe_path", "A technical directory is missing or is a link.")
        # Allow normal managed-Python/venv internal symlinks, but never links or
        # junctions that escape the private runtime tree. Do not follow links in
        # traversal (this also prevents cycles).
        for parent, directories, files in os.walk(directory, followlinks=False):
            for entry in directories + files:
                path = Path(parent) / entry
                if (path.is_symlink() or is_reparse(path)) and not inside(path, state):
                    fail("unsafe_path", "A runtime link escapes the project toolchain directory.")
    marker = state / ".bootstrap-owner"
    if marker.is_symlink() or is_reparse(marker) or not marker.is_file() or marker.stat().st_size > 128:
        fail("unowned_runtime", "Runtime ownership is missing or unsafe.")
    if marker.read_text(encoding="utf-8").strip() != OWNER_MARKER:
        fail("unowned_runtime", "Runtime ownership is not recognized; no files were replaced.")
    python = state / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file() or not inside(python, state):
        fail("unsafe_path", "The runtime interpreter is outside this project toolchain.")
    if Path(sys.prefix).resolve() != (state / "venv").resolve():
        fail("runtime_interpreter", "Use the absolute project venv Python interpreter.")
    if Path(sys.executable).absolute() != python.absolute():
        fail("runtime_interpreter", "The invoked interpreter does not match the owned venv.")
    if not inside(Path(sys.base_prefix), state / "python"):
        fail("runtime_interpreter", "The venv must use this project's managed Python installation.")
    if sys.version_info[:2] != (3, 12):
        fail("runtime_version", "This toolchain requires its managed Python 3.12 runtime.")
    if check_packages:
        for package in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin"):
            if importlib.metadata.version(package) != PACKAGE_VERSION:
                fail("runtime_version", "Installed SDK/runtime versions differ from the pinned release.")
    return project, state


def actual_platform() -> str:
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    system = platform.system()
    if system == "Darwin":
        return "macos-" + ("arm64" if machine == "aarch64" else machine)
    if system == "Windows":
        return "windows-" + machine
    if system == "Linux":
        return "linux-" + machine
    return "unsupported"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_uv(uv: dict, state: Path) -> None:
    if not isinstance(uv, dict) or set(uv) != {"path", "version", "source", "sha256"}:
        fail("runtime_manifest", "The runtime uv metadata is malformed.")
    if not isinstance(uv["path"], str) or not Path(uv["path"]).is_absolute():
        fail("runtime_manifest", "The recorded uv executable must be absolute.")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(uv["version"]))
    if not match or tuple(map(int, match.groups())) < (0, 9, 20):
        fail("runtime_version", "The recorded uv version is not supported.")
    if uv["source"] == "project":
        expected = state / "bin" / ("uv.exe" if os.name == "nt" else "uv")
        if Path(uv["path"]) != expected or expected.is_symlink() or is_reparse(expected):
            fail("unsafe_path", "The recorded local uv executable is not in the owned bin directory.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(uv["sha256"])) or digest(expected) != uv["sha256"]:
            fail("runtime_version", "The project uv binary has changed; it will not be replaced automatically.")
        if uv["version"] != "0.12.24":
            fail("runtime_version", "The local uv version differs from the pinned standalone release.")
    elif uv["source"] != "path" or uv["sha256"] is not None:
        fail("runtime_manifest", "The uv source is not recognized.")
    # PATH uv is never executed on validate/exec, and may have been uninstalled.
    # Its path records which existing tool was reused during explicit setup.


def expected_metadata(project: Path, state: Path) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "owner": "dsh-invoker",
        "state": "ready",
        "project_root": str(project),
        "platform": actual_platform(),
        "python_request": PYTHON_REQUEST,
        "python_path": str(state / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
        "python_version": platform.python_version(),
        "base_python_prefix": str(Path(sys.base_prefix).resolve()),
        "sdk_version": PACKAGE_VERSION,
        "runtime_version": PACKAGE_VERSION,
    }


def ready_report(metadata: dict, reused: bool) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "setup",
        "ok": True,
        "reused": reused,
        "project_root": metadata["project_root"],
        "runtime_manifest": str(Path(metadata["project_root"]) / ".dsh-invoker-profile/runtime.json"),
        "python_path": metadata["python_path"],
        "python_version": metadata["python_version"],
        "uv": metadata["uv"],
        "sdk_version": PACKAGE_VERSION,
        "runtime_version": PACKAGE_VERSION,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check-python", "record", "validate"))
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--platform")
    parser.add_argument("--uv-path")
    parser.add_argument("--uv-version")
    parser.add_argument("--uv-source", choices=("path", "project"))
    parser.add_argument("--uv-sha256", default="")
    args = parser.parse_args(argv)
    try:
        project, state = validate_paths(Path(args.project_root), check_packages=args.action != "check-python")
        if args.action == "check-python":
            return 0
        metadata = expected_metadata(project, state)
        manifest = state / "runtime.json"
        if args.action == "record":
            if args.platform != metadata["platform"]:
                fail("runtime_version", "Platform detection disagrees with the managed interpreter.")
            metadata["uv"] = {
                "path": args.uv_path, "version": args.uv_version,
                "source": args.uv_source, "sha256": args.uv_sha256 or None,
            }
            validate_uv(metadata["uv"], state)
            # Exclusive create: never overwrite a prior ready or foreign runtime.
            descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            reused = False
        else:
            saved = read_json(manifest)
            if set(saved) != set(metadata) | {"uv"} or any(saved.get(key) != value for key, value in metadata.items()):
                fail("runtime_version", "Runtime ownership, paths or versions have changed; no automatic repair was attempted.")
            validate_uv(saved["uv"], state)
            metadata = saved
            reused = True
        if not args.quiet:
            print(json.dumps(ready_report(metadata, reused), ensure_ascii=True))
        return 0
    except BootstrapError as error:
        result = {"ok": False, "error": error.code, "message": error.message}
    except (OSError, RuntimeError, ValueError, TypeError, importlib.metadata.PackageNotFoundError):
        result = {"ok": False, "error": "runtime_validation", "message": "Cannot verify the owned runtime. Inspect paths and installation locally; sensitive diagnostics are withheld."}
    print(json.dumps(result, ensure_ascii=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
