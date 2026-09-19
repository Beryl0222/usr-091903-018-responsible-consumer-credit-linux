"""贷款台账：等额本息、还款冲账、商户退款/分期取消原路冲减、结清。"""

import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import approved_line, make_app, seed_customer

CUSTOMER = {"id": "cust-1", "role": "customer"}
SYSTEM = {"id": "sys", "role": "system"}


def disbursed(app, amount_yuan=20000, merchant=None):
    seed_customer(app)
    approved_line(app, requested_yuan=50000)
    r = app.withdraw(
        {"customer_id": "cust-1", "amount_yuan": amount_yuan,
         "merchant": merchant or {"id": "m-1", "name": "商户甲"},
         "usage_purpose": "appliance"}, CUSTOMER)
    return r["withdrawal"]["id"], r["loan_id"], r["cost_disclosure"]


def advance_to_first_due(app, loan_view, days_after=5):
    due = datetime.fromisoformat(loan_view["schedule"][0]["due_date"]).replace(
        tzinfo=timezone.utc)
    app.clock.set(due + timedelta(days=days_after))


class ScheduleMathTests(unittest.TestCase):
    def test_schedule_principal_sums_exactly(self):
        from credit.util import build_schedule
        rows = build_schedule(1234567, "0.072", 12, datetime(2026, 10, 19).date())
        self.assertEqual(sum(r[2] for r in rows), 1234567)
        self.assertTrue(all(r[2] > 0 and r[3] >= 0 for r in rows))


class RepaymentTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.wid, self.loan_id, self.cost = disbursed(self.app)

    def test_cost_disclosure_is_customer_readable(self):
        self.assertEqual(self.cost["principal_yuan"], 20000)
        self.assertEqual(self.cost["annual_rate"], "0.072")
        self.assertGreater(self.cost["total_repayment_yuan"], 20000)
        self.assertAlmostEqual(
            self.cost["total_repayment_yuan"],
            20000 + self.cost["total_interest_yuan"], places=2)

    def test_normal_repayment_pays_first_installment(self):
        view = app_loan(self.app, self.loan_id)
        first = view["schedule"][0]
        advance_to_first_due(self.app, view)
        amount = (first["principal_cents"] + first["interest_cents"]) / 100
        r = self.app.repay(self.loan_id, {"amount_yuan": amount}, CUSTOMER)
        self.assertEqual(r["loan_status"], "repaying")
        view2 = app_loan(self.app, self.loan_id)
        self.assertEqual(view2["schedule"][0]["status"], "paid")
        # 偿还本金恢复可用额度；可用 = 总额度 - 剩余本金占用
        line = self.app.storage.get_line("cust-1")
        self.assertEqual(line["available_cents"],
                         line["total_limit_cents"] - view2["outstanding_principal_cents"])

    def test_interest_does_not_restore_available_credit(self):
        view = app_loan(self.app, self.loan_id)
        first = view["schedule"][0]
        advance_to_first_due(self.app, view)
        # 只还利息部分
        self.app.repay(self.loan_id,
                       {"amount_yuan": first["interest_cents"] / 100}, CUSTOMER)
        line = self.app.storage.get_line("cust-1")
        view2 = app_loan(self.app, self.loan_id)
        # 本金一分未减，可用额度不恢复（仅利息被清偿）
        self.assertEqual(view2["outstanding_principal_cents"],
                         view["outstanding_principal_cents"])
        self.assertEqual(line["available_cents"],
                         line["total_limit_cents"] - view2["outstanding_principal_cents"])

    def test_prepayment_rebuilds_remaining_schedule(self):
        view = app_loan(self.app, self.loan_id)
        advance_to_first_due(self.app, view, days_after=30)
        # 一次性提前结清：剩余本金 + 首期应计利息
        first = view["schedule"][0]
        payoff = (view["outstanding_principal_cents"] + first["interest_cents"]) / 100
        r = self.app.repay(self.loan_id, {"amount_yuan": payoff}, CUSTOMER)
        self.assertEqual(r["principal_balance_after_cents"], 0)
        self.assertEqual(r["loan_status"], "closed")
        view2 = app_loan(self.app, self.loan_id)
        self.assertEqual(view2["outstanding_principal_yuan"], 0)
        self.assertEqual(view2["outstanding_interest_yuan"], 0)

    def test_overpayment_is_rejected(self):
        from credit.errors import ValidationFailed
        with self.assertRaises(ValidationFailed):
            self.app.repay(self.loan_id, {"amount_yuan": 999999}, CUSTOMER)


class MerchantReversalTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.wid, self.loan_id, _ = disbursed(self.app)

    def test_refund_reduces_principal_but_is_not_a_repayment(self):
        r = self.app.refund(self.wid, {"amount_yuan": 5000,
                                       "route": "merchant_settlement"}, SYSTEM)
        self.assertFalse(r["treated_as_repayment"])
        self.assertEqual(r["reversed_principal_cents"], 500000)
        view = app_loan(self.app, self.loan_id)
        self.assertEqual(view["outstanding_principal_yuan"], 15000)
        # 台账中不存在 repayment 类型条目；没有任何期次被标记 paid
        kinds = {e["kind"] for e in self.app.storage.ledger_entries(loan_id=self.loan_id)}
        self.assertIn("merchant_refund", kinds)
        self.assertNotIn("repayment", kinds)
        self.assertTrue(all(s["status"] == "scheduled" for s in view["schedule"]))
        # 原路信息可追溯
        refund_entry = [e for e in self.app.storage.ledger_entries(loan_id=self.loan_id)
                        if e["kind"] == "merchant_refund"][0]
        self.assertEqual(refund_entry["original_route"], "merchant_settlement")

    def test_refund_after_partial_repayment_does_not_double_count(self):
        view = app_loan(self.app, self.loan_id)
        first = view["schedule"][0]
        advance_to_first_due(self.app, view)
        self.app.repay(self.loan_id,
                       {"amount_yuan": (first["principal_cents"] + first["interest_cents"]) / 100},
                       CUSTOMER)
        # 已还本金约 1612.38，退款 5000 只能冲减剩余本金
        r = self.app.refund(self.wid, {"amount_yuan": 5000}, SYSTEM)
        self.assertEqual(r["reversed_principal_cents"], 500000)
        view2 = app_loan(self.app, self.loan_id)
        # 计划仍 12 期，第 1 期保持 paid，后续期次按 13387.62 本金重排
        statuses = [s["status"] for s in view2["schedule"]]
        self.assertEqual(statuses[0], "paid")
        self.assertEqual(sum(1 for s in statuses if s == "scheduled"), 11)
        self.assertEqual(round(view2["outstanding_principal_yuan"], 2),
                         round(20000 - first["principal_cents"] / 100 - 5000, 2))

    def test_full_refund_closes_loan_and_waives_unpaid_interest(self):
        total_interest_before = app_loan(self.app, self.loan_id)["total_interest_cents"]
        r = self.app.refund(self.wid, {"amount_yuan": 20000}, SYSTEM)
        self.assertEqual(r["loan_status"], "closed")
        self.assertEqual(r["interest_waived_cents"], total_interest_before)
        view = app_loan(self.app, self.loan_id)
        self.assertEqual(view["outstanding_principal_yuan"], 0)

    def test_refund_over_remaining_principal_rejected(self):
        from credit.errors import Conflict
        # 先偿还首期，剩余本金约 18387.62；退款 20000（≤原提款但 >剩余本金）应被拒
        view = app_loan(self.app, self.loan_id)
        first = view["schedule"][0]
        advance_to_first_due(self.app, view)
        self.app.repay(
            self.loan_id,
            {"amount_yuan": (first["principal_cents"] + first["interest_cents"]) / 100},
            CUSTOMER)
        with self.assertRaises(Conflict):
            self.app.refund(self.wid, {"amount_yuan": 20000}, SYSTEM)


class InstallmentCancelTests(unittest.TestCase):
    def test_cancel_before_disbursement_releases_without_loan(self):
        app = make_app()
        seed_customer(app)
        _, line = approved_line(app, requested_yuan=50000)
        total = line["total_limit_yuan"]
        # 预留但不放款
        w = app.withdraw(
            {"customer_id": "cust-1", "amount_yuan": 10000, "auto_disburse": False,
             "merchant": {"id": "m-1"}}, CUSTOMER)
        wid = w["withdrawal"]["id"]
        r = app.cancel_installment(wid, {}, SYSTEM)
        self.assertIsNone(r["loan_id"])
        self.assertEqual(r["withdrawal_status"], "cancelled")
        line2 = app.storage.get_line("cust-1")
        self.assertEqual(line2["available_cents"], line2["total_limit_cents"])
        self.assertEqual(line2["total_limit_cents"], int(total * 100))
        # 没有任何贷款/还款/退款台账
        self.assertEqual(app.storage.query("SELECT * FROM loans"), [])

    def test_cancel_after_disbursement_reverses_in_full(self):
        app = make_app()
        wid, loan_id, _ = disbursed(app, amount_yuan=8000)
        r = app.cancel_installment(wid, {"route": "merchant_cancel"}, SYSTEM)
        self.assertEqual(r["treated_as_repayment"], False)
        self.assertEqual(r["principal_balance_after_cents"], 0)


def app_loan(app, loan_id):
    return app.loan_view(loan_id, {"id": "x", "role": "risk_officer"})


if __name__ == "__main__":
    unittest.main()
