"""HTTP API：JSON 接口，按领域服务路由。

身份约定（头）：
- X-Actor  操作人标识；客户自助操作传客户编号即可
- X-Role   员工岗位：risk_officer/admin/compliance/marketing/collector/customer
服务端对敏感动作强制校验岗位，不信任调用方自我声明的业务结论。
"""

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from credit import clock
from credit.app import BankApp
from credit.errors import DomainError, forbidden
from credit.money import cents_to_yuan


def _public_loan(loan: dict) -> dict:
    loan = dict(loan)
    loan["principal_yuan"] = cents_to_yuan(loan["principal_cents"])
    return loan


def build_handler(app: BankApp):
    def _staff(h, *roles) -> str:
        role = h.headers.get("X-Role") or "customer"
        if role not in roles:
            raise forbidden(f"该操作需要岗位: {', '.join(roles)}", {"actual_role": role})
        return role

    def _actor(h, data) -> str:
        return h.headers.get("X-Actor") or data.get("actor") or "anonymous"

    # --- 端点处理函数 ---------------------------------------------------

    def customer_create(h, data, **_):
        h._send(201, app.customers.create(data["customer_id"], data["name"]))

    def customer_get(h, data, customer_id, **_):
        h._send(200, app.customers.get(customer_id))

    def consent_grant(h, data, customer_id, **_):
        row = app.consents.grant(
            customer_id, data["scopes"], data["purposes"], data["grant_ref"],
            int(data.get("ttl_days", 180)))
        h._send(201, row)

    def consent_revoke(h, data, consent_id, **_):
        h._send(200, app.consents.revoke(consent_id))

    def marketing_consent(h, data, customer_id, **_):
        h._send(200, app.consents.set_marketing(
            customer_id, bool(data.get("granted", False)), data.get("channels")))

    def snapshot_ingest(h, data, customer_id, **_):
        row = app.consents.record_read(
            customer_id, data["kind"], data["purpose"], data["source"],
            data["as_of"], data["payload"])
        h._send(201, app.store.row_to_dict(row))

    def application_create(h, data, **_):
        row = app.underwriting.submit_application(
            data["customer_id"], data["amount_yuan"], str(data["annual_rate"]),
            int(data["term_months"]), data["declared_purpose"],
            data.get("merchant_category"))
        h._send(201, app.store.row_to_dict(row))

    def assess(h, data, application_id, **_):
        h._send(201, app.underwriting.assess(
            application_id, data.get("rule_set_version", "v1.0")))

    def assessment_get(h, data, assessment_id, **_):
        h._send(200, app.underwriting.get_assessment(assessment_id))

    def account_get(h, data, account_id, **_):
        h._send(200, app.credit_lines.get_account(account_id))

    def limit_history(h, data, account_id, **_):
        h._send(200, {"events": app.credit_lines.limit_history(account_id)})

    def drawdown_reserve(h, data, account_id, **_):
        row = app.credit_lines.reserve(
            data["customer_id"], account_id, data["amount_yuan"],
            data["declared_purpose"], data.get("merchant_id"),
            data.get("merchant_category"))
        h._send(201, row)

    def evidence_submit(h, data, drawdown_id, **_):
        h._send(200, app.credit_lines.submit_evidence(drawdown_id, data))

    def disburse(h, data, drawdown_id, **_):
        h._send(201, _public_loan(app.credit_lines.disburse(drawdown_id)))

    def drawdown_cancel(h, data, drawdown_id, **_):
        h._send(200, app.credit_lines.cancel(
            drawdown_id, _actor(h, data), data.get("reason", "customer_cancel")))

    def loan_get(h, data, loan_id, **_):
        h._send(200, _public_loan(app.credit_lines.get_loan(loan_id)))

    def repay(h, data, loan_id, **_):
        h._send(200, _public_loan(app.credit_lines.repay(
            loan_id, data["amount_yuan"], _actor(h, data))))

    def reverse(h, data, loan_id, **_):
        kind = data.get("kind", "merchant_refund")
        if kind == "installment_cancel":
            row = app.credit_lines.installment_cancel(
                loan_id, data["amount_yuan"], data["reference"], _actor(h, data))
        else:
            row = app.credit_lines.merchant_refund(
                loan_id, data["amount_yuan"], data["reference"], _actor(h, data))
        h._send(200, _public_loan(row))

    def monitor_income(h, data, customer_id, **_):
        h._send(201, app.monitoring.ingest_income(
            customer_id, data["source"], data["as_of"], data["payload"]))

    def monitor_debt(h, data, customer_id, **_):
        h._send(201, app.monitoring.ingest_debt(
            customer_id, data["source"], data["as_of"], data["payload"]))

    def case_get(h, data, case_id, **_):
        h._send(200, app.reviews.get_case(case_id))

    def cases_open(h, data, **_):
        _staff(h, "risk_officer", "admin", "compliance")
        qs = parse_qs(urlparse(h.path).query)
        customer_id = qs["customer_id"][0] if qs.get("customer_id") else None
        h._send(200, {"cases": app.reviews.list_open_cases(customer_id)})

    def case_decide(h, data, case_id, **_):
        role = _staff(h, "risk_officer", "admin")
        h._send(200, app.reviews.decide(
            case_id, _actor(h, data), role, data["decision"], data["reason_detail"],
            data.get("new_limit_yuan"), data.get("new_term_months"),
            data.get("new_annual_rate"), data.get("pending_drawdowns", "reject")))

    def hardship_submit(h, data, customer_id, **_):
        row = app.hardship.submit(
            customer_id, data["requested_action"], data["description"],
            data.get("evidence"), data.get("loan_id"), data.get("account_id"))
        h._send(201, row)

    def hardship_list(h, data, customer_id, **_):
        h._send(200, {"requests": app.hardship.list_for_customer(customer_id)})

    def collection_check(h, data, customer_id, **_):
        _staff(h, "risk_officer", "admin", "compliance", "collector")
        at = clock.parse(data["at"]) if data.get("at") else None
        h._send(200, app.collections.can_contact(customer_id, data.get("loan_id"), at))

    def collection_contact(h, data, customer_id, **_):
        _staff(h, "risk_officer", "admin", "collector")
        row = app.collections.contact(
            customer_id, data["channel"], _actor(h, data), data.get("loan_id"),
            data.get("at_iso"), data.get("result"), data.get("note"))
        h._send(201, row)

    def collection_history(h, data, customer_id, **_):
        _staff(h, "risk_officer", "admin", "compliance", "collector")
        h._send(200, {"contacts": app.collections.history(customer_id)})

    def reproduce(h, data, assessment_id, **_):
        role = _staff(h, "compliance", "admin")
        h._send(200, app.compliance.reproduce_assessment(
            assessment_id, h.headers.get("X-Actor", "anonymous"), role))

    def boundary_report(h, data, customer_id, **_):
        role = _staff(h, "compliance", "admin")
        h._send(200, app.compliance.collection_boundary_report(
            customer_id, h.headers.get("X-Actor", "anonymous"), role))

    def consent_usage(h, data, customer_id, **_):
        role = _staff(h, "compliance", "admin")
        h._send(200, app.compliance.consent_usage_report(
            customer_id, h.headers.get("X-Actor", "anonymous"), role))

    def campaign(h, data, customer_id, **_):
        _staff(h, "marketing", "admin")
        h._send(200, app.marketing.campaign(
            customer_id, data["channel"], _actor(h, data), data.get("traits_used")))

    def campaign_log(h, data, customer_id, **_):
        _staff(h, "compliance", "admin", "marketing")
        h._send(200, {"actions": app.marketing.action_log(customer_id)})

    def rate_card(h, data, **_):
        qs = parse_qs(urlparse(h.path).query)
        if qs.get("account_id"):
            h._send(200, app.disclosure.rate_card(account_id=qs["account_id"][0]))
        elif qs.get("application_id"):
            h._send(200, app.disclosure.rate_card(application_id=qs["application_id"][0]))
        raise DomainError("invalid_request", "需要 account_id 或 application_id", 400)

    def decision_explanation(h, data, assessment_id, **_):
        h._send(200, app.disclosure.decision_explanation(assessment_id))

    def limit_reasons(h, data, account_id, **_):
        h._send(200, {"changes": app.disclosure.limit_change_reasons(account_id)})

    CID = r"(?P<customer_id>[A-Za-z0-9_\-]+)"

    # (正则, {HTTP 方法: 处理函数})；命名组 customer_id/account_id/... 注入
    routes = [
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)", {"GET": customer_get}),
        (r"/v1/customers", {"POST": customer_create}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/consents", {"POST": consent_grant}),
        (rf"/v1/consents/(?P<consent_id>[A-Za-z0-9_\-]+)", {"DELETE": consent_revoke}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/marketing-consent",
         {"POST": marketing_consent}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/snapshots", {"POST": snapshot_ingest}),
        (r"/v1/applications", {"POST": application_create}),
        (rf"/v1/applications/(?P<application_id>[A-Za-z0-9_\-]+)/assess", {"POST": assess}),
        (rf"/v1/assessments/(?P<assessment_id>[A-Za-z0-9_\-]+)", {"GET": assessment_get}),
        (rf"/v1/assessments/(?P<assessment_id>[A-Za-z0-9_\-]+)/explanation",
         {"GET": decision_explanation}),
        (rf"/v1/accounts/(?P<account_id>[A-Za-z0-9_\-]+)", {"GET": account_get}),
        (rf"/v1/accounts/(?P<account_id>[A-Za-z0-9_\-]+)/limit-history", {"GET": limit_history}),
        (rf"/v1/accounts/(?P<account_id>[A-Za-z0-9_\-]+)/limit-reasons", {"GET": limit_reasons}),
        (rf"/v1/accounts/(?P<account_id>[A-Za-z0-9_\-]+)/drawdowns", {"POST": drawdown_reserve}),
        (rf"/v1/drawdowns/(?P<drawdown_id>[A-Za-z0-9_\-]+)/evidence", {"POST": evidence_submit}),
        (rf"/v1/drawdowns/(?P<drawdown_id>[A-Za-z0-9_\-]+)/disburse", {"POST": disburse}),
        (rf"/v1/drawdowns/(?P<drawdown_id>[A-Za-z0-9_\-]+)/cancel", {"POST": drawdown_cancel}),
        (rf"/v1/loans/(?P<loan_id>[A-Za-z0-9_\-]+)", {"GET": loan_get}),
        (rf"/v1/loans/(?P<loan_id>[A-Za-z0-9_\-]+)/repay", {"POST": repay}),
        (rf"/v1/loans/(?P<loan_id>[A-Za-z0-9_\-]+)/reverse", {"POST": reverse}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/monitoring/income",
         {"POST": monitor_income}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/monitoring/debt",
         {"POST": monitor_debt}),
        (r"/v1/review-cases", {"GET": cases_open}),
        (rf"/v1/review-cases/(?P<case_id>[A-Za-z0-9_\-]+)", {"GET": case_get}),
        (rf"/v1/review-cases/(?P<case_id>[A-Za-z0-9_\-]+)/decide", {"POST": case_decide}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/hardship",
         {"POST": hardship_submit, "GET": hardship_list}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/collections/check",
         {"POST": collection_check}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/collections",
         {"POST": collection_contact, "GET": collection_history}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/collection-boundary",
         {"GET": boundary_report}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/consent-usage", {"GET": consent_usage}),
        (rf"/v1/customers/(?P<customer_id>[A-Za-z0-9_\-]+)/campaigns",
         {"POST": campaign, "GET": campaign_log}),
        (rf"/v1/compliance/assessments/(?P<assessment_id>[A-Za-z0-9_\-]+)/reproduce",
         {"GET": reproduce}),
        (r"/v1/disclosure/rates", {"GET": rate_card}),
    ]

    class Handler(BaseHTTPRequestHandler):
        server_version = "RCC/1.0"

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise DomainError("invalid_request", "请求体不是合法 JSON", 400)
            if not isinstance(data, dict):
                raise DomainError("invalid_request", "请求体必须是 JSON 对象", 400)
            return data

        def _handle(self, method: str):
            try:
                self._dispatch(method)
            except DomainError as exc:
                self._send(exc.http_status, exc.to_dict())
            except (KeyError, ValueError, TypeError) as exc:
                self._send(400, {"error": "invalid_request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 - 兜底，避免堆栈泄漏
                self._send(500, {"error": "internal_error", "message": str(exc)})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

        def _dispatch(self, method: str):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                return self._send(200, {
                    "status": "ok", "service": "responsible-consumer-credit",
                    "name": "消费贷审慎额度管理"})
            for pattern, handlers in routes:
                match = re.fullmatch(pattern, path)
                if match and method in handlers:
                    data = self._read_json() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
                    return handlers[method](self, data, **match.groupdict())
            self._send(404, {"error": "not_found",
                             "message": f"无此路由: {method} {path}"})

        def log_message(self, *_args):
            return

    return Handler
