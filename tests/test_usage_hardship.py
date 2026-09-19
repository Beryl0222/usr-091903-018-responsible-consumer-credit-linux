"""用途凭证核验、困难协商（展期/重组/缓还）、催收联系边界。"""

import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import approved_line, make_app, seed_customer

CUSTOMER = {"id": "cust-1", "role": "customer"}
RISK = {"id": "officer-1", "role": "risk_officer"}
COLLECTOR = {"id": "col-1", "role": "collector"}


def disburse(app, amount_yuan=20000, purpose="appliance", merchant="m-1"):
    seed_customer(app)
    approved_line(app, requested_yuan=50000)
    r = app.withdraw(
        {"customer_id": "cust-1", "amount_yuan": amount_yuan,
         "merchant": {"id": merchant, "name": "商户"}, "usage_purpose": purpose},
        CUSTOMER)
    return r["withdrawal"]["id"], r["loan_id"]


class UsageEvidenceTests(unittest.TestCase):
    def test_matching_document_verified(self):
        app = make_app()
        wid, _ = disburse(app)
        r = app.submit_document(wid, {
            "doc_type": "receipt", "claimed_amount_yuan": 20000,
            "claimed_merchant": "m-1", "claimed_purpose": "appliance"}, RISK)
        self.assertEqual(r["status"], "verified")

    def test_amount_mismatch_conflict_freezes_line_and_opens_case(self):
        app = make_app()
        wid, _ = disburse(app)
        r = app.submit_document(wid, {
            "doc_type": "invoice", "claimed_amount_yuan": 12000,
            "claimed_merchant": "m-1", "claimed_purpose": "appliance"}, RISK)
        self.assertEqual(r["status"], "conflict")
        self.assertIn("AMOUNT_MISMATCH", [c["code"] for c in r["conflicts"]])
        self.assertEqual(app.storage.get_line("cust-1")["status"], "frozen")
        cases = app.storage.list_manual_cases(status="open")
        self.assertTrue(any(c["subject_type"] == "usage_evidence" for c in cases))

    def test_purpose_mismatch_conflict(self):
        app = make_app()
        wid, _ = disburse(app, purpose="education")
        r = app.submit_document(wid, {
            "doc_type": "receipt", "claimed_amount_yuan": 20000,
            "claimed_merchant": "m-1", "claimed_purpose": "stock_trading"}, RISK)
        self.assertEqual(r["status"], "conflict")
        self.assertIn("PURPOSE_MISMATCH", [c["code"] for c in r["conflicts"]])

    def test_rejected_evidence_reduces_line_to_used_level(self):
        app = make_app()
        wid, _ = disburse(app, amount_yuan=15000)
        app.submit_document(wid, {
            "doc_type": "invoice", "claimed_amount_yuan": 3000,
            "claimed_merchant": "m-1", "claimed_purpose": "appliance"}, RISK)
        case = [c for c in app.storage.list_manual_cases(status="open")
                if c["subject_type"] == "usage_evidence"][0]
        app.decide_case(case["id"],
                        {"decision": "evidence_rejected", "rationale": "凭证系伪造，用途不实"},
                        RISK)
        line = app.storage.get_line("cust-1")
        # 可用额度被压到 0，总额度降至已用 15000
        self.assertEqual(line["available_cents"], 0)
        self.assertEqual(line["total_limit_cents"], 1500000)


def advance(app, **delta):
    app.clock.set(app.clock.now() + timedelta(**delta))


class HardshipTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.wid, self.loan_id = disburse(self.app, amount_yuan=20000)

    def test_hardship_request_pauses_collections_and_opens_case(self):
        r = self.app.request_hardship(
            self.loan_id, {"plan_type": "extension", "reason": "失业"}, CUSTOMER)
        self.assertTrue(r["collections_paused"])
        # 协商期内任何催收联系都被拦截
        decision = self.app.collection_contact(
            self.loan_id, {"channel": "phone"}, COLLECTOR)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["blocked_reason"], "HARDSHIP_ARRANGEMENT_ACTIVE")
        self.assertTrue(decision["hardship_hold"])

    def test_extension_rebuilds_schedule_with_longer_term(self):
        before = self.app.loan_view(self.loan_id, RISK)
        case = self._open_case()
        r = self.app.decide_case(case, {
            "decision": "approve",
            "plan": {"type": "extension", "new_term_months": 24,
                     "new_annual_rate": "0.072"},
            "rationale": "客户失业 2 个月，展期 24 期"}, RISK)
        self.assertEqual(r["result"]["new_term_months"], 24)
        after = self.app.loan_view(self.loan_id, RISK)
        self.assertEqual(len(after["schedule"]), 24)
        # 展期后月供下降
        self.assertLess(
            after["schedule"][0]["principal_cents"] + after["schedule"][0]["interest_cents"],
            before["schedule"][0]["principal_cents"] + before["schedule"][0]["interest_cents"])

    def test_restructure_with_lower_rate(self):
        case = self._open_case()
        r = self.app.decide_case(case, {
            "decision": "approve",
            "plan": {"type": "restructure", "new_term_months": 36,
                     "new_annual_rate": "0.036", "capitalize_interest": True},
            "rationale": "长期偿债压力，降息重组"}, RISK)
        self.assertEqual(r["result"]["new_annual_rate"], "0.036")

    def test_forbearance_defers_due_dates_without_changing_amounts(self):
        before = self.app.loan_view(self.loan_id, RISK)
        before_total = sum(s["principal_cents"] + s["interest_cents"]
                           for s in before["schedule"])
        case = self._open_case()
        r = self.app.decide_case(case, {
            "decision": "approve",
            "plan": {"type": "forbearance", "defer_months": 3,
                     "capitalize_interest": False},
            "rationale": "暂时缓还 3 个月"}, RISK)
        after = self.app.loan_view(self.loan_id, RISK)
        after_total = sum(s["principal_cents"] + s["interest_cents"]
                          for s in after["schedule"])
        self.assertEqual(after_total, before_total)
        self.assertEqual(r["result"]["new_annual_rate"], "0.072")
        # 首期到期日顺延 3 个月
        self.assertEqual(after["schedule"][0]["due_date"], "2027-01-19")

    def test_rejected_hardship_resumes_collections(self):
        case = self._open_case(reason="不合条件")
        self.app.decide_case(case, {"decision": "reject",
                                    "rationale": "无法佐证收入困难"}, RISK)
        # 推进到逾期并出宽限期，电话催收恢复
        self.app.clock.set(datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc))
        decision = self.app.collection_contact(
            self.loan_id, {"channel": "phone"}, COLLECTOR)
        self.assertTrue(decision["allowed"])

    def test_duplicate_hardship_request_rejected(self):
        from credit.errors import Conflict
        self.app.request_hardship(self.loan_id, {"plan_type": "extension"}, CUSTOMER)
        with self.assertRaises(Conflict):
            self.app.request_hardship(self.loan_id, {"plan_type": "extension"}, CUSTOMER)

    def _open_case(self, reason="收入骤降"):
        self.app.request_hardship(
            self.loan_id, {"plan_type": "extension", "reason": reason}, CUSTOMER)
        return self.app.storage.list_manual_cases(status="open")[0]["id"]


class CollectionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.wid, self.loan_id = disburse(self.app)

    def test_no_contact_when_not_overdue(self):
        self.app.clock.set(datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc))
        r = self.app.collection_contact(self.loan_id, {"channel": "sms"}, COLLECTOR)
        # 未逾期也允许系统侧登记联系（无 overdue_days），但不得施压
        self.assertEqual(r["overdue_days"], 0)

    def test_grace_period_blocks_phone_allows_one_reminder(self):
        # 首期 2026-10-19 到期；10-21 处于 3 天宽限期
        self.app.clock.set(datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc))
        phone = self.app.collection_contact(self.loan_id, {"channel": "phone"}, COLLECTOR)
        self.assertFalse(phone["allowed"])
        self.assertEqual(phone["blocked_reason"], "GRACE_PERIOD_PRESSURE_PROHIBITED")
        sms = self.app.collection_contact(self.loan_id, {"channel": "sms"}, COLLECTOR)
        self.assertTrue(sms["allowed"])
        self.assertTrue(sms["in_grace_period"])
        # 宽限期内每天至多一次提醒
        sms2 = self.app.collection_contact(self.loan_id, {"channel": "app_push"}, COLLECTOR)
        self.assertFalse(sms2["allowed"])
        self.assertEqual(sms2["blocked_reason"], "GRACE_REMINDER_ALREADY_SENT_TODAY")

    def test_contact_time_window_enforced(self):
        # 逾期 30 天但凌晨联系 -> 拒绝
        self.app.clock.set(datetime(2026, 11, 25, 6, 0, tzinfo=timezone.utc))
        r = self.app.collection_contact(self.loan_id, {"channel": "phone"}, COLLECTOR)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["blocked_reason"], "OUTSIDE_CONTACT_WINDOW")

    def test_all_contact_attempts_are_logged(self):
        self.app.clock.set(datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc))
        self.app.collection_contact(self.loan_id, {"channel": "phone"}, COLLECTOR)
        log = self.app.collection_log(self.loan_id, RISK)["contacts"]
        self.assertEqual(len(log), 1)
        # 被拦截的尝试同样留痕
        self.assertEqual(log[0]["allowed"], 0)


if __name__ == "__main__":
    unittest.main()
