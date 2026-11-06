#!/usr/bin/env python3
"""Unit tests for fare-cache accounting and the coverage gate.

These tests never call main() and never touch the network or Telegram.
"""

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date
from unittest import mock

import check_beach as cb


class _Resp:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code):
    return urllib.error.HTTPError(
        "https://example.test/prices", code, "err", hdrs=None, fp=None
    )


class StatsMixin(unittest.TestCase):
    def setUp(self):
        self._stats = dict(cb.STATS)
        self._errors = list(cb._first_errors)
        for key in cb.STATS:
            cb.STATS[key] = 0
        cb._first_errors.clear()

    def tearDown(self):
        cb.STATS.update(self._stats)
        cb._first_errors[:] = self._errors


class PriceBodyTests(unittest.TestCase):
    def test_empty_list_is_empty(self):
        self.assertEqual(cb._price_body_kind({"success": True, "data": []}), "empty")

    def test_priced_row_is_ok(self):
        body = {"success": True, "data": [{"price": 41, "link": "/x"}]}
        self.assertEqual(cb._price_body_kind(body), "ok")

    def test_row_without_price_is_empty(self):
        self.assertEqual(cb._price_body_kind({"data": [{"airline": "FR"}]}), "empty")

    def test_success_false_is_error(self):
        self.assertEqual(
            cb._price_body_kind({"success": False, "error": "bad token"}), "error"
        )

    def test_non_dict_is_error(self):
        self.assertEqual(cb._price_body_kind(None), "error")
        self.assertEqual(cb._price_body_kind([]), "error")

    def test_success_true_without_data_is_empty(self):
        self.assertEqual(cb._price_body_kind({"success": True}), "empty")


class HttpGetTests(StatsMixin):
    def test_empty_200_counts_as_empty_not_ok(self):
        body = json.dumps({"success": True, "data": [], "currency": "eur"})
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
            data = cb.http_get_json("https://example.test/prices?token=super-secret-token")
        self.assertEqual(data["data"], [])
        self.assertEqual(cb.STATS["empty"], 1)
        self.assertEqual(cb.STATS["ok"], 0)
        self.assertEqual(cb.STATS["http_error"], 0)

    def test_priced_200_counts_as_ok(self):
        body = json.dumps({"success": True, "data": [{"price": 88}]})
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
            data = cb.http_get_json("https://example.test/prices")
        self.assertEqual(data["data"][0]["price"], 88)
        self.assertEqual(cb.STATS["ok"], 1)
        self.assertEqual(cb.STATS["empty"], 0)

    def test_error_body_counts_as_http_error(self):
        body = json.dumps({"success": False, "error": "unauthorized"})
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
            data = cb.http_get_json("https://example.test/prices")
        self.assertIsNone(data)
        self.assertEqual(cb.STATS["http_error"], 1)
        self.assertEqual(cb.STATS["ok"], 0)
        self.assertEqual(cb.STATS["empty"], 0)

    def test_rate_limit_then_empty_retries_and_counts_both(self):
        body = json.dumps({"success": True, "data": []})
        side = [_http_error(429), _Resp(body)]
        with mock.patch("urllib.request.urlopen", side_effect=side), \
                mock.patch("check_beach.time.sleep"):
            data = cb.http_get_json("https://example.test/prices", attempts=4)
        self.assertEqual(data["data"], [])
        self.assertEqual(cb.STATS["rate_limited"], 1)
        self.assertEqual(cb.STATS["empty"], 1)
        self.assertEqual(cb.STATS["ok"], 0)
        self.assertEqual(cb.STATS["gave_up"], 0)

    def test_exhausted_rate_limits_give_up(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(429)), \
                mock.patch("check_beach.time.sleep"):
            data = cb.http_get_json("https://example.test/prices", attempts=4)
        self.assertIsNone(data)
        self.assertEqual(cb.STATS["rate_limited"], 4)
        self.assertEqual(cb.STATS["gave_up"], 1)
        self.assertEqual(cb.STATS["ok"], 0)
        self.assertEqual(cb.STATS["empty"], 0)

    def test_error_log_omits_token(self):
        url = "https://example.test/prices?origin=VIE&token=super-secret-token"
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)):
            data = cb.http_get_json(url, attempts=1)
        self.assertIsNone(data)
        blob = " ".join(cb._first_errors)
        self.assertIn("HTTP 400", blob)
        self.assertNotIn("super-secret-token", blob)
        self.assertIn("token=***", blob)

    def test_fetch_flight_treats_empty_body_as_no_price(self):
        body = json.dumps({"success": True, "data": [], "currency": "eur"})
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
            result = cb.fetch_flight_rt(
                "VIE", "JTR", date(2026, 11, 6), date(2026, 11, 9)
            )
        self.assertIsNone(result)
        self.assertEqual(cb.STATS["empty"], 1)
        self.assertEqual(cb.STATS["ok"], 0)


class ScanHealthTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(cb.MIN_COVERAGE, 0.10)
        self.assertEqual(cb.PARTIAL_COVERAGE, 0.50)
        self.assertEqual(cb.MAX_TRANSPORT_ERROR_RATE, 0.25)
        self.assertEqual(cb.MAX_WORKERS, 6)

    def test_november_thin_cache_publishes_a_partial_board(self):
        # Shape of run 41843429992 after empty bodies are counted on their own:
        # ~6240 finished calls, a few hundred rate-limit retries, no HTTP errors,
        # and 24 of 65 destinations with a fare.
        stats = {
            "ok": 900,
            "empty": 5180,
            "rate_limited": 832,
            "http_error": 0,
            "network": 0,
            "gave_up": 160,
        }
        health = cb.scan_health(24, 65, stats)
        self.assertTrue(health["publish"])
        self.assertTrue(health["partial"])
        self.assertIn("(37%)", health["note"])
        self.assertIn("5180 responses were empty", health["note"])
        self.assertIn("832 rate-limit responses were retried", health["note"])
        self.assertNotIn("throttl", health["note"].lower())
        self.assertLess(health["error_rate"], 0.25)

    def test_just_over_half_has_no_partial_note(self):
        stats = {"ok": 3000, "empty": 3000, "rate_limited": 800,
                 "http_error": 0, "network": 0, "gave_up": 80}
        health = cb.scan_health(33, 65, stats)
        self.assertTrue(health["publish"])
        self.assertFalse(health["partial"])
        self.assertIsNone(health["note"])

    def test_just_under_half_is_partial_and_green(self):
        stats = {"ok": 2800, "empty": 3200, "rate_limited": 800,
                 "http_error": 0, "network": 0, "gave_up": 80}
        health = cb.scan_health(32, 65, stats)
        self.assertTrue(health["publish"])
        self.assertTrue(health["partial"])

    def test_near_zero_coverage_keeps_previous_file(self):
        stats = {"ok": 40, "empty": 6100, "rate_limited": 100,
                 "http_error": 0, "network": 0, "gave_up": 20}
        health = cb.scan_health(6, 65, stats)
        self.assertFalse(health["publish"])
        self.assertFalse(health["broken_transport"])
        text = cb.format_scan_refusal(health)
        self.assertIn("Not overwriting deals.json", text)
        self.assertIn("missing fares", text)
        self.assertIn("6100 empty", text)
        self.assertNotIn("throttl", text.lower())

    def test_floor_boundary(self):
        stats = {"ok": 100, "empty": 6000, "rate_limited": 0,
                 "http_error": 0, "network": 0, "gave_up": 0}
        self.assertTrue(cb.scan_health(7, 65, stats)["publish"])
        self.assertFalse(cb.scan_health(6, 65, stats)["publish"])

    def test_many_http_errors_fail_even_with_broad_coverage(self):
        stats = {"ok": 2000, "empty": 1000, "rate_limited": 0,
                 "http_error": 3000, "network": 10, "gave_up": 0}
        health = cb.scan_health(40, 65, stats)
        self.assertFalse(health["publish"])
        self.assertTrue(health["broken_transport"])
        text = cb.format_scan_refusal(health)
        self.assertIn("API calls failed", text)
        self.assertNotIn("throttl", text.lower())

    def test_no_finished_calls_fails(self):
        stats = {"ok": 0, "empty": 0, "rate_limited": 0,
                 "http_error": 0, "network": 0, "gave_up": 0}
        health = cb.scan_health(0, 65, stats)
        self.assertFalse(health["publish"])
        self.assertTrue(health["broken_transport"])

    def test_zero_destinations_fails_even_if_floor_is_disabled(self):
        stats = {"ok": 0, "empty": 100, "rate_limited": 0,
                 "http_error": 0, "network": 0, "gave_up": 0}
        health = cb.scan_health(0, 65, stats, min_coverage=0)
        self.assertFalse(health["publish"])
        self.assertIn("no destination returned a fare", health["reasons"])


class SiteDataTests(unittest.TestCase):
    def test_partial_health_is_written_for_the_dashboard(self):
        stats = {"ok": 900, "empty": 5180, "rate_limited": 832,
                 "http_error": 0, "network": 0, "gave_up": 160}
        health = cb.scan_health(24, 65, stats)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deals.json")
            with mock.patch.object(cb, "SITE_DATA_FILE", path):
                cb.write_site_data([], [], {}, health)
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        self.assertEqual(payload["note"], health["note"])
        self.assertEqual(payload["coverage"]["destinations_with_fares"], 24)
        self.assertEqual(payload["coverage"]["destinations"], 65)
        self.assertTrue(payload["coverage"]["partial"])
        self.assertEqual(payload["coverage"]["empty_responses"], 5180)
        self.assertNotIn("throttl", (payload["note"] or "").lower())


if __name__ == "__main__":
    unittest.main()
