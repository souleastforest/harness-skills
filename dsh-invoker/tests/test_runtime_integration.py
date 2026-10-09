"""Keyless integration tests for the published SDK and executable runtime.

Ordinary unit-test discovery explicitly skips this class when the pinned wheels
are unavailable. CI sets DSH_INVOKER_REQUIRE_RUNTIME=1: a missing wheel, wrong
version, missing sidecar, or unavailable native shell is then a failure, not a
skip. No dependency is installed by this file.

All provider traffic goes to a disposable 127.0.0.1 OpenAI chat-completions SSE
fixture. The skill's default ``sdk`` profile is tested without changing its
approval policy. The separately named ``sdk-minimal`` test is an explicit,
disposable upstream-runtime shell test, NEVER a production fallback.

Protocol references are pinned to dsh-v0.1.5-rc.1, especially
scripts/smoke-python-runtime.py and python/sdk/src/deepseek_harness/client.py.
This file is test infrastructure, not another production SDK client.
"""
from __future__ import annotations

import ctypes
import importlib.metadata
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SDK_VERSION = "0.1.5rc1"
SKILL_DIR = Path(__file__).resolve().parents[1]
HELPER = SKILL_DIR / "scripts" / "dsh_invoker.py"
PRIVACY_PATCH = SKILL_DIR / "assets" / "no-session-log-upload.patch.yml"
DUMMY_KEY = "sk-dsh-ci-dummy-not-a-real-key"
PRIVATE_ERROR = "DSH_CI_PRIVATE_PROVIDER_ERROR_MUST_NOT_BE_PRINTED"
FINAL_TEXT = "DSH_CI_FINAL_OK"
FIRST_PROMPT = "DSH_CI_FIRST_USER_HISTORY_873: remember this marker; no tools."
FOLLOWUP_PROMPT = "DSH_CI_FOLLOWUP_USER_HISTORY_642: use the earlier marker; no tools."
FIRST_RESPONSE = "DSH_CI_FIRST_ASSISTANT_HISTORY_359"
FOLLOWUP_RESPONSE = "DSH_CI_SECOND_ASSISTANT_HISTORY_126"
SHELL_TEXT = "DSH_CI_PERSISTENT_SHELL_OK"
SHELL_TOOL = "pwsh" if os.name == "nt" else "bash"
# Public failure metadata is literal/source-derived only. Never include exception
# messages, assertion values, paths, HTTP bodies, environment or raw child output.
COMMAND_STAGES = frozenset(("native-command", "helper-configure", "helper-invoke", "lifecycle", "continuation", "restore-methods", "shell"))
PROCESS_STAGES = frozenset(("create", "assign", "communicate", "cleanup", "complete"))
FIXTURE_STAGES = frozenset((
    "imports", "runtime-resolve", "client-enter", "initialize", "harness-enter",
    "first-turn", "second-turn", "restore-request", "shell-turn", "shutdown", "result",
))
EXCEPTION_TYPES = frozenset((
    "AssertionError", "NativeCommandFailure", "RuntimeError", "ValueError", "TypeError", "TimeoutError", "OSError",
    "FileNotFoundError", "PermissionError", "ImportError", "ModuleNotFoundError", "TimeoutExpired",
    "JsonRpcError", "TransportClosedError", "SdkProtocolError", "ValidationError", "JSONDecodeError", "KeyError", "AttributeError",
    "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError",
    "HarnessError", "HarnessTimeoutError", "HarnessProcessError", "HarnessProtocolError",
))
HELPER_ERROR_CODES = frozenset((
    "SDK_NOT_READY", "SDK_IMPORT_FAILED", "SDK_FAILURE", "SDK_REQUEST_TIMEOUT", "INVALID_SDK_RESULT",
    "PROCESS_OWNERSHIP_FAILED", "PROCESS_CLEANUP_FAILED", "WORKER_FAILURE", "UNSUPPORTED_CONTINUATION",
    "BINDING_REQUIRED", "BINDING_CHANGED", "STALE_BINDING", "INVALID_LIFECYCLE", "INVALID_BINDING",
    "INVALID_PATH", "UNSAFE_STATE_PATH", "INVALID_PROFILE", "NON_SDK_PROFILE", "MISSING_CUSTOM_PROFILE",
    "INVALID_PROMPT", "INVALID_PATCH", "INVALID_PRIVACY_PATCH", "INVALID_MODEL_OPTION", "INVALID_TIMEOUT",
    "INVALID_SESSION_ID", "INVALID_WORKER_REQUEST", "INVOCATION_DEADLINE", "IO_OR_STATE_FAILURE",
))
RPC_ERROR_CODES = frozenset((-32601, -32603))
FIXTURE_FAILURE_CATEGORIES = frozenset(("loader-settlement", "initialize-parameters", "model-resolution", "unknown"))
# dsh-v0.1.5-rc.1 packages/sdk/server/src/index.ts awaits Loader BEFORE
# server.ts initialize parameter validation / LLM model resolution. Both paths
# become -32603 in packages/sdk/protocol/src/transport.ts; the RPC code alone
# cannot identify a cause. Match only these exact pinned-source messages, never
# forward the message, structured data, or appended SDK runtime diagnostics.
_INITIALIZE_FAILURE_CATEGORIES = {
    "loader fibers failed": "loader-settlement",  # vendor/loader/src/config/tree.ts
    "initialize reasoningEffort must be a non-empty string": "initialize-parameters",
    "initialize maxTokens must be a positive safe integer": "initialize-parameters",
    'no adapter registered for provider "deepseek-official"': "model-resolution",
    'adapter returned invalid exact model metadata for provider "deepseek-official" model "ci-mock-model"': "model-resolution",
    'adapter returned invalid context metadata for provider "deepseek-official" model "ci-mock-model"': "model-resolution",
}
# EntryTree.await() rethrows ONE Entry._await() failure verbatim; entry.ts
# updateError() wraps it as "failed to <stage> loader entry <id> (<name>): <detail>".
# These module names are literal rows in the pinned base/sdk-minimal bundles,
# not module names or entry ids copied from a runtime error.
LOADER_FAILURE_STAGES = frozenset(("import", "apply", "dispose", "rollback"))
LOADER_FAILURE_MODULES = frozenset("""
@deepseek-ai/cordis-plugin-timer
@deepseek-ai/cordis-plugin-hmr
@deepseek-ai/dsh-llm
@deepseek-ai/dsh-deepseek-llm-api-extensions
@deepseek-ai/dsh-session
@deepseek-ai/dsh-session-log-deepseek
@deepseek-ai/dsh-typert-registry
@deepseek-ai/dsh-typert-loader
@deepseek-ai/dsh-api-gateway
@deepseek-ai/dsh-session-title
@deepseek-ai/dsh-session-title-first-prompt-llm
@deepseek-ai/dsh-user-questions
@deepseek-ai/dsh-agent
@deepseek-ai/dsh-plugin-package-inventory-deepseek
@deepseek-ai/dsh-agent-default-model
@deepseek-ai/dsh-jobs-local
@deepseek-ai/dsh-llm-retry
@deepseek-ai/dsh-settings-file
@deepseek-ai/dsh-credentials-local
@deepseek-ai/dsh-llm-pi-ai
@deepseek-ai/dsh-session-persistence-jsonl
@deepseek-ai/dsh-attachment-local
@deepseek-ai/dsh-session-query-sqlite
@deepseek-ai/dsh-session-projection
@deepseek-ai/dsh-storage
@deepseek-ai/dsh-storage-json
@deepseek-ai/dsh-storage-domain
@deepseek-ai/dsh-session-projection-cache
@deepseek-ai/dsh-session-telemetry-otel
@deepseek-ai/dsh-subprocess-local
@deepseek-ai/dsh-sandbox-local
@deepseek-ai/dsh-sandbox-policy
@deepseek-ai/dsh-bash-sandbox
@deepseek-ai/dsh-pwsh-sandbox
@deepseek-ai/dsh-user-approval
@deepseek-ai/dsh-permission-presets
@deepseek-ai/dsh-shell-env
@deepseek-ai/dsh-tool-bash
@deepseek-ai/dsh-tool-pwsh
@deepseek-ai/dsh-tool-jobs
@deepseek-ai/dsh-fs-observation-policy
@deepseek-ai/dsh-tool-fs
@deepseek-ai/dsh-tool-fs-search
@deepseek-ai/dsh-agent-instructions
@deepseek-ai/dsh-skill
@deepseek-ai/dsh-skill-filesystem
@deepseek-ai/dsh-skill-badge
@deepseek-ai/dsh-tool-skill
@deepseek-ai/dsh-commands
@deepseek-ai/dsh-command-feedback
@deepseek-ai/dsh-goal
@deepseek-ai/dsh-goal-round-driver
@deepseek-ai/dsh-command-goal
@deepseek-ai/dsh-plan-mode
@deepseek-ai/dsh-token-meter
@deepseek-ai/dsh-compaction-basic
@deepseek-ai/dsh-command-compact
@deepseek-ai/dsh-subagent
@deepseek-ai/dsh-subagent-spawn-in-process
@deepseek-ai/dsh-subagent-fork-in-process
@deepseek-ai/dsh-tool-subagent-control
@deepseek-ai/dsh-tool-subagent-control/list-agents
@deepseek-ai/dsh-tool-subagent
@deepseek-ai/dsh-workflow-worker-thread
@deepseek-ai/dsh-tool-workflow
@deepseek-ai/dsh-tool-call-timeout-policy
@deepseek-ai/dsh-spill-local
@deepseek-ai/dsh-spill-policy
@deepseek-ai/dsh-session-checkpoint-policy
@deepseek-ai/dsh-compaction-tool-result-pruner
@deepseek-ai/dsh-tool-todo
@deepseek-ai/dsh-tool-goal
@deepseek-ai/dsh-tool-ralph
@deepseek-ai/dsh-repeat-tool-reminder
@deepseek-ai/dsh-web
@deepseek-ai/dsh-web-search-deepseek
@deepseek-ai/dsh-web-fetch-http
@deepseek-ai/dsh-tool-web
@deepseek-ai/dsh-tools
@deepseek-ai/dsh-system-prompt
@deepseek-ai/dsh-agent-loop
@deepseek-ai/dsh-fs-sandbox
@deepseek-ai/dsh-llm-deepseek
@deepseek-ai/dsh-sdk-app
@deepseek-ai/dsh-sdk-jsonrpc-server
@deepseek-ai/dsh-terminal
@deepseek-ai/dsh-terminal-bash
@deepseek-ai/dsh-tool-bash-persistent
@deepseek-ai/dsh-tool-pwsh-persistent
""".split())
LOADER_CAUSE_TOKENS = {
    "native-addon": frozenset(("WIN32_MODULE_NOT_FOUND", "WIN32_PROCEDURE_NOT_FOUND", "WIN32_BAD_EXE_FORMAT",
                               "WIN32_DLL_INIT_FAILED", "MODULE_DID_NOT_SELF_REGISTER")),
    "os": frozenset(("ENOENT", "EACCES", "EPERM", "ENOTDIR", "EINVAL")),
    "import": frozenset(("MODULE_NOT_FOUND", "ERR_MODULE_NOT_FOUND", "ERR_UNSUPPORTED_ESM_URL_SCHEME")),
    "shell": frozenset(("PWSH_NOT_FOUND", "PWSH_INVALID_CONFIG")),
    "unknown": frozenset(),
}
LOADER_DIAGNOSTIC_FIELDS = ("loader_stage", "loader_module", "loader_cause", "loader_token")
_LOADER_WRAPPER = re.compile(
    r"failed to (import|apply|dispose|rollback) loader entry [A-Za-z0-9_.:-]{1,128} "
    r"\((@[A-Za-z0-9_./-]{1,128})\): (.+)"
)
_LOADER_NATIVE_SIGNATURES = {
    "The specified module could not be found.": "WIN32_MODULE_NOT_FOUND",
    "The specified procedure could not be found.": "WIN32_PROCEDURE_NOT_FOUND",
    "%1 is not a valid Win32 application.": "WIN32_BAD_EXE_FORMAT",
    "A dynamic link library (DLL) initialization routine failed.": "WIN32_DLL_INIT_FAILED",
}
_LOADER_OS_SIGNATURES = {
    "ENOENT": "no such file or directory", "EACCES": "permission denied", "EPERM": "operation not permitted",
    "ENOTDIR": "not a directory", "EINVAL": "invalid argument",
}


