import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from data_stream_monitor import evaluate_streams


class DataStreamMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "state.json"
        self.messages = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def sender(self, message):
        self.messages.append(message)
        return True

    def test_alert_once_then_recover_once(self):
        now = pd.Timestamp("2026-08-17 12:00:00")
        stale = {
            "TH|品質趨勢|排氣靜壓": now - pd.Timedelta(hours=7),
            "TH|設備運轉|酸排氣": now - pd.Timedelta(hours=8),
        }
        evaluate_streams(stale, now=now, state_path=self.state_path, sender=self.sender)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("廠區:TH", self.messages[0])
        self.assertIn("品質趨勢(排氣靜壓)", self.messages[0])
        self.assertIn("設備運轉(酸排氣)", self.messages[0])

        evaluate_streams(stale, now=now, state_path=self.state_path, sender=self.sender)
        self.assertEqual(len(self.messages), 1)

        fresh = {key: now for key in stale}
        evaluate_streams(fresh, now=now, state_path=self.state_path, sender=self.sender)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("已回補或恢復抓到新資料", self.messages[1])

        evaluate_streams(fresh, now=now, state_path=self.state_path, sender=self.sender)
        self.assertEqual(len(self.messages), 2)

    def test_exactly_six_hours_is_not_stale(self):
        now = pd.Timestamp("2026-08-17 12:00:00")
        observations = {"S2|品質趨勢|供應水質": now - pd.Timedelta(hours=6)}
        evaluate_streams(observations, now=now, state_path=self.state_path, sender=self.sender)
        self.assertEqual(self.messages, [])

    def test_failed_delivery_is_retried(self):
        now = pd.Timestamp("2026-08-17 12:00:00")
        observations = {"TH|設備運轉|空壓": now - pd.Timedelta(hours=7)}
        calls = []

        def fail(message):
            calls.append(message)
            return False

        evaluate_streams(observations, now=now, state_path=self.state_path, sender=fail)
        evaluate_streams(observations, now=now, state_path=self.state_path, sender=fail)
        self.assertEqual(len(calls), 2)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(state["streams"][next(iter(observations))]["alert_active"])

    def test_cached_last_seen_detects_database_outage(self):
        now = pd.Timestamp("2026-08-17 12:00:00")
        key = "HJ1|設備運轉|冰機"
        evaluate_streams({key: now}, now=now, state_path=self.state_path, sender=self.sender)
        evaluate_streams({}, now=now + pd.Timedelta(hours=7), state_path=self.state_path, sender=self.sender)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("資料中斷", self.messages[0])


if __name__ == "__main__":
    unittest.main()
