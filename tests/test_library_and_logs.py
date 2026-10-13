import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e
import library as lib
from test_automation import sample_plan, FakeServices


class PlanningLogTests(unittest.TestCase):
    def test_every_rejected_response_and_request_survives_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "ollama"
            count = len(e.schedule(30))
            def call(base, route, data, timeout):
                records = sorted(folder.glob("*.json"))
                pending = [lib.read_json(p) for p in records if lib.read_json(p)["status"] == "waiting"]
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["request"], data)
                return {"message": {"content": "{invalid JSON}", "thinking": "diagnostic"}, "done_reason": "stop"}
            for _ in range(2):
                with self.assertRaisesRegex(e.JobError, "nach drei Versuchen"):
                    e.create_plan(e.DEFAULTS, "brief", e.schedule(30), lambda _: None, call, folder)
            records = [lib.read_json(p) for p in folder.glob("*.json")]
            self.assertEqual(len(records), 6)
            for r in records:
                self.assertEqual(r["status"], "rejected")
                self.assertEqual(r["response"]["message"]["content"], "{invalid JSON}")
                self.assertEqual(r["response"]["message"]["thinking"], "diagnostic")
                self.assertTrue(r["validation_error"])
                self.assertGreaterEqual(r["elapsed_seconds"], 0)

    def test_long_dialogues_preserved_with_advisory_and_no_retry(self):
        slots = e.schedule(30)
        calls = []
        def call(base, route, data, timeout):
            calls.append(copy.deepcopy(data))
            plan = sample_plan(len(slots))
            if len(calls) == 1:
                for i in (0, 2):
                    plan["segments"][i]["dialogue"] = "word " * 20
            return {"message": {"content": json.dumps(plan)}}
        with tempfile.TemporaryDirectory() as temp:
            plan = e.create_plan(e.DEFAULTS, "A speaker greets us.", slots, lambda _: None, call, temp)
            self.assertEqual(len(calls), 1)
            records = [lib.read_json(p) for p in sorted(Path(temp).glob("*.json"))]
            self.assertEqual([r["status"] for r in records], ["accepted"])
            self.assertIn("Take 1", records[0]["warnings"][0])
            self.assertIn("Take 3", records[0]["warnings"][1])
            self.assertIn("20 Wörter", records[0]["warnings"][0])
            self.assertEqual(json.loads(records[0]["response"]["message"]["content"]), plan)
            self.assertEqual(len(records[0]["request"]["messages"]), 2)

    def test_transport_failure_records_request_and_error(self):
        def call(*args, **kwargs):
            raise e.JobError("/api/chat: timed out")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(e.JobError, "timed out"):
                e.create_plan(e.DEFAULTS, "brief", e.schedule(30), lambda _: None, call, temp)
            record = lib.read_json(next(Path(temp).glob("*.json")))
            self.assertEqual(record["status"], "request_failed")
            self.assertIsNone(record["response"])
            self.assertIn("timed out", record["error"])
            self.assertEqual(record["request"]["model"], e.DEFAULTS["ollama_model"])

    def test_runner_retains_failure_and_appends_log_on_resume(self):
        from unittest.mock import patch
        fake = FakeServices()
        def bad_call(base, route, data=None, timeout=30):
            if route == "/api/chat":
                return {"message": {"content": "{}"}}
            return fake(base, route, data, timeout)
        with tempfile.TemporaryDirectory() as temp, patch.object(e, "verify_input_image"), patch('resources.time.sleep'):
            r = e.Runner(Path(temp)/"job", lambda _: None, call=bad_call, poll_seconds=0)
            r.new({"image": "test.png"}, "A scene without speech.")
            with self.assertRaises(e.JobError):
                r.run()
            before = (r.directory/"events.jsonl").read_text(encoding="utf-8")
            details = lib.job_details(r.directory)
            self.assertEqual(len(details["attempts"]), 3)
            self.assertIn("nach drei Versuchen", details["error"])
            self.assertIn("abgelehnt", details["log"])
            resumed = e.Runner(r.directory, lambda _: None, call=fake, poll_seconds=0)
            resumed.run()
            self.assertTrue((r.directory/"events.jsonl").read_text(encoding="utf-8").startswith(before))
            details = lib.job_details(r.directory)
            self.assertEqual(len(details["attempts"]), 4)
            self.assertIsNone(details["error"])
            self.assertTrue(details["has_plan"])
            self.assertEqual(len(fake.prompts), 7)