def loader_cause_signature(detail: str) -> tuple[str, str | None]:
    """Match complete fixed error shapes; variable paths are inputs, never output."""
    native = _LOADER_NATIVE_SIGNATURES.get(detail)
    if native is not None:
        return "native-addon", native
    if re.fullmatch(r"Module did not self-register: '[^']{1,4096}'\.", detail):
        return "native-addon", "MODULE_DID_NOT_SELF_REGISTER"
    for code, signature in _LOADER_OS_SIGNATURES.items():
        if re.fullmatch(re.escape(code + ": " + signature)
                        + r", (?:open|read|write|stat|lstat|access|mkdir|rmdir|scandir|unlink|readlink|chmod) '[^']{1,4096}'", detail):
            return "os", code
    if detail == "spawn pwsh ENOENT":
        return "shell", "PWSH_NOT_FOUND"
    if re.fullmatch(r"pwsh-local: (?:timeoutMs|maxTimeoutMs|maxOutputBytes|maxSpillBytes|graceMs) must be a positive finite number", detail):
        return "shell", "PWSH_INVALID_CONFIG"
    if re.fullmatch(r"Cannot find module '[^']{1,4096}'", detail):
        return "import", "MODULE_NOT_FOUND"
    if re.fullmatch(r"Cannot find (?:module|package) '[^']{1,4096}' imported from [^\r\n]{1,4096}", detail):
        return "import", "ERR_MODULE_NOT_FOUND"
    if re.fullmatch(re.escape("Only URLs with a scheme in: file, data, and node are supported by the default ESM loader. "
                              "On Windows, absolute paths must be valid file:// URLs. Received protocol ")
                    + r"'[A-Za-z]:'", detail):
        return "import", "ERR_UNSUPPORTED_ESM_URL_SCHEME"
    return "unknown", None


