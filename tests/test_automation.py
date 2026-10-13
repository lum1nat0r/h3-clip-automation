import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from email.message import Message
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e

SCHEMA = json.loads(Path(__file__).with_name("node_schema_fixture.json").read_text(encoding="utf-8"))


def sample_plan(count):
    return {"subject": "The subject in <Picture 1>.", "setting": "Same location.",
            "look": "Natural live action.", "voice": "No speech or narration.", "music": "None.",
            "segments": [{"action": "Continue the movement, then settle.", "camera": "Static camera.",
                          "dialogue": "", "ambience": "Quiet room tone."} for _ in range(count)]}


class FakeServices:
    """Protocol double: exercise real controller sequencing without a GPU."""
    def __init__(self):
        self.calls, self.prompts, self.rows, self.metas = [], [], {}, {}
        self.encodings, self.decodings, self.cache = [], [], {}
        self.drop_encoding_response = False
        self.drop_decoding_response = False
        self.objects = copy.deepcopy(SCHEMA)
        self.drop_response = False
        self.error_job = False
        self.bad_meta = False
        self.export_args = None

    def __call__(self, base, route, data=None, timeout=30):
        self.calls.append((route, copy.deepcopy(data)))
        if route == "/object_info":
            self.objects["H3LoadMCtx"]["input"]["required"]["clip"][0] = list(self.metas) or [""]
            return self.objects
        if route == "/api/tags":
            return {"models": [{"name": e.DEFAULTS["ollama_model"]}]}
        if route == "/api/ps":
            return {"models": []}
        if route == '/system_stats':
            return {'system': {'ram_total': 64*1024**3, 'ram_free': 30*1024**3, 'comfyui_version': '0.37.0'},
                    'devices': [{'type': 'cuda', 'name': 'test GPU', 'vram_total': 16*1024**3, 'vram_free': 15*1024**3}]}
        if route == "/api/chat":
            n = data["format"]["properties"]["segments"]["minItems"]
            return {"message": {"content": json.dumps(sample_plan(n))}, "done_reason": "stop"}
        if route == "/queue":
            return {"queue_running": [], "queue_pending": []}
        if route == "/free":
            return {}
        if route == '/inspire/cache/list':
            return '---- [String Key Caches] ----\n' + '\n'.join(k + ': test' for k in self.cache) + '\n---- [Number Key Caches] ----'
        if route.startswith('/inspire/cache/remove?'):
            key = e.urllib.parse.parse_qs(e.urllib.parse.urlsplit(route).query)['key'][0]
            self.cache.pop(key, None)
            return {}
        if route == "/prompt":
            g = data["prompt"]
            e.validate_graph(g, self.objects)
            if '90' in g:
                self.encodings.append(copy.deepcopy(g))
                pid = 'e' + str(len(self.encodings))
                for k in ('90','91'):
                    self.cache[g[k]['inputs']['key']] = copy.deepcopy(g['9']['inputs'])
                self.rows[pid] = {'prompt': [0,pid,g,data['extra_data'],['90','91']], 'outputs': {},
                                  'status': {'completed': True, 'status_str': 'success', 'messages': []}}
                if self.drop_encoding_response:
                    self.drop_encoding_response = False
                    raise e.JobError('/prompt: encoding response lost after acceptance')
                return {'prompt_id':pid, 'node_errors':{}}
            if '92' in g:
                self.prompts.append(copy.deepcopy(g))
                pid = 'p' + str(len(self.prompts))
                source = copy.deepcopy(self.cache[g['9']['inputs']['key']])
                source['prev'] = g['10']['inputs'].get('clip')
                for k in ('92','93','94'):
                    self.cache[g[k]['inputs']['key']] = source
                self.rows[pid] = {'prompt':[0,pid,g,data['extra_data'],['92','93','94']], 'outputs':{},
                                  'status':{'completed':True,'status_str':'success','messages':[]}}
                if self.error_job:
                    self.rows[pid]['status']={'completed':False,'status_str':'error','messages':[
                        ['execution_error',{'exception_message':'simulated OOM'}]]}
                if self.drop_response:
                    self.drop_response=False
                    raise e.JobError('/prompt: response lost after acceptance')
                return {'prompt_id':pid,'node_errors':{}}
            self.decodings.append(copy.deepcopy(g))
            pid = "d" + str(len(self.decodings))
            save = g["19"]["inputs"]
            clip = save["base_folder"] + "/" + save["filename_prefix"] + "_00001.mp4"
            source = self.cache[g['96']['inputs']['key']]
            prev = source['prev']
            delivered = source["length"] - (39 if prev else 0)
            self.metas[clip] = {"self_id": pid, "parent_id": self.metas[prev]["self_id"] if prev else "",
                                "delivered_frames": str(delivered), "fps": "24"}
            self.rows[pid] = {"prompt": [len(self.prompts), pid, g, data["extra_data"], ["19", "20"]],
                              "outputs": {"20": {"h3_result": [{"clip": clip}]}},
                              "status": {"completed": True, "status_str": "success", "messages": []}}
            if self.drop_decoding_response:
                self.drop_decoding_response = False
                raise e.JobError("/prompt: response lost after acceptance")
            return {"prompt_id": pid, "node_errors": {}}
        if route.startswith("/history/"):
            pid = route.split("/")[-1]
            return {pid: self.rows[pid]} if pid in self.rows else {}
        if route.startswith("/history?"):
            return self.rows
        if route == "/obvpm/h3/clip_meta":
            return {"no_sidecar": "1"} if self.bad_meta else self.metas[data["clip"]]
        if route == "/obvpm/h3/preview_cut":
            self.assert_probe(data)
            total = 0
            for line in data["sequence"].splitlines():
                clip, sep, tail = line.partition(" @ ..")
                total += int(tail) if sep else int(self.metas[clip]["delivered_frames"])
            return {"total": total, "starts": [], "cached": False}
        if route == "/obvpm/h3/export":
            self.export_args = data
            return {"path": data["base_folder"] + "/final_00001.mp4", "cached": False}
        raise AssertionError("Unexpected route: " + route)

    @staticmethod
    def assert_probe(data):
        assert data["probe"] is True, "duration inspection must not render media"


