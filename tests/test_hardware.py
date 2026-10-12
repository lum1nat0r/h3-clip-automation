import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e
import hardware as h
import resources
import start
from test_automation import FakeServices


def stats(vram=16, ram=24, free=22, name='NVIDIA Blackwell Test GPU'):
    return {'system': {'ram_total':ram*h.GIB, 'ram_free':free*h.GIB, 'comfyui_version':'0.37.0'},
            'devices':[{'type':'cuda','name':name,'vram_total':vram*h.GIB,'vram_free':(vram-1)*h.GIB}]}


def objects():
    return {'UNETLoader':{'input':{'required':{'unet_name':[[s[0] for s in h.MODELS['unet']]]}}},
            'CLIPLoader':{'input':{'required':{'clip_name':[[s[0] for s in h.MODELS['clip']]]}}},
            'VAELoader':{'input':{'required':{'vae_name':[[s[0] for r in ('video_vae','audio_vae') for s in h.MODELS[r]]]}}}}


class HardwareTests(unittest.TestCase):
    def config(self, **profile):
        return {**e.DEFAULTS,'hardware_profile':{**h.DEFAULT_PROFILE, **profile}}

    def test_shared_budget_caps_entered_ram_and_preserves_thresholds(self):
        for ram in (32,64,512):
            result=h.overview(self.config(ram_available_gib=ram),24,objects(),stats())
            self.assertEqual(result['effective_ram_gib'],24)
            self.assertEqual(result['recommended'],'nvfp4')
            self.assertEqual(result['issues'],[])
            p=result['settings']['hardware_profile']
            self.assertEqual([p[k] for k in ('ram_start_gib','ram_stop_gib','vram_start_gib','headroom_gib')],[18,4,12,3])

    def test_larger_verified_hardware_recommends_int8_and_no_quant(self):
        for ram,vram,expected in ((40,24,'int8'),(80,48,'bf16'),(96,80,'bf16_full')):
            result=h.overview(self.config(ram_available_gib=ram,vram_gib=vram),ram,objects(),stats(vram,ram))
            self.assertEqual(result['recommended'],expected)
            chosen=next(p for p in result['presets'] if p['id']==expected)
            self.assertTrue(chosen['applicable'])

    def test_claimed_vram_does_not_override_measured_gpu(self):
        result=h.overview(self.config(ram_available_gib=96,vram_gib=80),96,objects(),stats())
        self.assertEqual(result['recommended'],'nvfp4')
        self.assertFalse(next(p for p in result['presets'] if p['id']=='bf16_full')['applicable'])

    def test_older_gpu_gets_int8_and_nvfp4_encoder_is_allowed(self):
        result=h.overview(self.config(ram_available_gib=40,vram_gib=24),40,objects(),stats(24,40,name='NVIDIA RTX 4090'))
        self.assertEqual(result['recommended'],'int8')
        candidate=next(p for p in result['presets'] if p['id']=='int8')
        self.assertTrue(candidate['applicable'])
        self.assertIn('nvfp4',candidate['models']['clip'])
        self.assertFalse(result['presets'][0]['applicable'])

    def test_missing_and_ambiguous_models_never_silently_applied(self):
        obj=objects()
        names=obj['UNETLoader']['input']['required']['unet_name'][0]
        nv=names.pop(0)
        names.extend(['one/'+nv,'two/'+nv])
        result=h.overview(self.config(),24,obj,stats())
        self.assertFalse(result['presets'][0]['applicable'])
        explicit={**self.config(),'unet':'two/'+nv}
        result=h.overview(explicit,24,obj,stats())
        self.assertTrue(result['presets'][0]['applicable'])
        self.assertEqual(result['presets'][0]['models']['unet'],'two/'+nv)
        result=h.overview(self.config(),24,{},stats())
        self.assertFalse(any(p['applicable'] for p in result['presets']))

    def test_manual_thresholds_survive_normalize_and_new_job(self):
        c=h.bind_settings(self.config(threshold_mode='manual',ram_start_gib=19,ram_stop_gib=3.5,
                                      vram_start_gib=11,headroom_gib=2.5),24)
        with tempfile.TemporaryDirectory() as tmp:
            r=e.Runner(Path(tmp)/'job')
            r.new({**c,'image':'x.png'},'A quiet scene.')
            loaded=e.Runner(r.directory).state['settings']
        self.assertEqual(loaded['hardware_profile'],c['hardware_profile'])
        self.assertEqual(loaded['headroom_gb'],2.5)
        graph=e.build_graph(loaded,'test',e.schedule(30)[0],'test/job',1)
        self.assertEqual(graph['4']['inputs']['headroom_gb'],2.5)

    def test_auto_recalculates_but_manual_does_not(self):
        auto,_=h.resolve_profile({**h.DEFAULT_PROFILE,'ram_available_gib':20},24)
        self.assertEqual(auto['ram_start_gib'],15)
        manual,_=h.resolve_profile({**h.DEFAULT_PROFILE,'threshold_mode':'manual','ram_start_gib':19},24)
        self.assertEqual(manual['ram_start_gib'],19)

    def test_invalid_or_impossible_thresholds_rejected(self):
        for change in ({'ram_available_gib':float('nan')},{'vram_gib':float('inf')},
                       {'ram_available_gib':True},{'gpu_arch':'madeup'},
                       {'threshold_mode':'manual','ram_stop_gib':1},
                       {'threshold_mode':'manual','ram_start_gib':24},
                       {'threshold_mode':'manual','ram_stop_gib':19},
                       {'threshold_mode':'manual','headroom_gib':12},
                       {'threshold_mode':'manual','vram_start_gib':16}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                h.resolve_profile({**h.DEFAULT_PROFILE,**change},24)

    def test_guard_uses_custom_start_stop_and_vram_thresholds(self):
        c=h.bind_settings(self.config(threshold_mode='manual',ram_start_gib=20,ram_stop_gib=5,vram_start_gib=14),24)
        with tempfile.TemporaryDirectory() as tmp:
            data=stats(free=19)
            guard=resources.MemoryGuard(c,Path(tmp),lambda *a,**k:data,lambda _:None)
            v=guard.measure('test')
            with self.assertRaisesRegex(resources.SafetyError,'20.0 GiB'):
                guard.check(v,admission=True)
            data['system']['ram_free']=4.9*h.GIB
            with self.assertRaisesRegex(resources.SafetyError,'5.0 GiB'):
                guard.running()
            data['system']['ram_free']=22*h.GIB
            data['devices'][0]['vram_free']=13*h.GIB
            with self.assertRaisesRegex(resources.SafetyError,'14 GiB'):
                guard.check(guard.measure('vram'),admission=True)
            guard.running()  # occupied VRAM alone is not a running OOM

    def test_wrong_container_measurement_fails_closed(self):
        c=h.bind_settings(self.config(),24)
        with self.assertRaises(resources.SafetyError):
            resources.ram_policy({'ram_total_gib':64,'comfyui_version':'0.37.0'},c)

    def test_each_model_bundle_reaches_real_graph_and_live_schema_validation(self):
        fake=FakeServices()
        for kind,info in objects().items():
            field=next(iter(info['input']['required']))
            fake.objects[kind]['input']['required'][field]=info['input']['required'][field]
        def call(base,route,data=None,timeout=30):
            return stats(80,96,90) if route=='/system_stats' else fake(base,route,data,timeout)
        result=h.overview(self.config(ram_available_gib=96,vram_gib=80),96,objects(),stats(80,96))
        for preset in result['presets']:
            c={**result['settings'],**preset['models']}
            e.preflight(c,call,require_image=False)
            graph=e.build_graph(c,'test',e.schedule(30)[0],'test/job',1)
            for key,node,field in [('unet','1','unet_name'),('clip','5','clip_name'),('video_vae','6','vae_name'),('audio_vae','7','vae_name')]:
                self.assertEqual(graph[node]['inputs'][field],preset['models'][key])

    def test_unknown_model_and_oversized_selection_rejected_before_ollama(self):
        fake=FakeServices()
        fake.objects['UNETLoader']['input']['required']['unet_name'][0].append(h.MODELS['unet'][4][0])
        c=h.bind_settings(self.config(),24)
        c['unet']=h.MODELS['unet'][4][0]
        def call(base,route,data=None,timeout=30):
            return stats() if route=='/system_stats' else fake(base,route,data,timeout)
        with self.assertRaisesRegex(e.JobError,'Hardware/Modelle'):
            e.preflight(c,call,require_image=False)
        self.assertFalse(any(p in ('/prompt','/api/chat') for p,_ in fake.calls))

    def test_saved_settings_persist_hardware_and_manual_overrides_without_mutating_jobs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(start,'BASE',Path(tmp)):
            safety={'blocked':False,'verified_budget_gib':24,'comfy_url':e.DEFAULTS['comfy_url'],'ollama_url':e.DEFAULTS['ollama_url']}
            e.atomic_json(Path(tmp)/'server_safety.json',safety)
            old=Path(tmp)/'runs'/'old'/'job.json'
            e.atomic_json(old,{'settings':e.DEFAULTS})
            before=old.read_bytes()
            cfg=self.config(threshold_mode='manual',ram_stop_gib=4.5)
            cfg['_verified_ram_budget_gib']=999  # cannot forge a larger kernel budget
            saved=start.save_preferences(cfg)
            self.assertEqual(saved['_verified_ram_budget_gib'],24)
            self.assertEqual(start.saved_settings()['hardware_profile']['ram_stop_gib'],4.5)
            self.assertEqual(old.read_bytes(),before)
            with patch.dict(start.CURRENT,running=True),self.assertRaises(e.JobError):
                start.save_preferences(cfg)

    def test_offline_inventory_does_not_claim_stale_models_installed(self):
        with patch.object(start,'request',side_effect=e.JobError('offline')):
            result=start.hardware_info(self.config(),refresh=True)
        self.assertFalse(result['live'])
        self.assertFalse(any(p['applicable'] for p in result['presets']))
        self.assertIn('Server nicht erreichbar',result['warning'])


if __name__=='__main__':
    unittest.main()