class PictureLibraryTests(unittest.TestCase):
    def test_preview_fetches_input_image_and_rejects_non_image(self):
        from unittest.mock import patch
        from test_automation import InputImageTests
        response = InputImageTests.response("image/webp", b"picture data")
        with patch.object(lib.urllib.request, "urlopen", return_value=response) as opening:
            data, content_type = lib.image_preview("https://comfy.example", "nested/picture.png")
            self.assertEqual(data, b"picture data")
            self.assertEqual(content_type, "image/webp")
            self.assertIn("subfolder=nested&type=input&preview=webp;70", opening.call_args.args[0])
        with patch.object(lib.urllib.request, "urlopen", return_value=InputImageTests.response("text/html", b"login")):
            with self.assertRaisesRegex(e.JobError, "keine Bildvorschau"):
                lib.image_preview("https://comfy.example", "picture.png")

    @staticmethod
    def server_call(base, route):
        assert route == "/object_info/LoadImage"
        return {"LoadImage": {"input": {"required": {"image": [["root.png", "root.png", "music.wav"]]}}}}

    def test_catalog_deduplicates_uploads_and_imports_old_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c = {**e.DEFAULTS, "image": "h3_automatik/main.png"}
            e.atomic_json(root/"runs"/"old"/"job.json", {"settings": {**c, "extra_images": ["profile.jpg"]}})
            lib.remember_images(root, c["comfy_url"], [c["image"]], "uploaded")
            catalog = lib.image_catalog(root, c, self.server_call)
            self.assertEqual({r["path"] for r in catalog["images"]}, {c["image"], "profile.jpg", "root.png"})
            self.assertEqual(len(catalog["images"]), 3)
            self.assertEqual(catalog["images"][0]["source"], "uploaded")
            self.assertIn("subfolder=h3_automatik", catalog["images"][0]["url"])
            (root/"runs"/"old"/"job.json").unlink()
            again = lib.image_catalog(root, {**c, "image": ""}, self.server_call)
            self.assertIn("profile.jpg", {r["path"] for r in again["images"]})

    def test_catalog_keeps_servers_separate_and_works_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            lib.remember_images(temp, "https://first.example", ["saved.png"], "uploaded")
            lib.remember_images(temp, "https://second.example", ["private.png"], "uploaded")
            def offline(*args):
                raise e.JobError("offline")
            result = lib.image_catalog(temp, {**e.DEFAULTS, "comfy_url": "https://first.example"}, offline)
            self.assertEqual([r["path"] for r in result["images"]], ["saved.png"])
            self.assertIn("offline", result["warning"])

    def test_catalog_rejects_paths_outside_input(self):
        with tempfile.TemporaryDirectory() as temp:
            entries = lib.remember_images(temp, "https://comfy.example", ["../bad.png", "/bad.png", "ok.png"])
            self.assertEqual(set(entries), {"ok.png"})


