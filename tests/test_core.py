import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hf.client import APIError, Client, api_url, endpoint, public_url, request_status_url
from hf.config import credentials
from hf.jobs import Jobs, run
from hf.models import (IMAGE_MODELS, VIDEO_MODELS, MINIMAX_H3_MODELS, WAN30_MODELS,
                       attach_references, image_input, video_input, minimax_h3_input, wan30_input)
from hf.secrets import protect


def response(data=None, status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class Clock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.clock = Clock()
        self.client = Client("key", "secret", self.session, self.clock.clock, self.clock.sleep)
        self.job = {"request_id": "abc", "status_url": "https://api.higgsfield.ai/requests/abc/status"}

    def wait(self, timeout=50):
        return self.client.wait(self.job, timeout, Mock(), Mock(), Mock())

    def test_authorization_and_no_redirects(self):
        self.session.request.return_value = response({"request_id": "abc"})
        self.client.submit("bytedance/seedance-2.0/text-to-video", {"prompt": "test"})
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("POST", "https://api.higgsfield.ai/bytedance/seedance-2.0/text-to-video"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Key key:secret")
        self.assertFalse(kwargs["allow_redirects"])

    def test_poll_backoff_terminal(self):
        self.session.request.side_effect = [response({"status": s}) for s in ["queued", "in_progress", "completed"]]
        self.assertEqual(self.wait()["status"], "completed")
        self.assertGreaterEqual(self.clock.now, 5)

    def test_all_terminal_states_stop(self):
        for status in ("completed", "failed", "nsfw", "canceled"):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                self.session.request.return_value = response({"status": status})
                self.assertEqual(self.wait()["status"], status)
                self.assertEqual(self.session.request.call_count, 1)

    def test_retry_status_500_429_and_network(self):
        import requests
        self.session.request.side_effect = [response(status=500), response(status=429), requests.Timeout(), response({"status": "completed"})]
        self.assertEqual(self.wait()["status"], "completed")
        self.assertEqual(self.session.request.call_count, 4)

    def test_auth_and_missing_request_not_retried(self):
        for status in (401, 404):
            self.session.request.reset_mock()
            self.session.request.return_value = response(status=status)
            with self.assertRaises(APIError):
                self.wait()
            self.assertEqual(self.session.request.call_count, 1)

    def test_timeout(self):
        self.session.request.return_value = response({"status": "queued"})
        with self.assertRaises(TimeoutError):
            self.wait(3)
        self.assertLessEqual(self.clock.now, 3)

    def test_wrong_request_id(self):
        self.session.request.return_value = response({"status": "completed", "request_id": "other"})
        with self.assertRaisesRegex(APIError, "mismatch"):
            self.wait()

    def test_interrupt_no_further_poll(self):
        interrupt = Mock(side_effect=RuntimeError("interrupted"))
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.client.wait(self.job, 10, Mock(), Mock(), interrupt)
        self.session.request.assert_not_called()

    def test_upload_does_not_send_credentials(self):
        self.session.request.return_value = response({"upload_url": "https://storage.example/upload", "public_url": "https://cdn.example/file", "upload_headers": {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"}})
        with patch("hf.client.public_url", side_effect=lambda value: value), patch("hf.client.requests.put", return_value=response()) as put:
            self.client.upload(b"png", "image/png")
        headers = put.call_args.kwargs["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertIn("x-amz-tagging", headers)

    def test_untrusted_urls(self):
        for url in ("https://evil.example/requests/a/status", "http://api.higgsfield.ai/requests/a/status", "https://api.higgsfield.ai.evil/requests/a/status", "https://api.higgsfield.ai/requests/../status", "https://api.higgsfield.ai/requests/a/status?secret=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                api_url(url)
        with self.assertRaises(ValueError):
            public_url("https://127.0.0.1/file")
        with self.assertRaises(ValueError):
            endpoint("https://api.higgsfield.ai/model")

    def test_status_url_normalization_never_uses_untrusted_host(self):
        canonical = "https://api.higgsfield.ai/requests/abc/status"
        for value in (None, "", "/requests/abc/status", "http://api.higgsfield.ai/requests/abc/status",
                      "https://platform.higgsfield.ai/requests/abc/status",
                      "https://unexpected.example/requests/abc/status", "https://api.higgsfield.ai:443/requests/abc/status"):
            with self.subTest(url=value):
                self.assertEqual(request_status_url("abc", value), canonical)

    def test_status_url_rejects_id_injection_and_mismatch(self):
        for request_id in ("../other", "abc?key=x", "abc\n", 123, ""):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                request_status_url(request_id)
        with self.assertRaisesRegex(APIError, "do not match"):
            request_status_url("abc", "https://api.higgsfield.ai/requests/other/status")

    def test_wait_uses_production_host_for_returned_alias(self):
        self.job["status_url"] = "https://unexpected.example/requests/abc/status"
        self.session.request.return_value = response({"status": "completed", "request_id": "abc"})
        self.wait()
        self.assertEqual(self.session.request.call_args.args[1], "https://api.higgsfield.ai/requests/abc/status")

    def test_error_does_not_echo_response_body(self):
        self.session.request.return_value = response({"detail": "secret"}, 401)
        with self.assertRaises(APIError) as caught:
            self.client.submit("provider/model", {})
        self.assertNotIn("secret", str(caught.exception).replace("key or secret", "credentials"))

    def test_download_without_auth_and_atomic_write(self):
        download = response()
        download.iter_content.return_value = iter([b"part1", b"part2"])
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "output.mp4"
            with patch("hf.client.public_url", side_effect=lambda url: url), patch("hf.client.requests.get", return_value=download) as get:
                self.client.download("https://cdn.example/video", target)
            self.assertEqual(target.read_bytes(), b"part1part2")
            self.assertNotIn("headers", get.call_args.kwargs)
            self.assertFalse(target.with_suffix(".mp4.part").exists())

    def test_failed_download_does_not_leave_partial_output(self):
        download = response()
        download.iter_content.return_value = iter([b"too large"])
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "output.mp4"
            with patch("hf.client.public_url", side_effect=lambda url: url), patch("hf.client.requests.get", return_value=download):
                with self.assertRaises(APIError):
                    self.client.download("https://cdn.example/video", target, limit=2)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".mp4.part").exists())


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = Jobs(self.temp.name, "owner-a")
        self.client = Mock()
        self.client.submit.return_value = ({"request_id": "abc", "status_url": "https://api.higgsfield.ai/requests/abc/status"}, "corr")
        self.result = {"status": "completed", "images": [{"url": "https://cdn.example/a.png"}]}

        def wait(job, timeout, update, progress, interrupt):
            update("completed", self.result, "corr")
            return self.result
        self.client.wait.side_effect = wait

    def tearDown(self):
        self.jobs.close()
        self.temp.cleanup()

    def run_job(self, identity=None, factory=None):
        return run(self.client, self.jobs, "provider/model", identity or {"take": 1}, factory or (lambda: {}), 60, Mock(), Mock())

    def test_duplicate_submits_once_even_after_reopen(self):
        self.run_job()
        self.jobs.close()
        self.jobs = Jobs(self.temp.name, "owner-a")
        self.run_job()
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(self.client.wait.call_count, 1)

    def test_generation_id_allows_new_generation(self):
        self.run_job({"take": 1})
        self.run_job({"take": 2})
        self.assertEqual(self.client.submit.call_count, 2)

    def test_timeout_retains_id_and_resumes(self):
        normal_wait = self.client.wait.side_effect
        self.client.wait.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.run_job()
        self.assertEqual(self.jobs.by_request("abc")["request_id"], "abc")
        self.client.wait.side_effect = normal_wait
        self.run_job()
        self.assertEqual(self.client.submit.call_count, 1)

    def test_ambiguous_submission_never_repeats(self):
        self.client.submit.side_effect = APIError("timeout")
        with self.assertRaises(APIError):
            self.run_job()
        with self.assertRaisesRegex(APIError, "not be submitted again"):
            self.run_job()
        self.assertEqual(self.client.submit.call_count, 1)

    def test_500_submission_never_repeats(self):
        self.client.submit.side_effect = APIError("server error", 500)
        for _ in range(2):
            with self.assertRaises(APIError):
                self.run_job()
        self.assertEqual(self.client.submit.call_count, 1)

    def test_validation_rejection_can_be_retried(self):
        accepted = self.client.submit.return_value
        self.client.submit.side_effect = [APIError("validation", 422), accepted]
        with self.assertRaises(APIError):
            self.run_job()
        self.run_job()
        self.assertEqual(self.client.submit.call_count, 2)

    def test_upload_failure_does_not_poison_generation(self):
        with self.assertRaises(ValueError):
            self.run_job(factory=Mock(side_effect=ValueError("upload failed")))
        self.client.submit.assert_not_called()
        self.run_job()

    def test_ownership(self):
        self.run_job()
        other = Jobs(self.temp.name, "owner-b")
        try:
            with self.assertRaises(ValueError):
                other.by_request("abc")
        finally:
            other.close()

    def test_terminal_failure_is_not_recharged(self):
        self.result = {"status": "failed"}
        def wait(job, timeout, update, progress, interrupt):
            update("failed", self.result, None)
            return self.result
        self.client.wait.side_effect = wait
        for _ in range(2):
            with self.assertRaisesRegex(APIError, "failed"):
                self.run_job()
        self.assertEqual(self.client.submit.call_count, 1)

    def test_atomic_claim_with_two_connections(self):
        second = Jobs(self.temp.name, "owner-a")
        try:
            self.assertTrue(self.jobs.claim("same-input", "provider/model"))
            self.assertFalse(second.claim("same-input", "provider/model"))
        finally:
            second.close()

    def test_submission_with_alternate_status_host(self):
        self.client.submit.return_value = ({"request_id": "abc", "status_url": "http://internal.example/requests/abc/status"}, "corr")
        self.run_job()
        self.assertEqual(self.jobs.by_request("abc")["status_url"], "https://api.higgsfield.ai/requests/abc/status")
        self.assertEqual(self.client.submit.call_count, 1)

    def test_recover_old_version_rejected_url_without_resubmission(self):
        from hf.jobs import fingerprint
        token = fingerprint({"model": "provider/model", "input": {"take": 1}})
        self.jobs.claim(token, "provider/model")
        with self.jobs.db:
            self.jobs.db.execute("UPDATE jobs SET status='submitting',request_id='abc',status_url=NULL")
        self.jobs.close()
        self.jobs = Jobs(self.temp.name, "owner-a")
        request_id, _ = self.run_job()
        self.assertEqual(request_id, "abc")
        self.client.submit.assert_not_called()
        self.assertEqual(self.client.wait.call_count, 1)
        self.assertEqual(self.jobs.by_request("abc")["status_url"], "https://api.higgsfield.ai/requests/abc/status")

    def test_ambiguous_submission_without_id_stays_blocked(self):
        self.jobs.claim("interrupted", "provider/model")
        self.jobs.update("interrupted", "submitting")
        recovered = self.jobs.recover_status_url(self.jobs.get("interrupted"))
        self.assertIsNone(recovered["status_url"])
        self.assertEqual(recovered["status"], "submitting")


class ModelTests(unittest.TestCase):
    def test_seedance20_reference_example(self):
        payload = video_input("Seedance 2.0 - Text to Video", "A cinematic coastal road", 5, "720p", "16:9", True, "mp4", [])
        self.assertEqual(payload, {"prompt": "A cinematic coastal road", "duration": 5, "resolution": "720p", "aspect_ratio": "16:9", "generate_audio": True})

    def test_25_rejects_unsupported_resolution_and_duration(self):
        for duration, resolution in ((31, "720p"), (5, "1080p")):
            with self.assertRaises(ValueError):
                video_input("Seedance 2.5 - Text to Video", "test", duration, resolution, "16:9", True, "mp4", [])

    def test_start_and_end_frames(self):
        refs = [{"kind": "image", "url": "start"}, {"kind": "image", "url": "end"}]
        model = "Seedance 2.5 - Image to Video"
        payload = video_input(model, "", 5, "720p", "16:9", True, "mp4", refs)
        payload = attach_references(VIDEO_MODELS[model], payload, refs, lambda r: r["url"])
        self.assertEqual(payload["image_url"], "start")
        self.assertEqual(payload["end_image_url"], "end")
        self.assertNotIn("aspect_ratio", payload)
        self.assertNotIn("prompt", payload)

    def test_multimodal_references(self):
        refs = [{"kind": kind, "url": kind} for kind in ("image", "video", "audio")]
        payload = attach_references("bytedance/seedance-2.5/reference-to-video", {}, refs, lambda r: r["url"])
        self.assertEqual(payload, {"image_urls": ["image"], "video_urls": ["video"], "audio_urls": ["audio"]})

    def test_minimax_h3_text_and_image(self):
        model = "MiniMax H3 - Text to Video"
        payload = minimax_h3_input(model, "ocean sunset", 5, "16:9", False, [])
        self.assertEqual(payload["resolution"], "2K")
        self.assertEqual(MINIMAX_H3_MODELS[model], "minimax/h3/text-to-video")
        with self.assertRaises(ValueError):
            minimax_h3_input(model, "ocean sunset", 4, "16:9", False, [])
        i2v = "MiniMax H3 - Image to Video"
        refs = [{"kind": "image", "url": "start"}, {"kind": "image", "url": "end"}]
        body = minimax_h3_input(i2v, "animate", 8, "auto", True, refs)
        body = attach_references(MINIMAX_H3_MODELS[i2v], body, refs, lambda r: r["url"])
        self.assertEqual(body["image_url"], "start")
        self.assertEqual(body["end_image_url"], "end")
        self.assertTrue(body["aigc_watermark"])

    def test_minimax_h3_reference_rules(self):
        model = "MiniMax H3 - Reference to Video"
        with self.assertRaises(ValueError):
            minimax_h3_input(model, "prompt", 5, "auto", False, [{"kind": "audio", "url": "a"}])
        refs = [{"kind": "image", "url": "i"}]
        body = attach_references(MINIMAX_H3_MODELS[model], minimax_h3_input(model, "prompt", 5, "auto", False, refs), refs, lambda r: r["url"])
        self.assertEqual(body["image_urls"], ["i"])

    def test_wan30_text_seed_and_i2v(self):
        model = "Wan 3.0 - Text to Video"
        payload = wan30_input(model, "waves", 5, "1080p", "adaptive", True, False, 0, [])
        self.assertNotIn("seed", payload)
        self.assertEqual(WAN30_MODELS[model], "alibaba/wan-3.0/text-to-video")
        with_seed = wan30_input(model, "waves", 5, "720p", "16:9", False, True, 42, [])
        self.assertEqual(with_seed["seed"], 42)
        self.assertTrue(with_seed["enable_thinking"])
        with self.assertRaises(ValueError):
            wan30_input(model, "waves", 1, "1080p", "adaptive", True, False, 0, [])
        i2v = "Wan 3.0 - Image to Video"
        refs = [{"kind": "image", "url": "frame"}]
        body = attach_references(WAN30_MODELS[i2v], wan30_input(i2v, "move", 5, "720p", "adaptive", True, False, 0, refs), refs, lambda r: r["url"])
        self.assertEqual(body["image_url"], "frame")

    def test_gpt_image_refs_optional_max_16(self):
        model = next(iter(IMAGE_MODELS))
        self.assertEqual(image_input(model, "test", "2k", "auto", "high", [])["quality"], "high")
        with self.assertRaises(ValueError):
            image_input(model, "test", "2k", "auto", "high", [{"kind": "image"}] * 17)

    def test_missing_credentials(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Configure"):
                credentials(folder)

    @unittest.skipUnless(sys.platform == "win32", "Windows credential protection")
    def test_encrypted_config_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {}, clear=True):
            values = {"HF_API_KEY_ID": "synthetic-id", "HF_API_KEY_SECRET": "synthetic-secret"}
            cipher = protect(json.dumps(values).encode())
            self.assertNotIn(b"synthetic-secret", cipher)
            (Path(folder) / "credentials.dpapi").write_bytes(cipher)
            self.assertEqual(credentials(folder)[:2], ("synthetic-id", "synthetic-secret"))
            with patch.dict("os.environ", {"HF_API_KEY_ID": "environment-id", "HF_API_KEY_SECRET": "environment-secret"}):
                self.assertEqual(credentials(folder)[:2], ("environment-id", "environment-secret"))


if __name__ == "__main__":
    unittest.main()
