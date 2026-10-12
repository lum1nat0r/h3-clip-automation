import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import container_memory as cm
import engine
import hardware
import model_setup as ms
import resources
import start
from runtime_lock import exclusive, BusyError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('container_entrypoint', ROOT / 'deploy/entrypoint.py')
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        (self.proc / 'self').mkdir(parents=True)
        (self.proc / 'meminfo').write_text(f'MemTotal: {128*1024**2} kB\nMemAvailable: {80*1024**2} kB\n')
        self.mount = self.root / 'cgroup'
        self.group = self.mount / 'pod'
        self.group.mkdir(parents=True)
        self.group_values(self.mount, 64, 8)
        self.group_values(self.group, 32, 4)

    def group_values(self, path, limit, used, v=2, cache=0):
        (path / ('memory.max' if v == 2 else 'memory.limit_in_bytes')).write_text(str(limit*cm.GIB) if limit else 'max' if v == 2 else str(2**63-4096))
        (path / ('memory.current' if v == 2 else 'memory.usage_in_bytes')).write_text(str(used*cm.GIB))
        (path / 'memory.stat').write_text(f"{'inactive_file' if v == 2 else 'total_inactive_file'} {cache*cm.GIB}\n")

    def read(self, env=None, v=2):
        return cm.snapshot(self.proc, (v, self.group, self.mount), env or {})

    def test_effective_parent_pressure_and_child_limit(self):
        self.group_values(self.mount, 24, 20)
        result = self.read()
        self.assertEqual(result['budget_gib'], 24)
        self.assertEqual(result['ram_free_gib'], 4)
        self.assertTrue(result['hard_limit_verified'])

    def test_inactive_file_cache_is_reclaimable(self):
        self.group_values(self.group, 32, 31, cache=25)
        self.assertEqual(self.read()['ram_free_gib'], 26)

    def test_v1_and_unlimited_budget_are_distinct(self):
        for path in (self.mount, self.group):
            self.group_values(path, None, 4, v=1)
        with self.assertRaisesRegex(ValueError, 'Kernel'):
            self.read(v=1)
        result = self.read({'H3_MEMORY_MODE':'budget', 'H3_RAM_BUDGET_GIB':'48'}, v=1)
        self.assertEqual(result['ram_free_gib'], 44)
        self.assertFalse(result['hard_limit_verified'])
        self.assertIsNone(result['hard_limit_gib'])

    def test_missing_usage_invalid_config_and_no_budget_fail_closed(self):
        for value in ('nan', '-1', '999999'):
            with self.assertRaises(ValueError):
                self.read({'H3_MEMORY_MODE':'budget','H3_RAM_BUDGET_GIB':value})
        with self.assertRaises(ValueError):
            self.read({'H3_MEMORY_MODE':'budget'})
        (self.group/'memory.current').unlink()
        with self.assertRaises(OSError):
            self.read()

    def test_locate_namespace_and_mount_root(self):
        (self.proc/'self/cgroup').write_text('0::/pod\n')
        (self.proc/'self/mountinfo').write_text(f'1 2 0:1 / {self.mount.as_posix()} rw - cgroup2 cgroup rw\n')
        self.assertEqual(cm.locate(self.proc), (2, self.group, self.mount))
        (self.proc/'self/cgroup').write_text('0::/../../escape\n')
        with self.assertRaises(ValueError):
            cm.locate(self.proc)

    def test_container_budget_does_not_claim_verified_limit(self):
        with patch.dict(os.environ, {'H3_CONTAINER':'1','H3_MEMORY_MODE':'budget'}):
            c = hardware.bind_settings(dict(engine.DEFAULTS), 48)
            self.assertIsNone(c['_verified_ram_budget_gib'])
            self.assertEqual(c['_ram_budget_gib'], 48)
            with patch.object(cm, 'snapshot', return_value=self.read({'H3_MEMORY_MODE':'budget','H3_RAM_BUDGET_GIB':'24'})):
                self.assertIsNone(start.server_safety()['verified_budget_gib'])

    def test_planning_cap_is_not_reported_as_kernel_limit(self):
        value = self.read({'H3_RAM_BUDGET_GIB':'16'})
        with patch.object(cm, 'snapshot', return_value=value):
            state = cm.safety()
        self.assertEqual(state['budget_gib'], 16)
        self.assertEqual(state['verified_budget_gib'], 32)

    def test_guard_uses_container_telemetry_instead_of_host_comfy_total(self):
        stats = {'system':{'ram_total':128*cm.GIB, 'ram_free':80*cm.GIB, 'comfyui_version':'0.37.0'},
                 'devices':[{'type':'cuda','name':'GPU','vram_total':16*cm.GIB,'vram_free':15*cm.GIB}]}
        value = self.read({'H3_MEMORY_MODE':'budget','H3_RAM_BUDGET_GIB':'24'})
        with patch.dict(os.environ, {'H3_CONTAINER':'1','H3_MEMORY_MODE':'budget'}):
            config = hardware.bind_settings(dict(engine.DEFAULTS), 24)
            with patch.object(cm, 'snapshot', return_value=value):
                guard = resources.MemoryGuard(config, self.root/'job', Mock(return_value=stats), lambda _:None)
                measured = guard.measure('test')
                self.assertEqual(measured['ram_total_gib'], 24)
                self.assertEqual(measured['ram_free_gib'], 20)
                self.assertFalse(measured['hard_limit_verified'])
                guard.check(measured, admission=True)
                value['budget_gib'] = 32
                with self.assertRaises(resources.SafetyError):
                    guard.measure('changed-limit')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = b'validated model data'
        self.source = dict(repo='example/model', revision='a'*40, path='vae/model.safetensors',
                           target='vae', filename='model.safetensors', size=len(self.content),
                           sha256=hashlib.sha256(self.content).hexdigest())
        self.target = self.root/'comfy/models/vae/model.safetensors'
        self.target.parent.mkdir(parents=True)

    def fetch(self, **kwargs):
        self.assertEqual(kwargs['revision'], 'a'*40)
        staged = self.root/'stage.safetensors'
        staged.write_bytes(self.content)
        return staged

    def test_download_hash_idempotency_and_token_only_in_environment(self):
        fetch = Mock(side_effect=self.fetch)
        with patch.dict(os.environ, {'HF_TOKEN':'test-secret'}):
            ms.download(self.source, self.root, fetch)
            ms.download(self.source, self.root, fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs['token'], 'test-secret')
        self.assertEqual(self.target.read_bytes(), self.content)

    def test_failure_preserves_existing_model_and_redacts_external_errors(self):
        self.target.write_bytes(b'previous')
        with self.assertRaisesRegex(RuntimeError, 'HF_TOKEN') as caught:
            ms.download(self.source, self.root, Mock(side_effect=RuntimeError('secret-in-url')))
        self.assertNotIn('secret-in-url', str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b'previous')

    def test_checksum_failure_never_replaces_existing_file(self):
        self.target.write_bytes(b'previous')
        staged = self.root/'bad'
        staged.write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError, 'Prüfsumme'):
            ms.download(self.source, self.root, Mock(return_value=staged))
        self.assertEqual(self.target.read_bytes(), b'previous')

    def test_aborted_download_can_be_retried(self):
        with self.assertRaises(KeyboardInterrupt):
            ms.download(self.source, self.root, Mock(side_effect=KeyboardInterrupt))
        ms.download(self.source, self.root, self.fetch)
        self.assertEqual(self.target.read_bytes(), self.content)

    def test_preferences_only_written_after_successful_validation(self):
        app = self.root/'app'
        app.mkdir()
        saved = app/'settings.json'
        saved.write_text('{"existing":true}')
        c = dict(engine.DEFAULTS)
        with patch.object(ms, 'preflight', side_effect=RuntimeError('missing node')):
            with self.assertRaises(RuntimeError):
                ms.apply_selection(self.root, c, call=Mock(return_value={}))
        self.assertEqual(saved.read_text(), '{"existing":true}')
        with patch.object(ms, 'preflight'):
            ms.apply_selection(self.root, c, call=Mock(return_value={}))
        self.assertEqual(json.loads(saved.read_text()), c)

    def test_disk_and_cloud_model_rejected_before_installation(self):
        with patch.object(ms.shutil, 'disk_usage', return_value=Mock(free=1)):
            with self.assertRaisesRegex(RuntimeError, 'Speicher'):
                ms.require_space(self.root, 2)
        for name in ('model', 'model:cloud', '../model:tag', 'https://example/model'):
            with self.assertRaises(ValueError):
                ms.model_name(name)

    def test_running_job_blocks_installer_before_network_calls(self):
        call = Mock()
        with exclusive(self.root/'app'):
            with self.assertRaises(BusyError):
                ms.install(self.root, call=call)
        call.assert_not_called()

    def test_installer_blocks_settings_save_and_releases_after_exception(self):
        with patch.object(start, 'BASE', self.root/'app'):
            with exclusive(self.root/'app'):
                with self.assertRaises(BusyError):
                    start.save_preferences(dict(engine.DEFAULTS))
            start.save_preferences(dict(engine.DEFAULTS))
        self.assertTrue((self.root/'app/settings.json').exists())

    def test_all_hardware_models_have_pinned_download_metadata(self):
        for specs in hardware.MODELS.values():
            for item in specs:
                source = hardware.SOURCES[item[0]]
                self.assertRegex(source['revision'], r'^[a-f0-9]{40}$')
                self.assertRegex(source['sha256'], r'^[a-f0-9]{64}$')
                self.assertGreater(source['size'], 0)


