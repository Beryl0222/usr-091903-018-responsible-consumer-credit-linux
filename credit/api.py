"""HTTP API：JSON over stdlib，角色化访问控制。

身份与角色经请求头传递（生产部署应由网关鉴权后写入）：
  X-Actor-Id、X-Actor-Role ∈ customer / risk_officer / compliance / marketing / collector / system

边界：
- marketing 角色只能访问营销准入接口，任何风控/授信/快照接口一律 403；
- 风控人工决定只有 risk_officer 可执行，且必须带 rationale；
- compliance 只读：复现决策、审计快照访问、台账与催收联系记录；
- customer 只能操作本人（X-Actor-Id 须与路径客户一致）。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import consent as consent_service
from . import workflow
from .clock import Clock
from .errors import DomainError, NotFound
from .storage import Storage
from .util import cents_to_yuan, yuan_to_cents

ROLE_CUSTOMER = "customer"
ROLE_RISK = "risk_officer"
ROLE_COMPLIANCE = "compliance"
ROLE_MARKETING = "marketing"
ROLE_COLLECTOR = "collector"
ROLE_SYSTEM = "system"

SERVICE_ID = "responsible-consumer-credit"
SERVICE_NAME = "消费贷审慎额度管理"
API_VERSION = "v1"
ALL_ROLES = {
    ROLE_CUSTOMER, ROLE_RISK, ROLE_COMPLIANCE,
    ROLE_MARKETING, ROLE_COLLECTOR, ROLE_SYSTEM,
}


class App:
    def __init__(self, storage=None, clock=None):
        self.storage = storage or Storage()
        self.clock = clock or Clock()

    # ---------- 授权与快照 ----------
    def grant_consent(self, body, actor):
        consent_service.grant(
            self.storage, body["customer_id"], body["scopes"],
            body.get("purpose", "customer_managed"), self.clock.now().isoformat(),
            expires_at=body.get("expires_at"),
        )
        return {"granted": body["scopes"]}

    def revoke_consent(self, body, actor):
        consent_service.revoke(
            self.storage, body["customer_id"], body["scope"], self.clock.now().isoformat()
        )
        return {"revoked": body["scope"]}

    def ingest_snapshot(self, body, actor):
        sid = consent_service.ingest_snapshot(
            self.storage, body["customer_id"], body["kind"], body["source"],
            body["as_of"], body["payload"], self.clock.now().isoformat(),
        )
        return {"snapshot_id": sid, "as_of": body["as_of"]}

    # ---------- 申请/授信 ----------
    def apply(self, body, actor):
        return workflow.apply_for_credit(
            self.storage, self.clock, body["customer_id"], body["requested_yuan"],
            product=body.get("product", "consumer_installment"),
            term_months=body.get("term_months"),
            annual_rate=body.get("annual_rate"),
            actor=actor["id"],
        )

    def offer(self, application_id, body, actor):
        line = workflow.offer_line(self.storage, self.clock, application_id)
        return {"line_id": line["id"],
                "total_limit_yuan": cents_to_yuan(line["total_limit_cents"]),
                "available_yuan": cents_to_yuan(line["available_cents"])}

    def assessment(self, application_id, actor):
        row = self.storage.get_assessment_by_application(application_id)
        if row is None:
            raise NotFound("评估不存在")
        return {
            "application_id": application_id,
            "rule_version": row["rule_version"],
            "decision": row["decision"],
            "max_amount_yuan": cents_to_yuan(row["max_amount_cents"]),
            "reasons": json.loads(row["reasons"]),
            "inputs": json.loads(row["inputs"]),
            "created_at": row["created_at"],
        }

    # ---------- 额度 ----------
    def line_view(self, customer_id, actor):
        return workflow.customer_line_view(self.storage, customer_id)

    def line_review(self, customer_id, body, actor):
        return workflow.review_line(self.storage, self.clock, customer_id, actor=actor["id"])

    def line_adjust(self, customer_id, body, actor):
        line = workflow.adjust_line(
            self.storage, self.clock, customer_id,
            yuan_to_cents(body["new_limit_yuan"]),
            body["reason_code"], body["reason_detail"], actor=actor["id"],
        )
        return {"total_limit_yuan": cents_to_yuan(line["total_limit_cents"]),
                "available_yuan": cents_to_yuan(line["available_cents"]),
                "status": line["status"]}

    # ---------- 提款 ----------
    def withdraw(self, body, actor):
        return workflow.request_withdrawal(
            self.storage, self.clock, body["customer_id"], body["amount_yuan"],
            merchant=body.get("merchant"), usage_purpose=body.get("usage_purpose"),
            idempotency_key=body.get("idempotency_key"),
            auto_disburse=body.get("auto_disburse", True), actor=actor["id"],
        )

    def withdrawal_view(self, withdrawal_id, actor):
        row = self.storage.get_withdrawal(withdrawal_id)
        if row is None:
            raise NotFound("提款不存在")
        return {
            "id": row["id"], "amount_yuan": cents_to_yuan(row["amount_cents"]),
            "status": row["status"], "merchant_id": row["merchant_id"],
            "merchant_name": row["merchant_name"], "usage_purpose": row["usage_purpose"],
            "disbursed_at": row["disbursed_at"],
            "hold_reasons": [
                {"trigger_code": h["trigger_code"], "manual_case_id": h["manual_case_id"]}
                for h in self.storage.list_hold_reasons(withdrawal_id)
            ],
        }

    def submit_document(self, withdrawal_id, body, actor):
        document = {
            "doc_type": body["doc_type"],
            "claimed_amount_cents": (
                yuan_to_cents(body["claimed_amount_yuan"])
                if body.get("claimed_amount_yuan") is not None else None
            ),
            "claimed_merchant": body.get("claimed_merchant"),
            "claimed_purpose": body.get("claimed_purpose"),
        }
        return workflow.submit_usage_document(
            self.storage, self.clock, withdrawal_id, document, actor=actor["id"]
        )

    def disburse(self, withdrawal_id, body, actor):
        return workflow.disburse_reserved(self.storage, self.clock, withdrawal_id)

    def refund(self, withdrawal_id, body, actor):
        return workflow.merchant_refund(
            self.storage, self.clock, withdrawal_id, body["amount_yuan"],
            route=body.get("route", "merchant_settlement"), ref_id=body.get("ref_id"),
        )

    def cancel_installment(self, withdrawal_id, body, actor):
        return workflow.installment_cancel(
            self.storage, self.clock, withdrawal_id,
            route=(body or {}).get("route", "merchant_cancel"),
            amount_yuan=(body or {}).get("amount_yuan"),
        )

    # ---------- 贷款 / 还款 ----------
    def loan_view(self, loan_id, actor):
        view = workflow.loan_view(self.storage, loan_id)
        view["annual_rate"] = view["annual_rate"]
        for key in ("principal_cents", "outstanding_principal_cents",
                    "outstanding_interest_cents", "total_interest_cents",
                    "total_cost_cents", "overdue_cents"):
            view[key.replace("_cents", "_yuan")] = cents_to_yuan(view[key])
        return view

    def repay(self, loan_id, body, actor):
        return workflow.repay(
            self.storage, self.clock, loan_id, body["amount_yuan"],
            route=body.get("route", "customer_account"), ref_id=body.get("ref_id"),
        )

    def loan_ledger(self, loan_id, actor):
        entries = self.storage.ledger_entries(loan_id=loan_id)
        return {"entries": [_ledger_json(e) for e in entries]}

    def request_hardship(self, loan_id, body, actor):
        return workflow.request_hardship(
            self.storage, self.clock, loan_id, body["plan_type"],
            reason=body.get("reason", ""), actor=actor["id"], plan=body.get("plan"),
        )

    def collection_contact(self, loan_id, body, actor):
        return workflow.collection_contact(
            self.storage, self.clock, loan_id, body["channel"], actor=actor["id"]
        )

    def collection_log(self, loan_id, actor):
        loan = self.storage.get_loan(loan_id)
        if loan is None:
            raise NotFound("贷款不存在")
        return {"contacts": self.storage.collection_contacts(loan["customer_id"])}

    # ---------- 人工案件 ----------
    def list_cases(self, query, actor):
        status = query.get("status")
        return {"cases": self.storage.list_manual_cases(status=status)}

    def decide_case(self, case_id, body, actor):
        return workflow.decide_manual_case(
            self.storage, self.clock, case_id, body["decision"],
            decided_by=actor["id"], rationale=body["rationale"],
            plan=body.get("plan"),
            reduced_amount_cents=(
                yuan_to_cents(body["reduced_amount_yuan"])
                if body.get("reduced_amount_yuan") is not None else None
            ),
        )

    # ---------- 合规 ----------
    def replay(self, application_id, actor):
        return workflow.replay_assessment(self.storage, application_id)

    def access_log(self, customer_id, actor):
        return {"accesses": self.storage.list_snapshot_access(customer_id)}

    def consents_view(self, customer_id, actor):
        return {"consents": self.storage.list_consents(customer_id)}

    # ---------- 营销 ----------
    def marketing_eligibility(self, customer_id, actor):
        return workflow.marketing_eligibility(
            self.storage, customer_id, self.clock.now().isoformat()
        )


def _ledger_json(entry):
    out = dict(entry)
    if out.get("detail"):
        out["detail"] = json.loads(out["detail"])
    out["amount_yuan"] = cents_to_yuan(out["amount_cents"])
    out["principal_balance_after_yuan"] = cents_to_yuan(out["principal_balance_after_cents"])
    return out


# (method, regex, app_method, allowed_roles, body_required, ownership_field, owner_kind)
# ownership_field：请求体内的客户字段；owner_kind：路径资源 ID 的属主解析类型
ROUTES = [
    ("POST", r"^/v1/consents/grant$", "grant_consent", {ROLE_CUSTOMER, ROLE_SYSTEM}, True, "customer_id", None),
    ("POST", r"^/v1/consents/revoke$", "revoke_consent", {ROLE_CUSTOMER, ROLE_SYSTEM}, True, "customer_id", None),
    ("POST", r"^/v1/snapshots$", "ingest_snapshot", {ROLE_SYSTEM}, True, "customer_id", None),

    ("POST", r"^/v1/applications$", "apply", {ROLE_CUSTOMER, ROLE_SYSTEM}, True, "customer_id", None),
    ("POST", r"^/v1/applications/([^/]+)/offer$", "offer", {ROLE_SYSTEM, ROLE_RISK}, True, None, None),
    ("GET", r"^/v1/applications/([^/]+)/assessment$", "assessment",
     {ROLE_CUSTOMER, ROLE_RISK, ROLE_COMPLIANCE, ROLE_SYSTEM}, False, None, "application"),

    ("GET", r"^/v1/customers/([^/]+)/line$", "line_view",
     {ROLE_CUSTOMER, ROLE_RISK, ROLE_COMPLIANCE}, False, None, "customer"),
    ("POST", r"^/v1/customers/([^/]+)/line/review$", "line_review", {ROLE_RISK}, False, None, None),
    ("POST", r"^/v1/customers/([^/]+)/line/adjust$", "line_adjust", {ROLE_RISK}, True, None, None),

    ("POST", r"^/v1/withdrawals$", "withdraw", {ROLE_CUSTOMER, ROLE_SYSTEM}, True, "customer_id", None),
    ("GET", r"^/v1/withdrawals/([^/]+)$", "withdrawal_view",
     {ROLE_CUSTOMER, ROLE_RISK, ROLE_COMPLIANCE}, False, None, "withdrawal"),
    ("POST", r"^/v1/withdrawals/([^/]+)/documents$", "submit_document",
     {ROLE_CUSTOMER, ROLE_RISK, ROLE_SYSTEM}, True, None, "withdrawal"),
    ("POST", r"^/v1/withdrawals/([^/]+)/disburse$", "disburse",
     {ROLE_SYSTEM, ROLE_RISK}, False, None, None),
    ("POST", r"^/v1/withdrawals/([^/]+)/refund$", "refund", {ROLE_SYSTEM}, True, None, None),
    ("POST", r"^/v1/withdrawals/([^/]+)/cancel$", "cancel_installment", {ROLE_SYSTEM}, False, None, None),

    ("GET", r"^/v1/loans/([^/]+)$", "loan_view",
     {ROLE_CUSTOMER, ROLE_RISK, ROLE_COMPLIANCE}, False, None, "loan"),
    ("GET", r"^/v1/loans/([^/]+)/ledger$", "loan_ledger",
     {ROLE_CUSTOMER, ROLE_RISK, ROLE_COMPLIANCE}, False, None, "loan"),
    ("POST", r"^/v1/loans/([^/]+)/repay$", "repay", {ROLE_CUSTOMER, ROLE_SYSTEM}, True, None, "loan"),
    ("POST", r"^/v1/loans/([^/]+)/hardship$", "request_hardship",
     {ROLE_CUSTOMER, ROLE_RISK}, True, None, "loan"),
    ("POST", r"^/v1/loans/([^/]+)/collection-contact$", "collection_contact",
     {ROLE_COLLECTOR, ROLE_RISK}, True, None, None),
    ("GET", r"^/v1/loans/([^/]+)/collection-contacts$", "collection_log",
     {ROLE_COMPLIANCE, ROLE_RISK}, False, None, None),

    ("GET", r"^/v1/cases$", "list_cases", {ROLE_RISK, ROLE_COMPLIANCE}, False, None, None),
    ("POST", r"^/v1/cases/([^/]+)/decide$", "decide_case", {ROLE_RISK}, True, None, None),

    ("GET", r"^/v1/applications/([^/]+)/replay$", "replay", {ROLE_COMPLIANCE}, False, None, None),
    ("GET", r"^/v1/customers/([^/]+)/snapshot-access$", "access_log", {ROLE_COMPLIANCE}, False, None, None),
    ("GET", r"^/v1/customers/([^/]+)/consents$", "consents_view",
     {ROLE_COMPLIANCE, ROLE_CUSTOMER}, False, None, "customer"),

    ("GET", r"^/v1/marketing/customers/([^/]+)/eligibility$", "marketing_eligibility",
     {ROLE_MARKETING}, False, None, None),
]

# 营销角色可见的路径前缀白名单（纵深防御：即使路由表配错也不泄露风控数据）
MARKETING_ALLOWED_PREFIXES = ("/v1/marketing/",)


class Handler(BaseHTTPRequestHandler):
    app = App()  # 模块级默认；create_server 可注入自定义 App

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        query = self._query_params()
        if path == "/health":
            self._write_json(200, {"status": "ok", "service": SERVICE_ID,
                                   "name": SERVICE_NAME, "api_version": API_VERSION})
            return
        for route in ROUTES:
            route_method, pattern, method_name, roles, body_required, owner_field, owner_kind = route
            if route_method != method:
                continue
            match = re.match(pattern, path)
            if not match:
                continue
            actor = self._actor()
            if isinstance(actor, DomainError):
                self._write_error(actor)
                return
            if not self._authorize(actor, roles, path):
                return
            body = {}
            if body_required or method == "POST":
                try:
                    body = self._read_body()
                except DomainError as exc:
                    self._write_error(exc)
                    return
            if owner_field and body.get(owner_field) and actor["role"] == ROLE_CUSTOMER:
                if body[owner_field] != actor["id"]:
                    self._write_error(DomainError(
                        "只能操作本人资源", status_code=403, code="ownership_denied"
                    ))
                    return
            try:
                args = match.groups()
                if args and not self._check_path_ownership(owner_kind, args[0], actor):
                    return
                target = getattr(self.app, method_name)
                if not args:
                    result = target(body, actor) if method == "POST" else target(query, actor)
                else:
                    resource_id = args[0]
                    if method == "POST":
                        result = target(resource_id, body, actor)
                    else:
                        result = target(resource_id, actor)
                self._write_json(200, result)
            except DomainError as exc:
                self._write_error(exc)
            return
        self._write_json(404, {"error": "not_found"})

    def _check_path_ownership(self, owner_kind, resource_id, actor):
        """customer 角色只能访问归属于本人的路径资源；不匹配返回 403，不存在返回 404。"""
        if not owner_kind or actor["role"] != ROLE_CUSTOMER:
            return True
        storage = self.app.storage
        if owner_kind == "customer":
            owner_id = resource_id  # 资源是否存在交由处理器返回 404
        else:
            if owner_kind == "loan":
                row = storage.get_loan(resource_id)
            elif owner_kind == "withdrawal":
                row = storage.get_withdrawal(resource_id)
            elif owner_kind == "application":
                row = storage.get_application(resource_id)
            else:
                return True
            if row is None:
                self._write_error(NotFound("资源不存在"))
                return False
            owner_id = row["customer_id"]
        if owner_id != actor["id"]:
            self._write_error(DomainError(
                "只能访问本人资源", status_code=403, code="ownership_denied"
            ))
            return False
        return True

    def _authorize(self, actor, roles, path):
        if actor["role"] not in roles:
            if actor["role"] == ROLE_MARKETING and not path.startswith(MARKETING_ALLOWED_PREFIXES):
                self._write_json(403, {"error": "marketing_scope_denied",
                                       "detail": "营销角色禁止访问风控与授信数据"})
                return False
            self._write_json(403, {"error": "forbidden",
                                   "role": actor["role"], "allowed_roles": sorted(roles)})
            return False
        return True

    def _actor(self):
        role = self.headers.get("X-Actor-Role", ROLE_SYSTEM)
        actor_id = self.headers.get("X-Actor-Id", "system")
        if role not in ALL_ROLES:
            return DomainError(f"未知角色：{role}", status_code=401, code="unknown_role")
        return {"id": actor_id, "role": role}

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("请求体必须是 JSON", status_code=400, code="invalid_json")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象", status_code=400, code="invalid_body")
        return data

    def _query_params(self):
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qs

        parsed = parse_qs(self.path.split("?", 1)[1])
        return {k: v[-1] for k, v in parsed.items()}

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, exc):
        status = getattr(exc, "status_code", None) or getattr(exc, "status", 400)
        code = getattr(exc, "code", "domain_error")
        self._write_json(status, {"error": code, "message": str(exc), "details": getattr(exc, "details", {})})

    def log_message(self, *_args):
        return


def create_server(host, port, app=None):
    handler = Handler
    if app is not None:
        class BoundHandler(Handler):
            pass
        BoundHandler.app = app
        handler = BoundHandler
    server = ThreadingHTTPServer((host, port), handler)
    server.app = app or Handler.app
    return server
