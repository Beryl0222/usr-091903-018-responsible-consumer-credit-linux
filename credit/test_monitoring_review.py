"""贷后监控、人工复核决定、营销隔离测试。"""

import unittest

from credit.errors import DomainError
from credit.store import Store
from credit.testing import approved_account, draw_and_disburse, make_app


class MonitoringAndReviewTest(unittest.TestCase):
    def _active_account_with_pending(self, amount="50000", draw="10000"):
        app = make_app()
        result = approved_account(app, amount_yuan=amount)
        account_id = result["account_id"]
        # 一笔已放款
        draw_and_disburse(app, "c1", account_id, draw)
        # 一笔已预留未放款
        pending = app.credit_lines.reserve(
            "c1", account_id, "8000", "装修", "M2", "home_improvement")
        return app, result, pending

    def test_income_drop_suspends_account_and_pending_drawdowns(self):
        app, result, pending = self._active_account_with_pending()
        # 最近月收入从 20000 降到 12000（降幅 40% > 30%）
        app.monitoring.ingest_income("c1", "payroll_bank", "2026-09-15", {
            "monthly_income_cents": 12000_00,
            "employment_status": "employed",
            "months": [
                {"month": "2026-06", "income_cents": 20000_00},
                {"month": "2026-07", "income_cents": 20000_00},
                {"month": "2026-08", "income_cents": 20000_00},
                {"month": "2026-09", "income_cents": 12000_00},
            ],
        })
        account = app.credit_lines.get_account(result["account_id"])
        self.assertEqual(account["status"], "suspended")
        self.assertEqual(app.store.require("drawdowns", pending["id"])["status"], "suspended")
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE customer_id='c1' AND topic='monitoring' "
            "AND status='open' ORDER BY created_at DESC LIMIT 1")
        self.assertIsNotNone(case)
        # 暂停的提款不能直接放款
        with self.assertRaises(DomainError):
            app.credit_lines.disburse(pending["id"])

    def test_income_monitoring_requires_monitoring_consent(self):
        app = make_app()
        app.customers.create("c9", "李四")
        app.consents.grant("c9", ["income_read"], ["credit_assessment"], "G9")
        with self.assertRaises(DomainError) as ctx:
            app.monitoring.ingest_income("c9", "bank", "2026-09-01",
                                         {"monthly_income_cents": 100})
        self.assertEqual(ctx.exception.code, "consent_required")

    def test_suspected_rollover_opens_case(self):
        app, _, _ = self._active_account_with_pending()
        app.monitoring.ingest_debt("c1", "credit_bureau", "2026-09-15", {
            "monthly_obligation_cents": 9000_00,
            "facilities": [
                {"institution": "机构A", "opened_days_ago": 10, "monthly_payment_cents": 3000_00},
                {"institution": "机构B", "opened_days_ago": 20, "monthly_payment_cents": 4000_00},
            ],
        })
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE topic='monitoring' AND status='open'")
        self.assertIsNotNone(case)
        signal = app.store.query_one("SELECT * FROM signal_events WHERE code='suspected_rollover'")
        self.assertIsNotNone(signal)

    def test_marketing_role_cannot_decide_risk_case(self):
        app, _, pending = self._active_account_with_pending()
        app.monitoring.ingest_income("c1", "payroll_bank", "2026-09-15", {
            "monthly_income_cents": 12000_00, "employment_status": "employed",
            "months": [
                {"month": "2026-06", "income_cents": 20000_00},
                {"month": "2026-07", "income_cents": 20000_00},
                {"month": "2026-08", "income_cents": 20000_00},
                {"month": "2026-09", "income_cents": 12000_00},
            ]})
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE topic='monitoring' AND status='open'")
        with self.assertRaises(DomainError) as ctx:
            app.reviews.decide(case["id"], "mkt-zhang", "marketing", "resume",
                               "营销冲量要求恢复")
        self.assertEqual(ctx.exception.code, "forbidden")
        self.assertEqual(ctx.exception.http_status, 403)
        # 案件仍未决定
        self.assertEqual(app.store.require("review_cases", case["id"])["status"], "open")

    def test_officer_limit_reduction_cancels_pending_and_records_reason(self):
        app, result, pending = self._active_account_with_pending()
        app.monitoring.ingest_income("c1", "payroll_bank", "2026-09-15", {
            "monthly_income_cents": 12000_00, "employment_status": "employed",
            "months": [
                {"month": "2026-06", "income_cents": 20000_00},
                {"month": "2026-07", "income_cents": 20000_00},
                {"month": "2026-08", "income_cents": 20000_00},
                {"month": "2026-09", "income_cents": 12000_00},
            ]})
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE topic='monitoring' AND status='open'")
        decided = app.reviews.decide(
            case["id"], "risk-li", "risk_officer", "limit_reduction",
            "客户收入骤降40%，下调额度控制风险", new_limit_yuan="20000")
        self.assertEqual(decided["decision"], "limit_reduction")
        account = app.credit_lines.get_account(result["account_id"])
        self.assertEqual(account["limit_cents"], 20000_00)
        # 未放款提款已驳回并释放预留；在贷 1 万，额度 2 万，可用 1 万
        self.assertEqual(account["reserved_cents"], 0)
        self.assertEqual(account["outstanding_cents"], 10000_00)
        self.assertEqual(account["available_cents"], 10000_00)
        self.assertEqual(app.store.require("drawdowns", pending["id"])["status"], "cancelled")
        # 额度变化原因留痕
        events = app.credit_lines.limit_history(result["account_id"])
        self.assertEqual(events[-1]["reason_code"], "manual_limit_reduction")
        self.assertIn("收入骤降", events[-1]["reason_detail"])

    def test_limit_cannot_be_raised_manually(self):
        app, result, _ = self._active_account_with_pending()
        # 直接构造一个 open 监控案件
        app.monitoring.ingest_income("c1", "payroll_bank", "2026-09-15", {
            "monthly_income_cents": 12000_00, "employment_status": "employed",
            "months": [
                {"month": "2026-06", "income_cents": 20000_00},
                {"month": "2026-07", "income_cents": 20000_00},
                {"month": "2026-08", "income_cents": 20000_00},
                {"month": "2026-09", "income_cents": 12000_00},
            ]})
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE topic='monitoring' AND status='open'")
        with self.assertRaises(DomainError) as ctx:
            app.reviews.decide(case["id"], "risk-li", "risk_officer",
                               "limit_reduction", "试提高", new_limit_yuan="99999")
        self.assertIn("降额只能低于", ctx.exception.message)

    def test_officer_resume_reopens_account_and_monitoring_pending(self):
        app, result, pending = self._active_account_with_pending()
        app.monitoring.ingest_income("c1", "payroll_bank", "2026-09-15", {
            "monthly_income_cents": 12000_00, "employment_status": "employed",
            "months": [
                {"month": "2026-06", "income_cents": 20000_00},
                {"month": "2026-07", "income_cents": 20000_00},
                {"month": "2026-08", "income_cents": 20000_00},
                {"month": "2026-09", "income_cents": 12000_00},
            ]})
        case = app.store.query_one(
            "SELECT * FROM review_cases WHERE topic='monitoring' AND status='open'")
        app.reviews.decide(case["id"], "risk-li", "risk_officer", "resume",
                           "补充收入证明后确认收入恢复")
        account = app.credit_lines.get_account(result["account_id"])
        self.assertEqual(account["status"], "active")
        self.assertEqual(app.store.require("drawdowns", pending["id"])["status"], "evidence_pending")

    def test_extension_rebuilds_schedule_with_lower_payment(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "12000")
        # 构造困难案件关联该贷款
        app.hardship.submit("c1", "extension", "失业三个月", loan_id=loan["id"],
                            account_id=result["account_id"])
        case = app.store.query_one("SELECT * FROM review_cases WHERE topic='hardship'")
        before = Store.loads(loan["schedule"])[0]["payment_cents"]
        app.reviews.decide(case["id"], "risk-wang", "risk_officer", "extension",
                           "客户失业，同意展期至 24 期", new_term_months=24)
        after = app.credit_lines.get_loan(loan["id"])
        self.assertEqual(after["status"], "extended")
        self.assertEqual(after["term_months"], 24)
        self.assertLess(after["schedule"][-1]["payment_cents"], before)


if __name__ == "__main__":
    unittest.main()
