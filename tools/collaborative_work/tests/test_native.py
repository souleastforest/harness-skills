"""Failure-oriented tests for native discovery, without a user installation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cwrelease import native
from cwrelease.package import ReleaseError

REPOSITORY = Path(__file__).resolve().parents[3]
SHORT_NAMES = ['brainstorm', 'complexity', 'develop', 'help', 'maintain', 'matt', 'start', 'story', 'team']

# A real child process exercises pipe buffering, deadlines, RPC failures and cleanup.
# It accepts only the three read-only protocol methods used by this tool.
FAKE_CODEX = r'''#!PYTHON
import json, os, pathlib, sys, time
base = pathlib.Path(__file__).parent
scenario = json.loads((base / 'scenario.json').read_text())
if sys.argv[1:] == ['--version']:
    print('codex-cli controlled-test-9.8.7')
    sys.exit(0)
(base / 'pid').write_text(str(os.getpid()))
for line in sys.stdin:
    value = json.loads(line)
    with (base / 'requests.jsonl').open('a') as log:
        log.write(json.dumps(value) + '\n')
    method = value['method']
    if method not in ('initialize', 'initialized', 'plugin/read', 'skills/list'):
        sys.exit(17)
    if method == 'initialized':
        continue
    if method == 'initialize':
        payload = {'id': value['id'], 'result': {'platformFamily': 'test', 'platformOs': 'test'}}
    else:
        behavior = scenario.get('behavior', 'success')
        if behavior == 'exit':
            sys.exit(19)
        if behavior == 'stall':
            time.sleep(20)
            continue
        if behavior == 'rpc-error':
            payload = {'id': value['id'], 'error': {'code': -32001, 'message': 'controlled discovery failure', 'data': 'private-error-data'}}
        elif behavior == 'invalid-json':
            os.write(1, b'not json\n')
            continue
        elif method == 'skills/list':
            payload = {'id': value['id'], 'result': scenario['response']}
        else:
            root = pathlib.Path(value['params']['marketplacePath']).parents[2]
            manifest = json.loads((root / '.codex-plugin/plugin.json').read_text())
            catalog = json.loads((root / '.agents/plugins/marketplace.json').read_text())
            name = manifest['name']
            skills = [{'name': name + ':' + skill.name, 'path': str(skill / 'SKILL.md'), 'enabled': True} for skill in sorted((root / 'skills').iterdir()) if skill.is_dir()]
            payload = {'id': value['id'], 'result': {'plugin': {'marketplaceName': catalog['name'], 'marketplacePath': value['params']['marketplacePath'], 'summary': {'id': name + '@' + catalog['name'], 'name': name, 'localVersion': manifest['version'], 'version': None, 'installed': False, 'enabled': False, 'source': {'type': 'local', 'path': str(root)}}, 'skills': skills}}}
    notification = {'method': 'private/notification', 'params': {'secret': 'unrelated-secret'}}
    wire = json.dumps(notification) + '\n' + json.dumps(payload) + '\n'
    if scenario.get('behavior') == 'partial':
        raw = wire.encode()
        os.write(1, raw[:17])
        time.sleep(.01)
        os.write(1, raw[17:])
    elif scenario.get('behavior') == 'glued':
        os.write(1, (json.dumps(notification) + json.dumps(payload) + '\n').encode())
    else:
        os.write(1, wire.encode())
'''


class NativeFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        shutil.copytree(REPOSITORY / 'plugins/collaborative-work', self.source)
        self.manifest = json.loads((self.source / '.codex-plugin/plugin.json').read_text())
        self.catalog = json.loads((self.source / '.agents/plugins/marketplace.json').read_text())
        self.version = self.manifest['version']
        self.plugin_id = 'collaborative-work@' + self.catalog['name']
        self.cache = self.root / 'cache' / self.catalog['name'] / 'collaborative-work' / self.version
        shutil.copytree(self.source, self.cache)
        self.skills = [{'name': 'collaborative-work:' + name, 'path': str(self.cache / 'skills' / name / 'SKILL.md'), 'pluginId': self.plugin_id, 'enabled': True} for name in SHORT_NAMES]
        self.response = {'data': [{'cwd': str(self.root), 'skills': copy.deepcopy(self.skills), 'errors': []}]}

    def read_response(self):
        return {'plugin': {'marketplaceName': self.catalog['name'], 'marketplacePath': str(self.cache / '.agents/plugins/marketplace.json'), 'summary': {'id': self.plugin_id, 'name': 'collaborative-work', 'localVersion': self.version, 'version': None, 'installed': False, 'enabled': False, 'source': {'type': 'local', 'path': str(self.cache)}}, 'skills': [{key: value for key, value in skill.items() if key != 'pluginId'} for skill in self.skills]}}


class DiscoveryValidationTests(NativeFixture):
    def test_complete_installed_plugin_passes_and_excludes_unrelated_skills(self):
        self.response['data'][0]['skills'].append({'name': 'other:skill', 'path': '/private/other', 'description': 'unrelated-secret'})
        result = native.validate_installed(self.response, self.source, self.cache)
        self.assertEqual(len(result['skills']), 9)
        self.assertNotIn('unrelated-secret', json.dumps(result))

    def test_missing_duplicate_and_extra_plugin_skills_fail(self):
        variants = [self.skills[:-1], self.skills + [self.skills[0]], self.skills + [{**self.skills[0], 'name': 'collaborative-work:unknown'}]]
        for skills in variants:
            with self.subTest(count=len(skills)):
                self.response['data'][0]['skills'] = skills
                with self.assertRaises(ReleaseError):
                    native.validate_installed(self.response, self.source, self.cache)

    def test_installed_rejects_wrong_plugin_id_disabled_and_nonboolean_enabled(self):
        for field, wrong in [('pluginId', 'collaborative-work@wrong'), ('enabled', False), ('enabled', 1), ('enabled', 'true')]:
            with self.subTest(field=field, wrong=wrong):
                bad = copy.deepcopy(self.response)
                bad['data'][0]['skills'][0][field] = wrong
                with self.assertRaises(ReleaseError):
                    native.validate_installed(bad, self.source, self.cache)

    def test_installed_rejects_wrong_source_or_skill_path(self):
        for wrong_path in [self.source / 'skills/brainstorm/SKILL.md', self.cache / 'skills/story/SKILL.md', self.cache / 'skills/brainstorm/../story/SKILL.md']:
            with self.subTest(path=wrong_path):
                bad = copy.deepcopy(self.response)
                bad['data'][0]['skills'][0]['path'] = str(wrong_path)
                with self.assertRaises(ReleaseError):
                    native.validate_installed(bad, self.source, self.cache)

    def test_wrong_version_cache_directory_fails_even_with_correct_manifest(self):
        wrong_cache = self.cache.with_name('99.99.99')
        shutil.copytree(self.cache, wrong_cache)
        with self.assertRaises(ReleaseError):
            native.validate_installed(self.response, self.source, wrong_cache)

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires extra Windows permissions')
    def test_cache_directory_symlink_cannot_bypass_the_expected_root(self):
        linked = self.root / 'alias' / self.catalog['name'] / 'collaborative-work' / self.version
        linked.parent.mkdir(parents=True)
        linked.symlink_to(self.cache, target_is_directory=True)
        with self.assertRaises(ReleaseError):
            native.validate_installed(self.response, self.source, linked)

    def test_changed_manifest_version_extra_file_and_tampering_fail(self):
        for mutation in ['manifest', 'extra', 'tamper']:
            with self.subTest(mutation=mutation):
                shutil.rmtree(self.cache)
                shutil.copytree(self.source, self.cache)
                if mutation == 'manifest':
                    manifest = dict(self.manifest, version='99.99.99')
                    (self.cache / '.codex-plugin/plugin.json').write_text(json.dumps(manifest))
                elif mutation == 'extra':
                    (self.cache / 'unexpected.txt').write_text('extra')
                else:
                    with (self.cache / 'skills/story/SKILL.md').open('a') as stream:
                        stream.write('\nChanged after installation.\n')
                with self.assertRaises(ReleaseError):
                    native.validate_installed(self.response, self.source, self.cache)

    def test_native_read_accepts_uninstalled_disabled_summary_without_fabricating_fields(self):
        result = native.validate_plugin_read(self.read_response(), self.source, self.cache)
        self.assertIs(result['summary']['enabled'], False)
        self.assertIsNone(result['summary']['version'])
        self.assertTrue(all('pluginId' not in item for item in result['skills']))

    def test_native_read_rejects_wrong_identity_version_and_source(self):
        for field, wrong in [('id', 'collaborative-work@wrong'), ('localVersion', '99.99.99'), ('name', 'wrong')]:
            with self.subTest(field=field):
                bad = self.read_response()
                bad['plugin']['summary'][field] = wrong
                with self.assertRaises(ReleaseError):
                    native.validate_plugin_read(bad, self.source, self.cache)
        bad = self.read_response()
        bad['plugin']['summary']['source']['path'] = str(self.source)
        with self.assertRaises(ReleaseError):
            native.validate_plugin_read(bad, self.source, self.cache)

    def test_native_read_rejects_missing_duplicate_and_tampered_content(self):
        for skills in [self.read_response()['plugin']['skills'][:-1], self.read_response()['plugin']['skills'] + [self.read_response()['plugin']['skills'][0]]]:
            bad = self.read_response()
            bad['plugin']['skills'] = skills
            with self.assertRaises(ReleaseError):
                native.validate_plugin_read(bad, self.source, self.cache)
        (self.cache / 'skills/story/SKILL.md').write_text('tampered')
        with self.assertRaises(ReleaseError):
            native.validate_plugin_read(self.read_response(), self.source, self.cache)


class CachePreflightTests(NativeFixture):
    def run_preflight_cli(self, cache=None):
        result = subprocess.run([sys.executable, '-m', 'cwrelease.native', 'preflight', '--source', str(self.source), '--cache-root', str(cache or self.cache)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stderr, '')
        return result, json.loads(result.stdout)

    def test_absent_cache_is_allowed_without_creating_it_or_starting_a_process(self):
        shutil.rmtree(self.cache)
        with patch.object(native.subprocess, 'Popen', side_effect=AssertionError('preflight must not start a process')):
            report = native.preflight_cache(self.source, self.cache)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['cache_state'], 'absent')
        self.assertFalse(self.cache.exists())
        result, report = self.run_preflight_cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['cache_state'], 'absent')
        self.assertFalse(self.cache.exists())

    def test_identical_cache_can_be_reused_without_codex_or_byte_changes(self):
        before = {str(path.relative_to(self.cache)): path.read_bytes() for path in self.cache.rglob('*') if path.is_file()}
        with patch.object(native.subprocess, 'Popen', side_effect=AssertionError('preflight must not start a process')):
            report = native.preflight_cache(self.source, self.cache)
        self.assertEqual(report['cache_state'], 'identical')
        result, report = self.run_preflight_cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['cache_state'], 'identical')
        self.assertEqual(before, {str(path.relative_to(self.cache)): path.read_bytes() for path in self.cache.rglob('*') if path.is_file()})

    def test_existing_same_version_with_changed_bytes_or_identity_is_refused(self):
        for mutation in ['tamper', 'identity', 'extra']:
            with self.subTest(mutation=mutation):
                shutil.rmtree(self.cache)
                shutil.copytree(self.source, self.cache)
                if mutation == 'tamper':
                    (self.cache / 'skills/story/SKILL.md').write_text('modified')
                elif mutation == 'identity':
                    (self.cache / '.codex-plugin/plugin.json').write_text(json.dumps(dict(self.manifest, version='99.99.99')))
                else:
                    (self.cache / 'extra.txt').write_text('unexpected')
                result, report = self.run_preflight_cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['error']['code'], 'cache_conflict')
                self.assertTrue(report['error']['details']['requires_new_version'])

    def test_wrong_cache_tail_and_non_directory_target_are_refused(self):
        for wrong_tail in [('wrong-market', 'collaborative-work', self.version), (self.catalog['name'], 'wrong-plugin', self.version), (self.catalog['name'], 'collaborative-work', '99.99.99')]:
            with self.subTest(tail=wrong_tail):
                wrong_cache = self.root / 'wrong-cache' / Path(*wrong_tail)
                result, report = self.run_preflight_cli(wrong_cache)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report['error']['code'], 'cache_version_mismatch')
                self.assertFalse(wrong_cache.exists())
        shutil.rmtree(self.cache)
        self.cache.write_text('occupied by a file')
        result, report = self.run_preflight_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['error']['code'], 'cache_conflict')
        self.assertEqual(self.cache.read_text(), 'occupied by a file')


class ReportOutputTests(NativeFixture):
    def tree_bytes(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}

    def output_cli(self, output, mode='preflight', binary=None):
        command = [sys.executable, '-B', '-m', 'cwrelease.native', mode, '--source', str(self.source), '--cache-root', str(self.cache), '--output', str(output)]
        if binary is not None:
            command.extend(['--codex-bin', str(binary)])
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stderr, '')
        return result, json.loads(result.stdout)

    def test_reports_cannot_overwrite_or_add_files_inside_source_or_cache(self):
        original_source, original_cache = self.tree_bytes(self.source), self.tree_bytes(self.cache)
        for root in (self.source, self.cache):
            for suffix in ('.codex-plugin/plugin.json', 'new-reports/nested/report.json'):
                with self.subTest(root=root, suffix=suffix):
                    result, report = self.output_cli(root / suffix)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(report['status'], 'failed')
                    self.assertEqual(self.tree_bytes(self.source), original_source)
                    self.assertEqual(self.tree_bytes(self.cache), original_cache)
                    self.assertFalse((root / 'new-reports').exists())

    def test_existing_evidence_is_preserved_instead_of_overwritten(self):
        evidence = self.root / 'existing-report.json'
        evidence.write_bytes(b'Original evidence bytes.\n')
        result, report = self.output_cli(evidence)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(evidence.read_bytes(), b'Original evidence bytes.\n')

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires extra Windows permissions')
    def test_report_symlinks_do_not_modify_existing_or_create_missing_targets(self):
        target = self.root / 'existing-data'
        target.write_bytes(b'Private unrelated data.\n')
        for target in (target, self.root / 'missing-data'):
            with self.subTest(target=target):
                output = self.root / ('link-' + target.name + '.json')
                output.symlink_to(target)
                result, report = self.output_cli(output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report['status'], 'failed')
                self.assertTrue(output.is_symlink())
                if target.name == 'existing-data':
                    self.assertEqual(target.read_bytes(), b'Private unrelated data.\n')
                else:
                    self.assertFalse(target.exists())

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires extra Windows permissions')
    def test_parent_symlink_cannot_redirect_a_new_report_into_protected_trees(self):
        original_source, original_cache = self.tree_bytes(self.source), self.tree_bytes(self.cache)
        for name, root in (('source-alias', self.source), ('cache-alias', self.cache)):
            with self.subTest(name=name):
                alias = self.root / name
                alias.symlink_to(root, target_is_directory=True)
                result, report = self.output_cli(alias / 'new-reports/nested/report.json')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(self.tree_bytes(self.source), original_source)
                self.assertEqual(self.tree_bytes(self.cache), original_cache)
                self.assertFalse((root / 'new-reports').exists())

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires extra Windows permissions')
    def test_looping_parent_symlink_is_a_json_failure(self):
        alias = self.root / 'loop'
        alias.symlink_to(alias, target_is_directory=True)
        original_source, original_cache = self.tree_bytes(self.source), self.tree_bytes(self.cache)
        result, report = self.output_cli(alias / 'report.json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(self.tree_bytes(self.source), original_source)
        self.assertEqual(self.tree_bytes(self.cache), original_cache)

    @unittest.skipIf(os.name == 'nt', 'Executable fixture uses a POSIX shebang')
    def test_unsafe_output_is_rejected_before_any_codex_process_starts(self):
        marker = self.root / 'codex-started'
        binary = self.root / 'codex'
        binary.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("started")\nprint("controlled-test-codex")\n')
        binary.chmod(0o755)
        output = self.root / 'existing-report.json'
        output.write_bytes(b'Existing evidence.\n')
        for output in (output, self.source / '.codex-plugin/plugin.json'):
            with self.subTest(output=output):
                before = output.read_bytes()
                result, report = self.output_cli(output, mode='read', binary=binary)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report['status'], 'failed')
                self.assertFalse(marker.exists())
                self.assertEqual(output.read_bytes(), before)

    def test_safe_new_report_creates_parent_directories_and_is_never_replaced(self):
        output = self.root / 'reports/new/run/report.json'
        original_source, original_cache = self.tree_bytes(self.source), self.tree_bytes(self.cache)
        result, report = self.output_cli(output)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(output.read_text()), report)
        self.assertEqual(report['cache_state'], 'identical')
        evidence = output.read_bytes()
        result, report = self.output_cli(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), evidence)
        self.assertEqual(self.tree_bytes(self.source), original_source)
        self.assertEqual(self.tree_bytes(self.cache), original_cache)

    def test_failed_check_is_recorded_once_at_a_safe_new_report_path(self):
        (self.cache / 'skills/story/SKILL.md').write_text('tampered')
        output = self.root / 'reports/failure.json'
        result, report = self.output_cli(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['error']['code'], 'cache_conflict')
        self.assertEqual(json.loads(output.read_text()), report)


@unittest.skipIf(os.name == 'nt', 'Executable fixture uses a POSIX shebang; validators are platform-independent')
class NativeCommandTests(NativeFixture):
    def fake_codex(self, behavior='success'):
        fake = self.root / 'codex'
        fake.write_text(FAKE_CODEX.replace('#!PYTHON', '#!' + sys.executable))
        fake.chmod(0o755)
        (self.root / 'scenario.json').write_text(json.dumps({'behavior': behavior, 'response': self.response}))
        return fake

    def run_cli(self, behavior='success', mode='installed', timeout='3'):
        fake = self.fake_codex(behavior)
        command = [sys.executable, '-m', 'cwrelease.native', mode, '--source', str(self.source), '--codex-bin', str(fake), '--cwd', str(self.root), '--timeout', timeout]
        if mode == 'installed':
            command.extend(['--cache-root', str(self.cache)])
        result = subprocess.run(command, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.stderr, '')
        return result, json.loads(result.stdout)

    def test_success_records_runtime_binary_and_source_fingerprints(self):
        result, report = self.run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['cli']['version'], 'codex-cli controlled-test-9.8.7')
        self.assertEqual(report['cli']['sha256'], hashlib.sha256((self.root / 'codex').read_bytes()).hexdigest())
        self.assertEqual(len(report['source']['sha256']), 64)
        self.assertIn('python', report['runtime'])
        self.assertNotIn('unrelated-secret', result.stdout)
        requests = [json.loads(line) for line in (self.root / 'requests.jsonl').read_text().splitlines()]
        self.assertEqual([request['method'] for request in requests], ['initialize', 'initialized', 'skills/list'])
        self.assertEqual(report['model_turns'], 0)

    def test_buffered_partial_and_glued_messages_are_parsed(self):
        for behavior in ['partial', 'glued']:
            with self.subTest(behavior=behavior):
                result, report = self.run_cli(behavior)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(report['status'], 'passed')

    def test_read_mode_verifies_temporary_source_without_requiring_install(self):
        result, report = self.run_cli(mode='read')
        self.assertEqual(result.returncode, 0)
        self.assertFalse(report['response']['summary']['installed'])
        self.assertFalse(report['response']['summary']['enabled'])

    def test_rpc_error_exit_and_invalid_json_fail_with_json_errors(self):
        for behavior in ['rpc-error', 'exit', 'invalid-json']:
            with self.subTest(behavior=behavior):
                result, report = self.run_cli(behavior)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report['status'], 'failed')
                self.assertIsInstance(report['error']['code'], str)
                self.assertTrue(report['error']['message'])
                self.assertNotIn('private-error-data', result.stdout)

    def test_deadline_covers_wait_and_child_is_reaped(self):
        started = time.monotonic()
        result, report = self.run_cli('stall', timeout='.4')
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(report['error']['code'], 'timeout')
        pid = int((self.root / 'pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_invalid_arguments_are_json_failures_and_do_not_launch_codex(self):
        result, report = self.run_cli(timeout='60')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse((self.root / 'pid').exists())

    def test_missing_binary_fails_without_a_traceback(self):
        result = subprocess.run([sys.executable, '-m', 'cwrelease.native', 'read', '--source', str(self.source), '--codex-bin', str(self.root / 'absent')], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
