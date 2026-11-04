import io
import json
import unittest
import urllib.error

import http_client
from http_client import Http, HttpError


class FakeResp(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class HttpTest(unittest.TestCase):
    def make(self, resp=None, exc=None):
        calls = []

        def opener(req, timeout=None, context=None):
            calls.append((req, timeout))
            if exc:
                raise exc
            return resp

        return Http(opener=opener), calls

    def test_get_json_bearer_params(self):
        h, calls = self.make(FakeResp(b'{"a": 1}'))
        self.assertEqual(h.get_json("https://x/y", params={"q": "1"}, bearer="T"), {"a": 1})
        req = calls[0][0]
        self.assertEqual(req.full_url, "https://x/y?q=1")
        self.assertEqual(req.get_header("Authorization"), "Bearer T")
        self.assertEqual(req.get_method(), "GET")

    def test_user_agent(self):
        # Netdata Cloud 前段の Cloudflare は urllib 既定の UA を 403 (error 1010) で拒否する
        h, calls = self.make(FakeResp(b"{}"))
        h.get_json("https://x/y")
        self.assertNotIn("Python-urllib", calls[0][0].get_header("User-agent") or "Python-urllib")

    def test_post_json(self):
        h, calls = self.make(FakeResp(b"{}"))
        h.post_json("https://x/y", {"a": 1})
        req = calls[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"a": 1})
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_post_form(self):
        h, calls = self.make(FakeResp(b"{}"))
        h.post_form("https://x/y", {"a": "b c"})
        self.assertEqual(calls[0][0].data, b"a=b+c")

    def test_http_error_hides_query(self):
        err = urllib.error.HTTPError("https://x/y?key=SECRET", 403, "Forbidden", {}, io.BytesIO(b""))
        h, _ = self.make(exc=err)
        with self.assertRaises(HttpError) as cm:
            h.get_json("https://x/y", params={"key": "SECRET"})
        self.assertEqual(cm.exception.status, 403)
        self.assertNotIn("SECRET", str(cm.exception))

    def test_network_error(self):
        h, _ = self.make(exc=urllib.error.URLError("boom"))
        with self.assertRaises(HttpError) as cm:
            h.get_bytes("https://x/y")
        self.assertIsNone(cm.exception.status)

    def test_timeout(self):
        h, _ = self.make(exc=TimeoutError())
        with self.assertRaises(HttpError):
            h.get_bytes("https://x/y")

    def test_request_meta_returns_status_headers_body(self):
        resp = FakeResp(b"ok")
        resp.status = 204
        resp.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2"}
        h, calls = self.make(resp)
        r = h.request_meta("POST", "https://x/y", data=b"{}")
        self.assertEqual((r.status, r.body), (204, b"ok"))
        self.assertEqual(r.headers["x-ratelimit-remaining"], "0")
        self.assertEqual(r.headers["x-ratelimit-reset-after"], "2")
        self.assertEqual(calls[0][0].get_method(), "POST")

    def test_429_carries_retry_after(self):
        err = urllib.error.HTTPError("https://x/y", 429, "Too Many", {"Retry-After": "7"}, io.BytesIO(b""))
        h, _ = self.make(exc=err)
        with self.assertRaises(HttpError) as cm:
            h.request_meta("POST", "https://x/y")
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 7.0))

    def test_retry_after_absent_or_invalid_is_none(self):
        for hdrs in ({}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}):
            err = urllib.error.HTTPError("https://x/y", 429, "Too Many", hdrs, io.BytesIO(b""))
            h, _ = self.make(exc=err)
            with self.assertRaises(HttpError) as cm:
                h.get_bytes("https://x/y")
            self.assertIsNone(cm.exception.retry_after)

    def test_non_429_has_no_retry_after(self):
        err = urllib.error.HTTPError("https://x/y", 503, "Unavailable", {"Retry-After": "7"}, io.BytesIO(b""))
        h, _ = self.make(exc=err)
        with self.assertRaises(HttpError) as cm:
            h.get_bytes("https://x/y")
        self.assertIsNone(cm.exception.retry_after)

    def test_redact_url_keeps_url_out_of_every_error(self):
        url = "https://discord.com/api/webhooks/123/TOKEN"
        cases = [
            urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"")),
            urllib.error.URLError("boom"),
            TimeoutError(),
        ]
        for exc in cases:
            h, _ = self.make(exc=exc)
            with self.assertRaises(HttpError) as cm:
                h.request_meta("POST", url, redact_url=True)
            for part in ("TOKEN", "webhooks", "discord"):
                self.assertNotIn(part, str(cm.exception))
        h, _ = self.make(FakeResp(b"not json"))
        with self.assertRaises(HttpError) as cm:
            h.post_json(url, {}, redact_url=True)
        self.assertNotIn("TOKEN", str(cm.exception))

    def test_default_error_still_names_url(self):
        err = urllib.error.HTTPError("https://x/y", 500, "Err", {}, io.BytesIO(b""))
        h, _ = self.make(exc=err)
        with self.assertRaises(HttpError) as cm:
            h.get_bytes("https://x/y")
        self.assertIn("https://x/y", str(cm.exception))

    def test_invalid_json(self):
        h, _ = self.make(FakeResp(b"not json"))
        with self.assertRaises(HttpError):
            h.get_json("https://x/y")


if __name__ == "__main__":
    unittest.main()