def loader_failure_fields(value: dict[str, Any]) -> dict[str, str]:
    """Reconstruct a coherent allowlisted tuple at every diagnostic boundary."""
    stage, module, cause, token = (value.get(field) for field in LOADER_DIAGNOSTIC_FIELDS)
    if (type(stage) is not str or stage not in LOADER_FAILURE_STAGES
            or type(module) is not str or module not in LOADER_FAILURE_MODULES
            or type(cause) is not str or cause not in LOADER_CAUSE_TOKENS):
        return {}
    safe = {"loader_stage": stage, "loader_module": module, "loader_cause": cause}
    if cause == "unknown":
        return safe if "loader_token" not in value else {}
    if type(token) is not str or token not in LOADER_CAUSE_TOKENS[cause]:
        return {}
    return {**safe, "loader_token": token}


def loader_entry_failure_diagnostic(message: str) -> dict[str, str]:
    """Parse just the pinned wrapper's first line, never SDK-appended stderr."""
    if len(message) > 64 * 1024:
        return {}
    line, newline, _tail = message.partition("\n")
    # Windows native loader errors may end their complete first line in CRLF.
    # A CR anywhere else (or a header split over lines) remains malformed.
    if newline and line.endswith("\r"):
        line = line[:-1]
    if any(ord(character) < 32 or ord(character) == 127 for character in line):
        return {}
    match = _LOADER_WRAPPER.fullmatch(line)
    if match is None or match[2] not in LOADER_FAILURE_MODULES:
        return {}
    cause, token = loader_cause_signature(match[3])
    safe = {"loader_stage": match[1], "loader_module": match[2], "loader_cause": cause}
    if token is not None:
        safe["loader_token"] = token
    return loader_failure_fields(safe)


def fixture_failure_diagnostic(error: Exception, diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Classify a native initialization failure without serializing any SDK text."""
    value: dict[str, Any] = {"fixture_failed": True, "failure_category": "unknown"}
    kind = type(error).__name__
    if kind in EXCEPTION_TYPES:
        value["error_type"] = kind
    stage = diagnostic.get("fixture_stage")
    if type(stage) is str and stage in FIXTURE_STAGES:
        value["fixture_stage"] = stage
    if kind == "JsonRpcError":
        fields = vars(error)
        code = fields.get("code")
        if type(code) is int and code in RPC_ERROR_CODES:
            value["rpc_code"] = code
        message = fields.get("message")
        if (type(code) is int and code == -32603 and stage in ("initialize", "harness-enter")
                and type(message) is str and len(message) <= 64 * 1024):
            # initialize() may append stderr after the wire message. Do not scan
            # those later lines: a log mention is not evidence of the RPC cause.
            value["failure_category"] = _INITIALIZE_FAILURE_CATEGORIES.get(message.partition("\n")[0], "unknown")
            loader = loader_entry_failure_diagnostic(message)
            if loader:
                value.update(failure_category="loader-settlement", **loader)
    return value


def command_diagnostic(returncode: Any, timed_out: bool, stdout: bytes | str = b"", stderr: bytes | str = b"") -> dict[str, Any]:
    """Reconstruct fixed codes from redacted JSON envelopes, not native output."""
    value: dict[str, Any] = {"timed_out": timed_out}
    if type(returncode) is int and -(2**31) <= returncode < 2**32:
        value["returncode"] = returncode
    for raw, channel in ((stdout, "helper"), (stderr, "fixture")):
        if len(raw) > 64 * 1024:
            continue
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            continue
        if not isinstance(envelope, dict):
            continue
        if channel == "helper" and envelope.get("ok") is False and envelope.get("action") in ("configure", "invoke"):
            error = envelope.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, str) and code in HELPER_ERROR_CODES:
                value["helper_error"] = code
        elif channel == "fixture" and envelope.get("fixture_failed") is True:
            kind, stage = envelope.get("error_type"), envelope.get("fixture_stage")
            if isinstance(kind, str) and kind in EXCEPTION_TYPES:
                value["child_exception"] = kind
            if isinstance(stage, str) and stage in FIXTURE_STAGES:
                value["fixture_stage"] = stage
            code = envelope.get("rpc_code")
            if type(code) is int and code in RPC_ERROR_CODES:
                value["rpc_code"] = code
            category = envelope.get("failure_category")
            if type(category) is str and category in FIXTURE_FAILURE_CATEGORIES:
                value["failure_category"] = category
            if (value.get("failure_category") == "loader-settlement" and value.get("rpc_code") == -32603
                    and value.get("fixture_stage") in ("initialize", "harness-enter")):
                value.update(loader_failure_fields(envelope))
    return value


@dataclass
class OwnedResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    observed_processes: int


class NativeCommandFailure(AssertionError):
    """Carry fixed CI metadata, never the child output or assertion values."""

    def __init__(self, stage: str, result: OwnedResult) -> None:
        super().__init__("native command failed; raw diagnostics withheld")
        self.ci_diagnostic = {"stage": stage, **command_diagnostic(result.returncode, result.timed_out, result.stdout, result.stderr)}


def isolated_environment(root: Path, *, base_url: str | None = None) -> dict[str, str]:
    """Exclude personal homes, provider credentials, proxies and DSH overrides."""
    environment = dict(os.environ)
    for key in list(environment):
        upper = key.upper()
        if (
            upper.startswith(("DSH_", "DEEPSEEK_", "UV_", "PYTHON", "CONDA"))
            or upper in {"VIRTUAL_ENV", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NODE_OPTIONS"}
            or any(part in upper for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
        ):
            environment.pop(key, None)
    user_home = root / "os home 空格"
    temporary = root / "tmp"
    user_home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    environment.update({
        "HOME": str(user_home),
        "USERPROFILE": str(user_home),
        "APPDATA": str(user_home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(user_home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(user_home / ".config"),
        "XDG_CACHE_HOME": str(user_home / ".cache"),
        "XDG_DATA_HOME": str(user_home / ".local" / "share"),
        "XDG_STATE_HOME": str(user_home / ".local" / "state"),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "DSH_HOME": str(root / "never-personal-dsh-home"),
        "DSH_RUNTIME_MODE": "exe",
        "DSH_TELEMETRY_DISABLED": "1",
        "DEEPSEEK_API_KEY": DUMMY_KEY,
        # A closed loopback port is safer than the SDK's public API default.
        "DEEPSEEK_BASE_URL": base_url or "http://127.0.0.1:1/v1",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    })
    return environment


class _PosixOwnedTree:
    """Track our process group plus descendants, including changed sessions."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.seen: dict[int, tuple[str, int]] = {}
        self.stop_event = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._monitor, daemon=True, name="ci-owned-tree")
        self._sample()
        self.thread.start()

    @staticmethod
    def _snapshot() -> dict[int, tuple[int, int, str, str]]:
        ps = next((path for path in ("/bin/ps", "/usr/bin/ps") if Path(path).is_file()), None)
        if ps is None:
            raise RuntimeError("owned-process verification requires the native ps utility")
        result = subprocess.run(
            [ps, "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
            text=True, encoding="utf-8", timeout=5, env={**os.environ, "LC_ALL": "C"},
        )
        rows: dict[int, tuple[int, int, str, str]] = {}
        for line in result.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) == 5:
                pid, ppid, pgid, state, started = fields
                rows[int(pid)] = (int(ppid), int(pgid), state, started)
        return rows

    def _sample(self) -> None:
        rows = self._snapshot()
        # Do not rediscover children through a recycled PID.
        owned = {
            pid for pid, (started, _pgid) in self.seen.items()
            if pid in rows and rows[pid][3] == started
        }
        if self.process.poll() is None and self.process.pid in rows:
            owned.add(self.process.pid)
        changed = True
        while changed:
            changed = False
            for pid, (ppid, pgid, _state, started) in rows.items():
                if pid in owned or ppid in owned or pgid == self.process.pid:
                    if pid not in owned:
                        changed = True
                    owned.add(pid)
                    self.seen[pid] = (started, pgid)

    def _monitor(self) -> None:
        try:
            while not self.stop_event.wait(0.075):
                self._sample()
        except BaseException as error:
            self.error = error

    def _living(self) -> dict[int, tuple[int, int, str, str]]:
        # Reap an exited leader before group cleanup: on Darwin an unreaped
        # zombie leader can make killpg report EPERM despite live owned members.
        self.process.poll()
        rows = self._snapshot()
        living = {}
        for pid, row in rows.items():
            _ppid, pgid, state, started = row
            known = pid in self.seen and self.seen[pid][0] == started
            # A fast parent can exit before the first 75ms sample. Its children
            # remain ours by PGID even after reparenting, with redirected pipes.
            if pgid == self.process.pid:
                self.seen[pid] = (started, pgid)
            if (known or pgid == self.process.pid) and not state.startswith("Z"):
                living[pid] = row
        return living

    def cleanup(self, *, timed_out: bool) -> int:
        self.stop_event.set()
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            raise AssertionError("owned-process monitor failed to stop")
        self._sample()
        living = self._living()
        # Give successful SDK context cleanup a small, bounded reap window.
        deadline = time.monotonic() + (0.0 if timed_out else 2.0)
        while living and time.monotonic() < deadline:
            time.sleep(0.05)
            living = self._living()
        leaked = bool(living) and not timed_out
        for sig in (signal.SIGTERM, signal.SIGKILL):
            living = self._living()
            # The command owns this exact PGID. Check membership independently
            # of leader liveness; never signal by a global process name.
            if any(row[1] == self.process.pid for row in living.values()):
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    pass
            for pid in reversed(list(living)):
                if living[pid][1] == self.process.pid:
                    continue
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            deadline = time.monotonic() + 2
            while self._living() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not self._living():
                break
        if self._living():
            raise AssertionError("test-owned process tree survived bounded cleanup")
        if self.error is not None:
            raise AssertionError("owned-process monitor failed; cleanup coverage is unverified") from self.error
        if leaked:
            raise AssertionError("successful command left a live test-owned descendant")
        return len(self.seen)


