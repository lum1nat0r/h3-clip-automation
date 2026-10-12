import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e
from test_automation import sample_plan, FakeServices


class SpeechGuidanceTests(unittest.TestCase):
    def setUp(self):
        sleep_patch = patch('resources.time.sleep')
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_worker_displays_terminal_error_only_once(self):
        import start
        messages = []
        fake = FakeServices()
        def call(base, route, data=None, timeout=30):
            if route == "/api/chat":
                return {"message": {"content": "{}"}}
            return fake(base, route, data, timeout)
        with tempfile.TemporaryDirectory() as temp, patch.object(e, "verify_input_image"), \
                patch.object(start, "log", messages.append), patch.dict(start.CURRENT, {}, clear=True):
            r = e.Runner(Path(temp)/"job", start.log, call=call, poll_seconds=0)
            r.new({"image": "test.png"}, "A scene.")
            start.work(r)
            self.assertEqual(sum(m.startswith("Fehler: ") for m in messages), 1)
            self.assertIn("nach drei Versuchen", start.CURRENT["error"])

    def test_duration_grid_counts_only_retained_time_after_pauses(self):
        for seconds in range(30, 61):
            slots = e.schedule(seconds)
            report = e.speech_timing(sample_plan(len(slots)), slots)
            self.assertAlmostEqual(report["available_speech_seconds"], seconds - .25*len(slots), places=2)
            self.assertEqual(report["target_words_total"], round((seconds-.25*len(slots))*150/60))
            for slot in slots:
                self.assertNotIn("speech_word_limit", slot)
                self.assertAlmostEqual(slot["speech_seconds"], slot["keep_frames"]/24-.25, places=2)
            self.assertEqual(slots[1]["speech_target_words"], 10)

    def test_tempo_and_pause_change_guidance_without_changing_frames(self):
        normal = e.schedule(45)
        slower = e.schedule(45, {"speech_wpm": 100, "speech_pause_seconds": .75})
        keys = ("index", "start_frame", "raw_frames", "context_frames", "delivered_frames", "keep_frames", "start_seconds", "end_seconds")
        self.assertEqual([{k: s[k] for k in keys} for s in normal], [{k: s[k] for k in keys} for s in slower])
        self.assertLess(slower[1]["speech_target_words"], normal[1]["speech_target_words"])
        for settings in ({"speech_wpm": 0}, {"speech_wpm": float("nan")}, {"speech_pause_seconds": -1}):
            with self.assertRaises(e.JobError):
                e.normalize_settings(settings)

    def test_synthetic_six_and_seven_word_phrases_pass_without_retry(self):
        plan = sample_plan(7)
        for i, dialogue in {
            1: "Heute betrachten wir gemeinsam diesen Gegenstand.",
            3: "Nun drehen wir ihn ein wenig weiter.",
            4: "Hier sehen wir eine weitere Ansicht.",
            5: "Damit endet unser kleiner gemeinsamer Rundgang.",
        }.items():
            plan["segments"][i]["dialogue"] = dialogue
        calls = []
        def call(base, route, body, timeout):
            calls.append(body)
            return {"message": {"content": json.dumps(plan)}}
        actual = e.create_plan(e.DEFAULTS, "A German object demonstration.", e.schedule(30), lambda _: None, call)
        self.assertEqual(actual, plan)
        self.assertEqual(len(calls), 1)
        self.assertEqual(e.speech_timing(actual, e.schedule(30))["warnings"], [])
        self.assertNotIn("speech_word_limit", json.dumps(calls[0]))

    def test_resuming_old_job_migrates_guidance_without_altering_saved_timing(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(e, "verify_input_image"):
            fake = FakeServices()
            r = e.Runner(Path(temp)/"job", lambda _: None, call=fake, poll_seconds=0)
            r.new({"image": "test.png"}, "A natural scene.")
            for key in ("speech_wpm", "speech_pause_seconds"):
                del r.state["settings"][key]
            for slot in r.state["slots"]:
                slot.pop("speech_seconds")
                slot.pop("speech_target_words")
                slot["speech_word_limit"] = 5
            before = copy.deepcopy(r.state["slots"])
            r.save()
            resumed = e.Runner(r.directory, lambda _: None, call=fake, poll_seconds=0)
            resumed.run()
            self.assertEqual(resumed.state["status"], "complete")
            self.assertEqual(resumed.state["settings"]["speech_wpm"], 150)
            self.assertEqual(len(fake.prompts), 7)
            for old, new in zip(before, resumed.state["slots"]):
                self.assertNotIn("speech_word_limit", new)
                for key, value in old.items():
                    if key != "speech_word_limit":
                        self.assertEqual(new[key], value)

    def test_renderer_uses_configured_pacing_instead_of_old_half_second_pause(self):
        plan = sample_plan(7)
        plan["segments"][0]["dialogue"] = "Guten Morgen zusammen."
        c = {**e.DEFAULTS, "speech_wpm": 180, "speech_pause_seconds": .1}
        text = e.render_prompt(plan, plan["segments"][0], e.schedule(30,c)[0], c)
        self.assertIn("180 words per minute", text)
        self.assertIn("0.1 seconds TOTAL", text)
        self.assertNotIn("half a second", text)


if __name__ == "__main__":
    unittest.main()