class LoraCatalogTests(unittest.TestCase):
    DEFAULT_STRENGTH = {"default": 1.0, "min": -100.0, "max": 100.0, "step": 0.01}

    @staticmethod
    def schema(names, metadata=None):
        return {"LoraLoaderModelOnly": {"input": {"required": {
            "model": ["MODEL"],
            "lora_name": [names],
            "strength_model": ["FLOAT", metadata if metadata is not None else {}],
        }}}}

    def test_catalog_preserves_exact_names_and_uses_targeted_live_schema(self):
        names = ["styles/Zebra.safetensors", r"characters\test [v1].safetensors",
                 "styles/alpha.safetensors", "Other.pt"]
        calls = []
        metadata = {"default": 0.75, "min": -2.5, "max": 3.25, "step": 0.005}
        def call(base, route, data=None, timeout=30):
            calls.append((base, route, data, timeout))
            return self.schema(names, metadata)
        settings = {**e.DEFAULTS, "comfy_url": "https://comfy.example"}
        result = lib.lora_catalog(settings, call)
        self.assertEqual(calls, [("https://comfy.example", "/object_info/LoraLoaderModelOnly", None, 8)])
        self.assertEqual(result, {"loras": [names[1], names[3], names[2], names[0]],
                                  "strength": metadata, "warning": ""})
        self.assertEqual(names[0], "styles/Zebra.safetensors")

    def test_refresh_never_retains_removed_or_saved_names(self):
        settings = {**e.DEFAULTS, "loras": [{"name": "saved.safetensors", "strength": 1.0}]}
        available = ["removed.safetensors", "current.safetensors"]
        def call(*args, **kwargs):
            return self.schema(list(available))
        self.assertEqual(lib.lora_catalog(settings, call)["loras"],
                         ["current.safetensors", "removed.safetensors"])
        available.remove("removed.safetensors")
        self.assertEqual(lib.lora_catalog(settings, call)["loras"], ["current.safetensors"])
        available.clear()
        self.assertEqual(lib.lora_catalog(settings, call),
                         {"loras": [], "strength": self.DEFAULT_STRENGTH, "warning": ""})

    def test_missing_loader_and_server_failure_return_empty_catalog_and_defaults(self):
        settings = {**e.DEFAULTS, "loras": [{"name": "saved.safetensors", "strength": 1.0}]}
        def offline(*args, **kwargs):
            raise e.JobError("Server nicht erreichbar")
        for call, message in ((lambda *args, **kwargs: {}, "LoraLoaderModelOnly"),
                              (offline, "Server nicht erreichbar")):
            with self.subTest(message=message):
                result = lib.lora_catalog(settings, call)
                self.assertEqual(result["loras"], [])
                self.assertEqual(result["strength"], self.DEFAULT_STRENGTH)
                self.assertIn(message, result["warning"])

    def test_optional_inputs_and_options_choices_follow_live_metadata(self):
        schema = {"LoraLoaderModelOnly": {"input": {"optional": {
            "lora_name": ["COMBO", {"options": [r"nested\test [v2].safetensors"]}],
            "strength_model": ["FLOAT", {"min": -0.125, "max": 0.875}],
        }}}}
        result = lib.lora_catalog(e.DEFAULTS, lambda *args, **kwargs: schema)
        self.assertEqual(result["loras"], [r"nested\test [v2].safetensors"])
        self.assertEqual(result["strength"],
                         {**self.DEFAULT_STRENGTH, "min": -0.125, "max": 0.875})
        self.assertEqual(result["warning"], "")


class DiagnosticFilesTests(unittest.TestCase):
    def test_legacy_job_explains_missing_responses_and_retains_error(self):
        with tempfile.TemporaryDirectory() as temp:
            e.atomic_json(Path(temp)/"job.json", {"id": "old", "status": "error", "error": "Dialog zu lang"})
            details = lib.job_details(temp)
            self.assertTrue(details["legacy"])
            self.assertEqual(details["attempts"], [])
            self.assertEqual(details["error"], "Dialog zu lang")

    def test_file_reader_only_serves_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            e.atomic_json(Path(temp)/"plan.json", {"test": True})
            self.assertIn('"test"', lib.job_file(temp, "plan.json")["content"])
            for file in ("../settings.json", "job.json", "/plan.json", "ollama/../../plan.json", "engine.py"):
                with self.assertRaises(e.JobError):
                    lib.job_file(temp, file)


if __name__ == "__main__":
    unittest.main()
