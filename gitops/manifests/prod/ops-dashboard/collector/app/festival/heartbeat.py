"""評価が例外なく終わったときだけ Healthchecks.io に ping する。"""
from datetime import datetime, timedelta

from http_client import HttpError


class FestivalHeartbeat:
    MIN_INTERVAL = timedelta(seconds=60)

    def __init__(self, http, ping_url: str | None, store=None):
        self.http = http
        self._url = ping_url or None
        self.store = store
        self._last: datetime | None = None

    @property
    def configured(self) -> bool:
        return self._url is not None

    def beat(self, now: datetime) -> str | None:
        """必要なら ping を送り、失敗の理由 (状態コード・network・timeout) を返す。送らなかった・成功したら None。"""
        if self._url is None or (self._last is not None and now - self._last < self.MIN_INTERVAL):
            return None
        # 失敗でも時刻を進める (同じ評価・同じ 60 秒の中で再送しない)
        self._last = now
        try:
            self.http.request_meta("GET", self._url, timeout=10, redact_url=True)
        except HttpError as e:
            reason = f"http_{e.status}" if e.status is not None else ("timeout" if "Timeout" in str(e) else "network")
            if self.store is not None:
                self.store.save_festival_heartbeat(now, False, reason)
            return reason
        if self.store is not None:
            self.store.save_festival_heartbeat(now, True, None)
        return None