class AutomationTests(unittest.TestCase):
    def setUp(self):
        sleep_patch = patch('resources.time.sleep')
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        image_patch = patch.object(e, "verify_input_image")
        self.image_check = image_patch.start()
        self.addCleanup(image_patch.stop)

    def runner(self, temp, fake, seconds=30):
        r = e.Runner(Path(temp) / "20000101_120000_12345678", lambda _: None, call=fake, poll_seconds=0)
        r.new({"image": "h3_automatik/upload.png", "seconds": seconds, "seed": 123}, "A calm scene with no speech.")
        return r

    def test_lora_normalization_preserves_names_order_duplicates_and_bounds(self):
        name = r"characters\test [v1] @ (copy).safetensors"
        loras = [{'name': name, 'strength': strength} for strength in (-100, 0, 100)]
        self.assertEqual(e.normalize_settings({'loras': loras})['loras'], loras)
        self.assertEqual(e.normalize_settings({})['loras'], [])
        for invalid in (None, {}, [None], [{}], [{'name': 'x'}],
                        [{'name': ' ', 'strength': 1}],
                        [{'name': 'x\x00', 'strength': 1}],
                        [{'name': 'x\n', 'strength': 1}],
                        [{'name': 'x\r', 'strength': 1}],
                        [{'name': 3, 'strength': 1}]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(e.JobError, 'LoRA'):
                e.normalize_settings({'loras': invalid})
        for strength in (True, False, '1', None, float('nan'), float('inf'), -float('inf'), -100.01, 100.01, 10**1000):
            with self.subTest(strength=strength), self.assertRaisesRegex(e.JobError, 'LoRA 2: Stärke'):
                e.normalize_settings({'loras': [{'name': 'ok', 'strength': 1}, {'name': 'bad', 'strength': strength}]})

    def test_uploaded_main_and_extra_images_checked_outside_dropdown(self):
        fake = FakeServices()
        c = e.normalize_settings({"image": "h3_automatik/upload.png",
                                  "extra_images": ["references/profile.png"]})
        e.preflight(c, fake)
        self.assertEqual([c.args[1] for c in self.image_check.call_args_list],
                         ["h3_automatik/upload.png", "references/profile.png"])
        for previous in (None, "h3_automatik/test/take.mp4"):
            g = e.build_graph(c, "text", e.schedule(30)[bool(previous)], "test", 12, previous)
            e.validate_graph(g, SCHEMA)
            self.assertEqual(g["8"]["inputs"]["image"], c["image"])
            self.assertEqual(g["30"]["inputs"]["image"], c["extra_images"][0])
        g["1"]["inputs"]["unet_name"] = "missing_model.safetensors"
        with self.assertRaisesRegex(e.JobError, "UNETLoader.unet_name"):
            e.validate_graph(g, fake.objects)

    def test_missing_uploaded_image_stops_before_planning_or_queue(self):
        self.image_check.side_effect = e.JobError("Start-/Referenzbild ist in ComfyUI nicht vorhanden")
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeServices()
            r = self.runner(temp, fake)
            with self.assertRaisesRegex(e.JobError, "nicht vorhanden"):
                r.run()
            self.assertFalse(any(route in ("/api/chat", "/free", "/prompt") for route, _ in fake.calls))

    def test_all_durations_on_av_grid_and_exact_ending(self):
        for seconds in range(30, 61):
            slots = e.schedule(seconds)
            self.assertEqual(sum(s["keep_frames"] for s in slots), seconds * 24)
            self.assertGreaterEqual(slots[-1]["keep_frames"], 48)
            for i, s in enumerate(slots):
                self.assertEqual(s["raw_frames"] % 17, 5)
                self.assertEqual(s["start_frame"], sum(t["keep_frames"] for t in slots[:i]))
                if i:
                    self.assertEqual((s["raw_frames"] - 39) % 51, 0)
                    self.assertEqual(s["context_frames"], 39)
            self.assertEqual(slots[-1]["end_seconds"], seconds)

    def test_plan_rejects_wrong_count_and_invalid_field_size(self):
        slots = e.schedule(30)
        with self.assertRaises(e.JobError):
            e.validate_plan(sample_plan(1), slots)
        p = sample_plan(len(slots))
        p["segments"][0]["dialogue"] = "word " * 400
        with self.assertRaises(e.JobError):
            e.validate_plan(p, slots)

    def test_explicit_no_speech_and_no_music_enforced_in_schema(self):
        self.assertEqual(e.audio_constraints("Ruhige Szene, keine Sprache und keine Musik."), (True, True))
        self.assertEqual(e.audio_constraints("No dialogue, without music."), (True, True))
        schema = e.plan_schema(7, True, True)
        self.assertEqual(schema["properties"]["segments"]["items"]["properties"]["dialogue"]["const"], "")
        self.assertEqual(schema["properties"]["music"]["const"], "None.")

    def test_plan_repairs_invalid_answer_before_generation(self):
        calls = []
        def call(base, route, data, timeout):
            calls.append(data)
            return {"message": {"content": "{}" if len(calls) == 1 else json.dumps(sample_plan(7))}}
        p = e.create_plan(e.DEFAULTS, "brief", e.schedule(30), lambda _: None, call)
        self.assertEqual(len(p["segments"]), 7)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["keep_alive"], 0)
        self.assertFalse(calls[0]["stream"])

    def test_graph_first_frame_only_on_root_and_raw_latent_saved(self):
        c = {**e.DEFAULTS, "image": "test.png"}
        root = e.build_graph(c, "text", e.schedule(30)[0], "test", 12)
        ext = e.build_graph(c, "text", e.schedule(30)[1], "test", 13, "h3_automatik/test/take.mp4")
        for g in (root, ext):
            e.validate_graph(g, SCHEMA)
            self.assertEqual(g["19"]["inputs"]["samples"], ["16", 1])
            self.assertEqual(g["16"]["inputs"]["latent_image"], ["11", 1])
            self.assertEqual(g["9"]["inputs"]["ref_images.ref_image_0"], ["8", 0])
        self.assertEqual(root["10"]["class_type"], "MiniMaxH3AddGuide")
        self.assertEqual(ext["10"]["class_type"], "H3LoadMCtx")
        self.assertEqual(ext["11"]["inputs"]["pin_specs"], ["10", 1])

    def test_schema_detects_bad_wire_and_missing_node(self):
        c = {**e.DEFAULTS, "image": "test.png"}
        g = e.build_graph(c, "text", e.schedule(30)[0], "test", 12)
        g["17"]["inputs"]["samples"] = ["8", 0]
        with self.assertRaises(e.JobError):
            e.validate_graph(g, SCHEMA)
        del g["8"]
        with self.assertRaises(e.JobError):
            e.validate_graph(g, SCHEMA)

    def test_complete_runs_30_45_60_export_only_once(self):
        for seconds, count in ((30, 7), (45, 11), (60, 14)):
            with tempfile.TemporaryDirectory() as temp:
                fake = FakeServices()
                r = self.runner(temp, fake, seconds)
                final = r.run()
                self.assertEqual(len(fake.prompts), count)
                self.assertEqual(len(r.state["takes"]), count)
                self.assertEqual(r.state["status"], "complete")
                self.assertEqual(sum(x[0] == "/api/chat" for x in fake.calls), 1)
                self.assertEqual(sum(x[0] == "/obvpm/h3/export" for x in fake.calls), 1)
                self.assertIn(" @ ..", fake.export_args["sequence"])
                self.assertEqual(r.run(), final)
                self.assertEqual(len(fake.prompts), count)

    def test_lost_response_recovers_accepted_prompt_without_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeServices()
            fake.drop_response = True
            r = self.runner(temp, fake)
            with self.assertRaises(e.JobError):
                r.run()
            self.assertIsNone(r.state["pending"]["prompt_id"])
            resumed = e.Runner(r.directory, lambda _: None, call=fake, poll_seconds=0)
            resumed.run()
            self.assertEqual(len(fake.prompts), 7)
            self.assertEqual(resumed.state["takes"][0]["prompt_id"], "d1")

    def test_ambiguous_request_never_blindly_resubmitted(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeServices()
            r = self.runner(temp, fake)
            r.state["pending"] = {"request_id": "unresolved", "prompt_id": None}
            with self.assertRaisesRegex(e.JobError, "Kein automatischer Doppelstart"):
                r.recover_submission()
            self.assertEqual(fake.prompts, [])

    def test_execution_error_stops_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeServices()
            fake.error_job = True
            r = self.runner(temp, fake)
            with self.assertRaisesRegex(e.JobError, "simulated OOM"):
                r.run()
            self.assertEqual(len(fake.prompts), 1)
            self.assertIsNone(fake.export_args)
            self.assertIsNone(r.state["pending"])

    def test_missing_sidecar_stops_before_next_take(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeServices()
            fake.bad_meta = True
            r = self.runner(temp, fake)
            with self.assertRaisesRegex(e.JobError, "Bewegungskontext"):
                r.run()
            self.assertEqual(len(fake.prompts), 1)
            self.assertIsNone(fake.export_args)

    def test_pause_does_not_interrupt_or_clear_server_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeServices()
            r = self.runner(temp, fake)
            r.stopped = lambda: True
            with self.assertRaises(e.Paused):
                r.run()
            self.assertFalse(any(route in ("/interrupt", "/prompt") for route, _ in fake.calls))
            self.assertEqual(r.state["status"], "paused")

    def test_busy_server_prevents_llm_and_free(self):
        fake = FakeServices()
        def call(base, route, data=None, timeout=30):
            if route == "/queue":
                return {"queue_running": [[1, "other", {}, {}, []]], "queue_pending": []}
            return fake(base, route, data, timeout)
        with tempfile.TemporaryDirectory() as temp:
            r = self.runner(temp, fake)
            r.call = call
            with self.assertRaises(e.JobError):
                r.run()
            self.assertFalse(any(route in ("/free", "/api/chat", "/prompt") for route, _ in fake.calls))

    def test_path_traversal_and_sequence_injection_rejected(self):
        for path in ("../other", "/root", "C:\\data", "clip.mp4\nother.mp4", "clip.mp4 @ ..1"):
            with self.assertRaises(e.JobError):
                e.relative_path(path)


class InputImageTests(unittest.TestCase):
    @staticmethod
    def response(content_type="image/png", data=b"\x89PNG\r\n\x1a\n" * 8):
        result = io.BytesIO(data)
        result.headers = Message()
        result.headers["Content-Type"] = content_type
        return result

    def test_root_and_nested_paths_use_real_input_file_with_bounded_read(self):
        for image, filename, subfolder in [
            ("test.png", "test.png", ""),
            ("h3_automatik/ansicht ä + #1.png", "ansicht ä + #1.png", "h3_automatik"),
            ("h3_automatik\\nested\\test.png", "test.png", "h3_automatik/nested"),
        ]:
            response = self.response()
            with patch.object(e.urllib.request, "urlopen", return_value=response) as opening:
                with patch.object(response, "read", wraps=response.read) as reading:
                    e.verify_input_image("https://comfy.example/", image)
                    reading.assert_called_once_with(16)
                req = opening.call_args.args[0]
                parsed = e.urllib.parse.urlsplit(req.full_url)
                self.assertEqual(parsed.path, "/view")
                self.assertEqual(e.urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
                                 {"filename": [filename], "subfolder": [subfolder], "type": ["input"]})
                self.assertEqual(req.get_header("Range"), "bytes=0-15")

    def test_missing_file_differs_from_access_or_network_failure(self):
        for code in (404, 403, 500):
            error = e.urllib.error.HTTPError("https://comfy.example/view", code, "error", {}, None)
            with patch.object(e.urllib.request, "urlopen", side_effect=error):
                message = "nicht vorhanden" if code == 404 else f"HTTP {code}"
                with self.assertRaisesRegex(e.JobError, message):
                    e.verify_input_image("https://comfy.example", "h3_automatik/test.png")
        with patch.object(e.urllib.request, "urlopen", side_effect=e.urllib.error.URLError("offline")):
            with self.assertRaisesRegex(e.JobError, "konnte nicht geprüft werden"):
                e.verify_input_image("https://comfy.example", "test.png")

    def test_html_and_empty_files_are_not_images(self):
        for content_type, data in (("text/html", b"login"), ("image/png", b"")):
            with patch.object(e.urllib.request, "urlopen", return_value=self.response(content_type, data)):
                with self.assertRaisesRegex(e.JobError, "keine Bilddatei"):
                    e.verify_input_image("https://comfy.example", "test.png")

    def test_invalid_input_paths_do_not_reach_server(self):
        with patch.object(e.urllib.request, "urlopen") as opening:
            for value in (None, "", "/root.png", "../test.png", "a/../test.png", "C:\\test.png",
                          "a//test.png", "test.png\n", "test.png [output]"):
                with self.assertRaises(e.JobError):
                    e.verify_input_image("https://comfy.example", value)
            opening.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
