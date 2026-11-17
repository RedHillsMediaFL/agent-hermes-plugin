import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

spec = importlib.util.spec_from_file_location("review_proxy", Path(__file__).with_name("reviewer-provider-proxy.py"))
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ReviewProviderTests(unittest.TestCase):
    def setUp(self):
        proxy.configure({"model": "review-local", "upstream_url": "http://192.168.1.10:8000/v1/chat/completions"})

    def test_provider_target_tools_and_output_cannot_be_overridden(self):
        request = proxy.prepare({"model": "review-local", "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100000, "tools": [{"type": "function"}], "base_url": "https://other.invalid", "stream": True,
            "reasoning_effort": "high"})
        self.assertEqual(request["max_tokens"], 256)
        self.assertEqual(request["reasoning_effort"], "none")
        self.assertNotIn("tools", request)
        self.assertNotIn("base_url", request)
        with self.assertRaises(proxy.AdmissionError):
            proxy.prepare({"model": "another-model", "messages": [{"role": "user", "content": "Hello"}]})

    def test_startup_configuration_is_one_fixed_private_endpoint(self):
        for target in ["https://api.example.com/v1/chat/completions", "http://1.1.1.1:8000/v1/chat/completions",
                       "http://169.254.169.254:8000/v1/chat/completions", "http://192.168.1.10:8000/v1/models",
                       "http://192.168.1.10:8000/v1/chat/completions?model=other", "http://user:password@192.168.1.10:8000/v1/chat/completions"]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                proxy.configure({"model": "review-local", "upstream_url": target})
        with self.assertRaises(ValueError):
            proxy.configure({"model": "review-local", "upstream_url": proxy.UPSTREAM, "max_tokens": 99999})

    def test_only_one_request_can_enter_the_shared_local_runtime(self):
        self.assertTrue(proxy.ACTIVE.acquire(blocking=False))
        try:
            self.assertFalse(proxy.ACTIVE.acquire(blocking=False))
        finally:
            proxy.ACTIVE.release()

    def test_images_and_oversized_context_are_rejected_before_provider_access(self):
        for content in [[{"type": "image_url", "image_url": {"url": "https://private.invalid"}}], "a" * 16001]:
            with self.assertRaises(proxy.AdmissionError):
                proxy.prepare({"model": "review-local", "messages": [{"role": "user", "content": content}]})

    def test_limits_survive_new_connections_and_allow_only_fifty_requests_daily(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for index in range(50):
                proxy.admit(directory, now=1800000000 + index * 61)
            with self.assertRaises(proxy.AdmissionError):
                proxy.admit(directory, now=1800000000 + 50 * 61)
            proxy.admit(directory, now=1800000000 + 86400)

    def test_rapid_requests_are_limited_even_below_daily_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for _ in range(5):
                proxy.admit(directory, now=1800000000)
            with self.assertRaises(proxy.AdmissionError):
                proxy.admit(directory, now=1800000001)

    def test_quota_state_has_private_file_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "private-state"
            proxy.admit(directory, now=1800000000)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((directory / "budget.sqlite3").stat().st_mode), 0o600)

    def test_upstream_errors_keep_safe_status_without_exposing_provider_body(self):
        body = json.dumps({"model": "review-local", "messages": [{"role": "user", "content": "Hello"}]}).encode()
        for upstream_status in [400, 401, 402, 403, 409, 429, 500]:
            with self.subTest(upstream_status=upstream_status):
                handler = object.__new__(proxy.Handler)
                handler.path = "/v1/chat/completions"
                handler.headers = {"Content-Length": str(len(body))}
                handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                error = urllib.error.HTTPError(proxy.UPSTREAM, upstream_status, "private-provider-detail", {}, io.BytesIO(b"private-provider-body"))
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(proxy, "admit"), patch.object(proxy.urllib.request, "build_opener", return_value=opener):
                    handler.do_POST()
                handler.send_response.assert_called_once_with(upstream_status if upstream_status < 500 else 503)
                response = handler.wfile.getvalue()
                self.assertNotIn(b"private-provider", response)
                self.assertNotIn("Authorization", opener.open.call_args.args[0].headers)


if __name__ == "__main__":
    unittest.main()
