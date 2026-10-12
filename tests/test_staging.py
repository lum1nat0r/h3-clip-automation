import copy
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from test_automation import FakeServices, SCHEMA, sample_plan
import engine as e
from staging import split_graph, cache_keys, cache_present, clear_cache


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target in ('resources.time.sleep', 'engine.verify_input_image'):
            p = patch(target); p.start(); self.addCleanup(p.stop)
        self.fake = FakeServices()
        self.r = e.Runner(Path(self.temp.name)/'job', lambda _: None, call=self.fake, poll_seconds=0)
        self.r.new({'image':'test.png'}, 'Quiet scene')

    def test_model_stages_are_disjoint_and_av_references_preserved(self):
        for previous in (None, 'h3_automatik/test/take.mp4'):
            g = e.build_graph(self.r.state['settings'],'text',e.schedule(30)[bool(previous)],'test',12,previous)
            enc, sample, decode = split_graph(g,'a'*32)
            e.validate_graph(enc,SCHEMA); e.validate_graph(sample,SCHEMA,skip_file_choices=True); e.validate_graph(decode,SCHEMA)
            self.assertNotIn('UNETLoader',{n['class_type'] for n in enc.values()})
            self.assertNotIn('CLIPLoader',{n['class_type'] for n in sample.values()})
            self.assertEqual(enc['9'],g['9'])
            self.assertEqual(sample['11']['inputs']['latent'],['89',0])
            self.assertNotIn('VAEDecode',{n['class_type'] for n in sample.values()})
            self.assertNotIn('UNETLoader',{n['class_type'] for n in decode.values()})
            self.assertEqual(decode['19']['inputs']['metadata'],g['19']['inputs']['metadata'])
            self.assertEqual(decode['19']['inputs']['samples'],['96',0])
            self.assertEqual(decode['19']['inputs']['pins'],['98',0])
            self.assertEqual(sample['10']['class_type'],g['10']['class_type'])
            self.assertEqual(sample['9']['inputs']['key'],enc['90']['inputs']['key'])

    def test_encoder_submission_recovered_without_duplicate_or_replanning(self):
        self.fake.drop_encoding_response=True
        with self.assertRaises(e.JobError):self.r.run()
        self.assertEqual(self.r.state['pending']['phase'],'encoding')
        self.assertIsNone(self.r.state['pending']['prompt_id'])
        e.Runner(self.r.directory,lambda _:None,call=self.fake,poll_seconds=0).run()
        self.assertEqual(len(self.fake.encodings),7)
        self.assertEqual(len(self.fake.prompts),7)
        self.assertEqual(sum(route=='/api/chat' for route,_ in self.fake.calls),1)
        self.assertFalse(self.fake.cache)

    def test_completed_decode_is_recovered_without_duplicate_files(self):
        self.fake.drop_decoding_response=True
        with self.assertRaises(e.JobError):self.r.run()
        self.assertEqual(self.r.state['pending']['phase'],'decoding')
        e.Runner(self.r.directory,lambda _:None,call=self.fake,poll_seconds=0).run()
        self.assertEqual(len(self.fake.decodings),7)
        self.assertEqual(len(self.fake.prompts),7)
        self.assertFalse(self.fake.cache)

    def test_cache_loss_on_restart_reencodes_without_duplicate_take(self):
        self.fake.drop_encoding_response=True
        with self.assertRaises(e.JobError):self.r.run()
        self.fake.cache.clear()
        e.Runner(self.r.directory,lambda _:None,call=self.fake,poll_seconds=0).run()
        self.assertEqual(len(self.fake.encodings),8)
        self.assertEqual(len(self.fake.prompts),7)
        self.assertFalse(self.fake.cache)

    def test_confirmed_old_guard_interrupt_can_resume_with_saved_plan(self):
        self.r.state.update(plan=sample_plan(7),status='safety_stopped',
                            pending={'request_id':'old','prompt_id':'old-prompt','take_index':0,'submitted_at':time.time()-7200})
        self.fake.rows['old-prompt']={'status':{'completed':False,'status_str':'error',
                                              'messages':[['execution_interrupted',{'node_id':'16'}]]}}
        self.r.run()
        self.assertEqual(len(self.r.state['takes']),7)
        self.assertEqual(len(self.fake.encodings),7)
        self.assertFalse(any(route=='/api/chat' for route,_ in self.fake.calls))

    def test_oom_is_never_retried_as_a_guard_interrupt(self):
        self.r.state.update(plan=sample_plan(7),status='safety_stopped',
                            pending={'request_id':'old','prompt_id':'old-prompt','take_index':0})
        self.fake.rows['old-prompt']={'status':{'status_str':'error','messages':[
            ['execution_error',{'exception_message':'CUDA out of memory'}]]}}
        with self.assertRaises(e.JobError):self.r.run()
        self.assertTrue(self.r.state['safety_hold'])
        self.assertFalse(self.fake.encodings)
        self.assertFalse(self.fake.prompts)

    def test_cleanup_only_removes_this_tokens_keys(self):
        own,other='a'*32,'b'*32
        self.fake.cache={k:{} for k in cache_keys(own)+cache_keys(other)}
        self.assertTrue(cache_present(self.fake,'test',own))
        clear_cache(self.fake,'test',own)
        self.assertFalse(cache_present(self.fake,'test',own))
        self.assertTrue(cache_present(self.fake,'test',other))
        with self.assertRaises(ValueError):clear_cache(self.fake,'test','*')

    def test_missing_cache_after_free_blocks_video_submission(self):
        def call(base,route,data=None,timeout=30):
            result=self.fake(base,route,data,timeout)
            if route=='/free' and self.fake.encodings:self.fake.cache.clear()
            return result
        self.r.call=call
        with self.assertRaisesRegex(e.JobError,'Zwischenspeicher nach Freigabe verloren'):self.r.run()
        self.assertEqual(len(self.fake.encodings),1)
        self.assertFalse(self.fake.prompts)

    def test_pause_during_encoding_finishes_this_take_with_guarding(self):
        def call(base,route,data=None,timeout=30):
            value=self.fake(base,route,data,timeout)
            if route=='/prompt' and '90' in data['prompt']:
                self.r.stopped=lambda:True
            return value
        self.r.call=call
        with self.assertRaises(e.Paused):self.r.run()
        self.assertEqual(len(self.r.state['takes']),1)
        self.assertEqual(len(self.fake.encodings),1)
        self.assertEqual(len(self.fake.prompts),1)
        self.assertEqual(len(self.fake.decodings),1)
        self.assertFalse(self.fake.cache)
        self.assertIsNone(self.r.state['pending'])


if __name__=='__main__':unittest.main()
