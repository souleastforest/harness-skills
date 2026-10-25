"""Read-only native Codex discovery checks; never install or start a model turn.

``plugin/read`` is an experimental, under-development Codex API. Passing this
probe establishes directory identity for the recorded CLI only, not production
client support. ``skills/list`` separately checks the real enabled installation.
``preflight`` guards an installation target using local files only.
Protocol reference: https://learn.chatgpt.com/docs/app-server
"""
from __future__ import annotations

import argparse
import codecs
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from .package import ReleaseError, snapshot_tree, validate_source

MAX_TIMEOUT = 50.0  # Together with bounded cleanup, a probe waits under 60s.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class NativeError(ReleaseError):
    """A filtered, machine-readable failure at the discovery boundary."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.report: dict = {}


def _require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise NativeError(code, message, details)


def _mapping(value: Any, label: str) -> dict:
    _require(isinstance(value, dict), 'invalid_response', f'{label} must be an object')
    return value


def _absolute_path(value: Any, label: str) -> Path:
    _require(isinstance(value, str) and bool(value), 'invalid_path', f'{label} is missing')
    path = Path(value)
    _require(path.is_absolute() and '..' not in path.parts, 'invalid_path', f'{label} must be an absolute path without traversal')
    return path.resolve()


def _fingerprint(files: dict) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _checked_tree(source: dict, root: Path) -> None:
    actual = validate_source(root)
    for field in ('name', 'version', 'marketplace', 'skills'):
        _require(actual[field] == source[field], 'identity_mismatch', f'Directory {field} differs from the verified source', root=str(root))
    files = snapshot_tree(root)
    _require(files == source['files'], 'content_mismatch', 'Directory bytes differ from the verified source', root=str(root), missing=sorted(source['files'].keys() - files.keys()), extra=sorted(files.keys() - source['files'].keys()), changed=sorted(key for key in files.keys() & source['files'].keys() if files[key] != source['files'][key]))


def _expected_cache_root(source: dict, cache_root: Path) -> Path:
    root = Path(cache_root)
    _require(not root.is_symlink(), 'source_mismatch', 'Expected cache root must be a real directory, not a symlink')
    root = root.resolve()
    expected_tail = (source['marketplace'], source['name'], source['version'])
    _require(root.parts[-3:] == expected_tail, 'cache_version_mismatch', 'Expected cache root must end in marketplace/plugin/version', expected=list(expected_tail), actual=list(root.parts[-3:]))
    return root


def preflight_cache(source_root: Path, cache_root: Path) -> dict:
    """Allow an absent or byte-identical version cache; never replace a conflict.

    This is a read-only point-in-time installation guard. It does not resolve a
    Codex binary or start a process. The caller must keep the verified source
    stable and repeat this check immediately before writing the target cache.
    """
    source_root = Path(source_root)
    source = validate_source(source_root)
    report = {'status': 'not_verified', 'mode': 'preflight', 'evidence_scope': 'cache_preflight', 'cache_state': 'not_verified', 'model_turns': 0, 'plugin_installs': 0, 'user_config_writes': 0, 'processes_started': 0, 'source': {key: source[key] for key in ('name', 'version', 'marketplace', 'files')}}
    report['source'].update({'root': str(source_root.resolve()), 'sha256': _fingerprint(source['files'])})
    try:
        root = _expected_cache_root(source, cache_root)
        report['cache_root'] = str(root)
        if root.exists():
            report['cache_state'] = 'conflict'
            try:
                _checked_tree(source, root)
            except (ReleaseError, OSError, ValueError) as error:
                raise NativeError('cache_conflict', 'This version cache already exists with different or invalid content; choose a new version instead of replacing it', {'requires_new_version': True, 'reason': str(error)}) from error
            report['cache_state'] = 'identical'
        else:
            report['cache_state'] = 'absent'
        _require(snapshot_tree(source_root) == source['files'], 'source_changed', 'The source changed during preflight; rerun against a stable snapshot')
        report['status'] = 'passed'
        return report
    except NativeError as error:
        error.report = report
        raise


def _validate_skills(skills: Any, source: dict, root: Path, installed: bool) -> list[dict]:
    _require(isinstance(skills, list), 'invalid_response', 'skills must be a list')
    expected = {source['name'] + ':' + name: name for name in source['skills']}
    _require(len(expected) == 9, 'invalid_source', 'The source must declare exactly nine unique skills')
    for skill in skills:
        _mapping(skill, 'skill')
        _require(isinstance(skill.get('name'), str), 'invalid_response', 'A skill name is missing')
    counts = Counter(skill['name'] for skill in skills)
    _require(counts == Counter(expected.keys()), 'skill_set_mismatch', 'Expected each of the nine plugin skills exactly once', missing=sorted(expected.keys() - counts.keys()), extra=sorted(counts.keys() - expected.keys()), duplicates=sorted(name for name, count in counts.items() if count != 1))
    plugin_id = source['name'] + '@' + source['marketplace']
    filtered = []
    for skill in skills:
        name = skill['name']
        if installed or 'pluginId' in skill:
            _require(skill.get('pluginId') == plugin_id, 'plugin_id_mismatch', 'A skill belongs to an unexpected plugin', skill=name)
        if installed:
            _require(skill.get('enabled') is True, 'skill_disabled', 'An installed skill is not enabled', skill=name)
        path = _absolute_path(skill.get('path'), 'skill.path')
        expected_path = root / 'skills' / expected[name] / 'SKILL.md'
        _require(path == expected_path, 'source_mismatch', 'A skill path differs from the expected plugin directory', skill=name, expected=str(expected_path), actual=str(path))
        filtered.append({key: skill[key] for key in ('name', 'path', 'enabled', 'pluginId') if key in skill})
    return sorted(filtered, key=lambda skill: skill['name'])


def validate_installed(result: dict, source_root: Path, cache_root: Path) -> dict:
    """Validate filtered real installation metadata and all cached source bytes.

    ``cache_root`` is the explicit version directory, ending in
    ``<marketplace>/<plugin-name>/<version>``. No cache root is guessed.
    """
    source = validate_source(Path(source_root))
    root = _expected_cache_root(source, cache_root)
    _checked_tree(source, root)
    rows = _mapping(result, 'skills/list result').get('data')
    _require(isinstance(rows, list) and bool(rows), 'invalid_response', 'skills/list did not return discovery rows')
    candidates = []
    plugin_id = source['name'] + '@' + source['marketplace']
    for row in rows:
        row = _mapping(row, 'discovery row')
        skills = row.get('skills')
        _require(isinstance(skills, list), 'invalid_response', 'A discovery row has no skills list')
        errors = row.get('errors', [])
        _require(isinstance(errors, list) and not errors, 'discovery_errors', 'Native skill discovery reported errors')
        for skill in skills:
            skill = _mapping(skill, 'skill')
            name = skill.get('name', '')
            path = skill.get('path', '')
            # A wrong pluginId or path cannot hide an expected skill. Also catch
            # unexpected names attributed to this plugin or its cache directory.
            related_path = isinstance(path, str) and (path == str(root) or path.startswith(str(root) + os.sep))
            if (isinstance(name, str) and name.startswith(source['name'] + ':')) or skill.get('pluginId') == plugin_id or related_path:
                candidates.append(skill)
    checked = _validate_skills(candidates, source, root, installed=True)
    _checked_tree(source, root)
    return {'skills': checked, 'cache_root': str(root), 'content_sha256': _fingerprint(source['files'])}


def validate_plugin_read(result: dict, source_root: Path, expected_root: Path) -> dict:
    """Validate native directory identity without claiming installation state."""
    source = validate_source(Path(source_root))
    root = Path(expected_root)
    _require(not root.is_symlink(), 'source_mismatch', 'Expected plugin root must be a real directory, not a symlink')
    root = root.resolve()
    _checked_tree(source, root)
    plugin = _mapping(_mapping(result, 'plugin/read result').get('plugin'), 'plugin')
    summary = _mapping(plugin.get('summary'), 'plugin.summary')
    _require(plugin.get('marketplaceName') == source['marketplace'], 'marketplace_mismatch', 'Native plugin marketplace differs from the source')
    catalog = _absolute_path(plugin.get('marketplacePath'), 'plugin.marketplacePath')
    _require(catalog == root / '.agents/plugins/marketplace.json', 'source_mismatch', 'Native plugin marketplace path differs from the expected directory')
    plugin_id = source['name'] + '@' + source['marketplace']
    _require(summary.get('id') == plugin_id and summary.get('name') == source['name'], 'plugin_id_mismatch', 'Native plugin identity differs from the source')
    versions = [summary[key] for key in ('localVersion', 'version') if summary.get(key) is not None]
    _require(bool(versions) and all(value == source['version'] for value in versions), 'version_mismatch', 'Native plugin version differs from the source')
    origin = _mapping(summary.get('source'), 'plugin.summary.source')
    _require(origin.get('type') == 'local', 'source_mismatch', 'Native plugin source is not local')
    _require(_absolute_path(origin.get('path'), 'plugin.summary.source.path') == root, 'source_mismatch', 'Native plugin source differs from the expected directory')
    checked = _validate_skills(plugin.get('skills'), source, root, installed=False)
    filtered_summary = {key: summary[key] for key in ('id', 'name', 'localVersion', 'version', 'installed', 'enabled') if key in summary}
    filtered_summary['source'] = {key: origin[key] for key in ('type', 'path') if key in origin}
    return {'marketplaceName': plugin['marketplaceName'], 'marketplacePath': plugin['marketplacePath'], 'summary': filtered_summary, 'skills': checked, 'content_sha256': _fingerprint(source['files'])}


def resolve_codex_binary(explicit: str | None = None) -> Path:
    """Resolve one executable; explicit paths never fall back silently."""
    candidate = explicit if explicit is not None else shutil.which('codex')
    _require(bool(candidate), 'binary_not_found', 'Codex was not found; supply --codex-bin with an executable path')
    if explicit is not None and not Path(explicit).is_file():
        candidate = shutil.which(explicit)
    _require(bool(candidate), 'binary_not_found', 'The requested Codex executable was not found')
    binary = Path(candidate).expanduser().resolve()
    _require(binary.is_file() and os.access(binary, os.X_OK), 'binary_not_executable', 'Codex is not an executable file', path=str(binary))
    return binary


class StdioRPC:
    """Bounded JSON stream reader, with a single shared absolute deadline.

    A byte-reader thread works with pipes on Windows as well as POSIX. Parsing
    happens before waiting for more input, so buffered multiline and adjacent
    JSON objects cannot leave an already-arrived response waiting in a buffer.
    Notifications and unrelated responses are discarded, never persisted.
    """

    def __init__(self, binary: Path, cwd: Path, deadline: float):
        self.deadline = deadline
        self.buffer = ''
        self.decoder = json.JSONDecoder()
        self.utf8 = codecs.getincrementaldecoder('utf-8')()
        self.stopping = threading.Event()
        self.chunks: queue.Queue = queue.Queue(maxsize=64)
        self.process = subprocess.Popen([str(binary), '--enable', 'plugins', 'app-server', '--listen', 'stdio://'], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self) -> None:
        try:
            while not self.stopping.is_set():
                chunk = os.read(self.process.stdout.fileno(), 65536)
                while not self.stopping.is_set():
                    try:
                        self.chunks.put(chunk, timeout=.05)
                        break
                    except queue.Full:
                        continue
                if not chunk:
                    return
        except (OSError, ValueError):
            if not self.stopping.is_set():
                try:
                    self.chunks.put(b'', timeout=.05)
                except queue.Full:
                    pass

    def send(self, value: dict) -> None:
        _require(time.monotonic() < self.deadline, 'timeout', 'Native discovery exceeded its total deadline')
        try:
            self.process.stdin.write((json.dumps(value) + '\n').encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise NativeError('process_exit', 'Native discovery process closed stdin') from error

    def receive(self, identifier: int) -> dict:
        # Decode bytes incrementally; a UTF-8 character can straddle pipe reads.
        total_bytes = 0
        while True:
            self.buffer = self.buffer.lstrip()
            if self.buffer:
                try:
                    item, end = self.decoder.raw_decode(self.buffer)
                except json.JSONDecodeError as error:
                    # NDJSON promises complete JSON before each newline.
                    if '\n' in self.buffer:
                        raise NativeError('invalid_json', 'Native discovery emitted invalid JSON') from error
                else:
                    self.buffer = self.buffer[end:]
                    _mapping(item, 'RPC message')
                    if item.get('id') == identifier:
                        if 'error' in item:
                            error = _mapping(item['error'], 'RPC error')
                            details = {key: error[key] for key in ('code', 'message') if isinstance(error.get(key), (str, int))}
                            if isinstance(details.get('message'), str):
                                details['message'] = details['message'][:1024]
                            raise NativeError('rpc_error', 'Native discovery request failed', details)
                        return _mapping(item.get('result'), 'RPC result')
                    continue
            remaining = self.deadline - time.monotonic()
            _require(remaining > 0, 'timeout', 'Native discovery exceeded its total deadline')
            try:
                chunk = self.chunks.get(timeout=min(.1, remaining))
            except queue.Empty:
                if self.process.poll() is not None:
                    # One last drain lets a response written immediately before
                    # exit be handled; the reader will also signal pipe EOF.
                    if not self.reader.is_alive() and self.chunks.empty():
                        raise NativeError('process_exit', 'Native discovery exited before its response', {'returncode': self.process.returncode})
                continue
            _require(bool(chunk), 'process_exit', 'Native discovery closed stdout before its response', returncode=self.process.poll())
            total_bytes += len(chunk)
            _require(total_bytes <= MAX_MESSAGE_BYTES, 'response_too_large', 'Native discovery exceeded the response size limit')
            try:
                self.buffer += self.utf8.decode(chunk)
            except UnicodeDecodeError as error:
                raise NativeError('invalid_json', 'Native discovery emitted invalid UTF-8') from error

    def close(self) -> None:
        self.stopping.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        else:
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()
        self.reader.join(timeout=.2)


def discover(mode: str, source_root: Path, *, codex_bin: str | None = None, cache_root: Path | None = None, cwd: Path | None = None, timeout: float = 30.0) -> dict:
    """Run a local cache preflight or one read-only app-server discovery request."""
    _require(mode in ('read', 'installed', 'preflight'), 'arguments', 'mode must be read, installed, or preflight')
    if mode == 'preflight':
        _require(cache_root is not None, 'arguments', 'preflight mode requires --cache-root with the exact version directory')
        return preflight_cache(source_root, cache_root)
    _require(0 < timeout <= MAX_TIMEOUT, 'arguments', f'timeout must be greater than zero and at most {MAX_TIMEOUT:g} seconds')
    _require(mode != 'installed' or cache_root is not None, 'arguments', 'installed mode requires --cache-root with the exact version directory')
    source_root = Path(source_root).absolute()
    source = validate_source(source_root)
    source_root = source_root.resolve()
    binary = resolve_codex_binary(codex_bin)
    report = {'status': 'not_verified', 'mode': mode, 'evidence_scope': 'experimental_native_plugin_read' if mode == 'read' else 'installed_native_skills', 'model_turns': 0, 'plugin_installs': 0, 'user_config_writes': 0, 'cli': {'path': str(binary), 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}, 'runtime': {'platform': platform.platform(), 'python': platform.python_version(), 'python_executable': sys.executable}, 'source': {key: source[key] for key in ('name', 'version', 'marketplace', 'files')}, 'response': None}
    report['source'].update({'root': str(source_root), 'sha256': _fingerprint(source['files'])})
    deadline = time.monotonic() + timeout
    try:
        try:
            version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=max(.001, deadline - time.monotonic()), check=False)
        except subprocess.TimeoutExpired as error:
            raise NativeError('timeout', 'Codex version lookup exceeded the total deadline') from error
        _require(version.returncode == 0 and bool(version.stdout.strip()), 'version_lookup_failed', 'Codex --version did not return a successful version')
        _require(len(version.stdout) <= 4096, 'version_lookup_failed', 'Codex --version output exceeded the size limit')
        report['cli']['version'] = version.stdout.strip()
        with ExitStack() as stack:
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix='cwrelease-native-'))
            if mode == 'read':
                expected_root = Path(temporary) / 'plugin'
                shutil.copytree(source_root, expected_root)
                process_cwd = Path(temporary)
                method = 'plugin/read'
                params = {'pluginName': source['name'], 'marketplacePath': str(expected_root / '.agents/plugins/marketplace.json')}
            else:
                expected_root = Path(cache_root).absolute()
                process_cwd = Path(cwd).resolve() if cwd is not None else source_root
                method = 'skills/list'
                params = {'cwds': [str(process_cwd)], 'forceReload': True}
            report['request'] = {'method': method, 'params': params}
            rpc = StdioRPC(binary, process_cwd, deadline)
            # LIFO cleanup reaps the child before deleting its working directory.
            stack.callback(rpc.close)
            rpc.send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'collaborative_work_release_validation', 'version': source['version']}, 'capabilities': {'experimentalApi': True}}})
            initialized = rpc.receive(1)
            report['initialized'] = {key: initialized[key] for key in ('platformFamily', 'platformOs') if key in initialized}
            rpc.send({'method': 'initialized', 'params': {}})
            rpc.send({'id': 2, 'method': method, 'params': params})
            result = rpc.receive(2)
            report['response'] = validate_plugin_read(result, source_root, expected_root) if mode == 'read' else validate_installed(result, source_root, expected_root)
            _require(snapshot_tree(source_root) == source['files'], 'source_changed', 'The source changed during discovery; rerun against a stable snapshot')
            _require(time.monotonic() < deadline, 'timeout', 'Native discovery exceeded its total deadline')
            report['status'] = 'passed'
        return report
    except NativeError as error:
        error.report = report
        raise
    except (ReleaseError, OSError, ValueError) as error:
        failure = NativeError('validation_failed', str(error))
        failure.report = report
        raise failure from error


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise NativeError('arguments', message)


def _report_destination(output: Path, source_root: Path, cache_root: Path | None) -> Path:
    """Check both the requested path and its resolved parent before any writes."""
    requested = Path(output).absolute()
    _require(not requested.is_symlink(), 'output_unsafe', 'Report output cannot be a symlink')
    # Resolve only the parent: the final component must never be followed, even
    # if another process creates a symlink between validation and file creation.
    destination = requested.parent.resolve() / requested.name
    for protected in (source_root, cache_root):
        if protected is None:
            continue
        lexical_root = Path(protected).absolute()
        for root in (lexical_root, lexical_root.resolve()):
            _require(not any(path.is_relative_to(root) for path in (requested, destination)), 'output_unsafe', 'Report output must be outside the source and cache trees', root=str(root))
    _require(not os.path.lexists(requested) and not os.path.lexists(destination), 'output_exists', 'Report output already exists; choose a new evidence filename')
    return destination


def _open_report(output: Path, source_root: Path, cache_root: Path | None):
    """Reserve a new safe output before discovery; never reopen it by pathname."""
    destination = _report_destination(output, source_root, cache_root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        if (os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
                and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW')):
            # Walk the canonical path through held directory descriptors. A
            # parent replaced with a symlink after validation is rejected rather
            # than redirecting a mkdir or the exclusive file creation.
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(destination.anchor, directory_flags)
            try:
                for component in destination.parent.parts[1:]:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass
                    child = os.open(component, directory_flags, dir_fd=directory)
                    os.close(directory)
                    directory = child
                descriptor = os.open(destination.name, flags, 0o600, dir_fd=directory)
            finally:
                os.close(directory)
        else:
            # Platforms without directory-descriptor operations still resolve
            # parent aliases and revalidate after creating missing directories;
            # exclusive creation prevents replacing any existing final entry.
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = _report_destination(output, source_root, cache_root)
            descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as error:
        raise NativeError('output_exists', 'Report output already exists; choose a new evidence filename') from error
    except OSError as error:
        raise NativeError('output_failed', f'Cannot create a new report: {error}') from error
    try:
        return os.fdopen(descriptor, 'w', encoding='utf-8')
    except BaseException:
        os.close(descriptor)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument('mode', choices=('read', 'installed', 'preflight'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--codex-bin')
    parser.add_argument('--cache-root', type=Path, help='Exact target or installed <marketplace>/<plugin>/<version> directory')
    parser.add_argument('--cwd', type=Path, help='Discovery working directory for installed mode')
    parser.add_argument('--timeout', type=float, default=30.0, help='Total discovery deadline, at most 50 seconds')
    parser.add_argument('--output', type=Path, help='New filtered JSON report outside the source and cache trees; existing files are never replaced')
    args = None
    output_stream = None
    try:
        args = parser.parse_args(argv)
        if args.output is not None:
            output_stream = _open_report(args.output, args.source, args.cache_root)
        report = discover(args.mode, args.source, codex_bin=args.codex_bin, cache_root=args.cache_root, cwd=args.cwd, timeout=args.timeout)
        code = 0
    except (NativeError, ReleaseError, OSError, ValueError) as error:
        report = dict(getattr(error, 'report', {}))
        report.update({'status': 'failed', 'error': {'code': getattr(error, 'code', 'validation_failed'), 'message': str(error), 'details': getattr(error, 'details', {})}})
        code = 1
    if output_stream is not None:
        try:
            with output_stream:
                output_stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        except OSError as error:
            report = {'status': 'failed', 'error': {'code': 'output_failed', 'message': str(error)}}
            code = 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
