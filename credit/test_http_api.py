"""HTTP 端到端契约测试：真实起服务，走 JSON 接口验证关键链路与权限边界。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from credit.app import BankApp
from credit.httpapi import build_handler
from credit.testing import (PURPOSES_ALL, SCOPES_ALL, healthy_credit,
                            healthy_debt, healthy_income)


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.app = BankApp(":memory:")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, role="customer", actor="c1"):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if role:
            headers["X-Role"] = role
        if actor:
            headers["X-Actor"] = actor
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # --- 全链路 ---------------------------------------------------------

    def test_full_journey_application_to_repayment_and_refund(self):
        # 客户建档
        status, _ = self.call("POST", "/v1/customers",
                              {"customer_id": "c1", "name": "张三"})
        self.assertEqual(status, 201)
        # 授权
        status, _ = self.call("POST", "/v1/customers/c1/consents", {
            "scopes": SCOPES_ALL, "purposes": PURPOSES_ALL, "grant_ref": "G-1"})
        self.assertEqual(status, 201)
        # 三类数据读取
        for kind, payload in (("income", healthy_income()),
                              ("debt", healthy_debt()),
                              ("credit_report", healthy_credit())):
            status, _ = self.call("POST", "/v1/customers/c1/snapshots", {
                "kind": kind, "purpose": "credit_assessment",
                "source": "bureau", "as_of": "2026-08-31", "payload": payload})
            self.assertEqual(status, 201)
        # 申请
        status, app_ = self.call("POST", "/v1/applications", {
            "customer_id": "c1", "amount_yuan": "50000", "annual_rate": "0.072",
            "term_months": 12, "declared_purpose": "家居装修",
            "merchant_category": "home_improvement"})
        self.assertEqual(status, 201)
        # 评估
        status, asm = self.call("POST", f"/v1/applications/{app_['id']}/assess", {})
        self.assertEqual(status, 201)
        self.assertEqual(asm["decision"], "approved")
        self.assertEqual(asm["approved_limit_cents"], 50000_00)

        status, account = self.call(
            "GET", f"/v1/disclosure/rates?application_id={app_['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(account["annual_rate_percent"], "7.20%")

        # 取账户
        import sqlite3
        account_id = self.app.store.query_one(
            "SELECT id FROM credit_accounts WHERE customer_id='c1'")["id"]
        status, acc = self.call("GET", f"/v1/accounts/{account_id}")
        self.assertEqual(acc["available_cents"], 50000_00)

        # 提款 → 凭证 → 放款
        status, drw = self.call("POST", f"/v1/accounts/{account_id}/drawdowns", {
            "customer_id": "c1", "amount_yuan": "12000", "declared_purpose": "家居装修",
            "merchant_id": "M-1", "merchant_category": "home_improvement"})
        self.assertEqual(status, 201)
        status, drw = self.call("POST", f"/v1/drawdowns/{drw['id']}/evidence", {
            "items": [{"category": "home_improvement", "amount_cents": 12000_00}]})
        self.assertEqual(drw["status"], "ready")
        status, loan = self.call("POST", f"/v1/drawdowns/{drw['id']}/disburse", {})
        self.assertEqual(status, 201)
        loan_id = loan["id"]

        # 商户退款 5000：原路冲减
        status, loan2 = self.call("POST", f"/v1/loans/{loan_id}/reverse", {
            "amount_yuan": "5000", "reference": "R-1", "kind": "merchant_refund"},
            role="customer", actor="merchant-system")
        self.assertEqual(status, 200)
        self.assertEqual(loan2["refunded_principal_cents"], 5000_00)

        # 贷款明细中两类流水性质不同
        status, detail = self.call("GET", f"/v1/loans/{loan_id}")
        kinds = {e["kind"] for e in detail["events"]}
        self.assertIn("merchant_refund", kinds)
        self.assertNotIn("repayment", kinds)

    def test_missing_consent_returns_403_over_http(self):
        self.call("POST", "/v1/customers", {"customer_id": "c2", "name": "李四"})
        # 无任何授权直接读收入
        status, body = self.call("POST", "/v1/customers/c2/snapshots", {
            "kind": "income", "purpose": "credit_assessment",
            "source": "bank", "as_of": "2026-08-31",
            "payload": healthy_income()})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "consent_required")
        self.assertEqual(body["details"]["required_scope"], "income_read")

    def test_marketing_role_cannot_review_case_over_http(self):
        # 授信阶段宽限 → 人工案件
        self.call("POST", "/v1/customers", {"customer_id": "c3", "name": "王五"})
        self.call("POST", "/v1/customers/c3/consents", {
            "scopes": SCOPES_ALL, "purposes": PURPOSES_ALL, "grant_ref": "G3"})
        for kind, payload in (("income", healthy_income()),
                              ("debt", healthy_debt()),
                              ("credit_report", healthy_credit("grace"))):
            self.call("POST", "/v1/customers/c3/snapshots", {
                "kind": kind, "purpose": "credit_assessment",
                "source": "bureau", "as_of": "2026-08-31", "payload": payload})
        _, app_ = self.call("POST", "/v1/applications", {
            "customer_id": "c3", "amount_yuan": "50000", "annual_rate": "0.072",
            "term_months": 12, "declared_purpose": "装修",
            "merchant_category": "home_improvement"})
        _, asm = self.call("POST", f"/v1/applications/{app_['id']}/assess", {})
        self.assertEqual(asm["decision"], "manual_review")

        status, cases = self.call("GET", "/v1/review-cases?customer_id=c3",
                                  role="risk_officer", actor="risk-1")
        self.assertEqual(status, 200)
        case_id = cases["cases"][0]["id"]

        # 营销角色试图覆盖
        status, body = self.call("POST", f"/v1/review-cases/{case_id}/decide", {
            "decision": "resume", "reason_detail": "冲业绩"},
            role="marketing", actor="mkt-1")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

        # 风控角色正常处理
        status, decided = self.call("POST", f"/v1/review-cases/{case_id}/decide", {
            "decision": "reject", "reason_detail": "宽限期内不予授信"},
            role="risk_officer", actor="risk-1")
        self.assertEqual(status, 200)
        self.assertEqual(decided["decision"], "reject")

    def test_compliance_reproduce_endpoint_and_role_gate(self):
        # 复用引导：直接用领域层准备数据
        from credit.testing import approved_account
        result = approved_account(self.app, customer_id="c4")
        asm_id = result["assessment"]["id"]

        status, body = self.call(
            "GET", f"/v1/compliance/assessments/{asm_id}/reproduce",
            role="marketing", actor="mkt-1")
        self.assertEqual(status, 403)

        status, report = self.call(
            "GET", f"/v1/compliance/assessments/{asm_id}/reproduce",
            role="compliance", actor="aud-1")
        self.assertEqual(status, 200)
        self.assertTrue(report["matches"])

    def test_campaign_endpoint_enforces_consent(self):
        from credit.testing import approved_account
        approved_account(self.app, customer_id="c5", marketing=False)
        status, body = self.call("POST", "/v1/customers/c5/campaigns", {
            "channel": "sms", "traits_used": []}, role="marketing", actor="mkt-9")
        self.assertEqual(status, 403)
        # 营销尝试已留痕
        status, log = self.call("GET", "/v1/customers/c5/campaigns",
                                role="compliance", actor="aud-1")
        self.assertEqual(log["actions"][0]["allowed"], 0)

    def test_unknown_route_is_404(self):
        status, body = self.call("GET", "/v1/nope", role=None, actor=None)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_health_still_identity(self):
        with urlopen(f"{self.base}/health", timeout=2) as resp:
            payload = json.load(resp)
        self.assertEqual(payload["service"], "responsible-consumer-credit")


if __name__ == "__main__":
    unittest.main()
