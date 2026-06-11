"""Persistent per-request send-result store.

为“结果对账”机制服务：上游(backend/auto_send worker)在请求 /tweet/post 时携带
request_id；本服务把发送结果落盘。即使 HTTP 响应在网络层丢失（如跨网段空闲
连接被中间设备掐断），上游也能用 GET /tweet/result/{request_id} 把结果找回来，
避免“实际已发出但上游不知道”导致的漏记/重发。
"""

import json
import os
import re
import threading
import time
from pathlib import Path

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_SENT_UNCONFIRMED = "sent_unconfirmed"
STATUS_FAILED = "failed"

TERMINAL_SENT_STATUSES = {STATUS_SUCCESS, STATUS_SENT_UNCONFIRMED}

DEFAULT_TTL_SECONDS = 7 * 24 * 3600
_CLEANUP_MIN_INTERVAL_SECONDS = 600


def is_valid_request_id(request_id: str) -> bool:
    return bool(REQUEST_ID_RE.match(request_id))


class ResultStore:
    """File-per-request JSON store with atomic writes and TTL cleanup."""

    def __init__(self, base_dir: Path, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.base_dir = Path(base_dir)
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._last_cleanup = 0.0
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, request_id: str) -> Path:
        if not is_valid_request_id(request_id):
            raise ValueError(f"invalid request_id: {request_id!r}")
        return self.base_dir / f"{request_id}.json"

    def record(
        self,
        request_id: str,
        status: str,
        *,
        tweet_id: str | None = None,
        warning: str | None = None,
        error: str | None = None,
    ) -> dict:
        entry: dict[str, object] = {
            "request_id": request_id,
            "status": status,
            "updated_at": time.time(),
        }
        if tweet_id:
            entry["tweet_id"] = tweet_id
        if warning:
            entry["warning"] = warning[:2000]
        if error:
            entry["error"] = error[:2000]
        path = self._path(request_id)
        tmp_path = path.with_suffix(".json.tmp")
        with self._lock:
            tmp_path.write_text(
                json.dumps(entry, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp_path, path)
        self._maybe_cleanup()
        return entry

    def get(self, request_id: str) -> dict | None:
        path = self._path(request_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return entry if isinstance(entry, dict) else None

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < _CLEANUP_MIN_INTERVAL_SECONDS:
            return
        self._last_cleanup = now
        cutoff = now - self.ttl_seconds
        try:
            for path in self.base_dir.glob("*.json"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass


_stores: dict[str, ResultStore] = {}
_stores_lock = threading.Lock()


def get_result_store(base_dir: str | Path = "data/tweet_results") -> ResultStore:
    key = str(base_dir)
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = ResultStore(Path(base_dir))
            _stores[key] = store
        return store
