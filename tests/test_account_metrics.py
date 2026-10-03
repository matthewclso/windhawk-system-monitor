import sys, unittest, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collector as c


class AccountMetrics(unittest.TestCase):
    def test_pro_weekly_only(self):
        data = c.normalize_codex(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 23,
                            "windowDurationMins": 10080,
                            "resetsAt": 2000000000,
                        },
                        "secondary": None,
                    }
                }
            }
        )
        self.assertEqual(
            [(w["label"], w["remaining"]) for w in data["windows"]], [("Weekly", 77)]
        )

    def test_five_hour_window_is_detected_by_duration(self):
        data = c.normalize_codex(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 90, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 20, "windowDurationMins": 10080},
                }
            }
        )
        self.assertEqual(
            [(w["label"], w["remaining"]) for w in data["windows"]],
            [("Weekly", 80), ("5h", 10)],
        )

    def test_other_limit_bucket_is_not_codex_usage(self):
        data = c.normalize_codex(
            {
                "rateLimitsByLimitId": {
                    "other": {"primary": {"usedPercent": 0, "windowDurationMins": 300}}
                },
                "rateLimits": {
                    "primary": {"usedPercent": 0, "windowDurationMins": 300}
                },
            }
        )
        self.assertEqual(data["windows"], [])

    def test_claude_session_does_not_duplicate_legacy_five_hour(self):
        data = c.normalize_claude(
            {
                "five_hour": {"utilization": 90},
                "limits": [
                    {"kind": "session", "percent": 12, "resets_at": None},
                    {
                        "kind": "weekly_all",
                        "percent": 36,
                        "resets_at": "2026-11-01T12:00:00Z",
                    },
                    {"kind": "weekly_scoped", "percent": 5},
                ],
            }
        )
        self.assertEqual(
            [(w["label"], w["remaining"]) for w in data["windows"]],
            [("Session", 88), ("Weekly", 64)],
        )

    def test_missing_usage_and_resets_are_not_full_or_zero(self):
        data = c.normalize_claude(
            {"five_hour": {"utilization": None}, "seven_day": None}
        )
        self.assertEqual(c.quota_text(data), "Usage unavailable")
        self.assertEqual(c.reset_text(data), "Unavailable")

    def test_capped_codex_reset_details_do_not_claim_earliest_expiry(self):
        data = c.normalize_codex(
            {
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [{"status": "available", "expiresAt": 2000000000}],
                }
            }
        )
        self.assertEqual(data["resets"]["count"], 2)
        self.assertIsNone(data["resets"]["expiresAt"])

    def test_complete_reset_inventory_uses_earliest_available(self):
        data = c.normalize_codex(
            {
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [
                        {"status": "available", "expiresAt": 2100000000},
                        {"status": "available", "expiresAt": 2000000000},
                        {"status": "consumed", "expiresAt": 1800000000},
                    ],
                }
            }
        )
        self.assertEqual(data["resets"]["expiresAt"], 2000000000)

    def test_count_zero_is_displayed(self):
        data = c.normalize_codex(
            {"rateLimitResetCredits": {"availableCount": 0, "credits": []}}
        )
        self.assertEqual(c.reset_text(data), "0 available")

    def test_signed_out_rows_hidden_and_cached_signed_in_rows_visible(self):
        rows = c.render(
            {},
            {
                "codex": {"signedIn": False},
                "claude": {
                    "signedIn": True,
                    "running": False,
                    "updatedAt": time.time(),
                    "data": {"windows": []},
                },
            },
        )["rows"]
        self.assertFalse(rows[4]["visible"])
        self.assertFalse(rows[5]["visible"])
        self.assertTrue(rows[6]["visible"])
        self.assertTrue(rows[7]["visible"])
        self.assertIn("cached", rows[6]["tooltip"])

    def test_elapsed_cached_window_requires_refresh(self):
        text = c.quota_text(
            {
                "windows": [
                    {"label": "Weekly", "remaining": 85, "resetsAt": time.time() - 1}
                ]
            }
        )
        self.assertIn("Refresh pending", text)
        self.assertNotIn("85%", text)

    def test_claude_grants_sum_remaining_and_use_earliest_expiry(self):
        now = time.time()
        data = c.normalize_claude_grants(
            {
                "grants": [
                    {"resets_left": 2, "ends_at": now + 1000},
                    {"resets_left": 1, "ends_at": now + 2000},
                    {"resets_left": 4, "ends_at": now - 1},
                    {"resets_left": 3, "paused": True, "ends_at": now + 500},
                    {"resets_left": 0, "ends_at": now + 100},
                ]
            }
        )
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["expiresAt"], now + 1000)

    def test_claude_claimable_grant_counts_once(self):
        data = c.normalize_claude_grants(
            {
                "grants": [
                    {"resets_left": 0, "claimable": True, "ends_at": time.time() + 100}
                ]
            }
        )
        self.assertEqual(data["count"], 1)

    def test_claude_missing_expiry_does_not_claim_known_earliest(self):
        data = c.normalize_claude_grants(
            {
                "grants": [
                    {"resets_left": 1},
                    {"resets_left": 1, "ends_at": time.time() + 100},
                ]
            }
        )
        self.assertEqual(data["count"], 2)
        self.assertFalse(data["expiryKnown"])
        self.assertIsNone(data["expiresAt"])

    def test_claude_missing_inventory_is_unknown(self):
        self.assertIsNone(c.normalize_claude_grants(None)["count"])


if __name__ == "__main__":
    unittest.main()
