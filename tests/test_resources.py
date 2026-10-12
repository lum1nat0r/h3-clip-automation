import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_automation import FakeServices, sample_plan
import engine as e
import resources as m
import library


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeServices()
        self.logs = []
        for name in ('resources.time.sleep', 'engine.verify_input_image'):
            mock = patch(name)
            mock.start()
            self.addCleanup(mock.stop)

    def guard(self, call=None):
        return m.MemoryGuard(e.DEFAULTS, self.root, call or self.fake, self.logs.append)

    def runner(self, call=None, **settings):
        r = e.Runner(self.root/'job', self.logs.append, call=call or self.fake, poll_seconds=0)
        r.new({'image': 'test.png', **settings}, 'A quiet scene without speech.')
        return r

    def stats_call(self, change):
        def call(base, route, data=None, timeout=30):
            result = self.fake(base, route, data, timeout)
            if route == '/system_stats':
                change(result)
            return result
        return call

    def test_resolution_cap_preserves_shape_as_far_as_grid_allows(self):
        self.assertEqual(m.fit_resolution(736,736), (544,544))
        self.assertEqual(m.fit_resolution(736,416), (736,416))
        self.assertEqual(m.fit_resolution(416,736), (416,736))
        for w in range(128,2049,32):
            for h in range(128,2049,32):
                a,b = m.fit_resolution(w,h)
                self.assertLessEqual(a*b, m.MAX_PIXELS)
                self.assertLessEqual(max(a,b), m.MAX_SIDE)
                self.assertEqual(a%32+b%32, 0)
        r = self.runner(width=736, height=736)
        self.assertEqual(r.state['requested_resolution'], [736,736])
        self.assertEqual([r.state['settings'][k] for k in ('width','height')], [544,544])

    def test_old_large_pending_job_is_preserved_and_never_queued(self):
        r = self.runner()
        r.state['settings'].update(width=736,height=736)
        r.state['pending'] = {'prompt_id': 'old-third-take', 'request_id':'old'}
        r.state['takes'] = [{'clip':'old1'}, {'clip':'old2'}]
        pending = copy.deepcopy(r.state['pending'])
        with self.assertRaisesRegex(m.SafetyError, '544 × 544'):
            r.run()
        self.assertEqual(r.state['pending'], pending)
        self.assertEqual(len(r.state['takes']), 2)
        self.assertEqual(self.fake.calls, [])

    def test_low_ram_prevents_ollama_and_gpu_even_after_successful_free_response(self):
        call = self.stats_call(lambda s: s['system'].update(ram_free=20*m.GIB))
        r = self.runner(call)
        with self.assertRaisesRegex(m.SafetyError, '24.0 GiB'):
            r.run()
        self.assertEqual(r.state['status'], 'safety_stopped')
        self.assertFalse(any(p in ('/api/chat','/prompt') for p,_ in self.fake.calls))
        self.assertTrue(any(p=='/free' for p,_ in self.fake.calls))

    def test_insufficient_vram_blocks_start(self):
        call = self.stats_call(lambda s: s['devices'][0].update(vram_free=10*m.GIB))
        with self.assertRaisesRegex(m.SafetyError, 'VRAM'):
            self.guard(call).prepare('before_take')
        self.assertFalse(self.fake.prompts)

    def test_verified_container_budget_accepts_idle_server_overhead(self):
        call = self.stats_call(lambda s: s['system'].update(ram_total=24*m.GIB, ram_free=22.8*m.GIB, comfyui_version='0.37.0'))
        self.guard(call).prepare('before_take')
        self.assertEqual(sum(p=='/system_stats' for p,_ in self.fake.calls), 2)
        self.assertIn('24-GiB-Containerbudget', self.logs[-1])

    def test_container_pressure_prevents_any_model_or_render_request(self):
        call = self.stats_call(lambda s: s['system'].update(ram_total=24*m.GIB, ram_free=17.9*m.GIB, comfyui_version='0.37.0'))
        r = self.runner(call)
        with self.assertRaisesRegex(m.SafetyError, '18.0 GiB'):
            r.run()
        self.assertFalse(any(p in ('/api/chat','/prompt') for p,_ in self.fake.calls))

    def test_container_running_reserve_is_enforced(self):
        call = self.stats_call(lambda s: s['system'].update(ram_total=24*m.GIB, ram_free=3.9*m.GIB, comfyui_version='0.37.0'))
        with self.assertRaisesRegex(m.SafetyError, '4.0 GiB'):
            self.guard(call).running()

    def test_unexpected_small_budget_or_unknown_reporting_fails_closed(self):
        for total, version in ((16,'0.37.0'), (24,'unknown'), (24,'0.36.0')):
            call = self.stats_call(lambda s: s['system'].update(ram_total=total*m.GIB, ram_free=total*m.GIB, comfyui_version=version))
            with self.assertRaises(m.SafetyError):
                self.guard(call).prepare('before_take')
        self.assertFalse(self.fake.prompts)

    def test_missing_or_invalid_telemetry_fails_closed(self):
        for value in ({}, {'system':{}}, {'system':{'ram_free':float('nan')}}):
            def call(base,route,data=None,timeout=30):
                if route=='/system_stats': return value
                return self.fake(base,route,data,timeout)
            with self.assertRaises(m.SafetyError):
                self.guard(call).measure('test')
        call = self.stats_call(lambda s: s['system'].update(ram_free=float('nan')))
        with self.assertRaises(m.SafetyError):
            self.guard(call).measure('test')
        self.assertFalse(self.fake.prompts)

    def test_all_ollama_models_must_be_unloaded_without_unloading_foreign_jobs(self):
        def call(base,route,data=None,timeout=30):
            if route=='/api/ps': return {'models':[{'name':'another-model'}]}
            return self.fake(base,route,data,timeout)
        with self.assertRaisesRegex(m.SafetyError, 'noch ein Modell'):
            self.guard(call).prepare('before_take')
        self.assertFalse(any(p in ('/free','/prompt','/api/generate') for p,_ in self.fake.calls))

    def test_cleanup_waits_for_two_healthy_samples(self):
        samples = iter((10, 30, 20, 30, 30))
        call = self.stats_call(lambda s: s['system'].update(ram_free=next(samples)*m.GIB))
        self.guard(call).prepare('before_take')
        self.assertEqual(sum(p=='/system_stats' for p,_ in self.fake.calls), 5)

    def test_every_take_and_export_follow_a_cleanup_and_admission_check(self):
        r = self.runner()
        r.run()
        phases = [json.loads(line)['phase'] for line in (r.directory/'resources.jsonl').read_text().splitlines()]
        self.assertEqual(phases.count('before_take'), 14)  # two samples per seven takes
        self.assertEqual(phases.count('before_encoding'), 14)
        self.assertEqual(phases.count('before_encoding_submit'), 7)
        self.assertEqual(phases.count('before_sampling_submit'), 7)
        self.assertEqual(phases.count('before_decoding'), 14)
        self.assertEqual(phases.count('before_decoding_submit'), 7)
        self.assertEqual(phases.count('before_export'), 2)
        free_count = 0
        for route,_ in self.fake.calls:
            if route=='/free': free_count+=1
            if route in ('/api/chat','/prompt','/obvpm/h3/export'):
                self.assertEqual(free_count, 1)
                free_count=0
        self.assertTrue(library.job_details(r.directory)['has_resources'])
        self.assertIn('before_sampling_submit',library.job_file(r.directory,'resources.jsonl')['content'])

    def active_call(self, fail='ram'):
        def call(base,route,data=None,timeout=30):
            if route in ('/interrupt','/queue') and data is not None:
                self.fake.calls.append((route,copy.deepcopy(data)))
                return {}
            if route=='/history/p1': return {}
            if route=='/queue' and self.fake.prompts:
                return {'queue_running':[[0,'p1',{}, {},[]]],'queue_pending':[]}
            if route=='/system_stats' and self.fake.prompts:
                if fail=='network': raise e.JobError('connection lost')
                result=self.fake(base,route,data,timeout)
                result['system']['ram_free']=7*m.GIB
                return result
            return self.fake(base,route,data,timeout)
        return call

    def test_pressure_or_lost_telemetry_interrupts_only_own_id_and_preserves_pending(self):
        for fail in ('ram','network'):
            with self.subTest(fail=fail):
                self.fake=FakeServices()
                r=self.runner(self.active_call(fail))
                with self.assertRaises(m.SafetyError): r.run()
                self.assertEqual(len(self.fake.prompts),1)
                self.assertEqual(r.state['pending']['prompt_id'],'p1')
                self.assertIn(('/interrupt',{'prompt_id':'p1'}),self.fake.calls)
                self.assertIn(('/queue',{'delete':['p1']}),self.fake.calls)
                self.assertNotIn(('/queue',{'clear':True}),self.fake.calls)
                self.assertIsNone(self.fake.export_args)

    def test_full_vram_during_sampling_is_not_alone_an_error(self):
        call=self.stats_call(lambda s:s['devices'][0].update(vram_free=0))
        self.guard(call).running()

    def test_unsupported_server_never_receives_potential_global_interrupt(self):
        g=self.guard()
        for version in ('','unknown','0.2.0'):
            g.latest={'comfyui_version':version}
            g.interrupt('own')
        self.assertFalse(self.fake.calls)

    def test_oom_locks_resume_without_repeating_generation(self):
        self.fake.error_job=True
        r=self.runner()
        with self.assertRaises(e.JobError): r.run()
        self.assertTrue(r.state['safety_hold'])
        with self.assertRaises(m.SafetyError): r.run()
        self.assertEqual(len(self.fake.prompts),1)

    def test_persisted_deadline_survives_resume(self):
        r=self.runner()
        r.state.update(plan=sample_plan(7), pending={'request_id':'old','prompt_id':'p1',
                                                    'submitted_at':time.time()-3601})
        with self.assertRaisesRegex(m.SafetyError,'eine Stunde'): r.run()
        self.assertFalse(self.fake.prompts)

    def test_higher_load_options_are_blocked(self):
        for settings in ({'extra_images':['extra.png']},{'reference_audio':'long.wav'},
                         {'loras':[{'name':'big','strength':1}]}):
            r=self.runner(**settings)
            with self.assertRaises(m.SafetyError):r.run()
            self.assertFalse(self.fake.prompts)

    def test_incident_gate_blocks_start_and_resume_without_mutating_projects(self):
        import start
        with patch.object(start, 'BASE', self.root):
            for data in (None, {'blocked':False}, {'blocked':False,'verified_budget_gib':'bad'},
                         {'blocked':True,'verified_budget_gib':24}):
                if data is not None:e.atomic_json(self.root/'server_safety.json',data)
                for resume in (False,True):
                    with self.assertRaises(e.JobError):start.launch({},resume=resume)
                self.assertFalse((self.root/'runs').exists())
            e.atomic_json(self.root/'server_safety.json',{'blocked':False,'verified_budget_gib':24})
            self.assertFalse(start.server_safety()['blocked'])

    def test_verified_limits_cannot_be_reused_for_a_different_server(self):
        import start
        with patch.object(start, 'BASE', self.root):
            e.atomic_json(self.root/'server_safety.json', {
                'blocked': False, 'verified_budget_gib': 24,
                'comfy_url': e.DEFAULTS['comfy_url'], 'ollama_url': e.DEFAULTS['ollama_url'],
            })
            for key in ('comfy_url', 'ollama_url'):
                with self.assertRaisesRegex(e.JobError, 'Serveradresse'):
                    start.launch({'settings': {key: 'https://another-server.invalid', 'image': 'test.png'},
                                  'master_prompt': 'test'})
            self.assertFalse((self.root/'runs').exists())
            self.assertFalse((self.root/'settings.json').exists())


if __name__=='__main__': unittest.main()
