"""授信与规则引擎测试：授权门槛、决策分流、规则留痕与可复现。"""

import unittest

from credit.errors import DomainError
from credit.rules import AffordabilityEngine
from credit.store import Store
from credit.testing import (PURPOSES_ALL, SCOPES_ALL, bootstrap_customer,
                            healthy_credit, healthy_debt, healthy_income, make_app)


class UnderwritingTest(unittest.TestCase):
    def test_healthy_customer_approved_with_limit_and_account(self):
        app = make_app()
        result = bootstrap_customer(app)
        self.assertEqual(result["assessment"]["decision"], "approved")
        # 申请 5 万，年收入 24 万 × 0.5 = 12 万上限，绝对上限 20 万，
        # 可负担额度高于申请额 → 按申请额批
        self.assertEqual(result["assessment"]["approved_limit_cents"], 50000_00)
        account = app.credit_lines.get_account(
            app.store.query_one("SELECT id FROM credit_accounts WHERE customer_id='c1'")["id"])
        self.assertEqual(account["status"], "active")
        self.assertEqual(account["available_cents"], 50000_00)

    def test_assessment_requires_all_three_scopes(self):
        app = make_app()
        app.customers.create("c1", "张三")
        # 只授权收入，不给负债与征信
        app.consents.grant("c1", ["income_read"], PURPOSES_ALL, "G-ONLY-INCOME")
        app.consents.record_read("c1", "income", "credit_assessment",
                                 "payroll_bank", "2026-08-31", healthy_income())
        app_submitted = app.underwriting.submit_application(
            "c1", "50000", "0.072", 12, "装修", "home_improvement")
        with self.assertRaises(DomainError) as ctx:
            app.underwriting.assess(app_submitted["id"])
        self.assertEqual(ctx.exception.code, "consent_required")
        self.assertEqual(ctx.exception.details["required_scope"], "debt_read")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_revoked_consent_blocks_new_read(self):
        app = make_app()
        app.customers.create("c1", "张三")
        consent = app.consents.grant("c1", SCOPES_ALL, PURPOSES_ALL, "G1")
        app.consents.revoke(consent["id"])
        with self.assertRaises(DomainError) as ctx:
            app.consents.record_read("c1", "income", "credit_assessment",
                                     "payroll_bank", "2026-08-31", healthy_income())
        self.assertEqual(ctx.exception.code, "consent_required")

    def test_grace_period_goes_to_manual_review_not_auto_approve(self):
        app = make_app()
        result = bootstrap_customer(app, customer_id="c2", credit=healthy_credit("grace"))
        self.assertEqual(result["assessment"]["decision"], "manual_review")
        self.assertIn("credit_in_grace", Store.loads(result["assessment"]["triggered_reasons"]))
        # 不产生额度账户
        self.assertIsNone(app.store.query_one(
            "SELECT id FROM credit_accounts WHERE customer_id='c2'"))
        # 产生授信阶段人工案件
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE customer_id='c2' AND topic='underwriting'")
        self.assertIsNotNone(case)
        self.assertEqual(case["status"], "open")

    def test_severe_overdue_rejected(self):
        app = make_app()
        result = bootstrap_customer(app, customer_id="c3",
                                    credit=healthy_credit("overdue_90plus"))
        self.assertEqual(result["assessment"]["decision"], "rejected")
        self.assertIn("credit_adverse", Store.loads(result["assessment"]["triggered_reasons"]))

    def test_dti_cap_reduces_or_rejects(self):
        # 月入 8000，已有月供 4000（DTI 已 50%），再申大额贷款必触红线
        app = make_app()
        result = bootstrap_customer(
            app, customer_id="c4",
            income=healthy_income(8000_00),
            debt=healthy_debt(4000_00),
            amount_yuan="50000")
        reasons = Store.loads(result["assessment"]["triggered_reasons"])
        self.assertEqual(result["assessment"]["decision"], "rejected")
        self.assertTrue({"dti_exceeded", "disposable_below_minimum"} & set(reasons))

    def test_income_interruption_blocks(self):
        app = make_app()
        income = healthy_income()
        income["employment_status"] = "interrupted"
        result = bootstrap_customer(app, customer_id="c5", income=income)
        self.assertEqual(result["assessment"]["decision"], "rejected")
        self.assertIn("income_interrupted",
                      Store.loads(result["assessment"]["triggered_reasons"]))

    def test_assessment_is_reproducible_from_stored_snapshots(self):
        app = make_app()
        result = bootstrap_customer(app)
        asm = result["assessment"]
        inputs = Store.loads(asm["inputs"])
        app_row = app.store.require("applications", asm["application_id"])
        snaps = {}
        for kind, ref in inputs["snapshots"].items():
            snaps[kind] = Store.loads(app.store.require("snapshot_reads", ref["id"])["payload"])
        recomputed = AffordabilityEngine.compute(
            app.store.row_to_dict(app_row), snaps["income"], snaps["debt"],
            snaps["credit_report"], inputs["rule_set_version"])
        self.assertEqual(recomputed["decision"], asm["decision"])
        self.assertEqual(recomputed["approved_limit_cents"], asm["approved_limit_cents"])
        self.assertEqual(recomputed["reasons"], Store.loads(asm["triggered_reasons"]))

    def test_assessment_records_rule_version_and_data_as_of(self):
        app = make_app()
        result = bootstrap_customer(app)
        inputs = Store.loads(result["assessment"]["inputs"])
        self.assertEqual(inputs["rule_set_version"], "v1.0")
        for kind in ("income", "debt", "credit_report"):
            self.assertEqual(inputs["snapshots"][kind]["as_of"], "2026-08-31")
            self.assertTrue(inputs["snapshots"][kind]["id"].startswith("snap_"))


if __name__ == "__main__":
    unittest.main()
