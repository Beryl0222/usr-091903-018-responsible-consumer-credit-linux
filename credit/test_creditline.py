"""额度台账测试：并发提款不超额、凭证核验、退款/取消原路冲减、还款。"""

import threading
import unittest
from decimal import Decimal

from credit.errors import DomainError
from credit.money import build_schedule
from credit.store import Store
from credit.testing import approved_account, draw_and_disburse, make_app


class CreditLineTest(unittest.TestCase):
    def test_concurrent_drawdowns_never_exceed_valid_limit(self):
        app = make_app()
        result = approved_account(app, amount_yuan="50000")
        account_id = result["account_id"]

        outcomes = []

        def reserve(amount):
            try:
                drw = app.credit_lines.reserve(
                    "c1", account_id, amount, "装修", "M", "home_improvement")
                outcomes.append(("ok", drw["id"], int(amount * 100)))
            except DomainError as exc:
                outcomes.append(("rejected", exc.code, int(amount * 100)))

        # 8 笔每笔 1 万，总额度 5 万：恰好 5 笔成功、3 笔被拒
        threads = [threading.Thread(target=reserve, args=(10000,)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok = [o for o in outcomes if o[0] == "ok"]
        rejected = [o for o in outcomes if o[0] == "rejected"]
        self.assertEqual(len(ok), 5)
        self.assertEqual(len(rejected), 3)
        self.assertTrue(all(o[1] == "conflict" for o in rejected))

        account = app.credit_lines.get_account(account_id)
        self.assertEqual(account["reserved_cents"], 50000_00)
        self.assertEqual(account["outstanding_cents"], 0)
        self.assertEqual(account["available_cents"], 0)

    def test_reserve_then_disburse_moves_reserved_to_outstanding(self):
        app = make_app()
        result = approved_account(app, amount_yuan="50000")
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "10000")
        account = app.credit_lines.get_account(result["account_id"])
        self.assertEqual(account["reserved_cents"], 0)
        self.assertEqual(account["outstanding_cents"], 10000_00)
        self.assertEqual(account["available_cents"], 40000_00)
        # 还款计划 12 期
        self.assertEqual(len(Store.loads(loan["schedule"])), 12)
        total = sum(r["payment_cents"] for r in Store.loads(loan["schedule"]))
        self.assertGreater(total, 10000_00)  # 含利息

    def test_suspended_account_cannot_draw(self):
        app = make_app()
        result = approved_account(app)
        app.store._conn.execute(
            "UPDATE credit_accounts SET status='suspended' WHERE id=?",
            (result["account_id"],))
        with self.assertRaises(DomainError) as ctx:
            app.credit_lines.reserve("c1", result["account_id"], "1000",
                                     "装修", "M", "home_improvement")
        self.assertEqual(ctx.exception.code, "conflict")

    def test_evidence_conflict_suspends_drawdown_and_opens_case(self):
        app = make_app()
        result = approved_account(app)
        drw = app.credit_lines.reserve(
            "c1", result["account_id"], "10000", "装修", "M", "home_improvement")
        # 凭证显示购买了理财（套现/违规用途）
        app.credit_lines.submit_evidence(drw["id"], {
            "items": [{"category": "securities", "amount_cents": 10000_00}]
        })
        refreshed = app.store.require("drawdowns", drw["id"])
        self.assertEqual(refreshed["status"], "suspended")
        with self.assertRaises(DomainError) as ctx:
            app.credit_lines.disburse(drw["id"])
        self.assertEqual(ctx.exception.code, "conflict")
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE drawdown_id=?", (drw["id"],))
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["topic"], "drawdown")

    def test_evidence_amount_mismatch_suspends(self):
        app = make_app()
        result = approved_account(app)
        drw = app.credit_lines.reserve(
            "c1", result["account_id"], "10000", "装修", "M", "home_improvement")
        app.credit_lines.submit_evidence(drw["id"], {
            "items": [{"category": "home_improvement", "amount_cents": 500_00}]
        })
        self.assertEqual(app.store.require("drawdowns", drw["id"])["status"], "suspended")

    def test_cancel_unleased_drawdown_releases_reservation(self):
        app = make_app()
        result = approved_account(app)
        drw = app.credit_lines.reserve(
            "c1", result["account_id"], "10000", "装修", "M", "home_improvement")
        app.credit_lines.cancel(drw["id"])
        account = app.credit_lines.get_account(result["account_id"])
        self.assertEqual(account["reserved_cents"], 0)
        self.assertEqual(account["available_cents"], 50000_00)

    def test_merchant_refund_reverses_principal_but_is_not_repayment(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "12000")
        loan_id = loan["id"]

        # 客户先还一期（还款）
        first_payment = Store.loads(loan["schedule"])[0]["payment_cents"]
        app.credit_lines.repay(loan_id, str(Decimal(first_payment) / 100))

        # 商户全额退款 1 万：原路冲减本金
        app.credit_lines.merchant_refund(loan_id, "10000", "REF-1")
        after = app.credit_lines.get_loan(loan_id)
        self.assertEqual(after["refunded_principal_cents"], 10000_00)

        kinds = [e["kind"] for e in after["events"]]
        self.assertIn("merchant_refund", kinds)
        refund_events = [e for e in after["events"] if e["kind"] == "merchant_refund"]
        self.assertTrue(all(e["detail"]["not_a_repayment"] is True for e in refund_events))
        # 已还本金不因退款而虚增
        paid_principal_before_refund = sum(
            1 for e in after["events"] if e["kind"] == "repayment")
        self.assertEqual(paid_principal_before_refund, 1)

        # 账户在贷余额下降，但没有新的 repayment
        account = app.credit_lines.get_account(result["account_id"])
        self.assertLess(account["outstanding_cents"], 12000_00)
        repayments = [e for e in after["events"] if e["kind"] == "repayment"]
        self.assertEqual(len(repayments), 1)

    def test_full_refund_closes_loan(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "5000")
        app.credit_lines.merchant_refund(loan["id"], "5000", "REF-FULL")
        after = app.credit_lines.get_loan(loan["id"])
        self.assertEqual(after["status"], "closed")
        account = app.credit_lines.get_account(result["account_id"])
        self.assertEqual(account["outstanding_cents"], 0)

    def test_installment_cancel_also_reverses_not_repays(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "6000")
        app.credit_lines.installment_cancel(loan["id"], "3000", "CAN-9")
        after = app.credit_lines.get_loan(loan["id"])
        kinds = [e["kind"] for e in after["events"]]
        self.assertIn("installment_cancel", kinds)
        self.assertNotIn("repayment", kinds)
        self.assertEqual(after["refunded_principal_cents"], 3000_00)

    def test_repayment_all_installments_settles_loan(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "6000")
        schedule = Store.loads(loan["schedule"])
        for row in schedule:
            app.credit_lines.repay(loan["id"], str(Decimal(row["payment_cents"]) / 100))
        after = app.credit_lines.get_loan(loan["id"])
        self.assertEqual(after["status"], "settled")
        account = app.credit_lines.get_account(result["account_id"])
        self.assertEqual(account["outstanding_cents"], 0)

    def test_overpayment_rejected(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "3000")
        with self.assertRaises(DomainError) as ctx:
            app.credit_lines.repay(loan["id"], "999999")
        self.assertEqual(ctx.exception.code, "conflict")

    def test_refund_cannot_exceed_remaining_principal(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "3000")
        with self.assertRaises(DomainError) as ctx:
            app.credit_lines.merchant_refund(loan["id"], "3001", "REF-X")
        self.assertEqual(ctx.exception.code, "conflict")

    def test_schedule_is_annuity(self):
        schedule = build_schedule(12000_00, Decimal("0.072"), 12, None)
        payments = [r["payment_cents"] for r in schedule[:-1]]
        self.assertTrue(all(p == payments[0] for p in payments))  # 前 11 期等额
        principal = sum(r["principal_cents"] for r in schedule)
        self.assertEqual(principal, 12000_00)


if __name__ == "__main__":
    unittest.main()