class DeploymentTests(unittest.TestCase):
    def test_auth_is_required_before_any_runtime_changes(self):
        for env in ({}, {'H3_AUTH_USER':'-option','H3_AUTH_PASSWORD':'a'*20},
                    {'H3_AUTH_USER':'test','H3_AUTH_PASSWORD':'a'*20+'\n'}):
            with self.assertRaises(ValueError):
                entrypoint.configure(env)

    def test_proxy_carries_websocket_streaming_and_auth(self):
        conf = entrypoint.nginx_config('selfsigned')
        self.assertIn('listen 8080 ssl', conf)
        self.assertIn('listen 8081 ssl', conf)
        self.assertEqual(conf.count('auth_basic_user_file'), 2)
        self.assertIn('proxy_buffering off', conf)
        self.assertIn('proxy_set_header Upgrade $http_upgrade', conf)
        self.assertNotIn('11434', conf)
        self.assertNotIn('ssl_certificate', entrypoint.nginx_config('proxy'))

    def test_no_automatic_download_or_job_resume_in_service_commands(self):
        conf = entrypoint.supervisor_config(Path('/workspace/h3'), True)
        self.assertIn('--listen 127.0.0.1', conf)
        self.assertNotIn('model_setup', conf)
        self.assertNotIn('--resume', conf)
        self.assertIn('stopasgroup=true', conf)

    def test_container_rejects_external_service_configuration(self):
        with patch.dict(os.environ, {'H3_CONTAINER':'1'}):
            with self.assertRaises(engine.JobError):
                engine.normalize_settings({'comfy_url':'https://example.com'})

    def test_data_directory_leaves_source_assets_in_place(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(start, 'BASE', Path(directory)):
            self.assertEqual(start.saved_settings()['image'], '')
            self.assertTrue((start.SOURCE/'index.html').is_file())
            self.assertFalse((Path(directory)/'index.html').exists())

    def test_data_directory_environment_is_used_on_fresh_start(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, H3_DATA_DIR=directory)
            result = subprocess.check_output([sys.executable, '-B', '-c',
                'import start,json; print(json.dumps([str(start.BASE),str(start.SOURCE)]))'], cwd=ROOT, env=env, text=True)
            data, source = json.loads(result)
            self.assertEqual(Path(data), Path(directory).resolve())
            self.assertEqual(Path(source), ROOT)


if __name__ == '__main__':
    unittest.main()
