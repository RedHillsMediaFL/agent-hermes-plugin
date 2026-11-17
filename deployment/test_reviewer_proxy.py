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
    def test_provider_target_tools_and_output_cannot_be_overridden(self):
        request = proxy.prepare({"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100000, "tools": [{"type": "function"}], "base_url": "https://other.invalid", "stream": True})
        self.assertEqual(request["max_tokens"], 256)
        self.assertNotIn("tools", request)
        self.assertNotIn("base_url", request)
        with self.assertRaises(proxy.AdmissionError):
            proxy.prepare({"model": "deepseek-reasoner", "messages": [{"role": "user", "content": "Hello"}]})

    def test_images_and_oversized_context_are_rejected_before_provider_access(self):
        for content in [[{"type": "image_url", "image_url": {"url": "https://private.invalid"}}], "a" * 16001]:
            with self.assertRaises(proxy.AdmissionError):
                proxy.prepare({"model": "deepseek-chat", "messages": [{"role": "user", "content": content}]})

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
        body = json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]}).encode()
        for upstream_status in [400, 401, 402, 403, 429, 500]:
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
                key = Mock()
                key.read_text.return_value = "synthetic-key"
                with patch.object(proxy, "admit"), patch.object(proxy, "KEY_FILE", key), patch.object(proxy.urllib.request, "build_opener", return_value=opener):
                    handler.do_POST()
                handler.send_response.assert_called_once_with(upstream_status if upstream_status < 500 else 503)
                response = handler.wfile.getvalue()
                self.assertNotIn(b"private-provider", response)
                self.assertNotIn(b"synthetic-key", response)


if __name__ == "__main__":
    unittest.main()
