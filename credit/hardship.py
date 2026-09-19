"""困难协商。

客户提交困难申请后：
- 立即生成 topic=hardship 的人工复核案件（展期/重组由风控岗决定）；
- 协商期间（申请未结案或方案已批准）催收联系必须暂停（见 collections）。
"""

from credit import clock
from credit.errors import conflict, validation_error
from credit.store import Store, new_id

ACTIVE_STATUSES = ("submitted", "under_review", "granted")
ACTION_TYPES = ("extension", "restructure", "payment_holiday")


class HardshipService:
    def __init__(self, store: Store):
        self.store = store

    def submit(self, customer_id: str, requested_action: str, description: str,
               evidence: dict | None = None, loan_id: str | None = None,
               account_id: str | None = None) -> dict:
        if requested_action not in ACTION_TYPES:
            raise validation_error(f"困难申请类型必须是 {ACTION_TYPES} 之一")
        if loan_id:
            loan = self.store.require("loans", loan_id)
            if loan["customer_id"] != customer_id:
                raise conflict("贷款不属于该客户")
            if loan["status"] in ("settled", "closed"):
                raise conflict("贷款已结清，无需困难协商")
        now = clock.now_iso()
        case = {
            "id": new_id("rev"), "customer_id": customer_id,
            "account_id": account_id, "drawdown_id": None, "loan_id": loan_id,
            "application_id": None, "signal_ids": Store.dumps([]),
            "topic": "hardship", "status": "open", "decision": None,
            "new_limit_cents": None, "new_term_months": None, "new_annual_rate": None,
            "reason_detail": None, "reviewer": None, "decided_at": None,
            "created_at": now,
        }
        request = {
            "id": new_id("hrd"), "customer_id": customer_id, "loan_id": loan_id,
            "account_id": account_id, "requested_action": requested_action,
            "status": "under_review",
            "evidence": Store.dumps({"description": description, "items": evidence or [],
                                     "submitted_at": now}),
            "linked_case_id": case["id"], "reviewer": None,
            "created_at": now, "decided_at": None,
        }
        with self.store.transaction():
            self.store.insert("review_cases", case)
            self.store.insert("hardship_requests", request)
        return self.store.row_to_dict(request)

    def get(self, request_id: str) -> dict:
        row = self.store.row_to_dict(self.store.require("hardship_requests", request_id))
        row["evidence"] = Store.loads(row["evidence"])
        return row

    def list_for_customer(self, customer_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM hardship_requests WHERE customer_id=? ORDER BY created_at DESC",
            (customer_id,))
        return [self.store.row_to_dict(r) for r in rows]

    def active_for(self, customer_id: str, loan_id: str | None = None):
        """是否存在生效中的困难协商（用于催收闸门）。"""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        params = [customer_id, *ACTIVE_STATUSES]
        sql = f"SELECT * FROM hardship_requests WHERE customer_id=? AND status IN ({placeholders})"
        if loan_id:
            sql += " AND (loan_id=? OR loan_id IS NULL)"
            params.append(loan_id)
        sql += " ORDER BY created_at DESC LIMIT 1"
        return self.store.query_one(sql, params)
