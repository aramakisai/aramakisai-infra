import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


class HttpError(Exception):
    """メッセージに URL のクエリ・応答本文を含めない (API キーが載り得るため)。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(self, timeout: float = 20, ssl_context: ssl.SSLContext | None = None, opener=None):
        self.timeout = timeout
        self._ctx = ssl_context
        self._open = opener or urllib.request.urlopen

    def request(self, method: str, url: str, *, params: Mapping[str, str] | None = None,
                headers: Mapping[str, str] | None = None, bearer: str | None = None,
                data: bytes | None = None, content_type: str | None = None, timeout: float | None = None) -> bytes:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        hdrs = dict(headers or {})
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"
        if content_type:
            hdrs["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        where = f"{method} {urllib.parse.urlsplit(url)._replace(query='', fragment='').geturl()}"
        try:
            kw = {"context": self._ctx} if self._ctx else {}
            with self._open(req, timeout=timeout or self.timeout, **kw) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise HttpError(f"{where} -> {e.code}", status=e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise HttpError(f"{where} failed: {type(e).__name__}") from None

    def get_bytes(self, url: str, **kw) -> bytes:
        return self.request("GET", url, **kw)

    def _json(self, method: str, url: str, **kw) -> Any:
        raw = self.request(method, url, **kw)
        try:
            return json.loads(raw)
        except ValueError:
            raise HttpError(f"{method} {urllib.parse.urlsplit(url).path} returned invalid JSON") from None

    def get_json(self, url: str, **kw) -> Any:
        return self._json("GET", url, **kw)

    def post_json(self, url: str, body: Any, **kw) -> Any:
        return self._json("POST", url, data=json.dumps(body).encode(), content_type="application/json", **kw)

    def post_form(self, url: str, data: Mapping[str, str], **kw) -> Any:
        return self._json("POST", url, data=urllib.parse.urlencode(data).encode(),
                          content_type="application/x-www-form-urlencoded", **kw)