class _WindowsOwnedJob:
    """A kill-on-close Job assigned before the gated child can spawn anything."""

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(field, ctypes.c_uint64) for field in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self.accounting_type = Accounting
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ]
        self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):  # type: ignore[attr-defined]
            raise ctypes.WinError(ctypes.get_last_error())

    def counts(self) -> tuple[int, int]:
        accounting = self.accounting_type()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses), int(accounting.TotalProcesses)

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 124):
            raise ctypes.WinError(ctypes.get_last_error())

    def cleanup(self, *, timed_out: bool) -> int:
        active, total = self.counts()
        deadline = time.monotonic() + (0.0 if timed_out else 2.0)
        while active and time.monotonic() < deadline:
            time.sleep(0.05)
            active, total = self.counts()
        leaked = bool(active) and not timed_out
        if active:
            self.terminate()
        deadline = time.monotonic() + 5
        while self.counts()[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.counts()[0]:
            raise AssertionError("test-owned Windows Job survived termination")
        if leaked:
            raise AssertionError("successful command left a live process in its test-owned Windows Job")
        return total

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_owned(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
    input_text: str | None = None, diagnostic: dict[str, Any] | None = None,
) -> OwnedResult:
    """Outer wall-clock watchdog independent of the SDK/helper request timeout.

    Windows uses a gated stdlib Python launcher so the Job is attached BEFORE
    the real command or any of its descendants execute. POSIX tracks the owned
    process group AND descendants that start another session (as the helper
    intentionally does). Normal completion is also checked for live leaks.
    """
    if diagnostic is not None:
        diagnostic.update(process_stage="create")
    job = _WindowsOwnedJob() if os.name == "nt" else None
    argv = command
    payload = (input_text or "").encode("utf-8")
    if job is not None:
        argv = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--owned-child", "--", *command]
        payload = b"DSH_CI_GO\n" + payload
    process: subprocess.Popen[bytes] | None = None
    tree: _PosixOwnedTree | None = None
    timed_out = False
    cleaned = False
    try:
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        if diagnostic is not None:
            diagnostic.update(process_stage="assign")
        if job is not None:
            job.assign(process)
        else:
            tree = _PosixOwnedTree(process)
        if diagnostic is not None:
            diagnostic.update(process_stage="communicate")
        try:
            stdout, stderr = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if diagnostic is not None:
                diagnostic.update(process_stage="cleanup", timed_out=True)
            if job is not None:
                job.terminate()
            assert job is not None or tree is not None
            observed = job.cleanup(timed_out=True) if job is not None else tree.cleanup(timed_out=True)  # type: ignore[union-attr]
            cleaned = True
            stdout, stderr = process.communicate(timeout=5)
        else:
            # Keep only reconstructed fields before cleanup, so a cleanup
            # assertion cannot erase the command's safe exit/stage evidence.
            if diagnostic is not None:
                diagnostic.update(process_stage="cleanup", **command_diagnostic(process.returncode, False, stdout, stderr))
            observed = job.cleanup(timed_out=False) if job is not None else tree.cleanup(timed_out=False)  # type: ignore[union-attr]
            cleaned = True
        if diagnostic is not None:
            diagnostic.update(process_stage="complete", **command_diagnostic(process.returncode, timed_out, stdout, stderr))
        return OwnedResult(
            process.returncode, stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"), timed_out, observed,
        )
    finally:
        try:
            if process is not None and not cleaned:
                if job is not None:
                    job.terminate()
                    job.cleanup(timed_out=True)
                elif tree is not None:
                    tree.cleanup(timed_out=True)
                elif process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        finally:
            if job is not None:
                job.close()
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def _text_chunks(text: str) -> list[dict[str, Any]]:
    # Published 0.1.5rc1 uses choices.delta, not master's messages protocol.
    return [
        {"choices": [{"delta": {"role": "assistant", "content": None, "reasoning_content": ""}}]},
        {"choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 3}},
    ]


def _tool_chunks(call_id: str, tool: str, command: str) -> list[dict[str, Any]]:
    return [
        {"choices": [{"delta": {"role": "assistant", "content": None, "reasoning_content": ""}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": call_id, "type": "function",
            "function": {"name": tool, "arguments": json.dumps({"command": command})},
        }]}}]},
        {"choices": [{"delta": {"content": ""}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 3}},
    ]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


