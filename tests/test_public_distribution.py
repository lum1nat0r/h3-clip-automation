import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine
import start

spec = importlib.util.spec_from_file_location('privacy_check', ROOT/'tools/check_staged_privacy.py')
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class PublicDistributionTests(unittest.TestCase):
    def test_neutral_defaults_and_no_implicit_server_release(self):
        self.assertEqual(engine.DEFAULTS['comfy_url'], 'http://127.0.0.1:8188')
        self.assertEqual(engine.DEFAULTS['ollama_url'], 'http://127.0.0.1:11434')
        self.assertEqual(engine.DEFAULTS['ollama_model'], 'YOUR_OLLAMA_MODEL')
        with tempfile.TemporaryDirectory() as directory, patch.object(start, 'BASE', Path(directory)):
            self.assertTrue(start.server_safety()['blocked'])
            self.assertIsNone(start.verified_budget(engine.DEFAULTS))
            self.assertEqual(start.saved_settings()['image'], '')

    def test_safety_example_cannot_unlock_generation(self):
        example = json.loads((ROOT/'server_safety.example.json').read_text(encoding='utf-8'))
        self.assertTrue(example['blocked'])
        with tempfile.TemporaryDirectory() as directory, patch.object(start, 'BASE', Path(directory)):
            engine.atomic_json(Path(directory)/'server_safety.json', example)
            self.assertTrue(start.server_safety()['blocked'])

    def test_privacy_check_recognizes_runtime_and_secrets(self):
        for name in ('settings.json', 'server_safety.json', 'runs/example/job.json', '.env', 'photo.png'):
            self.assertTrue(privacy.path_issues(name), name)
        for name in ('settings.example.json', 'tests/node_schema_fixture.json', 'engine.py'):
            self.assertFalse(privacy.path_issues(name), name)
        cases = [
            'ghp_' + 'A'*30,
            '-----BEGIN ' + 'PRIVATE KEY-----',
            'http://' + 'user:pass@service.example',
            'C:' + '\\Users\\' + 'example\\settings.json',
            '.'.join(('100', '64', '0', '9')),
        ]
        for value in cases:
            self.assertTrue(privacy.content_issues(value))
        self.assertFalse(privacy.content_issues('http://127.0.0.1:8188'))
        self.assertFalse(privacy.content_issues('https://comfy.example'))

    def test_template_has_no_saved_media_inputs(self):
        template = json.loads((ROOT/'H3_NVFP4_Timeline_Editor.json').read_text(encoding='utf-8'))
        for node in template['nodes']:
            if node['type'] in ('LoadImage', 'LoadAudio', 'LoadVideo'):
                self.assertEqual(node['widgets_values'][0], '')
        self.assertEqual(set(template['extra']['local_workflow_provenance']),
                         {'source', 'commit', 'source_sha256', 'license'})
