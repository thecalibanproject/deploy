#!/usr/bin/env python3
"""Unit tests of telemetry.py (stdlib unittest): python3 -m unittest discover -s e2e/demo -p 'test_*.py'"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry  # noqa: E402


class ServerTiming(unittest.TestCase):
    def test_parse(self):
        st = telemetry.parse_server_timing(
            'auth;dur=0.4, route;dur=12.5;desc="knn", embed;dur=9.1, pii;dur=1.2, cache;dur=0.3;desc="miss", '
            'upstream;dur=210.0, upstream_total;dur=880.25, node;dur=950;desc="run_01", total;dur=990.5')
        self.assertEqual(st["route"], {"dur": 12.5, "desc": "knn"})
        self.assertEqual(st["cache"]["desc"], "miss")
        self.assertEqual(st["node"], {"dur": 950.0, "desc": "run_01"})
        self.assertEqual(st["total"]["dur"], 990.5)

    def test_absent_or_odd(self):
        self.assertEqual(telemetry.parse_server_timing(None), {})
        self.assertEqual(telemetry.parse_server_timing(""), {})
        self.assertEqual(telemetry.parse_server_timing("node;desc=\"r1\""), {"node": {"desc": "r1"}})
        self.assertEqual(telemetry.parse_server_timing("x;dur=abc"), {"x": {}})


class Rows(unittest.TestCase):
    def test_classification_is_route_alone(self):
        """`embed` is measured inside `route`: classification is `route`, embedding a part of it."""
        st = telemetry.parse_server_timing('route;dur=12.5;desc="knn", embed;dur=9.1, total;dur=40')
        rows = telemetry.rows([{"kind": "request", "label": "R1", "server": st, "client": {"total_ms": 45.0}}])
        self.assertEqual(rows[0]["classify_ms"], 12.5)
        self.assertEqual(rows[0]["embed_ms"], 9.1)
        self.assertEqual(rows[0]["route_stage"], "knn")
        self.assertEqual(telemetry.classify_text(rows[0]), "12.5 (embed 9.1)")

    def test_client_side_only(self):
        rows = telemetry.rows([{"kind": "request", "label": "R2", "client": {"total_ms": 120.0}}])
        self.assertIsNone(rows[0]["classify_ms"])
        self.assertEqual(rows[0]["end_to_end_ms"], 120.0)
        self.assertFalse(rows[0]["server_timing"])

    def test_run_steps_merge(self):
        recs = [{"kind": "request", "label": "R1", "run_id": "run_1", "client": {"total_ms": 10.0}},
                {"kind": "run", "run_id": "run_1", "steps": [{"vertex": "assess", "duration_ms": 44.0}],
                 "duration_ms": 90.0}]
        rows = telemetry.rows(recs)
        self.assertEqual(rows[0]["steps"][0]["vertex"], "assess")
        self.assertEqual(rows[0]["node_ms"], 90.0)

    def test_append_and_report(self):
        """Scenarios may append their own request records (an SDK's classification rows)."""
        with tempfile.TemporaryDirectory() as d:
            old = telemetry.OUT
            telemetry.OUT = d
            try:
                telemetry.append("dev", {"kind": "request", "scenario": "dev", "label": "sdk",
                                         "server": {"route": {"dur": 3.0, "desc": "rules"}},
                                         "client": {"total_ms": 5.0}})
                telemetry.append("dev", {"kind": "eval", "label": "e", "trials": 3, "p50": 10.0, "p95": 20.0})
                telemetry.report(["dev"])
                with open(os.path.join(d, "telemetry.md"), encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("| sdk | - | rules | 3.0 |", text)
                self.assertIn("| e |", text)
                self.assertTrue(os.path.exists(os.path.join(d, "telemetry.csv")))
            finally:
                telemetry.OUT = old

    def test_rotate_starts_fresh(self):
        """A new recording's report holds only its own rows and evals, not an earlier run's."""
        with tempfile.TemporaryDirectory() as d:
            old = telemetry.OUT
            telemetry.OUT = d
            try:
                for _ in range(2):  # two recordings of the same scenario
                    telemetry.rotate("health")
                    telemetry.append("health", {"kind": "request", "label": "R1", "client": {"total_ms": 1.0}})
                    telemetry.append("health", {"kind": "eval", "label": "golden", "trials": 18})
                rows = telemetry.rows(telemetry.load("health"))
                self.assertEqual([r["label"] for r in rows], ["R1"])
                self.assertEqual(len(telemetry.evals(telemetry.load("health"))), 1)
                self.assertEqual(len(os.listdir(os.path.join(d, "history"))), 1)
                self.assertEqual(telemetry.scenarios_found(), ["health"])
                self.assertIsNone(telemetry.rotate("absent"))
            finally:
                telemetry.OUT = old

    def test_percentiles(self):
        self.assertEqual(telemetry.pct([1, 2, 3, 4, 100], 50), 3)
        self.assertEqual(telemetry.pct([1, 2, 3, 4, 100], 95), 100)
        self.assertIsNone(telemetry.pct([], 50))


if __name__ == "__main__":
    unittest.main()
