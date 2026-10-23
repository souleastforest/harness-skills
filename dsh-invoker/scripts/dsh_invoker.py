#!/usr/bin/env python3
"""Small, non-interactive adapter for the published DeepSeek Harness SDK.

Discovery reads directory names/marker existence, never profile or credential
contents. Configuration stores only a confirmed home and profile name. Invocation
runs the SDK in an owned worker: its stdout/stderr are not a diagnostic channel.
The SDK still owns its protocol, runtime, permissions and local session storage.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import queue
import select
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

STATE_DIRECTORY = ".dsh-invoker-profile"
BINDING_FILENAME = "binding.json"
LIFECYCLE_FILENAME = "profile-lifecycle.json"
SCHEMA_VERSION = 1
SDK_VERSION = "0.1.5rc1"
SDK_DISTRIBUTIONS = ("deepseek-harness-sdk", "deepseek-harness-runtime-bin")
PROFILE_MARKERS = ("package.json", "cordis.patch.yml")
NON_SDK_PROFILES = frozenset(("desktop", "web", "headless", "acp", "tui"))
MAX_BINDING_BYTES = 16_384
MAX_PROMPT_BYTES = 1_048_576
MAX_WORKER_BYTES = 8 * MAX_PROMPT_BYTES
PRIVACY_PATCH = Path(__file__).resolve().parent.parent / "assets" / "no-session-log-upload.patch.yml"
PRIVACY_PATCH_BYTES = (
    b"- id: session-log-deepseek\n"
    b"  name: '@deepseek-ai/dsh-session-log-deepseek'\n"
    b"  disabled: true\n"
)


class InvokerError(Exception):
    """Messages are fixed advice, not exceptions/paths supplied by the SDK."""

    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code


def _error(code: str, message: str, exit_code: int = 2) -> InvokerError:
    return InvokerError(code, message, exit_code)


def _error_document(action: str, error: InvokerError) -> dict[str, Any]:
    return {"ok": False, "action": action, "error": {"code": error.code, "message": error.message}}


def _emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, ensure_ascii=True, separators=(",", ":")), flush=True)


def _absolute_path(value: str, *, directory: bool = False) -> Path:
    try:
        if not isinstance(value, str) or not value or not value.strip() or "\x00" in value:
            raise ValueError
        path = Path(value)
        if not path.is_absolute():
            raise ValueError
        path = path.resolve()
        if directory and not path.is_dir():
            raise ValueError
        return path
    except (OSError, ValueError, RuntimeError):
        raise _error("INVALID_PATH", "Use an absolute path; project roots and task workspaces must already be directories.") from None


def _is_link(info: os.stat_result) -> bool:
    # Windows junctions and other reparse points need the same treatment as links.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_entry(path: Path, *, directory: bool = False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if _is_link(info) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise _error("UNSAFE_STATE_PATH", "A binding, state directory or Git ignore path is a link, junction or unexpected file type. Choose an owned regular location.")
    return info


def _state_path(project: Path) -> Path:
    state = project / STATE_DIRECTORY
    _safe_entry(state, directory=True)
    return state


def _read_regular(path: Path, limit: int) -> bytes | None:
    if _safe_entry(path) is None:
        return None
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _error("UNSAFE_STATE_PATH", "The requested state file is not a regular file.")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _error("INVALID_STATE", "The state file is too large. Reconfigure explicitly instead of interpreting it as a binding.")
    return data


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace regular files atomically, without following a final link.

    POSIX uses an opened, no-follow directory descriptor for both operations.
    Windows checks reparse points before creating and replacing the sibling file.
    This is not a security boundary against another process running as this user.
    """
    _safe_entry(path.parent, directory=True)
    previous = _safe_entry(path)
    if previous is not None:
        mode = stat.S_IMODE(previous.st_mode)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    directory_fd: int | None = None
    temporary_path = path.parent / temporary
    try:
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fd = os.open(temporary, flags, mode, dir_fd=directory_fd)
        else:
            fd = os.open(temporary_path, flags, mode)
        with os.fdopen(fd, "wb") as handle:
            if previous is not None and os.name != "nt":
                os.fchmod(handle.fileno(), mode)  # preserve, rather than change, the existing mode
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _safe_entry(path.parent, directory=True)
        _safe_entry(path)
        if directory_fd is not None:
            try:
                current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None and (_is_link(current) or not stat.S_ISREG(current.st_mode)):
                raise _error("UNSAFE_STATE_PATH", "The state target changed to an unsafe file type during the update.")
            os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            os.replace(temporary_path, path)
    finally:
        try:
            if directory_fd is not None:
                os.unlink(temporary, dir_fd=directory_fd)
            else:
                temporary_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


def validate_profile_name(value: str) -> str:
    # Match the launcher's core restrictions and avoid cross-platform aliases.
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or value in (".", "..") or value.casefold() == "node_modules"
        or any(character in value for character in '/\\<>:"|?*')
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value.endswith((".", " ")) or len(value) > 200
        or value.split(".", 1)[0].casefold() in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    ):
        raise _error("INVALID_PROFILE", "Use one profile name, not a path. Separators, dot traversal, node_modules and platform-reserved names are not accepted.")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _load_binding(project: Path) -> dict[str, Any] | None:
    path = _state_path(project) / BINDING_FILENAME
    try:
        raw = _read_regular(path, MAX_BINDING_BYTES)
    except InvokerError as error:
        if error.code != "INVALID_STATE":
            raise
        raise _error("INVALID_BINDING", "The regular binding file is oversized. Obtain confirmation and use configure --reconfigure to repair it.") from None
    if raw is None:
        return None
    try:
        binding = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if not isinstance(binding, dict) or set(binding) != {"schema_version", "home", "profile"}:
            raise ValueError
        if type(binding["schema_version"]) is not int or binding["schema_version"] != SCHEMA_VERSION:
            raise ValueError
        home = _absolute_path(binding["home"])
        if str(home) != binding["home"]:
            raise ValueError
        validate_profile_name(binding["profile"])
        return binding
    except (UnicodeError, ValueError, TypeError, KeyError, InvokerError):
        raise _error("INVALID_BINDING", "The saved binding is malformed or contains unsupported fields. Stop and obtain confirmation before configure --reconfigure; it will not be replaced automatically.") from None


