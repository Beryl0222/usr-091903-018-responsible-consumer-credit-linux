"""端到端 HTTP 集成：角色化访问控制、营销隔离、越权防护、完整业务链。"""

import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from credit.api import App, create_server
from credit.clock import Clock
from credit.storage import Storage
from tests.helpers import RISK_SCOPES


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc))
        self.app = App(Storage(), self.clock)
        self.server = create_server("127.0.0.1", 0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, role=None, actor_id=None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Actor-Role"] = role
            headers["X-Actor-Id"] = actor_id or role
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as e:
            payload = json.loads(e.read().decode())
            return e.code, payload

    def seed(self, customer_id="cust-1"):
        now = self.clock.now().isoformat()
        self.call("POST", "/v1/consents/grant",
                  {"customer_id": customer_id, "scopes": RISK_SCOPES,
                   "purpose": "affordability_assessment"}, role="customer",
                  actor_id=customer_id)
        for kind, payload in (
            ("income", {"monthly_income_cents": 2000000, "status": "employed"}),
            ("credit_report", {"in_grace_period": False, "has_overdue": False,
                               "max_overdue_days": 0}),
            ("debt_snapshot", {"monthly_obligation_cents": 300000,
                               "institution_count": 1,
                               "total_outstanding_cents": 5000000,
                               "new_short_term_debt_cents_since_assessment": 0}),
        ):
            status, _ = self.call("POST", "/v1/snapshots", {
                "customer_id": customer_id, "kind": kind, "source": "pboc",
                "as_of": "2026-09-10", "payload": payload}, role="system")
            assert status == 200


class RoleBoundaryTests(ApiTestBase):
    def test_marketing_role_cannot_touch_credit_or_risk_data(self):
        self.seed()
        for method, path in (
            ("GET", "/v1/customers/cust-1/line"),
            ("POST", "/v1/applications"),
            ("GET", "/v1/cases"),
        ):
            body = {"customer_id": "cust-1", "requested_yuan": 1} if method == "POST" else None
            status, payload = self.call(method, path, body, role="marketing",
                                        actor_id="mkt-bot")
            self.assertEqual(status, 403, (path, payload))
            self.assertEqual(payload["error"], "marketing_scope_denied")

    def test_marketing_can_only_check_marketing_eligibility(self):
        self.seed()
        status, payload = self.call(
            "GET", "/v1/marketing/customers/cust-1/eligibility", role="marketing")
        self.assertEqual(status, 200)
        self.assertFalse(payload["eligible"])  # 无营销授权
        self.assertFalse(payload["risk_data_used"])

    def test_compliance_is_read_only(self):
        self.seed()
        status, payload = self.call(
            "POST", "/v1/cases/x/decide", {"decision": "disburse", "rationale": "x"},
            role="compliance")
        self.assertEqual(status, 403)

    def test_collector_cannot_view_line_or_decide_case(self):
        self.seed()
        self.assertEqual(self.call("GET", "/v1/customers/cust-1/line", role="collector")[0], 403)
        self.assertEqual(
            self.call("POST", "/v1/cases/x/decide",
                      {"decision": "disburse", "rationale": "x"}, role="collector")[0],
            403)

    def test_customer_cannot_apply_for_another_customer(self):
        self.seed("cust-1")
        status, payload = self.call(
            "POST", "/v1/applications",
            {"customer_id": "cust-2", "requested_yuan": 1000},
            role="customer", actor_id="cust-1")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "ownership_denied")

    def test_customer_cannot_access_another_customers_resources_by_path(self):
        self.seed("cust-1")
        # cust-1 有自己的额度
        _, app_res = self.call("POST", "/v1/applications",
                               {"customer_id": "cust-1", "requested_yuan": 50000},
                               role="customer", actor_id="cust-1")
        self.call("POST", f"/v1/applications/{app_res['application_id']}/offer", {},
                  role="system")
        _, w = self.call("POST", "/v1/withdrawals",
                         {"customer_id": "cust-1", "amount_yuan": 5000},
                         role="customer", actor_id="cust-1")
        loan_id, wid = w["loan_id"], w["withdrawal"]["id"]

        # cust-2 尝试通过路径 ID 访问 cust-1 的额度/提款/贷款
        self.assertEqual(
            self.call("GET", "/v1/customers/cust-1/line",
                      role="customer", actor_id="cust-2")[0], 403)
        self.assertEqual(
            self.call("GET", f"/v1/withdrawals/{wid}",
                      role="customer", actor_id="cust-2")[0], 403)
        status, payload = self.call("POST", f"/v1/loans/{loan_id}/repay",
                                    {"amount_yuan": 100},
                                    role="customer", actor_id="cust-2")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "ownership_denied")
        # 访问不存在的他人资源返回 404 而非 500
        self.assertEqual(
            self.call("GET", "/v1/withdrawals/nonexistent",
                      role="customer", actor_id="cust-2")[0], 404)

    def test_risk_decision_requires_rationale(self):
        self.seed()
        _, app_res = self.call("POST", "/v1/applications",
                               {"customer_id": "cust-1", "requested_yuan": 50000},
                               role="customer", actor_id="cust-1")
        self.call("POST", f"/v1/applications/{app_res['application_id']}/offer", {},
                  role="system")
        self.call("POST", "/v1/snapshots", {
            "customer_id": "cust-1", "kind": "income", "source": "payroll",
            "as_of": "2026-09-20",
            "payload": {"monthly_income_cents": 900000, "status": "employed"}},
            role="system")
        self.call("POST", "/v1/withdrawals",
                  {"customer_id": "cust-1", "amount_yuan": 10000},
                  role="customer", actor_id="cust-1")
        _, cases = self.call("GET", "/v1/cases?status=open", role="risk_officer")
        case_id = cases["cases"][0]["id"]
        status, payload = self.call(
            "POST", f"/v1/cases/{case_id}/decide",
            {"decision": "disburse", "rationale": "  "}, role="risk_officer")
        self.assertEqual(status, 422)
        self.assertIn("理由", payload["message"])


