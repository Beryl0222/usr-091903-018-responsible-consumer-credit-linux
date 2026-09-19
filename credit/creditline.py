"""额度台账：提款预留、用途凭证核验、放款、原路冲减与还款。

关键不变量（在客户锁 + BEGIN IMMEDIATE 事务内成立）：
    reserved_cents + outstanding_principal_cents <= limit_cents
并发提款逐笔在同一事务内核减额度，因此不会突破仍有效的总额度。

资金性质严格区分：
- repayment          客户主动还款（利息→本金），计入 paid_*；
- merchant_refund    商户退款，按原路冲减本金，绝不算客户还款；
- installment_cancel 分期取消，同样原路冲减/释放，不产生还款记录。
"""

from datetime import date
from decimal import Decimal

from credit import clock
from credit.errors import conflict, not_found, validation_error
from credit.money import build_schedule
from credit.store import Store, new_id

# 与消费贷用途冲突的凭证类目（套现/理财/还贷/首付等）
PROHIBITED_CATEGORIES = {
    "cash_out", "securities", "investment", "loan_repayment",
    "mortgage_down_payment", "other_financial",
}

# 凭证金额与提款金额允许偏差 10%
EVIDENCE_TOLERANCE = Decimal("0.10")


class CreditLineService:
    def __init__(self, store: Store, consents=None):
        self.store = store
        self.consents = consents

    # --- 账户视图 -------------------------------------------------------

    def get_account(self, account_id: str) -> dict:
        account = self.store.row_to_dict(self.store.require("credit_accounts", account_id))
        account["available_cents"] = self.available_cents(account)
        return account

    @staticmethod
    def available_cents(account) -> int:
        return int(account["limit_cents"]) - int(account["reserved_cents"]) - int(account["outstanding_cents"])

    def limit_history(self, account_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM limit_change_events WHERE account_id=? ORDER BY created_at",
            (account_id,),
        )
        return [self.store.row_to_dict(r) for r in rows]

    # --- 提款预留 -------------------------------------------------------

    def reserve(self, customer_id: str, account_id: str, amount_yuan,
                declared_purpose: str, merchant_id: str | None = None,
                merchant_category: str | None = None) -> dict:
        from credit.money import yuan_to_cents
        try:
            amount = yuan_to_cents(amount_yuan)
        except (ValueError, ArithmeticError):
            raise validation_error("提款金额格式不正确")
        if amount <= 0:
            raise validation_error("提款金额必须大于零")
        if not declared_purpose:
            raise validation_error("提款必须声明用途")

        with self.store.customer_lock(customer_id), self.store.transaction():
            account = self.store.require("credit_accounts", account_id)
            if account["customer_id"] != customer_id:
                raise not_found("账户不存在")
            if account["status"] != "active":
                raise conflict(f"账户当前为 {account['status']}，不能发起新提款")
            available = self.available_cents(account)
            if amount > available:
                raise conflict(
                    "提款金额超过仍有效可用额度",
                    {"requested_cents": amount, "available_cents": available},
                )
            now = clock.now_iso()
            drawdown = {
                "id": new_id("drw"),
                "account_id": account_id,
                "customer_id": customer_id,
                "amount_cents": amount,
                "merchant_id": merchant_id,
                "merchant_category": merchant_category,
                "declared_purpose": declared_purpose,
                "status": "evidence_pending",
                "evidence": None,
                "hold_reasons": None,
                "reserved_at": now,
                "disbursed_at": None,
                "cancelled_at": None,
                "created_at": now,
            }
            self.store.insert("drawdowns", drawdown)
            self._bump_reserved(account_id, amount)
        return self.store.row_to_dict(self.store.require("drawdowns", drawdown["id"]))

    # --- 用途凭证核验 ---------------------------------------------------

    def submit_evidence(self, drawdown_id: str, evidence: dict) -> dict:
        """提交并核验用途凭证；冲突时暂停该笔提款并立案，不放款。"""
        with self.store.transaction():
            drw = self.store.require("drawdowns", drawdown_id)
            if drw["status"] not in ("evidence_pending", "reserved"):
                raise conflict(f"当前状态 {drw['status']} 不能提交凭证")

            problems = self._verify_evidence(drw, evidence)
            stored = Store.dumps({
                "items": evidence.get("items", []),
                "submitted_at": clock.now_iso(),
                "problems": problems,
            })
            if problems:
                self.store._conn.execute(
                    "UPDATE drawdowns SET status='suspended', evidence=?, hold_reasons=? WHERE id=?",
                    (stored, Store.dumps(["purpose_evidence_conflict"]), drawdown_id),
                )
                self._raise_signal_and_case(drw, "purpose_evidence_conflict",
                                            {"problems": problems, "evidence": evidence})
            else:
                self.store._conn.execute(
                    "UPDATE drawdowns SET status='ready', evidence=? WHERE id=?",
                    (stored, drawdown_id),
                )
        return self.store.row_to_dict(self.store.require("drawdowns", drawdown_id))

    def _verify_evidence(self, drw, evidence: dict) -> list[str]:
        problems = []
        items = evidence.get("items") or []
        if not items:
            return ["evidence_empty"]
        total = 0
        for item in items:
            category = item.get("category")
            total += int(item.get("amount_cents", 0))
            if category in PROHIBITED_CATEGORIES:
                problems.append(f"prohibited_category:{category}")
            if drw["merchant_category"] and category and category != drw["merchant_category"]:
                problems.append(f"category_mismatch:{category}!={drw['merchant_category']}")
        amount = int(drw["amount_cents"])
        if total <= 0:
            problems.append("evidence_amount_zero")
        else:
            deviation = abs(Decimal(total - amount)) / Decimal(amount)
            if deviation > EVIDENCE_TOLERANCE:
                problems.append(f"amount_mismatch:evidence={total},drawdown={amount}")
        return problems

    # --- 放款 -----------------------------------------------------------

    def disburse(self, drawdown_id: str) -> dict:
        """凭证通过后放款：预留转占用，生成贷款与还款计划。"""
        with self.store.customer_lock(self._drawdown_owner(drawdown_id)), self.store.transaction():
            drw = self.store.require("drawdowns", drawdown_id)
            account = self.store.require("credit_accounts", drw["account_id"])
            if drw["status"] == "suspended":
                raise conflict("提款已被风险暂停，等待人工决定", {"hold_reasons": Store.loads(drw["hold_reasons"])})
            if drw["status"] != "ready":
                raise conflict(f"提款状态 {drw['status']}，不能放款")
            if account["status"] != "active":
                raise conflict(f"账户已 {account['status']}，放款中止")

            amount = int(drw["amount_cents"])
            now = clock.now()
            start = now.date()
            schedule = build_schedule(amount, Decimal(account["annual_rate"]),
                                      int(account["term_months"]), start)
            loan = {
                "id": new_id("ln"),
                "drawdown_id": drawdown_id,
                "account_id": account["id"],
                "customer_id": drw["customer_id"],
                "principal_cents": amount,
                "annual_rate": account["annual_rate"],
                "term_months": len(schedule),
                "start_date": start.isoformat(),
                "schedule": Store.dumps(schedule),
                "status": "active",
                "paid_principal_cents": 0,
                "paid_interest_cents": 0,
                "refunded_principal_cents": 0,
                "created_at": now.isoformat(),
            }
            self.store.insert("loans", loan)
            self.store.insert("loan_events", {
                "id": new_id("evt"), "loan_id": loan["id"], "kind": "disbursement",
                "amount_cents": amount, "principal_delta_cents": 0,
                "detail": Store.dumps({"drawdown_id": drawdown_id}),
                "actor": "system", "created_at": now.isoformat(),
            })
            self.store._conn.execute(
                "UPDATE credit_accounts SET reserved_cents=reserved_cents-?, "
                "outstanding_cents=outstanding_cents+? WHERE id=?",
                (amount, amount, account["id"]),
            )
            self.store._conn.execute(
                "UPDATE drawdowns SET status='disbursed', disbursed_at=? WHERE id=?",
                (now.isoformat(), drawdown_id),
            )
        return self.store.row_to_dict(self.store.require("loans", loan["id"]))

    @staticmethod
    def _term_for(account) -> int:
        # 保留：账户期限已持久化在 term_months，放款直接读取
        return int(account["term_months"])

    # --- 取消（未放款，释放预留）----------------------------------------

    def cancel(self, drawdown_id: str, actor: str = "customer", reason: str = "customer_cancel") -> dict:
        owner = self._drawdown_owner(drawdown_id)
        with self.store.customer_lock(owner), self.store.transaction():
            drw = self.store.require("drawdowns", drawdown_id)
            if drw["status"] == "disbursed":
                raise conflict("已放款提款不能取消，请走退款/还款流程")
            if drw["status"] == "cancelled":
                raise conflict("提款已取消")
            amount = int(drw["amount_cents"])
            # 预留始终随取消释放（suspended 的人工驳回也走这里）
            self._bump_reserved(drw["account_id"], -amount)
            self.store._conn.execute(
                "UPDATE drawdowns SET status='cancelled', cancelled_at=? WHERE id=?",
                (clock.now_iso(), drawdown_id),
            )
        return self.store.row_to_dict(self.store.require("drawdowns", drawdown_id))

    # --- 原路冲减：商户退款 / 分期取消 ----------------------------------

    def merchant_refund(self, loan_id: str, amount_yuan, reference: str, actor: str = "merchant") -> dict:
        return self._reverse_principal(loan_id, amount_yuan, "merchant_refund", reference, actor)

    def installment_cancel(self, loan_id: str, amount_yuan, reference: str, actor: str = "merchant") -> dict:
        return self._reverse_principal(loan_id, amount_yuan, "installment_cancel", reference, actor)

    def _reverse_principal(self, loan_id: str, amount_yuan, kind: str, reference: str, actor: str) -> dict:
        from credit.money import yuan_to_cents
        amount = yuan_to_cents(amount_yuan)
        if amount <= 0:
            raise validation_error("冲减金额必须大于零")
        loan = self.store.require("loans", loan_id)
        owner = loan["customer_id"]
        with self.store.customer_lock(owner), self.store.transaction():
            loan = self.store.require("loans", loan_id)
            principal_outstanding = (int(loan["principal_cents"])
                                     - int(loan["paid_principal_cents"])
                                     - int(loan["refunded_principal_cents"]))
            if amount > principal_outstanding:
                raise conflict(
                    "冲减金额超过剩余本金",
                    {"amount_cents": amount, "principal_outstanding_cents": principal_outstanding},
                )
            self.store._conn.execute(
                "UPDATE loans SET refunded_principal_cents=refunded_principal_cents+? WHERE id=?",
                (amount, loan_id),
            )
            self.store._conn.execute(
                "UPDATE credit_accounts SET outstanding_cents=MAX(0, outstanding_cents-?) WHERE id=?",
                (amount, loan["account_id"]),
            )
            self.store.insert("loan_events", {
                "id": new_id("evt"), "loan_id": loan_id, "kind": kind,
                "amount_cents": amount, "principal_delta_cents": -amount,
                "detail": Store.dumps({"reference": reference, "original_route": True,
                                       "not_a_repayment": True}),
                "actor": actor, "created_at": clock.now_iso(),
            })
            self._reschedule_after_reversal(loan_id)
        return self.store.row_to_dict(self.store.require("loans", loan_id))

    def _reschedule_after_reversal(self, loan_id: str) -> None:
        """冲减后重算尚未到期的期次（已结清期次不动），期限不变、月供下降。"""
        loan = self.store.require("loans", loan_id)
        schedule = Store.loads(loan["schedule"])
        remaining_terms = [r for r in schedule if r["status"] != "paid"]
        remaining_principal = (int(loan["principal_cents"])
                               - int(loan["paid_principal_cents"])
                               - int(loan["refunded_principal_cents"]))
        if remaining_principal == 0:
            # 剩余本金已被退款/取消全部冲减：未到期期次作废，贷款结清
            for row in remaining_terms:
                row["status"] = "reversed"
            self.store._conn.execute(
                "UPDATE loans SET schedule=?, status='closed' WHERE id=?",
                (Store.dumps(schedule), loan_id),
            )
        elif remaining_terms:
            new_schedule = build_schedule(
                remaining_principal, Decimal(loan["annual_rate"]),
                len(remaining_terms), date.fromisoformat(remaining_terms[0]["due_date"]),
            )
            for old, new in zip(remaining_terms, new_schedule):
                old["payment_cents"] = new["payment_cents"]
                old["principal_cents"] = new["principal_cents"]
                old["interest_cents"] = new["interest_cents"]
            self.store._conn.execute(
                "UPDATE loans SET schedule=? WHERE id=?",
                (Store.dumps(schedule), loan_id),
            )

    # --- 客户还款 -------------------------------------------------------

    def repay(self, loan_id: str, amount_yuan, actor: str = "customer") -> dict:
        from credit.money import yuan_to_cents
        amount = yuan_to_cents(amount_yuan)
        if amount <= 0:
            raise validation_error("还款金额必须大于零")
        loan = self.store.require("loans", loan_id)
        with self.store.customer_lock(loan["customer_id"]), self.store.transaction():
            loan = self.store.require("loans", loan_id)
            schedule = Store.loads(loan["schedule"])
            due_total = sum(r["payment_cents"] - r.get("paid_cents", 0)
                            for r in schedule if r["status"] not in ("paid", "reversed"))
            if amount > due_total:
                raise conflict("还款金额超过剩余应还总额",
                               {"amount_cents": amount, "due_total_cents": due_total})
            left = amount
            paid_interest = paid_principal = 0
            for row in schedule:
                if row["status"] in ("paid", "reversed") or left <= 0:
                    continue
                outstanding = row["payment_cents"] - row.get("paid_cents", 0)
                take = min(left, outstanding)
                # 每期内先冲利息，再冲本金（剩余利息/本金挂在原到期金额上）
                interest_remaining = row["interest_cents"] - row.get("paid_interest_cents", 0)
                interest_part = min(take, interest_remaining)
                principal_part = take - interest_part
                row["paid_cents"] = row.get("paid_cents", 0) + take
                row["paid_interest_cents"] = row.get("paid_interest_cents", 0) + interest_part
                row["paid_principal_cents"] = row.get("paid_principal_cents", 0) + principal_part
                paid_interest += interest_part
                paid_principal += principal_part
                left -= take
                row["status"] = "paid" if row["paid_cents"] == row["payment_cents"] else "partial"
            self.store._conn.execute(
                "UPDATE loans SET paid_principal_cents=paid_principal_cents+?, "
                "paid_interest_cents=paid_interest_cents+? WHERE id=?",
                (paid_principal, paid_interest, loan_id),
            )
            self.store._conn.execute("UPDATE loans SET schedule=? WHERE id=?",
                                     (Store.dumps(schedule), loan_id))
            self.store._conn.execute(
                "UPDATE credit_accounts SET outstanding_cents=MAX(0, outstanding_cents-?) WHERE id=?",
                (paid_principal, loan["account_id"]),
            )
            self.store.insert("loan_events", {
                "id": new_id("evt"), "loan_id": loan_id, "kind": "repayment",
                "amount_cents": amount, "principal_delta_cents": -paid_principal,
                "detail": Store.dumps({"interest_cents": paid_interest,
                                       "principal_cents": paid_principal}),
                "actor": actor, "created_at": clock.now_iso(),
            })
            if all(r["status"] in ("paid", "reversed") for r in schedule):
                self.store._conn.execute(
                    "UPDATE loans SET status='settled' WHERE id=?", (loan_id,))
        return self.store.row_to_dict(self.store.require("loans", loan_id))

    def get_loan(self, loan_id: str, include_events: bool = True) -> dict:
        loan = self.store.row_to_dict(self.store.require("loans", loan_id))
        loan["schedule"] = Store.loads(loan["schedule"])
        if include_events:
            rows = self.store.query(
                "SELECT * FROM loan_events WHERE loan_id=? ORDER BY created_at", (loan_id,))
            loan["events"] = []
            for r in rows:
                event = self.store.row_to_dict(r)
                event["detail"] = Store.loads(event["detail"])
                loan["events"].append(event)
        return loan

    # --- 内部工具 -------------------------------------------------------

    def _drawdown_owner(self, drawdown_id: str) -> str:
        return self.store.require("drawdowns", drawdown_id)["customer_id"]

    def _bump_reserved(self, account_id: str, delta: int) -> None:
        self.store._conn.execute(
            "UPDATE credit_accounts SET reserved_cents=MAX(0, reserved_cents+?) WHERE id=?",
            (delta, account_id),
        )

    def _raise_signal_and_case(self, drw, code: str, payload: dict) -> dict:
        now = clock.now_iso()
        signal = {
            "id": new_id("sig"), "customer_id": drw["customer_id"],
            "account_id": drw["account_id"], "drawdown_id": drw["id"],
            "loan_id": None, "code": code, "severity": "block",
            "payload": Store.dumps(payload), "review_case_id": None, "created_at": now,
        }
        self.store.insert("signal_events", signal)
        case = {
            "id": new_id("rev"), "customer_id": drw["customer_id"],
            "account_id": drw["account_id"], "drawdown_id": drw["id"], "loan_id": None,
            "application_id": None, "signal_ids": Store.dumps([signal["id"]]),
            "topic": "drawdown", "status": "open", "decision": None,
            "new_limit_cents": None, "new_term_months": None, "new_annual_rate": None,
            "reason_detail": None, "reviewer": None, "decided_at": None,
            "created_at": now,
        }
        self.store.insert("review_cases", case)
        self.store._conn.execute(
            "UPDATE signal_events SET review_case_id=? WHERE id=?", (case["id"], signal["id"]))
        return case