def _marker_existence(profile: Path) -> dict[str, bool]:
    return {name: (profile / name).is_file() for name in PROFILE_MARKERS}


def _profile_details(home: Path, name: str) -> dict[str, Any]:
    directory = home / "profiles" / name
    markers = _marker_existence(directory)
    return {
        "name": name,
        "directory": str(directory),
        "exists": directory.is_dir(),
        "markers": markers,
        "initialization_required": name == "sdk" and not all(markers.values()),
        "sdk_compatibility": "not-sdk-profile" if name.casefold() in NON_SDK_PROFILES else "unverified-until-sdk-initializes",
    }


def _check_selected_profile(home: Path, name: str) -> dict[str, Any]:
    validate_profile_name(name)
    if name.casefold() in NON_SDK_PROFILES:
        raise _error("NON_SDK_PROFILE", "Desktop, web, headless, ACP and TUI profiles are not automatically Python SDK profiles. Confirm sdk, or an explicitly SDK-compatible custom profile; no fallback is performed.")
    # A home may be absent for the SDK's first initialization. Do not create it.
    _safe_entry(home, directory=True)
    _safe_entry(home / "profiles", directory=True)
    _safe_entry(home / "profiles" / name, directory=True)
    detail = _profile_details(home, name)
    for marker in PROFILE_MARKERS:
        _safe_entry(home / "profiles" / name / marker)
    if name != "sdk" and (not detail["exists"] or not all(detail["markers"].values())):
        raise _error("MISSING_CUSTOM_PROFILE", "The chosen custom profile is missing or lacks package.json / cordis.patch.yml. This helper can initialize only the built-in sdk on invocation; create a compatible custom profile separately, then confirm it.")
    return detail


def _binding_profile(project: Path, binding: dict[str, Any]) -> dict[str, Any]:
    detail = _check_selected_profile(Path(binding["home"]), binding["profile"])
    raw = _read_regular(_state_path(project) / LIFECYCLE_FILENAME, MAX_BINDING_BYTES)
    if raw is not None:
        try:
            marker = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
            if not isinstance(marker, dict) or set(marker) != {"schema_version", "home", "profile", "initialized"} or type(marker["initialized"]) is not bool or type(marker["schema_version"]) is not int or marker["schema_version"] != SCHEMA_VERSION:
                raise ValueError
        except (ValueError, UnicodeError):
            raise _error("INVALID_LIFECYCLE", "The nonsecret profile lifecycle marker is invalid. Obtain confirmation and configure --reconfigure to repair it.") from None
        if all(marker[key] == binding[key] for key in binding) and marker["initialized"] and (not detail["exists"] or not all(detail["markers"].values())):
            raise _error("STALE_BINDING", "The previously initialized home/profile has disappeared or lost its markers. Obtain confirmation and configure --reconfigure before initializing it again; no fallback is performed.")
    return detail


