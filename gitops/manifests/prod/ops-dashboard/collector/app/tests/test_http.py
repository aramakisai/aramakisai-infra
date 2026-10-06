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

    def test_invalid_json(self):
        h, _ = self.make(FakeResp(b"not json"))
        with self.assertRaises(HttpError):
            h.get_json("https://x/y")


if __name__ == "__main__":
    unittest.main()
