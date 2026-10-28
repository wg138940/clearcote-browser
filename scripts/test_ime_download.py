"""Bounded transport retries; integrity/format failures are never retried."""
import io
import unittest
from unittest.mock import patch
import urllib.error

import check_ime_patch as checker


class DownloadRetry(unittest.TestCase):
    def test_transient_failures_back_off_then_succeed(self):
        unavailable = urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, None)
        with patch.object(checker.urllib.request, "urlopen", side_effect=[TimeoutError(), unavailable, io.BytesIO(b"YWJj")]) as opened:
            with patch.object(checker.time, "sleep") as slept:
                self.assertEqual(checker.fetch("https://example.invalid"), b"YWJj")
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([call.args[0] for call in slept.call_args_list], [1, 2])

    def test_retry_limit(self):
        with patch.object(checker.urllib.request, "urlopen", side_effect=ConnectionResetError()) as opened:
            with patch.object(checker.time, "sleep") as slept:
                with self.assertRaises(ConnectionResetError):
                    checker.fetch("https://example.invalid")
        self.assertEqual(opened.call_count, 4)
        self.assertEqual([call.args[0] for call in slept.call_args_list], [1, 2, 4])

    def test_permanent_http_and_invalid_payload_do_not_retry(self):
        missing = urllib.error.HTTPError("https://example.invalid", 404, "missing", {}, None)
        with patch.object(checker.urllib.request, "urlopen", side_effect=missing) as opened:
            with self.assertRaises(urllib.error.HTTPError):
                checker.fetch("https://example.invalid")
        self.assertEqual(opened.call_count, 1)
        with patch.object(checker.urllib.request, "urlopen", return_value=io.BytesIO(b"!invalid-base64!")) as opened:
            with self.assertRaises(ValueError):
                checker.base64.b64decode(checker.fetch("https://example.invalid"), validate=True)
        self.assertEqual(opened.call_count, 1)

    def test_source_hash_mismatch_does_not_redownload(self):
        payload = checker.base64.b64encode(b"valid download, incorrect source bytes")
        with patch.object(checker.urllib.request, "urlopen", return_value=io.BytesIO(payload)) as opened:
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                checker.run()
        self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
