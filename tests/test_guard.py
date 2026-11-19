"""Guards that fail silently if removed: query length, result cap, daily quota.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing_mcp as m  # noqa: E402


class GuardTest(unittest.TestCase):
    def setUp(self):
        self._usage = m.USAGE
        self._quota_default = m.QUOTA
        m._quota.clear()

    def tearDown(self):
        m.USAGE = self._usage
        m.QUOTA = self._quota_default
        m._quota.clear()

    # 1) query length
    def test_short_query_rejected(self):
        for bad in ("", "   ", "a", "Z"):
            with self.assertRaises(ValueError):
                m.call("search_tools", {"query": bad})

    def test_two_char_query_works(self):
        out = m.call("search_tools", {"query": "demo", "limit": 50})
        self.assertTrue(out["results"])

    # 2) result cap
    def test_limit_is_capped(self):
        out = m.call("search_tools", {"query": "oo", "limit": 500})
        self.assertLessEqual(len(out["results"]), m.MAX_SEARCH)

    def test_schema_declares_caps(self):
        schema = next(s for s in m.SCHEMAS if s["name"] == "search_tools")
        props = schema["inputSchema"]["properties"]
        self.assertEqual(props["query"]["minLength"], 2)
        self.assertEqual(props["limit"]["maximum"], m.MAX_SEARCH)

    # 3) daily quota
    def test_quota_blocks_after_limit(self):
        m.USAGE = Path(tempfile.mkdtemp()) / "usage.jsonl"
        m.QUOTA = 2
        m._quota.clear()
        ip = "203.0.113.7"
        self.assertTrue(m.check_quota(ip)[0])
        self.assertTrue(m.check_quota(ip)[0])
        ok, left = m.check_quota(ip)
        self.assertFalse(ok)
        self.assertEqual(left, 0)

    def test_quota_counts_today_log(self):
        log = Path(tempfile.mkdtemp()) / "usage.jsonl"
        today = dt.date.today().isoformat()
        log.write_text("\n".join(json.dumps({"ts": f"{today}T09:0{i}:00", "tool": "get_pricing",
                                              "ip": "203.0.113.8"}) for i in range(2)) + "\n")
        m.USAGE = log
        m.QUOTA = 2
        m._quota.clear()
        self.assertFalse(m.check_quota("203.0.113.8")[0], "restart must not reset the daily counter")
        self.assertTrue(m.check_quota("203.0.113.9")[0])

    def test_yesterday_does_not_count(self):
        log = Path(tempfile.mkdtemp()) / "usage.jsonl"
        log.write_text(json.dumps({"ts": "2020-01-01T09:00:00", "tool": "get_pricing",
                                   "ip": "203.0.113.10"}) + "\n")
        m.USAGE = log
        m.QUOTA = 1
        m._quota.clear()
        self.assertTrue(m.check_quota("203.0.113.10")[0])


class DataRulesTest(unittest.TestCase):
    def test_annual_is_never_divided(self):
        out = m.call("stack_cost", {"domains": ["samplecrm.com"]})
        self.assertEqual(out["annual_usd_published"], 0, "annual-only plans must not enter the monthly sum")

    def test_unknown_domain_is_flagged(self):
        out = m.call("get_pricing", {"domain": "does-not-exist.example"})
        self.assertFalse(out["found"])
        self.assertEqual(out["plans"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