class EndToEndHttpTests(ApiTestBase):
    def test_full_journey_apply_withdraw_refund_repay_replay(self):
        self.seed()
        # 申请
        status, res = self.call(
            "POST", "/v1/applications",
            {"customer_id": "cust-1", "requested_yuan": 50000},
            role="customer", actor_id="cust-1")
        self.assertEqual(status, 200)
        aid = res["application_id"]
        self.assertEqual(res["assessment"]["decision"], "approved")

        # 合规复现
        status, replay = self.call("GET", f"/v1/applications/{aid}/replay",
                                   role="compliance")
        self.assertEqual(status, 200)
        self.assertTrue(replay["reproducible"])

        # 授信
        status, line = self.call("POST", f"/v1/applications/{aid}/offer", {},
                                 role="system")
        self.assertEqual(status, 200)

        # 提款
        status, w = self.call("POST", "/v1/withdrawals", {
            "customer_id": "cust-1", "amount_yuan": 20000,
            "merchant": {"id": "m-1", "name": "商户甲"},
            "usage_purpose": "appliance"}, role="customer", actor_id="cust-1")
        self.assertEqual(status, 200)
        self.assertTrue(w["disbursed"])
        loan_id = w["loan_id"]
        self.assertEqual(w["cost_disclosure"]["annual_rate"], "0.072")

        # 客户查看自己的额度变化原因
        status, view = self.call("GET", "/v1/customers/cust-1/line",
                                 role="customer", actor_id="cust-1")
        self.assertEqual(status, 200)
        self.assertEqual(view["change_history"][0]["reason_code"], "INITIAL_AFFORDABILITY")

        # 商户退款（系统角色代表商户结算回调）
        status, refund = self.call(
            "POST", f"/v1/withdrawals/{w['withdrawal']['id']}/refund",
            {"amount_yuan": 8000, "route": "merchant_settlement"}, role="system")
        self.assertEqual(status, 200)
        self.assertFalse(refund["treated_as_repayment"])

        # 台账可供合规追溯
        status, ledger = self.call(f"GET", f"/v1/loans/{loan_id}/ledger",
                                   role="compliance")
        self.assertEqual(status, 200)
        kinds = [e["kind"] for e in ledger["entries"]]
        self.assertEqual(kinds, ["disbursement", "merchant_refund"])

    def test_income_shock_holds_withdrawal_and_officer_decides(self):
        self.seed()
        _, app_res = self.call("POST", "/v1/applications",
                               {"customer_id": "cust-1", "requested_yuan": 50000},
                               role="customer", actor_id="cust-1")
        self.call("POST", f"/v1/applications/{app_res['application_id']}/offer", {},
                  role="system")
        # 收入骤降快照
        self.call("POST", "/v1/snapshots", {
            "customer_id": "cust-1", "kind": "income", "source": "payroll",
            "as_of": "2026-09-20",
            "payload": {"monthly_income_cents": 900000, "status": "employed"}},
            role="system")
        status, hold = self.call("POST", "/v1/withdrawals", {
            "customer_id": "cust-1", "amount_yuan": 10000},
            role="customer", actor_id="cust-1")
        self.assertEqual(status, 409)
        self.assertEqual(hold["error"], "risk_held")
        self.assertIn("INCOME_DROP", hold["details"]["triggers"])

        # 风控查看待办案件并降额决定
        _, cases = self.call("GET", "/v1/cases?status=open", role="risk_officer")
        case_id = cases["cases"][0]["id"]
        status, decision = self.call("POST", f"/v1/cases/{case_id}/decide", {
            "decision": "reduce_and_disburse", "reduced_amount_yuan": 4000,
            "rationale": "按骤降后收入重算，降额至 4000"}, role="risk_officer")
        self.assertEqual(status, 200)
        self.assertEqual(decision["result"]["disbursed_cents"], 400000)


if __name__ == "__main__":
    unittest.main()
