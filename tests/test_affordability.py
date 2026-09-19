"""可负担性规则：DTI、收入中断、宽限期、多头负债、额度按收入核定。"""

import unittest

from credit.rules import Rule
from tests.helpers import make_app, seed_customer


def bundle(income=2000000, status="employed", obligation=300000, institutions=1,
           grace=False, overdue=0):
    return {
        "income": {"monthly_income_cents": income, "status": status},
        "credit_report": {"in_grace_period": grace, "has_overdue": overdue > 0,
                          "max_overdue_days": overdue},
        "debt_snapshot": {"monthly_obligation_cents": obligation,
                          "institution_count": institutions},
    }


class AffordabilityRuleTests(unittest.TestCase):
    def setUp(self):
        self.rule = Rule()

    def test_healthy_applicant_approved_with_income_based_limit(self):
        r = self.rule.assess_affordability(bundle(), 5000000, 12, "0.072")
        self.assertEqual(r["decision"], "approved")
        # 月供约 4324 元，DTI (3000+4324)/20000 = 36.6%，远低于 55%
        self.assertGreaterEqual(r["approved_limit_cents"], 5000000)
        self.assertEqual(r["reasons"], [])

    def test_request_above_affordable_limit_rejected_with_reason(self):
        r = self.rule.assess_affordability(bundle(), 15000000, 12, "0.072")
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("REQUESTED_ABOVE_AFFORDABLE_LIMIT", r["reasons"])
        # 但可负担上限仍然给出（供降额/沟通）
        self.assertLess(r["approved_limit_cents"], 15000000)
        self.assertGreater(r["approved_limit_cents"], 0)

    def test_income_interruption_is_hard_block(self):
        r = self.rule.assess_affordability(
            bundle(status="interrupted", income=0), 100000, 12, "0.072")
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("INCOME_INTERRUPTED", r["hard_blocks"])

    def test_borrower_in_grace_period_is_hard_block(self):
        r = self.rule.assess_affordability(bundle(grace=True), 100000, 12, "0.072")
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("BORROWER_IN_GRACE_PERIOD", r["hard_blocks"])

    def test_existing_overdue_is_hard_block(self):
        r = self.rule.assess_affordability(bundle(overdue=5), 100000, 12, "0.072")
        self.assertIn("EXISTING_OVERDUE", r["hard_blocks"])

    def test_high_existing_obligation_leaves_no_capacity(self):
        # 存量月供 11000，收入 20000：55% 上限 11000 已被占满
        r = self.rule.assess_affordability(
            bundle(obligation=1100000), 100000, 12, "0.072")
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("NO_AFFORDABLE_PAYMENT_CAPACITY", r["reasons"])

    def test_residual_income_floor_protects_basic_living(self):
        # 收入仅 2000，存量 0：55%=1100 月供能力，但扣 1500 元最低留存后只剩 500；
        # 借 10000 元月供约 865 元，突破留存底线
        r = self.rule.assess_affordability(
            bundle(income=200000, obligation=0), 1000000, 12, "0.072")
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("RESIDUAL_INCOME_BELOW_FLOOR", r["reasons"])

    def test_multi_institution_debt_flagged(self):
        r = self.rule.assess_affordability(
            bundle(institutions=4, obligation=100000), 5000000, 12, "0.072")
        self.assertIn("MULTI_INSTITUTION_DEBT", r["reasons"])

    def test_rule_versions_are_stable_string(self):
        self.assertEqual(self.rule.version, "affordability-v1.0")


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_application_requires_all_three_snapshots(self):
        from credit.errors import ValidationFailed
        from credit import consent
        s = self.app.storage
        cid = "cust-2"
        now = self.app.clock.now().isoformat()
        consent.grant(s, cid, ["income", "credit_report", "debt_snapshot"],
                      "affordability_assessment", now)
        with self.assertRaises(ValidationFailed) as ctx:
            self.app.apply({"customer_id": cid, "requested_yuan": 10000},
                           {"id": cid, "role": "customer"})
        self.assertIn("missing_snapshot", ctx.exception.details)

    def test_line_granted_at_affordable_cap_not_requested_amount(self):
        seed_customer(self.app, income_cents=2000000, obligation_cents=300000)
        res = self.app.apply(
            {"customer_id": "cust-1", "requested_yuan": 1000},
            {"id": "cust-1", "role": "customer"})
        line = self.app.offer(res["application_id"], {}, {"id": "sys", "role": "system"})
        # 即使只申请 1000 元，额度按可负担上限（约 9 万）核定
        self.assertGreater(line["total_limit_yuan"], 50000)


if __name__ == "__main__":
    unittest.main()
