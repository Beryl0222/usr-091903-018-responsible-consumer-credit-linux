"""授权范围、用途绑定与营销隔离。"""

import unittest

from credit.errors import ConsentRequired
from tests.helpers import make_app, seed_customer


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        seed_customer(self.app)

    def test_risk_read_requires_each_scope(self):
        from credit import consent
        s = self.app.storage
        now = self.app.clock.now().isoformat()
        consent.revoke(s, "cust-1", "income", now)
        with self.assertRaises(ConsentRequired) as ctx:
            consent.read_snapshot(s, "cust-1", "income",
                                  "affordability_assessment", "risk", now)
        self.assertEqual(ctx.exception.details["scope"], "income")

    def test_scope_cannot_be_used_for_marketing_purpose(self):
        from credit import consent
        s = self.app.storage
        now = self.app.clock.now().isoformat()
        # 即使持有 income 授权，也不能用于 marketing 用途
        with self.assertRaises(ConsentRequired):
            consent.read_snapshot(s, "cust-1", "income", "marketing", "mkt", now)

    def test_collections_purpose_cannot_read_sensitive_snapshots(self):
        from credit import consent
        s = self.app.storage
        now = self.app.clock.now().isoformat()
        with self.assertRaises(ConsentRequired):
            consent.read_snapshot(s, "cust-1", "income", "collections", "collector", now)

    def test_each_read_is_logged_for_audit(self):
        from credit import consent
        s = self.app.storage
        now = self.app.clock.now().isoformat()
        consent.read_risk_bundle(s, "cust-1", "withdrawal_risk_check", "risk_engine", now)
        accesses = s.list_snapshot_access("cust-1")
        self.assertEqual({a["scope"] for a in accesses},
                         {"income", "credit_report", "debt_snapshot"})
        self.assertTrue(all(a["purpose"] == "withdrawal_risk_check" for a in accesses))

    def test_revoked_consent_blocks_further_reads(self):
        from credit import consent
        s = self.app.storage
        consent.revoke(s, "cust-1", "credit_report", self.app.clock.now().isoformat())
        with self.assertRaises(ConsentRequired):
            consent.read_snapshot(
                s, "cust-1", "credit_report", "affordability_assessment",
                "risk", self.app.clock.now().isoformat())

    def test_marketing_eligibility_uses_only_marketing_consent(self):
        from credit import workflow
        # 持有风控授权但无营销授权：不可促销
        elig = workflow.marketing_eligibility(
            self.app.storage, "cust-1", self.app.clock.now().isoformat())
        self.assertFalse(elig["eligible"])
        self.assertFalse(elig["risk_data_used"])
        # 即使撤销全部风控授权，仅凭营销授权即可促销
        from credit import consent
        for scope in ("income", "credit_report", "debt_snapshot"):
            consent.revoke(self.app.storage, "cust-1", scope,
                           self.app.clock.now().isoformat())
        consent.grant(self.app.storage, "cust-1", ["marketing_use"], "marketing",
                      self.app.clock.now().isoformat())
        elig = workflow.marketing_eligibility(
            self.app.storage, "cust-1", self.app.clock.now().isoformat())
        self.assertTrue(elig["eligible"])


if __name__ == "__main__":
    unittest.main()
