import tempfile
import time
import unittest
from pathlib import Path

from src.result_store import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    ResultStore,
    is_valid_request_id,
)


class ResultStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ResultStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_unknown_returns_none(self) -> None:
        self.assertIsNone(self.store.get("nope"))

    def test_record_and_get_roundtrip(self) -> None:
        self.store.record("req-1", STATUS_SUCCESS, tweet_id="123")
        entry = self.store.get("req-1")
        assert entry is not None
        self.assertEqual(entry["status"], STATUS_SUCCESS)
        self.assertEqual(entry["tweet_id"], "123")
        self.assertIn("updated_at", entry)

    def test_record_overwrites_previous_status(self) -> None:
        self.store.record("req-2", STATUS_RUNNING)
        self.store.record("req-2", STATUS_FAILED, error="boom")
        entry = self.store.get("req-2")
        assert entry is not None
        self.assertEqual(entry["status"], STATUS_FAILED)
        self.assertEqual(entry["error"], "boom")

    def test_invalid_request_id_rejected(self) -> None:
        self.assertFalse(is_valid_request_id("../etc/passwd"))
        self.assertFalse(is_valid_request_id(""))
        self.assertFalse(is_valid_request_id("a" * 129))
        self.assertTrue(is_valid_request_id("upload_5032.attempt-1:x"))
        with self.assertRaises(ValueError):
            self.store.record("../x", STATUS_RUNNING)

    def test_cleanup_removes_expired_entries(self) -> None:
        import os

        self.store.record("old-1", STATUS_SUCCESS, tweet_id="1")
        path = Path(self._tmp.name) / "old-1.json"
        self.assertTrue(path.exists())
        # 把 old-1 的 mtime 回拨到 TTL 之外，再触发一次清扫
        expired_ts = time.time() - self.store.ttl_seconds - 60
        os.utime(path, (expired_ts, expired_ts))
        self.store._last_cleanup = 0.0
        self.store.record("new-1", STATUS_RUNNING)
        self.assertFalse(path.exists())
        self.assertTrue((Path(self._tmp.name) / "new-1.json").exists())

    def test_truncates_long_error(self) -> None:
        self.store.record("req-3", STATUS_FAILED, error="x" * 5000)
        entry = self.store.get("req-3")
        assert entry is not None
        self.assertEqual(len(entry["error"]), 2000)


if __name__ == "__main__":
    unittest.main()
