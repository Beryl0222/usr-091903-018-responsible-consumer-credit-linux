"""提款：并发不超额、幂等、风险触发器暂停、人工决定（降额/取消/放款）。"""

import threading
import unittest
from datetime import timedelta

from credit.errors import InsufficientAvailable, RiskHeld
from tests.helpers import (
    approved_line,
    first_period_payment_yuan,
    make_app,
    seed_customer,
    update_snapshots,
)

CUSTOMER = {"id": "cust-1", "role": "customer"}
RISK_OFFICER = {"id": "officer-1", "role": "risk_officer"}


class WithdrawalConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        seed_customer(self.app)
        self.app_id, self.line = approved_line(self.app, requested_yuan=50000)

    def test_concurrent_withdrawals_never_exceed_valid_total_limit(self):
        total = int(self.line["total_limit_yuan"] * 100)
        amount_yuan = 10000
        amount_cents = amount_yuan * 100
        n = 10
        expect_success = total // amount_cents
        results = []
        errors = []
        barrier = threading.Barrier(n)

        def withdraw(i):
            barrier.wait()
            try:
                r = self.app.withdraw(
                    {"customer_id": "cust-1", "amount_yuan": amount_yuan,
                     "idempotency_key": f"w-{i}"}, CUSTOMER)
                results.append(r)
            except (InsufficientAvailable, RiskHeld) as e:
                errors.append(e)

        threads = [threading.Thread(target=withdraw, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        line = self.app.storage.get_line("cust-1")
        disbursed_total = sum(r["withdrawal"]["amount_yuan"] for r in results)
        self.assertEqual(len(results), expect_success)
        self.assertEqual(len(errors), n - expect_success)
        self.assertEqual(disbursed_total, expect_success * amount_yuan)
        self.assertGreaterEqual(line["available_cents"], 0)
        # 已用 + 可用 = 总额度（任何时点成立），且并发下绝不超额
        self.assertEqual(
            line["available_cents"]
            + sum(r["withdrawal"]["amount_yuan"] * 100 for r in results),
            line["total_limit_cents"],
        )

    def test_idempotency_key_replays_same_withdrawal(self):
        payload = {"customer_id": "cust-1", "amount_yuan": 5000,
                   "idempotency_key": "idem-1"}
        first = self.app.withdraw(payload, CUSTOMER)
        second = self.app.withdraw(payload, CUSTOMER)
        self.assertEqual(first["withdrawal"]["id"], second["withdrawal"]["id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(len(self.app.storage.query("SELECT * FROM withdrawals")), 1)

    def test_withdrawal_over_available_rejected(self):
        with self.assertRaises(InsufficientAvailable):
            self.app.withdraw(
                {"customer_id": "cust-1",
                 "amount_yuan": self.line["total_limit_yuan"] + 1}, CUSTOMER)


class WithdrawalRiskHoldTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        seed_customer(self.app)
        self.app_id, self.line = approved_line(self.app, requested_yuan=50000)

    def _withdraw_expecting_hold(self, amount=10000, purpose=None):
        try:
            self.app.withdraw(
                {"customer_id": "cust-1", "amount_yuan": amount,
                 "usage_purpose": purpose}, CUSTOMER)
            self.fail("应当触发风险暂停")
        except RiskHeld as e:
            return e

    def test_income_drop_holds_undisbursed_and_freezes_line(self):
        update_snapshots(self.app, "cust-1", income_cents=1200000)  # 下降 40%
        e = self._withdraw_expecting_hold()
        self.assertIn("INCOME_DROP", e.details["triggers"])
        self.assertFalse(e.details["disbursed"])
        line = self.app.storage.get_line("cust-1")
        self.assertEqual(line["status"], "frozen")
        wid = e.details["withdrawal_id"]
        self.assertEqual(self.app.storage.get_withdrawal(wid)["status"], "held")
        # 占用额度仍保留（不能被并发提款再占用）
        self.assertEqual(line["available_cents"], line["total_limit_cents"] - 1000000)
        # 有开放的人工案件
        case = self.app.storage.get_manual_case(e.details["manual_case_id"])
        self.assertEqual(case["case_status"], "open")

    def test_suspected_borrow_to_repay_triggers_hold(self):
        update_snapshots(self.app, "cust-1",
                         new_short_term_cents=2000000, institutions=2)
        e = self._withdraw_expecting_hold(purpose="debt_repayment")
        self.assertIn("SUSPECTED_BORROW_TO_REPAY", e.details["triggers"])

    def test_new_multi_institution_debt_worsening_dti_triggers_hold(self):
        update_snapshots(self.app, "cust-1", obligation_cents=600000)
        e = self._withdraw_expecting_hold()
        self.assertIn("NEW_MULTI_INSTITUTION_DEBT", e.details["triggers"])

    def test_entering_grace_period_after_assessment_holds(self):
        update_snapshots(self.app, "cust-1", in_grace=True)
        e = self._withdraw_expecting_hold()
        self.assertIn("BORROWER_IN_GRACE_PERIOD", e.details["triggers"])

    def test_manual_rejection_cannot_be_overridden_by_marketing_goal(self):
        update_snapshots(self.app, "cust-1", income_cents=1000000)  # 降 50%
        e = self._withdraw_expecting_hold()
        # 不存在"忽略触发器强制放款"的参数；risk_officer 必须走案件决定并给理由
        from credit.errors import ValidationFailed
        with self.assertRaises(ValidationFailed):
            self.app.decide_case(
                e.details["manual_case_id"],
                {"decision": "disburse", "rationale": "   "}, RISK_OFFICER)

    def test_officer_can_disburse_after_review_with_rationale(self):
        update_snapshots(self.app, "cust-1", income_cents=1300000)  # 降 35%
        e = self._withdraw_expecting_hold()
        result = self.app.decide_case(
            e.details["manual_case_id"],
            {"decision": "disburse",
             "rationale": "已核实客户为单位延迟发薪，下月补发，负债无变化"},
            RISK_OFFICER)
        self.assertEqual(result["result"]["withdrawal_status"], "disbursed")
        self.assertEqual(self.app.storage.get_line("cust-1")["status"], "active")

    def test_officer_can_cancel_and_release_reserved_amount(self):
        update_snapshots(self.app, "cust-1", income_cents=1000000)
        e = self._withdraw_expecting_hold()
        result = self.app.decide_case(
            e.details["manual_case_id"],
            {"decision": "cancel", "rationale": "收入未恢复，暂不放款"}, RISK_OFFICER)
        self.assertEqual(result["result"]["withdrawal_status"], "cancelled")
        line = self.app.storage.get_line("cust-1")
        self.assertEqual(line["available_cents"], line["total_limit_cents"])
        self.assertEqual(line["status"], "active")

    def test_officer_can_reduce_then_disburse(self):
        update_snapshots(self.app, "cust-1", income_cents=1000000)
        e = self._withdraw_expecting_hold(amount=20000)
        result = self.app.decide_case(
            e.details["manual_case_id"],
            {"decision": "reduce_and_disburse", "reduced_amount_yuan": 8000,
             "rationale": "按当前收入重算可负担月供，降额至 8000"}, RISK_OFFICER)
        self.assertEqual(result["result"]["disbursed_cents"], 800000)
        loan_id = result["result"]["loan_id"]
        loan = self.app.storage.get_loan(loan_id)
        self.assertEqual(loan["principal_cents"], 800000)
        line = self.app.storage.get_line("cust-1")
        # 释放 12000 差额，占用 8000
        self.assertEqual(line["available_cents"], line["total_limit_cents"] - 800000)


class PeriodicReviewTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        seed_customer(self.app)
        approved_line(self.app, requested_yuan=50000)

    def test_periodic_review_freezes_line_on_income_shock(self):
        update_snapshots(self.app, "cust-1", income_cents=900000)
        r = self.app.line_review("cust-1", {}, RISK_OFFICER)
        self.assertEqual(r["line_status"], "frozen")
        self.assertIn("INCOME_DROP", r["triggers"])

    def test_clean_review_leaves_line_active(self):
        r = self.app.line_review("cust-1", {}, RISK_OFFICER)
        self.assertEqual(r["triggers"], [])
        self.assertEqual(r["line_status"], "active")


if __name__ == "__main__":
    unittest.main()