def _record_initialized(project: Path, binding: dict[str, Any]) -> None:
    # Store lifecycle facts only, never profile contents, credentials or sessions.
    if _load_binding(project) != binding:
        raise _error("BINDING_CHANGED", "The confirmed binding changed before the task was sent. Recheck status and intentionally invoke again; no fallback is performed.")
    detail = _check_selected_profile(Path(binding["home"]), binding["profile"])
    if detail["exists"] and all(detail["markers"].values()):
        marker = {**binding, "initialized": True}
        path = _state_path(project) / LIFECYCLE_FILENAME
        data = (json.dumps(marker, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
        if _read_regular(path, MAX_BINDING_BYTES) != data:
            _atomic_write(path, data)


def _candidate(home: Path, source: str) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    readable = True
    try:
        for directory in sorted((home / "profiles").iterdir(), key=lambda path: path.name):
            if not directory.is_dir() or _is_link(directory.lstat()):
                continue
            try:
                validate_profile_name(directory.name)
            except InvokerError:
                continue
            profiles.append(_profile_details(home, directory.name))
    except FileNotFoundError:
        pass
    except OSError:
        readable = False
    return {"source": source, "home": str(home), "exists": home.is_dir(), "profiles_readable": readable, "profiles": profiles}


def discover(project: Path) -> dict[str, Any]:
    binding = _load_binding(project)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    environment_home = os.environ.get("DSH_HOME", "")
    sources: list[tuple[Path, str]] = []
    candidate_problems: list[dict[str, str]] = []
    if environment_home.strip():
        try:
            home = Path(environment_home).expanduser()
            sources.append(((home if home.is_absolute() else project / home).resolve(), "DSH_HOME"))
        except (OSError, ValueError, RuntimeError):
            candidate_problems.append({"source": "DSH_HOME", "code": "INVALID_ENV_HOME"})
    sources.append(((Path.home() / ".dsh").resolve(), "user-home-default"))
    if binding is not None:
        sources.insert(0, (Path(binding["home"]), "saved-binding"))
    for home, source in sources:
        if str(home) not in seen:
            candidates.append(_candidate(home, source))
            seen.add(str(home))
    selection = {
        "home": binding["home"] if binding else candidates[0]["home"],
        "profile": binding["profile"] if binding else "sdk",
        "confirmed": binding is not None,
        "source": "saved-binding" if binding else candidates[0]["source"],
    }
    return {
        "ok": True, "action": "discover", "project_root": str(project),
        "binding_file": str(project / STATE_DIRECTORY / BINDING_FILENAME),
        "binding": {"state": "valid", **binding} if binding else {"state": "missing"},
        "selection": selection, "candidates": candidates, "candidate_problems": candidate_problems,
    }


def _sdk_metadata() -> dict[str, Any]:
    versions: dict[str, str | None] = {}
    for distribution in SDK_DISTRIBUTIONS:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return {"required_version": SDK_VERSION, "versions": versions, "ready": all(version == SDK_VERSION for version in versions.values())}


def _require_sdk() -> None:
    metadata = _sdk_metadata()
    if not metadata["ready"]:
        raise _error("SDK_NOT_READY", "The project environment needs deepseek-harness-sdk and deepseek-harness-runtime-bin at 0.1.5rc1. Use the consented bootstrap setup; this helper never installs dependencies or falls back to a CLI.", 3)


def status(project: Path) -> dict[str, Any]:
    document = discover(project)
    document["action"] = "status"
    selected = document["selection"]
    home, name = Path(selected["home"]), selected["profile"]
    detail = _profile_details(home, name)
    problem: str | None = None
    if selected["confirmed"]:
        try:
            binding = {key: document["binding"][key] for key in ("schema_version", "home", "profile")}
            detail = _binding_profile(project, binding)
        except InvokerError as error:
            problem = error.code
    document["profile"] = detail
    document["profile_problem"] = problem
    document["sdk"] = _sdk_metadata()
    document["credentials"] = {
        "environment_key_present": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        "home_credentials_file_exists": (home / ".credentials.yaml").is_file(),
        "home_dotenv_exists": (home / ".env").is_file(),
        "project_dotenv_exists": (project / ".env").is_file(),
        "verified": False,
        "note": "Presence hints only; no credential or .env files were read. Absence of the environment key does not rule out stored credentials, other providers or plugins. A task workspace can differ from this project root.",
    }
    return document


def _configuration_target(args: argparse.Namespace) -> tuple[Path, str]:
    if args.profile_dir is not None:
        supplied = Path(args.profile_dir)
        if not supplied.is_absolute() or ".." in supplied.parts or supplied.parent.name != "profiles":
            raise _error("INVALID_PROFILE_DIRECTORY", "--profile-dir must explicitly name an absolute <home>/profiles/<name> directory; other layouts are not inferred.")
        name = validate_profile_name(supplied.name)
        if args.profile is not None and args.profile != name:
            raise _error("PROFILE_DIRECTORY_CONFLICT", "The profile name conflicts with --profile-dir. Confirm one home/profile pair.")
        home = _absolute_path(str(supplied.parent.parent))
        _safe_entry(home / "profiles", directory=True)
        _safe_entry(home / "profiles" / name, directory=True)
        if _absolute_path(str(supplied)) != home / "profiles" / name:
            raise _error("INVALID_PROFILE_DIRECTORY", "The profile directory resolves outside its stated home/profiles parent.")
        return home, name
    home = _absolute_path(args.home)
    if home.parent.name == "profiles":
        raise _error("HOME_IS_PROFILE_DIRECTORY", "--home expects the Harness home, not a profile directory. To split a full <home>/profiles/<name> path, explicitly use --profile-dir.")
    return home, validate_profile_name(args.profile if args.profile is not None else "sdk")


def _gitignore_bytes(project: Path) -> tuple[Path, bytes, bool]:
    path = project / ".gitignore"
    original = _read_regular(path, MAX_PROMPT_BYTES)
    original = b"" if original is None else original
    rule = b"/.dsh-invoker-profile/"
    # Git splits on LF, not on a bare CR. Any later negation (including !*)
    # can undo the directory rule, so conservatively put our rule last again.
    lines = [line.removesuffix(b"\r") for line in original.split(b"\n")]
    indices = [index for index, line in enumerate(lines) if line == rule]
    if indices and not any(line.startswith(b"!") for line in lines[indices[-1] + 1:]):
        return path, original, False
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    separator = b"" if not original or original.endswith(b"\n") else newline
    return path, original + separator + rule + newline, True


def configure(project: Path, args: argparse.Namespace) -> dict[str, Any]:
    # Fail before even creating the shared toolchain/binding directory.
    if not args.confirmed:
        raise _error("CONFIRMATION_REQUIRED", "Ask the user to confirm the absolute home and profile first, then pass --confirmed. This non-interactive helper cannot supply consent.")
    home, name = _configuration_target(args)
    detail = _check_selected_profile(home, name)
    desired = {"schema_version": SCHEMA_VERSION, "home": str(home), "profile": name}
    state = _state_path(project)
    try:
        existing = _load_binding(project)
    except InvokerError as error:
        if error.code != "INVALID_BINDING" or not args.reconfigure:
            raise
        existing = None
    if existing is not None and existing != desired and not args.reconfigure:
        raise _error("RECONFIGURE_REQUIRED", "A different confirmed binding already exists. Obtain confirmation for the change and use --reconfigure; environment changes do not replace it.")
    if existing == desired and not args.reconfigure:
        _binding_profile(project, existing)
    # A marker is a separate, nonsecret state file; links are never repairable.
    lifecycle = state / LIFECYCLE_FILENAME
    _safe_entry(lifecycle)
    ignore_path, ignore_data, ignore_changed = _gitignore_bytes(project)
    state.mkdir(exist_ok=True)
    _safe_entry(state, directory=True)
    if ignore_changed:
        _atomic_write(ignore_path, ignore_data, mode=0o644)
    changed = existing != desired
    if changed:
        _atomic_write(state / BINDING_FILENAME, (json.dumps(desired, ensure_ascii=True, indent=2) + "\n").encode("utf-8"))
    if changed or args.reconfigure:
        lifecycle.unlink(missing_ok=True)
    _record_initialized(project, desired)
    return {
        "ok": True, "action": "configure", "project_root": str(project),
        "binding_file": str(state / BINDING_FILENAME), "binding": {"state": "valid", **desired},
        "changed": changed, "gitignore_updated": ignore_changed,
        "profile": detail,
    }


def _positive_float(value: str) -> float:
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("A positive finite number is required.") from None


def _positive_int(value: str) -> int:
    try:
        number = int(value)
        if number <= 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("A positive integer is required.") from None


def _validate_session_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 200 or value in (".", "..") or any(not (character.isascii() and (character.isalnum() or character in "._:-")) for character in value):
        raise _error("INVALID_SESSION_ID", "Use an intentional, nonempty session identifier of letters, digits, dot, underscore, colon or hyphen; omit it for a new independent session.")
    return value


def _deadline_error() -> InvokerError:
    return _error("INVOCATION_DEADLINE", "The invocation deadline expired during input or SDK execution. Owned processes were stopped with a bounded cleanup grace; remote request state may be uncertain. No automatic retry was made.", 124)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _deadline_error()
    return remaining


def _bounded_input(source: Any, deadline: float) -> bytes | str:
    """Read at most 1 MiB + one byte/character without an unbounded pipe wait."""
    try:
        fd = source.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if os.name != "nt" and fd is not None:
        result = bytearray()
        while len(result) <= MAX_PROMPT_BYTES:
            readable, _, _ = select.select([fd], [], [], _remaining(deadline))
            if not readable:
                raise _deadline_error()
            block = os.read(fd, min(65_536, MAX_PROMPT_BYTES + 1 - len(result)))
            if not block:
                break
            result.extend(block)
        return bytes(result)
    # Windows pipes cannot be selected. A single bounded daemon read keeps the
    # command deadline effective even when its producer never closes stdin.
    # The CLI exits after the deadline; this is not a reusable background reader.
    completed: queue.Queue[Any] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            completed.put(source.read(MAX_PROMPT_BYTES + 1))
        except Exception as error:
            completed.put(error)

    threading.Thread(target=read, daemon=True, name="dsh-prompt-reader").start()
    try:
        value = completed.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise _deadline_error() from None
    if isinstance(value, Exception):
        raise value
    return value


def _read_prompt(args: argparse.Namespace, cwd: Path, *, deadline: float | None = None) -> str:
    deadline = time.monotonic() + getattr(args, "timeout_seconds", 300.0) if deadline is None else deadline
    try:
        _remaining(deadline)
        if args.prompt_file is not None:
            supplied = Path(args.prompt_file)
            path = (supplied if supplied.is_absolute() else cwd / supplied).resolve()
            # O_NONBLOCK closes the stat/open FIFO replacement race. fstat is
            # authoritative; FIFOs, devices, sockets and directories are refused.
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError
                raw = _bounded_input(source, deadline)
        else:
            if sys.stdin.isatty():
                raise ValueError
            # Pipes are always UTF-8 bytes, independent of the console codec.
            raw = _bounded_input(getattr(sys.stdin, "buffer", sys.stdin), deadline)
        prompt = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > MAX_PROMPT_BYTES or not prompt.strip() or "\x00" in prompt:
            raise ValueError
        _remaining(deadline)
        return prompt
    except (OSError, UnicodeError, ValueError, RuntimeError):
        raise _error("INVALID_PROMPT", "Supply a regular UTF-8 prompt file (relative paths use --cwd) or --stdin, with nonempty text no larger than 1 MiB. Input acquisition shares the invocation deadline; prompt data is never saved in the binding.") from None


def _privacy_patches(user_patches: list[str]) -> tuple[str, ...]:
    patches: list[str] = []
    for value in user_patches:
        patch = _absolute_path(value)
        if not patch.is_file():
            raise _error("INVALID_PATCH", "Each --patch must be an existing absolute file path.")
        if patch != PRIVACY_PATCH:
            patches.append(str(patch))
    try:
        if PRIVACY_PATCH.read_bytes().replace(b"\r\n", b"\n") != PRIVACY_PATCH_BYTES:
            raise ValueError
    except (OSError, ValueError):
        raise _error("INVALID_PRIVACY_PATCH", "The bundled session-log privacy patch is missing or modified. Restore this skill before running; no SDK request was made.") from None
    return (*patches, str(PRIVACY_PATCH))


def _validate_request(request: dict[str, Any]) -> tuple[dict[str, Any], str, str | None]:
    keys = {"project_root", "cwd", "prompt", "session_id", "patches", "provider", "model", "max_tokens", "request_timeout_seconds"}
    if not isinstance(request, dict) or set(request) not in (keys, keys | {"binding"}):
        raise _error("INVALID_WORKER_REQUEST", "The owned worker received an invalid request.")
    project = _absolute_path(request["project_root"], directory=True)
    binding = _load_binding(project)
    if binding is None:
        raise _error("BINDING_REQUIRED", "Discover candidates and obtain home/profile confirmation, then configure this project before invocation.")
    # First validation freezes the selection for in-process callers too. The
    # worker gate requires the snapshot explicitly; it never invents a selection.
    if "binding" not in request:
        request["binding"] = dict(binding)
    if request["binding"] != binding:
        raise _error("BINDING_CHANGED", "The confirmed home/profile changed after this invocation selected it. Recheck status and intentionally invoke again; no task was sent to the replacement binding.")
    _binding_profile(project, binding)
    cwd = _absolute_path(request["cwd"], directory=True)
    prompt = request["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise _error("INVALID_PROMPT", "The worker requires a nonempty UTF-8 prompt no larger than 1 MiB.")
    if not isinstance(request["patches"], list) or any(not isinstance(item, str) for item in request["patches"]):
        raise _error("INVALID_PATCH", "SDK patches must be absolute file paths.")
    for option in ("provider", "model"):
        value = request[option]
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(character) < 32 for character in value):
            raise _error("INVALID_MODEL_OPTION", "Provider and model must be nonempty names without control characters.")
    max_tokens = request["max_tokens"]
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
        raise _error("INVALID_MODEL_OPTION", "--max-tokens must be a positive integer.")
    timeout = request["request_timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise _error("INVALID_TIMEOUT", "The SDK request timeout must be positive and finite; it is not a whole-turn deadline.")
    options: dict[str, Any] = {
        "dsh_home": binding["home"], "profile": binding["profile"], "cwd": str(cwd),
        "patches": _privacy_patches(request["patches"]),
        "provider": request["provider"], "model": request["model"],
        "request_timeout_seconds": timeout,
        "initialize_timeout_seconds": min(30.0, timeout), "shutdown_timeout_seconds": 1.0,
        "env": {"DSH_RUNTIME_MODE": "exe"},
    }
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    return options, prompt, _validate_session_id(request["session_id"])


def _checked_result(result: Any) -> dict[str, str]:
    session_id = result.session_id
    response = result.final_response
    reason = result.finish_reason
    if not isinstance(session_id, str) or not session_id or not isinstance(response, str) or reason not in ("completed", "error", "max-tokens"):
        raise _error("INVALID_SDK_RESULT", "The SDK returned no recognized completed/error/max-tokens result. No raw events or diagnostics are exposed.", 5)
    return {"session_id": session_id, "finish_reason": reason, "final_response": response}


def _unsupported_continuation() -> InvokerError:
    # Released 0.1.5rc1 SDK server accepts only initialize/session/prompt/shutdown;
    # session/prompt creates a session, it does not restore persisted history.
    return _error("UNSUPPORTED_CONTINUATION", "SDK/runtime 0.1.5rc1 cannot restore a persisted session in a fresh process. --session-id is refused before any SDK request. Omit it for a new independent task, or obtain approval for a separately supported integration; no CLI/profile fallback is used.", 3)


def _execute_sdk(request: dict[str, Any]) -> dict[str, str]:
    options, prompt, session_id = _validate_request(request)
    if session_id is not None:
        raise _unsupported_continuation()
    _require_sdk()
    # The published launch resolver reads os.environ, not HarnessConfig.env.
    # This function runs in the disposable worker, never the caller's shell.
    os.environ["DSH_RUNTIME_MODE"] = "exe"
    try:
        factory = importlib.import_module("deepseek_harness").DeepSeekHarness
    except Exception:
        raise _error("SDK_IMPORT_FAILED", "The pinned project SDK could not be imported. Repair the project toolchain; no global or CLI fallback is used.", 3) from None
    try:
        instance = factory(**options)
        entered = False
        try:
            with instance as harness:
                entered = True
                project = Path(request["project_root"])
                _record_initialized(project, request["binding"])
                result = _checked_result(harness.run(prompt))
        finally:
            if not entered:
                try:
                    instance.close()
                except Exception:
                    pass
        return result
    except InvokerError:
        raise
    except TimeoutError:
        raise _error("SDK_REQUEST_TIMEOUT", "An SDK RPC timed out. This is not a complete-turn timeout; the owned-worker watchdog also bounds the invocation. Check profile, network and approvals without increasing permissions.", 5) from None
    except Exception:
        raise _error("SDK_FAILURE", "The SDK could not complete. Check the confirmed profile, authentication, network and SDK approval requirements in your own terminal. Sensitive exceptions and runtime stderr are intentionally not echoed; no retry or fallback was attempted.", 5) from None


def _worker_main() -> int:
    # A private duplicate preserves the one-result protocol even if the SDK or a
    # native dependency writes directly to stdout/stderr. It is not inherited.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8") as output:
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
            os.dup2(sink.fileno(), sys.stderr.fileno())
        try:
            raw = sys.stdin.buffer.read(MAX_WORKER_BYTES + 1)
            if len(raw) > MAX_WORKER_BYTES:
                raise ValueError
            request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
            if not isinstance(request, dict) or "binding" not in request:
                raise _error("INVALID_WORKER_REQUEST", "The owned worker requires the parent's validated binding snapshot.")
            document: dict[str, Any] = _execute_sdk(request)
            code = 0 if document["finish_reason"] == "completed" else 4
        except InvokerError as error:
            document, code = _error_document("invoke", error), error.exit_code
        except Exception:
            document, code = _error_document("invoke", _error("WORKER_FAILURE", "The owned worker failed before producing a valid result; sensitive diagnostics were suppressed.", 5)), 5
        output.write(json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n")
        output.flush()
    return code


class _WindowsJob:
    """Kill-on-close job assigned while the worker is blocked on its stdin gate."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise _error("PROCESS_OWNERSHIP_FAILED", "Windows could not create an owned process job. Stop; do not run the SDK without an outer process-tree watchdog.", 5)
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                raise _error("PROCESS_OWNERSHIP_FAILED", "Windows could not own the worker process tree. No SDK request was released; use a supported outer process-tree watchdog.", 5)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _PosixTree:
    """Track gated worker ancestry and creation identities, not process names.

    Polling is not kernel containment: an adversarial setsid+double-fork between
    samples can escape. Long-lived detached children and native shell groups are
    tracked. Windows instead uses a non-breakaway kill-on-close Job.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.seen: dict[int, tuple[int, ...]] = {}
        self.lib = None
        if sys.platform == "darwin":
            import ctypes

            class BsdInfo(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint32) for name in (
                    "flags", "status", "xstatus", "pid", "ppid", "uid", "gid",
                    "ruid", "rgid", "svuid", "svgid", "reserved",
                )] + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
                    (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")
                ] + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]

            self.ctypes, self.info_type = ctypes, BsdInfo
            self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.lib.proc_listpids.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
            self.lib.proc_listpids.restype = ctypes.c_int
            self.lib.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
            self.lib.proc_pidinfo.restype = ctypes.c_int
        elif not sys.platform.startswith("linux"):
            raise _error("PROCESS_OWNERSHIP_FAILED", "Owned ancestry tracking requires macOS libproc or Linux procfs. No SDK request was released.", 5)
        self.sample()
        if process.pid not in self.seen:
            raise _error("PROCESS_OWNERSHIP_FAILED", "The gated worker's creation identity could not be verified. No SDK request was released.", 5)

    def _row(self, pid: int) -> tuple[int, int, bool, tuple[int, ...]] | None:
        try:
            if self.lib is not None:
                info = self.info_type()
                size = self.ctypes.sizeof(info)
                if self.lib.proc_pidinfo(pid, 3, 0, self.ctypes.byref(info), size) != size:
                    return None
                return info.ppid, info.pgid, info.status == 5, (info.start_sec, info.start_usec)
            # /proc stat's comm may contain spaces and parentheses.
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return int(fields[1]), int(fields[2]), fields[0] == "Z", (int(fields[19]),)
        except (OSError, ValueError, IndexError):
            return None

    def _snapshot(self) -> dict[int, tuple[int, int, bool, tuple[int, ...]]]:
        if self.lib is not None:
            size = self.lib.proc_listpids(1, 0, None, 0)
            if size <= 0:
                raise OSError("process enumeration unavailable")
            entries = (self.ctypes.c_int * (size // 4 + 1024))()
            used = self.lib.proc_listpids(1, 0, entries, self.ctypes.sizeof(entries))
            if used <= 0:
                raise OSError("process enumeration unavailable")
            pids = list(entries[:used // 4])
        else:
            pids = [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdecimal()]
        return {pid: row for pid in pids if pid > 0 and (row := self._row(pid)) is not None}

    def sample(self) -> dict[int, tuple[int, int, bool, tuple[int, ...]]]:
        rows = self._snapshot()
        owned = {pid for pid, identity in self.seen.items() if pid in rows and rows[pid][3] == identity}
        if not self.seen and self.process.poll() is None and self.process.pid in rows:
            owned.add(self.process.pid)
        # Reparented original-group members remain ours only while a known
        # creation identity still anchors that group; never claim a recycled PGID.
        group_owned = any(rows[pid][1] == self.process.pid for pid in owned)
        while True:
            added = {pid for pid, row in rows.items() if pid not in owned and (
                row[0] in owned or (group_owned and row[1] == self.process.pid)
            )}
            if not added:
                break
            owned.update(added)
        for pid in owned:
            self.seen[pid] = rows[pid][3]
        return {pid: rows[pid] for pid in owned if not rows[pid][2]}

    def cleanup(self) -> None:
        failed = False
        for sig, grace in ((signal.SIGTERM, 0.15), (signal.SIGKILL, 2.0)):
            living = self.sample()
            for pid, row in reversed(list(living.items())):
                # Recheck the exact creation identity at signalling, not merely
                # kill(pid, 0). Linux pidfds additionally close the check/kill race.
                pidfd = None
                try:
                    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                        pidfd = os.pidfd_open(pid)
                    current = self._row(pid)
                    if current is None or current[3] != row[3]:
                        continue
                    if pidfd is not None:
                        signal.pidfd_send_signal(pidfd, sig)
                    else:
                        os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except OSError:
                    failed = True
                finally:
                    if pidfd is not None:
                        os.close(pidfd)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                self.process.poll()
                if not self.sample():
                    break
                time.sleep(0.02)
        if self.sample() or failed:
            raise _error("PROCESS_CLEANUP_FAILED", "The OS blocked cleanup or an observed owned descendant survived. Use your approved process controls before another invocation; no retry was made.", 5)


class _Termination(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


@contextlib.contextmanager
def _termination_handlers():
    previous = {}

    def terminate(signum: int, _frame: Any) -> None:
        raise _Termination(signum)

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, terminate)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _stop_owned_worker(process: subprocess.Popen[bytes], job: _WindowsJob | None, *, released: bool, tree: _PosixTree | None = None) -> None:
    cleanup_failed = False
    try:
        if os.name == "nt" and job is not None:
            job.close()
        elif tree is not None:
            tree.cleanup()
        elif process.poll() is None:
            process.kill()  # no gate release without established ancestry/job
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    except OSError:
        cleanup_failed = True
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
    if cleanup_failed:
        raise _error("PROCESS_CLEANUP_FAILED", "The OS blocked owned process-tree cleanup. A runtime descendant might remain; stop it using your own approved process controls before another invocation. No automatic retry was made.", 5)


def _probe_process_group(*, cwd: Path, environment: dict[str, str]) -> None:
    """Verify real group signalling, not just kill(pid, 0), before SDK launch.

    A restrictive runner can permit process lookup but deny group termination.
    The probe contains stdlib code only and cannot start a runtime or request.
    """
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(cwd), env=environment, start_new_session=True,
    )
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            raise _error("PROCESS_OWNERSHIP_FAILED", "The OS denied process-group cleanup support. No SDK worker or request was started; use an approved runner with process-tree cleanup support.", 5) from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if process.stdin is not None:
            process.stdin.close()


def _collect_worker(process: subprocess.Popen[bytes], payload: bytes, deadline: float, tree: _PosixTree | None) -> bytes:
    """Bound pipe buffers while sampling ownership, including a blocked writer."""
    chunks: queue.Queue[Any] = queue.Queue(maxsize=2)
    stop = threading.Event()
    writer_error: list[Exception] = []

    def read() -> None:
        try:
            while not stop.is_set():
                block = process.stdout.read1(65_536)
                while not stop.is_set():
                    try:
                        chunks.put(block, timeout=0.025)
                        break
                    except queue.Full:
                        pass
                if not block:
                    return
        except Exception as error:
            if not stop.is_set():
                chunks.put(error)

    def write() -> None:
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError) as error:
            writer_error.append(error)

    reader = threading.Thread(target=read, daemon=True, name="dsh-worker-output")
    writer = threading.Thread(target=write, daemon=True, name="dsh-worker-input")
    output = bytearray()
    reader.start()
    writer.start()
    try:
        eof = False
        while True:
            if tree is not None:
                tree.sample()
            remaining = _remaining(deadline)
            if eof:
                # Capture the final ancestry snapshot before reaping the leader.
                try:
                    process.wait(timeout=min(0.025, remaining))
                except subprocess.TimeoutExpired:
                    continue
                break
            try:
                block = chunks.get(timeout=min(0.025, remaining))
            except queue.Empty:
                continue
            if isinstance(block, Exception):
                raise block
            if not block:
                eof = True
            elif len(output) + len(block) > MAX_WORKER_BYTES:
                raise ValueError("worker output exceeds byte limit")
            else:
                output.extend(block)
        if writer_error:
            raise ValueError("worker input pipe failed")
        return bytes(output)
    finally:
        stop.set()
        # Forced process cleanup runs next and unblocks pipe reads/writes. These
        # bounded daemon threads cannot buffer more than two 64-KiB chunks.


def _supervise(command: list[str], request: dict[str, Any], *, cwd: Path, environment: dict[str, str], timeout: float) -> tuple[dict[str, Any], int]:
    with _termination_handlers():
        return _supervise_owned(command, request, cwd=cwd, environment=environment, timeout=timeout)


def _supervise_owned(command: list[str], request: dict[str, Any], *, cwd: Path, environment: dict[str, str], timeout: float) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + timeout
    if os.name != "nt":
        _probe_process_group(cwd=cwd, environment=environment)
    process: subprocess.Popen[bytes] | None = None
    job: _WindowsJob | None = None
    tree: _PosixTree | None = None
    released = False
    try:
        _remaining(deadline)
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(cwd), env=environment,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        if os.name == "nt":
            job = _WindowsJob(process)
        else:
            tree = _PosixTree(process)  # identity/ancestry established before gate
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        released = True
        output = _collect_worker(process, payload, deadline, tree)
        document = json.loads(output.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if process.returncode in (0, 4):
            if not isinstance(document, dict) or set(document) != {"session_id", "finish_reason", "final_response"}:
                raise ValueError
            class Result:
                pass
            result = Result()
            result.session_id, result.finish_reason, result.final_response = document["session_id"], document["finish_reason"], document["final_response"]
            checked = _checked_result(result)
            if (checked["finish_reason"] == "completed") != (process.returncode == 0):
                raise ValueError
            return checked, process.returncode
        if isinstance(document, dict) and set(document) == {"ok", "action", "error"} and document.get("ok") is False and document.get("action") == "invoke":
            code = document.get("error", {}).get("code") if isinstance(document.get("error"), dict) else None
            allowed = _worker_errors().get(code)
            if allowed is not None and process.returncode == allowed.exit_code:
                raise allowed
        raise ValueError
    except InvokerError:
        raise
    except (OSError, ValueError, UnicodeError, TypeError, AttributeError):
        raise _error("WORKER_FAILURE", "The owned worker failed or returned an invalid result. Its diagnostics were suppressed and its observed owned descendants were stopped; no retry was made.", 5) from None
    finally:
        if process is not None:
            # Further termination signals must not interrupt forced cleanup.
            with _ignore_termination():
                _stop_owned_worker(process, job, released=released, tree=tree)


@contextlib.contextmanager
def _ignore_termination():
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _worker_errors() -> dict[str, InvokerError]:
    # Never forward a worker-provided message, even if it looks like JSON.
    return {
        "SDK_NOT_READY": _error("SDK_NOT_READY", "The worker requires both pinned SDK/runtime packages at 0.1.5rc1; repair the project toolchain.", 3),
        "SDK_IMPORT_FAILED": _error("SDK_IMPORT_FAILED", "The worker could not import the pinned SDK; repair the project toolchain.", 3),
        "SDK_REQUEST_TIMEOUT": _error("SDK_REQUEST_TIMEOUT", "An SDK RPC timed out. The owned process tree was cleaned up; no retry or permission escalation was attempted.", 5),
        "SDK_FAILURE": _error("SDK_FAILURE", "The SDK could not complete. Check the confirmed profile, credentials, network and SDK approval requirements in your own terminal. Sensitive diagnostics were suppressed; no retry or fallback was attempted.", 5),
        "INVALID_SDK_RESULT": _error("INVALID_SDK_RESULT", "The SDK returned no recognized completed/error/max-tokens result. No raw events or diagnostics are exposed.", 5),
        "WORKER_FAILURE": _error("WORKER_FAILURE", "The worker failed before producing a valid SDK result; sensitive diagnostics were suppressed.", 5),
        "UNSUPPORTED_CONTINUATION": _unsupported_continuation(),
        **{code: _error(code, "The binding, profile, prompt or invocation options changed or became invalid before the worker started. Recheck status and obtain any necessary confirmation; no fallback was attempted.") for code in ("BINDING_REQUIRED", "BINDING_CHANGED", "STALE_BINDING", "INVALID_LIFECYCLE", "INVALID_BINDING", "INVALID_PATH", "UNSAFE_STATE_PATH", "INVALID_PROFILE", "NON_SDK_PROFILE", "MISSING_CUSTOM_PROFILE", "INVALID_PROMPT", "INVALID_PATCH", "INVALID_PRIVACY_PATCH", "INVALID_MODEL_OPTION", "INVALID_TIMEOUT", "INVALID_SESSION_ID", "INVALID_WORKER_REQUEST")},
    }


def invoke(project: Path, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    with _termination_handlers():
        return _invoke_bounded(project, args)


def _invoke_bounded(project: Path, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + args.timeout_seconds
    cwd = _absolute_path(args.cwd, directory=True)
    if _validate_session_id(args.session_id) is not None:
        raise _unsupported_continuation()
    binding = _load_binding(project)
    if binding is None:
        raise _error("BINDING_REQUIRED", "Discover candidates and obtain home/profile confirmation, then configure this project before invocation.")
    _binding_profile(project, binding)
    prompt = _read_prompt(args, cwd, deadline=deadline)
    request = {
        "project_root": str(project), "binding": binding, "cwd": str(cwd), "prompt": prompt,
        "session_id": None, "patches": args.patch or [],
        "provider": args.provider, "model": args.model, "max_tokens": args.max_tokens,
        "request_timeout_seconds": args.request_timeout_seconds,
    }
    _validate_request(request)
    _require_sdk()
    state = _state_path(project)
    temporary = state / "tmp"
    _safe_entry(temporary, directory=True)
    temporary.mkdir(exist_ok=True)
    _safe_entry(temporary, directory=True)
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"})
    return _supervise(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "_worker"], request,
        cwd=state, environment=environment, timeout=_remaining(deadline),
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally echoes the entire invalid argument, potentially a key.
        raise _error("INVALID_ARGUMENTS", "Invalid arguments. Use --help for the interface; values are intentionally not echoed.")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    for action in ("discover", "status", "configure", "invoke"):
        subparser = subparsers.add_parser(action)
        subparser.add_argument("--project-root", required=True)
        if action == "configure":
            source = subparser.add_mutually_exclusive_group(required=True)
            source.add_argument("--home")
            source.add_argument("--profile-dir")
            subparser.add_argument("--profile", default=None)
            subparser.add_argument("--confirmed", action="store_true")
            subparser.add_argument("--reconfigure", action="store_true")
        elif action == "invoke":
            subparser.add_argument("--cwd", required=True)
            prompt = subparser.add_mutually_exclusive_group(required=True)
            prompt.add_argument("--prompt-file")
            prompt.add_argument("--stdin", action="store_true")
            subparser.add_argument("--session-id", help="Unsupported across fresh runtimes in 0.1.5rc1; fails before SDK startup.")
            subparser.add_argument("--patch", action="append")
            subparser.add_argument("--provider", default="deepseek-official")
            subparser.add_argument("--model", default="deepseek-v4-flash")
            subparser.add_argument("--max-tokens", type=_positive_int)
            subparser.add_argument("--request-timeout-seconds", type=_positive_float, default=60.0, help="Per-RPC timeout, not a turn limit (default: 60).")
            subparser.add_argument("--timeout-seconds", type=_positive_float, default=300.0, help="Input + SDK operation deadline; bounded forced-cleanup grace follows expiry (default: 300).")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["_worker"]:
        return _worker_main()
    action = argv[0] if argv and argv[0] in ("discover", "status", "configure", "invoke") else "arguments"
    try:
        args = build_parser().parse_args(argv)
        project = _absolute_path(args.project_root, directory=True)
        if args.action == "discover":
            document, code = discover(project), 0
        elif args.action == "status":
            document, code = status(project), 0
        elif args.action == "configure":
            document, code = configure(project, args), 0
        else:
            document, code = invoke(project, args)
        _emit(document)
        return code
    except InvokerError as error:
        _emit(_error_document(action, error))
        return error.exit_code
    except _Termination as termination:
        code = 128 + termination.signum
        _emit(_error_document(action, _error("INTERRUPTED", "Invocation terminated; observed owned descendants were stopped. Remote request state may be uncertain; no automatic retry was made.", code)))
        return code
    except KeyboardInterrupt:
        _emit(_error_document(action, _error("INTERRUPTED", "Invocation interrupted; observed owned descendants were stopped. No automatic retry was made.", 130)))
        return 130
    except Exception:
        _emit(_error_document(action, _error("IO_OR_STATE_FAILURE", "The requested operation failed. Check directory access and state integrity in your own terminal; sensitive diagnostics were suppressed.", 5)))
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
