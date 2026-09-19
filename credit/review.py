"""人工复核与困难协商决定。

只有风控岗位（risk_officer / admin）可以出具决定；
marketing 角色的任何请求都会被拒绝——营销目标永远不能覆盖风险结论。

决定类型：
- limit_reduction  降额（只能下调，不得低于当前在贷+仍保留金额，差额需先驳回相关提款）
- extension        展期（贷款期限延长，利率不变，重算还款计划，月供下降）
- restructure      重组（可调整期限与利率，重算计划，全部留痕）
- resume           恢复账户/暂停的未放款提款
- reject           驳回：授信阶段拒绝申请，或提款阶段取消提款并释放预留

系统不提供"上调额度"的人工通道；提额只能来自一次新的可负担性评估。
"""

from datetime import date
from decimal import Decimal

from credit import clock
from credit.errors import conflict, forbidden, validation_error
from credit.money import build_schedule
from credit.store import Store, new_id

RISK_ROLES = ("risk_officer", "admin")


class ReviewService:
    def __init__(self, store: Store):
        self.store = store

    def get_case(self, case_id: str) -> dict:
        case = self.store.row_to_dict(self.store.require("review_cases", case_id))
        case["signal_ids"] = Store.loads(case["signal_ids"])
        signals = []
        for sid in case["signal_ids"]:
            row = self.store.get("signal_events", sid)
            if row:
                item = self.store.row_to_dict(row)
                item["payload"] = Store.loads(item["payload"])
                signals.append(item)
        case["signals"] = signals
        return case

    def list_open_cases(self, customer_id: str | None = None) -> list[dict]:
        if customer_id:
            rows = self.store.query(
                "SELECT * FROM review_cases WHERE customer_id=? AND status='open' ORDER BY created_at",
                (customer_id,))
        else:
            rows = self.store.query(
                "SELECT * FROM review_cases WHERE status='open' ORDER BY created_at")
        return [self.store.row_to_dict(r) for r in rows]

    def decide(self, case_id: str, reviewer: str, role: str, decision: str,
               reason_detail: str, new_limit_yuan=None, new_term_months: int | None = None,
               new_annual_rate: str | None = None, pending_drawdowns: str = "reject") -> dict:
        if role not in RISK_ROLES:
            raise forbidden(
                "仅风控岗位可以出具风险复核决定，营销角色不得覆盖风险结论",
                {"actor_role": role, "required_roles": list(RISK_ROLES)},
            )
        if decision not in ("limit_reduction", "extension", "restructure", "resume", "reject"):
            raise validation_error(f"未知决定类型: {decision}")
        if not reason_detail:
            raise validation_error("决定必须填写理由，供客户披露与合规复核")

        with self.store.transaction():
            case = self.store.require("review_cases", case_id)
            if case["status"] != "open":
                raise conflict(f"案件已决定: {case['decision']}")
            customer_id = case["customer_id"]

        with self.store.customer_lock(customer_id), self.store.transaction():
            case = self.store.require("review_cases", case_id)
            if decision == "limit_reduction":
                self._limit_reduction(case, reviewer, new_limit_yuan, reason_detail, pending_drawdowns)
            elif decision == "extension":
                self._extension(case, reviewer, new_term_months, reason_detail)
            elif decision == "restructure":
                self._restructure(case, reviewer, new_term_months, new_annual_rate, reason_detail)
            elif decision == "resume":
                self._resume(case, reviewer, reason_detail)
            elif decision == "reject":
                self._reject(case, reviewer, reason_detail)

            self.store._conn.execute(
                "UPDATE review_cases SET status='decided', decision=?, reason_detail=?, "
                "reviewer=?, decided_at=? WHERE id=?",
                (decision, reason_detail, reviewer, clock.now_iso(), case_id),
            )
            if case["topic"] == "hardship":
                self.store._conn.execute(
                    "UPDATE hardship_requests SET status=?, linked_case_id=?, reviewer=?, decided_at=? "
                    "WHERE linked_case_id=?",
                    ("granted" if decision in ("extension", "restructure") else "rejected",
                     case_id, reviewer, clock.now_iso(), case_id),
                )
        return self.get_case(case_id)

    # --- 各决定的落地 ---------------------------------------------------

    def _limit_reduction(self, case, reviewer: str, new_limit_yuan, reason: str,
                         pending_drawdowns: str) -> None:
        from credit.money import yuan_to_cents
        if not case["account_id"]:
            raise validation_error("该案件未关联额度账户，不能降额")
        account = self.store.require("credit_accounts", case["account_id"])
        old_limit = int(account["limit_cents"])
        if new_limit_yuan is None:
            raise validation_error("降额决定必须给出新额度")
        new_limit = yuan_to_cents(new_limit_yuan)
        if new_limit >= old_limit:
            raise validation_error("降额只能低于当前额度，提额须重新进行可负担性评估")

        if pending_drawdowns == "reject":
            self._cancel_pending(case["account_id"])
            account = self.store.require("credit_accounts", case["account_id"])
        floor = int(account["outstanding_cents"]) + int(account["reserved_cents"])
        if new_limit < floor:
            raise conflict(
                "新额度不能低于当前在贷余额与仍保留金额之和",
                {"new_limit_cents": new_limit, "floor_cents": floor},
            )
        self.store._conn.execute(
            "UPDATE credit_accounts SET limit_cents=? WHERE id=?",
            (new_limit, account["id"]))
        self._limit_event(account["id"], old_limit, new_limit, "manual_limit_reduction",
                          reason, case["id"], reviewer)

    def _extension(self, case, reviewer: str, new_term_months, reason: str) -> None:
        loan = self._case_loan(case)
        if not new_term_months or new_term_months <= int(loan["term_months"]):
            raise validation_error("展期期限必须长于剩余期限")
        self._rebuild_loan(loan, new_term_months, loan["annual_rate"], "extension",
                           "extended", reviewer, reason)

    def _restructure(self, case, reviewer: str, new_term_months, new_annual_rate, reason: str) -> None:
        loan = self._case_loan(case)
        term = int(new_term_months or loan["term_months"])
        rate = str(new_annual_rate or loan["annual_rate"])
        try:
            Decimal(rate)
        except Exception:
            raise validation_error("重组利率格式不正确")
        if term == int(loan["term_months"]) and rate == loan["annual_rate"]:
            raise validation_error("重组方案与现状一致，无实际调整")
        self._rebuild_loan(loan, term, rate, "restructure",
                           "restructured", reviewer, reason)

    def _rebuild_loan(self, loan, term_months: int, annual_rate: str, event_kind: str,
                      new_status: str, reviewer: str, reason: str) -> None:
        schedule = Store.loads(loan["schedule"])
        unpaid = [r for r in schedule if r["status"] != "paid"]
        paid = [r for r in schedule if r["status"] == "paid"]
        remaining_principal = (int(loan["principal_cents"])
                               - int(loan["paid_principal_cents"])
                               - int(loan["refunded_principal_cents"]))
        if remaining_principal <= 0:
            raise conflict("贷款剩余本金为零，无需展期/重组")
        # 新期限覆盖剩余期数：未还期次按新条款展到新期限
        remaining_count = term_months - len(paid)
        if remaining_count <= 0:
            raise validation_error("新期限短于已还期数")
        start = date.fromisoformat(unpaid[0]["due_date"]) if unpaid else date.fromisoformat(loan["start_date"])
        new_rows = build_schedule(remaining_principal, Decimal(annual_rate), remaining_count, start)
        schedule = paid + new_rows
        self.store._conn.execute(
            "UPDATE loans SET schedule=?, annual_rate=?, term_months=?, status=? WHERE id=?",
            (Store.dumps(schedule), str(annual_rate), term_months, new_status, loan["id"]))
        self.store.insert("loan_events", {
            "id": new_id("evt"), "loan_id": loan["id"], "kind": event_kind,
            "amount_cents": 0, "principal_delta_cents": 0,
            "detail": Store.dumps({
                "old_term_months": loan["term_months"], "new_term_months": term_months,
                "old_annual_rate": loan["annual_rate"], "new_annual_rate": str(annual_rate),
                "remaining_principal_cents": remaining_principal, "reason": reason,
            }),
            "actor": reviewer, "created_at": clock.now_iso(),
        })

    def _resume(self, case, reviewer: str, reason: str) -> None:
        if case["account_id"]:
            self.store._conn.execute(
                "UPDATE credit_accounts SET status='active' WHERE id=?",
                (case["account_id"],))
            # 用途冲突的提款不随账户恢复：必须由该提款自己的案件单独决定
            if case["topic"] == "monitoring":
                self.store._conn.execute(
                    "UPDATE drawdowns SET status='evidence_pending' WHERE account_id=? "
                    "AND status='suspended' AND hold_reasons NOT LIKE '%purpose_evidence_conflict%'",
                    (case["account_id"],))
        if case["drawdown_id"] and case["topic"] == "drawdown":
            self.store._conn.execute(
                "UPDATE drawdowns SET status='ready', hold_reasons=NULL WHERE id=? "
                "AND status='suspended'",
                (case["drawdown_id"],))

    def _reject(self, case, reviewer: str, reason: str) -> None:
        if case["application_id"]:
            self.store._conn.execute(
                "UPDATE applications SET status='rejected' WHERE id=? AND status='manual_review'",
                (case["application_id"],))
        if case["drawdown_id"]:
            drw = self.store.require("drawdowns", case["drawdown_id"])
            if drw["status"] not in ("disbursed", "cancelled"):
                self.store._conn.execute(
                    "UPDATE credit_accounts SET reserved_cents=MAX(0, reserved_cents-?) WHERE id=?",
                    (int(drw["amount_cents"]), drw["account_id"]))
                self.store._conn.execute(
                    "UPDATE drawdowns SET status='cancelled', hold_reasons=NULL, cancelled_at=? WHERE id=?",
                    (clock.now_iso(), drw["id"]))
        # 监控类驳回不自动恢复账户：保持暂停，等待客户补充材料或重新评估
        if case["topic"] == "monitoring" and not case["drawdown_id"] and not case["application_id"]:
            self.store._conn.execute(
                "UPDATE credit_accounts SET status='suspended' WHERE id=?",
                (case["account_id"],))

    def _cancel_pending(self, account_id: str) -> None:
        rows = self.store.query(
            "SELECT * FROM drawdowns WHERE account_id=? AND status IN "
            "('reserved','evidence_pending','ready','suspended')", (account_id,))
        now = clock.now_iso()
        for r in rows:
            self.store._conn.execute(
                "UPDATE credit_accounts SET reserved_cents=MAX(0, reserved_cents-?) WHERE id=?",
                (int(r["amount_cents"]), account_id))
            self.store._conn.execute(
                "UPDATE drawdowns SET status='cancelled', cancelled_at=? WHERE id=?",
                (now, r["id"]))

    def _case_loan(self, case):
        loan_id = case["loan_id"]
        if not loan_id:
            raise validation_error("该案件未关联贷款，不能展期/重组")
        return self.store.require("loans", loan_id)

    def _limit_event(self, account_id, old, new, code, reason, case_id, reviewer) -> None:
        self.store.insert("limit_change_events", {
            "id": new_id("lce"), "account_id": account_id,
            "old_limit_cents": old, "new_limit_cents": new,
            "reason_code": code, "reason_detail": reason,
            "source": "manual_review", "review_case_id": case_id,
            "actor": reviewer, "created_at": clock.now_iso(),
        })
