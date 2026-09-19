"""合规复现、客户可理解性、额度变化原因、人工决定留痕。"""

import json
import unittest

from tests.helpers import approved_line, make_app, seed_customer, update_snapshots

CUSTOMER = {"id": "cust-1", "role": "customer"}
RISK = {"id": "officer-1", "role": "risk_officer"}
COMPLIANCE = {"id": "auditor", "role": "compliance"}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        seed_customer(self.app, as_of_income="2026-08-31", as_of_credit="2026-09-05")

    def test_assessment_is_reproducible_from_frozen_inputs(self):
        res = self.app.apply(
            {"customer_id": "cust-1", "requested_yuan": 50000, "term_months": 12},
            CUSTOMER)
        replay = self.app.replay(res["application_id"], COMPLIANCE)
        self.assertTrue(replay["reproducible"])
        self.assertEqual(replay["decision"], replay["recomputed"]["decision"])
        self.assertEqual(replay["max_amount_cents"], replay["recomputed"]["max_amount_cents"])
        self.assertEqual(replay["rule_version"], "affordability-v1.0")

    def test_replay_includes_data_point_in_time(self):
        res = self.app.apply(
            {"customer_id": "cust-1", "requested_yuan": 50000}, CUSTOMER)
        replay = self.app.replay(res["application_id"], COMPLIANCE)
        self.assertEqual(replay["data_as_of"]["income"], "2026-08-31")
        self.assertEqual(replay["data_as_of"]["credit_report"], "2026-09-05")
        self.assertEqual(replay["data_as_of"]["debt_snapshot"], "2026-09-05")
        # 输入中固化了快照 ID，可回溯到具体快照行
        self.assertTrue(replay["inputs"]["income"]["snapshot_id"])

    def test_later_snapshot_changes_do_not_alter_historical_decision(self):
        res = self.app.apply(
            {"customer_id": "cust-1", "requested_yuan": 50000}, CUSTOMER)
        original = self.app.replay(res["application_id"], COMPLIANCE)
        # 授信后客户收入骤降：历史决策仍可按当时输入复现
        update_snapshots(self.app, "cust-1", income_cents=500000)
        again = self.app.replay(res["application_id"], COMPLIANCE)
        self.assertTrue(again["reproducible"])
        self.assertEqual(again["max_amount_cents"], original["max_amount_cents"])

    def test_snapshot_access_log_shows_who_read_what_and_why(self):
        res = self.app.apply(
            {"customer_id": "cust-1", "requested_yuan": 50000},
            {"id": "cust-1", "role": "customer"})
        log = self.app.access_log("cust-1", COMPLIANCE)["accesses"]
        purposes = {a["purpose"] for a in log}
        self.assertIn("affordability_assessment", purposes)
        self.assertTrue(all(a["actor"] for a in log))
        self.assertTrue(all(a["snapshot_id"] for a in log))


class CustomerExplanationTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        seed_customer(self.app)

    def test_rejection_explains_reasons_in_business_terms(self):
        # 收入中断且处于宽限期
        update_snapshots(self.app, "cust-1", income_cents=0, income_status="interrupted",
                         in_grace=True)
        res = self.app.apply(
            {"customer_id": "cust-1", "requested_yuan": 50000}, CUSTOMER)
        self.assertEqual(res["assessment"]["decision"], "rejected")
        reasons = res["assessment"]["reasons"]
        self.assertIn("INCOME_INTERRUPTED", reasons)
        self.assertIn("BORROWER_IN_GRACE_PERIOD", reasons)
        # 同时给出价格/期限字段（即使被拒），口径透明
        self.assertIn("annual_rate", res["assessment"])

    def test_line_change_history_explains_every_change(self):
        approved_line(self.app, requested_yuan=50000)
        # 风险触发冻结
        update_snapshots(self.app, "cust-1", income_cents=900000)
        self.app.line_review("cust-1", {}, RISK)
        view = self.app.line_view("cust-1", CUSTOMER)
        codes = {c["reason_code"] for c in view["change_history"]}
        self.assertIn("INITIAL_AFFORDABILITY", codes)
        self.assertIn("PERIODIC_RISK_REVIEW", codes)
        freeze = [c for c in view["change_history"]
                  if c["reason_code"] == "PERIODIC_RISK_REVIEW"][0]
        self.assertIn("INCOME_DROP", freeze["reason_detail"])

    def test_manual_reduction_records_officer_and_rationale(self):
        approved_line(self.app, requested_yuan=50000)
        update_snapshots(self.app, "cust-1", income_cents=900000)
        hold = None
        from credit.errors import RiskHeld
        try:
            self.app.withdraw(
                {"customer_id": "cust-1", "amount_yuan": 20000}, CUSTOMER)
        except RiskHeld as e:
            hold = e
        self.app.decide_case(hold.details["manual_case_id"], {
            "decision": "reduce_and_disburse", "reduced_amount_yuan": 8000,
            "rationale": "按当前收入下调"}, RISK)
        case = self.app.storage.get_manual_case(hold.details["manual_case_id"])
        self.assertEqual(case["decided_by"], "officer-1")
        self.assertEqual(case["decision"], "reduce_and_disburse")
        self.assertIn("按当前收入下调", case["rationale"])

    def test_adjust_line_rejects_increase_without_new_assessment(self):
        from credit.errors import Conflict
        approved_line(self.app, requested_yuan=50000)
        with self.assertRaises(Conflict):
            self.app.line_adjust("cust-1", {
                "new_limit_yuan": 999999, "reason_code": "MARKETING_CAMPAIGN",
                "reason_detail": "季度营销冲量"}, RISK)

    def test_marketing_campaign_cannot_reduce_risk_conclusion(self):
        # 被拒申请不会因为营销目标而产生额度
        update_snapshots(self.app, "cust-1", in_grace=True)
        res = self.app.apply(
            {"customer_id": "cust-1", "requested_yuan": 50000}, CUSTOMER)
        self.assertEqual(res["assessment"]["decision"], "rejected")
        from credit.errors import Conflict
        with self.assertRaises(Conflict):
            self.app.offer(res["application_id"], {}, {"id": "sys", "role": "system"})


if __name__ == "__main__":
    unittest.main()