class MockModel:
    """All requests captured in memory; no credentials or requests printed."""

    def __init__(self, mode: str = "text", *, workspace: Path | None = None) -> None:
        self.mode = mode
        self.workspace = workspace
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.errors: list[str] = []
        self.tool_outputs: list[str] = []
        self.release = threading.Event()
        self.lock = threading.Lock()

    def __enter__(self) -> "MockModel":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                self.connection.settimeout(10)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2_000_000:
                        raise ValueError("invalid model request length")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("model request is not an object")
                    with owner.lock:
                        owner.requests.append((self.path, body))
                    if self.path != "/v1/chat/completions":
                        raise ValueError("unexpected model endpoint")
                    if body.get("stream") is not True or not isinstance(body.get("messages"), list):
                        raise ValueError("not the published streaming chat-completions protocol")
                    if owner.mode == "error":
                        response = json.dumps({"error": {
                            "message": PRIVATE_ERROR, "type": "authentication_error", "code": "invalid_api_key",
                        }}).encode()
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(response)
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    chunks = owner.chunks(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in chunks:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    if owner.mode == "stall":
                        owner.release.wait(timeout=45)
                    else:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    self.close_connection = True
                except (BrokenPipeError, ConnectionResetError):
                    # Expected when the helper's actual total deadline fires.
                    return
                except Exception:
                    # Do not echo request bodies, SDK diagnostics or headers.
                    with owner.lock:
                        owner.errors.append("mock protocol or shell-state assertion failed")
                    try:
                        self.send_error(500, "synthetic mock assertion failed")
                    except OSError:
                        pass

            def do_GET(self) -> None:
                with owner.lock:
                    owner.errors.append("unexpected non-model GET request")
                self.send_error(404)

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="ci-loopback-model")
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise AssertionError("loopback mock server failed to stop")

    def chunks(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        if self.mode == "stall":
            return [{"choices": [{"delta": {"role": "assistant", "content": None}}]}]
        if self.mode == "continuation":
            messages = body["messages"]
            latest = next(message for message in reversed(messages) if message.get("role") == "user" and _message_text(message.get("content")) in (FIRST_PROMPT, FOLLOWUP_PROMPT))
            text = _message_text(latest.get("content"))
            if text == FIRST_PROMPT:
                return _text_chunks(FIRST_RESPONSE)
            if text != FOLLOWUP_PROMPT:
                raise AssertionError("unexpected follow-up input")
            if not any(message.get("role") == "user" and _message_text(message.get("content")) == FIRST_PROMPT for message in messages):
                raise AssertionError("second request lost the first user history")
            if not any(message.get("role") == "assistant" and _message_text(message.get("content")) == FIRST_RESPONSE for message in messages):
                raise AssertionError("second request lost the first assistant history")
            return _text_chunks(FOLLOWUP_RESPONSE)
        if self.mode != "shell":
            return _text_chunks(FINAL_TEXT)
        assert self.workspace is not None
        latest = next(message for message in reversed(body["messages"]) if message.get("role") != "system")
        if latest.get("role") != "tool":
            names = {tool.get("function", {}).get("name") for tool in body.get("tools", [])}
            if SHELL_TOOL not in names:
                raise AssertionError("native platform shell is not advertised")
            if os.name == "nt":
                command = (
                    "$global:dshCiCounter = 1; "
                    'Write-Output "COUNT=$global:dshCiCounter CWD=$((Get-Location).Path)"; '
                    "Set-Location -LiteralPath './shell 子目录'"
                )
            else:
                command = (
                    "counter=1; export counter; "
                    "printf 'COUNT=%s CWD=%s\\n' \"$counter\" \"$PWD\"; "
                    "cd './shell 子目录'"
                )
            return _tool_chunks("ci-shell-1", SHELL_TOOL, command)
        text = _message_text(latest.get("content"))
        self.tool_outputs.append(text)
        if latest.get("tool_call_id") == "ci-shell-1":
            if f"COUNT=1 CWD={self.workspace}".casefold() not in text.casefold():
                raise AssertionError("first shell call did not run in the requested workspace")
            command = (
                "$global:dshCiCounter = [int]$global:dshCiCounter + 1; "
                'Write-Output "COUNT=$global:dshCiCounter CWD=$((Get-Location).Path)"'
                if os.name == "nt" else
                "counter=$((counter + 1)); export counter; printf 'COUNT=%s CWD=%s\\n' \"$counter\" \"$PWD\""
            )
            return _tool_chunks("ci-shell-2", SHELL_TOOL, command)
        if latest.get("tool_call_id") == "ci-shell-2":
            expected = f"COUNT=2 CWD={self.workspace / 'shell 子目录'}"
            if expected.casefold() not in text.casefold():
                raise AssertionError("native shell did not preserve counter and cwd")
            return _text_chunks(SHELL_TEXT)
        raise AssertionError("unexpected native shell tool-result id")


class RuntimeFixture:
    def __init__(self, root: Path, profile: str = "sdk") -> None:
        self.root = root
        self.project = root / "project 工作区"
        self.workspace = self.project / "workspace 空格"
        self.home = root / "dsh home 固定"
        self.profile = profile
        self.profile_dir = self.home / "profiles" / profile
        self.workspace.mkdir(parents=True)
        (self.workspace / "shell 子目录").mkdir()
        self.profile_dir.mkdir(parents=True)
        bundles = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-sdk-app"] if profile == "sdk" else ["@deepseek-ai/dsh-sdk-minimal"]
        self.manifest = self.profile_dir / "package.json"
        self.manifest.write_text(json.dumps({
            "name": f"dsh-profile-{profile}", "private": True, "dependencies": {},
            "dsh": {"profile": {"bundles": bundles, "patchReload": "startup"}},
        }, indent=2) + "\n", encoding="utf-8")
        contribution = {
            "id": "session-log-deepseek", "name": "@deepseek-ai/dsh-session-log-deepseek",
            "disabled": False, "config": {"enabled": True},
        }
        self.home_patch = self.home / "cordis.patch.yml"
        self.home_patch.write_text(json.dumps([contribution], indent=2) + "\n", encoding="utf-8")
        self.profile_patch = self.profile_dir / "cordis.patch.yml"
        persistence_id = "session-persistence-jsonl" if profile == "sdk" else "sessions"
        self.profile_patch.write_text(json.dumps([
            contribution,
            {"id": persistence_id, "config": {"root": str(self.home / "sessions"), "compression": "none"}},
        ], indent=2) + "\n", encoding="utf-8")
        self.overlay = root / "enable-upload-before-privacy.patch.yml"
        self.overlay.write_text(json.dumps([contribution], indent=2) + "\n", encoding="utf-8")
        self.unchanged = {path: path.read_bytes() for path in (self.manifest, self.home_patch, self.profile_patch, self.overlay)}
        self.prompt = root / "prompt 文本.txt"
        self.prompt.write_text("Reply with DSH_CI_FINAL_OK only; do not use tools.", encoding="utf-8")

    def environment(self, url: str) -> dict[str, str]:
        return isolated_environment(self.root, base_url=url)

    def child_config(self, url: str) -> Path:
        path = self.root / "synthetic-child.json"
        path.write_text(json.dumps({
            "home": str(self.home), "cwd": str(self.workspace), "profile": self.profile,
            "overlay": str(self.overlay), "privacy": str(PRIVACY_PATCH), "base_url": url,
        }), encoding="utf-8")
        return path


class NativeRuntimeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = os.environ.get("DSH_INVOKER_REQUIRE_RUNTIME") == "1"
        problems = []
        for package in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin"):
            try:
                actual = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                actual = None
            if actual != SDK_VERSION:
                problems.append(f"{package}=={SDK_VERSION} is not installed")
        if problems:
            message = "; ".join(problems)
            if required:
                raise RuntimeError("REQUIRED native runtime lane: " + message)
            raise unittest.SkipTest("native integration unavailable (offline units are still valid): " + message)
        if required and sys.version_info[:2] != (3, 12):
            raise RuntimeError("REQUIRED native runtime lane must use managed Python 3.12")
        if not HELPER.is_file() or not PRIVACY_PATCH.is_file():
            raise RuntimeError("native integration requires the real helper and privacy asset")

    def setUp(self) -> None:
        parent_string = os.environ.get("DSH_INVOKER_TEST_ROOT") or os.environ.get("DSH_INVOKER_TEST_TMP")
        parent = Path(parent_string).resolve() if parent_string else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="native runtime 空格 ", dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = RuntimeFixture(self.root)

    def run_command(self, command: list[str], *, stage: str, **options: Any) -> OwnedResult:
        if stage not in COMMAND_STAGES:
            raise ValueError("unknown native command stage")
        self.ci_diagnostic = {"stage": stage}
        return run_owned(command, diagnostic=self.ci_diagnostic, **options)

    def assert_owned_success(self, result: OwnedResult, *, stage: str = "native-command") -> dict[str, Any]:
        if result.timed_out or result.returncode != 0:
            raise NativeCommandFailure(stage, result)
        self.assertFalse(DUMMY_KEY in result.stdout + result.stderr, "dummy provider key leaked; captured output withheld")
        self.assertFalse(PRIVATE_ERROR in result.stdout + result.stderr, "private provider error leaked; captured output withheld")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AssertionError("native command did not return the expected JSON object") from error
        self.assertIsInstance(value, dict)
        return value

    def configure(self, fixture: RuntimeFixture, environment: dict[str, str]) -> None:
        result = self.run_command([
            sys.executable, "-I", "-B", str(HELPER), "configure", "--project-root", str(fixture.project),
            "--home", str(fixture.home), "--profile", fixture.profile, "--confirmed",
        ], stage="helper-configure", cwd=fixture.workspace, env=environment, timeout=20)
        self.assert_owned_success(result, stage="helper-configure")

    def invoke(
        self, fixture: RuntimeFixture, environment: dict[str, str], *,
        stdin: bool = False, session_id: str | None = None, total_timeout: float = 60,
        input_text: str | None = None,
    ) -> OwnedResult:
        command = [
            sys.executable, "-I", "-B", str(HELPER), "invoke", "--project-root", str(fixture.project),
            "--cwd", str(fixture.workspace), "--patch", str(fixture.overlay),
            "--timeout-seconds", str(total_timeout),
        ]
        if stdin:
            command.append("--stdin")
        else:
            command.extend(["--prompt-file", str(fixture.prompt)])
        if session_id is not None:
            command.extend(["--session-id", session_id])
        return self.run_command(
            command, stage="helper-invoke", cwd=fixture.workspace, env=environment, timeout=total_timeout + 20,
            input_text=(input_text or "Reply with DSH_CI_FINAL_OK only; do not use tools.\n") if stdin else None,
        )

    def assert_private_requests(self, model: MockModel, *, minimum: int = 1) -> None:
        self.assertFalse(model.errors, "loopback fixture rejected the native protocol/tool state")
        self.assertGreaterEqual(len(model.requests), minimum, "no model request reached the loopback fixture")
        for path, body in model.requests:
            self.assertTrue(path == "/v1/chat/completions", "unexpected model request path; captured path withheld")
            self.assertFalse("dsh_session_log" in body, "privacy requires ABSENCE, not a null/empty session-log field; captured request withheld")

    def assert_original_files_unchanged(self, fixture: RuntimeFixture) -> None:
        for path, original in fixture.unchanged.items():
            self.assertTrue(path.read_bytes() == original, "synthetic original was modified; file contents withheld")
        self.assertTrue(PRIVACY_PATCH.read_bytes() == self.privacy_original, "privacy patch was modified; file contents withheld")

    def assert_local_session(self, fixture: RuntimeFixture, session_id: str, text: str) -> None:
        # JSONL is made uncompressed only in this synthetic fixture. Production
        # profile persistence/compression is not changed by the privacy patch.
        logs = list((fixture.home / "sessions").rglob("*.jsonl"))
        self.assertTrue(logs, "privacy override unexpectedly removed local session persistence")
        found = False
        for path in logs:
            content = path.read_text(encoding="utf-8")
            if session_id in content and text in content:
                # Validate the file is JSONL without printing its records.
                for line in content.splitlines():
                    if line.strip():
                        self.assertIsInstance(json.loads(line), dict)
                found = True
        self.assertTrue(found, "expected local session id/final response is missing")

    @property
    def privacy_original(self) -> bytes:
        # Cached lazily before the first actual invocation in each test.
        if not hasattr(self, "_privacy_original"):
            self._privacy_original = PRIVACY_PATCH.read_bytes()
        return self._privacy_original

    def test_published_executable_initialize_shutdown_has_zero_model_requests(self) -> None:
        fixture = self.fixture
        _ = self.privacy_original
        with MockModel() as model:
            result = self.run_command([
                sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--native-child", "lifecycle",
                str(fixture.child_config(model.url)),
            ], stage="lifecycle", cwd=fixture.workspace, env=fixture.environment(model.url), timeout=45)
            value = self.assert_owned_success(result, stage="lifecycle")
            self.assertEqual(value["server_name"], "deepseek-harness-sdk-runtime")
            self.assertEqual(value["server_version"], "0.0.1")
            self.assertEqual(value["profile"], "sdk")
            self.assertTrue(value["reaped"])
            self.assertEqual(value["carrier"], "exe")
            self.assertFalse(bool(model.requests), "initialize/shutdown unexpectedly made a model request; captured requests withheld")
            self.assertFalse(model.errors)
        self.assert_original_files_unchanged(fixture)

    def test_default_sdk_helper_final_persistence_privacy_and_unsupported_restore(self) -> None:
        fixture = self.fixture
        _ = self.privacy_original
        fixture.prompt.write_text(FIRST_PROMPT, encoding="utf-8")
        with MockModel("continuation") as model:
            environment = fixture.environment(model.url)
            # The helper must override a stale developer-mode resolver override
            # in its disposable worker, not only in the runtime's child env map.
            environment["DSH_RUNTIME_MODE"] = "node"
            self.configure(fixture, environment)
            first = self.assert_owned_success(self.invoke(fixture, environment), stage="helper-invoke")
            self.assertEqual(set(first), {"session_id", "finish_reason", "final_response"})
            self.assertIsInstance(first["session_id"], str)
            self.assertTrue(first["session_id"])
            self.assertEqual(first["finish_reason"], "completed")
            self.assertEqual(first["final_response"], FIRST_RESPONSE)
            before = len(model.requests)
            second = self.invoke(fixture, environment, stdin=True, session_id=first["session_id"], input_text=FOLLOWUP_PROMPT)
            self.assertFalse(second.timed_out)
            self.assertEqual(second.returncode, 3)
            document = json.loads(second.stdout)
            self.assertEqual(document["error"]["code"], "UNSUPPORTED_CONTINUATION")
            self.assertEqual(len(model.requests), before, "unsupported restore must not send a second provider request")
            self.assert_private_requests(model)
        self.assert_local_session(fixture, first["session_id"], FIRST_RESPONSE)
        self.assert_original_files_unchanged(fixture)

    def test_published_sdk_same_runtime_continuation_preserves_both_turns(self) -> None:
        fixture = self.fixture
        _ = self.privacy_original
        with MockModel("continuation") as model:
            result = self.run_command([
                sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--native-child", "continuation",
                str(fixture.child_config(model.url)),
            ], stage="continuation", cwd=fixture.workspace, env=fixture.environment(model.url), timeout=60)
            value = self.assert_owned_success(result, stage="continuation")
            self.assertEqual(value["first_response"], FIRST_RESPONSE)
            self.assertEqual(value["second_response"], FOLLOWUP_RESPONSE)
            self.assertEqual(value["finish_reason"], "completed")
            self.assertEqual(value["session_id"], "ci-same-runtime-continuation")
            self.assertEqual(len(model.requests), 2)
            self.assert_private_requests(model, minimum=2)
            messages = model.requests[1][1]["messages"]
            self.assertTrue(any(message.get("role") == "user" and _message_text(message.get("content")) == FIRST_PROMPT for message in messages))
            self.assertTrue(any(message.get("role") == "assistant" and _message_text(message.get("content")) == FIRST_RESPONSE for message in messages))
            self.assertTrue(any(message.get("role") == "user" and _message_text(message.get("content")) == FOLLOWUP_PROMPT for message in messages))
        for text in (FIRST_PROMPT, FIRST_RESPONSE, FOLLOWUP_PROMPT, FOLLOWUP_RESPONSE):
            self.assert_local_session(fixture, value["session_id"], text)
        self.assert_original_files_unchanged(fixture)

    def test_released_sdk_client_restore_methods_are_not_implemented(self) -> None:
        fixture = self.fixture
        _ = self.privacy_original
        with MockModel() as model:
            result = self.run_command([
                sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--native-child", "restore-methods",
                str(fixture.child_config(model.url)),
            ], stage="restore-methods", cwd=fixture.workspace, env=fixture.environment(model.url), timeout=45)
            value = self.assert_owned_success(result, stage="restore-methods")
            self.assertEqual(value["unsupported_methods"], ["session/resume", "session/load", "session/restore"])
            self.assertFalse(model.requests, "restore capability check must have zero model traffic")
            self.assertFalse(model.errors)
        self.assert_original_files_unchanged(fixture)

    def test_helper_auth_error_is_nonzero_redacted_and_reaped(self) -> None:
        fixture = self.fixture
        _ = self.privacy_original
        with MockModel("error") as model:
            environment = fixture.environment(model.url)
            self.configure(fixture, environment)
            result = self.invoke(fixture, environment, total_timeout=45)
            self.assertFalse(result.timed_out, "CI watchdog, not error handling, ended the request")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotEqual(result.returncode, 124, "authentication error must not be mistaken for a deadline")
            self.assertFalse(DUMMY_KEY in result.stdout + result.stderr, "dummy provider key leaked; captured output withheld")
            self.assertFalse(PRIVATE_ERROR in result.stdout + result.stderr, "private provider error leaked; captured output withheld")
            value = json.loads(result.stdout)
            self.assertIsInstance(value, dict)
            if set(value) == {"session_id", "finish_reason", "final_response"}:
                self.assertEqual(result.returncode, 4)
                self.assertNotEqual(value["finish_reason"], "completed")
            else:
                self.assertEqual(set(value), {"ok", "action", "error"})
                self.assertIs(value["ok"], False)
                self.assertEqual(value["action"], "invoke")
                self.assertIsInstance(value["error"], dict)
                self.assertEqual(set(value["error"]), {"code", "message"})
                self.assertIn(value["error"]["code"], {"SDK_FAILURE", "SDK_REQUEST_TIMEOUT"})
                self.assertIsInstance(value["error"]["message"], str)
                self.assertEqual(result.returncode, 5)
            self.assert_private_requests(model)
        self.assert_original_files_unchanged(fixture)

    def test_helper_whole_turn_deadline_and_owned_process_cleanup(self) -> None:
        fixture = self.fixture
        _ = self.privacy_original
        with MockModel("stall") as model:
            environment = fixture.environment(model.url)
            self.configure(fixture, environment)
            result = self.invoke(fixture, environment, total_timeout=15)
            self.assertFalse(result.timed_out, "independent CI watchdog fired before the helper's total deadline")
            self.assertEqual(result.returncode, 124, "stalled model must hit the helper's actual whole-turn deadline")
            self.assertFalse(DUMMY_KEY in result.stdout + result.stderr, "dummy provider key leaked; captured output withheld")
            self.assertFalse(PRIVATE_ERROR in result.stdout + result.stderr, "private provider error leaked; captured output withheld")
            self.assert_private_requests(model)
        self.assert_original_files_unchanged(fixture)

    def test_explicit_synthetic_sdk_minimal_native_persistent_shell(self) -> None:
        # Upstream sdk-minimal deliberately has different permissions. It is
        # selected ONLY here, in a new fixture, never after default-sdk failure.
        fixture = RuntimeFixture(self.root / "explicit minimal synthetic", "sdk-minimal")
        _ = self.privacy_original
        with MockModel("shell", workspace=fixture.workspace) as model:
            result = self.run_command([
                sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--native-child", "shell",
                str(fixture.child_config(model.url)),
            ], stage="shell", cwd=fixture.workspace, env=fixture.environment(model.url), timeout=90)
            value = self.assert_owned_success(result, stage="shell")
            self.assertEqual(value["profile"], "sdk-minimal")
            self.assertEqual(value["finish_reason"], "completed")
            self.assertTrue(value["final_response"] == SHELL_TEXT, "unexpected shell final response; captured response withheld")
            self.assertEqual(value["shell"], SHELL_TOOL)
            self.assertEqual(len(model.tool_outputs), 2)
            self.assertEqual(len(model.requests), 3)
            self.assert_private_requests(model, minimum=3)
        self.assert_local_session(fixture, value["session_id"], SHELL_TEXT)
        self.assert_original_files_unchanged(fixture)


def _native_child(scenario: str, config_path: str, diagnostic: dict[str, Any] | None = None) -> int:
    """A watchdog-contained fixture calling the public, installed SDK only."""
    def stage(value: str) -> None:
        if diagnostic is not None:
            diagnostic["fixture_stage"] = value

    stage("imports")
    from deepseek_harness import DeepSeekHarness
    from deepseek_harness.client import HarnessClient, HarnessConfig
    from deepseek_harness_runtime import bundled_runtime_path, resolve_bundled_launch_args

    stage("runtime-resolve")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    executable = bundled_runtime_path().resolve()
    launch = resolve_bundled_launch_args("exe")
    if len(launch) != 1 or Path(launch[0]).resolve() != executable or not executable.is_file():
        raise AssertionError("not the installed published executable runtime")
    if not executable.is_relative_to(Path(sys.prefix).resolve()):
        raise AssertionError("runtime escaped the active SDK virtual environment")
    common = {
        "cwd": config["cwd"], "dsh_home": config["home"], "profile": config["profile"],
        "patches": (config["overlay"], config["privacy"]),
        "request_timeout_seconds": 10, "initialize_timeout_seconds": 20,
        "shutdown_timeout_seconds": 3,
    }
    if scenario == "lifecycle":
        stage("client-enter")
        with HarnessClient(HarnessConfig(**common)) as client:
            stage("initialize")
            initialized = client.initialize(cwd=config["cwd"], provider="deepseek-official", model="ci-mock-model")
            process = client._proc  # Test-only assertion of actual SDK subprocess reaping.
            if process is None:
                raise AssertionError("SDK did not start a runtime")
            runtime_pid = process.pid
            stage("shutdown")
        stage("result")
        info = initialized.serverInfo
        value = {
            "server_name": info.name if info else None, "server_version": info.version if info else None,
            "runtime_pid": runtime_pid, "reaped": process.poll() is not None,
            "profile": config["profile"], "carrier": "exe",
        }
    elif scenario == "continuation":
        stage("harness-enter")
        with DeepSeekHarness(
            **common, provider="deepseek-official", model="ci-mock-model",
            base_url=config["base_url"], api_key=DUMMY_KEY,
        ) as harness:
            stage("first-turn")
            first = harness.run(FIRST_PROMPT, session_id="ci-same-runtime-continuation")
            if first.finish_reason != "completed":
                raise AssertionError("first same-runtime turn failed")
            stage("second-turn")
            second = harness.run(FOLLOWUP_PROMPT, session_id=first.session_id)
            stage("shutdown")
        stage("result")
        value = {
            "session_id": second.session_id, "finish_reason": second.finish_reason,
            "first_response": first.final_response, "second_response": second.final_response,
        }
    elif scenario == "restore-methods":
        from deepseek_harness.errors import JsonRpcError
        from pydantic import BaseModel

        class EmptyResponse(BaseModel):
            pass

        unsupported = []
        stage("client-enter")
        with HarnessClient(HarnessConfig(**common)) as client:
            stage("initialize")
            client.initialize(cwd=config["cwd"], provider="deepseek-official", model="ci-mock-model")
            for method in ("session/resume", "session/load", "session/restore"):
                stage("restore-request")
                try:
                    client.request(method, {"sessionId": "ci-no-persisted-session"}, response_model=EmptyResponse)
                except JsonRpcError as error:
                    if "unknown DeepSeek Harness SDK runtime method" not in error.message:
                        raise AssertionError("unexpected restore capability response") from None
                    unsupported.append(method)
                else:
                    raise AssertionError("released SDK unexpectedly implements restoration; revisit helper policy")
            stage("shutdown")
        stage("result")
        value = {"unsupported_methods": unsupported}
    elif scenario == "shell":
        if config["profile"] != "sdk-minimal":
            raise AssertionError("persistent-shell fixture must opt into sdk-minimal explicitly")
        stage("harness-enter")
        with DeepSeekHarness(
            **common, provider="deepseek-official", model="ci-mock-model",
            base_url=config["base_url"], api_key=DUMMY_KEY,
        ) as harness:
            stage("shell-turn")
            result = harness.run("Exercise the synthetic persistent shell.", session_id="ci-explicit-minimal")
            stage("shutdown")
        stage("result")
        value = {
            "session_id": result.session_id, "finish_reason": result.finish_reason,
            "final_response": result.final_response, "profile": config["profile"], "shell": SHELL_TOOL,
        }
    else:
        raise ValueError("unknown native child scenario")
    print(json.dumps(value))
    return 0


def _owned_child(arguments: list[str]) -> int:
    """Windows only: do not spawn until the parent has assigned our Job."""
    if arguments[:1] != ["--"] or sys.stdin.buffer.readline() != b"DSH_CI_GO\n":
        return 125
    payload = sys.stdin.buffer.read()
    result = subprocess.run(arguments[1:], input=payload, check=False)
    return result.returncode


if __name__ == "__main__":
    if sys.argv[1:2] == ["--owned-child"]:
        raise SystemExit(_owned_child(sys.argv[2:]))
    if sys.argv[1:2] == ["--native-child"]:
        diagnostic: dict[str, Any] = {}
        try:
            raise SystemExit(_native_child(sys.argv[2], sys.argv[3], diagnostic))
        except Exception as error:
            # Emit only literal allowlisted fields, never arbitrary SDK text.
            print(json.dumps(fixture_failure_diagnostic(error, diagnostic)), file=sys.stderr)
            raise SystemExit(1)
    unittest.main()
