import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class HttpError(Exception):
    """メッセージに URL のクエリ・応答本文を含めない (API キーが載り得るため)。"""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # キーは小文字
    body: bytes


def _retry_after(headers) -> float | None:
    # HTTP-date 形式は扱わない (Discord は秒数で返す)
    try:
        v = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


class Http:
    def __init__(self, timeout: float = 20, ssl_context: ssl.SSLContext | None = None, opener=None):
        self.timeout = timeout
        self._ctx = ssl_context
        self._open = opener or urllib.request.urlopen

    def request_meta(self, method: str, url: str, *, params: Mapping[str, str] | None = None,
                     headers: Mapping[str, str] | None = None, bearer: str | None = None,
                     data: bytes | None = None, content_type: str | None = None, timeout: float | None = None,
                     redact_url: bool = False) -> HttpResponse:
        """redact_url は URL 自体がシークレット (webhook・ping URL) のとき、エラー文から URL を全て伏せる。"""
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        # Netdata Cloud 前段の Cloudflare は urllib 既定の UA を 403 (error 1010) で拒否する
        hdrs = {"User-Agent": "aramakisai-ops-dashboard", **(headers or {})}
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"
        if content_type:
            hdrs["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        where = method if redact_url else f"{method} {urllib.parse.urlsplit(url)._replace(query='', fragment='').geturl()}"
        try:
            kw = {"context": self._ctx} if self._ctx else {}
            with self._open(req, timeout=timeout or self.timeout, **kw) as resp:
                return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as e:
            raise HttpError(f"{where} -> {e.code}", status=e.code,
                            retry_after=_retry_after(e.headers or {}) if e.code == 429 else None) from None
        except (urllib.error.URLError, OSError) as e:
            raise HttpError(f"{where} failed: {type(e).__name__}") from None

    def request(self, method: str, url: str, **kw) -> bytes:
        return self.request_meta(method, url, **kw).body

    def get_bytes(self, url: str, **kw) -> bytes:
        return self.request("GET", url, **kw)

    def _json(self, method: str, url: str, **kw) -> Any:
        raw = self.request(method, url, **kw)
        try:
            return json.loads(raw)
        except ValueError:
            where = method if kw.get("redact_url") else f"{method} {urllib.parse.urlsplit(url).path}"
            raise HttpError(f"{where} returned invalid JSON") from None

    def get_json(self, url: str, **kw) -> Any:
        return self._json("GET", url, **kw)

    def post_json(self, url: str, body: Any, **kw) -> Any:
        return self._json("POST", url, data=json.dumps(body).encode(), content_type="application/json", **kw)

    def post_text(self, url: str, text: str, **kw) -> Any:
        return self._json("POST", url, data=text.encode(), content_type="text/plain", **kw)

    def post_form(self, url: str, data: Mapping[str, str], **kw) -> Any:
        return self._json("POST", url, data=urllib.parse.urlencode(data).encode(),
                          content_type="application/x-www-form-urlencoded", **kw)
