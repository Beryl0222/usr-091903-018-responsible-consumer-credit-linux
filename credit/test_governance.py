"""催收边界、困难协商、营销闸门、合规复现、客户披露测试。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from credit.errors import DomainError
from credit.store import Store
from credit.testing import approved_account, draw_and_disburse, make_app

UTC = ZoneInfo("UTC")
SH = ZoneInfo("Asia/Shanghai")


def iso(y, m, d, h, minute=0, tz=SH):
    return datetime(y, m, d, h, minute, tzinfo=tz).isoformat()


class CollectionsTest(unittest.TestCase):
    def _overdue_loan(self, app):
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "6000")
        # 把第一期到期日改到过去，制造逾期
        schedule = Store.loads(loan["schedule"])
        schedule[0]["due_date"] = "2026-08-01"
        app.store._conn.execute("UPDATE loans SET schedule=? WHERE id=?",
                                (Store.dumps(schedule), loan["id"]))
        return result, loan

    def test_contact_blocked_before_8am_and_after_9pm(self):
        app = make_app()
        _, loan = self._overdue_loan(app)
        self.assertIn("outside_contact_window",
                      app.collections.can_contact("c1", loan["id"], datetime(2026, 9, 19, 7, 0, tzinfo=SH))["reasons"])
        self.assertIn("outside_contact_window",
                      app.collections.can_contact("c1", loan["id"], datetime(2026, 9, 19, 21, 5, tzinfo=SH))["reasons"])
        allowed = app.collections.can_contact(
            "c1", loan["id"], datetime(2026, 9, 19, 10, 0, tzinfo=SH))
        self.assertTrue(allowed["allowed"])

    def test_max_one_contact_per_day(self):
        app = make_app()
        _, loan = self._overdue_loan(app)
        app.collections.contact("c1", "phone", "collector-1", loan["id"],
                                at_iso=iso(2026, 9, 10, 10))
        with self.assertRaises(DomainError) as ctx:
            app.collections.contact("c1", "phone", "collector-1", loan["id"],
                                    at_iso=iso(2026, 9, 10, 15))
        self.assertIn("daily_contact_limit", ctx.exception.details["reasons"])
        # 次日可联系
        app.collections.contact("c1", "phone", "collector-1", loan["id"],
                                at_iso=iso(2026, 9, 11, 10))

    def test_max_three_contacts_per_7_days(self):
        app = make_app()
        _, loan = self._overdue_loan(app)
        for day in (10, 11, 12):
            app.collections.contact("c1", "phone", "collector-1", loan["id"],
                                    at_iso=iso(2026, 9, day, 10))
        # 第 4 次仍在滚动 7 天窗口内（9/10~9/16）
        with self.assertRaises(DomainError) as ctx:
            app.collections.contact("c1", "phone", "collector-1", loan["id"],
                                    at_iso=iso(2026, 9, 16, 10))
        self.assertIn("weekly_contact_limit", ctx.exception.details["reasons"])
        # 9/17 已超出距 9/10 的 7 天窗口
        app.collections.contact("c1", "phone", "collector-1", loan["id"],
                                at_iso=iso(2026, 9, 17, 10))

    def test_hardship_negotiation_stops_collections(self):
        app = make_app()
        _, loan = self._overdue_loan(app)
        app.hardship.submit("c1", "extension", "失业", loan_id=loan["id"])
        check = app.collections.can_contact(
            "c1", loan["id"], datetime(2026, 9, 19, 10, tzinfo=SH))
        self.assertFalse(check["allowed"])
        self.assertIn("hardship_negotiation_active", check["reasons"])
        with self.assertRaises(DomainError):
            app.collections.contact("c1", "phone", "collector-1", loan["id"],
                                    at_iso=iso(2026, 9, 19, 10))

    def test_cannot_collect_non_overdue_loan(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "6000")
        # 首期到期日在未来
        check = app.collections.can_contact(
            "c1", loan["id"], datetime(2026, 9, 19, 10, tzinfo=SH))
        self.assertIn("loan_not_overdue", check["reasons"])


class MarketingGateTest(unittest.TestCase):
    def test_no_consent_blocks_campaign_even_for_marketing_role(self):
        app = make_app()
        app.customers.create("c1", "张三")
        with self.assertRaises(DomainError) as ctx:
            app.marketing.campaign("c1", "sms", "mkt-1")
        self.assertEqual(ctx.exception.code, "forbidden")
        # 拒绝也留痕
        log = app.marketing.action_log("c1")
        self.assertEqual(log[0]["allowed"], 0)

    def test_risk_consent_does_not_imply_marketing_consent(self):
        app = make_app()
        result = approved_account(app, marketing=False)
        with self.assertRaises(DomainError):
            app.marketing.campaign("c1", "sms", "mkt-1")

    def test_sensitive_traits_blocked_even_with_marketing_consent(self):
        app = make_app()
        approved_account(app, marketing=True)
        with self.assertRaises(DomainError) as ctx:
            app.marketing.campaign("c1", "sms", "mkt-1",
                                   traits_used=["dti_ratio", "risk_level_high"])
        self.assertEqual(ctx.exception.code, "forbidden")
        self.assertEqual(ctx.exception.details["sensitive_traits"],
                         ["dti_ratio", "risk_level_high"])

    def test_consented_neutral_campaign_allowed(self):
        app = make_app()
        approved_account(app, marketing=True)
        decision = app.marketing.campaign(
            "c1", "sms", "mkt-1", traits_used=["product_interest"])
        self.assertTrue(decision["allowed"])

    def test_revoked_marketing_consent_blocks(self):
        app = make_app()
        approved_account(app, marketing=True)
        app.consents.set_marketing("c1", False)
        with self.assertRaises(DomainError):
            app.marketing.campaign("c1", "sms", "mkt-1")


class ComplianceReproduceTest(unittest.TestCase):
    def test_reproduce_matches_stored_and_builds_timeline(self):
        app = make_app()
        result = approved_account(app)
        report = app.compliance.reproduce_assessment(
            result["assessment"]["id"], "auditor-1", "compliance")
        self.assertTrue(report["matches"])
        self.assertEqual(report["rule_set_version"], "v1.0")
        types_ = {e["type"] for e in report["timeline"]}
        self.assertIn("data_read", types_)
        self.assertIn("assessment", types_)

    def test_non_compliance_role_cannot_reproduce(self):
        app = make_app()
        result = approved_account(app)
        with self.assertRaises(DomainError) as ctx:
            app.compliance.reproduce_assessment(
                result["assessment"]["id"], "mkt-1", "marketing")
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_reproduce_detects_tampering(self):
        app = make_app()
        result = approved_account(app)
        asm_id = result["assessment"]["id"]
        # 篡改已落库结论
        app.store._conn.execute(
            "UPDATE assessments SET decision='approved' WHERE id=? "
            "AND decision='manual_review'", (asm_id,))
        # 该用例本身是 approved，改为另造一个 manual_review 场景更直接：
        # 这里直接篡改触发原因
        app.store._conn.execute(
            "UPDATE assessments SET triggered_reasons=? WHERE id=?",
            (Store.dumps(["tampered_reason"]), asm_id))
        report = app.compliance.reproduce_assessment(asm_id, "auditor-1", "compliance")
        self.assertFalse(report["matches"])


class DisclosureTest(unittest.TestCase):
    def test_rate_card_shows_apr_and_total_cost(self):
        app = make_app()
        result = approved_account(app)
        _, loan = draw_and_disburse(app, "c1", result["account_id"], "12000")
        card = app.disclosure.rate_card(account_id=result["account_id"])
        self.assertEqual(card["annual_rate_percent"], "7.20%")
        loan_card = card["loans"][0]
        # 总成本 = 本金 + 利息，利息为正且数字透明
        self.assertGreater(float(loan_card["total_interest_yuan"]), 0)
        self.assertEqual(
            round(float(loan_card["total_repayment_yuan"]), 2),
            round(float(loan_card["principal_yuan"])
                  + float(loan_card["total_interest_yuan"]), 2))

    def test_limit_change_reasons_are_plain_language(self):
        app = make_app()
        result = approved_account(app)
        reasons = app.disclosure.limit_change_reasons(result["account_id"])
        self.assertEqual(reasons[0]["reason_code"], "initial_assessment")
        self.assertIn("授权", reasons[0]["reason_in_plain_language"])

    def test_decision_explanation_lists_data_as_of(self):
        app = make_app()
        result = approved_account(app)
        explanation = app.disclosure.decision_explanation(result["assessment"]["id"])
        self.assertEqual(explanation["decision"], "approved")
        self.assertEqual(explanation["data_as_of"]["income"]["as_of"], "2026-08-31")


if __name__ == "__main__":
    unittest.main()
